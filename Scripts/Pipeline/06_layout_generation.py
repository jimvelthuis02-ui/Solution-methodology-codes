import csv
import heapq
import os
import re
import time
from collections import Counter, defaultdict
from functools import lru_cache
import itertools
import sys
from pathlib import Path
from typing import Any, Sequence, TypeAlias

PIPELINE_ROOT = Path(__file__).resolve().parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common
from heuristic_output_utils import suffix_output_tree


# Stage 6 source tables and output destinations.
INPUT_CONFIG_FILE = common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv"
INPUT_CAPACITY_SUMMARY_FILE = common.STAGE5_OUTPUT_DIR / "Capacity_Determination_Summary.csv"
INPUT_PREPARED = common.STAGE1_OUTPUT_DIR / "Location_Details_Prepared.csv"
INPUT_LOCATION_BEAM_MAP = common.STAGE1_OUTPUT_DIR / "Location_Beam_Map.csv"
INPUT_BEAM_HEIGHT_COORDS = common.STAGE1_OUTPUT_DIR / "Beam_Height_Coordinates.csv"
LAYOUT_OUTPUT_DIR = Path(os.environ.get("PIPELINE_STAGE6_OUTPUT_DIR", common.STAGE6_OUTPUT_DIR))
PICKING_INPUT_FILE = common.ROOT / "Input files" / "Locations" / "Most frequently picked items heights 24-9 good.csv"
# Profile-search budgets and per-config work limits.
MAX_REFINEMENT_SWAPS_PER_RACK = 3
PROFILE_ORDERING_LIMIT = 10000
MAX_SEARCH_STATES = 256
MAX_PROFILE_OPTIONS = 256
# The pre-robust pass should keep every valid generated layout candidate rather
# than artificially chopping the search space down to a fixed-size shortlist.
PRE_ROBUST_LAYOUT_LIMIT = None
EXHAUSTIVE_SEARCH_CONFIG_LIMIT = 1000
STAGE6_CONFIG_LIMIT = 180
IMPLEMENTATION_STYLE = "implementation"
# Optional candidate filtering and profile-screening defaults.
# Stage 6 should evaluate all valid configurations with K >= 3; the earlier 3-slot smoke focus was
# artificially restricting the search space and creating false negatives for larger valid families.
STAGE6_CONFIG_SLOT_SIZE_FOCUS = None
DEFAULT_TARGET_CONFIGS = "CFG_001-CFG_180"
# Keep the screening pass lightweight so K-value benchmarking can score a config quickly without
# spending the majority of the run on a combinatorial profile explosion.
# Larger slot families are assigned a stricter family-dominant stream and a much lower shortlist cap
# so Stage 6 remains practical without throwing away the legal exact-fill semantics.
PROFILE_CANDIDATE_LIMIT = 20
PROFILE_GENERATION_TIMEOUT_SECONDS = 600.0
RACK_SEARCH_TIMEOUT_SECONDS = 600.0
FAST_FAIL_PROFILE_PROBE_SECONDS = 30.0
FAST_FAIL_PROFILE_PROBE_FAMILY_SIZE = 7
# Latest timeout flags and generated profiles for the active config.
_LAST_STAGE6_TIMEOUTS: dict[str, bool] = {
    "profile_generation": False,
    "rack_search": False,
}
_LAST_STAGE6_PROFILE_POOL: list[list[float]] = []


class Stage6ProfileGenerationTimeout(RuntimeError):
    """Raised when Stage 6 profile generation exceeds the configured per-config limit."""


class Stage6RackSearchTimeout(RuntimeError):
    """Raised when Stage 6 rack assignment exceeds the configured per-config limit."""

# Row, candidate, and profile shapes shared across layout-generation helpers.
SummaryRow: TypeAlias = dict[str, str]
DetailRows: TypeAlias = list[dict[str, str]]
LayoutSignature: TypeAlias = tuple[str, ...]
StyleCandidate: TypeAlias = tuple[SummaryRow, DetailRows, DetailRows, LayoutSignature]
ConfigStyleBundle: TypeAlias = tuple[str, list[StyleCandidate]]
SlotSizeSequence: TypeAlias = Sequence[float | int] | None


class _Stage6Context:
    """Expose the active Stage 6 namespace to method-specific assignment routines."""

    def __init__(self, namespace: dict[str, Any]) -> None:
        """Bind the proxy to the live module namespace used by Stage 6."""
        object.__setattr__(self, "_namespace", namespace)

    def __getattr__(self, name: str) -> Any:
        """Read an attribute from the wrapped Stage 6 namespace."""
        return self._namespace[name]

    def __setattr__(self, name: str, value: Any) -> None:
        """Write an attribute through to the wrapped Stage 6 namespace."""
        self._namespace[name] = value


# Per-config timing diagnostics and the ranking tuple used to compare profile requirements.
_LAST_STAGE6_STEP_TIMINGS: dict[str, float] = {
    "profile_generation": 0.0,
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
    int,
    tuple[int, ...],
    tuple[int, ...],
    int,
    int,
]


_read_csv = common._read_csv


_write_csv_preserve = common._write_csv_preserve


def _fallback_path(path: Path, suffix: str = "_Backup") -> Path:
    """Build a sibling filename by inserting a fallback suffix before the extension."""
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def _write_csv_preserve_with_fallback(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    """Write ordered CSV columns, retrying at a backup path if the target is locked."""
    try:
        _write_csv_preserve(path, fieldnames, rows)
        return path
    except PermissionError:
        fallback = _fallback_path(path)
        _write_csv_preserve(fallback, fieldnames, rows)
        return fallback


def _write_csv_clean_with_fallback(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    """Write cleaned CSV rows, using a backup path when the target is locked."""
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


def _target_config_ids_from_script_args() -> list[str]:
    """Allow a quick one-off override like: python 06_layout_generation.py CFG_001 or CFG_001,CFG_003."""
    if len(sys.argv) <= 1:
        return []
    joined_args = " ".join(sys.argv[1:]).strip()
    if not joined_args or joined_args.startswith("-"):
        return []
    return _parse_target_config_ids(joined_args)


def _target_config_ids_from_environment() -> list[str]:
    """Return config IDs selected via CLI args, then environment override, then the script default.

    Examples: python 06_layout_generation.py CFG_001, CFG_006, 001-007
    """
    script_args = _target_config_ids_from_script_args()
    if script_args:
        return script_args

    raw = os.environ.get("PIPELINE_TARGET_CONFIGS", "").strip()
    if raw:
        return _parse_target_config_ids(raw)

    if DEFAULT_TARGET_CONFIGS:
        return _parse_target_config_ids(str(DEFAULT_TARGET_CONFIGS))
    return []


def _candidate_configs_for_exhaustive_search(configs: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    """Sample the valid candidate configs for the Stage 6 exhaustive exact-fill search.

    The default path should retain all valid warehouse configurations with at least two slot sizes so
    Stage 6 does not artificially drop the smaller but still legitimate exact-fill families.
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
        """Count configured slot sizes in a candidate row."""
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
    """Expand the compact Stage 5 exact-count signature for layout assignment."""
    rows = _read_csv(INPUT_CAPACITY_SUMMARY_FILE)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for summary in rows:
        config_id = str(summary.get("Config_ID", "")).strip()
        exact_counts = _parse_count_signature(str(summary.get("Exact_Count_Distribution", "")))
        sizes = sorted(exact_counts)
        for slot_size in sizes:
            cumulative = sum(count for size, count in exact_counts.items() if size >= slot_size)
            grouped[config_id].append(
                {
                    "Config_ID": config_id,
                    "SKU_Scenario": str(summary.get("SKU_Scenario", "")),
                    "Representative_Slot_Size": str(slot_size),
                    "Exact_Count": str(exact_counts[slot_size]),
                    "Min_Required_Locations_At_Or_Above_Size": str(cumulative),
                    "Cumulative_Assigned_SKUs_At_Or_Above_Size": str(cumulative),
                    "Required_Locations_Total": str(summary.get("Required_Locations_Total", "")),
                }
            )
    return grouped


def _slot_sizes_from_capacity(rows: list[dict[str, str]]) -> list[float]:
    """Return the sorted unique representative slot sizes in capacity rows."""
    # Extract unique representative slot sizes for reporting fields.
    sizes = {
        common._to_float(row.get("Representative_Slot_Size"))
        for row in rows
        if common._to_float(row.get("Representative_Slot_Size")) is not None
    }
    return sorted(float(value) for value in sizes if value is not None)


def _slot_sizes_for_config_id(
    capacity_rows_by_config: dict[str, list[dict[str, str]]],
    config_id: str,
) -> list[float]:
    """Return the configured representative slot sizes for the exact config being exported."""
    rows = capacity_rows_by_config.get(str(config_id).strip(), [])
    return _slot_sizes_from_capacity(rows)


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
    """Build base-demand exact counts, preferring explicit counts over cumulative rows."""
    # Layout generation should be independent of SKU-scenario stress tests.
    # Use base demand rows when available; robustness checks handle low/high demand.
    exact_rows = [row for row in rows if row.get("Exact_Count") not in (None, "")]
    if exact_rows:
        exact_counts = {
            common._to_float(row.get("Representative_Slot_Size")): common._to_int_default(row.get("Exact_Count"), 0)
            for row in exact_rows
        }
        return common._enforce_occupied_location_target(
            {float(size): count for size, count in exact_counts.items() if size is not None}
        )

    base_rows = [
        row
        for row in rows
        if str(row.get("SKU_Scenario", "")).strip() == "Base_Count"
    ]
    exact_counts = _worst_case_exact_counts(base_rows) if base_rows else _worst_case_exact_counts(rows)
    return common._enforce_occupied_location_target(exact_counts)


def _layout_signature(generated_location_rows: list[dict[str, str]]) -> tuple[str, ...]:
    """Create a stable, ordered signature for comparing generated layouts."""
    # Signature used to detect whether styles produce materially different layouts.
    ordered = sorted(
        generated_location_rows,
        key=lambda row: (
            str(row.get("Rack", "")),
            str(row.get("Column", "")),
            str(row.get("Row", "")),
            str(row.get("Assigned_Slot_Size_cm", "")),
        ),
    )
    return tuple(
        f"{row.get('Rack','')}{row.get('Column','')}:{row.get('Row','')}:{row.get('Assigned_Slot_Size_cm','')}"
        for row in ordered
    )


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

    physical_total = sum(slots) + (len(slots) - 1) * common.BEAM_HEIGHT
    if abs(physical_total - common.MAX_USED_HEIGHT_BASE) > 1e-9:
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

    if not lower_slots:
        return False

    required_completion = (
        common.MAX_USED_HEIGHT_BASE
        - sum(lower_slots)
        - common.BEAM_HEIGHT * len(lower_slots)
    )
    if required_completion <= 0.0:
        return False

    if abs(float(final_slot) - float(required_completion)) > 1e-9:
        return False

    effective_final_slot = _effective_requirement_slot_size(final_slot, slots, available_slot_sizes)
    if effective_final_slot not in config_values:
        return False

    effective_profile = []
    for index, raw_value in enumerate(slots):
        rounded = int(round(float(raw_value)))
        if index == len(slots) - 1 and rounded not in config_values:
            effective_profile.append(float(effective_final_slot))
        else:
            effective_profile.append(float(rounded))

    if any(float(effective_profile[index]) < float(effective_profile[index + 1]) for index in range(len(effective_profile) - 1)):
        return False

    return True


def _normalize_slot_family(candidate_slot_sizes: SlotSizeSequence) -> tuple[float, ...]:
    """Normalize slot sizes into a hashable tuple so repeated profile generation can be cached."""
    return tuple(sorted({float(size) for size in (candidate_slot_sizes or []) if size is not None}))


def _profile_generation_policy(candidate_slot_sizes: SlotSizeSequence) -> tuple[int, int, int, int]:
    """Return the runtime-safe generation window for the active slot family.

    Larger families need a wider legal profile pool and a wider shortlist so the rack search does not
    starve itself on a tiny subset of exact-fill candidates. This keeps the pool family-aware while
    still preventing the broad combinatorial explosion that would otherwise happen for 8+ slot values.
    """
    configured_sizes = sorted(set(_config_size_values(candidate_slot_sizes or [])), reverse=True)
    family_size = len(configured_sizes)
    # Constrained exact families need a wider legal profile pool than the historical defaults.
    # Several configs still fail not because the final family is impossible, but because the generator
    # was artificially starving the rack search on a tiny legal profile set. A slightly wider pool
    # keeps the search bounded while retaining the exact-fill semantics the user requires.
    if family_size <= 4:
        return 18, 2, 36, 18
    if family_size <= 6:
        return 16, 2, 32, 20
    if family_size <= 8:
        return 12, 2, 24, 22
    return 10, 2, 20, 26


def _normalize_required_counts(required_counts: dict[float, int] | Sequence[tuple[float, int]] | None) -> tuple[tuple[float, int], ...] | None:
    """Convert requirement quotas into a hashable key for cache-safe profile generation."""
    if required_counts is None:
        return None
    if isinstance(required_counts, dict):
        items = required_counts.items()
    else:
        items = required_counts
    return tuple(sorted((float(size), int(count)) for size, count in items if int(count) > 0))


def _quick_profile_feasibility_probe(
    candidate_slot_sizes: SlotSizeSequence,
    required_counts: dict[float, int] | None = None,
    timeout_seconds: float = FAST_FAIL_PROFILE_PROBE_SECONDS,
    config_deadline: float | None = None,
) -> bool:
    """Return False immediately for large dead families that cannot support a legal profile.

    The full Stage 6 search is expensive, and many larger exact families are structurally dead before
    the recursive rack search is meaningful. A short probe is enough to reject those families early
    without spending the full 120-second per-config budget on impossible patterns.
    """
    normalized_sizes = sorted(set(_config_size_values(candidate_slot_sizes or [])))
    if not normalized_sizes:
        return False
    if len(normalized_sizes) < FAST_FAIL_PROFILE_PROBE_FAMILY_SIZE:
        return True
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
            required_counts=normalized_required,
        )
    except Stage6ProfileGenerationTimeout:
        return False
    return bool(profiles)


def _profile_quota_signature(
    profile: Sequence[float],
    available_slot_sizes: SlotSizeSequence = None,
) -> tuple[tuple[int, int], ...]:
    """Canonicalize a profile by the effective quota counts it contributes.

    The Stage 6 generator should keep one representative per quota-signature so repeated exact-fill
    variants do not crowd the legal pool. This is a canonicalization step, not a second generation
    policy. The profile-space remains broad enough for feasible exact-fill layouts, while the search
    stays focused on the remaining quota deficit.
    """
    normalized_sizes = tuple(
        sorted({int(round(float(size))) for size in (available_slot_sizes or []) if float(size) > 0.0})
    )
    counts = _effective_requirement_counts(profile, normalized_sizes)
    return tuple(sorted((int(size), int(count)) for size, count in counts.items()))


def _generate_feasible_rack_profiles_cached(
    candidate_slot_sizes: tuple[float, ...],
    timeout_seconds: float | None = None,
    config_deadline: float | None = None,
    required_counts: tuple[tuple[float, int], ...] | None = None,
) -> tuple[tuple[float, ...], ...]:
    """Generate the accepted exact-fill profile stream in descending legal order.

    The search uses a canonical descending tree: keep the largest feasible next slot first and only
    split the earliest legal slot that can still support a valid exact-fill residual. This avoids the
    full product-space explosion that makes larger 4-slot and 5-slot configs blow up in runtime even
    when the final accepted profile pool stays small.
    """
    configured_sizes = sorted(set(_config_size_values(candidate_slot_sizes or [])), reverse=True)
    if not configured_sizes:
        return ()

    family_set = set(configured_sizes)
    legal_values = set(_legal_topfill_values(candidate_slot_sizes or []))
    required_map = {float(size): int(count) for size, count in (required_counts or ())}
    deadline = None if timeout_seconds is None else time.perf_counter() + float(timeout_seconds)
    seen: set[tuple[float, ...]] = set()
    seen_quota_signatures: set[tuple[tuple[int, int], ...]] = set()
    generated_order: list[tuple[float, ...]] = []
    # Generate the legal exact-fill space by enumerating a bounded lower-stack search over the current
    # configured family. This is the core fix for persistent hard configs: valid profiles such as
    # (239, 239, 114, 114) and (174, 124, 124, 84, 84, 84) arise from repeated dominant values, not only
    # a single pass through lower-value alternatives. We keep the search bounded, but we do not reject
    # profiles merely because they are longer than the raw family count.
    max_lower_rows = max(2, min(18, len(configured_sizes) * 4 + 2))

    def _add_candidate(lower_tuple: tuple[float, ...], residual_value: float) -> None:
        """Keep a feasible unseen profile with a distinct requirement effect."""
        nonlocal generated_order
        candidate = tuple(sorted([*lower_tuple, float(residual_value)], reverse=True))
        if not _profile_is_feasible_exact_fill(list(candidate), candidate_slot_sizes):
            return
        if candidate in seen:
            return
        seen.add(candidate)
        quota_signature = _profile_quota_signature(candidate, configured_sizes)
        if quota_signature in seen_quota_signatures:
            return
        seen_quota_signatures.add(quota_signature)
        generated_order.append(candidate)

    for lower_count in range(1, max_lower_rows + 1):
        for lower_combo in itertools.product(configured_sizes, repeat=lower_count):
            lower_tuple = tuple(sorted(lower_combo, reverse=True))
            support_height = sum(lower_tuple) + (len(lower_tuple) - 1) * common.BEAM_HEIGHT
            if support_height < 504.0 - 1e-9:
                continue
            remaining_height = common.MAX_USED_HEIGHT_BASE - sum(lower_tuple) - common.BEAM_HEIGHT * len(lower_tuple)
            if remaining_height <= 0.0 or remaining_height > 214.0:
                continue
            rounded_residual = int(round(float(remaining_height)))
            if rounded_residual not in family_set and rounded_residual not in legal_values:
                continue
            _add_candidate(lower_tuple, float(rounded_residual))

    if len(generated_order) < 8:
        fallback_depth = max(18, max_lower_rows + 6)
        for lower_count in range(1, fallback_depth + 1):
            for lower_combo in itertools.product(configured_sizes, repeat=lower_count):
                lower_tuple = tuple(sorted(lower_combo, reverse=True))
                if len(lower_tuple) > 12:
                    continue
                support_height = sum(lower_tuple) + (len(lower_tuple) - 1) * common.BEAM_HEIGHT
                if support_height < 504.0 - 1e-9:
                    continue
                remaining_height = common.MAX_USED_HEIGHT_BASE - sum(lower_tuple) - common.BEAM_HEIGHT * len(lower_tuple)
                if remaining_height <= 0.0 or remaining_height > 214.0:
                    continue
                rounded_residual = int(round(float(remaining_height)))
                if rounded_residual not in family_set and rounded_residual not in legal_values:
                    continue
                _add_candidate(lower_tuple, float(rounded_residual))
                if len(generated_order) >= 64:
                    break
            if len(generated_order) >= 64:
                break

    ordered_profiles = sorted(
        generated_order,
        key=lambda profile: tuple(-float(value) for value in profile),
    )
    return tuple(ordered_profiles)


def _generate_all_canonical_exact_fill_profiles(
    candidate_slot_sizes: SlotSizeSequence,
    timeout_seconds: float | None = None,
    config_deadline: float | None = None,
) -> list[list[float]]:
    """Enumerate distinct legal exact-fill profiles with the topfill kept in its physical top row."""
    family = sorted(set(_config_size_values(candidate_slot_sizes or [])))
    if not family:
        return []

    deadlines = [deadline for deadline in (config_deadline,) if deadline is not None]
    if timeout_seconds is not None:
        deadlines.append(time.perf_counter() + float(timeout_seconds))
    deadline = min(deadlines) if deadlines else None
    legal_topfills = _legal_topfill_values(family)
    max_lower_rows = max(
        1,
        min(
            18,
            int(common.MAX_USED_HEIGHT_BASE // (min(family) + common.BEAM_HEIGHT)),
        ),
    )
    profiles: set[tuple[float, ...]] = set()
    candidate_checks = 0

    for lower_count in range(1, max_lower_rows + 1):
        for lower_combo in itertools.combinations_with_replacement(family, lower_count):
            candidate_checks += 1
            if candidate_checks % 128 == 0 and deadline is not None and time.perf_counter() >= deadline:
                raise Stage6ProfileGenerationTimeout("exact-fill profile enumeration timed out")

            lower_stack = tuple(sorted(lower_combo, reverse=True))
            support_height = sum(lower_stack) + (len(lower_stack) - 1) * common.BEAM_HEIGHT
            if support_height < 504.0 - 1e-9:
                continue

            topfill = common.MAX_USED_HEIGHT_BASE - sum(lower_stack) - common.BEAM_HEIGHT * len(lower_stack)
            if topfill <= 0.0 or topfill > 214.0:
                continue
            rounded_topfill = int(round(topfill))
            if rounded_topfill not in family and rounded_topfill not in legal_topfills:
                continue

            profile = (*lower_stack, float(rounded_topfill))
            if _profile_is_feasible_exact_fill(profile, family):
                profiles.add(profile)

    return [
        list(profile)
        for profile in sorted(profiles, key=lambda values: tuple(-float(value) for value in values))
    ]


def _generate_feasible_rack_profiles(
    candidate_slot_sizes: SlotSizeSequence,
    timeout_seconds: float | None = None,
    config_deadline: float | None = None,
    required_counts: dict[float, int] | None = None,
) -> list[list[float]]:
    """Generate the canonical legal exact-fill profile pool for one configuration.

    The quota deficit is not evaluated here. A single profile is never deemed sufficient for a full
    layout; the actual demand reduction only becomes meaningful once a profile is assigned to a rack
    and multiplied by the number of columns in that rack. Profile generation therefore stays limited to
    legal exact-fill candidates and canonical deduplication.
    """
    normalized = _normalize_slot_family(candidate_slot_sizes)
    normalized_required = _normalize_required_counts(required_counts)

    if 3 <= len(_config_size_values(normalized)) <= 10:
        return _generate_all_canonical_exact_fill_profiles(
            normalized,
            timeout_seconds=timeout_seconds,
            config_deadline=config_deadline,
        )

    profiles = [
        list(profile)
        for profile in _generate_feasible_rack_profiles_cached(
            normalized,
            timeout_seconds=timeout_seconds,
            config_deadline=config_deadline,
            required_counts=normalized_required,
        )
    ]
    if not profiles:
        return []

    profiles.sort(key=lambda profile: tuple(-float(value) for value in profile))

    unique_profiles: list[list[float]] = []
    seen_profile_keys: set[tuple[float, ...]] = set()
    for profile in profiles:
        profile_key = tuple(float(value) for value in profile)
        if profile_key in seen_profile_keys:
            continue
        seen_profile_keys.add(profile_key)
        unique_profiles.append(list(profile))
    return unique_profiles


def _search_all_profiles_by_rack(
    rack_columns: list[str],
    required_counts: dict[float, int],
    slot_sizes: SlotSizeSequence,
    profiles: list[list[float]],
    config_deadline: float | None = None,
) -> dict[str, list[float]]:
    """Search every distinct legal profile effect, applying it once per column in its rack."""
    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[_rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns)
    ordered_sizes = sorted((float(size) for size in required_counts), reverse=True)
    initial_remaining = tuple(sorted((float(size), int(count)) for size, count in required_counts.items()))

    profile_options: list[tuple[list[float], tuple[int, ...]]] = []
    seen_effects: set[tuple[int, ...]] = set()
    for profile in profiles:
        effective_counts = _effective_requirement_counts(profile, slot_sizes)
        effect = tuple(int(effective_counts.get(int(size), 0)) for size in ordered_sizes)
        if effect in seen_effects:
            continue
        seen_effects.add(effect)
        profile_options.append((list(profile), effect))
    profile_options.sort(key=lambda option: tuple(-count for count in option[1]))
    if not profile_options:
        return {}

    def _objective(remaining: dict[float, int]) -> tuple[int, int, int]:
        """Rank remaining-demand states by coverage, total shortage, and balance."""
        satisfied = sum(1 for size in ordered_sizes if remaining.get(size, 0) <= 0)
        shortage = sum(max(int(remaining.get(size, 0)), 0) for size in ordered_sizes)
        total_shortage = sum(max(int(value), 0) for value in remaining.values())
        distribution_gap = sum(
            abs(float(max(int(remaining.get(size, 0)), 0)) / float(total_shortage))
            for size in ordered_sizes
            if remaining.get(size, 0) > 0 and total_shortage > 0
        )
        return satisfied, -shortage, int(-distribution_gap * 1000.0)

    @lru_cache(maxsize=100_000)
    def _search(
        rack_index: int,
        remaining_key: tuple[tuple[float, int], ...],
    ) -> tuple[tuple[int, int, int], tuple[int, ...]]:
        """Return the best remaining-demand score and profile choice from this rack onward."""
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            raise Stage6RackSearchTimeout("rack search exceeded the combined config timeout")
        remaining = {float(size): int(count) for size, count in remaining_key}
        if rack_index >= len(rack_order):
            return _objective(remaining), ()
        if all(value <= 0 for value in remaining.values()):
            return _objective(remaining), tuple(0 for _ in rack_order[rack_index:])

        rack_width = len(rack_to_columns[rack_order[rack_index]])
        best_score: tuple[int, int, int] | None = None
        best_choices: tuple[int, ...] | None = None
        for profile_index, (_, effect) in enumerate(profile_options):
            next_remaining = dict(remaining)
            for size_index, size in enumerate(ordered_sizes):
                next_remaining[size] = max(
                    int(next_remaining.get(size, 0)) - int(effect[size_index]) * rack_width,
                    0,
                )
            if next_remaining == remaining:
                continue

            child_score, child_choices = _search(
                rack_index + 1,
                tuple(sorted((float(size), int(count)) for size, count in next_remaining.items())),
            )
            if best_score is None or child_score > best_score:
                best_score = child_score
                best_choices = (profile_index, *child_choices)

        if best_score is None or best_choices is None:
            return _objective(remaining), tuple(0 for _ in rack_order[rack_index:])
        return best_score, best_choices

    _, selected_profiles = _search(0, initial_remaining)
    assignments: dict[str, list[float]] = {}
    for rack_index, profile_index in enumerate(selected_profiles):
        rack = rack_order[rack_index]
        profile = profile_options[profile_index][0]
        for column_key in sorted(rack_to_columns[rack]):
            assignments[column_key] = list(profile)
    return assignments


def _search_profiles_by_rack_beam(
    rack_columns: list[str],
    required_counts: dict[float, int],
    slot_sizes: SlotSizeSequence,
    profiles: list[list[float]],
    config_deadline: float | None = None,
    beam_width: int = 128,
) -> dict[str, list[float]]:
    """Search a bounded set of rack-level quota states for larger profile families."""
    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[_rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns)
    ordered_sizes = sorted((float(size) for size in required_counts), reverse=True)

    profile_options: list[tuple[list[float], tuple[int, ...]]] = []
    seen_effects: set[tuple[int, ...]] = set()
    for profile in profiles:
        effective_counts = _effective_requirement_counts(profile, slot_sizes)
        effect = tuple(int(effective_counts.get(int(size), 0)) for size in ordered_sizes)
        if effect in seen_effects:
            continue
        seen_effects.add(effect)
        profile_options.append((list(profile), effect))
    if not profile_options:
        return {}

    smallest_profile_index = min(
        range(len(profile_options)),
        key=lambda index: (len(profile_options[index][0]), profile_options[index][0]),
    )
    required_vector = tuple(int(required_counts[size]) for size in ordered_sizes)
    initial_state = (required_vector, ())
    beam = [initial_state]

    def _state_score(remaining: tuple[int, ...]) -> tuple[int, int, tuple[int, ...]]:
        """Rank beam states by satisfied requirements and the remaining shortage vector."""
        satisfied = sum(value <= 0 for value in remaining)
        shortage = sum(max(value, 0) for value in remaining)
        return satisfied, -shortage, tuple(-max(value, 0) for value in remaining)

    def _build_assignments(profile_choices: tuple[int, ...]) -> dict[str, list[float]]:
        """Expand rack-level profile choices into per-column assignments."""
        assignments: dict[str, list[float]] = {}
        for rack_index, rack in enumerate(rack_order):
            if rack_index < len(profile_choices):
                profile_index = profile_choices[rack_index]
            else:
                profile_index = smallest_profile_index
            profile = profile_options[profile_index][0]
            for column_key in sorted(rack_to_columns[rack]):
                assignments[column_key] = list(profile)
        return assignments

    transition_count = 0
    for rack_index, rack in enumerate(rack_order):
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            raise Stage6RackSearchTimeout("rack search exceeded the combined config timeout")
        rack_width = len(rack_to_columns[rack])
        next_states: dict[tuple[int, ...], tuple[int, ...]] = {}

        for remaining, choices in beam:
            for profile_index, (_, effect) in enumerate(profile_options):
                transition_count += 1
                if transition_count % 2048 == 0 and config_deadline is not None and time.perf_counter() >= config_deadline:
                    raise Stage6RackSearchTimeout("rack search exceeded the combined config timeout")

                next_remaining = tuple(
                    max(remaining[size_index] - effect[size_index] * rack_width, 0)
                    for size_index in range(len(ordered_sizes))
                )
                if next_remaining == remaining:
                    continue

                next_choices = (*choices, profile_index)
                previous_choices = next_states.get(next_remaining)
                if previous_choices is None or len(next_choices) < len(previous_choices):
                    next_states[next_remaining] = next_choices

        if not next_states:
            return _build_assignments(beam[0][1])

        complete_states = [
            (remaining, choices)
            for remaining, choices in next_states.items()
            if all(value <= 0 for value in remaining)
        ]
        if complete_states:
            _, choices = max(
                complete_states,
                key=lambda state: (
                    -sum(
                        len(profile_options[profile_index][0])
                        * len(rack_to_columns[rack_order[index]])
                        for index, profile_index in enumerate(state[1])
                    ),
                    tuple(-value for value in state[1]),
                ),
            )
            return _build_assignments(choices)

        best_states = sorted(
            next_states.items(),
            key=lambda state: _state_score(state[0]),
            reverse=True,
        )[: max(int(beam_width), 1)]
        beam = [(remaining, choices) for remaining, choices in best_states]

    best_remaining, best_choices = max(beam, key=lambda state: _state_score(state[0]))
    _ = best_remaining
    return _build_assignments(best_choices)


def _choose_profile_shortlist(
    profiles: list[list[float]],
    required_counts: dict[float, int],
    limit: int | None = None,
) -> list[list[float]]:
    """Keep the legal profile search focused without losing required slot-size coverage.

    The shortlist is ranked by remaining-deficit usefulness, but the selection is also family-aware:
    at least one profile covering a required family size is kept when possible so the rack search does
    not starve on a single dominant profile pattern. This preserves diversity without inflating the
    shortlist to the full raw exact-fill pool.
    """
    if not profiles:
        return []

    configured_sizes = sorted(set(_config_size_values(list(required_counts.keys()))), reverse=True)
    _, _, _, shortlist_cap = _profile_generation_policy(configured_sizes)
    explicit_limit = int(limit if limit is not None else PROFILE_CANDIDATE_LIMIT)
    search_limit = max(1, min(explicit_limit, shortlist_cap, len(profiles)))
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

    # Deduplicate by the effective quota signature so repeated profile shapes do not crowd out the
    # genuinely different exact-fill solutions that resolve the remaining shortage vector. This keeps
    # the legal pool canonical even after quota-driven expansion has been applied.
    deduped_ranked: list[list[float]] = []
    seen_quota_signatures: set[tuple[tuple[int, int], ...]] = set()
    for profile in ranked:
        quota_signature = _profile_quota_signature(profile, list(required_counts.keys()))
        if quota_signature in seen_quota_signatures:
            continue
        seen_quota_signatures.add(quota_signature)
        deduped_ranked.append(list(profile))

    ranked = deduped_ranked

    kept: list[list[float]] = []
    covered_sizes: set[int] = set()
    seen_profiles: set[tuple[float, ...]] = set()
    for profile in ranked:
        profile_key = tuple(float(value) for value in profile)
        if profile_key in seen_profiles:
            continue
        seen_profiles.add(profile_key)
        counts = _effective_requirement_counts(profile, list(required_counts.keys()))
        uncovered = [
            size for size in required_sizes
            if size not in covered_sizes and counts.get(size, 0) > 0
        ]
        if uncovered or len(kept) < search_limit:
            kept.append(list(profile))
            covered_sizes.update(uncovered)
        if len(kept) >= search_limit and all(size in covered_sizes for size in required_sizes):
            break

    if len(kept) < search_limit:
        for profile in ranked:
            candidate = list(profile)
            candidate_key = tuple(float(value) for value in candidate)
            if candidate_key in {tuple(float(value) for value in item) for item in kept}:
                continue
            kept.append(candidate)
            if len(kept) >= search_limit:
                break

    if not kept:
        return [list(profile) for profile in ranked[:search_limit]]

    # Preserve at least one profile covering each required family size when possible, before we allow
    # the shortlist to fall back to generic top-ranked profiles. This keeps the rack search from
    # starving on a single dominant family even when that family is not the one currently in deficit.
    family_coverage: set[int] = set()
    for profile in kept:
        counts = _effective_requirement_counts(profile, list(required_counts.keys()))
        family_coverage.update(size for size in required_sizes if counts.get(size, 0) > 0)
    for size in required_sizes:
        if size in family_coverage:
            continue
        for profile in ranked:
            counts = _effective_requirement_counts(profile, list(required_counts.keys()))
            if counts.get(size, 0) > 0 and list(profile) not in kept:
                kept.append(list(profile))
                if len(kept) >= search_limit:
                    break
        if len(kept) >= search_limit:
            break

    return kept[:search_limit]


def _nearest_configured_family_slot(
    slot_value: float | int,
    available_slot_sizes: SlotSizeSequence = None,
    profile: Sequence[float | int] | None = None,
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
    slot_value: float | int,
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

    # Legal topfilled values are recorded under their underlying configured family, not as synthetic
    # buckets such as 64, 79, 114, 184, etc. A final topfill is always charged to the largest config
    # value below it, which preserves the dominant-order semantics of the exact-fill stack.
    mapped = _nearest_configured_family_slot(rounded, config_values, profile_values)
    if mapped is not None:
        return mapped
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


def _profile_requirement_priority(
    profile: Sequence[float],
    remaining: dict[float, int],
) -> ProfileRequirementPriorityKey:
    """Rank feasible profiles by exact remaining-deficit coverage.

    The minimum required counts per slot size are a hard gate: profiles that still leave a required
    slot family short must rank below any profile that satisfies the outstanding minimums, even if
    they appear to cover more total locations in aggregate.

    This function intentionally maintains a single fixed tuple shape so every comparison path in
    Stage 6 uses the same ranking contract. If the tuple shape changes later, the assertion below
    fails immediately instead of causing a silent tuple comparison bug elsewhere in the pipeline.
    """
    counts = _effective_requirement_counts(profile, list(remaining.keys()))
    # Resolve the shortage vector in descending slot-size order, not by the smallest remaining count.
    # For the legal profile family, the critical ordering is 234 -> 124 -> 69 because a profile that
    # still leaves the 234 family short should rank below one that closes the 234 gap even if the
    # 69-family shortage looks numerically larger on a raw count basis.
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
    largest_unmet_shortage = max(shortage_vector) if shortage_vector else 0
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

    # The shortage-closing objective must prefer the profile that reduces the biggest unmet exact-fill
    # family first. Using a negative "largest shortage" value makes the ranking prefer generic filler
    # instead of a profile that genuinely resolves the dominant quota gap.
    base_coverage = sum(coverage_vector)
    weighted_coverage = sum(
        int(round(float(size))) * min(counts.get(int(round(float(size))), 0), int(remaining[size]))
        for size in ordered_sizes
        if int(remaining[size]) > 0
    )

    scarce_coverage = sum(
        1
        for size in ordered_sizes
        if int(remaining[size]) > 0 and counts.get(int(round(float(size))), 0) > 0 and int(remaining[size]) == min(int(value) for value in remaining.values() if value > 0)
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
    weighted_distribution_gap = sum(
        float(int(round(float(size)))) * gap
        for size, gap in zip(ordered_sizes, distribution_gap_vector)
        if int(remaining[size]) > 0
    )
    distribution_fit_score = int(round(-weighted_distribution_gap * 1000.0))

    # The dominant ordering should favor the profile that closes the largest unmet family shortage,
    # before generic coverage or filler-heavy totals. This keeps 124/239 families from being starved by
    # a profile that only packs more 69 slots.
    key: ProfileRequirementPriorityKey = (
        minimums_satisfied,
        -unmet_requirements,
        largest_unmet_shortage,
        distribution_fit_score,
        weighted_coverage,
        base_coverage,
        distinct_coverage + scarce_coverage,
        -total_shortage,
        tuple(-value for value in shortage_vector),
        coverage_vector,
        -synthetic_topfill_penalty,
        exact_completion_bonus,
    )
    assert len(key) == 12, f"Profile requirement priority tuple shape changed unexpectedly: {len(key)} items"
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
        "Rack_Search_Seconds": "0.000",
        "Profile_Generation_Timeout": "YES",
        "Rack_Search_Timeout": "YES",
        "Layout_Feasible": "NO",
        "Allocation_Feasible_Initial": "NO",
        "Total_Locations": "0",
        "Capacity_Margin": "0",
        "Beam_Relocations_Total": "0",
        "Required_Beams_Total": "0",
        "Additional_Beams_Required": "0",
        "Required_Grids_Total": "0",
        "Additional_Grids_Required": "0",
        "Minimum_Required_Counts": minimum_counts,
        "Slot_Composition_Signature": minimum_counts,
        "Layout_Slot_Size_Distribution": "",
        "Layout_Slot_Size_Cumulative_Coverage": "",
        "Source_Slot_Sizes": "",
        "Feasible_Profiles_Considered_Total": "0",
        "Feasible_Profiles_Used_Total": "0",
    }


def _build_deficit_coverage_layout(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: SlotSizeSequence,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]]:
    """Choose rack profiles with a quota-aware global search.

    The legal profile pool is generated once and then the assignment is optimized across the full set of
    racks so that the chosen profile mix still matches the minimal slot-size quotas. This prevents the
    per-rack selection loop from repeatedly favoring the same profile family and under-serving the rare
    124/234 requirements.
    """
    global _LAST_STAGE6_PROFILE_POOL

    if not rack_columns:
        return {}

    required = {float(size): int(count) for size, count in required_counts.items()}
    profile_generation_start = time.perf_counter()
    profiles = _generate_feasible_rack_profiles(
        config_slot_sizes or list(required_counts.keys()),
        timeout_seconds=PROFILE_GENERATION_TIMEOUT_SECONDS,
        config_deadline=config_deadline,
        required_counts=None,
    )
    _LAST_STAGE6_PROFILE_POOL = [list(profile) for profile in profiles]
    _LAST_STAGE6_STEP_TIMINGS["profile_generation"] = time.perf_counter() - profile_generation_start
    if not profiles:
        if _LAST_STAGE6_TIMEOUTS.get("profile_generation", False):
            _LAST_STAGE6_PROFILE_POOL = []
            return {column_key: [] for column_key in rack_columns}
        return {column_key: [] for column_key in rack_columns}
    _LAST_STAGE6_TIMEOUTS["profile_generation"] = False

    profile_family_size = len(_config_size_values(config_slot_sizes or list(required)))
    if 3 <= profile_family_size <= 10:
        all_profile_search_start = time.perf_counter()
        try:
            if profile_family_size <= 4:
                all_profile_assignments = _search_all_profiles_by_rack(
                    rack_columns,
                    required,
                    config_slot_sizes,
                    profiles,
                    config_deadline=config_deadline,
                )
            else:
                all_profile_assignments = {}
                for beam_width in (128, 512, 1024):
                    candidate_assignments = _search_profiles_by_rack_beam(
                        rack_columns,
                        required,
                        config_slot_sizes,
                        profiles,
                        config_deadline=config_deadline,
                        beam_width=beam_width,
                    )
                    candidate_assigned_total = max(
                        sum(len(slots) for slots in candidate_assignments.values())
                        - common._fixed_layout_location_total(),
                        0,
                    )
                    candidate_is_feasible = (
                        candidate_assigned_total >= common._explicit_occupied_target_total()
                        and candidate_assigned_total >= sum(required.values())
                        and _layout_assignments_are_feasible(
                            candidate_assignments,
                            list(candidate_assignments),
                            float(min(required, default=0.0)),
                            config_slot_sizes,
                            minimum_required_counts=required,
                            enforce_minimum_total_locations=True,
                        )
                    )
                    all_profile_assignments = candidate_assignments
                    if candidate_is_feasible:
                        break
        except Stage6RackSearchTimeout:
            _LAST_STAGE6_TIMEOUTS["rack_search"] = True
        else:
            assigned_total = max(
                sum(len(slots) for slots in all_profile_assignments.values())
                - common._fixed_layout_location_total(),
                0,
            )
            all_profile_is_feasible = (
                assigned_total >= common._explicit_occupied_target_total()
                and assigned_total >= sum(required.values())
                and _layout_assignments_are_feasible(
                    all_profile_assignments,
                    list(all_profile_assignments),
                    float(min(required, default=0.0)),
                    config_slot_sizes,
                    minimum_required_counts=required,
                    enforce_minimum_total_locations=True,
                )
            )
            if all_profile_is_feasible:
                _LAST_STAGE6_TIMEOUTS["rack_search"] = False
                _LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - all_profile_search_start
                return all_profile_assignments

    # Keep the rack search on a family-aware shortlist. Larger slot families need a wider candidate
    # pool to avoid starving the recursion on a single dominant profile pattern while still keeping the
    # search compact enough that the runtime stays controlled.
    family_policy_cap = _profile_generation_policy(list(required_counts.keys()))[3]
    shortlist_limit = max(4, min(len(profiles), family_policy_cap)) if profiles else 0
    profile_pool = _choose_profile_shortlist(profiles, required, limit=shortlist_limit)
    if not profile_pool:
        return {column_key: [] for column_key in rack_columns}

    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[_rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns)

    sizes = sorted(required, key=lambda size: int(round(float(size))))

    last_assignments: dict[str, list[float]] | None = None
    last_timeout = False

    def _remaining_objective(remaining: dict[float, int]) -> tuple[int, int, int]:
            """Rank a partial assignment by fulfilled families and remaining shortage."""
            satisfied = sum(1 for size in sizes if int(remaining.get(size, 0)) <= 0)
            total_shortage = sum(max(0, int(remaining.get(size, 0))) for size in sizes)
            distribution_gap = 0.0
            total_need = sum(int(value) for value in remaining.values())
            if total_need > 0:
                for size in sizes:
                    if int(remaining.get(size, 0)) <= 0:
                        continue
                    distribution_gap += abs(float(int(remaining.get(size, 0))) / float(total_need))
            return (satisfied, -total_shortage, int(-distribution_gap * 1000.0))

    def _candidate_profiles_for_remaining(remaining: dict[float, int]) -> list[list[float]]:
        """Use one remaining-deficit ordering for all rack-search decisions.

        There is no separate quota-generation pass here: the legal pool is already canonical, and the
        rack search simply selects the profiles that most strongly reduce the current remaining demand.
        """
        ordered = sorted(
            profile_pool,
            key=lambda profile: _profile_requirement_priority(list(profile), remaining),
            reverse=True,
        )
        return [list(profile) for profile in ordered[: min(max(4, min(8, len(profile_pool))), len(profile_pool))]]

    rack_search_start = time.perf_counter()
    rack_search_deadline = rack_search_start + RACK_SEARCH_TIMEOUT_SECONDS

    @lru_cache(maxsize=20000)
    def _search(index: int, remaining_key: tuple[tuple[float, int], ...]) -> tuple[tuple[int, int, int], dict[str, list[float]]]:
        """Recursively choose a profile for each rack and return the best assignment."""
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            raise Stage6RackSearchTimeout("rack search exceeded the combined config timeout")
        if time.perf_counter() >= rack_search_deadline:
            raise Stage6RackSearchTimeout("rack search exceeded the per-config timeout")
        remaining = {float(size): int(count) for size, count in remaining_key}
        if index >= len(rack_order):
            return _remaining_objective(remaining), {}

        rack = rack_order[index]
        columns = sorted(rack_to_columns[rack])
        best_score: tuple[int, int, int] | None = None
        best_assignments: dict[str, list[float]] | None = None

        rack_columns_count = len(columns)
        for profile in _candidate_profiles_for_remaining(remaining):
            if config_deadline is not None and time.perf_counter() >= config_deadline:
                raise Stage6RackSearchTimeout("rack search exceeded the combined config timeout")
            if time.perf_counter() >= rack_search_deadline:
                raise Stage6RackSearchTimeout("rack search exceeded the per-config timeout")
            next_remaining = {float(size): int(count) for size, count in remaining.items()}
            effective_counts = _effective_requirement_counts(profile, list(required.keys()))
            for size_int, count in effective_counts.items():
                size_key = next((key for key in next_remaining if int(round(float(key))) == size_int), None)
                if size_key is None:
                    continue
                next_remaining[size_key] = max(next_remaining[size_key] - count * rack_columns_count, 0)

            try:
                child_score, child_assignments = _search(index + 1, tuple(sorted((float(size), int(value)) for size, value in next_remaining.items())))
            except Stage6RackSearchTimeout:
                raise
            candidate_score = child_score
            if best_score is None or candidate_score > best_score:
                best_score = candidate_score
                best_assignments = {column_key: list(profile) for column_key in columns}
                best_assignments.update(child_assignments)

        if best_score is None or best_assignments is None:
            fallback_profile = list(profile_pool[0]) if profile_pool else [float(sizes[0])]
            return _remaining_objective(remaining), {column_key: list(fallback_profile) for column_key in columns}

        return best_score, best_assignments

    try:
        _, assignments = _search(0, tuple(sorted((float(size), int(count)) for size, count in required.items())))
    except Stage6RackSearchTimeout:
        last_timeout = True
        _LAST_STAGE6_TIMEOUTS["rack_search"] = True
        return {column_key: [] for column_key in rack_columns}

    _LAST_STAGE6_TIMEOUTS["rack_search"] = False
    _LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - rack_search_start
    for column_key in rack_columns:
        assignments.setdefault(column_key, [])
    last_assignments = assignments
    if assignments and any(columns for columns in assignments.values()):
        return assignments

    if last_timeout:
        return {column_key: [] for column_key in rack_columns}
    if last_assignments is not None:
        for column_key in rack_columns:
            last_assignments.setdefault(column_key, [])
        return last_assignments
    return {column_key: [] for column_key in rack_columns}


def _parse_count_signature(value: str) -> dict[int, int]:
    """Parse a pipe-delimited ``size:count`` signature and combine repeated sizes."""
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


def _occupied_allocation_by_exact_slot_size(
    minimum_counts: dict[int, int],
    total_counts: dict[int, int],
) -> dict[int, int]:
    """Allocate each demand bucket to the smallest remaining slot size that fits."""
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


def _effective_slot_distribution_counts(
    exact_counts: dict[int, int] | Counter[int],
    available_slot_sizes: SlotSizeSequence = None,
) -> dict[int, int]:
    """Map legal topfill values to the nearest lower configured family size for summary output."""
    if not exact_counts:
        return {}

    normalized = {int(size): int(count) for size, count in dict(exact_counts).items() if int(count) > 0}
    if not normalized:
        return {}

    normalized_keys = tuple(normalized.keys())
    config_values = tuple(sorted(_config_size_values(available_slot_sizes or normalized_keys)))
    if not config_values:
        config_values = tuple(sorted(normalized_keys))

    mapped: Counter[int] = Counter()
    raw_keys = tuple(sorted(normalized.keys()))
    for size, count in normalized.items():
        mapped_size = _effective_requirement_slot_size(size, raw_keys, config_values)
        if mapped_size is None:
            mapped_size = size
        mapped[mapped_size] += count

    return {size: mapped[size] for size in sorted(mapped)}


def _empty_locations_rows_by_slot_size(
    summary_rows: list[dict[str, str]],
    method_label: str,
) -> list[dict[str, str]]:
    """Report occupied, total, and empty locations by configured slot-size family."""
    rows: list[dict[str, str]] = []
    for row in summary_rows:
        config_id = str(row.get("Config_ID", "")).strip()
        if not config_id:
            continue

        source_slot_sizes = common._decode_excel_text(row.get("Source_Slot_Sizes", "")).strip()
        parsed_sources = []
        if source_slot_sizes:
            parsed_sources = [
                int(round(float(token)))
                for token in re.split(r"[,;\s]+", source_slot_sizes)
                if str(token).strip()
            ]
        minimum_counts = _parse_count_signature(str(row.get("Minimum_Required_Counts", "")))
        total_counts = common._exclude_fixed_layout_slot_counts(
            _parse_count_signature(str(row.get("Layout_Slot_Size_Distribution", "")))
        )

        config_family = tuple(sorted(set(parsed_sources) or set(minimum_counts.keys()) | set(total_counts.keys())))
        minimum_counts = _effective_slot_distribution_counts(minimum_counts, config_family)
        total_counts = _effective_slot_distribution_counts(total_counts, config_family)
        occupied_exact = _occupied_allocation_by_exact_slot_size(minimum_counts, total_counts)

        all_sizes = sorted(set(total_counts.keys()) | set(occupied_exact.keys()))
        for size in all_sizes:
            total_count = max(total_counts.get(size, 0), 0)
            occupied_count = max(occupied_exact.get(size, 0), 0)
            empty_count = max(total_count - occupied_count, 0)
            rows.append(
                {
                    "Heuristic": method_label,
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
            if rounded < min(config_values):
                continue
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
        if any(int(round(float(value))) < minimum_value for value in slots):
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
                    if rounded < minimum_value:
                        return False
                    if rounded not in config_values and rounded not in legal_topfill_values:
                        return False
        target_total = sum(slots) + (len(slots) - 1) * common.BEAM_HEIGHT
        if target_total > common.MAX_USED_HEIGHT_BASE + 1e-6:
            return False
        if target_total < 0.0:
            return False
        if abs(target_total - common.MAX_USED_HEIGHT_BASE) <= 1e-6 and not _column_support_band_is_valid(slots):
            pass
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

        for idx, slot_size in enumerate(slots):
            rounded = int(round(float(slot_size)))
            counted_as = _effective_requirement_slot_size(rounded, slots, available_slot_sizes)
            if counted_as is None:
                return False
            layout_counts[counted_as] += 1

    if not nonempty_column_keys:
        return False

    if not _rack_profiles_are_exactly_uniform(column_assignments, nonempty_column_keys):
        return False

    full_layout_target_reached = (
        enforce_minimum_total_locations
        or assigned_locations_total >= common._explicit_occupied_target_total()
    )
    if (
        minimum_required_counts is not None
        and full_layout_target_reached
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


def _column_physical_height_usage(column_slots: list[float]) -> float:
    """Return the full physical column usage including beam gap height."""
    slots = [float(value) for value in (column_slots or []) if float(value) > 0.0]
    if not slots:
        return 0.0
    return sum(slots) + max(len(slots) - 1, 0) * common.BEAM_HEIGHT


def _slot_signatures_from_location_rows(
    location_rows: list[dict[str, str]],
    available_slot_sizes: SlotSizeSequence = None,
) -> tuple[str, str]:
    """Build exact-family distribution and cumulative-coverage signatures from locations."""
    # Recompute slot-distribution signatures directly from location-level rows,
    # excluding only the actual non-usable layout rows from capacity totals.
    # Legal topfill values are counted as their underlying lower-slot family for
    # summary output, because they represent a final completion of that family.
    exact_counts: dict[int, int] = defaultdict(int)
    for row in location_rows:
        slot_size = common._to_float(row.get("Assigned_Slot_Size_cm"))
        if slot_size is None:
            continue
        exact_counts[int(round(float(slot_size)))] += 1

    effective_counts = _effective_slot_distribution_counts(exact_counts, available_slot_sizes)
    distribution = "|".join(f"{size}:{count}" for size, count in sorted(effective_counts.items()))

    running = 0
    cumulative: dict[int, int] = {}
    for size in sorted(effective_counts.keys(), reverse=True):
        running += effective_counts[size]
        cumulative[size] = running
    cumulative_signature = "|".join(f"{size}:{cumulative[size]}" for size in sorted(cumulative.keys()))

    return distribution, cumulative_signature


def _rack_profile_rows_from_location_rows(
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
        raw_slot_counts: Counter[int] = Counter()
        for row in rows:
            slot_size = common._to_float(row.get("Assigned_Slot_Size_cm"))
            if slot_size is not None:
                rounded_slot = int(round(float(slot_size)))
                raw_slot_counts[rounded_slot] += 1

        # Export-only remapping: keep the true rack profile order untouched, but report the
        # per-rack distribution using the configured family buckets used by the legal exact-fill
        # summary. This is a CSV/reporting fix only; it does not change the underlying physical
        # assignment or rack ordering.
        slot_counts = Counter(_effective_slot_distribution_counts(dict(raw_slot_counts), config_slot_sizes))

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
                "Config_ID": config_id,
                "Rack": rack,
                "Rack_Column_Count": str(len(columns)),
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


def _pre_robust_sort_key(summary_row: dict[str, str]) -> tuple[int, int, int, float]:
    """Rank layouts by feasibility, beam work, then remaining physical space."""
    feasible_penalty = 0 if str(summary_row.get("Layout_Feasible", "")).strip().upper() == "YES" else 1
    additional_beams = common._to_int_default(summary_row.get("Additional_Beams_Required"), 0)
    return (
        feasible_penalty,
        common._to_int_default(summary_row.get("Beam_Relocations_Total"), 0),
        additional_beams,
        common._to_float(summary_row.get("_Pre_Robust_Space_Left")) or 0.0,
    )


def build_layout_generation() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Generate candidate layouts and emit summary, column, and location-level outputs."""
    print(f"[Stage 6] starting layout generation for configs from {INPUT_CONFIG_FILE.name} and {INPUT_CAPACITY_SUMMARY_FILE.name}")
    prepared_rows = _read_csv(INPUT_PREPARED)
    raw_configs = _candidate_configs()
    configs = _candidate_configs_for_exhaustive_search(raw_configs)
    if not configs:
        configs = raw_configs
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
        style_candidates: list[StyleCandidate] = []
        style = IMPLEMENTATION_STYLE
        layout_id = f"LAY_{layout_counter:03d}"
        layout_counter += 1
        if _raise_config_timeout_and_return(config_id, config_start_time, config_time_limit_seconds):
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            continue
        config_slot_sizes = _slot_sizes_from_capacity(rows)

        # The deficit-coverage rule is the actual assignment decision in Stage 6.
        # Subsequent repair passes are disabled here so they cannot rewrite the selected rack profile
        # away from the best remaining-deficit choice.
        print(f"[Stage 6] config {config_id}: building rack profiles from slot family {config_slot_sizes}")
        if len(config_slot_sizes) >= FAST_FAIL_PROFILE_PROBE_FAMILY_SIZE:
            required_map = {float(size): int(count) for size, count in base_exact_counts.items()}
            probe_deadline = config_start_time + max(FAST_FAIL_PROFILE_PROBE_SECONDS + 5.0, 20.0)
            probe_result = _quick_profile_feasibility_probe(
                config_slot_sizes,
                required_counts=required_map,
                timeout_seconds=FAST_FAIL_PROFILE_PROBE_SECONDS,
                config_deadline=min(probe_deadline, _LAST_STAGE6_CONFIG_DEADLINE) if _LAST_STAGE6_CONFIG_DEADLINE is not None else probe_deadline,
            )
            if not probe_result:
                print(f"[Stage 6] config {config_id}: quick probe found no immediate legal profile path; continuing with full wider search under the larger time budget")
        column_assignments = _build_deficit_coverage_layout(
            rack_columns=layout_columns,
            required_counts={float(size): int(count) for size, count in base_exact_counts.items()},
            config_slot_sizes=config_slot_sizes,
            config_deadline=_LAST_STAGE6_CONFIG_DEADLINE,
            config_id=config_id,
        )
        if _raise_config_timeout_and_return(config_id, config_start_time, config_time_limit_seconds):
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            generated_profile_rows.append({
                "Config_ID": config_id,
                "Profile_Index": "0",
                "Profile_Values": "",
                "Profile_Signature": "TIMED_OUT",
                "Source_Slot_Sizes": ",".join(f"{int(size)}" for size in config_slot_sizes),
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
            })
            continue
        # The legal rack profile is selected directly from the generated legal candidate pool.
        # No post-assignment repair or rebuild step is allowed to mutate the chosen profile.
        print(f"[Stage 6] config {config_id}: assigned profiles to {len(column_assignments)} rack columns")
        if _raise_config_timeout_and_return(config_id, config_start_time, config_time_limit_seconds):
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            generated_profile_rows.append({
                "Config_ID": config_id,
                "Profile_Index": "0",
                "Profile_Values": "",
                "Profile_Signature": "TIMED_OUT",
                "Source_Slot_Sizes": ",".join(f"{int(size)}" for size in config_slot_sizes),
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
            })
            continue
        expansion_slot_sizes = sorted({float(size) for size in config_slot_sizes}) if config_slot_sizes else sorted(float(slot_size) for slot_size in base_exact_counts)
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
                })
                continue
        for profile_index, profile in enumerate(feasible_profile_pool, start=1):
            generated_profile_rows.append({
                "Config_ID": config_id,
                "Profile_Index": str(profile_index),
                "Profile_Values": ",".join(str(int(round(float(value)))) for value in profile),
                "Profile_Signature": "|".join(f"{int(round(float(value)))}" for value in profile),
                "Source_Slot_Sizes": ",".join(f"{int(size)}" for size in config_slot_sizes),
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
        if _raise_config_timeout_and_return(config_id, config_start_time, config_time_limit_seconds):
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            continue
        if _LAST_STAGE6_CONFIG_DEADLINE is not None and time.perf_counter() >= _LAST_STAGE6_CONFIG_DEADLINE:
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            continue
        generated_location_rows = common._build_generated_layout_location_rows(
            layout_id,
            config_id,
            style,
            column_assignments,
            segments=beam_segments,
            layout_thresholds_by_rack=layout_thresholds_by_rack,
        )
        layout_slot_distribution, layout_slot_cumulative = _slot_signatures_from_location_rows(
            generated_location_rows,
            available_slot_sizes=expansion_slot_sizes,
        )
        layout_signature = _layout_signature(generated_location_rows)
        if _raise_config_timeout_and_return(config_id, config_start_time, config_time_limit_seconds):
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            continue
        if _LAST_STAGE6_CONFIG_DEADLINE is not None and time.perf_counter() >= _LAST_STAGE6_CONFIG_DEADLINE:
            candidate_layout_rows.append(_timeout_summary_row(config_id, layout_id, config_start_time, config_time_limit_seconds, base_exact_counts))
            continue
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
        space_left = sum(
            max(common.MAX_USED_HEIGHT_BASE - physical_used_by_column.get(column_key, 0.0), 0.0)
            for column_key in layout_columns
        )
        capacity_margin = assigned_total - required_locations_total

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
        rack_search_elapsed = _LAST_STAGE6_STEP_TIMINGS.get("rack_search", 0.0)
        profile_generation_timed_out = _LAST_STAGE6_TIMEOUTS.get("profile_generation", False)
        rack_search_timed_out = _LAST_STAGE6_TIMEOUTS.get("rack_search", False)
        total_runtime_seconds = _runtime_clamped_to_limit(config_start_time, config_time_limit_seconds)
        print(
            f"[Stage 6] config {config_id}: timings -> "
            f"profile_generation={profile_generation_elapsed:.3f}s | "
            f"rack_search={rack_search_elapsed:.3f}s | "
            f"total={total_runtime_seconds:.3f}s | "
            f"profile_generation_timeout={str(profile_generation_timed_out).upper()} | "
            f"rack_search_timeout={str(rack_search_timed_out).upper()} | "
            f"final_layout_feasible={str(final_layout_feasible).upper()}"
        )
        summary_row = {
                "Layout_ID": layout_id,
                "Config_ID": config_id,
                "Runtime_Seconds": f"{total_runtime_seconds:.3f}",
                "Runtime_Minutes": f"{total_runtime_seconds / 60.0:.3f}",
                "Profile_Generation_Seconds": f"{profile_generation_elapsed:.3f}",
                "Rack_Search_Seconds": f"{rack_search_elapsed:.3f}",
                "Profile_Generation_Timeout": "YES" if profile_generation_timed_out else "NO",
                "Rack_Search_Timeout": "YES" if rack_search_timed_out else "NO",
                "Layout_Feasible": "YES" if final_layout_feasible else "NO",
                "Allocation_Feasible_Initial": "YES" if feasible_layout else "NO",
                "_Pre_Robust_Space_Left": f"{space_left:.3f}",
                "Total_Locations": str(assigned_total),
                "Capacity_Margin": str(capacity_margin),
                "Beam_Relocations_Total": str(relocation_total),
                "Required_Beams_Total": str(required_beams),
                "Additional_Beams_Required": str(additional_beams),
                "Required_Grids_Total": str(required_grids),
                "Additional_Grids_Required": str(additional_grids),
                "Minimum_Required_Counts": "|".join(f"{int(size)}:{count}" for size, count in sorted(base_exact_counts.items())),
                "Slot_Composition_Signature": "|".join(f"{int(size)}:{count}" for size, count in sorted(base_exact_counts.items())),
                "Layout_Slot_Size_Distribution": layout_slot_distribution,
                "Layout_Slot_Size_Cumulative_Coverage": layout_slot_cumulative,
                "Source_Slot_Sizes": common._encode_excel_text(",".join(f"{int(size)}" for size in config_slot_sizes)),
                "Feasible_Profiles_Considered_Total": str(int(allocation_diagnostics.get("Feasible_Profiles_Considered_Total", 0.0))),
                "Feasible_Profiles_Used_Total": str(int(allocation_diagnostics.get("Feasible_Profiles_Used_Total", 0.0))),
            }

        # Capture per-column slot mix and beam movement details.
        slot_mix_by_column: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        for row in generated_location_rows:
            rack = str(row.get("Rack", "")).strip()
            column = str(row.get("Column", "")).strip()
            slot = common._to_float(row.get("Assigned_Slot_Size_cm"))
            if rack and column and slot is not None:
                rack_column = f"{rack}{int(column):02d}"
                slot_mix_by_column[rack_column][int(round(float(slot)))] += 1
        for rack_column, slot_counts in list(slot_mix_by_column.items()):
            slot_mix_by_column[rack_column] = dict(sorted(slot_counts.items()))
            slot_mix_by_column[rack_column] = dict(
                _effective_slot_distribution_counts(slot_mix_by_column[rack_column], config_slot_sizes)
            )

        style_column_rows: list[dict[str, str]] = []
        for column_key, slots in sorted(column_assignments.items()):
            beam_count = max(len(slots) - 1, 0)
            mix = slot_mix_by_column.get(column_key, {})
            style_column_rows.append(
                {
                    "Layout_ID": layout_id,
                    "Config_ID": config_id,
                    "Rack_Column": column_key,
                    "Beam_Count_Used": str(beam_count),
                    "Beam_Relocations_In_Column": str(relocation_by_column.get(column_key, 0)),
                    "Removed_Beams_In_Column": str(removed_by_column.get(column_key, 0)),
                    "Added_Beams_In_Column": str(added_by_column.get(column_key, 0)),
                    "Slot_Size_Distribution": "|".join(
                        f"{int(slot_size)}:{count}" for slot_size, count in sorted(mix.items())
                    ),
                }
            )

        style_candidates.append((summary_row, generated_location_rows, style_column_rows, layout_signature))

        if not style_candidates:
            continue

        for summary, _, _, _ in style_candidates:
            summary["Pre_Robustness_Status"] = "PENDING"
            summary["Pre_Robustness_Rank"] = ""
            summary["Pre_Robustness_Prune_Reason"] = ""

        config_style_bundles.append((config_id, style_candidates))

    if config_style_bundles:
        def _bundle_score(bundle: ConfigStyleBundle) -> tuple[int, int, int]:
            """Summarize a config bundle by its strongest pre-robust candidate rank."""
            _config_id, candidates = bundle
            return min(_pre_robust_sort_key(candidate[0]) for candidate in candidates)

        by_composition: dict[str, list[ConfigStyleBundle]] = defaultdict(list)
        for bundle in config_style_bundles:
            _config_id, candidates = bundle
            signature = str(candidates[0][0].get("Slot_Composition_Signature", "")).strip() if candidates else ""
            by_composition[signature].append(bundle)

        composition_winners: list[ConfigStyleBundle] = []
        for bundles in by_composition.values():
            composition_winners.append(min(bundles, key=_bundle_score))

        finalists = sorted(composition_winners, key=_bundle_score)
        selected_config_ids = {config_id for config_id, _candidates in finalists}

        if PRE_ROBUST_LAYOUT_LIMIT is not None:
            target_count = min(PRE_ROBUST_LAYOUT_LIMIT, len(config_style_bundles))
            finalists = sorted(composition_winners, key=_bundle_score)[:target_count]
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
                export_slot_sizes = _slot_sizes_for_config_id(capacity_rows, config_id)
                rack_rows = _rack_profile_rows_from_location_rows(
                    config_id,
                    location_rows,
                    export_slot_sizes,
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
        "Rack_Search_Seconds",
        "Profile_Generation_Timeout",
        "Rack_Search_Timeout",
        "Layout_Feasible",
        "Allocation_Feasible_Initial",
        "Total_Locations",
        "Capacity_Margin",
        "Beam_Relocations_Total",
        "Required_Beams_Total",
        "Additional_Beams_Required",
        "Required_Grids_Total",
        "Additional_Grids_Required",
        "Minimum_Required_Counts",
        "Layout_Slot_Size_Distribution",
        "Layout_Slot_Size_Cumulative_Coverage",
        "Source_Slot_Sizes",
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
        "Profile_Generation_Timeout",
        "Rack_Search_Timeout",
        "Beam_Relocations_Total",
        "Initial_Beams_Total",
        "Required_Beams_Total",
        "Additional_Beams_Required",
        "Initial_Grids_Total",
        "Required_Grids_Total",
        "Additional_Grids_Required",
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
            "Beam_Relocations_In_Column",
            "Removed_Beams_In_Column",
            "Added_Beams_In_Column",
            "Slot_Size_Distribution",
        ],
        [
            {
                field: str(row.get(field, ""))
                for field in [
                    "Config_ID",
                    "Rack_Column",
                    "Beam_Count_Used",
                    "Beam_Relocations_In_Column",
                    "Removed_Beams_In_Column",
                    "Added_Beams_In_Column",
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
                ]
            }
            for row in candidate_layout_location_rows_all
        ],
    )

    rack_export_fieldnames = [
        "Config_ID",
        "Rack",
        "Rack_Column_Count",
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
            "Heuristic",
            "Config_ID",
            "Slot_Size_cm",
            "Occupied",
            "Total_Locations_In_Layout",
            "Empty",
        ],
        empty_rows,
    )

    return candidate_layout_rows, candidate_layout_column_rows, candidate_layout_location_rows


def _su_cached_feasible_assignment(
    stage6: Any,
    config_id: str,
    rack_columns: list[str],
    required: dict[float, int],
    slot_sizes: object,
) -> dict[str, list[float]] | None:
    """Reuse a feasible baseline assignment for this config when its counts still fit."""
    output_dir = stage6.common.STAGE6_OUTPUT_DIR
    summary_path = output_dir / "Candidate_Layout_Summary.csv"
    locations_path = output_dir / "Candidate_Layout_By_Location.csv"
    if not summary_path.exists() or not locations_path.exists():
        return None

    summaries = stage6._read_csv(summary_path)
    matching = next(
        (
            row for row in summaries
            if str(row.get("Config_ID", "")).strip() == config_id
            and str(row.get("Layout_Feasible", "")).strip().upper() == "YES"
        ),
        None,
    )
    if matching is None:
        return None

    assignments: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for row in stage6._read_csv(locations_path):
        if str(row.get("Config_ID", "")).strip() != config_id:
            continue
        try:
            row_index = int(str(row.get("Row", "0")).strip())
            slot_size = float(str(row.get("Assigned_Slot_Size_cm", "")).strip())
            column_index = str(row.get("Column", "")).strip()
        except ValueError:
            continue
        rack = str(row.get("Rack", "")).strip()
        if rack and column_index and slot_size > 0:
            assignments[f"{rack}{int(column_index):02d}"].append((row_index, slot_size))

    ordered_assignments = {
        column: [size for _row, size in sorted(values)]
        for column, values in assignments.items()
        if values
    }
    if not ordered_assignments or not stage6._layout_assignments_are_feasible(
        ordered_assignments,
        list(ordered_assignments),
        float(min(slot_sizes or required)),
        slot_sizes,
        minimum_required_counts=required,
        enforce_minimum_total_locations=True,
    ):
        return None

    return ordered_assignments


def _su_cached_profiles(stage6: Any, config_id: str, slot_sizes: object) -> list[list[float]]:
    """Load unique exact-fill profiles cached for the requested configuration."""
    profiles_path = stage6.common.STAGE6_OUTPUT_DIR / "Generated_Profiles_By_Config.csv"
    if not profiles_path.exists():
        return []
    profiles: list[list[float]] = []
    seen: set[tuple[float, ...]] = set()
    for row in stage6._read_csv(profiles_path):
        if str(row.get("Config_ID", "")).strip() != config_id:
            continue
        try:
            profile = [float(value) for value in str(row.get("Profile_Values", "")).split(",") if value.strip()]
        except ValueError:
            continue
        key = tuple(profile)
        if key in seen or not stage6._profile_is_feasible_exact_fill(profile, slot_sizes):
            continue
        seen.add(key)
        profiles.append(profile)
    return profiles


def _su_counted_space(stage6: Any, profile: list[float], slot_sizes: object) -> float:
    """Total a profile using configured family sizes for legal topfill slots."""
    total = 0.0
    for value in profile:
        family = stage6._effective_requirement_slot_size(value, profile, slot_sizes)
        total += float(family if family is not None else value)
    return total


def _su_space_maximizing_assignment(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: object,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]]:
    """Swap rack profiles to increase slot space without violating required counts."""
    stage6 = _su_space_maximizing_assignment.stage6
    if not rack_columns:
        return {}

    required = {float(size): int(count) for size, count in required_counts.items()}
    generation_start = time.perf_counter()
    assignments = _su_cached_feasible_assignment(stage6, str(config_id or "").strip(), rack_columns, required, config_slot_sizes)
    profiles = _su_cached_profiles(stage6, str(config_id or "").strip(), config_slot_sizes)
    if assignments is None or not profiles:
        assignments = _su_space_maximizing_assignment.baseline_builder(
            rack_columns,
            required,
            config_slot_sizes,
            config_deadline=config_deadline,
            config_id=config_id,
        )
        profiles = [list(profile) for profile in stage6._LAST_STAGE6_PROFILE_POOL]
    if not assignments or not profiles:
        return assignments

    sizes = sorted(required)
    best_profile_by_effect: dict[tuple[int, ...], tuple[list[float], float, dict[float, int]]] = {}
    for profile in profiles:
        raw_effect = stage6._effective_requirement_counts(profile, config_slot_sizes)
        effect = tuple(int(raw_effect.get(int(size), 0)) for size in sizes)
        effect_by_size = {size: int(raw_effect.get(int(size), 0)) for size in sizes}
        profile_space = _su_counted_space(stage6, profile, config_slot_sizes)
        previous = best_profile_by_effect.get(effect)
        if previous is None or profile_space > previous[1]:
            best_profile_by_effect[effect] = (list(profile), profile_space, effect_by_size)
    profile_options = list(best_profile_by_effect.values())

    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[stage6._rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns)
    assigned_counts = {size: 0 for size in sizes}
    current_profiles: dict[str, list[float]] = {}
    for rack in rack_order:
        columns = rack_to_columns[rack]
        profile = list(assignments[columns[0]]) if columns else []
        current_profiles[rack] = profile
        effect = stage6._effective_requirement_counts(profile, config_slot_sizes)
        for size in sizes:
            assigned_counts[size] += int(effect.get(int(size), 0)) * len(columns)

    if any(assigned_counts[size] < required[size] for size in sizes):
        stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - generation_start
        return assignments

    for _ in range(max(len(rack_order) * MAX_REFINEMENT_SWAPS_PER_RACK, 1)):
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            break
        best_swap: tuple[float, str, list[float], dict[float, int]] | None = None
        for rack in rack_order:
            width = len(rack_to_columns[rack])
            current_profile = current_profiles[rack]
            current_effect_raw = stage6._effective_requirement_counts(current_profile, config_slot_sizes)
            current_effect = {size: int(current_effect_raw.get(int(size), 0)) for size in sizes}
            current_space = _su_counted_space(stage6, current_profile, config_slot_sizes)
            for candidate, candidate_space, candidate_effect in profile_options:
                gain = (candidate_space - current_space) * width
                if gain <= 1e-9:
                    continue
                if any(
                    assigned_counts[size] + (candidate_effect[size] - current_effect[size]) * width < required[size]
                    for size in sizes
                ):
                    continue
                if best_swap is None or gain > best_swap[0]:
                    best_swap = (gain, rack, candidate, candidate_effect)
        if best_swap is None:
            break

        _gain, rack, candidate, candidate_effect = best_swap
        current_profile = current_profiles[rack]
        current_raw = stage6._effective_requirement_counts(current_profile, config_slot_sizes)
        current_effect = {size: int(current_raw.get(int(size), 0)) for size in sizes}
        width = len(rack_to_columns[rack])
        for size in sizes:
            assigned_counts[size] += (candidate_effect[size] - current_effect[size]) * width
        current_profiles[rack] = candidate
        for column_key in rack_to_columns[rack]:
            assignments[column_key] = list(candidate)

    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - generation_start
    return assignments


_su_space_maximizing_assignment.stage6 = None
_su_space_maximizing_assignment.baseline_builder = None


def _parse_size_counts(value: str) -> dict[int, int]:
    """Parse a pipe-delimited slot-size count signature, ignoring invalid entries."""
    counts: dict[int, int] = {}
    for token in str(value).split("|"):
        if ":" not in token:
            continue
        size_text, count_text = token.split(":", 1)
        try:
            size = int(round(float(size_text)))
            count = int(round(float(count_text)))
        except ValueError:
            continue
        if size > 0 and count > 0:
            counts[size] = counts.get(size, 0) + count
    return counts


def _add_space_metrics(output_dir: Path) -> None:
    """Append occupied space, empty space, and utilization fields to each summary row."""
    summary_path = output_dir / "Candidate_Layout_Summary.csv"
    with summary_path.open("r", newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
        fields = list(rows[0].keys()) if rows else []

    metric_fields = ["Occupied_Slot_Space_cm", "Total_Slot_Space_cm", "Empty_Slot_Space_cm", "Space_Utilization_Pct"]
    for field in metric_fields:
        if field not in fields:
            fields.append(field)

    for row in rows:
        occupied_counts = _parse_size_counts(row.get("Minimum_Required_Counts", ""))
        total_counts = _parse_size_counts(row.get("Layout_Slot_Size_Distribution", ""))
        occupied_space = sum(size * count for size, count in occupied_counts.items())
        total_space = sum(size * count for size, count in total_counts.items())
        empty_space = max(total_space - occupied_space, 0)
        utilization = (occupied_space / total_space * 100.0) if total_space else 0.0
        row["Occupied_Slot_Space_cm"] = f"{occupied_space:.3f}"
        row["Total_Slot_Space_cm"] = f"{total_space:.3f}"
        row["Empty_Slot_Space_cm"] = f"{empty_space:.3f}"
        row["Space_Utilization_Pct"] = f"{utilization:.6f}"

    with summary_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _picking_state_rank(item: tuple[tuple[int, ...], tuple[float, tuple[int, ...]]]) -> tuple[object, ...]:
    """Rank picking-search states by satisfied demand, shortage, and accumulated score."""
    remaining, (score, _choices) = item
    return (
        sum(value <= 0 for value in remaining),
        -sum(max(value, 0) for value in remaining),
        tuple(-value for value in remaining),
        score,
    )


def _picking_prune_states(
    states: dict[tuple[int, ...], tuple[float, tuple[int, ...]]],
) -> dict[tuple[int, ...], tuple[float, tuple[int, ...]]]:
    """Trim the assignment search to its highest-ranked states."""
    if len(states) <= MAX_SEARCH_STATES:
        return states
    return dict(heapq.nlargest(MAX_SEARCH_STATES, states.items(), key=_picking_state_rank))


def _picking_demand(slot_sizes: list[float]) -> tuple[dict[int, int], int, int, int, int]:
    """Map picked items to their smallest fitting slot family and tally uncovered demand."""
    demand: dict[int, int] = defaultdict(int)
    covered_items = 0
    covered_picks = 0
    uncovered_items = 0
    uncovered_picks = 0
    if not PICKING_INPUT_FILE.exists():
        return {}, 0, 0, 0, 0

    with PICKING_INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            try:
                item_height = float(str(row.get("Item Height", "")).strip())
                picks = int(float(str(row.get("Count of Assembly", "0")).strip()))
            except ValueError:
                continue
            if item_height <= 0 or picks < 0:
                continue
            fitting_sizes = [int(round(size)) for size in slot_sizes if size >= item_height]
            if not fitting_sizes:
                uncovered_items += 1
                uncovered_picks += picks
                continue
            assigned_size = min(fitting_sizes)
            demand[assigned_size] += picks
            covered_items += 1
            covered_picks += picks

    return dict(demand), covered_items, covered_picks, uncovered_items, uncovered_picks


def _picking_profile_is_feasible_unordered(profile: list[float], slot_sizes: list[float], stage6: Any) -> bool:
    """Check slot limits, physical fill, and configured-family rules for a profile."""
    slots = [float(value) for value in profile if float(value) > 0.0]
    if not slots:
        return False
    if any(int(round(value)) > int(round(stage6.common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)) for value in slots):
        return False
    if any(int(round(value)) % 10 not in (4, 9) for value in slots):
        return False
    physical_total = sum(slots) + (len(slots) - 1) * stage6.common.BEAM_HEIGHT
    if abs(physical_total - stage6.common.MAX_USED_HEIGHT_BASE) > 1e-9:
        return False

    config_values = set(stage6._config_size_values(slot_sizes))
    legal_topfills = set(stage6._legal_topfill_values(slot_sizes))
    lower_slots = [int(round(value)) for value in slots[:-1]]
    if any(value not in config_values for value in lower_slots):
        return False

    final_slot = int(round(slots[-1]))
    if final_slot in config_values:
        return True
    if final_slot not in legal_topfills:
        return False
    effective_final = stage6._effective_requirement_slot_size(final_slot, slots, slot_sizes)
    return effective_final in config_values


def _profile_picking_score(profile: list[float], demand: dict[int, int], slot_sizes: list[float], stage6: Any) -> float:
    """Score pick demand by slot family, weighting lower rack positions more highly."""
    if not profile:
        return 0.0
    score = 0.0
    profile_length = len(profile)
    for index, value in enumerate(profile):
        family = stage6._effective_requirement_slot_size(value, profile, slot_sizes)
        picks = demand.get(int(family), 0) if family is not None else 0
        position_quality = (profile_length - index) / profile_length
        score += picks * position_quality
    return score


def _generate_picking_ordered_profiles(
    slot_sizes: list[float],
    demand: dict[int, int],
    stage6: Any,
    deadline: float | None,
) -> list[list[float]]:
    """Generate legal profile orderings and sort them by weighted picking score."""
    family = sorted(set(stage6._config_size_values(slot_sizes)))
    if not family:
        return []
    legal_topfills = stage6._legal_topfill_values(family)
    max_lower_rows = max(1, min(18, int(stage6.common.MAX_USED_HEIGHT_BASE // (min(family) + stage6.common.BEAM_HEIGHT))))
    profiles: list[list[float]] = []
    seen: set[tuple[int, ...]] = set()
    combination_checks = 0

    for lower_count in range(1, max_lower_rows + 1):
        if deadline is not None and time.perf_counter() >= deadline:
            raise stage6.Stage6ProfileGenerationTimeout("picking profile generation exceeded the config deadline")
        for lower_combo in itertools.combinations_with_replacement(family, lower_count):
            combination_checks += 1
            if combination_checks % 256 == 0 and deadline is not None and time.perf_counter() >= deadline:
                raise stage6.Stage6ProfileGenerationTimeout("picking profile generation exceeded the config deadline")
            lower_stack = tuple(sorted(lower_combo, reverse=True))
            support_height = sum(lower_stack) + (len(lower_stack) - 1) * stage6.common.BEAM_HEIGHT
            if support_height < 504.0 - 1e-9:
                continue
            topfill = stage6.common.MAX_USED_HEIGHT_BASE - sum(lower_stack) - stage6.common.BEAM_HEIGHT * len(lower_stack)
            if topfill <= 0.0 or topfill > 214.0:
                continue
            rounded_topfill = int(round(topfill))
            if rounded_topfill not in family and rounded_topfill not in legal_topfills:
                continue

            if rounded_topfill in family:
                ordered_candidates = itertools.permutations((*lower_stack, rounded_topfill))
            else:
                ordered_candidates = ((*ordering, float(rounded_topfill)) for ordering in itertools.permutations(lower_stack))

            for candidate in ordered_candidates:
                if len(profiles) >= PROFILE_ORDERING_LIMIT:
                    break
                normalized = tuple(int(round(value)) for value in candidate)
                if normalized in seen:
                    continue
                candidate_values = [float(value) for value in normalized]
                if not _picking_profile_is_feasible_unordered(candidate_values, slot_sizes, stage6):
                    continue
                seen.add(normalized)
                profiles.append(candidate_values)
            if len(profiles) >= PROFILE_ORDERING_LIMIT:
                break
        if len(profiles) >= PROFILE_ORDERING_LIMIT:
            break

    return sorted(profiles, key=lambda profile: _profile_picking_score(profile, demand, slot_sizes, stage6), reverse=True)


def _picking_assignment(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: object,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]]:
    """Choose picking-ranked profiles per rack while satisfying exact slot-family counts."""
    stage6 = _picking_assignment.stage6
    if not rack_columns:
        return {}
    slot_sizes = [float(value) for value in (config_slot_sizes or list(required_counts))]
    demand, *_ = _picking_demand(slot_sizes)
    generation_start = time.perf_counter()
    profiles = _generate_picking_ordered_profiles(slot_sizes, demand, stage6, config_deadline)
    stage6._LAST_STAGE6_PROFILE_POOL = [list(profile) for profile in profiles]
    stage6._LAST_STAGE6_STEP_TIMINGS["profile_generation"] = time.perf_counter() - generation_start
    stage6._LAST_STAGE6_STEP_TIMINGS["profile_shortlist"] = 0.0
    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = 0.0
    if not profiles:
        return _picking_assignment.baseline_builder(
            rack_columns, required_counts, config_slot_sizes,
            config_deadline=config_deadline, config_id=config_id,
        )

    required = {float(size): int(count) for size, count in required_counts.items()}
    ordered_sizes = sorted(required, reverse=True)
    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[stage6._rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns)
    best_effect: dict[tuple[int, ...], tuple[list[float], float]] = {}
    for profile in profiles:
        counts = stage6._effective_requirement_counts(profile, slot_sizes)
        effect = tuple(int(counts.get(int(size), 0)) for size in ordered_sizes)
        score = _profile_picking_score(profile, demand, slot_sizes, stage6)
        previous = best_effect.get(effect)
        if previous is None or score > previous[1]:
            best_effect[effect] = (list(profile), score)
    ranked_effects = sorted(
        best_effect.items(),
        key=lambda item: (
            sum(min(item[0][index], int(required[size])) > 0 for index, size in enumerate(ordered_sizes)),
            sum(min(item[0][index], int(required[size])) for index, size in enumerate(ordered_sizes)),
            sum(min(item[0][index], int(required[size])) * size for index, size in enumerate(ordered_sizes)),
            item[1][1],
        ),
        reverse=True,
    )[:MAX_PROFILE_OPTIONS]
    profile_options = [(profile, effect, score) for effect, (profile, score) in ranked_effects]
    states: dict[tuple[int, ...], tuple[float, tuple[int, ...]]] = {
        tuple(int(required[size]) for size in ordered_sizes): (0.0, ())
    }
    for rack in rack_order:
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            raise stage6.Stage6RackSearchTimeout("picking rack search exceeded the config deadline")
        rack_width = len(rack_to_columns[rack])
        next_states: dict[tuple[int, ...], tuple[float, tuple[int, ...]]] = {}
        for remaining, (score, choices) in states.items():
            for profile_index, (_profile, effect, profile_score) in enumerate(profile_options):
                next_remaining = tuple(
                    max(remaining[index] - effect[index] * rack_width, 0)
                    for index in range(len(ordered_sizes))
                )
                next_value = (score + profile_score * rack_width, (*choices, profile_index))
                previous = next_states.get(next_remaining)
                if previous is None or next_value[0] > previous[0]:
                    next_states[next_remaining] = next_value
                if len(next_states) > MAX_SEARCH_STATES * 2:
                    next_states = _picking_prune_states(next_states)
        states = _picking_prune_states(next_states)

    chosen = max(states.items(), key=_picking_state_rank)
    choices = chosen[1][1]
    assignments: dict[str, list[float]] = {}
    for rack_index, rack in enumerate(rack_order):
        profile_index = choices[rack_index] if rack_index < len(choices) else 0
        profile = profile_options[profile_index][0]
        for column_key in sorted(rack_to_columns[rack]):
            assignments[column_key] = list(profile)
    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - generation_start
    if not stage6._layout_assignments_are_feasible(
        assignments, list(assignments), float(min(slot_sizes)), slot_sizes,
        minimum_required_counts=required, enforce_minimum_total_locations=True,
    ):
        return _picking_assignment.baseline_builder(
            rack_columns, required, config_slot_sizes,
            config_deadline=config_deadline, config_id=config_id,
        )
    return assignments


_picking_assignment.stage6 = None
_picking_assignment.baseline_builder = None


def _add_picking_metrics(output_dir: Path, stage6: Any) -> None:
    """Add picking coverage counts and scores to the generated layout summaries."""
    summary_path = output_dir / "Candidate_Layout_Summary.csv"
    locations_path = output_dir / "Candidate_Layout_By_Location.csv"
    with summary_path.open("r", newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
        fields = list(rows[0].keys()) if rows else []
    with locations_path.open("r", newline="", encoding="utf-8-sig") as source:
        locations = list(csv.DictReader(source))

    metric_fields = [
        "Picking_Covered_Item_Count",
        "Picking_Covered_Pick_Count",
        "Picking_Uncovered_Item_Count",
        "Picking_Uncovered_Pick_Count",
        "Picking_Score",
    ]
    for field in metric_fields:
        if field not in fields:
            fields.append(field)

    location_groups: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for location in locations:
        location_groups[str(location.get("Config_ID", ""))][
            f"{location.get('Rack', '')}{location.get('Column', '')}"
        ].append(location)

    for row in rows:
        config_id = str(row.get("Config_ID", ""))
        slot_sizes = [
            float(value)
            for value in str(row.get("Source_Slot_Sizes", "")).replace('="', '').replace('"', '').split(",")
            if value.strip()
        ]
        demand, covered_items, covered_picks, uncovered_items, uncovered_picks = _picking_demand(slot_sizes)
        score = 0.0
        for column_rows in location_groups.get(config_id, {}).values():
            ordered = sorted(column_rows, key=lambda item: int(item.get("Row", "0") or 0))
            profile_values = [float(item.get("Assigned_Slot_Size_cm", "0") or 0) for item in ordered]
            for index, location in enumerate(ordered):
                size = float(location.get("Assigned_Slot_Size_cm", "0") or 0)
                family = stage6._effective_requirement_slot_size(size, profile_values, slot_sizes)
                if family is not None:
                    score += demand.get(int(family), 0) * ((len(ordered) - index) / len(ordered))
        row.update(
            {
                "Picking_Covered_Item_Count": str(covered_items),
                "Picking_Covered_Pick_Count": str(covered_picks),
                "Picking_Uncovered_Item_Count": str(uncovered_items),
                "Picking_Uncovered_Pick_Count": str(uncovered_picks),
                "Picking_Score": f"{score:.6f}",
            }
        )

    with summary_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _run_space_utilization() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Run layout generation with the space objective and write suffixed SU outputs."""
    global LAYOUT_OUTPUT_DIR, IMPLEMENTATION_STYLE, _build_deficit_coverage_layout
    old_output, old_style, old_builder = LAYOUT_OUTPUT_DIR, IMPLEMENTATION_STYLE, _build_deficit_coverage_layout
    old_stage6, old_baseline = _su_space_maximizing_assignment.stage6, _su_space_maximizing_assignment.baseline_builder
    output_dir = Path(
        os.environ.get("PIPELINE_SPACE_UTILIZATION_OUTPUT_DIR", common.OUTPUT_ROOT / "06_Layout_Generation_Space_Utilization")
    ).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    context = _Stage6Context(globals())
    try:
        LAYOUT_OUTPUT_DIR = output_dir
        IMPLEMENTATION_STYLE = "space_utilization"
        _su_space_maximizing_assignment.stage6 = context
        _su_space_maximizing_assignment.baseline_builder = _build_deficit_coverage_layout
        _build_deficit_coverage_layout = _su_space_maximizing_assignment
        print(f"[Stage 6 Space] output directory: {output_dir}")
        result = build_layout_generation()
        _add_space_metrics(output_dir)
        suffix_output_tree(output_dir, "SU")
        print(f"[Stage 6 Space] complete. Layouts: {len(result[0])}, columns: {len(result[1])}, locations: {len(result[2])}.")
        return result
    finally:
        LAYOUT_OUTPUT_DIR, IMPLEMENTATION_STYLE, _build_deficit_coverage_layout = old_output, old_style, old_builder
        _su_space_maximizing_assignment.stage6 = old_stage6
        _su_space_maximizing_assignment.baseline_builder = old_baseline


def _run_picking_efficiency() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Run layout generation with the picking objective and write suffixed PE outputs."""
    global LAYOUT_OUTPUT_DIR, IMPLEMENTATION_STYLE, _build_deficit_coverage_layout
    old_output, old_style, old_builder = LAYOUT_OUTPUT_DIR, IMPLEMENTATION_STYLE, _build_deficit_coverage_layout
    old_stage6, old_baseline = _picking_assignment.stage6, _picking_assignment.baseline_builder
    output_dir = Path(
        os.environ.get("PIPELINE_PICKING_EFFICIENCY_OUTPUT_DIR", common.OUTPUT_ROOT / "06_Layout_Generation_Picking_Efficiency")
    ).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    context = _Stage6Context(globals())
    try:
        LAYOUT_OUTPUT_DIR = output_dir
        IMPLEMENTATION_STYLE = "picking_efficiency"
        _picking_assignment.stage6 = context
        _picking_assignment.baseline_builder = _build_deficit_coverage_layout
        _build_deficit_coverage_layout = _picking_assignment
        result = build_layout_generation()
        _add_picking_metrics(output_dir, context)
        suffix_output_tree(output_dir, "PE")
        print(f"[Stage 6 Picking] complete. Layouts: {len(result[0])}, columns: {len(result[1])}, locations: {len(result[2])}.")
        return result
    finally:
        LAYOUT_OUTPUT_DIR, IMPLEMENTATION_STYLE, _build_deficit_coverage_layout = old_output, old_style, old_builder
        _picking_assignment.stage6 = old_stage6
        _picking_assignment.baseline_builder = old_baseline


def _beam_state_rank(
    item: tuple[tuple[int, ...], tuple[tuple[int, int, int], tuple[int, ...]]],
) -> tuple[object, ...]:
    """Rank beam-search states by demand coverage, shortage, and relocation score."""
    remaining, (score, _choices) = item
    return (
        sum(value <= 0 for value in remaining),
        -sum(max(value, 0) for value in remaining),
        tuple(-value for value in remaining),
        score,
    )


def _beam_prune_states(
    states: dict[tuple[int, ...], tuple[tuple[int, int, int], tuple[int, ...]]],
) -> dict[tuple[int, ...], tuple[tuple[int, int, int], tuple[int, ...]]]:
    """Keep the best beam-search states when the configured state limit is exceeded."""
    if len(states) <= MAX_SEARCH_STATES:
        return states
    return dict(heapq.nlargest(MAX_SEARCH_STATES, states.items(), key=_beam_state_rank))


def _beam_profile_is_feasible_unordered(profile: list[float], slot_sizes: list[float], stage6: Any) -> bool:
    """Validate physical fill and legal configured-family/topfill slot sizes."""
    slots = [float(value) for value in profile if float(value) > 0.0]
    if not slots:
        return False
    if any(int(round(value)) > int(round(stage6.common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)) for value in slots):
        return False
    if any(int(round(value)) % 10 not in (4, 9) for value in slots):
        return False
    physical_total = sum(slots) + (len(slots) - 1) * stage6.common.BEAM_HEIGHT
    if abs(physical_total - stage6.common.MAX_USED_HEIGHT_BASE) > 1e-9:
        return False

    config_values = set(stage6._config_size_values(slot_sizes))
    legal_topfills = set(stage6._legal_topfill_values(slot_sizes))
    if any(int(round(value)) not in config_values for value in slots[:-1]):
        return False
    final_slot = int(round(slots[-1]))
    if final_slot in config_values:
        return True
    if final_slot not in legal_topfills:
        return False
    effective_final = stage6._effective_requirement_slot_size(final_slot, slots, slot_sizes)
    return effective_final in config_values


def _beam_ordered_profiles(slot_sizes: list[float], stage6: Any, deadline: float | None) -> list[list[float]]:
    """Generate unique legal rack profiles for evaluating current beam positions."""
    family = sorted(set(stage6._config_size_values(slot_sizes)))
    if not family:
        return []
    legal_topfills = stage6._legal_topfill_values(family)
    max_lower_rows = max(1, min(18, int(stage6.common.MAX_USED_HEIGHT_BASE // (min(family) + stage6.common.BEAM_HEIGHT))))
    profiles: list[list[float]] = []
    seen: set[tuple[int, ...]] = set()
    combination_checks = 0

    for lower_count in range(1, max_lower_rows + 1):
        if deadline is not None and time.perf_counter() >= deadline:
            raise stage6.Stage6ProfileGenerationTimeout("beam profile generation exceeded the config deadline")
        for lower_combo in itertools.combinations_with_replacement(family, lower_count):
            combination_checks += 1
            if combination_checks % 256 == 0 and deadline is not None and time.perf_counter() >= deadline:
                raise stage6.Stage6ProfileGenerationTimeout("beam profile generation exceeded the config deadline")
            lower_stack = tuple(sorted(lower_combo, reverse=True))
            support_height = sum(lower_stack) + (len(lower_stack) - 1) * stage6.common.BEAM_HEIGHT
            if support_height < 504.0 - 1e-9:
                continue
            topfill = stage6.common.MAX_USED_HEIGHT_BASE - sum(lower_stack) - stage6.common.BEAM_HEIGHT * len(lower_stack)
            if topfill <= 0.0 or topfill > 214.0:
                continue
            rounded_topfill = int(round(topfill))
            if rounded_topfill not in family and rounded_topfill not in legal_topfills:
                continue

            if rounded_topfill in family:
                candidates = itertools.permutations((*lower_stack, rounded_topfill))
            else:
                candidates = ((*ordering, float(rounded_topfill)) for ordering in itertools.permutations(lower_stack))
            for candidate in candidates:
                if len(profiles) >= PROFILE_ORDERING_LIMIT:
                    break
                normalized = tuple(int(round(value)) for value in candidate)
                if normalized in seen:
                    continue
                values = [float(value) for value in normalized]
                if not _beam_profile_is_feasible_unordered(values, slot_sizes, stage6):
                    continue
                seen.add(normalized)
                profiles.append(values)
            if len(profiles) >= PROFILE_ORDERING_LIMIT:
                break
        if len(profiles) >= PROFILE_ORDERING_LIMIT:
            break
    return profiles


_BEAM_RELOCATION_CONTEXT: dict[str, Any] = {}


def _current_beams_by_rack(stage6: Any) -> dict[str, list[float]]:
    """Read current beam heights and group them by rack."""
    beam_map = stage6._read_csv(stage6.INPUT_LOCATION_BEAM_MAP)
    prepared = stage6._read_csv(stage6.INPUT_PREPARED)
    beam_heights = stage6._read_csv(stage6.INPUT_BEAM_HEIGHT_COORDS)
    current_units, _segments, current_unit_heights = stage6.common._build_current_beam_units_and_segments(
        beam_map, prepared, beam_heights
    )
    by_rack: dict[str, list[float]] = defaultdict(list)
    for unit in current_units:
        parsed = stage6.common._parse_beam_coordinate_parts(unit)
        height = current_unit_heights.get(unit)
        if parsed is not None and height is not None:
            by_rack[parsed[0]].append(float(height))
    return {rack: sorted(values) for rack, values in by_rack.items()}


def _profile_beam_levels(profile: list[float], stage6: Any) -> list[float]:
    """Convert a bottom-up slot profile into the heights of its internal beams."""
    levels: list[float] = []
    cumulative = 0.0
    for index, value in enumerate(profile[:-1]):
        cumulative += float(value)
        levels.append(cumulative + index * stage6.common.BEAM_HEIGHT)
    return levels


def _beam_profile_score(profile: list[float], current_heights: list[float], stage6: Any) -> tuple[int, int, int]:
    """Score a profile by beam matches, required relocations, and added beams."""
    proposed = _profile_beam_levels(profile, stage6)
    available = list(current_heights)
    matches = 0
    for height in proposed:
        matching_index = next(
            (index for index, current in enumerate(available) if abs(current - height) <= stage6.common.BEAM_RELOCATION_TOLERANCE_CM + 1e-9),
            None,
        )
        if matching_index is not None:
            matches += 1
            available.pop(matching_index)
    relocations = max(len(proposed), len(current_heights)) - matches
    additions = max(len(proposed) - len(current_heights), 0)
    return -relocations, matches, -additions


def _beam_search_assignment(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: object,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]] | None:
    """Find feasible rack assignments that best preserve existing beam elevations."""
    stage6 = _beam_assignment.stage6
    if not rack_columns:
        return {}
    slot_sizes = [float(value) for value in (config_slot_sizes or list(required_counts))]
    generation_start = time.perf_counter()
    profiles = _beam_ordered_profiles(slot_sizes, stage6, config_deadline)
    stage6._LAST_STAGE6_PROFILE_POOL = [list(profile) for profile in profiles]
    stage6._LAST_STAGE6_STEP_TIMINGS["profile_generation"] = time.perf_counter() - generation_start
    stage6._LAST_STAGE6_STEP_TIMINGS["profile_shortlist"] = 0.0
    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = 0.0
    if not profiles:
        return None

    required = {float(size): int(count) for size, count in required_counts.items()}
    ordered_sizes = sorted(required, reverse=True)
    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[stage6._rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns)
    current_by_rack = _current_beams_by_rack(stage6)
    options_by_rack: dict[str, list[tuple[list[float], tuple[int, ...], tuple[int, int, int]]]] = {}
    for rack in rack_order:
        best_options: dict[tuple[int, ...], tuple[list[float], tuple[int, int, int]]] = {}
        for profile in profiles:
            counts = stage6._effective_requirement_counts(profile, slot_sizes)
            effect = tuple(int(counts.get(int(size), 0)) for size in ordered_sizes)
            score = _beam_profile_score(profile, current_by_rack.get(rack, []), stage6)
            previous = best_options.get(effect)
            if previous is None or score > previous[1]:
                best_options[effect] = (profile, score)
        ranked_options = sorted(
            best_options.items(),
            key=lambda item: (
                sum(min(item[0][index], int(required[size])) > 0 for index, size in enumerate(ordered_sizes)),
                sum(min(item[0][index], int(required[size])) for index, size in enumerate(ordered_sizes)),
                sum(min(item[0][index], int(required[size])) * size for index, size in enumerate(ordered_sizes)),
                item[1][1][0],
            ),
            reverse=True,
        )[:MAX_PROFILE_OPTIONS]
        options_by_rack[rack] = [(profile, effect, score) for effect, (profile, score) in ranked_options]

    states: dict[tuple[int, ...], tuple[tuple[int, int, int], tuple[int, ...]]] = {
        tuple(int(required[size]) for size in ordered_sizes): ((0, 0, 0), ())
    }
    for rack in rack_order:
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            raise stage6.Stage6RackSearchTimeout("beam rack search exceeded the config deadline")
        rack_width = len(rack_to_columns[rack])
        next_states: dict[tuple[int, ...], tuple[tuple[int, int, int], tuple[int, ...]]] = {}
        for remaining, (score, choices) in states.items():
            for profile_index, (_profile, effect, profile_score) in enumerate(options_by_rack[rack]):
                next_remaining = tuple(max(remaining[index] - effect[index] * rack_width, 0) for index in range(len(ordered_sizes)))
                next_score = tuple(score[index] + profile_score[index] * rack_width for index in range(3))
                previous = next_states.get(next_remaining)
                if previous is None or next_score > previous[0]:
                    next_states[next_remaining] = (next_score, (*choices, profile_index))
                if len(next_states) > MAX_SEARCH_STATES * 2:
                    next_states = _beam_prune_states(next_states)
        states = _beam_prune_states(next_states)

    chosen = max(states.items(), key=_beam_state_rank)
    choices = chosen[1][1]
    assignments: dict[str, list[float]] = {}
    for rack_index, rack in enumerate(rack_order):
        profile_index = choices[rack_index] if rack_index < len(choices) else 0
        profile = options_by_rack[rack][profile_index][0]
        for column_key in sorted(rack_to_columns[rack]):
            assignments[column_key] = list(profile)
    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - generation_start
    if not stage6._layout_assignments_are_feasible(
        assignments, list(assignments), float(min(slot_sizes)), slot_sizes,
        minimum_required_counts=required, enforce_minimum_total_locations=True,
    ):
        return None
    return assignments


def _beam_relocation_total(assignments: dict[str, list[float]], config_id: str, stage6: Any) -> int:
    """Measure total beam relocations for a proposed assignment against current geometry."""
    common_module = stage6.common
    if not _BEAM_RELOCATION_CONTEXT:
        beam_map = stage6._read_csv(stage6.INPUT_LOCATION_BEAM_MAP)
        units, segments, unit_heights = common_module._build_current_beam_units_and_segments(
            beam_map, stage6._read_csv(stage6.INPUT_PREPARED), stage6._read_csv(stage6.INPUT_BEAM_HEIGHT_COORDS)
        )
        _BEAM_RELOCATION_CONTEXT.update(
            units=units,
            segments=segments,
            unit_heights=unit_heights,
            units_by_column=common_module._beam_units_by_column(beam_map),
        )
    context = _BEAM_RELOCATION_CONTEXT
    locations = common_module._build_generated_layout_location_rows(
        "LAY_BEAM_EVAL", config_id, stage6.IMPLEMENTATION_STYLE, assignments,
        segments=context["segments"], layout_thresholds_by_rack={},
    )
    proposed_units, proposed_heights = common_module._build_proposed_beam_units_from_layout_rows(locations, context["segments"])
    total, _by_column, _removed, _added = common_module._beam_relocations(
        context["units"], proposed_units, context["unit_heights"], proposed_heights,
        current_units_by_column=context["units_by_column"],
        proposed_units_by_column=common_module._beam_units_by_column(locations),
    )
    return int(total)


def _beam_assignment(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: object,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]]:
    """Keep a beam-preserving assignment only when it reduces relocations from baseline."""
    stage6 = _beam_assignment.stage6
    baseline = _beam_assignment.baseline_builder(
        rack_columns, required_counts, config_slot_sizes,
        config_deadline=config_deadline, config_id=config_id,
    )
    baseline_pool = [list(profile) for profile in stage6._LAST_STAGE6_PROFILE_POOL]
    candidate = _beam_search_assignment(
        rack_columns, required_counts, config_slot_sizes,
        config_deadline=config_deadline, config_id=config_id,
    )
    if not baseline:
        return candidate
    if not candidate:
        stage6._LAST_STAGE6_PROFILE_POOL = baseline_pool
        return baseline
    if _beam_relocation_total(candidate, str(config_id or ""), stage6) < _beam_relocation_total(baseline, str(config_id or ""), stage6):
        return candidate
    stage6._LAST_STAGE6_PROFILE_POOL = baseline_pool
    return baseline


_beam_assignment.stage6 = None
_beam_assignment.baseline_builder = None


def _run_beam_preservation() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Run Stage 6 with beam preservation and write the suffixed BP output tree."""
    global LAYOUT_OUTPUT_DIR, IMPLEMENTATION_STYLE, _build_deficit_coverage_layout
    old_output, old_style, old_builder = LAYOUT_OUTPUT_DIR, IMPLEMENTATION_STYLE, _build_deficit_coverage_layout
    old_stage6, old_baseline = _beam_assignment.stage6, _beam_assignment.baseline_builder
    output_dir = Path(os.environ.get("PIPELINE_BEAM_PRESERVATION_OUTPUT_DIR", common.OUTPUT_ROOT / "06_Layout_Generation_Beam_Preservation")).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    context = _Stage6Context(globals())
    try:
        LAYOUT_OUTPUT_DIR = output_dir
        IMPLEMENTATION_STYLE = "beam_preservation"
        _beam_assignment.stage6 = context
        _beam_assignment.baseline_builder = _build_deficit_coverage_layout
        _build_deficit_coverage_layout = _beam_assignment
        result = build_layout_generation()
        suffix_output_tree(output_dir, "BP")
        print(f"[Stage 6 Beam] complete. Layouts: {len(result[0])}, columns: {len(result[1])}, locations: {len(result[2])}.")
        return result
    finally:
        LAYOUT_OUTPUT_DIR, IMPLEMENTATION_STYLE, _build_deficit_coverage_layout = old_output, old_style, old_builder
        _beam_assignment.stage6 = old_stage6
        _beam_assignment.baseline_builder = old_baseline


PICKING_COMPARISON_METHODS = {
    "Baseline": (common.STAGE6_OUTPUT_DIR, ""),
    "SU": (common.OUTPUT_ROOT / "06_Layout_Generation_Space_Utilization", "_SU"),
    "PE": (common.OUTPUT_ROOT / "06_Layout_Generation_Picking_Efficiency", "_PE"),
    "BP": (common.OUTPUT_ROOT / "06_Layout_Generation_Beam_Preservation", "_BP"),
}


def _comparison_base_id(config_id: str, suffix: str) -> str:
    """Remove a known heuristic suffix to obtain the shared configuration ID."""
    return config_id[:-len(suffix)] if suffix and config_id.endswith(suffix) else config_id


def _comparison_family(size: float, config_sizes: list[float]) -> float | None:
    """Map a physical slot height to the largest configured size it can represent."""
    fitting = [value for value in config_sizes if value <= size + 1e-9]
    return max(fitting) if fitting else None


def _picking_comparison_for_method(method: str) -> dict[str, dict[str, float]]:
    """Score each feasible layout for one method and count its assigned locations."""
    folder, suffix = PICKING_COMPARISON_METHODS[method]
    summary = common._read_csv(folder / f"Candidate_Layout_Summary{suffix}.csv")
    locations = common._read_csv(folder / f"Candidate_Layout_By_Location{suffix}.csv")
    config_sizes: dict[str, list[float]] = {}
    for row in summary:
        if str(row.get("Layout_Feasible", "")).strip().upper() != "YES":
            continue
        source_sizes = common._decode_excel_text(row.get("Source_Slot_Sizes", ""))
        config_sizes[_comparison_base_id(row["Config_ID"], suffix)] = [
            float(value) for value in source_sizes.split(",") if value.strip()
        ]

    columns: dict[str, dict[str, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
    for row in locations:
        config_id = _comparison_base_id(row["Config_ID"], suffix)
        if config_id in config_sizes:
            columns[config_id][f"{row['Rack']}{row['Column']}"] .append(
                (int(row["Row"]), float(row["Assigned_Slot_Size_cm"]))
            )

    scores: dict[str, dict[str, float]] = {}
    for config_id, slot_sizes in config_sizes.items():
        demand = _picking_demand(slot_sizes)[0]
        score = 0.0
        location_count = 0
        for slots in columns[config_id].values():
            ordered = [size for _row, size in sorted(slots)]
            location_count += len(ordered)
            for index, size in enumerate(ordered):
                family = _comparison_family(size, slot_sizes)
                if family is not None:
                    score += demand.get(int(family), 0) * (len(ordered) - index) / len(ordered)
        scores[config_id] = {"score": score, "locations": location_count}
    return scores


def write_picking_score_comparison() -> Path:
    """Write shared picking scores and per-location scores for configs common to all methods."""
    scored = {method: _picking_comparison_for_method(method) for method in PICKING_COMPARISON_METHODS}
    common_ids = sorted(set.intersection(*(set(values) for values in scored.values())))
    output_file = common.OUTPUT_ROOT / "06_Picking_Score_Comparison" / "Picking_Score_Comparison.csv"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["Config_ID"]
    for method in PICKING_COMPARISON_METHODS:
        fieldnames.extend([f"{method}_Picking_Score", f"{method}_Locations", f"{method}_Score_Per_Location"])
    with output_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        for config_id in common_ids:
            row = {"Config_ID": config_id}
            for method in PICKING_COMPARISON_METHODS:
                entry = scored[method][config_id]
                row[f"{method}_Picking_Score"] = f"{entry['score']:.3f}"
                row[f"{method}_Locations"] = str(int(entry["locations"]))
                row[f"{method}_Score_Per_Location"] = f"{entry['score'] / entry['locations']:.3f}"
            writer.writerow(row)
    print(f"Wrote {len(common_ids)} configurations to {output_file}")
    return output_file


def run_layout_generation(method: str = "Baseline") -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Dispatch baseline or heuristic layout generation through the shared Stage 6 engine."""
    method = str(method or "Baseline").strip().upper()
    if method in {"BASELINE", "BASE"}:
        print("[Stage 6] entrypoint starting (Baseline)")
        result = build_layout_generation()
        print(f"[Stage 6] complete. Layouts: {len(result[0])}, columns: {len(result[1])}, locations: {len(result[2])}.")
        return result
    if method == "SU":
        return _run_space_utilization()
    if method == "PE":
        return _run_picking_efficiency()
    if method == "BP":
        return _run_beam_preservation()
    raise ValueError(f"Unknown Stage 6 method: {method}. Expected Baseline, SU, PE, BP, or PICKING_SCORE_COMPARISON.")


if __name__ == "__main__":
    method = os.environ.get("PIPELINE_ACTIVE_METHOD", "Baseline").strip().upper()
    if method == "PICKING_SCORE_COMPARISON":
        write_picking_score_comparison()
    else:
        run_layout_generation(method)
