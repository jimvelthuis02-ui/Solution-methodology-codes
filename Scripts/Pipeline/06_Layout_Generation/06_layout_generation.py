import csv
import os
from collections import Counter, defaultdict
from functools import lru_cache
from itertools import combinations_with_replacement, product
import sys
from pathlib import Path
from typing import Sequence, TypeAlias

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
LAYOUT_DIAGNOSTICS_DIR = LAYOUT_OUTPUT_DIR
# The pre-robust pass should keep every valid generated layout candidate rather
# than artificially chopping the search space down to a fixed-size shortlist.
PRE_ROBUST_LAYOUT_LIMIT = None
EXHAUSTIVE_SEARCH_CONFIG_LIMIT = 3
IMPLEMENTATION_STYLE = "implementation"
STYLE_PRIORITY = (IMPLEMENTATION_STYLE,)
LOCAL_SEARCH_MAX_ITERATIONS = 6
LOCAL_SEARCH_MAX_EVALUATIONS_PER_ITER = 120
EXHAUSTIVE_PROFILE_LIMIT = 2000
EXHAUSTIVE_PROFILE_NO_IMPROVEMENT_STREAK = 200
EXHAUSTIVE_PROFILE_MAX_SLOT_FAMILY_SIZE = 20
STAGE6_CONFIG_SLOT_SIZE_FOCUS = 3
DEFAULT_TARGET_CONFIGS = "CFG_001"

SummaryRow: TypeAlias = dict[str, str]
DetailRows: TypeAlias = list[dict[str, str]]
LayoutSignature: TypeAlias = tuple[str, ...]
StyleCandidate: TypeAlias = tuple[SummaryRow, DetailRows, DetailRows, LayoutSignature]
ConfigStyleBundle: TypeAlias = tuple[str, list[StyleCandidate]]
SlotSizeSequence: TypeAlias = Sequence[float | int] | None


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


def _fallback_path(path: Path, suffix: str = "_Heuristic") -> Path:
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


def _parse_target_config_ids(raw: str | None) -> set[str]:
    """Parse config IDs from a string, supporting single IDs, comma-separated lists, and ranges."""
    text = str(raw or "").strip()
    if not text:
        return set()

    selected: set[str] = set()
    for token in text.split(","):
        piece = str(token).strip()
        if not piece:
            continue
        if "-" in piece:
            start_text, end_text = piece.split("-", 1)
            start_id = int(_normalize_config_id(start_text).replace("CFG_", ""))
            end_id = int(_normalize_config_id(end_text).replace("CFG_", ""))
            for config_number in range(min(start_id, end_id), max(start_id, end_id) + 1):
                selected.add(f"CFG_{config_number:03d}")
            continue
        normalized = _normalize_config_id(piece)
        if normalized:
            selected.add(normalized)
    return selected


def _target_config_ids_from_script_args() -> set[str]:
    """Allow a quick one-off override like: python 06_layout_generation.py CFG_001 or CFG_001,CFG_003."""
    if len(sys.argv) <= 1:
        return set()
    joined_args = " ".join(sys.argv[1:]).strip()
    if not joined_args or joined_args.startswith("-"):
        return set()
    return _parse_target_config_ids(joined_args)


def _target_config_ids_from_environment() -> set[str]:
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
    return set()


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
        rows = [
            row for row in rows
            if _normalize_config_id(str(row.get("Config_ID", ""))) in target_configs
        ]

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

    if STAGE6_CONFIG_SLOT_SIZE_FOCUS is not None:
        focus = int(STAGE6_CONFIG_SLOT_SIZE_FOCUS)
        filtered_rows = [
            row
            for row in rows
            if str(row.get("Config_ID", "")).strip()
            and 1 <= _slot_count(row) <= focus + 2
        ]
    else:
        filtered_rows = [
            row
            for row in rows
            if str(row.get("Config_ID", "")).strip() and str(row.get("Slot_Sizes", "")).strip()
        ]
    if not filtered_rows:
        filtered_rows = [
            row
            for row in rows
            if str(row.get("Config_ID", "")).strip() and str(row.get("Slot_Sizes", "")).strip()
        ]

    limit = min(EXHAUSTIVE_SEARCH_CONFIG_LIMIT, len(filtered_rows))
    rng = __import__("random").Random(42)
    return rng.sample(filtered_rows, k=limit)


def _segment_bounds(max_col: int) -> list[tuple[int, int]]:
    """Return the warehouse column segments used for same-segment profile enforcement."""
    if max_col < 0:
        return []

    bounds: list[tuple[int, int]] = []
    start = 0

    first_end = min(max_col, 3)
    bounds.append((start, first_end))
    start = first_end + 1

    while start <= max_col:
        end = min(max_col, start + 2)
        bounds.append((start, end))
        start = end + 1

    return bounds


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


def _layout_signature(generated_location_rows: list[dict[str, str]]) -> tuple[str, ...]:
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


def _style_rank(style: str) -> int:
    try:
        return STYLE_PRIORITY.index(style)
    except ValueError:
        return len(STYLE_PRIORITY)


def _signature_similarity(sig_a: LayoutSignature, sig_b: LayoutSignature) -> float:
    if not sig_a and not sig_b:
        return 1.0
    if not sig_a or not sig_b:
        return 0.0
    length = min(len(sig_a), len(sig_b))
    if length <= 0:
        return 0.0
    matches = sum(1 for idx in range(length) if sig_a[idx] == sig_b[idx])
    return matches / length


def _beam_preference_by_column(current_beam_units: set[str]) -> dict[str, int]:
    preference: dict[str, int] = defaultdict(int)
    for beam_unit in current_beam_units:
        for column_key in common._beam_unit_columns(beam_unit):
            preference[column_key] += 1
    return dict(preference)


def _column_order_for_style(column_keys: list[str], used_by_column: dict[str, float], style: str) -> list[str]:
    _ = style
    return sorted(column_keys, key=lambda key: (-used_by_column.get(key, 0.0), key))


def _residual_fill_target_profiles(
    candidate_slot_sizes: list[float],
    target_shares: dict[float, float],
) -> list[dict[float, float]]:
    """Build a small neighborhood around the base residual profile, so the
    remaining height can be filled with nearby distribution shifts rather than
    being rigidly tied to the original exact-count shares.
    """
    profiles: list[dict[float, float]] = []
    base = {size: float(target_shares.get(size, 0.0)) for size in candidate_slot_sizes}
    profiles.append(base)

    for shift in (0.10, 0.20, 0.30):
        for low_size, high_size in zip(candidate_slot_sizes[:-1], candidate_slot_sizes[1:]):
            shifted = dict(base)
            moved = max(shifted.get(low_size, 0.0) * shift, 0.0)
            shifted[low_size] = max(shifted.get(low_size, 0.0) - moved, 0.0)
            shifted[high_size] = shifted.get(high_size, 0.0) + moved
            total = sum(shifted.values())
            if total > 0.0:
                shifted = {size: value / total for size, value in shifted.items()}
            profiles.append(shifted)

            shifted_reverse = dict(base)
            moved_reverse = max(shifted_reverse.get(high_size, 0.0) * shift, 0.0)
            shifted_reverse[high_size] = max(shifted_reverse.get(high_size, 0.0) - moved_reverse, 0.0)
            shifted_reverse[low_size] = shifted_reverse.get(low_size, 0.0) + moved_reverse
            total_reverse = sum(shifted_reverse.values())
            if total_reverse > 0.0:
                shifted_reverse = {size: value / total_reverse for size, value in shifted_reverse.items()}
            profiles.append(shifted_reverse)

    deduped: list[dict[float, float]] = []
    seen: set[tuple[tuple[float, float], ...]] = set()
    for profile in profiles:
        key = tuple(sorted((float(size), float(value)) for size, value in profile.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(profile)
    return deduped


def _fill_columns_for_profile(
    column_assignments: dict[str, list[float]],
    used_by_column: dict[str, float],
    column_keys: list[str],
    candidate_slot_sizes: list[float],
    target_shares: dict[float, float],
    style: str,
    beam_preference: dict[str, int],
) -> tuple[dict[str, list[float]], dict[str, float]]:
    expanded_assignments: dict[str, list[float]] = {
        column_key: list(column_assignments.get(column_key, []))
        for column_key in column_keys
    }
    expanded_used: dict[str, float] = {
        column_key: float(used_by_column.get(column_key, 0.0))
        for column_key in column_keys
    }

    minimum_locations = max(int(common.MIN_LOCATIONS_PER_COLUMN), 1)
    smallest_slot = min(candidate_slot_sizes)
    for column_key in column_keys:
        while len(expanded_assignments[column_key]) < minimum_locations:
            current_count = len(expanded_assignments[column_key])
            next_count = current_count + 1
            allowed_after = common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(next_count - 1, common.MIN_BEAMS_PER_COLUMN)
            proposed_used = expanded_used[column_key] + smallest_slot
            if proposed_used > allowed_after + 1e-9:
                break
            expanded_assignments[column_key].append(smallest_slot)
            expanded_used[column_key] = proposed_used

    added_counts: dict[float, int] = {slot_size: 0 for slot_size in candidate_slot_sizes}
    while True:
        placed = False
        tried_slot_sizes: set[float] = set()
        _ = beam_preference
        expansion_columns = _column_order_for_style(column_keys, expanded_used, style)
        while len(tried_slot_sizes) < len(candidate_slot_sizes):
            remaining_sizes = [size for size in candidate_slot_sizes if size not in tried_slot_sizes]
            total_added = sum(added_counts.values())
            target_size = max(
                remaining_sizes,
                key=lambda size: (
                    (target_shares.get(size, 0.0) * (total_added + 1)) - added_counts.get(size, 0),
                    size,
                ),
            )

            placed_target = False
            for column_key in expansion_columns:
                current_count = len(expanded_assignments[column_key])
                next_count = current_count + 1
                allowed_after = common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(next_count - 1, common.MIN_BEAMS_PER_COLUMN)
                proposed_used = expanded_used[column_key] + target_size
                if proposed_used > allowed_after + 1e-9:
                    continue

                expanded_assignments[column_key].append(target_size)
                expanded_used[column_key] = proposed_used
                added_counts[target_size] = added_counts.get(target_size, 0) + 1
                placed_target = True
                placed = True
                break

            if placed_target:
                break

            tried_slot_sizes.add(target_size)

        if not placed:
            break

    compact_assignments = {
        column_key: slots
        for column_key, slots in expanded_assignments.items()
        if slots
    }
    compact_used = {
        column_key: expanded_used[column_key]
        for column_key in compact_assignments
    }
    return compact_assignments, compact_used


def _expand_layout_capacity(
    column_assignments: dict[str, list[float]],
    used_by_column: dict[str, float],
    column_keys: list[str],
    slot_sizes: list[float],
    minimum_exact_counts: dict[float, int] | None,
    style: str,
    beam_preference: dict[str, int],
) -> tuple[dict[str, list[float]], dict[str, float]]:
    # Fill remaining feasible height while tracking the target slot-size profile.
    # The base policy keeps the original distribution, but a small residual-fill
    # neighborhood is also explored so the remaining space is not forced into the
    # exact same mix when a nearby alternative gives higher utilization.
    expanded_assignments: dict[str, list[float]] = {
        column_key: list(column_assignments.get(column_key, []))
        for column_key in column_keys
    }
    expanded_used: dict[str, float] = {
        column_key: float(used_by_column.get(column_key, 0.0))
        for column_key in column_keys
    }
    candidate_slot_sizes = sorted(
        {
            common._cap_slot_size(size)
            for size in slot_sizes
            if common._to_float(size) is not None and float(size) > 0.0
        },
        reverse=True,
    )
    if not candidate_slot_sizes:
        compact_assignments = {
            column_key: slots
            for column_key, slots in expanded_assignments.items()
            if slots
        }
        compact_used = {
            column_key: expanded_used[column_key]
            for column_key in compact_assignments
        }
        return compact_assignments, compact_used

    target_weights_raw: dict[float, int] = {}
    if isinstance(minimum_exact_counts, dict):
        for slot_size in candidate_slot_sizes:
            target_weights_raw[slot_size] = max(
                common._to_int_default(minimum_exact_counts.get(slot_size), 0),
                0,
            )
    target_weight_total = sum(target_weights_raw.values())
    if target_weight_total <= 0:
        target_shares = {slot_size: 1.0 / len(candidate_slot_sizes) for slot_size in candidate_slot_sizes}
    else:
        target_shares = {
            slot_size: (target_weights_raw.get(slot_size, 0) / target_weight_total)
            for slot_size in candidate_slot_sizes
        }

    profile_variants = _residual_fill_target_profiles(candidate_slot_sizes, target_shares)
    best_assignments = expanded_assignments
    best_used = expanded_used
    best_score = -1.0

    for profile in profile_variants:
        trial_assignments, trial_used = _fill_columns_for_profile(
            expanded_assignments,
            expanded_used,
            column_keys,
            candidate_slot_sizes,
            profile,
            style,
            beam_preference,
        )
        if not trial_assignments:
            continue
        total_allowed = sum(
            common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(len(trial_assignments.get(column_key, [])) - 1, common.MIN_BEAMS_PER_COLUMN)
            for column_key in column_keys
        )
        total_used = sum(trial_used.values())
        score = total_used / total_allowed if total_allowed > 0 else 0.0
        if score > best_score:
            best_score = score
            best_assignments = trial_assignments
            best_used = trial_used

    compact_assignments = {
        column_key: slots
        for column_key, slots in best_assignments.items()
        if slots
    }
    compact_used = {
        column_key: best_used[column_key]
        for column_key in compact_assignments
    }
    return compact_assignments, compact_used


def _profile_is_feasible_exact_fill(
    profile: list[float] | tuple[float, ...],
    available_slot_sizes: SlotSizeSequence = None,
) -> bool:
    """Return True when a candidate profile is an exact legal full-height stack.

    The legal completion is determined by the residual value that is not part of the configured lower
    stack, not by the last list element. This allows valid exact-fill profiles such as
    124-124-124-124-194 to pass while still rejecting non-exact stacks like 234-234-124-104.
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

    if not slots:
        return False

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

    return True


def _profile_generation_priority(profile: list[float]) -> tuple[int, int, int, int, float, float]:
    """Rank candidate profiles by exact-fill usefulness and diversity.

    Profiles that close the 754 cm stack exactly and use a wider spread of relevant slot sizes are
    kept ahead of redundant near-duplicates, so the search remains deterministic and still captures
    the most relevant exact-fill families.
    """
    values = [int(round(float(value))) for value in profile]
    counts = Counter(values)
    total_physical = sum(values) + max(len(values) - 1, 0) * common.BEAM_HEIGHT
    return (
        1 if abs(total_physical - common.MAX_USED_HEIGHT_BASE) <= 1e-9 else 0,
        len(set(values)),
        len(profile),
        sum(counts.values()),
        -abs(total_physical - common.MAX_USED_HEIGHT_BASE),
        max(values, default=0),
    )


def _generate_feasible_rack_profiles(candidate_slot_sizes: SlotSizeSequence) -> list[list[float]]:
    """Generate the full legal rack-profile space for the configured slot family.

    The legal profile set must include mixed lower-stack combinations, not just a narrow exact-fill
    subset. Each candidate is a full-height descending profile whose lower rows use configured slot
    sizes and whose final row is either another configured size or a valid exact topfill completion.
    """
    configured_sizes = sorted(set(_config_size_values(candidate_slot_sizes or [])))
    if not configured_sizes:
        return []

    legal_values = set(_legal_topfill_values(candidate_slot_sizes or []))
    seen: set[tuple[float, ...]] = set()
    ranked_profiles: list[tuple[tuple[int, int, int, int, float, float], list[float]]] = []
    max_lower_rows = min(12, max(2, len(configured_sizes) * 6))

    for lower_count in range(1, max_lower_rows + 1):
        for lower_combo in combinations_with_replacement(configured_sizes, lower_count):
            lower_slots = [float(value) for value in lower_combo]
            if not lower_slots:
                continue

            lower_sum = sum(lower_slots)
            if lower_sum > common.MAX_USED_HEIGHT_BASE - 60.0:
                continue

            support_height = lower_sum + max(len(lower_slots) - 1, 0) * common.BEAM_HEIGHT
            if support_height < 504.0 - 1e-9:
                continue

            for final_value in sorted(set(configured_sizes) | legal_values):
                candidate_total = lower_sum + float(final_value) + max(len(lower_slots), 0) * common.BEAM_HEIGHT
                if abs(candidate_total - common.MAX_USED_HEIGHT_BASE) > 1e-9:
                    continue

                candidate = tuple(sorted(lower_slots, reverse=True) + [float(final_value)])
                if not _profile_is_feasible_exact_fill(list(candidate), candidate_slot_sizes or lower_slots):
                    continue

                if candidate in seen:
                    continue
                seen.add(candidate)
                ranked_profiles.append((_profile_generation_priority(list(candidate)), list(candidate)))

    ranked_profiles.sort(key=lambda item: item[0], reverse=True)
    return [profile for _, profile in ranked_profiles]


def _effective_requirement_slot_size(
    slot_value: float | int,
    profile: list[float] | tuple[float, ...] | None = None,
    available_slot_sizes: SlotSizeSequence = None,
) -> int:
    """Map a legal final topfill to the lower slot family it completes for exact-count accounting."""
    rounded = int(round(float(slot_value)))
    profile_values = [int(round(float(value))) for value in (profile or []) if float(value) > 0.0]
    if len(profile_values) < 2:
        return rounded

    config_values = set(_config_size_values(available_slot_sizes or profile_values))
    if rounded == profile_values[-1] and rounded not in config_values:
        return profile_values[-2]
    return rounded


def _effective_requirement_counts(
    profile: list[float] | tuple[float, ...],
    available_slot_sizes: SlotSizeSequence = None,
) -> dict[int, int]:
    """Count a profile using the underlying lower-slot family for legal final topfills."""
    profile_values = [int(round(float(value))) for value in (profile or []) if float(value) > 0.0]
    if not profile_values:
        return {}

    config_values = set(_config_size_values(available_slot_sizes or profile_values))
    counts: Counter[int] = Counter({size: 0 for size in sorted(config_values)})

    for index, value in enumerate(profile_values):
        effective = value
        if index == len(profile_values) - 1 and len(profile_values) > 1 and value not in config_values:
            effective = profile_values[-2]
        counts[effective] += 1

    return dict(sorted(counts.items()))


def _profile_requirement_priority(
    profile: list[float],
    remaining: dict[float, int],
) -> tuple[int, int, int, int, tuple[int, ...], tuple[int, ...], int, int, int, int]:
    """Rank feasible profiles by exact remaining-deficit coverage.

    The minimum required counts per slot size are a hard gate: profiles that still leave a required
    slot family short must rank below any profile that satisfies the outstanding minimums, even if
    they appear to cover more total locations in aggregate.
    """
    counts = _effective_requirement_counts(profile, list(remaining.keys()))
    ordered_sizes = sorted(
        remaining,
        key=lambda size: (
            int(remaining[size]),
            int(round(float(size))),
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

    full_height_bonus = 1 if _profile_is_feasible_exact_fill(profile) else 0
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

    return (
        minimums_satisfied,
        -unmet_requirements,
        -total_shortage,
        distinct_coverage + scarce_coverage,
        coverage_vector,
        tuple(-value for value in shortage_vector),
        weighted_coverage,
        base_coverage,
        -synthetic_topfill_penalty,
        exact_completion_bonus,
    )


def _rack_from_column_key(column_key: str) -> str:
    """Return the rack label associated with a rack-column key such as H04 or R01C01.

    Simple labels such as A00 are treated as independent rack units so each column can be scored
    against the remaining profile counts instead of being merged into a single rack.
    """
    text = str(column_key).strip()
    if not text:
        return text
    if "C" in text:
        return text.split("C", 1)[0]
    return text


def _build_deficit_coverage_layout(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: SlotSizeSequence,
) -> dict[str, list[float]]:
    """Greedily assign each rack the feasible profile with the highest remaining-deficit coverage ratio.

    The selected profile is shared across the whole rack. Different racks may use different profiles,
    but all columns in the same rack must use the same chosen profile after the rack is scored.
    """
    if not rack_columns:
        return {}

    profiles = _generate_feasible_rack_profiles(config_slot_sizes or list(required_counts.keys()))
    if not profiles:
        return {column_key: [] for column_key in rack_columns}

    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[_rack_from_column_key(column_key)].append(column_key)

    remaining = {float(size): int(count) for size, count in required_counts.items()}
    assignments: dict[str, list[float]] = {}

    for rack in sorted(rack_to_columns):
        columns = sorted(rack_to_columns[rack])
        best_profile: list[float] | None = None
        best_key: tuple[int, int, int, int, tuple[int, ...], tuple[int, ...], int, int, int, int] | None = None

        for profile in profiles:
            candidate_key = _profile_requirement_priority(profile, remaining)
            if best_key is None or candidate_key > best_key:
                best_key = candidate_key
                best_profile = list(profile)

        if best_profile is None:
            best_profile = list(profiles[0])

        remaining_coverage = {float(size): int(count) for size, count in remaining.items()}
        for profile in profiles:
            if profile == best_profile:
                continue
            profile_counts = _effective_requirement_counts(profile, list(required_counts.keys()))
            profile_key = _profile_requirement_priority(profile, remaining)
            if best_key is None or profile_key > best_key:
                best_key = profile_key
                best_profile = list(profile)

        effective_counts = _effective_requirement_counts(best_profile, list(required_counts.keys()))
        for size_int, count in effective_counts.items():
            size_key = next((key for key in remaining if int(round(float(key))) == size_int), None)
            if size_key is None:
                continue
            remaining[size_key] = max(remaining[size_key] - count, 0)
        remaining = {size: count for size, count in remaining.items() if count > 0}

        for column_key in columns:
            assignments[column_key] = list(best_profile)

    for column_key in rack_columns:
        assignments.setdefault(column_key, [])

    return assignments


def _slot_distribution_signature(column_assignments: dict[str, list[float]]) -> str:
    counts: dict[int, int] = defaultdict(int)
    for slots in column_assignments.values():
        for slot_size in slots:
            counts[int(round(slot_size))] += 1
    counts = common._exclude_fixed_layout_slot_counts(counts)
    return "|".join(f"{size}:{count}" for size, count in sorted(counts.items()))


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


def _additional_fill_signature_from_location_rows(
    location_rows: list[dict[str, str]],
    minimum_required_counts_signature: str,
) -> str:
    layout_counts: dict[int, int] = defaultdict(int)
    for row in location_rows:
        slot_size = common._to_float(row.get("Assigned_Slot_Size_cm"))
        if slot_size is None:
            continue
        layout_counts[int(round(slot_size))] += 1

    minimum_counts = _parse_count_signature(minimum_required_counts_signature)
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


def _enforce_segment_uniform_slot_profiles(
    column_assignments: dict[str, list[float]],
    segments: set[tuple[str, int, int]],
    layout_thresholds_by_rack: dict[str, tuple[int, float]] | None = None,
) -> dict[str, list[float]]:
    """Force every column in the same beam segment to share one exact profile.

    Stage 6 output is only valid when a segment is treated as a single physical block:
    two columns in the same segment cannot end up with different slot-size profiles.
    """
    if not column_assignments:
        return {}

    normalized: dict[str, list[float]] = {
        column_key: sorted(float(value) for value in slots if float(value) > 0.0)
        for column_key, slots in column_assignments.items()
    }

    if not segments:
        segment_map: dict[str, tuple[str, int, int]] = {}
        for column_key, slots in normalized.items():
            if not slots:
                continue
            rack = _rack_from_column_key(column_key)
            textual = column_key[len(rack):]
            try:
                column_number = int(textual)
            except ValueError:
                continue
            if column_number <= 3:
                segment_start, segment_end = 0, 3
            else:
                segment_start = ((column_number - 1) // 3) * 3
                segment_end = min(segment_start + 2, max(column_number, segment_start + 2))
            segment_map[column_key] = (rack, segment_start, segment_end)
        segments = {
            (rack, c0, c1)
            for (rack, c0, c1) in segment_map.values()
        }

    segment_columns: dict[tuple[str, int, int], list[str]] = defaultdict(list)
    for column_key in normalized:
        rack = _rack_from_column_key(column_key)
        textual = column_key[len(rack):]
        try:
            column_number = int(textual)
        except ValueError:
            continue
        segment_match = None
        for start, end in [(segment[1], segment[2]) for segment in segments if segment[0] == rack]:
            if start <= column_number <= end:
                segment_match = (rack, start, end)
                break
        if segment_match is not None:
            segment_columns[segment_match].append(column_key)

    for segment_key, keys in segment_columns.items():
        if len(keys) <= 1:
            continue
        profile_votes: Counter[tuple[int, ...]] = Counter()
        for key in keys:
            slots = normalized.get(key, [])
            if not slots:
                continue
            profile_votes[tuple(int(round(float(value))) for value in slots)] += 1
        if not profile_votes:
            continue
        chosen_profile = max(
            profile_votes.items(),
            key=lambda item: (item[1], sum(item[0]), len(item[0]), item[0][-1]),
        )[0]
        chosen_list = [float(value) for value in chosen_profile]
        for key in keys:
            normalized[key] = chosen_list

    return normalized


def _is_legal_config_slot_size(
    value: float | int,
    minimum_slot_size: float | int | None = None,
) -> bool:
    rounded = int(round(float(value)))
    if rounded <= 0:
        return False
    if rounded > int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)):
        return False
    if minimum_slot_size is not None:
        minimum_value = int(round(float(minimum_slot_size)))
        if rounded < minimum_value:
            return False
    return rounded % 10 in (4, 9)


def _slot_size_is_allowed_for_configuration(slot_size: float | int, config_slot_sizes: SlotSizeSequence) -> bool:
    """Each slot must be an actual configured representative size for the current configuration."""
    rounded = int(round(float(slot_size)))
    if rounded <= 0:
        return False
    if rounded > int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)):
        return False
    if not config_slot_sizes:
        return rounded % 10 in (4, 9)
    config_values = {int(round(float(size))) for size in config_slot_sizes if float(size) > 0.0}
    return rounded in config_values and rounded % 10 in (4, 9)


def _config_size_values(available_slot_sizes: SlotSizeSequence) -> set[int]:
    """Return only the exact configured representative sizes for the current configuration."""
    if not available_slot_sizes:
        return set()
    return {
        int(round(float(size)))
        for size in available_slot_sizes
        if float(size) > 0.0
        and int(round(float(size))) <= int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM))
        and int(round(float(size))) % 10 in (4, 9)
    }


def _legal_topfill_values(available_slot_sizes: SlotSizeSequence) -> set[int]:
    """Return legal final residual completions for any valid lower stack, including mixed setups.

    A topfill is legal when it exactly completes a physically valid lower stack to the 754 cm rack
    height, regardless of whether the lower stack is a single repeated family size or a mixed set of
    configured slot sizes. This intentionally keeps only true completion values and excludes synthetic
    residuals that do not correspond to an exact stack fit.
    """
    config_values = sorted(_config_size_values(available_slot_sizes))
    if not config_values:
        return set()

    legal_values: set[int] = set()
    max_lower_rows = min(12, max(2, len(config_values) * 6))
    for lower_count in range(1, max_lower_rows + 1):
        for lower_combo in product(config_values, repeat=lower_count):
            lower_slots = [float(value) for value in lower_combo]
            if not lower_slots:
                continue
            support_height = sum(lower_slots) + max(len(lower_slots) - 1, 0) * common.BEAM_HEIGHT
            if support_height < 504.0 - 1e-9:
                continue

            final_value = common.MAX_USED_HEIGHT_BASE - sum(lower_slots) - common.BEAM_HEIGHT * len(lower_slots)
            if final_value <= 0.0:
                continue
            if final_value > 214.0:
                continue
            rounded = int(round(final_value))
            if rounded < min(config_values):
                continue
            if rounded % 10 not in (4, 9):
                continue
            legal_values.add(rounded)
    return legal_values


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


def _column_top_slot_in_physical_order(column_slots: list[float]) -> float | None:
    """Return the slot occupying the highest physical position in the current column order."""
    slots = [float(value) for value in (column_slots or []) if float(value) > 0.0]
    if not slots:
        return None
    return float(slots[-1])


def _column_support_band_is_valid(column_slots: list[float]) -> bool:
    """Return True when the support beam beneath the top slot clears the minimum legal band."""
    slots = [float(value) for value in (column_slots or []) if float(value) > 0.0]
    if len(slots) <= 1:
        return False
    support_below_top = sum(slots[:-1]) + common.BEAM_HEIGHT * max(len(slots) - 2, 0)
    return support_below_top >= 504.0 - 1e-9


def _beam_height_below_top_row(column_slots: list[float]) -> float:
    """Return the physical support height below the current top row, including beam gaps."""
    slots = [float(value) for value in (column_slots or []) if float(value) > 0.0]
    if len(slots) <= 1:
        return 0.0
    below_top = slots[:-1]
    return sum(below_top) + max(len(below_top) - 1, 0) * common.BEAM_HEIGHT


def _column_can_topfill_to_limit(
    column_slots: list[float],
    available_slot_sizes: SlotSizeSequence = None,
) -> bool:
    """Return True when the column is compatible with the legal family and can be topped to a valid final slot.

    A partial column is still valid if every non-final row uses a configured size and the final row is
    either a configured value or a legal topfill completion. The check intentionally does not reject
    a valid underfilled column merely because it has not reached the full 754 cm nominal height yet.
    """
    slots = [float(value) for value in (column_slots or []) if float(value) > 0.0]
    if not slots:
        return False

    config_values = set(_config_size_values(available_slot_sizes or slots))
    legal_values = set(_legal_topfill_values(available_slot_sizes or slots))
    if not config_values and not legal_values:
        config_values = {int(round(float(slot))) for slot in slots if float(slot) > 0.0}
        legal_values = set(config_values)

    for value in slots:
        rounded = int(round(float(value)))
        if rounded <= 0 or rounded > int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)):
            return False
        if rounded % 10 not in (4, 9):
            return False

    if len(slots) == 1:
        final_value = int(round(float(slots[-1])))
        return final_value in config_values or final_value in legal_values

    lower_values = slots[:-1]
    if any(int(round(float(value))) not in config_values for value in lower_values):
        return False

    final_value = int(round(float(slots[-1])))
    if final_value in config_values:
        return True
    if final_value not in legal_values:
        return False

    physical_total = sum(slots) + max(len(slots) - 1, 0) * common.BEAM_HEIGHT
    if abs(physical_total - common.MAX_USED_HEIGHT_BASE) <= 1e-9:
        required_completion = (
            common.MAX_USED_HEIGHT_BASE
            - sum(lower_values)
            - common.BEAM_HEIGHT * len(lower_values)
        )
        if required_completion <= 0.0:
            return False
        return abs(float(final_value) - float(required_completion)) <= 1e-9

    return True


def _rack_profiles_are_exactly_uniform(
    column_assignments: dict[str, list[float]],
    column_keys: list[str],
) -> bool:
    """Return True when each rack uses exactly one profile across all of its columns."""
    profiles_by_rack: dict[str, set[tuple[int, ...]]] = defaultdict(set)
    for column_key in column_keys:
        slots = [float(value) for value in column_assignments.get(column_key, []) if float(value) > 0.0]
        if not slots:
            return False
        canonical = tuple(sorted(int(round(float(value))) for value in slots))
        rack_key = _rack_from_column_key(column_key)
        profiles_by_rack[rack_key].add(canonical)
    return all(len(canonical_profiles) == 1 for canonical_profiles in profiles_by_rack.values())


def _minimum_required_counts_are_satisfied(
    column_assignments: dict[str, list[float]],
    column_keys: list[str],
    available_slot_sizes: SlotSizeSequence = None,
    minimum_required_counts: dict[float, int] | None = None,
) -> bool:
    """Return True when the exact effective count accounting satisfies the minimum per-size requirements."""
    if minimum_required_counts is None:
        return True

    layout_counts: dict[int, int] = defaultdict(int)
    for column_key in column_keys:
        slots = [float(value) for value in column_assignments.get(column_key, []) if float(value) > 0.0]
        if not slots:
            return False
        for idx, slot_size in enumerate(slots):
            rounded = int(round(float(slot_size)))
            counted_as = _effective_requirement_slot_size(rounded, slots, available_slot_sizes)
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

    The total occupied-location minimum is a full-layout gate, not a per-column legality rule.
    Small unit tests and partial assignments can still be valid physically even before the
    complete layout reaches the 890-location target.
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
    for column_key in column_keys:
        slots = [float(value) for value in column_assignments.get(column_key, []) if float(value) > 0.0]
        if not slots:
            return False
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
            layout_counts[counted_as] += 1

    if not _rack_profiles_are_exactly_uniform(column_assignments, column_keys):
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
            column_keys,
            available_slot_sizes=available_slot_sizes,
            minimum_required_counts=minimum_required_counts,
        )
    ):
        return False

    if enforce_minimum_total_locations and assigned_locations_total < common._explicit_occupied_target_total():
        return False

    return True


def _candidate_fill_pool(
    available_slot_sizes: list[float] | None = None,
    maximum_slot_size: float = common.MAX_REPRESENTATIVE_SLOT_SIZE_CM,
) -> list[int]:
    preferred = {
        int(round(float(size)))
        for size in (available_slot_sizes or [])
        if float(size) > 0.0 and float(size) <= float(maximum_slot_size)
    }
    if not preferred:
        preferred = {int(float(maximum_slot_size))}
    return sorted(preferred)


def _config_legal_slot_family(available_slot_sizes: SlotSizeSequence) -> list[int]:
    """Allow only the exact configured legal representative sizes, never nearby synthetic values."""
    return _exact_config_slot_family(available_slot_sizes)


def _legal_filler_candidates(minimum_slot_size: float, candidate_pool: list[float] | None = None) -> list[float]:
    minimum_value = int(round(float(minimum_slot_size)))
    source = list(candidate_pool or [])
    floor = max(minimum_value - 30, 4)
    candidates = sorted({
        float(size)
        for size in source
        if float(size) >= floor and int(round(float(size))) % 10 in (4, 9)
    })
    if candidates:
        return candidates

    legal_floor = floor
    while legal_floor <= int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)):
        if legal_floor % 10 in (4, 9):
            return [float(legal_floor)]
        legal_floor += 1
    return []


def _column_physical_height_usage(column_slots: list[float]) -> float:
    """Return the full physical column usage including beam gap height."""
    slots = [float(value) for value in (column_slots or []) if float(value) > 0.0]
    if not slots:
        return 0.0
    return sum(slots) + max(len(slots) - 1, 0) * common.BEAM_HEIGHT


def _layout_physical_height_metrics(column_assignments: dict[str, list[float]]) -> tuple[dict[str, float], float, float]:
    """Compute a physically consistent used-height and budget summary for a layout."""
    used_by_column = {
        column_key: _column_physical_height_usage(slots)
        for column_key, slots in column_assignments.items()
    }
    allowed_total = len(column_assignments) * common.MAX_USED_HEIGHT_BASE
    total_used = sum(used_by_column.values())
    utilization = (total_used / allowed_total) if allowed_total > 0 else 0.0
    return used_by_column, allowed_total, utilization


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


def _exact_fill_to_column_limit(
    slots: list[float],
    available_slot_sizes: SlotSizeSequence = None,
    target_row_count: int | None = None,
) -> list[float] | None:
    """Find a legal column fill that exactly reaches the physical limit without synthetic 1 cm fillers.

    The fill is built from eligible slot sizes only (original configuration sizes, their nearby legal
    alternatives, and bounded values up to 234 cm). This keeps the rack-row consistency repair fast
    while honoring the physical constraints.
    """
    normalized = [float(value) for value in slots if float(value) > 0.0]
    if not normalized and target_row_count is None:
        return []

    target_rows = max(int(target_row_count) if target_row_count is not None else len(normalized), 1)
    if len(normalized) > target_rows:
        normalized = normalized[:target_rows]

    if len(normalized) > target_rows:
        return None

    beam_count = max(target_rows - 1, common.MIN_BEAMS_PER_COLUMN)
    target_slot_sum = common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * beam_count
    current_slot_sum = sum(normalized)
    required_fill = target_slot_sum - current_slot_sum

    if abs(required_fill) <= 1e-9:
        if len(normalized) != target_rows:
            return None
        return sorted(normalized, reverse=True)

    if required_fill < 0.0:
        return None

    legal_family = sorted({
        int(round(float(value)))
        for value in (available_slot_sizes or normalized)
        if float(value) > 0.0
        and int(round(float(value))) <= int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM))
        and int(round(float(value))) % 10 in (4, 9)
    })
    if len(normalized) == target_rows and normalized:
        top_value = max(int(round(float(normalized[-1]))), 4)
        for candidate_value in legal_family:
            if candidate_value < top_value:
                continue
            trial = [float(value) for value in normalized[:-1]] + [float(candidate_value)]
            if len(trial) != target_rows:
                continue
            trial_total = sum(trial) + (len(trial) - 1) * common.BEAM_HEIGHT
            if abs(trial_total - common.MAX_USED_HEIGHT_BASE) <= 1e-9 and _column_support_band_is_valid(trial):
                return sorted(trial, reverse=True)

    max_slot = float(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)
    minimum_allowed = min(
        (float(value) for value in (available_slot_sizes or normalized) if float(value) > 0.0),
        default=0.0,
    )
    base_pool = _config_legal_slot_family(available_slot_sizes or normalized)
    if not base_pool:
        base_pool = sorted({
            int(round(float(size)))
            for size in (available_slot_sizes or normalized)
            if float(size) > 0.0 and float(size) <= max_slot and int(round(float(size))) % 10 in (4, 9)
        })

    if not base_pool:
        return None

    legal_pool = sorted(
        (
            value
            for value in base_pool
            if value > 0
            and value <= int(max_slot)
            and value >= max(int(round(minimum_allowed)) - 30, 4)
            and int(round(value)) % 10 in (4, 9)
        ),
        reverse=True,
    )
    if not legal_pool:
        return None

    needed_count = max(target_rows - len(normalized), 0)
    if needed_count == 0:
        return normalized if abs(required_fill) <= 1e-9 else None

    required_int = int(round(required_fill))
    min_possible = needed_count * min(legal_pool)
    max_possible = needed_count * max(legal_pool)
    if required_int < min_possible or required_int > max_possible:
        return None

    @lru_cache(maxsize=None)
    def can_make(remaining: int, slots_left: int) -> bool:
        if slots_left == 0:
            return remaining == 0
        if remaining < 0:
            return False
        for size in legal_pool:
            if size > remaining:
                continue
            if can_make(remaining - size, slots_left - 1):
                return True
        return False

    @lru_cache(maxsize=None)
    def build(remaining: int, slots_left: int) -> tuple[int, ...] | None:
        if slots_left == 0:
            return () if remaining == 0 else None
        if remaining < 0:
            return None
        for size in legal_pool:
            if size > remaining:
                continue
            remainder = build(remaining - size, slots_left - 1)
            if remainder is not None:
                return (size,) + remainder
        return None

    if not can_make(required_int, needed_count):
        return None

    fill = [float(value) for value in build(required_int, needed_count) or ()]
    fill = sorted(fill)
    if len(fill) != needed_count:
        return None
    if any(value > max_slot + 1e-9 for value in fill):
        return None
    if fill and max(normalized, default=0.0) > max(fill) + 1e-9:
        return None
    candidate = sorted(normalized + fill, reverse=True)
    if candidate and max(candidate[:-1], default=0.0) > candidate[-1] + 1e-9:
        return None
    return candidate


def _build_legal_row_count_column(
    slots: list[float],
    available_slot_sizes: SlotSizeSequence,
    target_row_count: int,
    minimum_slot_size: float | None = None,
    target_slot_sum: float | int | None = None,
) -> list[float] | None:
    """Rebuild a column at a legal row count from the allowed legal slot family.

    This handles both the common fill-up case and the overfull-column repair case:
    if a column is already taller than the target row count, the function can
    select a different legal combination of size target_row_count that is within
    the physical 754 cm limit while maintaining the 504-520 support-band rule.
    """
    if target_row_count <= 0:
        return None

    legal_pool = sorted(set(_exact_config_slot_family(available_slot_sizes or slots or [])))
    if not legal_pool:
        legal_pool = sorted({
            int(round(float(size)))
            for size in (slots or [])
            if float(size) > 0.0
            and int(round(float(size))) <= int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM))
            and int(round(float(size))) % 10 in (4, 9)
        })
    if not legal_pool:
        if minimum_slot_size is not None:
            legal_pool = [int(round(float(minimum_slot_size)))]
        else:
            legal_pool = [int(round(float(max(slots, default=0.0) or 1.0)))]

    lower_bound = min(legal_pool)
    if minimum_slot_size is not None:
        lower_bound = max(lower_bound, int(round(float(minimum_slot_size))))
    legal_pool = [size for size in legal_pool if size >= lower_bound and size <= int(round(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM))]
    legal_pool = sorted(legal_pool, reverse=True)
    if not legal_pool:
        return None

    target_sum_value = float(target_slot_sum) if target_slot_sum is not None else (
        common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(target_row_count - 1, common.MIN_BEAMS_PER_COLUMN)
    )
    target_int = int(round(target_sum_value))

    @lru_cache(maxsize=None)
    def can_make(remaining: int, slots_left: int) -> bool:
        if slots_left == 0:
            return remaining == 0
        if remaining < 0:
            return False
        for size in legal_pool:
            if size > remaining:
                continue
            if can_make(remaining - size, slots_left - 1):
                return True
        return False

    @lru_cache(maxsize=None)
    def build(remaining: int, slots_left: int) -> tuple[int, ...] | None:
        if slots_left == 0:
            return () if remaining == 0 else None
        if remaining < 0:
            return None
        for size in legal_pool:
            if size > remaining:
                continue
            remainder = build(remaining - size, slots_left - 1)
            if remainder is not None:
                return (size,) + remainder
        return None

    if not can_make(target_int, target_row_count):
        return None
    exact_combo = build(target_int, target_row_count)
    if exact_combo is None:
        return None
    candidate = [float(value) for value in sorted(exact_combo)]
    if not _column_support_band_is_valid(candidate):
        return None
    return candidate


def _build_uniform_rack_columns(
    column_assignments: dict[str, list[float]],
    available_slot_sizes: SlotSizeSequence = None,
    minimum_required_counts: dict[float, int] | None = None,
) -> dict[str, list[float]]:
    """Repair each column to a legal physical profile while enforcing one exact profile per rack."""
    if not column_assignments:
        return {}

    adjusted: dict[str, list[float]] = {
        column_key: [float(value) for value in slots if float(value) > 0.0]
        for column_key, slots in column_assignments.items()
    }

    if available_slot_sizes:
        minimum_slot = min(float(value) for value in available_slot_sizes if float(value) > 0.0)
    else:
        minimum_slot = min(
            (
                float(value)
                for column_key in adjusted
                for value in adjusted.get(column_key, [])
                if float(value) > 0.0
            ),
            default=1.0,
        )

    rack_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in adjusted:
        rack_columns[_rack_from_column_key(column_key)].append(column_key)

    for rack_key, rack_keys in rack_columns.items():
        rack_profiles: list[tuple[int, ...]] = []
        for column_key in rack_keys:
            cleaned = sorted(float(value) for value in adjusted.get(column_key, []) if float(value) > 0.0)
            if not cleaned:
                continue
            rack_profiles.append(tuple(int(round(float(value))) for value in cleaned))

        if len(rack_profiles) <= 1:
            continue

        shared_profile = max(
            set(rack_profiles),
            key=lambda profile: (sum(profile), len(profile), profile[0] if profile else 0),
        )
        for column_key in rack_keys:
            adjusted[column_key] = [float(value) for value in shared_profile]

    for column_key, slots in list(adjusted.items()):
        cleaned = sorted(float(value) for value in slots if float(value) > 0.0)
        if not cleaned:
            adjusted[column_key] = []
            continue

        rack_key = _rack_from_column_key(column_key)
        rack_row_counts = [len([val for val in adjusted.get(key, []) if float(val) > 0.0]) for key in rack_columns.get(rack_key, [])]
        dominant_row_count = max(rack_row_counts, default=len(cleaned))
        if dominant_row_count < 4 and len(cleaned) < dominant_row_count:
            adjusted[column_key] = sorted(cleaned)
            continue

        if rack_row_counts and max(rack_row_counts) - min(rack_row_counts) > 1 and len(cleaned) < max(rack_row_counts):
            adjusted[column_key] = sorted(cleaned)
            continue

        legal_base = [
            float(value)
            for value in cleaned
            if float(value) >= float(minimum_slot)
            and int(round(float(value))) % 10 in (4, 9)
        ]
        repair_source = legal_base if legal_base else [float(minimum_slot)]

        candidate: list[float] | None = None
        for target_rows in range(max(len(repair_source), 1), 13):
            trial = _exact_fill_to_column_limit(
                repair_source,
                available_slot_sizes=available_slot_sizes,
                target_row_count=target_rows,
            )
            if trial is None:
                continue
            if _layout_assignments_are_feasible(
                {column_key: list(trial)},
                [column_key],
                minimum_slot,
                available_slot_sizes,
                minimum_required_counts=minimum_required_counts,
            ) and abs(sum(trial) + (len(trial) - 1) * common.BEAM_HEIGHT - common.MAX_USED_HEIGHT_BASE) <= 1e-6:
                candidate = [float(value) for value in trial]
                break

        adjusted[column_key] = sorted(candidate) if candidate is not None else sorted(cleaned)

    for rack_key, rack_keys in rack_columns.items():
        rack_profile_set = {
            tuple(int(round(float(value))) for value in adjusted.get(column_key, []))
            for column_key in rack_keys
            if adjusted.get(column_key)
        }
        if len(rack_profile_set) > 1:
            chosen = sorted(rack_profile_set, key=lambda profile: (sum(profile), len(profile), profile[-1]))[-1]
            for column_key in rack_keys:
                adjusted[column_key] = [float(value) for value in chosen]

    return adjusted


def _enforce_rack_row_count_consistency(
    column_assignments: dict[str, list[float]],
    available_slot_sizes: SlotSizeSequence = None,
    minimum_required_counts: dict[float, int] | None = None,
) -> dict[str, list[float]]:
    """Backward-compatible wrapper around the strict uniform-rack repair rule."""
    return _build_uniform_rack_columns(
        column_assignments,
        available_slot_sizes=available_slot_sizes,
        minimum_required_counts=minimum_required_counts,
    )


def _cumulative_coverage_signature(column_assignments: dict[str, list[float]]) -> str:
    exact_counts: dict[int, int] = defaultdict(int)
    for slots in column_assignments.values():
        for slot_size in slots:
            exact_counts[int(round(slot_size))] += 1

    running = 0
    cumulative: dict[int, int] = {}
    for size in sorted(exact_counts.keys(), reverse=True):
        running += exact_counts[size]
        cumulative[size] = running

    return "|".join(f"{size}:{cumulative[size]}" for size in sorted(cumulative.keys()))


def _effective_slot_size_for_summary(slot_size: float, column_slots: list[float], available_slot_sizes: SlotSizeSequence = None) -> int:
    """Treat legal final topfill values as the underlying configured slot family for reporting."""
    rounded = int(round(float(slot_size)))
    if not column_slots:
        return rounded
    config_values = set(_config_size_values(available_slot_sizes or column_slots))
    if rounded in config_values:
        return rounded
    profile_values = [int(round(float(value))) for value in column_slots if float(value) > 0.0]
    if len(profile_values) > 1 and rounded == profile_values[-1] and rounded not in config_values:
        if profile_values[-2] in config_values:
            return profile_values[-2]
    return rounded


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
            exact_counts[effective] += 1

    distribution = "|".join(f"{size}:{count}" for size, count in sorted(exact_counts.items()))

    running = 0
    cumulative: dict[int, int] = {}
    for size in sorted(exact_counts.keys(), reverse=True):
        running += exact_counts[size]
        cumulative[size] = running
    cumulative_signature = "|".join(f"{size}:{cumulative[size]}" for size in sorted(cumulative.keys()))

    return distribution, cumulative_signature


def _enforce_min_locations_per_column(
    column_assignments: dict[str, list[float]],
    column_keys: list[str],
    min_slot_size: float,
) -> dict[str, list[float]]:
    # Final safety net: every column must have at least MIN_LOCATIONS_PER_COLUMN
    # while staying within per-column allowed height. Any existing illegal filler
    # values are removed before padding so the column never reintroduces a size
    # smaller than the configuration minimum or a non-legal 4/9 ending.
    minimum_locations = max(int(common.MIN_LOCATIONS_PER_COLUMN), 1)
    adjusted: dict[str, list[float]] = {
        key: [float(value) for value in column_assignments.get(key, [])]
        for key in column_keys
    }

    legal_candidates = _legal_filler_candidates(min_slot_size, [float(min_slot_size)])
    if not legal_candidates:
        return {key: [] for key in column_keys}

    for column_key in column_keys:
        slots = [
            float(value)
            for value in adjusted.get(column_key, [])
            if float(value) > 0.0
            and int(round(float(value))) >= int(round(float(min_slot_size)))
            and int(round(float(value))) % 10 in (4, 9)
        ]
        adjusted[column_key] = slots

        while len(slots) < minimum_locations:
            next_count = len(slots) + 1
            allowed_after = common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(next_count - 1, common.MIN_BEAMS_PER_COLUMN)
            remaining = allowed_after - sum(slots)
            chosen = None
            for candidate in legal_candidates:
                if float(candidate) <= remaining + 1e-9 and float(candidate) >= float(min_slot_size):
                    chosen = float(candidate)
                    break
            if chosen is None:
                break
            slots.append(chosen)
        adjusted[column_key] = slots

    return {key: values for key, values in adjusted.items() if values}
def _clone_column_assignments(column_assignments: dict[str, list[float]]) -> dict[str, list[float]]:
    return {key: [float(value) for value in values] for key, values in column_assignments.items()}


def _assignment_cache_key(column_assignments: dict[str, list[float]]) -> tuple[tuple[str, tuple[float, ...]], ...]:
    return tuple(
        (column_key, tuple(float(value) for value in slots))
        for column_key, slots in sorted(column_assignments.items())
    )



def _pre_robust_sort_key(summary_row: dict[str, str]) -> tuple[int, int, int, float, int, int]:
    feasible_penalty = 0 if str(summary_row.get("Layout_Feasible", "")).strip().upper() == "YES" else 1
    additional_beams = common._to_int_default(summary_row.get("Additional_Beams_Required"), 0)
    return (
        feasible_penalty,
        common._to_int_default(summary_row.get("Beam_Relocations_Total"), 0),
        additional_beams,
        common._to_float(summary_row.get("Space_Left")) or 0.0,
        -common._to_int_default(summary_row.get("Assigned_Locations_Total"), 0),
        _style_rank(str(summary_row.get("Style", ""))),
    )


def _percent_diff(value_a: float, value_b: float) -> float:
    scale = max(abs(value_a), abs(value_b), 1e-9)
    return abs(value_a - value_b) / scale


def _kpi_winner(util_value: float, reloc_value: float, higher_is_better: bool) -> str:
    if abs(util_value - reloc_value) <= 1e-9:
        return "tie"
    if higher_is_better:
        return "utilization" if util_value > reloc_value else "relocation"
    return "utilization" if util_value < reloc_value else "relocation"


def build_layout_generation() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Generate candidate layouts and emit summary, column, and location-level outputs."""
    print(f"[Stage 6] starting layout generation for configs from {INPUT_CONFIG_FILE.name} and {INPUT_CAPACITY_FILE.name}")
    prepared_rows = _read_csv(INPUT_PREPARED)
    raw_configs = _candidate_configs()
    configs = _candidate_configs_for_exhaustive_search(raw_configs)
    if not configs:
        configs = raw_configs
    capacity_rows = _capacity_rows_by_config()
    ignore_layout_layout = common._should_ignore_layout_for_layout_generation()
    layout_thresholds_by_rack: dict[str, tuple[int, float]] = {}
    fixed_layout_slot_by_column: dict[str, float] = {}
    layout_columns = common._build_layout_columns(prepared_rows)
    total_physical_locations = len(
        [
            row
            for row in prepared_rows
            if str(row.get("Location Type", "")).strip().lower() != "layout"
            and not common._is_split_location(str(row.get("Location", "")).strip())
        ]
    )

    config_style_bundles: list[ConfigStyleBundle] = []
    print(f"[Stage 6] shortlisted {len(configs)} configs for exact-fill evaluation")

    candidate_layout_rows: list[dict[str, str]] = []
    candidate_layout_location_rows: list[dict[str, str]] = []
    candidate_layout_column_rows: list[dict[str, str]] = []
    candidate_layout_location_rows_all: list[dict[str, str]] = []
    candidate_layout_column_rows_all: list[dict[str, str]] = []
    # Build layout variants for every shortlisted config and layout style.
    layout_counter = 1
    for config in configs:
        config_id = str(config.get("Config_ID", "")).strip()
        rows = capacity_rows.get(config_id, [])
        if not rows:
            continue

        base_exact_counts = _base_exact_counts(rows)
        if not base_exact_counts:
            continue

        print(f"[Stage 6] config {config_id}: base counts = {[f'{int(size)}:{count}' for size, count in sorted(base_exact_counts.items())]}")
        config_slot_sizes = _slot_sizes_from_capacity(rows)
        style_candidates: list[StyleCandidate] = []
        style = IMPLEMENTATION_STYLE
        layout_id = f"LAY_{layout_counter:03d}"
        layout_counter += 1

        # The greedy deficit-coverage rule is the actual assignment decision in Stage 6.
        # All later repair and local-search passes are disabled here so they cannot rewrite
        # the selected rack profile away from the best remaining-deficit choice.
        print(f"[Stage 6] config {config_id}: building rack profiles from slot family {config_slot_sizes}")
        column_assignments = _build_deficit_coverage_layout(
            rack_columns=layout_columns,
            required_counts={float(size): int(count) for size, count in base_exact_counts.items()},
            config_slot_sizes=config_slot_sizes,
        )
        segment_bounds = set()
        for rack in sorted({str(column_key[0]) for column_key in column_assignments}):
            rack_cols = sorted(
                int(str(column_key)[len(rack):])
                for column_key in column_assignments
                if str(column_key).startswith(rack) and str(column_key)[len(rack):].isdigit()
            )
            if not rack_cols:
                continue
            max_col = max(rack_cols)
            for c0, c1 in _segment_bounds(max_col):
                segment_bounds.add((rack, c0, c1))
        column_assignments = _enforce_segment_uniform_slot_profiles(column_assignments, segment_bounds)
        print(f"[Stage 6] config {config_id}: assigned profiles to {len(column_assignments)} rack columns")
        used_by_column = {
            column_key: _column_physical_height_usage(slots)
            for column_key, slots in column_assignments.items()
        }

        expansion_slot_sizes = sorted(float(slot_size) for slot_size in base_exact_counts)
        layout_alignment_conversions = 0
        smallest_config_slot = min(expansion_slot_sizes) if expansion_slot_sizes else 0.0

        # Baseline Stage 6 does not rebuild the physical beam layout or run local-search improvement.
        # Those are intentionally deferred to a separate improvement heuristic after the baseline
        # assignment logic is working and validated.
        generated_location_rows = [
            {
                "Config_ID": config_id,
                "Location": f"{rack}{column:02d}",
                "Rack": rack,
                "Column": str(column),
                "Row": str(row_index + 1),
                "Beam_Coordinate": "",
                "Beam_Height_Range_cm": "",
                "Assigned_Slot_Size_cm": f"{float(slot_size):.3f}",
                "Usable_Location": "YES",
            }
            for rack_column, slots in sorted(column_assignments.items())
            for row_index, slot_size in enumerate(slots)
            for rack, column in [(str(rack_column).rstrip("0123456789"), int(str(rack_column)[-2:]))]
        ]
        layout_slot_distribution, layout_slot_cumulative = _slot_signatures_from_location_rows(
            generated_location_rows,
            available_slot_sizes=expansion_slot_sizes,
        )
        layout_signature = _layout_signature(generated_location_rows)
        relocation_total = 0
        relocation_by_column = {key: 0 for key in column_assignments}
        removed_by_column = {key: 0 for key in column_assignments}
        added_by_column = {key: 0 for key in column_assignments}
        required_beams = 0
        required_grids = 0
        additional_beams = 0
        additional_grids = 0

        # Compute utilization and implementation-effort KPIs per configuration.
        assigned_total = max(len(generated_location_rows) - common._fixed_layout_location_total(), 0)
        layout_physical_locations = assigned_total
        required_locations_total = sum(base_exact_counts.values())
        physical_used_by_column = {
            column_key: _column_physical_height_usage(slots)
            for column_key, slots in column_assignments.items()
        }
        total_used_height = sum(physical_used_by_column.values())
        total_allowed_height = len(layout_columns) * common.MAX_USED_HEIGHT_BASE
        space_utilization = (total_used_height / total_allowed_height) if total_allowed_height > 0 else 0.0
        capacity_margin = assigned_total - required_locations_total
        required_beam_moves = relocation_total
        pct_rack_height_used = space_utilization * 100.0
        space_left = sum(
            max(common.MAX_USED_HEIGHT_BASE - physical_used_by_column.get(column_key, 0.0), 0.0)
            for column_key in layout_columns
        )

        # Final feasibility must reflect the repaired layout itself, not the stale
        # signal from the initial allocator. The layout can become feasible only
        # after repair and local search, so the export gate must validate the
        # final assignment state rather than rejecting everything that was not
        # initially marked feasible.
        final_layout_feasible = (
            assigned_total >= common._explicit_occupied_target_total()
            and capacity_margin >= 0
            and space_utilization <= 1.0
            and _layout_assignments_are_feasible(
                column_assignments,
                layout_columns,
                float(smallest_config_slot),
                expansion_slot_sizes,
                minimum_required_counts=base_exact_counts,
                enforce_minimum_total_locations=True,
            )
        )
        feasible_layout = final_layout_feasible
        allocation_diagnostics = {
            "Feasible_Columns_Considered_Total": float(len(column_assignments)),
            "Feasible_Columns_Min": float(min(len(column_assignments), 1)),
            "Feasible_Columns_Max": float(len(column_assignments)),
            "Feasible_Columns_Average": float(len(column_assignments) / max(len(layout_columns), 1)),
        }

        summary_row = {
                "Layout_ID": layout_id,
                "Config_ID": config_id,
                "Layout_Feasible": "YES" if final_layout_feasible else "NO",
                "Allocation_Feasible_Initial": "YES" if feasible_layout else "NO",
                "Required_Locations_Total": str(required_locations_total),
                "Total_Locations": str(assigned_total),
                "Capacity_Margin": str(capacity_margin),
                "Assigned_Used_Height_Total": f"{total_used_height:.3f}",
                "Total_Allowed_Height": f"{total_allowed_height:.3f}",
                "Space_Left": f"{space_left:.3f}",
                "Beam_Relocations_Total": "0",
                "Beam_Relocations_Before_Local_Search": "0",
                "Beam_Relocations_After_Local_Search": "0",
                "Local_Search_Accepted_Moves": "0",
                "Initial_Beams_Total": "0",
                "Required_Beams_Total": str(required_beams),
                "Additional_Beams_Required": str(additional_beams),
                "Initial_Grids_Total": "0",
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
                "Feasible_Columns_Considered_Total": str(int(allocation_diagnostics.get("Feasible_Columns_Considered_Total", 0.0))),
                "Feasible_Columns_Min": str(int(allocation_diagnostics.get("Feasible_Columns_Min", 0.0))),
                "Feasible_Columns_Max": str(int(allocation_diagnostics.get("Feasible_Columns_Max", 0.0))),
                "Feasible_Columns_Average": f"{allocation_diagnostics.get('Feasible_Columns_Average', 0.0):.3f}",
            }

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

        style_candidates.append((summary_row, generated_location_rows, style_column_rows, layout_signature))

        if not style_candidates:
            continue

        for summary, _loc, _col, _signature in style_candidates:
            summary["Pre_Robustness_Status"] = "PENDING"
            summary["Pre_Robustness_Rank"] = ""
            summary["Pre_Robustness_Prune_Reason"] = ""

        config_style_bundles.append((config_id, style_candidates))

    if config_style_bundles:
        def _bundle_score(bundle: ConfigStyleBundle) -> tuple[int, int, int, float, int, int]:
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
                if config_id in selected_config_ids:
                    summary["Pre_Robustness_Status"] = "SELECTED"
                    summary["Pre_Robustness_Rank"] = str(config_rank.get(config_id, ""))
                    summary["Pre_Robustness_Prune_Reason"] = ""
                    candidate_layout_column_rows.extend(column_rows)
                    candidate_layout_location_rows.extend(location_rows)
                else:
                    summary["Pre_Robustness_Status"] = "PRUNED"
                    summary["Pre_Robustness_Rank"] = ""
                    summary["Pre_Robustness_Prune_Reason"] = "Outside pre-robust top configuration set"

                candidate_layout_rows.append({key: str(value) for key, value in summary.items()})

    print(f"[Stage 6] generated {len(candidate_layout_rows)} candidate summaries across {len(config_style_bundles)} selected config bundles")

    # Write layout-level KPIs.
    summary_export_fieldnames = [
        "Config_ID",
        "Layout_Feasible",
        "Allocation_Feasible_Initial",
        "Required_Locations_Total",
        "Total_Locations",
        "Capacity_Margin",
        "Assigned_Used_Height_Total",
        "Total_Allowed_Height",
        "Space_Left",
        "Beam_Relocations_Total",
        "Beam_Relocations_Before_Local_Search",
        "Beam_Relocations_After_Local_Search",
        "Local_Search_Accepted_Moves",
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
        "Feasible_Columns_Considered_Total",
        "Feasible_Columns_Min",
        "Feasible_Columns_Max",
        "Feasible_Columns_Average",
    ]
    summary_output_rows = [{key: str(value) for key, value in row.items()} for row in candidate_layout_rows]

    _write_csv_preserve_with_fallback(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary.csv",
        summary_export_fieldnames,
        [{field: str(row.get(field, "")) for field in summary_export_fieldnames} for row in summary_output_rows],
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

    method_label = "Greedy" if "greedy" in str(LAYOUT_OUTPUT_DIR.name).lower() else "Baseline"
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
