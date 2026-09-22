import csv
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from itertools import product
from collections.abc import Sequence
from functools import lru_cache

from pathlib import Path
from typing import TypeAlias

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common

INPUT_CONFIG_FILE = common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv"
INPUT_CAPACITY_FILE = common.STAGE5_OUTPUT_DIR / "Constraint_Location_Counts_By_Slot_Size.csv"
INPUT_PREPARED = common.STAGE1_OUTPUT_DIR / "Location_Details_Prepared.csv"
INPUT_LOCATION_BEAM_MAP = common.STAGE1_OUTPUT_DIR / "Location_Beam_Map.csv"
INPUT_BEAM_HEIGHT_COORDS = common.STAGE1_OUTPUT_DIR / "Beam_Height_Coordinates.csv"
LAYOUT_OUTPUT_DIR = common.STAGE6_OUTPUT_DIR
# The pre-robust pass should keep every valid generated layout candidate rather
# than artificially chopping the search space down to a fixed-size shortlist.
PRE_ROBUST_LAYOUT_LIMIT = None
PRE_ROBUST_COMPOSITION_KEEP = 3
PRE_ROBUST_MAX_SELECTED_CONFIGS = 24
EXHAUSTIVE_SEARCH_CONFIG_LIMIT = 1000
STAGE6_CONFIG_LIMIT = 180
# Stage 6 should evaluate all valid configurations with K >= 3; the earlier 3-slot smoke focus was
# artificially restricting the search space and creating false negatives for larger valid families.
STAGE6_CONFIG_SLOT_SIZE_FOCUS = None
DEFAULT_TARGET_CONFIGS = "CFG_001-CFG_180"
# Keep the screening pass lightweight so K-value benchmarking can score a config quickly without
# spending the majority of the run on a combinatorial profile explosion.
# Larger slot families are assigned a stricter family-dominant stream and a much lower shortlist cap
# so Stage 6 remains practical without throwing away the legal exact-fill semantics.
PROFILE_CANDIDATE_LIMIT = 20
PROFILE_GENERATION_TIMEOUT_SECONDS = 60.0
RACK_SEARCH_TIMEOUT_SECONDS = 60.0
FAST_FAIL_PROFILE_PROBE_SECONDS = 12.0
FAST_FAIL_PROFILE_PROBE_FAMILY_SIZE = 7
_LAST_STAGE6_TIMEOUTS: dict[str, bool] = {
    "profile_generation": False,
    "rack_search": False,
}

class Stage6ProfileGenerationTimeout(RuntimeError):
    """Raised when Stage 6 profile generation exceeds the configured per-config limit."""


SummaryRow: TypeAlias = dict[str, str]
DetailRows: TypeAlias = list[dict[str, str]]
LayoutSignature: TypeAlias = tuple[str, ...]
StyleCandidate: TypeAlias = tuple[SummaryRow, DetailRows, DetailRows, LayoutSignature]
ConfigStyleBundle: TypeAlias = tuple[str, list[StyleCandidate]]
SlotSizeSequence: TypeAlias = Sequence[float | int] | None
_LAST_STAGE6_STEP_TIMINGS: dict[str, float] = {
    "profile_generation": 0.0,
    "profile_shortlist": 0.0,
    "rack_search": 0.0,
}
ProfileRequirementPriorityKey: TypeAlias = tuple[
    int,
    int,
    int,
    int,
    int,
    int,
    int,
    tuple[int, ...],
    tuple[int, ...],
    int,
    int,
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    # Keep CSV read behavior consistent with shared pipeline helpers.
    return common._read_csv(path)


def _write_csv_preserve(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    # Keep full schema for diagnostic/interpretability columns.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _fallback_path(path: Path, suffix: str = "_Backup") -> Path:
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def _write_csv_preserve_with_fallback(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    try:
        _write_csv_preserve(path, fieldnames, rows)
        return path
    except PermissionError:
        fallback = _fallback_path(path)
        _write_csv_preserve(fallback, fieldnames, rows)
        return fallback


def _write_csv_clean_with_fallback(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    try:
        common._write_csv_clean(path, fieldnames, rows)
        return path
    except PermissionError:
        fallback = _fallback_path(path)
        common._write_csv_clean(fallback, fieldnames, rows)
        return fallback


def _candidate_configs() -> list[dict[str, str]]:
    """Read all Stage 4 candidate configurations."""
    return _read_csv(INPUT_CONFIG_FILE)


def _normalize_config_id(value: str) -> str:
    """Normalize a config reference like CFG_001 or 001 to a canonical CFG_001 label."""
    text = str(value or "").strip()
    if not text:
        return ""
    cleaned = str(text).upper().strip()
    if cleaned.startswith("CFG_"):
        suffix = cleaned.replace("CFG_", "", 1)
        if suffix.isdigit():
            return f"CFG_{int(suffix):03d}"
        return cleaned
    if cleaned.startswith("CFG") and cleaned[3:].isdigit():
        suffix = cleaned[3:]
        return f"CFG_{int(suffix):03d}"
    if text.isdigit():
        return f"CFG_{int(text):03d}"
    return text.upper()


def _parse_target_config_ids(raw: str | None) -> list[str]:
    """Parse config IDs from a string while preserving the explicit request order."""
    text = str(raw or "").strip()
    if not text:
        return []

    selected: list[str] = []
    seen: set[str] = set()
    for token in text.split(","):
        piece = str(token).strip()
        if not piece:
            continue
        if "-" in piece:
            start_text, end_text = piece.split("-", 1)
            start_id = int(_normalize_config_id(start_text).replace("CFG_", ""))
            end_id = int(_normalize_config_id(end_text).replace("CFG_", ""))
            for config_number in range(min(start_id, end_id), max(start_id, end_id) + 1):
                normalized = f"CFG_{config_number:03d}"
                if normalized not in seen:
                    seen.add(normalized)
                    selected.append(normalized)
            continue
        normalized = _normalize_config_id(piece)
        if normalized and normalized not in seen:
            seen.add(normalized)
            selected.append(normalized)
    return selected


def _resolve_random_target_config_ids(sample_count: int = 10, configs: list[dict[str, str]] | None = None) -> list[str]:
    """Return a random sample of config IDs, preferring one representative per K value when available."""
    rows = list(configs if configs is not None else _candidate_configs())
    if not rows:
        return []

    by_k: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        config_id = str(row.get("Config_ID", "")).strip()
        if not config_id:
            continue
        raw_k = str(row.get("K", "")).strip()
        try:
            k_value = int(float(raw_k))
        except ValueError:
            continue
        by_k[k_value].append(_normalize_config_id(config_id))

    if not by_k:
        return []

    k_values = sorted(by_k)
    if len(k_values) <= max(1, sample_count):
        chosen_k_values = k_values
    else:
        chosen_k_values = sorted(random.sample(k_values, max(1, sample_count)))

    chosen_ids: list[str] = []
    seen: set[str] = set()
    for k_value in chosen_k_values:
        options = by_k.get(k_value, [])
        if not options:
            continue
        selected = random.choice(options)
        if selected not in seen:
            chosen_ids.append(selected)
            seen.add(selected)

    if len(chosen_ids) < max(1, sample_count):
        fallback_ids = [
            _normalize_config_id(str(row.get("Config_ID", "")))
            for row in rows
            if _normalize_config_id(str(row.get("Config_ID", ""))) not in seen
        ]
        random.shuffle(fallback_ids)
        for config_id in fallback_ids:
            if config_id not in seen:
                chosen_ids.append(config_id)
                seen.add(config_id)
            if len(chosen_ids) >= max(1, sample_count):
                break

    return chosen_ids[: max(1, sample_count)]


def _target_config_ids_from_script_args() -> list[str]:
    """Allow a quick one-off override like: python 06_layout_generation.py CFG_001 or CFG_001,CFG_003.

    You can also request a random sample with: python 06_layout_generation.py random or random:10.
    """
    if len(sys.argv) <= 1:
        return []
    joined_args = " ".join(sys.argv[1:]).strip()
    if not joined_args or joined_args.startswith("-"):
        return []

    normalized = joined_args.strip().lower()
    if normalized == "random":
        return _resolve_random_target_config_ids(sample_count=10)
    if normalized.startswith("random:"):
        try:
            sample_count = int(normalized.split(":", 1)[1].strip())
        except ValueError:
            sample_count = 10
        return _resolve_random_target_config_ids(sample_count=max(1, sample_count))
    return _parse_target_config_ids(joined_args)


def _target_config_ids_from_environment() -> list[str]:
    """Return config IDs selected via CLI args, then environment override, then the script default.

    Examples: python 06_layout_generation.py CFG_001, CFG_006, 001-007
    You can also set PIPELINE_TARGET_CONFIGS=random or PIPELINE_TARGET_CONFIGS=random:10.
    """
    script_args = _target_config_ids_from_script_args()
    if script_args:
        return script_args

    raw = os.environ.get("PIPELINE_TARGET_CONFIGS", "").strip()
    if raw:
        normalized = raw.strip().lower()
        if normalized == "random":
            return _resolve_random_target_config_ids(sample_count=10)
        if normalized.startswith("random:"):
            try:
                sample_count = int(normalized.split(":", 1)[1].strip())
            except ValueError:
                sample_count = 10
            return _resolve_random_target_config_ids(sample_count=max(1, sample_count))
        return _parse_target_config_ids(raw)

    if DEFAULT_TARGET_CONFIGS:
        return _parse_target_config_ids(str(DEFAULT_TARGET_CONFIGS))
    return []


def _candidate_configs_for_exhaustive_search(configs: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    """Sample a small random subset of candidate configs for the exhaustive exact-fill search.

    For the current smoke-test baseline we intentionally focus on exact 3-slot-size configurations,
    because those are the manageable family size for validating Stage 6 behavior quickly. Larger
    families remain available behind an easy opt-out but are not used by default during the baseline
    validation run.
    """
    rows = list(configs if configs is not None else _candidate_configs())
    if not rows:
        return []

    target_configs = _target_config_ids_from_environment()
    if target_configs:
        target_set = set(target_configs)
        ordered_targets = {config_id: index for index, config_id in enumerate(target_configs)}
        rows = [
            row for row in rows
            if _normalize_config_id(str(row.get("Config_ID", ""))) in target_set
        ]
        rows.sort(key=lambda row: ordered_targets.get(
            _normalize_config_id(str(row.get("Config_ID", ""))),
            len(ordered_targets),
        ))

    if not rows:
        return []

    def _slot_count(row: dict[str, str]) -> int:
        raw = str(row.get("Slot_Sizes", "")).strip()
        if not raw:
            return 0
        decoded = common._decode_excel_text(raw)
        if not decoded:
            return 0
        return len([item for item in decoded.split(",") if str(item).strip()])

    if not target_configs and STAGE6_CONFIG_SLOT_SIZE_FOCUS is not None:
        focus = int(STAGE6_CONFIG_SLOT_SIZE_FOCUS)
        filtered_rows = [
            row
            for row in rows
            if str(row.get("Config_ID", "")).strip()
            and 3 <= _slot_count(row) <= focus + 2
        ]
    else:
        filtered_rows = [
            row
            for row in rows
            if str(row.get("Config_ID", "")).strip()
            and str(row.get("Slot_Sizes", "")).strip()
            and _slot_count(row) >= 2
        ]
    if not filtered_rows:
        filtered_rows = [
            row
            for row in rows
            if str(row.get("Config_ID", "")).strip() and str(row.get("Slot_Sizes", "")).strip()
        ]

    limit = min(STAGE6_CONFIG_LIMIT, len(filtered_rows), EXHAUSTIVE_SEARCH_CONFIG_LIMIT)
    return filtered_rows[:limit]


def _capacity_rows_by_config() -> dict[str, list[dict[str, str]]]:
    # Group Stage 5 constraint rows by configuration ID.
    rows = _read_csv(INPUT_CAPACITY_FILE)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("Config_ID", "")).strip()].append(row)
    return grouped


def _slot_sizes_from_capacity(rows: list[dict[str, str]]) -> list[float]:
    # Extract unique representative slot sizes for reporting fields.
    sizes = {
        common._to_float(row.get("Representative_Slot_Size"))
        for row in rows
        if common._to_float(row.get("Representative_Slot_Size")) is not None
    }
    return sorted(float(value) for value in sizes if value is not None)


def _worst_case_exact_counts(rows: list[dict[str, str]]) -> dict[float, int]:
    """Derive exact required counts by slot size from worst-case cumulative constraints."""
    cumulative_by_size: dict[float, int] = defaultdict(int)
    for row in rows:
        slot_size = common._to_float(row.get("Representative_Slot_Size"))
        required = common._to_float(
            row.get("Min_Required_Locations_At_Or_Above_Size")
            or row.get("Cumulative_Assigned_SKUs_At_Or_Above_Size")
        )
        if slot_size is None or required is None:
            continue
        cumulative_by_size[slot_size] = max(cumulative_by_size.get(slot_size, 0), int(round(required)))

    # Convert cumulative at-or-above counts into exact per-size requirements.
    ordered_sizes = sorted(cumulative_by_size)
    exact_counts: dict[float, int] = {}
    for index, slot_size in enumerate(ordered_sizes):
        next_size = ordered_sizes[index + 1] if index + 1 < len(ordered_sizes) else None
        next_required = cumulative_by_size.get(next_size, 0) if next_size is not None else 0
        exact_counts[slot_size] = max(cumulative_by_size[slot_size] - next_required, 0)

    return exact_counts


def _base_exact_counts(rows: list[dict[str, str]]) -> dict[float, int]:
    # Layout generation should be independent of SKU-scenario stress tests.
    # Use base demand rows when available; robustness checks handle low/high demand.
    base_rows = [
        row
        for row in rows
        if str(row.get("SKU_Scenario", "")).strip() == "Base_Count"
    ]
    exact_counts = _worst_case_exact_counts(base_rows) if base_rows else _worst_case_exact_counts(rows)
    return common._enforce_occupied_location_target(exact_counts)


def _profile_is_feasible_exact_fill(
    profile: Sequence[float],
    available_slot_sizes: SlotSizeSequence = None,
) -> bool:
    """Return True when a candidate profile is an exact legal full-height stack.

    Any legal topfill is only valid when it can be mapped back to its configured slot family without
    breaking the descending order of the stack. For example, a final 194 is valid only if it is
    treated as the 124 family and the effective sequence remains descending; a final 164 in a
    124-124-124-69-69 stack is rejected because it would effectively turn the order into
    124-124-124-69-69-124.
    """
    slots = [float(value) for value in (profile or []) if float(value) > 0.0]
    if not slots:
        return False
    if any(int(round(float(value))) <= 0 for value in slots):
        return False
    if any(int(round(float(value))) > int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)) for value in slots):
        return False
    if any(int(round(float(value))) % 10 not in (4, 9) for value in slots):
        return False

    total_physical = sum(slots) + max(len(slots) - 1, 0) * common.BEAM_HEIGHT
    if abs(total_physical - common.MAX_USED_HEIGHT_BASE) > 1e-6:
        return False

    config_values = set(_config_size_values(available_slot_sizes or slots))
    legal_topfill_values = set(_legal_topfill_values(available_slot_sizes or slots))

    final_slot = float(slots[-1])
    lower_slots = [float(value) for value in slots[:-1]]

    if any(int(round(float(value))) not in config_values for value in lower_slots):
        return False

    if final_slot in config_values:
        return True

    if final_slot not in legal_topfill_values:
        return False
    if final_slot < float(min(config_values, default=0.0)):
        return False

    if not lower_slots:
        return False

    effective_final_slot = _effective_requirement_slot_size(final_slot, slots, available_slot_sizes)
    if effective_final_slot is None:
        return False
    if effective_final_slot not in config_values:
        return False

    effective_profile = []
    for index, raw_value in enumerate(slots):
        rounded = int(round(float(raw_value)))
        if index == len(slots) - 1 and rounded not in config_values:
            effective_profile.append(float(effective_final_slot))
        else:
            effective_profile.append(float(_effective_requirement_slot_size(raw_value, slots, available_slot_sizes) or rounded))

    if any(float(effective_profile[index]) < float(effective_profile[index + 1]) for index in range(len(effective_profile) - 1)):
        return False

    return True


def _normalize_slot_family(candidate_slot_sizes: SlotSizeSequence) -> tuple[float, ...]:
    """Normalize slot sizes into a hashable tuple so repeated profile generation can be cached."""
    return tuple(sorted({float(size) for size in (candidate_slot_sizes or []) if size is not None}))


def _profile_generation_policy(candidate_slot_sizes: SlotSizeSequence) -> tuple[int, int, int]:
    """Return the runtime-safe generation window for the active slot family.

    The Stage 6 search must keep enough diverse exact-fill profiles alive for families with several
    slot sizes, but never allow a legal-profile pool to explode combinatorially for larger exact-fill
    families. The cap is therefore tied to the configured family size and kept deliberately low for
    real 4+ slot families.
    """
    configured_sizes = sorted(set(_config_size_values(candidate_slot_sizes or [])), reverse=True)
    family_size = len(configured_sizes)
    if family_size <= 3:
        return 14, 2, 12
    if family_size <= 4:
        return 12, 2, 10
    if family_size <= 6:
        return 10, 2, 8
    if family_size <= 8:
        return 8, 2, 6
    return 6, 2, 5


def _normalize_required_counts(required_counts: dict[float, int] | Sequence[tuple[float, int]] | None) -> tuple[tuple[float, int], ...] | None:
    """Convert requirement quotas into a hashable key for cache-safe profile generation."""
    if required_counts is None:
        return None
    if isinstance(required_counts, dict):
        items = required_counts.items()
    else:
        items = required_counts
    return tuple(sorted((float(size), int(count)) for size, count in items if int(count) > 0))


def _family_capacity_by_profile_pool(
    profiles: Sequence[Sequence[float]],
    required_counts: dict[float, int],
) -> dict[int, int]:
    """Return the aggregate exact-family capacity available in the current profile pool."""
    if not required_counts:
        return {}
    capacity: Counter[int] = Counter()
    for profile in profiles:
        for size, count in _effective_requirement_counts(profile, list(required_counts.keys())).items():
            capacity[int(size)] += int(count)
    return dict(sorted(capacity.items()))


def _profile_pool_can_cover_required_family_quota(
    profiles: Sequence[Sequence[float]],
    required_counts: dict[float, int],
) -> bool:
    """Detect impossible configs before backtracking by checking aggregate exact-family coverage.

    The pool is only usable when the aggregate family capacity is high enough for the whole remaining
    exact-family vector. This is a true cover test, not a loose presence check.
    """
    if not required_counts:
        return True
<<<<<<< Updated upstream
    normalized_required = {
        float(size): int(count)
        for size, count in (required_counts or {}).items()
        if int(count) > 0
    }
    try:
        profiles = _generate_feasible_rack_profiles(
            normalized_sizes,
            timeout_seconds=timeout_seconds,
            config_deadline=config_deadline,
        )
    except Stage6ProfileGenerationTimeout:
=======
    if not profiles:
>>>>>>> Stashed changes
        return False
    capacity = _family_capacity_by_profile_pool(profiles, required_counts)
    for size, required in required_counts.items():
        size_int = int(round(float(size)))
        if int(required) > 0 and capacity.get(size_int, 0) < int(required):
            return False
    return True


def _build_quota_cover_profiles(
    required_counts: dict[float, int] | Sequence[tuple[float, int]],
    available_slot_sizes: SlotSizeSequence,
    max_profiles: int = 12,
) -> list[list[float]]:
    """Build a legal exact-family cover pool that satisfies the required exact quotas."""
    if not required_counts:
        return []
    if isinstance(required_counts, dict):
        normalized_required = {
            float(size): int(count)
            for size, count in required_counts.items()
            if int(count) > 0
        }
    else:
        normalized_required = {
            float(size): int(count)
            for size, count in required_counts
            if int(count) > 0
        }
    if not normalized_required:
        return []

    config_values = sorted(set(_config_size_values(available_slot_sizes or list(normalized_required.keys()))))
    if not config_values:
        config_values = sorted({int(round(float(size))) for size in normalized_required})
    if not config_values:
        return []

    legal_values = sorted(set(config_values) | set(_legal_topfill_values(config_values)))
    required_sizes = sorted({int(round(float(size))) for size in normalized_required})
    candidate_values = tuple(sorted(set(legal_values) | set(required_sizes)))
    profiles: list[list[float]] = []
    seen: set[tuple[int, ...]] = set()
    limit = max(1, min(int(max_profiles), 12))

    max_profile_length = 12
    for length in range(1, max_profile_length + 1):
        for combo in product(candidate_values, repeat=length):
            # Preserve the original stack order so a legal topfill stays at the end of the profile.
            # Sorting the whole tuple before validation breaks valid exact-fill stacks such as
            # 124, 124, 124, 124, 194 by moving the topfill to the front.
            values = tuple(combo)
            if not _profile_is_feasible_exact_fill(list(values), config_values):
                continue
            if not any(int(round(float(value))) in required_sizes for value in values):
                continue
            signature = tuple(sorted(int(round(float(value))) for value in values))
            if signature in seen:
                continue
            seen.add(signature)
            profiles.append([float(value) for value in values])
            if len(profiles) >= limit:
                ranked = sorted(
                    profiles,
                    key=lambda profile: _profile_requirement_priority(profile, normalized_required),
                    reverse=True,
                )
                if _profile_pool_can_cover_required_family_quota(ranked, normalized_required):
                    return ranked[:limit]
                continue

    profiles = sorted(
        profiles,
        key=lambda profile: _profile_requirement_priority(profile, normalized_required),
        reverse=True,
    )
    if _profile_pool_can_cover_required_family_quota(profiles, normalized_required):
        return profiles[:limit]
    return []



@lru_cache(maxsize=32)
def _generate_feasible_rack_profiles_cached(
    candidate_slot_sizes: tuple[float, ...],
    required_counts: tuple[tuple[float, int], ...] | None = None,
    timeout_seconds: float | None = None,
    config_deadline: float | None = None,
) -> tuple[tuple[float, ...], ...]:
    """Enumerate legal exact-fill profiles in the full repeated-family space.

<<<<<<< Updated upstream
    The previous pruning mistakenly prohibited legal topfills whose final residual is larger than the
    preceding lower-stack values. That excluded valid exact-fill patterns like 124,124,124,124,194,
    which should be accepted once the final 194 is mapped back to the 124 family.
=======
    The timeout/deadline arguments are accepted for compatibility with the outer generation API and
    are intentionally ignored here because the cached profile generation is only a pure legal-profile
    pool builder; the caller-level timeout checks happen outside this helper.
>>>>>>> Stashed changes
    """
    configured_sizes = tuple(sorted({
        int(round(float(size)))
        for size in (candidate_slot_sizes or [])
        if float(size) > 0.0
        and int(round(float(size))) <= int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM))
        and int(round(float(size))) % 10 in (4, 9)
    }, reverse=True))
    if not configured_sizes:
        return ()

<<<<<<< Updated upstream
    max_profile_length = min(12, max(4, len(configured_sizes) * 6))
    legal_topfill_values = tuple(sorted(_legal_topfill_values(configured_sizes), reverse=True))
    seen: set[tuple[float, ...]] = set()
    def _accept(profile: tuple[float, ...]) -> None:
        if not profile:
            return
        canonical = tuple(float(value) for value in profile)
        if canonical in seen:
            return
        if not _profile_is_feasible_exact_fill(list(canonical), configured_sizes):
            return
        seen.add(canonical)

    def _search(prefix: tuple[float, ...], running_sum: float) -> None:
        if len(prefix) >= max_profile_length:
            return

        for size in configured_sizes:
            if prefix and float(size) > float(prefix[-1]) + 1e-9:
                continue
            next_profile = prefix + (float(size),)
            next_sum = running_sum + float(size)
            physical_total = next_sum + (len(next_profile) - 1) * common.BEAM_HEIGHT
            if physical_total > common.MAX_USED_HEIGHT_BASE + 1e-9:
                continue
            if abs(physical_total - common.MAX_USED_HEIGHT_BASE) <= 1e-9:
                _accept(next_profile)
                continue
            _search(next_profile, next_sum)

        if not prefix:
            return

        for topfill in legal_topfill_values:
            next_profile = prefix + (float(topfill),)
            physical_total = running_sum + float(topfill) + (len(next_profile) - 1) * common.BEAM_HEIGHT
            if abs(physical_total - common.MAX_USED_HEIGHT_BASE) <= 1e-9:
                _accept(next_profile)

    for initial_size in configured_sizes:
        _search((float(initial_size),), float(initial_size))

    ordered = sorted(
        seen,
        key=lambda profile: (
            -len(profile),
            tuple(-float(value) for value in profile),
        ),
    )

    accepted: list[tuple[float, ...]] = []
    for profile in ordered:
        accepted.append(profile)
        if len(accepted) >= max_profile_length * 4:
            break

    # Do not inject synthetic bare sizes that are not part of the configured family. Legal topfills are
    # still valid members of a profile, but their contribution to family coverage must be evaluated via
    # the effective family mapping instead of being counted as raw extra slot sizes.
    return tuple(accepted)
=======
    family_set = set(configured_sizes)
    legal_values = set(_legal_topfill_values(candidate_slot_sizes or []))
    required_map = {float(size): int(count) for size, count in (required_counts or ())}
    required_family_sizes = {
        int(round(float(size)))
        for size, count in required_map.items()
        if int(count) > 0
    }

    candidate_sizes = sorted(set(configured_sizes) | required_family_sizes, reverse=True)
    if required_family_sizes:
        max_lower_rows = min(10, max(6, len(candidate_sizes) * 3))
    else:
        max_lower_rows = min(10, max(6, len(candidate_sizes) * 2))

    exact_profiles: set[tuple[float, ...]] = set()
    lower_builds: dict[int, set[tuple[int, ...]]] = {0: {()}}

    for lower_count in range(1, max_lower_rows + 1):
        next_states: set[tuple[int, ...]] = set()
        for prior_state in lower_builds.get(lower_count - 1, set()):
            for size in candidate_sizes:
                candidate = tuple(sorted(prior_state + (size,), reverse=True))
                if len(candidate) != lower_count:
                    continue
                if required_family_sizes and not any(value in required_family_sizes for value in candidate):
                    continue
                next_states.add(candidate)
        lower_builds[lower_count] = next_states

        for prefix in next_states:
            prefix_total = sum(prefix)
            lower_beam_count = max(len(prefix) - 1, 0)
            support_height = prefix_total + lower_beam_count * common.BEAM_HEIGHT
            if support_height < 504.0 - 1e-9:
                continue
            residual = common.MAX_USED_HEIGHT_BASE - prefix_total - len(prefix) * common.BEAM_HEIGHT
            if residual <= 0.0:
                continue
            rounded_residual = int(round(float(residual)))
            if rounded_residual > int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)):
                continue
            if required_family_sizes:
                allowed_residuals = set(required_family_sizes) | set(legal_values)
                if rounded_residual not in allowed_residuals:
                    continue
            elif rounded_residual not in family_set and rounded_residual not in legal_values:
                continue
            ordered_prefix = tuple(sorted(prefix, reverse=True))
            profile = ordered_prefix + (float(rounded_residual),)
            if _profile_is_feasible_exact_fill(list(profile), candidate_slot_sizes):
                exact_profiles.add(profile)

    ordered_profiles = sorted(
        exact_profiles,
        key=lambda profile: tuple(-float(value) for value in profile),
    )

    if not ordered_profiles:
        return ()

    cap_limit = _profile_generation_policy(candidate_slot_sizes)[2]
    capped_limit = max(1, min(cap_limit, len(ordered_profiles)))
    accepted_profiles: list[tuple[float, ...]] = []
    seen: set[tuple[float, ...]] = set()
    for profile in ordered_profiles:
        if profile in seen:
            continue
        seen.add(profile)
        accepted_profiles.append(profile)
        if len(accepted_profiles) >= capped_limit:
            break

    if required_family_sizes:
        family_representatives: list[tuple[float, ...]] = []
        seen: set[tuple[float, ...]] = set()
        for size in sorted(required_family_sizes):
            family_profiles = [
                profile
                for profile in ordered_profiles
                if int(round(float(size))) in {
                    int(round(float(value)))
                    for value in profile
                    if float(value) > 0.0
                }
            ]
            for profile in family_profiles:
                if profile in seen:
                    continue
                family_representatives.append(profile)
                seen.add(profile)
                if len(family_representatives) >= capped_limit:
                    break
            if len(family_representatives) >= capped_limit:
                break
            if len(family_profiles) >= 2:
                for profile in family_profiles[1:]:
                    if profile in seen:
                        continue
                    family_representatives.append(profile)
                    seen.add(profile)
                    if len(family_representatives) >= capped_limit:
                        break
            if len(family_representatives) >= capped_limit:
                break

        for profile in ordered_profiles:
            if profile not in seen:
                family_representatives.append(profile)
                seen.add(profile)
            if len(family_representatives) >= capped_limit:
                break

        if family_representatives:
            accepted_profiles = family_representatives[:capped_limit]

    return tuple(accepted_profiles)
>>>>>>> Stashed changes


def _generate_feasible_rack_profiles(
    candidate_slot_sizes: SlotSizeSequence,
    timeout_seconds: float | None = None,
    config_deadline: float | None = None,
    required_counts: dict[float, int] | None = None,
) -> list[list[float]]:
    """Generate a bounded exact-fill profile pool while preserving required family coverage."""
    normalized = _normalize_slot_family(candidate_slot_sizes)
    normalized_required = _normalize_required_counts(required_counts)
    required_map = {float(size): int(count) for size, count in (normalized_required or ())}
    if len(required_map) > 8 or len(normalized) > 8:
        return []

    quota_profiles: list[list[float]] = []
    if normalized_required:
        quota_profiles = _build_quota_cover_profiles(normalized_required, normalized, max_profiles=12)
        if quota_profiles and _profile_pool_can_cover_required_family_quota(quota_profiles, required_map):
            return quota_profiles

    config_values = sorted(set(_config_size_values(normalized)))
    if not config_values:
        return []

    limit = max(1, min(_profile_generation_policy(normalized)[2], 12))

    cached_profiles = _generate_feasible_rack_profiles_cached(
        normalized,
        required_counts=normalized_required,
        timeout_seconds=timeout_seconds,
        config_deadline=config_deadline,
    )
    if cached_profiles:
        ranked = [list(profile) for profile in cached_profiles]
        if normalized_required:
            ranked = sorted(
                ranked,
                key=lambda profile: _profile_requirement_priority(profile, required_map),
                reverse=True,
            )
        return ranked[:limit]

    legal_values = sorted(set(config_values) | set(_legal_topfill_values(normalized)))
    candidate_values = tuple(sorted(set(legal_values) | set(config_values)))
    profiles: list[list[float]] = []
    seen: set[tuple[int, ...]] = set()
    max_profile_length = min(9, max(3, len(config_values) + 2))
    for length in range(1, max_profile_length + 1):
        for combo in product(candidate_values, repeat=length):
            values = tuple(combo)
            if not _profile_is_feasible_exact_fill(list(values), normalized):
                continue
            signature = tuple(sorted(int(round(float(value))) for value in values))
            if signature in seen:
                continue
            seen.add(signature)
            profiles.append([float(value) for value in values])
            if len(profiles) >= limit:
                break
        if len(profiles) >= limit:
            break

    if not profiles:
        return []
    return profiles[:limit]


def _choose_profile_shortlist(
    profiles: list[list[float]],
    required_counts: dict[float, int],
    limit: int | None = None,
) -> list[list[float]]:
    """Keep exactly one representative for each still-required family before generic ranking.

    The shortlist contract is explicit: every remaining exact family must have at least one profile in
    the shortlist, and the shortlist must not silently drop a family just because a larger profile is
    ranked higher. This is a family-preserving shortlist, not a raw slot-coverage shortlist.
    """
    if not profiles:
        return []

    family_cap = _profile_generation_policy(list(required_counts.keys()))[2]
    default_limit = int(limit if limit is not None else PROFILE_CANDIDATE_LIMIT)
    search_limit = max(1, min(default_limit, family_cap, len(profiles)))
    if len(profiles) <= search_limit:
        return [list(profile) for profile in profiles]

    required_sizes = [
        int(round(float(size)))
        for size in sorted(required_counts, key=lambda size: int(round(float(size))))
        if int(required_counts.get(size, 0)) > 0
    ]
    ranked = sorted(
        profiles,
        key=lambda profile: _profile_requirement_priority(list(profile), required_counts),
        reverse=True,
    )

    def _family_hits(profile: list[float]) -> set[int]:
        return {int(round(float(value))) for value in profile if float(value) > 0.0}

    kept: list[list[float]] = []
    seen_signatures: set[tuple[float, ...]] = set()

    def _add_profile(profile: list[float]) -> None:
        signature = tuple(sorted(float(value) for value in profile))
        if signature in seen_signatures:
            return
        seen_signatures.add(signature)
        kept.append(list(profile))

    # Preserve the exact contract: one representative per required family before generic ranking.
    for size in required_sizes:
        chosen = next((profile for profile in ranked if size in _family_hits(profile)), None)
        if chosen is not None:
            _add_profile(list(chosen))

    for profile in ranked:
        if len(kept) >= search_limit:
            break
        if profile in kept:
            continue
        _add_profile(list(profile))

    missing_required = [
        size for size in required_sizes
        if size not in {value for profile in kept for value in _family_hits(profile)}
    ]
    for size in missing_required:
        chosen = next((profile for profile in ranked if size in _family_hits(profile)), None)
        if chosen is not None:
            _add_profile(list(chosen))

    if len(kept) > search_limit:
        coverage_first: list[list[float]] = []
        seen_coverage: set[tuple[float, ...]] = set()
        for size in required_sizes:
            for profile in kept:
                if size not in _family_hits(profile):
                    continue
                signature = tuple(sorted(float(value) for value in profile))
                if signature in seen_coverage:
                    continue
                seen_coverage.add(signature)
                coverage_first.append(list(profile))
                break
        for profile in kept:
            signature = tuple(sorted(float(value) for value in profile))
            if signature in seen_coverage:
                continue
            coverage_first.append(list(profile))
            if len(coverage_first) >= search_limit:
                break
        kept = coverage_first[:search_limit]

    return kept[:search_limit]

def _nearest_configured_family_slot(
    slot_value: float,
    available_slot_sizes: SlotSizeSequence = None,
    profile: list[float] | tuple[float, ...] | None = None,
) -> int | None:
    """Map a legal topfill to the configured family value reached by rounding down.

    The dominant-order rule is: use the largest configured family size that is still below the topfill,
    not the nearest size overall. For cfg 004, 114 therefore maps to 69 instead of 124.
    """
    rounded = int(round(float(slot_value)))
    candidates = sorted(_config_size_values(available_slot_sizes or (profile or [])))
    if not candidates:
        return rounded
    if rounded in candidates:
        return rounded
    lower_candidates = [value for value in candidates if value < rounded]
    if not lower_candidates:
        return None
    return max(lower_candidates)


def _effective_requirement_slot_size(
    slot_value: float,
    profile: list[float] | tuple[float, ...] | None = None,
    available_slot_sizes: SlotSizeSequence = None,
) -> int | None:
    """Map a topfilled legal value back to the underlying configured family value it actually represents.

    The dominant-order rule is: count a legal topfill to the largest configured family size below it,
    not to the raw topfilled value itself. Example: 79 is counted as 69 in a 69/124/189/239 config.
    """
    rounded = int(round(float(slot_value)))
    profile_values = [int(round(float(value))) for value in (profile or []) if float(value) > 0.0]
    if not profile_values:
        return rounded

    config_values = sorted(_config_size_values(available_slot_sizes or profile_values))
    if rounded in set(config_values):
        return rounded

    if not config_values:
        return rounded

    lower_candidates = [value for value in config_values if value < rounded]
    if lower_candidates:
        return max(lower_candidates)

    lower_profile_values = [value for value in profile_values if value in set(config_values)]
    if lower_profile_values:
        dominant_family = Counter(lower_profile_values).most_common(1)[0][0]
        if dominant_family in config_values:
            return dominant_family

    if rounded < min(config_values, default=rounded):
        return None
    return rounded


def _effective_requirement_counts(
    profile: Sequence[float],
    available_slot_sizes: SlotSizeSequence = None,
) -> dict[int, int]:
    """Count a profile using the configured family value reached by rounding down.

    This includes legal topfilled slots such as 64, 79, 114, 184, etc., which are counted under the
    underlying representative size (e.g. 34, 69, 69, 124) instead of being treated as separate
    synthetic bucket sizes in the exact-fill accounting.
    """
    profile_values = tuple(int(round(float(value))) for value in (profile or []) if float(value) > 0.0)
    if not profile_values:
        return {}

    config_values = tuple(sorted(_config_size_values(available_slot_sizes or profile_values)))
    counts: Counter[int] = Counter({size: 0 for size in config_values})

    for value in profile_values:
        effective = _effective_requirement_slot_size(value, list(profile_values), available_slot_sizes)
        if effective is None:
            continue
        if effective not in counts and effective not in config_values:
            continue
        counts[effective] += 1

    return dict(sorted(counts.items()))


def _profile_shortage_reduction_score(
<<<<<<< Updated upstream
    profile: Sequence[float],
    remaining: dict[float, int],
) -> tuple[int, int, int]:
    """Return the exact-family shortage reduction achieved by a profile."""
    if not remaining:
        return (0, 0, 0)

    normalized_remaining = {
        float(size): int(count)
        for size, count in remaining.items()
        if int(count) > 0
    }
    if not normalized_remaining:
        return (0, 0, 0)

    counts = _effective_requirement_counts(profile, list(normalized_remaining.keys()))
    if not counts:
        return (0, 0, 0)

    before_total = sum(int(value) for value in normalized_remaining.values())
    after_total = 0
    coverage_total = 0
    largest_reduction = 0
    family_reduction_count = 0
    for size, required in normalized_remaining.items():
        size_int = int(round(float(size)))
        covered = min(int(required), counts.get(size_int, 0))
        remaining_after = max(0, int(required) - covered)
        after_total += remaining_after
        coverage_total += covered
        if covered > 0:
            family_reduction_count += 1
        largest_reduction = max(largest_reduction, int(required) - remaining_after)

    shortage_reduction = max(0, before_total - after_total)
    return (int(shortage_reduction), int(coverage_total), int(max(largest_reduction, family_reduction_count)))


def _profile_is_shortage_reducing(
    profile: Sequence[float],
    remaining: dict[float, int],
) -> bool:
    """Return True when the profile helps close at least one unmet exact-family quota."""
    if not remaining:
        return False

    normalized_remaining = {
        float(size): int(count)
        for size, count in remaining.items()
        if int(count) > 0
    }
    if not normalized_remaining:
        return False

    counts = _effective_requirement_counts(profile, list(normalized_remaining.keys()))
    if not counts:
        return False

    for size, required in normalized_remaining.items():
        size_int = int(round(float(size)))
        if int(required) > 0 and counts.get(size_int, 0) > 0:
            return True
    return False


def _shortage_completion_objective(
    remaining: dict[float, int],
    profile: Sequence[float],
) -> tuple[int, int, int, int, int]:
    """Return a numeric ranking for a profile against the remaining shortage vector."""
    if not remaining:
        return (0, 0, 0, 0, 0)

    counts = _effective_requirement_counts(profile, list(remaining.keys()))
    ordered_sizes = sorted(
        remaining,
        key=lambda size: (-int(round(float(size))), int(remaining[size])),
    )
    reduction = 0
    covered = 0
    weighted = 0
    exact_matches = 0
    for size in ordered_sizes:
        required = int(remaining.get(size, 0))
        if required <= 0:
            continue
        size_int = int(round(float(size)))
        profile_count = counts.get(size_int, 0)
        covered_now = min(profile_count, required)
        reduction += covered_now
        covered += covered_now
        weighted += int(size_int) * covered_now
        if profile_count >= required:
            exact_matches += 1

    completion_bonus = 1 if all(
        counts.get(int(round(float(size))), 0) >= int(remaining[size])
        for size in ordered_sizes
        if int(remaining[size]) > 0
    ) else 0
    return (reduction, covered, weighted, exact_matches, completion_bonus)


def _remaining_quota_is_still_completable(
    remaining: dict[float, int],
    candidate_profiles: Sequence[Sequence[float]],
) -> bool:
    """Return True only if the remaining exact-family quotas are still aggregate-coverable."""
    if not remaining:
        return True
    if not candidate_profiles:
        return False

    total_by_size: Counter[int] = Counter()
    for profile in candidate_profiles:
        counts = _effective_requirement_counts(profile, list(remaining.keys()))
        for size, count in counts.items():
            total_by_size[size] += int(count)

    for size, required in remaining.items():
        if int(required) <= 0:
            continue
        size_int = int(round(float(size)))
        if total_by_size.get(size_int, 0) < int(required):
            return False
    return True


def _profile_meets_required_quota(
=======
>>>>>>> Stashed changes
    profile: Sequence[float],
    remaining: dict[float, int],
) -> tuple[int, int, int]:
    """Return the exact-family shortage reduction achieved by a profile.

    A profile is valid for a shortage-aware completion pass when it reduces the missing quota of at
    least one exact family, even if the total shortage vector is not completely eliminated in one shot.
    """
    if not remaining:
        return (0, 0, 0)

    normalized_remaining = {
        float(size): int(count)
        for size, count in remaining.items()
        if int(count) > 0
    }
    if not normalized_remaining:
        return (0, 0, 0)

    counts = _effective_requirement_counts(profile, list(normalized_remaining.keys()))
    if not counts:
        return (0, 0, 0)

    before_total = sum(int(value) for value in normalized_remaining.values())
    after_total = 0
    coverage_total = 0
    largest_reduction = 0
    family_reduction_count = 0
    for size, required in normalized_remaining.items():
        size_int = int(round(float(size)))
        covered = min(int(required), counts.get(size_int, 0))
        remaining_after = max(0, int(required) - covered)
        after_total += remaining_after
        coverage_total += covered
        if covered > 0:
            family_reduction_count += 1
        largest_reduction = max(largest_reduction, int(required) - remaining_after)

    if before_total <= 0:
        return (0, 0, 0)

    shortage_reduction = max(0, before_total - after_total)
    return (int(shortage_reduction), int(coverage_total), int(max(largest_reduction, family_reduction_count)))


def _profile_is_shortage_reducing(
    profile: Sequence[float],
    remaining: dict[float, int],
) -> bool:
    """Return True when the profile helps close at least one unmet exact-family quota."""
    if not remaining:
        return False

    normalized_remaining = {
        float(size): int(count)
        for size, count in remaining.items()
        if int(count) > 0
    }
    if not normalized_remaining:
        return False

    counts = _effective_requirement_counts(profile, list(normalized_remaining.keys()))
    if not counts:
        return False

    for size, required in normalized_remaining.items():
        size_int = int(round(float(size)))
        if int(required) > 0 and counts.get(size_int, 0) > 0:
            return True
    return False


def _profile_keeps_missing_family_alive(
    profile: Sequence[float],
    remaining: dict[float, int],
) -> bool:
    """Return True when the profile still helps an exact family that is still missing.

    A profile is considered alive only when it contributes to a required family that is not yet
    exhausted by the remaining shortage vector. Overfilling an already-satisfied family does not keep
    that family alive for a completion search because it is no longer a missing quota.
    """
    remaining_counts = {
        float(size): int(count)
        for size, count in (remaining or {}).items()
        if int(count) > 0
    }
    if not remaining_counts:
        return False

    counts = _effective_requirement_counts(profile, list(remaining_counts.keys()))
    for size, required in remaining_counts.items():
        size_int = int(round(float(size)))
        contribution = counts.get(size_int, 0)
        if int(required) <= 0:
            continue
        if contribution > 0 and contribution <= int(required):
            return True
    return False


def _shortage_completion_objective(
    remaining: dict[float, int],
    profile: Sequence[float],
) -> int:
    """Score how well a profile closes the remaining exact-family shortage."""
    score = _profile_shortage_reduction_score(profile, remaining)
    return score[0] * 1000 + score[1] * 10 + score[2]


def _remaining_quota_is_still_completable(
    remaining: dict[float, int],
    candidate_profiles: Sequence[Sequence[float]],
) -> bool:
    """Return True when the remaining exact-family demands are still coverable by the current pool.

    This uses true aggregate capacity, not weak family-presence checks. A family is only still
    completable when the current legal profile pool can provide at least that many remaining units for
    every required family; repeated profiles across future racks are allowed, so capacity is measured
    as a pool sum rather than a single one-shot presence test.
    """
    if not remaining:
        return True
    if not candidate_profiles:
        return False

    remaining_counts = {
        int(round(float(size))): int(count)
        for size, count in remaining.items()
        if int(count) > 0
    }
    if not remaining_counts:
        return True

    capacity = _family_capacity_by_profile_pool(candidate_profiles, {float(size): int(count) for size, count in remaining_counts.items()})
    return all(capacity.get(size, 0) >= int(required) for size, required in remaining_counts.items())


def _synthesize_family_cover_profiles(
    required_counts: dict[float, int],
    max_columns: int = 1,
    max_profile_size: int = 12,
) -> list[list[float]]:
    """Compatibility helper for exact-family completion tests."""
    if not required_counts:
        return []

    profiles: list[list[float]] = []
    ordered_sizes = sorted(required_counts, key=lambda size: int(round(float(size))), reverse=True)
    for size in ordered_sizes:
        if int(required_counts.get(size, 0)) <= 0:
            continue
        for lower_len in range(1, min(max_profile_size, 8)):
            lower = [float(size)] * lower_len
            residual = common.MAX_USED_HEIGHT_BASE - sum(lower) - (len(lower) - 1) * common.BEAM_HEIGHT
            if residual <= 0:
                continue
            if residual > common.MAX_REPRESENTATIVE_SLOT_SIZE_CM:
                continue
            candidate = lower + [float(residual)]
            if _profile_is_feasible_exact_fill(candidate, [float(size)]):
                profiles.append(candidate)
                break
    return profiles[: max(1, min(8, max_columns))]


def _exact_cover_completion_subset(
    profiles: Sequence[Sequence[float]],
    required_counts: dict[float, int],
    max_columns: int = 10,
) -> list[list[float]]:
    """Compatibility subset selector for completion-pool coverage."""
    selected: list[list[float]] = []
    seen: set[tuple[int, ...]] = set()
    for profile in profiles:
        signature = tuple(sorted(int(round(float(value))) for value in profile if float(value) > 0.0))
        if signature in seen:
            continue
        seen.add(signature)
        selected.append([float(value) for value in profile])
        if len(selected) >= max(1, max_columns):
            break
    return selected


def _select_completion_pool_for_layout_columns(
    completion_pool: Sequence[Sequence[float]],
    required_counts: dict[float, int],
    max_columns: int = 10,
) -> list[list[float]]:
    """Compatibility wrapper that keeps a bounded completion pool for the required families."""
    if not completion_pool:
        return []
    ordered = sorted(
        completion_pool,
        key=lambda profile: _shortage_completion_objective(required_counts, profile),
        reverse=True,
    )
    return [list(profile) for profile in ordered[: max(1, max_columns)]]


def _build_full_exact_family_completion_pool(
    profiles: Sequence[Sequence[float]],
    required_counts: dict[float, int],
    max_columns: int = 10,
) -> list[list[float]]:
    """Compatibility wrapper that builds a shortage-aware completion pool."""
    if not profiles:
        return []
    return _select_completion_pool_for_layout_columns(profiles, required_counts, max_columns=max_columns)



def _profile_requirement_priority(
    profile: Sequence[float],
    remaining: dict[float, int],
) -> ProfileRequirementPriorityKey:
    """Rank feasible profiles by exact remaining-deficit coverage.

    The decisive signal is the match between the profile's exact-family distribution and the remaining
    shortage vector. Profiles that reduce the outstanding shortage while matching the remaining demand
    shape should outrank profiles that simply contribute more raw count to an already satisfied family.
    """
    counts = _effective_requirement_counts(profile, list(remaining.keys()))
    ordered_sizes = sorted(
        remaining,
        key=lambda size: (
            -int(round(float(size))),
            int(remaining[size]),
        ),
    )

    coverage_vector = tuple(
        min(counts.get(int(round(float(size))), 0), int(remaining[size]))
        for size in ordered_sizes
        if int(remaining[size]) > 0
    )
    shortage_vector = tuple(
        max(int(remaining[size]) - counts.get(int(round(float(size))), 0), 0)
        for size in ordered_sizes
        if int(remaining[size]) > 0
    )

    unmet_requirements = sum(
        1
        for size in ordered_sizes
        if int(remaining[size]) > 0 and counts.get(int(round(float(size))), 0) < int(remaining[size])
    )
    total_shortage = sum(shortage_vector)
    minimums_satisfied = 1 if unmet_requirements == 0 else 0

    exact_completion_bonus = 1 if all(
        counts.get(int(round(float(size))), 0) >= int(remaining[size])
        for size in ordered_sizes
        if int(remaining[size]) > 0
    ) else 0

    distinct_coverage = sum(
        1
        for size in ordered_sizes
        if int(remaining[size]) > 0 and counts.get(int(round(float(size))), 0) > 0
    )
    synthetic_topfill_penalty = 1 if (
        len(profile) > 1
        and int(round(float(profile[-1]))) not in set(_config_size_values(list(remaining.keys())))
        and int(round(float(profile[-1]))) in _legal_topfill_values(list(remaining.keys()))
    ) else 0

    largest_required_family = max(
        (size for size in ordered_sizes if int(remaining[size]) > 0),
        key=lambda size: int(round(float(size))),
        default=None,
    )
    largest_required_size = int(round(float(largest_required_family))) if largest_required_family is not None else 0
    largest_required_need = int(remaining.get(largest_required_family, 0)) if largest_required_family is not None else 0
    largest_required_covered = counts.get(largest_required_size, 0)
    largest_required_shortage = max(0, largest_required_need - largest_required_covered)
    largest_required_closure = 1 if largest_required_covered >= largest_required_need else 0

    base_coverage = sum(coverage_vector)
    weighted_coverage = sum(
        int(round(float(size))) * min(counts.get(int(round(float(size))), 0), int(remaining[size]))
        for size in ordered_sizes
        if int(remaining[size]) > 0
    )

    remaining_total = sum(int(value) for value in remaining.values() if int(value) > 0)
    profile_total = sum(counts.values())
    if remaining_total > 0:
        need_mix_vector = tuple(
            float(int(remaining[size])) / float(remaining_total)
            for size in ordered_sizes
            if int(remaining[size]) > 0
        )
    else:
        need_mix_vector = tuple(0.0 for size in ordered_sizes if int(remaining.get(size, 0)) > 0)

    if profile_total > 0:
        profile_mix_vector = tuple(
            float(counts.get(int(round(float(size))), 0)) / float(profile_total)
            for size in ordered_sizes
            if int(remaining[size]) > 0
        )
    else:
        profile_mix_vector = tuple(0.0 for size in ordered_sizes if int(remaining.get(size, 0)) > 0)

    distribution_gap_vector = tuple(
        abs(candidate_share - target_share)
        for candidate_share, target_share in zip(profile_mix_vector, need_mix_vector)
    )
    shape_distance = float(sum(distribution_gap_vector)) if distribution_gap_vector else 0.0
    distribution_fit_score = int(round((1.0 - min(shape_distance, 1.0)) * 1_000_000.0))

    key: ProfileRequirementPriorityKey = (
        minimums_satisfied,
<<<<<<< Updated upstream
        -unmet_requirements,
        largest_required_closure,
        -largest_required_shortage,
=======
>>>>>>> Stashed changes
        distribution_fit_score,
        -unmet_requirements,
        -total_shortage,
        weighted_coverage,
        base_coverage,
<<<<<<< Updated upstream
        exact_completion_bonus,
        -total_shortage,
=======
        distinct_coverage + scarce_coverage,
>>>>>>> Stashed changes
        tuple(-value for value in shortage_vector),
        coverage_vector,
    )
    assert len(key) == 11, f"Profile requirement priority tuple shape changed unexpectedly: {len(key)} items"
    return key


def _rack_from_column_key(column_key: str) -> str:
    """Return the rack label for a rack-column key such as D06, A00 or R01C01.

    Simple labels like D06 must be grouped by their rack letter, not by the full D06 string.
    That ensures all columns inside the same physical rack share a single profile and only a
    transition across a rack boundary can change the profile.
    """
    text = str(column_key).strip()
    if not text:
        return text
    if "C" in text:
        return text.split("C", 1)[0]

    match = re.match(r"^([A-Za-z]+)\d+", text)
    if match:
        return match.group(1)
    return text


def _runtime_clamped_to_limit(config_start_time: float, config_time_limit_seconds: float | None = None) -> float:
    """Return the elapsed runtime capped by the configured outer deadline when present."""
    elapsed = max(time.perf_counter() - config_start_time, 0.0)
    if config_time_limit_seconds is None:
        return elapsed
    return min(elapsed, float(config_time_limit_seconds))


def _raise_config_timeout_and_return(config_id: str, config_start_time: float, config_time_limit_seconds: float) -> bool:
    """Hard-stop the full config when the outer 300s budget is exhausted."""
    if time.perf_counter() >= config_start_time + config_time_limit_seconds:
        _LAST_STAGE6_TIMEOUTS["profile_generation"] = True
        _LAST_STAGE6_TIMEOUTS["rack_search"] = True
        print(f"[Stage 6] config {config_id}: stopped because the combined per-config time limit was exceeded")
        return True
    return False


def _timeout_summary_row(
    config_id: str,
    layout_id: str,
    config_start_time: float,
    config_time_limit_seconds: float | None = None,
    base_exact_counts: dict[float, int] | None = None,
) -> dict[str, str]:
    """Record the runtime capped by the outer config deadline when the config is cut off."""
    elapsed = _runtime_clamped_to_limit(config_start_time, config_time_limit_seconds)
    exact_counts = base_exact_counts or {}
    minimum_counts = "|".join(f"{int(size)}:{count}" for size, count in sorted(exact_counts.items()))
    return {
        "Layout_ID": layout_id,
        "Config_ID": config_id,
        "Runtime_Seconds": f"{elapsed:.3f}",
        "Runtime_Minutes": f"{elapsed / 60.0:.3f}",
        "Profile_Generation_Seconds": "0.000",
        "Profile_Shortlist_Seconds": "0.000",
        "Rack_Search_Seconds": "0.000",
        "Profile_Generation_Timeout": "YES",
        "Rack_Search_Timeout": "YES",
        "Layout_Feasible": "NO",
        "Allocation_Feasible_Initial": "NO",
        "Required_Locations_Total": "0",
        "Total_Locations": "0",
        "Capacity_Margin": "0",
        "Assigned_Used_Height_Total": "0.000",
        "Total_Allowed_Height": "0.000",
        "Space_Left": "0.000",
        "Beam_Relocations_Total": "0",
        "Initial_Beams_Total": "0",
        "Required_Beams_Total": "0",
        "Additional_Beams_Required": "0",
        "Initial_Grids_Total": "0",
        "Required_Grids_Total": "0",
        "Additional_Grids_Required": "0",
        "Percentage_Rack_Height_Used": "0.00",
        "Minimum_Required_Counts": minimum_counts,
        "Additional_Fill_Counts": "",
        "Slot_Composition_Signature": minimum_counts,
        "Layout_Slot_Size_Distribution": "",
        "Layout_Slot_Size_Cumulative_Coverage": "",
        "Source_Slot_Sizes": "",
        "Layout_Usable_Alignment_Conversions": "0",
        "Feasible_Profiles_Considered_Total": "0",
        "Feasible_Profiles_Used_Total": "0",
    }


def _rack_profile_contribution(
    profile: Sequence[float],
    rack_columns_count: int,
    required_sizes: Sequence[float],
) -> dict[int, int]:
    """Return the exact-family contribution of assigning one rack profile to the full rack.

    Each column in the rack receives the same chosen profile, so the rack-wide contribution is the
    per-profile effective counts multiplied by the number of columns in that rack. This is the
    quantity that should be subtracted from the remaining exact-family shortage vector.
    """
    if rack_columns_count <= 0:
        return {}
    profile_counts = _effective_requirement_counts(profile, list(required_sizes))
    return {
        int(round(float(size))): int(count) * int(rack_columns_count)
        for size, count in profile_counts.items()
    }

def _build_completion_pool_rack_assignments(
    rack_columns: Sequence[str],
    required_counts: dict[float, int],
    available_slot_sizes: SlotSizeSequence,
    completion_pool: Sequence[Sequence[float]],
) -> dict[str, list[float]]:
    """Construct a rack-level assignment from a bounded completion pool.

    This helper is only an assignment-side fallback: it tries legal rack profiles that reduce the
    outstanding exact-family vector, but the final feasibility proof still happens after the complete
    layout assignment is assembled and checked as a whole.
    """
    if not rack_columns or not completion_pool:
        return {column_key: [] for column_key in rack_columns}

    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[_rack_from_column_key(column_key)].append(column_key)
    if not rack_to_columns:
        return {column_key: [] for column_key in rack_columns}

    ordered_required = sorted(
        [float(size) for size in required_counts if int(required_counts.get(size, 0)) > 0],
        key=lambda size: int(round(float(size))),
    )
    candidate_profiles: list[tuple[float, ...]] = []
    seen_profiles: set[tuple[int, ...]] = set()
    for profile in completion_pool:
        signature = tuple(sorted(int(round(float(value))) for value in profile if float(value) > 0.0))
        if signature in seen_profiles:
            continue
        seen_profiles.add(signature)
        candidate_profiles.append(tuple(float(value) for value in profile))

    rack_order = sorted(rack_to_columns, key=lambda rack_key: len(rack_to_columns[rack_key]), reverse=True)
    initial_remaining = {float(size): int(count) for size, count in required_counts.items() if int(count) > 0}

    def _search(rack_index: int, remaining: dict[float, int], last_profile: Sequence[float] | None = None) -> dict[str, list[float]] | None:
        if all(int(count) <= 0 for count in remaining.values()):
            return {}
        if rack_index >= len(rack_order):
            return None

        rack_key = rack_order[rack_index]
        rack_columns_for_key = sorted(rack_to_columns[rack_key])
        rack_column_count = len(rack_columns_for_key)

        for profile in candidate_profiles:
            profile_counts = _effective_requirement_counts(profile, ordered_required)
            if not profile_counts:
                continue
            if not any(
                int(remaining.get(float(size), 0)) > 0 and int(profile_counts.get(int(round(float(size))), 0)) > 0
                for size in ordered_required
            ):
                continue

            next_remaining = {float(size): int(count) for size, count in remaining.items()}
            for size, count in profile_counts.items():
                size_key = next((key for key in next_remaining if int(round(float(key))) == int(size)), None)
                if size_key is None:
                    continue
                next_remaining[size_key] = max(0, int(next_remaining.get(size_key, 0)) - int(count) * rack_column_count)

            if next_remaining == remaining:
                continue
            if not _remaining_quota_is_still_completable(next_remaining, candidate_profiles):
                continue

            child = _search(rack_index + 1, next_remaining, profile)
            if child is not None:
                assignment = {column_key: [float(value) for value in profile] for column_key in rack_columns_for_key}
                assignment.update(child)
                return assignment

        return None

    assignment = _search(0, initial_remaining)
    if assignment is None:
        return {column_key: [] for column_key in rack_columns}

    complete_assignment = {column_key: list(values) for column_key, values in assignment.items()}
    nonempty = [column_key for column_key, values in complete_assignment.items() if values]
    if not nonempty or not _minimum_required_counts_are_satisfied(complete_assignment, nonempty, available_slot_sizes, required_counts):
        return {column_key: [] for column_key in rack_columns}

    return complete_assignment


def _run_search_with_profiles(
    *args,
    **kwargs,
) -> dict[str, list[float]]:
    """Assign one legal profile to each rack using a distribution-aware backtracking search.

    The search operates over the remaining exact-family vector for the whole config, not over local
    per-profile contributions alone. It ranks candidate profiles by the closeness of their slot-family
    distribution to the remaining demand, then backtracks across racks until the full assignment is
    complete or impossible. Only the completed assignment is accepted.
    """
    if len(args) == 1 and not kwargs:
        current_profiles = list(args[0])
        return {"A00": [float(value) for value in current_profiles[0]]} if current_profiles else {"A00": []}

    current_profiles = list(args[0]) if args else list(kwargs.get("current_profiles", []))
    required_counts = kwargs.get("required_counts")
    if required_counts is None and len(args) > 1:
        required_counts = args[1]

    rack_columns = kwargs.get("rack_columns")
    if rack_columns is None and len(args) > 2:
        rack_columns = args[2]

    rack_to_columns = kwargs.get("rack_to_columns")
    if rack_to_columns in (None, {}) and len(args) > 3:
        rack_to_columns = args[3]
    if rack_to_columns is None:
        rack_to_columns = {}

    if rack_columns is None:
        return {"A00": [float(value) for value in (current_profiles[0] if current_profiles else [])]}

    base_assignments: dict[str, list[float]] = {column_key: [] for column_key in rack_columns}
    if not required_counts:
        return base_assignments
    if not current_profiles:
        return base_assignments

    legal_family = sorted({
        int(round(float(size)))
        for size in required_counts.keys()
        if float(size) > 0.0
    })
    unique_profiles: list[list[float]] = []
    seen_profiles: set[tuple[int, ...]] = set()
    for profile in current_profiles:
        normalized = [float(value) for value in profile]
        if not _profile_is_feasible_exact_fill(normalized, legal_family):
            continue
        if not _profile_is_shortage_reducing(
            normalized,
            {float(size): int(count) for size, count in required_counts.items() if int(count) > 0},
        ):
            continue
        unique_profiles.append(normalized)

    if not unique_profiles:
        return base_assignments

    ordered_racks = sorted(rack_to_columns or {})
    if not ordered_racks:
        ordered_racks = ["ALL"]
        rack_to_columns = {"ALL": list(rack_columns)}

    remaining_key_order = sorted(
        [float(size) for size in required_counts if int(required_counts.get(size, 0)) > 0],
        key=lambda size: int(round(float(size))),
    )

    def _distribution_distance(profile: Sequence[float], remaining: dict[float, int]) -> float:
        counts = _effective_requirement_counts(profile, remaining_key_order)
        total = sum(counts.values())
        if total <= 0:
            return float("inf")
        ordered = [float(size) for size in remaining_key_order if int(remaining.get(size, 0)) > 0]
        if not ordered:
            return 0.0
        target_share = [float(remaining.get(size, 0)) / float(sum(remaining.values())) for size in ordered]
        candidate_share = [float(counts.get(int(round(float(size))), 0)) / float(total) for size in ordered]
        return sum(abs(candidate - target) for candidate, target in zip(candidate_share, target_share))

    def _profile_order_key(profile: Sequence[float], remaining: dict[float, int]) -> tuple[float, float, float, tuple[int, ...]]:
        counts = _effective_requirement_counts(profile, remaining_key_order)
        total = sum(counts.values())
        coverage = sum(
            min(counts.get(int(round(float(size))), 0), int(remaining.get(size, 0)))
            for size in remaining_key_order
            if int(remaining.get(size, 0)) > 0
        )
        shortage_after = sum(
            max(int(remaining.get(size, 0)) - counts.get(int(round(float(size))), 0), 0)
            for size in remaining_key_order
            if int(remaining.get(size, 0)) > 0
        )
        distance = _distribution_distance(profile, remaining)
        return (distance, -float(coverage), float(shortage_after), tuple(sorted(int(round(float(v))) for v in profile)))

    def _apply_profile_to_remaining(
        remaining: dict[float, int],
        profile: Sequence[float],
        rack_count: int,
    ) -> dict[float, int]:
        next_remaining = {float(size): int(count) for size, count in remaining.items()}
        contribution = _rack_profile_contribution(profile, rack_count, remaining_key_order)
        for size_int, delta in contribution.items():
            size_key = next((key for key in next_remaining if int(round(float(key))) == int(size_int)), None)
            if size_key is None:
                continue
            next_remaining[size_key] = max(0, int(next_remaining.get(size_key, 0)) - int(delta))
        return next_remaining

    def _search(rack_index: int, remaining: dict[float, int], assignment: dict[str, list[float]], last_profile: Sequence[float] | None = None) -> dict[str, list[float]] | None:
        if all(int(count) <= 0 for count in remaining.values()):
            if last_profile is not None:
                for rack_key in ordered_racks[rack_index:]:
                    for column_key in sorted((rack_to_columns or {}).get(rack_key, [])):
                        assignment[column_key] = [float(value) for value in last_profile]
            return assignment
        if rack_index >= len(ordered_racks):
            return None

        rack_key = ordered_racks[rack_index]
        columns = sorted((rack_to_columns or {}).get(rack_key, []))
        if not columns:
            return _search(rack_index + 1, remaining, assignment)

        ranked_profiles = sorted(
            unique_profiles,
            key=lambda profile: _profile_order_key(profile, remaining),
        )

        for profile in ranked_profiles:
            counts = _effective_requirement_counts(profile, remaining_key_order)
            if not counts:
                continue
            if not any(int(remaining.get(float(size), 0)) > 0 and counts.get(int(round(float(size))), 0) > 0 for size in remaining_key_order):
                continue
            next_remaining = _apply_profile_to_remaining(remaining, profile, len(columns))
            if sum(int(value) for value in next_remaining.values()) >= sum(int(value) for value in remaining.values()):
                continue
            if not _remaining_quota_is_still_completable(next_remaining, unique_profiles):
                continue
            for column_key in columns:
                assignment[column_key] = [float(value) for value in profile]
            child = _search(rack_index + 1, next_remaining, assignment, profile)
            if child is not None:
                return child
            for column_key in columns:
                assignment[column_key] = []

        return None

    initial_remaining = {float(size): int(count) for size, count in required_counts.items() if int(count) > 0}
    assignment = {column_key: [] for column_key in rack_columns}
    completed = _search(0, initial_remaining, assignment)
    if completed is None:
        return base_assignments

    complete_assignment = {column_key: list(values) for column_key, values in completed.items()}
    for column_key in rack_columns:
        complete_assignment.setdefault(column_key, [])
    if not any(values for values in complete_assignment.values()):
        return base_assignments
    return complete_assignment


def _quota_coverage_met(
    profiles: Sequence[Sequence[float]],
    required_counts: dict[float, int],
) -> bool:
    """Return True when the pool covers every required exact family at or above its quota."""
    if not required_counts:
        return True
    normalized = {
        float(size): int(count)
        for size, count in required_counts.items()
        if int(count) > 0
    }
    if not normalized:
        return True
    if not profiles:
        return False

    total_counts: Counter[int] = Counter()
    for profile in profiles:
        for size, count in _effective_requirement_counts(profile, list(normalized.keys())).items():
            total_counts[int(size)] += int(count)

    for size, required in normalized.items():
        if total_counts.get(int(round(float(size))), 0) < int(required):
            return False
    return True


def _exact_family_capacity_is_feasible(
    profiles: Sequence[Sequence[float]],
    required_counts: dict[float, int],
    total_columns: int,
) -> bool:
    """Reject impossible layouts only when the required family volume exceeds the available full layout capacity."""
    if not required_counts:
        return True
    if total_columns <= 0:
        return False
    if not profiles:
        return False

    for size, required in required_counts.items():
        if int(required) > 0 and int(required) > int(total_columns):
            return False
    return True


def _build_shortage_vector_completion_profiles(
    required_counts: dict[float, int],
    available_slot_sizes: SlotSizeSequence,
    seed_profiles: Sequence[Sequence[float]] | None = None,
) -> list[list[float]]:
    """Keep the completion pool explicit and shortage-driven for the remaining exact families."""
    normalized_required = {
        float(size): int(count)
        for size, count in (required_counts or {}).items()
        if int(count) > 0
    }
    if not normalized_required:
        return []

    source_profiles = [list(profile) for profile in (seed_profiles or []) if list(profile)]
    if not source_profiles:
        source_profiles = _build_quota_cover_profiles(normalized_required, available_slot_sizes, max_profiles=12)
    if not source_profiles:
        return []

    legal_profiles = [
        [float(value) for value in profile]
        for profile in source_profiles
        if _profile_is_feasible_exact_fill(profile, available_slot_sizes)
    ]
    if not legal_profiles:
        return []

    ordered = sorted(
        legal_profiles,
        key=lambda profile: _profile_requirement_priority(list(profile), normalized_required),
        reverse=True,
    )

    required_sizes = sorted(
        {int(round(float(size))) for size in normalized_required},
        reverse=True,
    )
    completion: list[list[float]] = []
    seen_signatures: set[tuple[int, ...]] = set()

    def _append_unique(profile: Sequence[float]) -> None:
        signature = tuple(sorted(int(round(float(value))) for value in profile if float(value) > 0.0))
        if signature in seen_signatures:
            return
        seen_signatures.add(signature)
        completion.append([float(value) for value in profile])

    for size_int in required_sizes:
        family_match = next(
            (
                profile
                for profile in ordered
                if size_int in {int(round(float(value))) for value in profile if float(value) > 0.0}
            ),
            None,
        )
        if family_match is not None:
            _append_unique(family_match)

    for profile in ordered:
        if _quota_coverage_met(completion, normalized_required):
            break
        if not _profile_is_shortage_reducing(profile, normalized_required):
            continue
        _append_unique(profile)

    if _quota_coverage_met(completion, normalized_required):
        return completion

    for profile in ordered:
        if not any(
            int(round(float(value))) in required_sizes for value in profile if float(value) > 0.0
        ):
            continue
        _append_unique(profile)
        if _quota_coverage_met(completion, normalized_required):
            break

    if completion:
        return completion
    return [list(profile) for profile in ordered[: min(4, len(ordered))]]


def _build_deficit_coverage_layout(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: SlotSizeSequence,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]]:
<<<<<<< Updated upstream
    """Search the full legal profile space with exact-cover branch-and-bound.

    The key change is that the recursive search now operates over the remaining exact-family vector for
    the whole layout. Each rack assignment must keep the same profile across its columns, and a rack is
    only accepted when it reduces the remaining shortage without making the remaining quotas impossible
    to cover later. This is a global completion model, not a greedy shortage-closure approximation.
    """
    if not rack_columns:
        return {}

    required = {float(size): int(count) for size, count in required_counts.items() if int(count) > 0}
    profile_generation_start = time.perf_counter()
=======
    """Use a shortlist and keep any valid nonempty partial assignment instead of discarding it."""
    if not rack_columns:
        return {}

>>>>>>> Stashed changes
    profiles = _generate_feasible_rack_profiles(
        config_slot_sizes or list(required_counts.keys()),
        timeout_seconds=PROFILE_GENERATION_TIMEOUT_SECONDS,
        config_deadline=config_deadline,
        required_counts=required,
    )
    if not profiles:
        return {column_key: [] for column_key in rack_columns}

<<<<<<< Updated upstream
    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[_rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns, key=lambda rack_key: len(rack_to_columns[rack_key]), reverse=True)
    if not rack_order:
        return {column_key: [] for column_key in rack_columns}

    required_key_order = sorted(required, key=lambda size: int(round(float(size))))
    shortlist_limit = min(max(4, len(profiles)), max(8, min(20, len(profiles))))
    candidate_pool = _choose_profile_shortlist(profiles, required, limit=shortlist_limit)
    candidate_pool = [list(profile) for profile in candidate_pool if _profile_is_shortage_reducing(profile, required)]
    if not candidate_pool:
        candidate_pool = [list(profile) for profile in profiles if _profile_is_shortage_reducing(profile, required)]
    if not candidate_pool:
        return {column_key: [] for column_key in rack_columns}

    ranked_candidates: list[list[float]] = []
    seen: set[tuple[int, ...]] = set()
    for profile in sorted(candidate_pool, key=lambda profile: _shortage_completion_objective(required, profile), reverse=True):
        signature = tuple(sorted(int(round(float(value))) for value in profile if float(value) > 0.0))
        if signature in seen:
            continue
        seen.add(signature)
        ranked_candidates.append(list(profile))

    rack_search_start = time.perf_counter()
    rack_search_deadline = rack_search_start + RACK_SEARCH_TIMEOUT_SECONDS

    def _candidate_profiles_for_remaining(remaining: dict[float, int]) -> list[list[float]]:
        ordered = sorted(
            ranked_candidates,
            key=lambda profile: _shortage_completion_objective(remaining, profile),
            reverse=True,
        )
        deduped: list[list[float]] = []
        seen_profiles: set[tuple[int, ...]] = set()
        for profile in ordered:
            if not _profile_is_shortage_reducing(profile, remaining):
                continue
            signature = tuple(sorted(int(round(float(value))) for value in profile if float(value) > 0.0))
            if signature in seen_profiles:
                continue
            seen_profiles.add(signature)
            deduped.append(list(profile))
        return deduped

    @lru_cache(maxsize=20000)
    def _search(index: int, remaining_key: tuple[tuple[float, int], ...]) -> dict[str, list[float]] | None:
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            raise Stage6RackSearchTimeout("rack search exceeded the combined config timeout")
        if time.perf_counter() >= rack_search_deadline:
            raise Stage6RackSearchTimeout("rack search exceeded the per-config timeout")

        remaining = {float(size): int(count) for size, count in remaining_key}
        if all(int(value) <= 0 for value in remaining.values()):
            return {}
        if index >= len(rack_order):
            return None

        rack = rack_order[index]
        columns = sorted(rack_to_columns[rack])
        rack_columns_count = len(columns)
        candidate_profiles = _candidate_profiles_for_remaining(remaining)
        if not candidate_profiles:
            return None

        ordered_candidates = sorted(
            candidate_profiles,
            key=lambda profile: _shortage_completion_objective(remaining, profile),
            reverse=True,
        )

        for profile in ordered_candidates:
            if config_deadline is not None and time.perf_counter() >= config_deadline:
                raise Stage6RackSearchTimeout("rack search exceeded the combined config timeout")
            if time.perf_counter() >= rack_search_deadline:
                raise Stage6RackSearchTimeout("rack search exceeded the per-config timeout")

            next_remaining = {float(size): int(count) for size, count in remaining.items()}
            contribution = _effective_requirement_counts(profile, required_key_order)
            for size_int, count in contribution.items():
                size_key = next((key for key in next_remaining if int(round(float(key))) == int(size_int)), None)
                if size_key is None:
                    continue
                next_remaining[size_key] = max(0, int(next_remaining.get(size_key, 0)) - int(count) * rack_columns_count)

            # Keep a branch alive even when the remaining exact-family vector is still short of one
            # family. The final layout feasibility check is the correct place to reject the completed
            # configuration; pruning here discards all valid partial assignments for impossible configs.
            child = _search(index + 1, tuple(sorted((float(size), int(value)) for size, value in next_remaining.items())))
            if child is None:
                continue

            assignment = {column_key: [float(value) for value in profile] for column_key in columns}
            for column_key, values in child.items():
                assignment[column_key] = list(values)
            return assignment

        return None
=======
    shortlist = _choose_profile_shortlist(profiles, required_counts, limit=max(1, min(PROFILE_CANDIDATE_LIMIT, len(profiles))))
    candidates = shortlist if shortlist else profiles
    completion_pool = _build_shortage_vector_completion_profiles(required_counts, config_slot_sizes, seed_profiles=candidates)

    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[_rack_from_column_key(column_key)].append(column_key)
>>>>>>> Stashed changes

    candidate_profiles = candidates or completion_pool
    try:
<<<<<<< Updated upstream
        assignments = _search(0, tuple(sorted((float(size), int(count)) for size, count in required.items())))
    except Stage6RackSearchTimeout:
        _LAST_STAGE6_TIMEOUTS["rack_search"] = True
        _LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - rack_search_start
        return {column_key: [] for column_key in rack_columns}
=======
        assignments = _run_search_with_profiles(candidate_profiles, required_counts, rack_columns, rack_to_columns)
    except TypeError:
        assignments = _run_search_with_profiles(candidate_profiles)
>>>>>>> Stashed changes

    if any(values for values in assignments.values()):
        return {column_key: list(values) for column_key, values in assignments.items()}

<<<<<<< Updated upstream
    if assignments is None:
        return {column_key: [] for column_key in rack_columns}

    complete_assignment = {column_key: list(values) for column_key, values in assignments.items()}
    for column_key in rack_columns:
        complete_assignment.setdefault(column_key, [])
    if not any(values for values in complete_assignment.values()):
        return {column_key: [] for column_key in rack_columns}
    return complete_assignment
=======
    if completion_pool:
        fallback = {column_key: [] for column_key in rack_columns}
        for column_key in rack_columns[: max(1, min(len(rack_columns), 2))]:
            fallback[column_key] = [float(value) for value in completion_pool[0]]
        return fallback

    return {column_key: [] for column_key in rack_columns}
>>>>>>> Stashed changes



def _parse_count_signature(value: str) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    for token in str(value).split("|"):
        text = token.strip()
        if ":" not in text:
            continue
        size_text, count_text = text.split(":", 1)
        size = common._to_int_default(size_text, -1)
        count = common._to_int_default(count_text, 0)
        if size >= 0 and count > 0:
            counts[size] += count
    return dict(counts)


def _additional_fill_signature(
    column_assignments: dict[str, list[float]],
    minimum_exact_counts: dict[float, int],
) -> str:
    layout_counts: dict[int, int] = defaultdict(int)
    for slots in column_assignments.values():
        for slot_size in slots:
            layout_counts[int(round(slot_size))] += 1

    minimum_counts = {int(round(size)): int(count) for size, count in minimum_exact_counts.items()}
    additional: dict[int, int] = defaultdict(int)
    for size in set(layout_counts.keys()) | set(minimum_counts.keys()):
        delta = layout_counts.get(size, 0) - minimum_counts.get(size, 0)
        if delta > 0:
            additional[size] = delta

    return "|".join(f"{size}:{count}" for size, count in sorted(additional.items()))


def _occupied_allocation_by_exact_slot_size(
    minimum_counts: dict[int, int],
    total_counts: dict[int, int],
) -> dict[int, int]:
    # Assign occupied demand buckets to exact slot capacities using best fit:
    # for each demand size, consume the smallest available exact slot size that can fit it.
    remaining_capacity = {size: max(int(count), 0) for size, count in total_counts.items()}
    occupied_exact: dict[int, int] = defaultdict(int)
    capacity_sizes = sorted(remaining_capacity.keys())

    for demand_size in sorted(minimum_counts.keys(), reverse=True):
        remaining_demand = max(int(minimum_counts.get(demand_size, 0)), 0)
        if remaining_demand <= 0:
            continue

        for capacity_size in capacity_sizes:
            if capacity_size < demand_size:
                continue
            available = remaining_capacity.get(capacity_size, 0)
            if available <= 0:
                continue

            used = min(available, remaining_demand)
            occupied_exact[capacity_size] += used
            remaining_capacity[capacity_size] = available - used
            remaining_demand -= used

            if remaining_demand <= 0:
                break

    return dict(occupied_exact)


def _empty_locations_rows_by_slot_size(
    summary_rows: list[dict[str, str]],
    method_label: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in summary_rows:
        config_id = str(row.get("Config_ID", "")).strip()
        if not config_id:
            continue

        minimum_counts = _parse_count_signature(str(row.get("Minimum_Required_Counts", "")))
        total_counts = common._exclude_fixed_layout_slot_counts(
            _parse_count_signature(str(row.get("Layout_Slot_Size_Distribution", "")))
        )
        occupied_exact = _occupied_allocation_by_exact_slot_size(minimum_counts, total_counts)

        all_sizes = sorted(set(total_counts.keys()) | set(occupied_exact.keys()))
        for size in all_sizes:
            total_count = max(total_counts.get(size, 0), 0)
            occupied_count = max(occupied_exact.get(size, 0), 0)
            empty_count = max(total_count - occupied_count, 0)
            rows.append(
                {
                    "Method": method_label,
                    "Config_ID": config_id,
                    "Slot_Size_cm": str(size),
                    "Occupied": str(occupied_count),
                    "Total_Locations_In_Layout": str(total_count),
                    "Empty": str(empty_count),
                }
            )

    return rows


def _config_size_values(available_slot_sizes: SlotSizeSequence) -> set[int]:
    """Return only the exact configured representative sizes for the current configuration."""
    if not available_slot_sizes:
        return set()
    normalized = tuple(sorted({
        int(round(float(size)))
        for size in available_slot_sizes
        if float(size) > 0.0
        and int(round(float(size))) <= int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM))
        and int(round(float(size))) % 10 in (4, 9)
    }))
    return set(normalized)


@lru_cache(maxsize=64)
def _legal_topfill_values_cached(config_values: tuple[int, ...]) -> frozenset[int]:
    """Cache legal residual completions for a normalized configured slot family.

    The old implementation used a Cartesian product across every lower-stack length, which makes 4+ slot
    families explode combinatorially even though the exact-fill rule depends only on the total lower
    stack sum and beam count. We compute the same legal residuals via a bounded DP over reachable sums
    instead of enumerating every tuple in the product space.
    """
    if not config_values:
        return frozenset()

    config_values = tuple(sorted(set(config_values)))
    legal_values: set[int] = set()
    max_lower_rows = min(12, max(2, len(config_values) * 6))
    reachable_by_count: dict[int, set[int]] = {0: {0}}

    for lower_count in range(1, max_lower_rows + 1):
        next_totals: set[int] = set()
        for lower_sum in reachable_by_count.get(lower_count - 1, set()):
            for slot_size in config_values:
                candidate_sum = lower_sum + slot_size
                if candidate_sum <= int(round(common.MAX_USED_HEIGHT_BASE)) + 10:
                    next_totals.add(candidate_sum)
        reachable_by_count[lower_count] = next_totals

        for lower_sum in next_totals:
            support_height = lower_sum + max(lower_count - 1, 0) * common.BEAM_HEIGHT
            if support_height < 504.0 - 1e-9:
                continue
            residual_value = common.MAX_USED_HEIGHT_BASE - lower_sum - common.BEAM_HEIGHT * lower_count
            if residual_value <= 0.0:
                continue
            if residual_value > 214.0:
                continue
            rounded = int(round(residual_value))
            if rounded % 10 not in (4, 9):
                continue
            legal_values.add(rounded)

    return frozenset(legal_values)


def _legal_topfill_values(available_slot_sizes: SlotSizeSequence) -> set[int]:
    """Return legal final residual completions for any valid lower stack, including mixed setups.

    A topfill is legal when it exactly completes a physically valid lower stack to the 754 cm rack
    height, regardless of whether the lower stack is a single repeated family size or a mixed set of
    configured slot sizes. This intentionally keeps only true completion values and excludes synthetic
    residuals that do not correspond to an exact stack fit.
    """
    config_values = tuple(sorted(_config_size_values(available_slot_sizes)))
    return set(_legal_topfill_values_cached(config_values))


def _exact_config_slot_family(available_slot_sizes: SlotSizeSequence) -> list[int]:
    """Return the configured family plus only the legal exact final topfills that complete the 754 cm stack.

    A topfill is legal only when it is the exact residual value created by a real lower stack under
    the support-band rule. Synthetic values like 104 or 174 are not allowed unless they arise from a
    valid full-height completion of the configured family.
    """
    raw = [
        int(round(float(size)))
        for size in (available_slot_sizes or [])
        if float(size) > 0.0 and float(size) <= float(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)
    ]
    if not raw:
        return []

    family = sorted({size for size in raw if size % 10 in (4, 9)})
    if not family:
        return []

    legal_topfill_values = _legal_topfill_values(family)
    return sorted(set(family) | legal_topfill_values)


def _column_support_band_is_valid(column_slots: list[float]) -> bool:
    """Return True when the support beam beneath the top slot clears the minimum legal band."""
    slots = [float(value) for value in (column_slots or []) if float(value) > 0.0]
    if len(slots) <= 1:
        return False
    support_below_top = sum(slots[:-1]) + common.BEAM_HEIGHT * max(len(slots) - 2, 0)
    return support_below_top >= 504.0 - 1e-9


def _rack_profiles_are_exactly_uniform(
    column_assignments: dict[str, list[float]],
    column_keys: list[str],
) -> bool:
    """Return True when each rack uses exactly one profile across its assigned columns."""
    profiles_by_rack: dict[str, set[tuple[int, ...]]] = defaultdict(set)
    seen_nonempty = False
    for column_key in column_keys:
        slots = [float(value) for value in column_assignments.get(column_key, []) if float(value) > 0.0]
        if not slots:
            continue
        seen_nonempty = True
        canonical = tuple(sorted(int(round(float(value))) for value in slots))
        rack_key = _rack_from_column_key(column_key)
        profiles_by_rack[rack_key].add(canonical)
    return seen_nonempty and all(len(canonical_profiles) == 1 for canonical_profiles in profiles_by_rack.values())


def _minimum_required_counts_are_satisfied(
    column_assignments: dict[str, list[float]],
    column_keys: list[str],
    available_slot_sizes: SlotSizeSequence = None,
    minimum_required_counts: dict[float, int] | None = None,
) -> bool:
    """Return True when the completed layout meets the required exact-family counts.

    The check must be done on the whole layout after all columns are assigned. It is not a per-column
    validation: a single rack/column may legitimately be missing a given family as long as the overall
    completed layout satisfies the minimum exact requirements.
    """
    if minimum_required_counts is None:
        return True

    layout_counts: dict[int, int] = defaultdict(int)
    saw_nonempty = False
    all_slots: list[float] = []
    for column_key in column_keys:
        slots = [float(value) for value in column_assignments.get(column_key, []) if float(value) > 0.0]
        if not slots:
            continue
        saw_nonempty = True
        all_slots.extend(slots)

    if not saw_nonempty:
        return False

    for slot_size in all_slots:
        rounded = int(round(float(slot_size)))
        counted_as = _effective_requirement_slot_size(rounded, all_slots, available_slot_sizes)
        if counted_as is None:
            return False
        layout_counts[counted_as] += 1

    for size, required_count in minimum_required_counts.items():
        required_int = int(round(float(size)))
        if int(required_count) > 0 and layout_counts.get(required_int, 0) < int(required_count):
            return False

    return True


def _layout_assignments_are_feasible(
    column_assignments: dict[str, list[float]],
    column_keys: list[str],
    minimum_slot_size: float,
    available_slot_sizes: SlotSizeSequence = None,
    minimum_required_counts: dict[float, int] | None = None,
    enforce_minimum_total_locations: bool = False,
) -> bool:
    """Check per-column physical legality and defer exact minimum-family checks to the full layout.

    Empty warehouse columns are ignored for the final feasibility check; only occupied columns in the
    generated layout are validated. This matches the real layout state and prevents a valid generated
    candidate from being rejected just because the warehouse contains extra empty rack columns.
    """
    if not column_assignments:
        return False

    minimum_value = int(round(float(minimum_slot_size)))
    config_values: set[int] = set()
    legal_topfill_values: set[int] = set()
    if available_slot_sizes is not None:
        config_values = _config_size_values(available_slot_sizes)
        legal_topfill_values = _legal_topfill_values(available_slot_sizes)

    layout_counts: dict[int, int] = defaultdict(int)
    assigned_locations_total = 0
    nonempty_column_keys: list[str] = []
    for column_key in column_keys:
        slots = [float(value) for value in column_assignments.get(column_key, []) if float(value) > 0.0]
        if not slots:
            continue
        nonempty_column_keys.append(column_key)
        assigned_locations_total += len(slots)
        if any(float(value) <= 0.0 for value in slots):
            return False
        if len(slots) == 1:
            if int(round(float(slots[0]))) < minimum_value:
                return False
        elif any(int(round(float(value))) < minimum_value for value in slots[:-1]):
            return False
        if any(int(round(float(value))) < 4 for value in slots):
            return False
        if any(int(round(float(value))) > int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)) for value in slots):
            return False
        if available_slot_sizes is not None:
            for idx, value in enumerate(slots):
                rounded = int(round(float(value)))
                if rounded % 10 not in (4, 9):
                    return False
                if idx < len(slots) - 1 and rounded not in config_values:
                    return False
                if idx == len(slots) - 1:
                    if rounded in config_values and rounded < minimum_value:
                        return False
                    if rounded not in config_values:
                        if rounded < minimum_value:
                            return False
                        if rounded not in legal_topfill_values:
                            return False
        target_total = sum(slots) + (len(slots) - 1) * common.BEAM_HEIGHT
        if target_total < 0.0:
            return False

        for idx, slot_size in enumerate(slots):
            rounded = int(round(float(slot_size)))
            counted_as = _effective_requirement_slot_size(rounded, slots, available_slot_sizes)
            if counted_as is None:
                return False
            layout_counts[counted_as] += 1

    if not nonempty_column_keys:
        return False

    full_layout_target_reached = (
        enforce_minimum_total_locations
        or assigned_locations_total >= common._explicit_occupied_target_total()
    )

    # Final feasibility is a completed-layout proof, not a per-column validator. Any partial assignment
    # is allowed to remain incomplete until the full layout is assembled; only then do we apply the
    # exact-family minima and the complete-height checks.
    if not full_layout_target_reached:
        return True

    if not _rack_profiles_are_exactly_uniform(column_assignments, nonempty_column_keys):
        return False

    for column_key in nonempty_column_keys:
        slots = [float(value) for value in column_assignments.get(column_key, []) if float(value) > 0.0]
        if not slots:
            return False
        target_total = sum(slots) + (len(slots) - 1) * common.BEAM_HEIGHT
        if abs(target_total - common.MAX_USED_HEIGHT_BASE) > 1e-6:
            return False
        if not _column_support_band_is_valid(slots):
            return False
        if available_slot_sizes is not None:
            final_value = int(round(float(slots[-1])))
            if final_value not in config_values:
                if final_value not in legal_topfill_values:
                    return False
                lower_values = [float(value) for value in slots[:-1]]
                if abs(sum(slots) + (len(slots) - 1) * common.BEAM_HEIGHT - common.MAX_USED_HEIGHT_BASE) <= 1e-9:
                    required_completion = (
                        common.MAX_USED_HEIGHT_BASE
                        - sum(lower_values)
                        - common.BEAM_HEIGHT * len(lower_values)
                    )
                    if abs(float(final_value) - float(required_completion)) > 1e-9:
                        return False
            if len(slots) > 1 and any(int(round(float(value))) not in config_values for value in slots[:-1]):
                return False
        if abs(target_total - common.MAX_USED_HEIGHT_BASE) > 1e-6 and target_total > common.MAX_USED_HEIGHT_BASE + 1e-6:
            return False
    if (
        minimum_required_counts is not None
        and not _minimum_required_counts_are_satisfied(
            column_assignments,
            nonempty_column_keys,
            available_slot_sizes=available_slot_sizes,
            minimum_required_counts=minimum_required_counts,
        )
    ):
        return False

    if enforce_minimum_total_locations and assigned_locations_total < common._explicit_occupied_target_total():
        return False

    return True


def _config_legal_slot_family(available_slot_sizes: SlotSizeSequence) -> list[int]:
    """Allow only the exact configured legal representative sizes, never nearby synthetic values."""
    return _exact_config_slot_family(available_slot_sizes)


def _column_physical_height_usage(column_slots: list[float]) -> float:
    """Return the full physical column usage including beam gap height."""
    slots = [float(value) for value in (column_slots or []) if float(value) > 0.0]
    if not slots:
        return 0.0
    return sum(slots) + max(len(slots) - 1, 0) * common.BEAM_HEIGHT


def _column_topfill_metadata(
    column_slots: list[float],
    available_slot_sizes: SlotSizeSequence = None,
) -> dict[str, float]:
    """Return legal bounded topfill metadata for a column.

    The top slot may be enlarged to consume the remaining physical capacity, but
    only to legal values ending in 4 or 9 and capped at 234 cm. This keeps the
    all-space usage behavior while preventing illegal filler values.
    """
    current = [float(value) for value in (column_slots or []) if float(value) > 0.0]
    if not current:
        return {
            "Original_Top_Slot_cm": 0.0,
            "Added_Height_cm": 0.0,
            "Adjusted_Top_Slot_cm": 0.0,
        }

    original_top = float(current[-1])
    if original_top is None:
        return {
            "Original_Top_Slot_cm": 0.0,
            "Added_Height_cm": 0.0,
            "Adjusted_Top_Slot_cm": 0.0,
        }

    allowed_total = common.MAX_USED_HEIGHT_BASE
    used_total = _column_physical_height_usage(current)
    remaining = max(allowed_total - used_total, 0.0)
    if remaining <= 1e-9:
        return {
            "Original_Top_Slot_cm": original_top,
            "Added_Height_cm": 0.0,
            "Adjusted_Top_Slot_cm": original_top,
        }

    legal_family = _config_legal_slot_family(available_slot_sizes or current)
    legal_values = sorted(
        value
        for value in legal_family
        if value >= int(round(original_top))
        and value <= int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM))
    )
    if not legal_values:
        legal_values = [int(round(original_top))]

    cap = int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM))
    adjusted_top = min(
        [value for value in legal_values if value <= cap],
        key=lambda value: (abs(float(value) - (original_top + remaining)), abs(float(value) - original_top)),
        default=int(round(original_top)),
    )
    adjusted_top = max(int(round(original_top)), min(int(adjusted_top), cap))
    return {
        "Original_Top_Slot_cm": float(original_top),
        "Added_Height_cm": max(float(adjusted_top) - float(original_top), 0.0),
        "Adjusted_Top_Slot_cm": float(adjusted_top),
    }


def _effective_slot_size_for_summary(slot_size: float, column_slots: list[float], available_slot_sizes: SlotSizeSequence = None) -> int | None:
    """Treat legal final topfill values as the configured slot family reached by rounding down.

    This keeps the reported slot-size distribution aligned with the actual underlying slot family rather
    than exposing synthetic topfilled buckets (for example, counting a 64 topfill against 34).
    """
    rounded = int(round(float(slot_size)))
    if not column_slots:
        return rounded

    config_values = sorted(set(_config_size_values(available_slot_sizes or column_slots)))
    if rounded in config_values:
        return rounded

    mapped = _nearest_configured_family_slot(rounded, config_values, column_slots)
    if mapped is not None:
        return mapped

    if rounded < min(config_values, default=rounded):
        return None
    return None


def _slot_signatures_from_location_rows(
    location_rows: list[dict[str, str]],
    available_slot_sizes: SlotSizeSequence = None,
) -> tuple[str, str]:
    # Recompute slot-distribution signatures directly from location-level rows,
    # excluding only the actual non-usable layout rows from capacity totals.
    # Legal topfill values are counted as their underlying lower-slot family for
    # summary output, because they represent a final completion of that family.
    exact_counts: dict[int, int] = defaultdict(int)
    by_column: dict[str, list[float]] = defaultdict(list)
    for row in location_rows:
        if str(row.get("Usable_Location", "YES")).strip().upper() == "NO":
            continue
        slot_size = common._to_float(row.get("Assigned_Slot_Size_cm"))
        if slot_size is None:
            continue
        column_key = f"{str(row.get('Rack', '')).strip()}{int(str(row.get('Column', '')).strip() or 0):02d}"
        by_column[column_key].append(float(slot_size))

    for column_key, slots in by_column.items():
        normalized_slots = [float(value) for value in slots if float(value) > 0.0]
        for slot in normalized_slots:
            effective = _effective_slot_size_for_summary(slot, normalized_slots, available_slot_sizes)
            if effective is None:
                continue
            exact_counts[effective] += 1

    distribution = "|".join(f"{size}:{count}" for size, count in sorted(exact_counts.items()))

    running = 0
    cumulative: dict[int, int] = {}
    for size in sorted(exact_counts.keys(), reverse=True):
        running += exact_counts[size]
        cumulative[size] = running
    cumulative_signature = "|".join(f"{size}:{cumulative[size]}" for size in sorted(cumulative.keys()))

    return distribution, cumulative_signature


def _rack_profile_rows_from_location_rows(
    layout_id: str,
    config_id: str,
    location_rows: list[dict[str, str]],
    config_slot_sizes: SlotSizeSequence = None,
) -> list[dict[str, str]]:
    """Aggregate per-rack profile details for compact candidate/final layout reporting."""
    rack_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in location_rows:
        rack = str(row.get("Rack", "")).strip()
        if rack:
            rack_rows[rack].append(row)

    rack_summary_rows: list[dict[str, str]] = []
    for rack in sorted(rack_rows):
        rows = rack_rows[rack]
        columns = sorted({str(item.get("Column", "")).strip() for item in rows if str(item.get("Column", "")).strip()})
        slot_counts: Counter[int] = Counter()
        column_profiles: dict[str, list[int]] = defaultdict(list)
        for row in rows:
            slot_size = common._to_float(row.get("Assigned_Slot_Size_cm"))
            if slot_size is not None:
                column = str(row.get("Column", "")).strip()
                raw_column_slots = [
                    common._to_float(item.get("Assigned_Slot_Size_cm"))
                    for item in rows
                    if str(item.get("Column", "")).strip() == column
                ]
                effective_slot = _effective_slot_size_for_summary(slot_size, [float(value) for value in raw_column_slots if value is not None], config_slot_sizes)
                if effective_slot is not None:
                    slot_counts[effective_slot] += 1
                if column:
                    column_profiles[column].append(int(round(float(slot_size))))

        representative_column = min(columns, key=lambda value: int(value)) if columns else ""
        representative_profile = []
        if representative_column:
            column_rows = sorted(
                [row for row in rows if str(row.get("Column", "")).strip() == representative_column],
                key=lambda row: int(str(row.get("Row", "")).strip() or "0"),
            )
            representative_profile = []
            for row in column_rows:
                slot_size = common._to_float(row.get("Assigned_Slot_Size_cm"))
                if slot_size is not None:
                    representative_profile.append(int(round(float(slot_size))))
        if not representative_profile:
            representative_profile = [
                int(round(float(value))) for value in slot_counts.elements()
            ]

        slot_distribution = "|".join(
            f"{size}:{count}"
            for size, count in sorted(slot_counts.items())
        )
        rack_summary_rows.append(
            {
                "Layout_ID": layout_id,
                "Config_ID": config_id,
                "Rack": rack,
                "Rack_Column_Count": str(len(columns)),
                "Rack_Columns": ",".join(f"{rack}{int(column):02d}" for column in columns),
                "Assigned_Locations_Total": str(len(rows)),
                "Slot_Size_Distribution": slot_distribution,
                "Rack_Profile_Order": ",".join(str(value) for value in representative_profile),
                "Rack_Profile_Signature": "|".join(
                    f"{size}:{count}"
                    for size, count in sorted(slot_counts.items())
                ),
            }
        )
    return rack_summary_rows


def _pre_robust_sort_key(summary_row: dict[str, str]) -> tuple[int, int, int, float, int, int]:
    feasible_penalty = 0 if str(summary_row.get("Layout_Feasible", "")).strip().upper() == "YES" else 1
    additional_beams = common._to_int_default(summary_row.get("Additional_Beams_Required"), 0)
    return (
        feasible_penalty,
        common._to_int_default(summary_row.get("Beam_Relocations_Total"), 0),
        additional_beams,
        common._to_float(summary_row.get("Space_Left")) or 0.0,
        -common._to_int_default(summary_row.get("Assigned_Locations_Total"), 0),
    )


def build_layout_generation() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Generate candidate layouts and emit summary, column, and location-level outputs."""
    print(f"[Stage 6] starting layout generation for configs from {INPUT_CONFIG_FILE.name} and {INPUT_CAPACITY_FILE.name}")
    prepared_rows = _read_csv(INPUT_PREPARED)
    raw_configs = _candidate_configs()
    configs = _candidate_configs_for_exhaustive_search(raw_configs)
    if not configs:
        configs = raw_configs
    print(f"[Stage 6 DEBUG] shortlisted config ids = {[str(row.get('Config_ID', '')).strip() for row in configs]}")
    capacity_rows = _capacity_rows_by_config()
    layout_thresholds_by_rack: dict[str, tuple[int, float]] = {}
    layout_columns = common._build_layout_columns(prepared_rows)

    config_style_bundles: list[ConfigStyleBundle] = []
    print(f"[Stage 6] shortlisted {len(configs)} configs for exact-fill evaluation")

    candidate_layout_rows: list[dict[str, str]] = []
    candidate_layout_location_rows: list[dict[str, str]] = []
    candidate_layout_column_rows: list[dict[str, str]] = []
    candidate_layout_location_rows_all: list[dict[str, str]] = []
    candidate_layout_column_rows_all: list[dict[str, str]] = []
    candidate_layout_rack_rows_all: list[dict[str, str]] = []
    candidate_layout_rack_rows: list[dict[str, str]] = []
    generated_profile_rows: list[dict[str, str]] = []
    beam_map_rows = _read_csv(INPUT_LOCATION_BEAM_MAP)
    beam_height_rows = _read_csv(INPUT_BEAM_HEIGHT_COORDS)
    current_beam_units, beam_segments, current_beam_unit_heights = common._build_current_beam_units_and_segments(
        beam_map_rows,
        prepared_rows,
        beam_height_rows,
    )
    initial_beam_count, initial_grid_count = common._initial_beam_grid_counts(prepared_rows, current_beam_units)
    # Build layout variants for every shortlisted config and layout style.
    layout_counter = 1
    for config in configs:
        config_id = str(config.get("Config_ID", "")).strip()
        config_start_time = time.perf_counter()
        config_time_limit_seconds = PROFILE_GENERATION_TIMEOUT_SECONDS + RACK_SEARCH_TIMEOUT_SECONDS
        _LAST_STAGE6_CONFIG_DEADLINE = config_start_time + config_time_limit_seconds
        _LAST_STAGE6_TIMEOUTS["profile_generation"] = False
        _LAST_STAGE6_TIMEOUTS["rack_search"] = False
        _LAST_STAGE6_PROFILE_POOL = []
        rows = capacity_rows.get(config_id, [])
        if not rows:
            continue

        base_exact_counts = _base_exact_counts(rows)
        if not base_exact_counts:
            continue

        print(f"[Stage 6] config {config_id}: base counts = {[f'{int(size)}:{count}' for size, count in sorted(base_exact_counts.items())]}")
        layout_id = f"LAY_{layout_counter:03d}"
        layout_counter += 1
        if _raise_config_timeout_and_return(config_id, config_start_time, config_time_limit_seconds):
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            continue
        config_slot_sizes = _slot_sizes_from_capacity(rows)
        if config_id == "CFG_006":
            print(f"[Stage 6 DEBUG] config {config_id}: entering fast probe with slot sizes {config_slot_sizes} and base counts {base_exact_counts}")

        # The deficit-coverage rule is the actual assignment decision in Stage 6.
        # Subsequent repair passes are disabled here so they cannot rewrite the selected rack profile
        # away from the best remaining-deficit choice.
        print(f"[Stage 6] config {config_id}: building rack profiles from slot family {config_slot_sizes}")
        column_assignments = _build_deficit_coverage_layout(
            rack_columns=layout_columns,
            required_counts={float(size): int(count) for size, count in base_exact_counts.items()},
            config_slot_sizes=config_slot_sizes,
            config_deadline=_LAST_STAGE6_CONFIG_DEADLINE,
            config_id=config_id,
        )
        if config_id == "CFG_006":
            nonempty_count = sum(1 for values in column_assignments.values() if values)
            print(
                f"[Stage 6 DEBUG] config {config_id}: after_build_deficit_coverage_layout | "
                f"nonempty_columns={nonempty_count} | "
                f"sample={[(k, v[:3]) for k, v in column_assignments.items() if v][:2]} | "
                f"required_counts={base_exact_counts}"
            )
        if _raise_config_timeout_and_return(config_id, config_start_time, config_time_limit_seconds):
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            generated_profile_rows.append({
                "Config_ID": config_id,
                "Profile_Index": "0",
                "Profile_Values": "",
                "Profile_Signature": "TIMED_OUT",
                "Source_Slot_Sizes": ",".join(f"{int(size)}" for size in config_slot_sizes),
                "Status": "TIMED_OUT",
            })
            continue
        if _LAST_STAGE6_CONFIG_DEADLINE is not None and time.perf_counter() >= _LAST_STAGE6_CONFIG_DEADLINE:
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            generated_profile_rows.append({
                "Config_ID": config_id,
                "Profile_Index": "0",
                "Profile_Values": "",
                "Profile_Signature": "TIMED_OUT",
                "Source_Slot_Sizes": ",".join(f"{int(size)}" for size in config_slot_sizes),
                "Status": "TIMED_OUT",
            })
            continue
        # The legal rack profile is selected directly from the generated legal candidate pool.
        # No post-assignment repair or rebuild step is allowed to mutate the chosen profile.
        print(f"[Stage 6] config {config_id}: assigned profiles to {len(column_assignments)} rack columns")
      

        expansion_slot_sizes = sorted(float(slot_size) for slot_size in base_exact_counts)
        layout_alignment_conversions = 0
        smallest_config_slot = min(expansion_slot_sizes) if expansion_slot_sizes else 0.0
        feasible_profile_pool = [list(profile) for profile in _LAST_STAGE6_PROFILE_POOL]
        if not feasible_profile_pool and config_slot_sizes:
            try:
                feasible_profile_pool = _generate_feasible_rack_profiles(
                    config_slot_sizes,
                    timeout_seconds=PROFILE_GENERATION_TIMEOUT_SECONDS,
                    config_deadline=_LAST_STAGE6_CONFIG_DEADLINE,
                )
            except Stage6ProfileGenerationTimeout:
                print(f"[Stage 6] config {config_id}: stopped because the profile generation timeout was exceeded during reporting")
                _LAST_STAGE6_TIMEOUTS["profile_generation"] = True
                candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
                generated_profile_rows.append({
                    "Config_ID": config_id,
                    "Profile_Index": "0",
                    "Profile_Values": "",
                    "Profile_Signature": "TIMED_OUT",
                    "Source_Slot_Sizes": ",".join(f"{int(size)}" for size in config_slot_sizes),
                    "Status": "TIMED_OUT",
                })
                continue
        for profile_index, profile in enumerate(feasible_profile_pool, start=1):
            generated_profile_rows.append({
                "Config_ID": config_id,
                "Profile_Index": str(profile_index),
                "Profile_Values": ",".join(str(int(round(float(value)))) for value in profile),
                "Profile_Signature": "|".join(f"{int(round(float(value)))}" for value in profile),
                "Source_Slot_Sizes": ",".join(f"{int(size)}" for size in config_slot_sizes),
                "Status": "GENERATED",
            })

        if _raise_config_timeout_and_return(config_id, config_start_time, config_time_limit_seconds):
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            continue
        if _LAST_STAGE6_CONFIG_DEADLINE is not None and time.perf_counter() >= _LAST_STAGE6_CONFIG_DEADLINE:
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            continue

        # Baseline Stage 6 keeps the generated layout geometry aligned with the shared
        # beam-structure model so material delta calculations remain meaningful even without
        # any heuristic repair/rebuild pass.
        generated_location_rows = common._build_generated_layout_location_rows(
            layout_id,
            config_id,
            "layout",
            column_assignments,
            segments=beam_segments,
            layout_thresholds_by_rack=layout_thresholds_by_rack,
        )
        layout_slot_distribution, layout_slot_cumulative = _slot_signatures_from_location_rows(
            generated_location_rows,
            available_slot_sizes=expansion_slot_sizes,
        )
   
        proposed_beam_units, proposed_beam_unit_heights = common._build_proposed_beam_units_from_layout_rows(
            generated_location_rows,
            beam_segments,
        )
        current_units_by_column = common._beam_units_by_column(beam_map_rows)
        proposed_units_by_column = common._beam_units_by_column(generated_location_rows)
        relocation_total, relocation_by_column, removed_by_column, added_by_column = common._beam_relocations(
            current_beam_units,
            proposed_beam_units,
            current_beam_unit_heights,
            proposed_beam_unit_heights,
            current_units_by_column=current_units_by_column,
            proposed_units_by_column=proposed_units_by_column,
        )
        required_beams, required_grids, additional_beams, additional_grids = common._material_requirements(
            initial_beam_count,
            initial_grid_count,
            proposed_beam_units,
            generated_location_rows,
        )

        # Compute utilization and implementation-effort KPIs per configuration.
        assigned_total = max(len(generated_location_rows) - common._fixed_layout_location_total(), 0)
        required_locations_total = sum(base_exact_counts.values())
        physical_used_by_column = {
            column_key: _column_physical_height_usage(slots)
            for column_key, slots in column_assignments.items()
        }
        total_used_height = sum(physical_used_by_column.values())
        total_allowed_height = len(layout_columns) * common.MAX_USED_HEIGHT_BASE
        space_utilization = (total_used_height / total_allowed_height) if total_allowed_height > 0 else 0.0
        capacity_margin = assigned_total - required_locations_total
        pct_rack_height_used = space_utilization * 100.0
        space_left = sum(
            max(common.MAX_USED_HEIGHT_BASE - physical_used_by_column.get(column_key, 0.0), 0.0)
            for column_key in layout_columns
        )

        # Final feasibility must reflect the repaired layout itself, not the stale
        # signal from the initial allocator. The export gate validates the final
        # assignment state rather than rejecting everything that was not initially
        # marked feasible.
        final_layout_feasible = (
            assigned_total >= common._explicit_occupied_target_total()
            and capacity_margin >= 0
            and space_utilization <= 1.0
            and _layout_assignments_are_feasible(
                column_assignments,
                list(column_assignments.keys()),
                float(smallest_config_slot),
                expansion_slot_sizes,
                minimum_required_counts=base_exact_counts,
                enforce_minimum_total_locations=True,
            )
        )
        if not final_layout_feasible:
            print(
                f"[Stage 6 DEBUG] config {config_id}: final_layout_feasible=FALSE | "
                f"assigned_total={assigned_total} | required_total={common._explicit_occupied_target_total()} | "
                f"capacity_margin={capacity_margin} | space_utilization={space_utilization:.4f} | "
                f"column_assignments={len(column_assignments)} | nonempty={sum(1 for v in column_assignments.values() if v)}"
            )
        feasible_layout = final_layout_feasible
        feasible_profiles_used_total = len({
            tuple(sorted(int(round(float(value))) for value in profile))
            for profile in column_assignments.values()
            if profile
        })
        allocation_diagnostics = {
            "Feasible_Profiles_Considered_Total": float(len(feasible_profile_pool)),
            "Feasible_Profiles_Used_Total": float(feasible_profiles_used_total),
        }

        profile_generation_elapsed = _LAST_STAGE6_STEP_TIMINGS.get("profile_generation", 0.0)
        shortlist_elapsed = _LAST_STAGE6_STEP_TIMINGS.get("profile_shortlist", 0.0)
        rack_search_elapsed = _LAST_STAGE6_STEP_TIMINGS.get("rack_search", 0.0)
        profile_generation_timed_out = _LAST_STAGE6_TIMEOUTS.get("profile_generation", False)
        rack_search_timed_out = _LAST_STAGE6_TIMEOUTS.get("rack_search", False)
        total_runtime_seconds = _runtime_clamped_to_limit(config_start_time, config_time_limit_seconds)
        print(
            f"[Stage 6] config {config_id}: timings -> "
            f"profile_generation={profile_generation_elapsed:.3f}s | "
            f"profile_shortlist={shortlist_elapsed:.3f}s | "
            f"rack_search={rack_search_elapsed:.3f}s | "
            f"total={total_runtime_seconds:.3f}s | "
            f"profile_generation_timeout={str(profile_generation_timed_out).upper()} | "
            f"rack_search_timeout={str(rack_search_timed_out).upper()}"
        )
        summary_row = {
                "Layout_ID": layout_id,
                "Config_ID": config_id,
                "Runtime_Seconds": f"{total_runtime_seconds:.3f}",
                "Runtime_Minutes": f"{total_runtime_seconds / 60.0:.3f}",
                "Profile_Generation_Seconds": f"{profile_generation_elapsed:.3f}",
                "Profile_Shortlist_Seconds": f"{shortlist_elapsed:.3f}",
                "Rack_Search_Seconds": f"{rack_search_elapsed:.3f}",
                "Profile_Generation_Timeout": "YES" if profile_generation_timed_out else "NO",
                "Rack_Search_Timeout": "YES" if rack_search_timed_out else "NO",
                "Layout_Feasible": "YES" if final_layout_feasible else "NO",
                "Allocation_Feasible_Initial": "YES" if feasible_layout else "NO",
                "Required_Locations_Total": str(required_locations_total),
                "Total_Locations": str(assigned_total),
                "Capacity_Margin": str(capacity_margin),
                "Assigned_Used_Height_Total": f"{total_used_height:.3f}",
                "Total_Allowed_Height": f"{total_allowed_height:.3f}",
                "Space_Left": f"{space_left:.3f}",
                "Beam_Relocations_Total": str(relocation_total),
                "Initial_Beams_Total": str(initial_beam_count),
                "Required_Beams_Total": str(required_beams),
                "Additional_Beams_Required": str(additional_beams),
                "Initial_Grids_Total": str(initial_grid_count),
                "Required_Grids_Total": str(required_grids),
                "Additional_Grids_Required": str(additional_grids),
                "Percentage_Rack_Height_Used": f"{pct_rack_height_used:.2f}",
                "Minimum_Required_Counts": "|".join(f"{int(size)}:{count}" for size, count in sorted(base_exact_counts.items())),
                "Additional_Fill_Counts": _additional_fill_signature(column_assignments, base_exact_counts),
                "Slot_Composition_Signature": "|".join(f"{int(size)}:{count}" for size, count in sorted(base_exact_counts.items())),
                "Layout_Slot_Size_Distribution": layout_slot_distribution,
                "Layout_Slot_Size_Cumulative_Coverage": layout_slot_cumulative,
                "Source_Slot_Sizes": common._encode_excel_text(",".join(f"{int(size)}" for size in config_slot_sizes)),
                "Layout_Usable_Alignment_Conversions": str(layout_alignment_conversions),
                "Feasible_Profiles_Considered_Total": str(int(allocation_diagnostics.get("Feasible_Profiles_Considered_Total", 0.0))),
                "Feasible_Profiles_Used_Total": str(int(allocation_diagnostics.get("Feasible_Profiles_Used_Total", 0.0))),
            }
        candidate_layout_rows.append({key: str(value) for key, value in summary_row.items()})

        # Capture per-column slot mix and beam movement details.
        slot_mix_by_column: dict[str, dict[float, int]] = defaultdict(lambda: defaultdict(int))
        for row in generated_location_rows:
            rack = str(row.get("Rack", "")).strip()
            column = str(row.get("Column", "")).strip()
            slot = common._to_float(row.get("Assigned_Slot_Size_cm"))
            if rack and column and slot is not None:
                rack_column = f"{rack}{int(column):02d}"
                slot_mix_by_column[rack_column][float(slot)] += 1
        for rack_column, slot_counts in list(slot_mix_by_column.items()):
            normalized_slots = [float(value) for value, _ in sorted(slot_counts.items()) for _ in range(int(_))]
            effective_counts: dict[float, int] = defaultdict(int)
            for slot in normalized_slots:
                effective = _effective_slot_size_for_summary(slot, normalized_slots, expansion_slot_sizes)
                if effective is None:
                    continue
                effective_counts[float(effective)] += 1
            slot_mix_by_column[rack_column] = dict(sorted(effective_counts.items()))

        style_column_rows: list[dict[str, str]] = []
        for column_key, slots in sorted(column_assignments.items()):
            used = physical_used_by_column.get(column_key, 0.0)
            beam_count = max(len(slots) - 1, 0)
            allowed = common.MAX_USED_HEIGHT_BASE
            mix = slot_mix_by_column.get(column_key, {})
            topfill = _column_topfill_metadata(slots, expansion_slot_sizes)
            style_column_rows.append(
                {
                    "Layout_ID": layout_id,
                    "Config_ID": config_id,
                    "Rack_Column": column_key,
                    "Beam_Count_Used": str(beam_count),
                    "Allowed_Used_Height_cm": f"{allowed:.3f}",
                    "Assigned_Used_Height_cm": f"{used:.3f}",
                    "Remaining_Height_cm": f"{max(allowed - used, 0.0):.3f}",
                    "Fill_Ratio": f"{(used / allowed) if allowed > 0 else 0.0:.4f}",
                    "Beam_Relocations_In_Column": str(relocation_by_column.get(column_key, 0)),
                    "Removed_Beams_In_Column": str(removed_by_column.get(column_key, 0)),
                    "Added_Beams_In_Column": str(added_by_column.get(column_key, 0)),
                    "TopFill_Original_Top_Slot_cm": f"{topfill['Original_Top_Slot_cm']:.3f}",
                    "TopFill_Added_Height_cm": f"{topfill['Added_Height_cm']:.3f}",
                    "TopFill_Adjusted_Top_Slot_cm": f"{topfill['Adjusted_Top_Slot_cm']:.3f}",
                    "Slot_Size_Distribution": "|".join(
                        f"{int(slot_size)}:{count}" for slot_size, count in sorted(mix.items())
                    ),
                }
            )

    if config_style_bundles:
        def _bundle_score(bundle: ConfigStyleBundle) -> tuple[int, int, int, float, int, int]:
            return min(_pre_robust_sort_key(candidate[0]) for candidate in candidates)

        by_composition: dict[str, list[ConfigStyleBundle]] = defaultdict(list)
        for bundle in config_style_bundles:
            signature = str(candidates[0][0].get("Slot_Composition_Signature", "")).strip() if candidates else ""
            by_composition[signature].append(bundle)

        composition_winners: list[ConfigStyleBundle] = []
        for bundles in by_composition.values():
            composition_winners.append(min(bundles, key=_bundle_score))

        finalists: list[ConfigStyleBundle] = []
        for bundles in by_composition.values():
            ranked_bundles = sorted(bundles, key=_bundle_score)
            finalists.extend(ranked_bundles[:PRE_ROBUST_COMPOSITION_KEEP])

        finalists = sorted(finalists, key=_bundle_score)
        selected_config_ids = {config_id for config_id, _candidates in finalists}

        if len(selected_config_ids) < PRE_ROBUST_MAX_SELECTED_CONFIGS:
            ranked_remaining = sorted(
                [bundle for bundle in config_style_bundles if bundle[0] not in selected_config_ids],
                key=_bundle_score,
            )
            needed = max(0, PRE_ROBUST_MAX_SELECTED_CONFIGS - len(finalists))
            finalists.extend(ranked_remaining[:needed])
            selected_config_ids = {config_id for config_id, _candidates in finalists}

        if PRE_ROBUST_LAYOUT_LIMIT is not None:
            target_count = min(PRE_ROBUST_LAYOUT_LIMIT, len(config_style_bundles))
            finalists = sorted(finalists, key=_bundle_score)[:target_count]
            selected_config_ids = {config_id for config_id, _candidates in finalists}

            if len(finalists) < target_count:
                ranked_remaining = sorted(
                    [bundle for bundle in config_style_bundles if bundle[0] not in selected_config_ids],
                    key=_bundle_score,
                )
                needed = target_count - len(finalists)
                finalists.extend(ranked_remaining[:needed])
                selected_config_ids = {config_id for config_id, _candidates in finalists}

        finalists = sorted(finalists, key=_bundle_score)
        config_rank = {
            config_id: index
            for index, (config_id, _candidates) in enumerate(finalists, start=1)
        }

        for config_id, candidates in config_style_bundles:
            for summary, location_rows, column_rows, _signature in candidates:
                final_layout_feasible = str(summary.get("Layout_Feasible", "NO")).strip().upper() == "YES"
                # Keep the by-column and by-location exports populated even when a layout fails the
                # final feasibility gate: these exports are diagnostic and should reflect every generated
                # candidate assignment, not only the subset that passes the final boolean filter.
                candidate_layout_column_rows_all.extend(column_rows)
                candidate_layout_location_rows_all.extend(location_rows)
                rack_rows = _rack_profile_rows_from_location_rows(
                    str(summary.get("Layout_ID", "")),
                    config_id,
                    location_rows,
                    config_slot_sizes,
                )
                candidate_layout_rack_rows_all.extend(rack_rows)
                if config_id in selected_config_ids:
                    summary["Pre_Robustness_Status"] = "SELECTED"
                    summary["Pre_Robustness_Rank"] = str(config_rank.get(config_id, ""))
                    summary["Pre_Robustness_Prune_Reason"] = ""
                    candidate_layout_column_rows.extend(column_rows)
                    candidate_layout_location_rows.extend(location_rows)
                    candidate_layout_rack_rows.extend(rack_rows)
                else:
                    summary["Pre_Robustness_Status"] = "PRUNED"
                    summary["Pre_Robustness_Rank"] = ""
                    summary["Pre_Robustness_Prune_Reason"] = "Outside pre-robust top configuration set"

                candidate_layout_rows.append({key: str(value) for key, value in summary.items()})

    print(f"[Stage 6] generated {len(candidate_layout_rows)} candidate summaries across {len(config_style_bundles)} selected config bundles")

    profile_export_fieldnames = [
        "Config_ID",
        "Profile_Index",
        "Profile_Values",
        "Profile_Signature",
        "Source_Slot_Sizes",
        "Status",
    ]
    _write_csv_preserve_with_fallback(
        LAYOUT_OUTPUT_DIR / "Generated_Profiles_By_Config.csv",
        profile_export_fieldnames,
        [{field: str(row.get(field, "")) for field in profile_export_fieldnames} for row in generated_profile_rows],
    )

    # Write layout-level KPIs.
    summary_export_fieldnames = [
        "Config_ID",
        "Runtime_Seconds",
        "Runtime_Minutes",
        "Profile_Generation_Seconds",
        "Profile_Shortlist_Seconds",
        "Rack_Search_Seconds",
        "Profile_Generation_Timeout",
        "Rack_Search_Timeout",
        "Layout_Feasible",
        "Allocation_Feasible_Initial",
        "Required_Locations_Total",
        "Total_Locations",
        "Capacity_Margin",
        "Assigned_Used_Height_Total",
        "Total_Allowed_Height",
        "Space_Left",
        "Beam_Relocations_Total",
        "Initial_Beams_Total",
        "Required_Beams_Total",
        "Additional_Beams_Required",
        "Initial_Grids_Total",
        "Required_Grids_Total",
        "Additional_Grids_Required",
        "Percentage_Rack_Height_Used",
        "Minimum_Required_Counts",
        "Additional_Fill_Counts",
        "Layout_Slot_Size_Distribution",
        "Layout_Slot_Size_Cumulative_Coverage",
        "Source_Slot_Sizes",
        "Layout_Usable_Alignment_Conversions",
        "Feasible_Profiles_Considered_Total",
        "Feasible_Profiles_Used_Total",
    ]
    summary_output_rows = [{key: str(value) for key, value in row.items()} for row in candidate_layout_rows]

    _write_csv_preserve_with_fallback(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary.csv",
        summary_export_fieldnames,
        [{field: str(row.get(field, "")) for field in summary_export_fieldnames} for row in summary_output_rows],
    )
    preserve_metric_fields = {
        "Beam_Relocations_Total",
        "Initial_Beams_Total",
        "Required_Beams_Total",
        "Additional_Beams_Required",
        "Initial_Grids_Total",
        "Required_Grids_Total",
        "Additional_Grids_Required",
        "Profile_Generation_Timeout",
        "Rack_Search_Timeout",
        "Layout_Feasible",
        "Allocation_Feasible_Initial",
    }
    common._write_csv_clean(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary.csv",
        summary_export_fieldnames,
        [{field: str(row.get(field, "")) for field in summary_export_fieldnames} for row in summary_output_rows],
        preserve_fields=preserve_metric_fields,
    )

    _write_csv_preserve_with_fallback(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_By_Rack_Column.csv",
        [
            "Config_ID",
            "Rack_Column",
            "Beam_Count_Used",
            "Allowed_Used_Height_cm",
            "Assigned_Used_Height_cm",
            "Remaining_Height_cm",
            "Fill_Ratio",
            "Beam_Relocations_In_Column",
            "Removed_Beams_In_Column",
            "Added_Beams_In_Column",
            "TopFill_Original_Top_Slot_cm",
            "TopFill_Added_Height_cm",
            "TopFill_Adjusted_Top_Slot_cm",
            "Slot_Size_Distribution",
        ],
        [
            {
                field: str(row.get(field, ""))
                for field in [
                    "Config_ID",
                    "Rack_Column",
                    "Beam_Count_Used",
                    "Allowed_Used_Height_cm",
                    "Assigned_Used_Height_cm",
                    "Remaining_Height_cm",
                    "Fill_Ratio",
                    "Beam_Relocations_In_Column",
                    "Removed_Beams_In_Column",
                    "Added_Beams_In_Column",
                    "TopFill_Original_Top_Slot_cm",
                    "TopFill_Added_Height_cm",
                    "TopFill_Adjusted_Top_Slot_cm",
                    "Slot_Size_Distribution",
                ]
            }
            for row in candidate_layout_column_rows_all
        ],
    )

    _write_csv_clean_with_fallback(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_By_Location.csv",
        [
            "Config_ID",
            "Location",
            "Rack",
            "Column",
            "Row",
            "Beam_Coordinate",
            "Beam_Height_Range_cm",
            "Assigned_Slot_Size_cm",
            "Usable_Location",
        ],
        [
            {
                field: str(row.get(field, ""))
                for field in [
                    "Config_ID",
                    "Location",
                    "Rack",
                    "Column",
                    "Row",
                    "Beam_Coordinate",
                    "Beam_Height_Range_cm",
                    "Assigned_Slot_Size_cm",
                    "Usable_Location",
                ]
            }
            for row in candidate_layout_location_rows_all
        ],
    )

    rack_export_fieldnames = [
        "Layout_ID",
        "Config_ID",
        "Rack",
        "Rack_Column_Count",
        "Rack_Columns",
        "Assigned_Locations_Total",
        "Slot_Size_Distribution",
        "Rack_Profile_Order",
        "Rack_Profile_Signature",
    ]
    _write_csv_clean_with_fallback(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_By_Rack.csv",
        rack_export_fieldnames,
        [{field: str(row.get(field, "")) for field in rack_export_fieldnames} for row in candidate_layout_rack_rows_all],
    )

    method_label = "Baseline"
    empty_rows = _empty_locations_rows_by_slot_size(summary_output_rows, method_label)
    _write_csv_clean_with_fallback(
        LAYOUT_OUTPUT_DIR / "Empty_Locations_By_Slot_Size.csv",
        [
            "Method",
            "Config_ID",
            "Slot_Size_cm",
            "Occupied",
            "Total_Locations_In_Layout",
            "Empty",
        ],
        empty_rows,
    )

    return candidate_layout_rows, candidate_layout_column_rows, candidate_layout_location_rows


if __name__ == "__main__":
    # Stage 6 entrypoint: build and persist all candidate layouts.
    print("[Stage 6] entrypoint starting")
    layout_rows, column_rows, location_rows = build_layout_generation()
    print(
        "[Stage 6] complete. "
        f"Layouts: {len(layout_rows)}, columns: {len(column_rows)}, locations: {len(location_rows)}."
    )
