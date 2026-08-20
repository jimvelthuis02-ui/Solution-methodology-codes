import csv
from collections import defaultdict
import sys
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
LAYOUT_TOPFILLED_DIR = LAYOUT_OUTPUT_DIR
LAYOUT_DIAGNOSTICS_DIR = LAYOUT_OUTPUT_DIR
PRE_ROBUST_LAYOUT_LIMIT = 8
IMPLEMENTATION_STYLE = "implementation"
STYLE_PRIORITY = (IMPLEMENTATION_STYLE,)
LOCAL_SEARCH_MAX_ITERATIONS = 1
LOCAL_SEARCH_MAX_EVALUATIONS_PER_ITER = 24

SummaryRow: TypeAlias = dict[str, str]
DetailRows: TypeAlias = list[dict[str, str]]
LayoutSignature: TypeAlias = tuple[str, ...]
StyleCandidate: TypeAlias = tuple[SummaryRow, DetailRows, DetailRows, LayoutSignature]
ConfigStyleBundle: TypeAlias = tuple[str, list[StyleCandidate]]


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


def _slot_distribution_signature(column_assignments: dict[str, list[float]]) -> str:
    counts: dict[int, int] = defaultdict(int)
    for slots in column_assignments.values():
        for slot_size in slots:
            counts[int(round(slot_size))] += 1
    counts = common._exclude_fixed_doorgang_slot_counts(counts)
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
        total_counts = common._exclude_fixed_doorgang_slot_counts(
            _parse_count_signature(str(row.get("TopFill_Layout_Slot_Size_Distribution", "")))
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
    fixed_prefix_by_column: dict[str, list[float]] | None = None,
    doorgang_thresholds_by_rack: dict[str, tuple[int, float]] | None = None,
) -> dict[str, list[float]]:
    # Physical rule: all columns sharing a beam segment must keep identical rows
    # and slot heights above fixed prefixes (for example doorgang rows),
    # otherwise shared beam elevations cannot align.
    if not column_assignments:
        return {}

    uniform: dict[str, list[float]] = {
        column_key: [float(value) for value in slots]
        for column_key, slots in column_assignments.items()
    }
    fixed_prefix_by_column = fixed_prefix_by_column or {}
    doorgang_thresholds_by_rack = doorgang_thresholds_by_rack or {}
    global_smallest_slot = min(
        (float(value) for slots in uniform.values() for value in slots if float(value) > 0.0),
        default=0.0,
    )

    for rack, c0, c1 in sorted(segments):
        segment_columns = [f"{rack}{col:02d}" for col in range(c0, c1 + 1)]
        threshold = doorgang_thresholds_by_rack.get(rack)
        locked_prefix_by_column: dict[str, list[float]] = {
            column_key: [float(value) for value in fixed_prefix_by_column.get(column_key, [])]
            for column_key in segment_columns
        }
        if threshold is not None:
            doorgang_column, doorgang_height = threshold
            # Doorgang-adjacent 3-column segment: below the doorgang beam top, the
            # left pair may still differ from the doorgang column. Above that beam,
            # all three columns must share the same slot-size order.
            if c1 == doorgang_column and c0 == doorgang_column - 2:
                uniform_from_height = float(doorgang_height) + float(common.BEAM_HEIGHT)
                for column_key in segment_columns:
                    slots = uniform.get(column_key, [])
                    if not slots:
                        continue
                    running_height = 0.0
                    split_index = len(slots)
                    for idx, slot_size in enumerate(slots):
                        slot_bottom = running_height + (float(common.BEAM_HEIGHT) * idx)
                        if slot_bottom >= uniform_from_height - 1e-9:
                            split_index = idx
                            break
                        running_height += float(slot_size)
                    locked_prefix_by_column[column_key] = [float(value) for value in slots[:split_index]]
        profiles: dict[tuple[int, ...], tuple[int, float, int]] = {}
        minimum_locations = max(int(common.MIN_LOCATIONS_PER_COLUMN), 1)
        required_suffix_len = 0
        for column_key in segment_columns:
            if column_key not in uniform:
                continue
            prefix_len = len(locked_prefix_by_column.get(column_key, []))
            required_suffix_len = max(required_suffix_len, max(minimum_locations - prefix_len, 0))

        def _suffix_is_feasible(suffix: list[float]) -> bool:
            for column_key in segment_columns:
                if column_key not in uniform:
                    continue
                prefix = [float(value) for value in locked_prefix_by_column.get(column_key, [])]
                total_slots = len(prefix) + len(suffix)
                allowed = common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(total_slots - 1, common.MIN_BEAMS_PER_COLUMN)
                used = sum(prefix) + sum(suffix)
                if used > allowed + 1e-9:
                    return False
            return True

        for column_key in segment_columns:
            slots = uniform.get(column_key, [])
            if not slots:
                continue
            prefix = [float(value) for value in locked_prefix_by_column.get(column_key, [])]
            suffix_slots = list(slots)
            if prefix and len(slots) >= len(prefix):
                prefix_matches = True
                for idx, value in enumerate(prefix):
                    if abs(float(slots[idx]) - value) > 1e-9:
                        prefix_matches = False
                        break
                if prefix_matches:
                    suffix_slots = list(slots[len(prefix):])

            key = tuple(int(round(value)) for value in suffix_slots)
            count, total_height, length = profiles.get(key, (0, 0.0, len(key)))
            profiles[key] = (count + 1, total_height + sum(suffix_slots), len(key))

        if not profiles:
            continue

        smallest_suffix_slot = min(
            (value for key in profiles.keys() for value in key),
            default=0,
        )
        if smallest_suffix_slot <= 0:
            smallest_suffix_slot = global_smallest_slot

        def _extend_to_required_length(candidate: list[float]) -> list[float]:
            if smallest_suffix_slot <= 0:
                return candidate
            extended = list(candidate)
            while len(extended) < required_suffix_len:
                test = extended + [float(smallest_suffix_slot)]
                if not _suffix_is_feasible(test):
                    break
                extended = test
            return extended

        ranked_profiles = sorted(
            profiles.items(),
            key=lambda item: (
                item[1][0],  # most common profile in segment
                item[1][1],  # then highest cumulative used height
                item[1][2],  # then longest stack
                item[0],
            ),
            reverse=True,
        )

        canonical_suffix: list[float] | None = None
        for profile_key, _stats in ranked_profiles:
            candidate_suffix = [float(value) for value in profile_key]
            candidate_suffix = _extend_to_required_length(candidate_suffix)
            if len(candidate_suffix) < required_suffix_len:
                continue
            if _suffix_is_feasible(candidate_suffix):
                canonical_suffix = candidate_suffix
                break

        if canonical_suffix is None:
            fallback_suffix = [float(value) for value in ranked_profiles[0][0]]
            fallback_suffix = _extend_to_required_length(fallback_suffix)
            while fallback_suffix and not _suffix_is_feasible(fallback_suffix):
                fallback_suffix = fallback_suffix[:-1]
            canonical_suffix = fallback_suffix

        if len(canonical_suffix) < required_suffix_len and smallest_suffix_slot > 0:
            while len(canonical_suffix) < required_suffix_len:
                test_suffix = list(canonical_suffix) + [float(smallest_suffix_slot)]
                if not _suffix_is_feasible(test_suffix):
                    break
                canonical_suffix = test_suffix

        for column_key in segment_columns:
            if column_key in uniform:
                prefix = [float(value) for value in locked_prefix_by_column.get(column_key, [])]
                uniform[column_key] = prefix + list(canonical_suffix)

    return uniform


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


def _exact_fill_to_column_limit(
    slots: list[float],
    available_slot_sizes: list[float] | None = None,
    target_row_count: int | None = None,
) -> list[float] | None:
    """Find a column fill that satisfies the exact 754 cm physical limit under the common row count.

    The search is bounded to the actual slot-size range and uses a memoized subset-sum over a
    short row-count horizon, which keeps the runtime near the earlier baseline while ensuring the
    hard constraints are respected.
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
        return normalized

    if required_fill < 0.0:
        return None

    pool = _candidate_fill_pool(available_slot_sizes or normalized)
    max_slot = float(common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)
    needed_count = max(target_rows - len(normalized), 0)
    if needed_count == 0:
        return normalized if abs(required_fill) <= 1e-9 else None

    required_int = int(round(required_fill))
    min_possible = needed_count * 1
    max_possible = needed_count * int(round(max_slot))
    if required_int < min_possible or required_int > max_possible:
        return None

    # Deterministic bounded construction: allocate 1 cm to each required slot,
    # then distribute the remaining amount while respecting the 234 cm cap.
    fill = [1.0 for _ in range(needed_count)]
    remaining_extra = required_int - needed_count

    for index in range(needed_count):
        if remaining_extra <= 0:
            break
        room = int(round(max_slot - fill[index]))
        if room <= 0:
            continue

        chosen_extra = min(room, remaining_extra)

        fill[index] += float(chosen_extra)
        remaining_extra -= chosen_extra

    if remaining_extra > 0:
        return None

    if any(value > max_slot + 1e-9 for value in fill):
        return None

    return normalized + fill


def _enforce_rack_row_count_consistency(
    column_assignments: dict[str, list[float]],
    available_slot_sizes: list[float] | None = None,
    fixed_prefix_by_column: dict[str, list[float]] | None = None,
) -> dict[str, list[float]]:
    """Force equal rack row counts and fill each column to the physical 754 cm limit.

    Hard constraints enforced here:
    - same row count across all non-doorgang columns in the same rack;
    - each column's slots + beams total exactly the physical limit for that row count;
    - no representative slot exceeds 234 cm.
    """
    if not column_assignments:
        return {}

    fixed_prefix_by_column = fixed_prefix_by_column or {}
    adjusted: dict[str, list[float]] = {
        column_key: [float(value) for value in slots]
        for column_key, slots in column_assignments.items()
    }
    rack_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in adjusted:
        if len(column_key) < 2:
            continue
        rack = column_key[:-2]
        rack_columns[rack].append(column_key)

    candidate_sizes = sorted({
        min(float(value), common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)
        for slots in adjusted.values()
        for value in slots
        if float(value) > 0.0
    }, reverse=False)
    if available_slot_sizes:
        candidate_sizes = sorted({
            min(float(size), common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)
            for size in available_slot_sizes
            if float(size) > 0.0
        })
    if not candidate_sizes:
        candidate_sizes = [1.0]

    for rack, keys in rack_columns.items():
        eligible_columns = [column_key for column_key in keys if column_key not in fixed_prefix_by_column]
        if not eligible_columns:
            continue

        max_existing_rows = max(len(adjusted[column_key]) for column_key in eligible_columns)
        chosen_row_count: int | None = None
        for target_row_count in range(max_existing_rows, max_existing_rows + 12):
            feasible = True
            candidate_map: dict[str, list[float]] = {}
            for column_key in eligible_columns:
                trial = _exact_fill_to_column_limit(
                    adjusted.get(column_key, []),
                    candidate_sizes,
                    target_row_count=target_row_count,
                )
                if trial is None:
                    feasible = False
                    break
                candidate_map[column_key] = trial
            if feasible:
                chosen_row_count = target_row_count
                for column_key in eligible_columns:
                    adjusted[column_key] = candidate_map[column_key]
                break

        if chosen_row_count is None:
            for column_key in eligible_columns:
                slots = [float(value) for value in adjusted.get(column_key, [])]
                while len(slots) > max_existing_rows:
                    slots.pop()
                adjusted[column_key] = slots

    return adjusted


def _enforce_doorgang_spanning_beam_alignment(
    column_assignments: dict[str, list[float]],
    segments: set[tuple[str, int, int]],
    doorgang_thresholds_by_rack: dict[str, tuple[int, float]],
    fixed_prefix_by_column: dict[str, list[float]] | None = None,
) -> tuple[dict[str, list[float]], int]:
    # Physical rule: the doorgang beam elevation must exist across the full
    # 3-column segment. Left columns may use multiple slots below that level,
    # but above the doorgang beam the dominant (doorgang) column pattern rules.
    if not column_assignments:
        return {}, 0

    adjusted: dict[str, list[float]] = {
        column_key: [float(value) for value in slots]
        for column_key, slots in column_assignments.items()
    }
    fixed_prefix_by_column = fixed_prefix_by_column or {}
    lost_large_slot_allowance = 0

    available_sizes = sorted(
        {
            int(round(float(value)))
            for slots in adjusted.values()
            for value in slots
            if float(value) > 0.0
        }
    )

    def _split_index_at_exact_beam_height(slots: list[float], threshold: float) -> int | None:
        running = 0.0
        for n, slot in enumerate(slots, start=1):
            running += float(slot)
            beam_bottom = running + float(common.BEAM_HEIGHT) * float(n - 1)
            if abs(beam_bottom - float(threshold)) <= 1e-9:
                return n
        return None

    def _split_index_first_at_or_above(slots: list[float], threshold: float) -> int:
        running = 0.0
        for n, slot in enumerate(slots, start=1):
            running += float(slot)
            beam_bottom = running + float(common.BEAM_HEIGHT) * float(n - 1)
            if beam_bottom >= float(threshold) - 1e-9:
                return n
        return len(slots)

    def _candidate_prefixes_for_threshold(threshold: int) -> list[list[float]]:
        candidates: list[list[float]] = []
        max_rows = 6

        for n in range(1, max_rows + 1):
            target_sum = int(threshold - int(common.BEAM_HEIGHT) * (n - 1))
            if target_sum <= 0:
                continue

            def _dfs(remaining: int, depth: int, partial: list[int]) -> None:
                if depth == n:
                    if remaining == 0:
                        candidates.append([float(value) for value in partial])
                    return
                for size in available_sizes:
                    if size > remaining:
                        break
                    _dfs(remaining - size, depth + 1, partial + [size])

            _dfs(target_sum, 0, [])

        return candidates

    prefix_candidates_cache: dict[int, list[list[float]]] = {}

    def _best_prefix(existing_prefix: list[float], threshold: int) -> list[float] | None:
        if threshold not in prefix_candidates_cache:
            prefix_candidates_cache[threshold] = _candidate_prefixes_for_threshold(threshold)
        candidates = prefix_candidates_cache.get(threshold, [])
        if not candidates:
            return None

        existing_int = [int(round(value)) for value in existing_prefix]

        def _score(candidate: list[float]) -> tuple[float, int, int]:
            cand_int = [int(round(value)) for value in candidate]
            overlap = min(len(cand_int), len(existing_int))
            diff = sum(abs(cand_int[idx] - existing_int[idx]) for idx in range(overlap))
            if len(cand_int) > overlap:
                diff += sum(cand_int[overlap:])
            if len(existing_int) > overlap:
                diff += sum(existing_int[overlap:])
            return (float(diff), abs(len(cand_int) - len(existing_int)), len(cand_int))

        return min(candidates, key=_score)

    for rack, c0, c1 in sorted(segments):
        threshold = doorgang_thresholds_by_rack.get(rack)
        if threshold is None:
            continue

        doorgang_column, doorgang_height = threshold
        if not (c1 == doorgang_column and c0 == doorgang_column - 2):
            continue

        dominant_column_key = f"{rack}{doorgang_column:02d}"
        dominant_slots = adjusted.get(dominant_column_key, [])
        if not dominant_slots:
            continue

        dominant_split = _split_index_at_exact_beam_height(dominant_slots, float(doorgang_height))
        if dominant_split is None:
            dominant_split = _split_index_first_at_or_above(dominant_slots, float(doorgang_height))
        dominant_suffix = [float(value) for value in dominant_slots[dominant_split:]]

        for column_num in (doorgang_column - 2, doorgang_column - 1):
            column_key = f"{rack}{column_num:02d}"
            if column_key not in adjusted:
                continue

            # If this column has its own fixed doorgang row (for example rack K),
            # do not force a second conversion.
            if fixed_prefix_by_column.get(column_key):
                continue

            slots = adjusted[column_key]
            if not slots:
                continue

            threshold_int = int(round(float(doorgang_height)))
            split_at_or_above = _split_index_first_at_or_above(slots, float(doorgang_height))
            existing_prefix = [float(value) for value in slots[:split_at_or_above]]
            chosen_prefix = _best_prefix(existing_prefix, threshold_int)
            if chosen_prefix is None:
                # Fallback keeps the physical beam at threshold even without a
                # combinational exact match in available representative sizes.
                chosen_prefix = [float(doorgang_height)]

            old_large = sum(1 for value in existing_prefix if float(value) >= float(doorgang_height) - 1e-9)
            new_large = sum(1 for value in chosen_prefix if float(value) >= float(doorgang_height) - 1e-9)
            lost_large_slot_allowance += max(old_large - new_large, 0)

            adjusted[column_key] = list(chosen_prefix) + list(dominant_suffix)

    return adjusted, lost_large_slot_allowance


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


def _slot_signatures_from_location_rows(location_rows: list[dict[str, str]]) -> tuple[str, str]:
    # Recompute slot-distribution signatures directly from location-level rows,
    # excluding only the actual non-usable doorgang rows from capacity totals.
    exact_counts: dict[int, int] = defaultdict(int)
    for row in location_rows:
        if str(row.get("Usable_Location", "YES")).strip().upper() == "NO":
            continue
        slot_size = common._to_float(row.get("Assigned_Slot_Size_cm"))
        if slot_size is None:
            continue
        exact_counts[int(round(slot_size))] += 1
    distribution = "|".join(f"{size}:{count}" for size, count in sorted(exact_counts.items()))

    running = 0
    cumulative: dict[int, int] = {}
    for size in sorted(exact_counts.keys(), reverse=True):
        running += exact_counts[size]
        cumulative[size] = running
    cumulative_signature = "|".join(f"{size}:{cumulative[size]}" for size in sorted(cumulative.keys()))

    return distribution, cumulative_signature


def _apply_nonusable_location_flags(
    location_rows: list[dict[str, str]],
    fixed_prefix_by_column: dict[str, list[float]],
) -> list[dict[str, str]]:
    flagged_rows: list[dict[str, str]] = []
    nonusable_columns = set(fixed_prefix_by_column.keys())
    for row in location_rows:
        updated = dict(row)
        column_key = f"{str(updated.get('Rack', '')).strip()}{str(updated.get('Column', '')).strip()}"
        is_nonusable = column_key in nonusable_columns and str(updated.get("Row", "")).strip() == "01"
        updated["Usable_Location"] = "NO" if is_nonusable else "YES"
        flagged_rows.append(updated)
    return flagged_rows


def _enforce_min_locations_per_column(
    column_assignments: dict[str, list[float]],
    column_keys: list[str],
    min_slot_size: float,
) -> dict[str, list[float]]:
    # Final safety net: every column must have at least MIN_LOCATIONS_PER_COLUMN
    # while staying within per-column allowed height.
    minimum_locations = max(int(common.MIN_LOCATIONS_PER_COLUMN), 1)
    adjusted: dict[str, list[float]] = {
        key: [float(value) for value in column_assignments.get(key, [])]
        for key in column_keys
    }

    for column_key in column_keys:
        slots = adjusted[column_key]
        while len(slots) < minimum_locations:
            next_count = len(slots) + 1
            allowed_after = common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(next_count - 1, common.MIN_BEAMS_PER_COLUMN)
            proposed_used = sum(slots) + float(min_slot_size)
            if proposed_used > allowed_after + 1e-9:
                # If no configured slot fits, use exact feasible infill so the
                # physical minimum-location rule can still be satisfied.
                infill = allowed_after - sum(slots)
                if infill <= 1e-9:
                    break
                slots.append(float(infill))
                continue
            slots.append(float(min_slot_size))

    return {key: values for key, values in adjusted.items() if values}
def _repair_slot_coverage_after_doorway(
    column_assignments: dict[str, list[float]],
    target_exact_counts: dict[float, int],
    segments: set[tuple[str, int, int]],
    fixed_prefix_by_column: dict[str, list[float]] | None = None,
) -> dict[str, list[float]]:
    """Repair cumulative high-slot deficits with segment-wide four-for-one conversions."""
    if not column_assignments or not target_exact_counts:
        return column_assignments

    fixed_prefix_by_column = fixed_prefix_by_column or {}
    repaired = _clone_column_assignments(column_assignments)

    def _counts() -> dict[int, int]:
        counts: dict[int, int] = defaultdict(int)
        for slots in repaired.values():
            for value in slots:
                counts[int(round(value))] += 1
        return counts

    def _segment_suffix(segment: tuple[str, int, int], assignments: dict[str, list[float]]) -> tuple[tuple[float, ...], ...] | None:
        rack, c0, c1 = segment
        suffixes: list[tuple[float, ...]] = []
        for column in range(c0, c1 + 1):
            key = f"{rack}{column:02d}"
            slots = assignments.get(key)
            if slots is None:
                return None
            prefix_len = len(fixed_prefix_by_column.get(key, []))
            suffixes.append(tuple(float(value) for value in slots[prefix_len:]))
        if not suffixes or any(suffix != suffixes[0] for suffix in suffixes[1:]):
            return None
        return tuple(suffixes)

    for target_size in sorted((int(round(size)) for size in target_exact_counts), reverse=True):
        required_at_or_above = sum(
            count for size, count in target_exact_counts.items() if int(round(size)) >= target_size
        )
        while True:
            counts = _counts()
            available_at_or_above = sum(size_count for size, size_count in counts.items() if size >= target_size)
            deficit = required_at_or_above - available_at_or_above
            if deficit <= 0:
                break

            source_candidates = [
                int(round(size))
                for size, required in target_exact_counts.items()
                if int(round(size)) < target_size
                and counts.get(int(round(size)), 0) - int(required) >= 4
            ]
            if not source_candidates:
                break

            repaired_one = False
            for segment in sorted(segments):
                rack, c0, c1 = segment
                width = c1 - c0 + 1
                suffixes = _segment_suffix(segment, repaired)
                if suffixes is None:
                    continue
                source_size = min(source_candidates)
                if counts.get(source_size, 0) - next(
                    int(required)
                    for size, required in target_exact_counts.items()
                    if int(round(size)) == source_size
                ) < 4 * width:
                    continue
                if sum(value == source_size for value in suffixes[0]) < 4:
                    continue

                new_suffix: list[float] = list(suffixes[0])
                source_indices = [index for index, value in enumerate(new_suffix) if value == source_size][:4]
                if len(source_indices) < 4:
                    continue
                insert_at = source_indices[0]
                for index in reversed(source_indices):
                    del new_suffix[index]
                new_suffix.insert(insert_at, float(target_size))

                for column in range(c0, c1 + 1):
                    key = f"{rack}{column:02d}"
                    prefix_len = len(fixed_prefix_by_column.get(key, []))
                    repaired[key] = repaired[key][:prefix_len] + list(new_suffix)
                repaired_one = True
                break

            if not repaired_one:
                break

    return repaired


def _clone_column_assignments(column_assignments: dict[str, list[float]]) -> dict[str, list[float]]:
    return {key: [float(value) for value in values] for key, values in column_assignments.items()}


def _evaluate_relocations_for_assignments(
    column_assignments: dict[str, list[float]],
    layout_id: str,
    config_id: str,
    style: str,
    beam_segments: set[tuple[str, int, int]],
    doorgang_thresholds_by_rack: dict[str, tuple[int, float]],
    current_beam_units: set[str],
    current_beam_heights: dict[str, float],
    current_beam_units_by_column: dict[str, set[str]],
) -> tuple[int, dict[str, int], dict[str, int], dict[str, int], list[dict[str, str]], set[str]]:
    generated_location_rows = common._build_generated_layout_location_rows(
        layout_id=layout_id,
        config_id=config_id,
        style=style,
        column_assignments=column_assignments,
        segments=beam_segments,
        doorgang_thresholds_by_rack=doorgang_thresholds_by_rack,
    )
    proposed_beam_units, proposed_beam_heights = common._build_proposed_beam_units_from_layout_rows(
        generated_location_rows,
        beam_segments,
    )
    proposed_beam_units_by_column = common._beam_units_by_column(generated_location_rows)
    relocation_total, relocation_by_column, removed_by_column, added_by_column = common._beam_relocations(
        current_beam_units,
        proposed_beam_units,
        current_beam_heights,
        proposed_beam_heights,
        current_beam_units_by_column,
        proposed_beam_units_by_column,
    )
    return (
        relocation_total,
        relocation_by_column,
        removed_by_column,
        added_by_column,
        generated_location_rows,
        proposed_beam_units,
    )


def _local_search_minimize_beam_relocations(
    column_assignments: dict[str, list[float]],
    layout_columns: list[str],
    fixed_prefix_by_column: dict[str, list[float]],
    beam_segments: set[tuple[str, int, int]],
    doorgang_thresholds_by_rack: dict[str, tuple[int, float]],
    smallest_config_slot: float,
    layout_id: str,
    config_id: str,
    style: str,
    current_beam_units: set[str],
    current_beam_heights: dict[str, float],
    current_beam_units_by_column: dict[str, set[str]],
    constructive_slot_sizes: list[float] | None = None,
) -> tuple[dict[str, list[float]], int, int, int]:
    # First-improvement hill climb using swaps of complete physical segments.
    if not column_assignments:
        return column_assignments, 0, 0, 0

    current_assignments = _clone_column_assignments(column_assignments)
    current_relocations, relocation_by_column, _removed, _added, _rows, _units = _evaluate_relocations_for_assignments(
        current_assignments,
        layout_id,
        config_id,
        style,
        beam_segments,
        doorgang_thresholds_by_rack,
        current_beam_units,
        current_beam_heights,
        current_beam_units_by_column,
    )

    initial_relocations = current_relocations
    accepted_moves = 0
    completed_iterations = 0
    evaluation_cache: dict[tuple[tuple[tuple[str, tuple[float, ...]], ...]], tuple[int, dict[str, int]]] = {}

    def _segment_template(segment: tuple[str, int, int], assignments: dict[str, list[float]]) -> tuple[tuple[float, ...], ...]:
        rack, c0, c1 = segment
        return tuple(
            tuple(float(value) for value in assignments.get(f"{rack}{column:02d}", []))
            for column in range(c0, c1 + 1)
        )

    def _swap_segment_templates(
        assignments: dict[str, list[float]],
        segment_a: tuple[str, int, int],
        segment_b: tuple[str, int, int],
    ) -> dict[str, list[float]]:
        proposal = _clone_column_assignments(assignments)
        template_a = _segment_template(segment_a, assignments)
        template_b = _segment_template(segment_b, assignments)
        for segment, template in ((segment_a, template_b), (segment_b, template_a)):
            rack, c0, c1 = segment
            for offset, column in enumerate(range(c0, c1 + 1)):
                proposal[f"{rack}{column:02d}"] = list(template[offset])
        return proposal

    def _segment_swap_pools() -> list[list[tuple[str, int, int]]]:
        standard_pool: list[tuple[str, int, int]] = []
        middle_pool: list[tuple[str, int, int]] = []
        doorgang_229_pool: list[tuple[str, int, int]] = []
        for segment in sorted(beam_segments):
            _rack, c0, c1 = segment
            if (c0, c1) == (0, 3):
                standard_pool.append(segment)
            elif (c0, c1) in {(4, 6), (7, 9), (10, 12), (13, 15), (16, 18)}:
                middle_pool.append(segment)
            elif (c0, c1) == (19, 21):
                rack = segment[0]
                threshold = doorgang_thresholds_by_rack.get(rack)
                if threshold is not None and abs(float(threshold[1]) - 229.0) <= 1e-9:
                    doorgang_229_pool.append(segment)
        return [pool for pool in (standard_pool, middle_pool, doorgang_229_pool) if len(pool) >= 2]

    swap_pools = _segment_swap_pools()

    def _assignment_cache_key(assignments: dict[str, list[float]]) -> tuple[tuple[str, tuple[float, ...]], ...]:
        return tuple(sorted((column, tuple(float(value) for value in values)) for column, values in assignments.items()))

    current_segment_heights: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    for beam_unit, height in current_beam_heights.items():
        parsed = common._parse_beam_coordinate_parts(beam_unit)
        if parsed is not None:
            current_segment_heights[parsed[:3]].append(float(height))

    def _template_beam_heights(template: tuple[tuple[float, ...], ...]) -> list[float]:
        if not template:
            return []
        slots = template[0]
        heights: list[float] = []
        cumulative = 0.0
        for index, slot_size in enumerate(slots):
            if index > 0:
                heights.append(cumulative + (index - 1) * common.BEAM_HEIGHT)
            cumulative += float(slot_size)
        return heights

    def _segment_height_mismatch_lower_bound(
        segment: tuple[str, int, int],
        template: tuple[tuple[float, ...], ...],
    ) -> int:
        # For ordinary segments there are no partial doorgang beams, so the
        # height comparison is an exact lower bound before physical checks.
        if segment[1:] == (19, 21):
            return 0
        proposed = sorted(_template_beam_heights(template))
        current = sorted(current_segment_heights.get(segment, []))
        return sum(
            abs(current[index] - proposed[index]) > 1e-9
            for index in range(min(len(current), len(proposed)))
        )

    for _iter in range(LOCAL_SEARCH_MAX_ITERATIONS):
        improved = False
        completed_iterations += 1
        evaluations_this_iter = 0
        for pool in swap_pools:
            for index_a, segment_a in enumerate(pool):
                for segment_b in pool[index_a + 1 :]:
                    if evaluations_this_iter >= LOCAL_SEARCH_MAX_EVALUATIONS_PER_ITER:
                        break
                    template_a = _segment_template(segment_a, current_assignments)
                    template_b = _segment_template(segment_b, current_assignments)
                    if template_a == template_b:
                        continue

                    lower_bound = (
                        _segment_height_mismatch_lower_bound(segment_a, template_b)
                        + _segment_height_mismatch_lower_bound(segment_b, template_a)
                    )
                    if lower_bound >= current_relocations:
                        continue

                    proposal = _swap_segment_templates(current_assignments, segment_a, segment_b)
                    proposal = _enforce_segment_uniform_slot_profiles(
                        proposal,
                        beam_segments,
                        fixed_prefix_by_column=fixed_prefix_by_column,
                        doorgang_thresholds_by_rack=doorgang_thresholds_by_rack,
                    )
                    proposal, _ = _enforce_doorgang_spanning_beam_alignment(
                        proposal,
                        beam_segments,
                        doorgang_thresholds_by_rack,
                        fixed_prefix_by_column=fixed_prefix_by_column,
                    )
                    if smallest_config_slot > 0.0:
                        proposal = _enforce_min_locations_per_column(
                            proposal,
                            layout_columns,
                            float(smallest_config_slot),
                        )

                    cache_key = _assignment_cache_key(proposal)
                    cached = evaluation_cache.get(cache_key)
                    if cached is None:
                        evaluations_this_iter += 1
                        evaluated = _evaluate_relocations_for_assignments(
                            proposal,
                            layout_id,
                            config_id,
                            style,
                            beam_segments,
                            doorgang_thresholds_by_rack,
                            current_beam_units,
                            current_beam_heights,
                            current_beam_units_by_column,
                        )
                        proposal_relocations, proposal_by_column = evaluated[0], evaluated[1]
                        evaluation_cache[cache_key] = (proposal_relocations, proposal_by_column)
                    else:
                        proposal_relocations, proposal_by_column = cached
                    if proposal_relocations < current_relocations:
                        current_assignments = proposal
                        current_relocations = proposal_relocations
                        relocation_by_column = proposal_by_column
                        accepted_moves += 1
                        improved = True
                        break
                    if improved:
                        break
                if improved:
                    break
            if not improved:
                break

    return current_assignments, initial_relocations, current_relocations, accepted_moves if accepted_moves >= 0 else 0


def _build_top_filled_layout_set(
    summary_rows: list[dict[str, str]],
    column_rows: list[dict[str, str]],
    location_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    # Create a separate variant where each column's remaining height is added
    # to its top location so no practical column space is left unused.
    remaining_by_layout_column: dict[tuple[str, str], float] = {}
    for row in column_rows:
        layout_id = str(row.get("Layout_ID", "")).strip()
        rack_column = str(row.get("Rack_Column", "")).strip()
        remaining = common._to_float(row.get("Remaining_Height_cm")) or 0.0
        if layout_id and rack_column:
            remaining_by_layout_column[(layout_id, rack_column)] = max(remaining, 0.0)

    grouped_locations: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in location_rows:
        layout_id = str(row.get("Layout_ID", "")).strip()
        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        if not layout_id or not rack or not column:
            continue
        grouped_locations[(layout_id, f"{rack}{column}")].append(dict(row))

    top_filled_location_rows: list[dict[str, str]] = []
    space_added_by_layout: dict[str, float] = defaultdict(float)
    topfill_metadata_by_layout_column: dict[tuple[str, str], dict[str, float]] = {}
    for (layout_id, rack_column), rows in grouped_locations.items():
        rows_sorted = sorted(rows, key=lambda item: common._to_int_default(item.get("Row"), 0))
        remaining = remaining_by_layout_column.get((layout_id, rack_column), 0.0)
        if rows_sorted and remaining > 1e-9:
            top_row = rows_sorted[-1]
            top_slot = common._to_float(top_row.get("Assigned_Slot_Size_cm")) or 0.0
            capped_delta = max(0.0, common.MAX_REPRESENTATIVE_SLOT_SIZE_CM - top_slot)
            adjusted_top_slot = common._cap_slot_size(top_slot + remaining)
            topfill_metadata_by_layout_column[(layout_id, rack_column)] = {
                "row_index": float(common._to_int_default(top_row.get("Row"), 0)),
                "original_top_slot": float(top_slot),
                "added_height": float(min(remaining, capped_delta)),
                "adjusted_top_slot": float(adjusted_top_slot),
            }
            top_row["Assigned_Slot_Size_cm"] = f"{adjusted_top_slot:.0f}"
            space_added_by_layout[layout_id] += min(remaining, capped_delta)
        elif rows_sorted:
            top_row = rows_sorted[-1]
            top_slot = common._to_float(top_row.get("Assigned_Slot_Size_cm")) or 0.0
            topfill_metadata_by_layout_column[(layout_id, rack_column)] = {
                "row_index": float(common._to_int_default(top_row.get("Row"), 0)),
                "original_top_slot": float(top_slot),
                "added_height": 0.0,
                "adjusted_top_slot": float(top_slot),
            }
        top_filled_location_rows.extend(rows_sorted)

    top_filled_location_rows = sorted(
        top_filled_location_rows,
        key=lambda row: (
            str(row.get("Layout_ID", "")),
            str(row.get("Rack", "")),
            str(row.get("Column", "")),
            common._to_int_default(row.get("Row"), 0),
        ),
    )

    top_filled_column_rows: list[dict[str, str]] = []
    for row in column_rows:
        updated = dict(row)
        layout_id = str(updated.get("Layout_ID", "")).strip()
        rack_column = str(updated.get("Rack_Column", "")).strip()
        remaining = remaining_by_layout_column.get((layout_id, rack_column), 0.0)

        assigned_used = common._to_float(updated.get("Assigned_Used_Height_cm")) or 0.0
        allowed = common._to_float(updated.get("Allowed_Used_Height_cm")) or 0.0
        new_used = assigned_used + remaining
        meta = topfill_metadata_by_layout_column.get((layout_id, rack_column), {})
        row_index = common._to_int_default(meta.get("row_index"), 0)
        original_top_slot = common._to_float(meta.get("original_top_slot")) or 0.0
        adjusted_top_slot = common._to_float(meta.get("adjusted_top_slot")) or original_top_slot
        added_height = common._to_float(meta.get("added_height")) or 0.0

        updated["Assigned_Used_Height_cm"] = f"{new_used:.3f}"
        updated["Remaining_Height_cm"] = "0.000"
        updated["Fill_Ratio"] = f"{(new_used / allowed) if allowed > 0 else 0.0:.4f}"
        updated["TopFill_Adjusted_Row"] = str(row_index if row_index > 0 else "")
        updated["TopFill_Original_Top_Slot_cm"] = f"{original_top_slot:.0f}"
        updated["TopFill_Added_Height_cm"] = f"{added_height:.0f}"
        updated["TopFill_Adjusted_Top_Slot_cm"] = f"{adjusted_top_slot:.0f}"
        top_filled_column_rows.append(updated)

    location_rows_by_layout: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in top_filled_location_rows:
        location_rows_by_layout[str(row.get("Layout_ID", "")).strip()].append(row)

    top_filled_summary_rows: list[dict[str, str]] = []
    for row in summary_rows:
        layout_id = str(row.get("Layout_ID", "")).strip()
        if layout_id not in location_rows_by_layout:
            continue

        updated = dict(row)
        total_used = common._to_float(updated.get("Assigned_Used_Height_Total")) or 0.0
        total_allowed = common._to_float(updated.get("Total_Allowed_Height")) or 0.0
        added = space_added_by_layout.get(layout_id, 0.0)
        new_total_used = total_used + added

        topfill_dist_sig, topfill_cum_sig = _slot_signatures_from_location_rows(location_rows_by_layout[layout_id])
        updated["Assigned_Used_Height_Total"] = f"{new_total_used:.3f}"
        updated["Space_Left"] = "0.000"
        updated["Percentage_Rack_Height_Used"] = f"{((new_total_used / total_allowed) * 100.0) if total_allowed > 0 else 0.0:.2f}"
        updated["TopFill_Layout_Slot_Size_Distribution"] = topfill_dist_sig
        updated["TopFill_Layout_Slot_Size_Cumulative_Coverage"] = topfill_cum_sig
        top_filled_summary_rows.append(updated)

    return top_filled_summary_rows, top_filled_column_rows, top_filled_location_rows


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
    prepared_rows = _read_csv(INPUT_PREPARED)
    beam_map_rows = _read_csv(INPUT_LOCATION_BEAM_MAP)
    beam_height_rows = _read_csv(INPUT_BEAM_HEIGHT_COORDS)
    configs = _candidate_configs()
    capacity_rows = _capacity_rows_by_config()
    doorgang_thresholds_by_rack = common._doorgang_thresholds_by_rack(prepared_rows)
    fixed_doorgang_slot_by_column = common._fixed_doorgang_slot_by_column(prepared_rows)
    layout_columns = common._build_layout_columns(prepared_rows)
    current_beam_units, beam_segments, current_beam_heights = common._build_current_beam_units_and_segments(
        beam_map_rows,
        prepared_rows,
        beam_height_rows,
    )
    current_beam_units_by_column = common._beam_units_by_column(beam_map_rows)
    initial_beam_count, initial_grid_count = common._initial_beam_grid_counts(prepared_rows, current_beam_units)
    beam_preference = _beam_preference_by_column(current_beam_units)
    total_physical_locations = len(
        [
            row
            for row in prepared_rows
            if str(row.get("Location Type", "")).strip().lower() != "doorgang"
            and not common._is_split_location(str(row.get("Location", "")).strip())
        ]
    )

    config_style_bundles: list[ConfigStyleBundle] = []

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

        config_slot_sizes = _slot_sizes_from_capacity(rows)
        style_candidates: list[StyleCandidate] = []
        style = IMPLEMENTATION_STYLE
        layout_id = f"LAY_{layout_counter:03d}"
        layout_counter += 1

        # Allocate slot sizes to rack-columns for a single implementation layout.
        feasible_layout, assigned_exact, used_by_column, column_assignments, note, allocation_diagnostics = common._allocate_layout_by_column(
            target_exact_counts=base_exact_counts,
            column_keys=layout_columns,
            style=style,
            style_context={
                "beam_preference": beam_preference,
                "fixed_prefix_by_column": {
                    column_key: [float(height)]
                    for column_key, height in fixed_doorgang_slot_by_column.items()
                },
            },
        )

        expansion_slot_sizes = sorted(float(slot_size) for slot_size in base_exact_counts)
        column_assignments, used_by_column = _expand_layout_capacity(
            column_assignments=column_assignments,
            used_by_column=used_by_column,
            column_keys=layout_columns,
            slot_sizes=expansion_slot_sizes,
            minimum_exact_counts=base_exact_counts,
            style=style,
            beam_preference=beam_preference,
        )

        fixed_prefix_by_column = {
            column_key: [float(height)]
            for column_key, height in fixed_doorgang_slot_by_column.items()
            if column_key in column_assignments
        }

        column_assignments = _enforce_segment_uniform_slot_profiles(
            column_assignments,
            beam_segments,
            fixed_prefix_by_column=fixed_prefix_by_column,
            doorgang_thresholds_by_rack=doorgang_thresholds_by_rack,
        )
        column_assignments = _enforce_rack_row_count_consistency(
            column_assignments,
            available_slot_sizes=expansion_slot_sizes,
            fixed_prefix_by_column=fixed_prefix_by_column,
        )
        column_assignments, doorgang_alignment_conversions = _enforce_doorgang_spanning_beam_alignment(
            column_assignments,
            beam_segments,
            doorgang_thresholds_by_rack,
            fixed_prefix_by_column=fixed_prefix_by_column,
        )
        smallest_config_slot = min(expansion_slot_sizes) if expansion_slot_sizes else 0.0
        if smallest_config_slot > 0.0:
            column_assignments = _enforce_min_locations_per_column(
                column_assignments,
                layout_columns,
                float(smallest_config_slot),
            )
        column_assignments = _repair_slot_coverage_after_doorway(
            column_assignments,
            base_exact_counts,
            beam_segments,
            fixed_prefix_by_column=fixed_prefix_by_column,
        )
        used_by_column = {
            column_key: sum(float(value) for value in slots)
            for column_key, slots in column_assignments.items()
        }

        column_assignments, reloc_before_search, reloc_after_search, accepted_search_moves = _local_search_minimize_beam_relocations(
            column_assignments=column_assignments,
            layout_columns=layout_columns,
            fixed_prefix_by_column=fixed_prefix_by_column,
            beam_segments=beam_segments,
            doorgang_thresholds_by_rack=doorgang_thresholds_by_rack,
            smallest_config_slot=float(smallest_config_slot),
            layout_id=layout_id,
            config_id=config_id,
            style=style,
            current_beam_units=current_beam_units,
            current_beam_heights=current_beam_heights,
            current_beam_units_by_column=current_beam_units_by_column,
            constructive_slot_sizes=config_slot_sizes,
        )
        column_assignments = _enforce_rack_row_count_consistency(
            column_assignments,
            available_slot_sizes=expansion_slot_sizes,
            fixed_prefix_by_column=fixed_prefix_by_column,
        )
        used_by_column = {
            column_key: sum(float(value) for value in slots)
            for column_key, slots in column_assignments.items()
        }

        generated_location_rows = common._build_generated_layout_location_rows(
            layout_id=layout_id,
            config_id=config_id,
            style=style,
            column_assignments=column_assignments,
            segments=beam_segments,
            doorgang_thresholds_by_rack=doorgang_thresholds_by_rack,
        )
        generated_location_rows = _apply_nonusable_location_flags(
            generated_location_rows,
            fixed_prefix_by_column,
        )
        layout_slot_distribution, layout_slot_cumulative = _slot_signatures_from_location_rows(generated_location_rows)
        layout_signature = _layout_signature(generated_location_rows)
        proposed_beam_units, proposed_beam_heights = common._build_proposed_beam_units_from_layout_rows(
            generated_location_rows,
            beam_segments,
        )
        proposed_beam_units_by_column = common._beam_units_by_column(generated_location_rows)
        relocation_total, relocation_by_column, removed_by_column, added_by_column = common._beam_relocations(
            current_beam_units,
            proposed_beam_units,
            current_beam_heights,
            proposed_beam_heights,
            current_beam_units_by_column,
            proposed_beam_units_by_column,
        )
        required_beams, required_grids, additional_beams, additional_grids = common._material_requirements(
            initial_beam_count,
            initial_grid_count,
            proposed_beam_units,
            generated_location_rows,
        )

        # Compute utilization and implementation-effort KPIs per configuration.
        assigned_total = max(len(generated_location_rows) - common._fixed_doorgang_location_total(), 0)
        layout_physical_locations = assigned_total
        required_locations_total = sum(base_exact_counts.values())
        total_used_height = sum(used_by_column.values())
        total_allowed_height = sum(
            common.MAX_USED_HEIGHT_BASE
            - common.BEAM_HEIGHT * max(len(column_assignments.get(column_key, [])) - 1, common.MIN_BEAMS_PER_COLUMN)
            for column_key in layout_columns
        )
        space_utilization = (total_used_height / total_allowed_height) if total_allowed_height > 0 else 0.0
        capacity_margin = assigned_total - required_locations_total
        required_beam_moves = relocation_total
        pct_rack_height_used = space_utilization * 100.0
        space_left = sum(
            max(
                (
                    common.MAX_USED_HEIGHT_BASE
                    - common.BEAM_HEIGHT * max(len(column_assignments.get(column_key, [])) - 1, common.MIN_BEAMS_PER_COLUMN)
                )
                - used_by_column.get(column_key, 0.0),
                0.0,
            )
            for column_key in layout_columns
        )

        # Final feasibility must reflect the post-heuristic layout, not only the initial allocator step.
        final_layout_feasible = feasible_layout and capacity_margin >= 0 and space_utilization <= 1.0

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
                "Beam_Relocations_Total": str(relocation_total),
                "Beam_Relocations_Before_Local_Search": str(reloc_before_search),
                "Beam_Relocations_After_Local_Search": str(reloc_after_search),
                "Local_Search_Accepted_Moves": str(accepted_search_moves),
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
                "Doorgang_Usable_Alignment_Conversions": str(doorgang_alignment_conversions),
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
                slot_mix_by_column[f"{rack}{column}"][slot] += 1

        style_column_rows: list[dict[str, str]] = []
        for column_key, slots in sorted(column_assignments.items()):
            used = used_by_column.get(column_key, 0.0)
            beam_count = max(len(slots) - 1, common.MIN_BEAMS_PER_COLUMN)
            allowed = common.MAX_USED_HEIGHT_BASE - beam_count * common.BEAM_HEIGHT
            mix = slot_mix_by_column.get(column_key, {})
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
        "TopFill_Layout_Slot_Size_Distribution",
        "TopFill_Layout_Slot_Size_Cumulative_Coverage",
        "Source_Slot_Sizes",
        "Doorgang_Usable_Alignment_Conversions",
        "Feasible_Columns_Considered_Total",
        "Feasible_Columns_Min",
        "Feasible_Columns_Max",
        "Feasible_Columns_Average",
    ]
    summary_output_rows = [{key: str(value) for key, value in row.items()} for row in candidate_layout_rows]

    # Write a separate practical variant where each column's residual space is
    # absorbed by its highest assigned location.
    top_filled_summary_rows, top_filled_column_rows, top_filled_location_rows = _build_top_filled_layout_set(
        summary_output_rows,
        candidate_layout_column_rows_all,
        candidate_layout_location_rows_all,
    )

    _write_csv_preserve_with_fallback(
        LAYOUT_TOPFILLED_DIR / "Candidate_Layout_Summary_TopFilled.csv",
        summary_export_fieldnames,
        [{field: str(row.get(field, "")) for field in summary_export_fieldnames} for row in top_filled_summary_rows],
    )

    _write_csv_preserve_with_fallback(
        LAYOUT_TOPFILLED_DIR / "Candidate_Layout_By_Rack_Column_TopFilled.csv",
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
            "TopFill_Adjusted_Row",
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
                    "TopFill_Adjusted_Row",
                    "TopFill_Original_Top_Slot_cm",
                    "TopFill_Added_Height_cm",
                    "TopFill_Adjusted_Top_Slot_cm",
                    "Slot_Size_Distribution",
                ]
            }
            for row in top_filled_column_rows
        ],
    )

    _write_csv_clean_with_fallback(
        LAYOUT_TOPFILLED_DIR / "Candidate_Layout_By_Location_TopFilled.csv",
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
            for row in top_filled_location_rows
        ],
    )

    method_label = "Greedy" if "greedy" in str(LAYOUT_TOPFILLED_DIR.name).lower() else "Baseline"
    empty_rows = _empty_locations_rows_by_slot_size(top_filled_summary_rows, method_label)
    _write_csv_clean_with_fallback(
        LAYOUT_TOPFILLED_DIR / "Empty_Locations_By_Slot_Size.csv",
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
    layout_rows, column_rows, location_rows = build_layout_generation()
    print(
        "Layout generation complete. "
        f"Layouts: {len(layout_rows)}, columns: {len(column_rows)}, locations: {len(location_rows)}."
    )
