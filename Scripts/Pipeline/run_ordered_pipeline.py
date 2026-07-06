import csv
import math
import re
import runpy
from functools import lru_cache
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = ROOT / "Output"
STAGE1_OUTPUT_DIR = OUTPUT_ROOT / "01_Data_Preparation"
STAGE2_OUTPUT_DIR = OUTPUT_ROOT / "02_Scenario_Generation"
STAGE3_OUTPUT_DIR = OUTPUT_ROOT / "03_Slot_Size_Generation"
STAGE4_OUTPUT_DIR = OUTPUT_ROOT / "04_Candidate_Configuration_Filtering"
STAGE5_OUTPUT_DIR = OUTPUT_ROOT / "05_Capacity_Determination"
STAGE6_OUTPUT_DIR = OUTPUT_ROOT / "06_Layout_Generation"
STAGE7_OUTPUT_DIR = OUTPUT_ROOT / "07_Robustness_Evaluation"
STAGE8_OUTPUT_DIR = OUTPUT_ROOT / "08_Final_Selection"

SLOT_SIZE_ROOT = STAGE3_OUTPUT_DIR

METHODS = ("quantile_binning", "hierarchical_clustering", "kmeans_clustering")
BASE_SKU_COUNT = 843
SKU_SCENARIO_FACTORS = {
    "Low_Count": 0.9,
    "Base_Count": 1.0,
    "High_Count": 1.1,
}
CANDIDATE_LAYOUT_STYLES = ("implementation",)

COLUMN_MAX_HEIGHT = 770.0
TOP_BEAM_HEIGHT = 16.0
MAX_USED_HEIGHT_BASE = COLUMN_MAX_HEIGHT - TOP_BEAM_HEIGHT
MIN_BEAMS_PER_COLUMN = 3
BEAM_HEIGHT = 16.0
BEAM_RELOCATION_TOLERANCE_CM = 1.0

LOCATION_CODE_PATTERN = re.compile(r"^([A-Z])(\d{2})(\d{2})([A-Za-z]+)?$")
BEAM_SPAN_PATTERN = re.compile(r"^([A-Z])\[(\d{2})-(\d{2})\]:([0-9]{1,2}[A-Za-z]?)$")
BEAM_SINGLE_PATTERN = re.compile(r"^([A-Z])(\d{2}):([0-9]{1,2}[A-Za-z]?)$")


def _to_float(value: object | None) -> float | None:
    # Shared safe numeric parser used across pipeline stages.
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int_default(value: object | None, default: int = 0) -> int:
    # Convert numeric-like values to int with fallback default.
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def _allocate_counts_from_percentages(total_count: int, percentages: list[float]) -> list[int]:
    # Proportional allocation using floor + largest remainder balancing.
    raw = [max(0.0, percentage) * total_count for percentage in percentages]
    floors = [int(math.floor(value)) for value in raw]
    remainder = max(total_count - sum(floors), 0)
    ranked = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)

    for index in ranked[:remainder]:
        floors[index] += 1

    return floors


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read a UTF-8 CSV with required header row validation."""
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header row: {path}")
        return list(reader)


def _deduplicate_output_columns(
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> tuple[list[str], list[dict[str, str]]]:
    # Remove duplicate output columns that carry identical values row-by-row.
    if not fieldnames or not rows:
        return fieldnames, rows

    column_values: dict[str, list[str]] = {
        field: [str(row.get(field, "")) for row in rows]
        for field in fieldnames
    }

    kept_fields: list[str] = []
    seen_signatures: set[tuple[str, ...]] = set()
    for field in fieldnames:
        signature = tuple(column_values[field])
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        kept_fields.append(field)

    if not kept_fields:
        kept_fields = [fieldnames[0]]

    cleaned_rows = [
        {field: str(row.get(field, "")) for field in kept_fields}
        for row in rows
    ]
    return kept_fields, cleaned_rows


def _write_csv_clean(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    # Standardized CSV writer with parent-folder creation and column cleanup.
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_fields, cleaned_rows = _deduplicate_output_columns(fieldnames, rows)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=cleaned_fields)
        writer.writeheader()
        writer.writerows(cleaned_rows)


def _slot_size_variable_name(slot_size: float) -> str:
    # Canonical variable label used in capacity constraints.
    return f"x_{int(round(slot_size))}"


def _is_split_location(location: str) -> bool:
    match = LOCATION_CODE_PATTERN.match(location)
    if not match:
        return False
    suffix = match.group(4)
    return suffix is not None and suffix.strip() != ""


def _build_layout_columns(prepared_rows: list[dict[str, str]]) -> list[str]:
    """Build sorted rack-column keys eligible for generated layout assignment."""
    columns: set[str] = set()
    for row in prepared_rows:
        location_type = str(row.get("Location Type", "")).strip().lower()
        if location_type == "doorgang":
            continue
        location = str(row.get("Location", "")).strip()
        if location and _is_split_location(location):
            continue
        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        if rack and column:
            columns.add(f"{rack}{column}")
    return sorted(columns)


def _build_generated_layout_location_rows(
    layout_id: str,
    config_id: str,
    style: str,
    column_assignments: dict[str, list[float]],
    segments: set[tuple[str, int, int]] | None = None,
) -> list[dict[str, str]]:
    # Expand column-level slot assignments into synthetic location rows.
    location_rows: list[dict[str, str]] = []
    segment_by_column: dict[str, tuple[str, int, int]] = {}

    if segments:
        for rack, c0, c1 in segments:
            for col in range(c0, c1 + 1):
                segment_by_column[f"{rack}{col:02d}"] = (rack, c0, c1)

    for column_key in sorted(column_assignments.keys()):
        rack = column_key[0]
        column = column_key[1:]
        segment = segment_by_column.get(column_key)
        ordered_slots = list(column_assignments[column_key])
        for row_index, slot_size in enumerate(ordered_slots, start=1):
            if row_index <= 1:
                beam_coordinate = ""
            elif segment is None:
                beam_coordinate = f"{rack}{column}:{row_index:02d}"
            else:
                seg_rack, seg_c0, seg_c1 = segment
                beam_coordinate = f"{seg_rack}[{seg_c0:02d}-{seg_c1:02d}]:{row_index:02d}"

            location_rows.append(
                {
                    "Layout_ID": layout_id,
                    "Config_ID": config_id,
                    "Style": style,
                    "Location": f"{rack}{column}{row_index:02d}",
                    "Rack": rack,
                    "Column": column,
                    "Row": f"{row_index:02d}",
                    "Beam_Coordinate": beam_coordinate,
                    "Assignment_Unit_ID": f"COL::{column_key}::{row_index:02d}",
                    "Assignment_Unit_Type": "rack_column",
                    "Assigned_Slot_Size_cm": f"{slot_size:.0f}",
                }
            )

    return location_rows


def _allocate_layout_by_column(
    target_exact_counts: dict[float, int],
    column_keys: list[str],
    style: str,
    style_context: dict[str, object] | None = None,
) -> tuple[bool, dict[float, int], dict[str, float], dict[str, list[float]], str, dict[str, float]]:
    """Allocate exact slot-size demand using style-specific candidate filtering rules."""
    assigned_exact_counts: dict[float, int] = {slot_size: 0 for slot_size in target_exact_counts}
    used_height_by_column: dict[str, float] = defaultdict(float)
    counts_by_column: dict[str, int] = defaultdict(int)
    assignments_by_column: dict[str, list[float]] = defaultdict(list)
    beam_preference = style_context.get("beam_preference", {}) if isinstance(style_context, dict) else {}

    decisions_total = 0
    feasible_total = 0
    feasible_min: int | None = None
    feasible_max = 0
    style_rejected_total = 0
    style_influenced_total = 0
    forced_total = 0

    def _rack_loads() -> dict[str, int]:
        loads: dict[str, int] = defaultdict(int)
        for column_key, count in counts_by_column.items():
            if count > 0:
                loads[column_key[0]] += count
        return dict(loads)

    def _candidate_score(candidate: dict[str, float | int | str], selected_style: str) -> tuple[float, float, float, str]:
        projected_fill = float(candidate["projected_fill"])
        remaining_after = float(candidate["remaining_after"])
        current_count = float(candidate["current_count"])
        relocation_proxy = float(candidate["relocation_proxy"])
        imbalance_after = float(candidate["imbalance_after"])
        future_adaptability = float(candidate["future_adaptability"])
        column_key = str(candidate["column_key"])

        if selected_style == "utilization":
            return (-projected_fill, -current_count, remaining_after, column_key)
        if selected_style == "relocation":
            return (relocation_proxy, current_count * -1.0, projected_fill, column_key)
        if selected_style == "balanced":
            return (imbalance_after, projected_fill, remaining_after, column_key)
        if selected_style == "future_flexibility":
            return (-future_adaptability, projected_fill, current_count, column_key)
        return (remaining_after, current_count, projected_fill, column_key)

    def _style_filter(candidates: list[dict[str, float | int | str]], selected_style: str) -> list[dict[str, float | int | str]]:
        if not candidates:
            return []

        if selected_style == "utilization":
            ranked = sorted(candidates, key=lambda c: (-float(c["projected_fill"]), str(c["column_key"])))
            keep = max(1, len(ranked) // 4)
            return ranked[:keep]

        if selected_style == "relocation":
            ranked = sorted(candidates, key=lambda c: (float(c["relocation_proxy"]), str(c["column_key"])))
            keep = max(1, len(ranked) // 3)
            return ranked[:keep]

        if selected_style == "balanced":
            ranked = sorted(candidates, key=lambda c: (float(c["imbalance_after"]), str(c["column_key"])))
            keep = max(1, len(ranked) // 4)
            return ranked[:keep]

        if selected_style == "future_flexibility":
            flexibility_ready = [
                candidate
                for candidate in candidates
                if float(candidate["projected_fill"]) <= 0.80
            ]
            ranked = sorted(
                flexibility_ready if flexibility_ready else candidates,
                key=lambda c: (-float(c["future_adaptability"]), str(c["column_key"])),
            )
            keep = max(1, len(ranked) // 4)
            return ranked[:keep]

        return list(candidates)

    slot_sizes_desc = sorted(target_exact_counts.keys(), reverse=True)
    for slot_size in slot_sizes_desc:
        needed = int(target_exact_counts.get(slot_size, 0))
        while needed > 0:
            decisions_total += 1
            candidates: list[dict[str, float | int | str]] = []
            rack_loads_before = _rack_loads()
            avg_fill_before = (
                sum(used_height_by_column.get(column, 0.0) / max(MAX_USED_HEIGHT_BASE, 1e-9) for column in column_keys)
                / max(len(column_keys), 1)
            )
            for column_key in column_keys:
                current_count = counts_by_column.get(column_key, 0)
                next_count = current_count + 1
                allowed_after = MAX_USED_HEIGHT_BASE - BEAM_HEIGHT * max(next_count - 1, MIN_BEAMS_PER_COLUMN)
                proposed_used = used_height_by_column.get(column_key, 0.0) + slot_size
                if proposed_used > allowed_after + 1e-9:
                    continue

                remaining_after = allowed_after - proposed_used
                projected_fill = proposed_used / max(allowed_after, 1e-9)
                beam_pref = float(beam_preference.get(column_key, 0)) if isinstance(beam_preference, dict) else 0.0
                target_depth = max(int(round(beam_pref)) + 1, 1)
                # Relocation effort proxy: stay close to existing beam-derived depth and
                # avoid columns without beam support.
                relocation_proxy = (
                    abs((current_count + 1) - target_depth)
                    + (0.0 if beam_pref > 0 else 2.0)
                    + (0.0 if current_count > 0 else 0.5)
                )

                rack_key = column_key[0]
                rack_load_after = rack_loads_before.get(rack_key, 0) + 1
                rack_mean_after = (sum(rack_loads_before.values()) + 1) / max(len({key[0] for key in column_keys}), 1)
                imbalance_after = abs(rack_load_after - rack_mean_after) + abs(projected_fill - avg_fill_before)
                future_adaptability = remaining_after / max(allowed_after, 1e-9)

                candidates.append(
                    {
                        "column_key": column_key,
                        "current_count": current_count,
                        "remaining_after": remaining_after,
                        "projected_fill": projected_fill,
                        "relocation_proxy": relocation_proxy,
                        "imbalance_after": imbalance_after,
                        "future_adaptability": future_adaptability,
                    }
                )

            if not candidates:
                diagnostics = {
                    "Allocation_Decisions_Total": float(decisions_total),
                    "Feasible_Columns_Considered_Total": float(feasible_total),
                    "Feasible_Columns_Min": float(feasible_min or 0),
                    "Feasible_Columns_Max": float(feasible_max),
                    "Feasible_Columns_Average": (feasible_total / decisions_total) if decisions_total else 0.0,
                    "Candidates_Rejected_By_Style_Total": float(style_rejected_total),
                    "Decisions_Influenced_By_Style": float(style_influenced_total),
                    "Forced_By_Feasibility_Count": float(forced_total),
                }
                return False, assigned_exact_counts, dict(used_height_by_column), dict(assignments_by_column), f"No allocatable column found for slot size {slot_size:.0f} with remaining demand {needed}.", diagnostics

            feasible_count = len(candidates)
            feasible_total += feasible_count
            feasible_min = feasible_count if feasible_min is None else min(feasible_min, feasible_count)
            feasible_max = max(feasible_max, feasible_count)
            if feasible_count == 1:
                forced_total += 1

            baseline_sorted = sorted(
                candidates,
                key=lambda c: (float(c["remaining_after"]), int(c["current_count"]), str(c["column_key"])),
            )
            baseline_choice = baseline_sorted[0]

            style_candidates = _style_filter(candidates, style)
            style_rejected_total += max(feasible_count - len(style_candidates), 0)
            chosen_pool = style_candidates if style_candidates else candidates

            chosen_sorted = sorted(chosen_pool, key=lambda c: _candidate_score(c, style))
            chosen = chosen_sorted[0]

            if str(chosen["column_key"]) != str(baseline_choice["column_key"]):
                style_influenced_total += 1

            chosen_column = str(chosen["column_key"])
            assignments_by_column[chosen_column].append(slot_size)
            used_height_by_column[chosen_column] += slot_size
            counts_by_column[chosen_column] += 1
            assigned_exact_counts[slot_size] += 1
            needed -= 1

    assignments_by_column = {column_key: slots for column_key, slots in assignments_by_column.items() if slots}
    used_height_by_column = {column_key: used_height_by_column[column_key] for column_key in assignments_by_column}
    diagnostics = {
        "Allocation_Decisions_Total": float(decisions_total),
        "Feasible_Columns_Considered_Total": float(feasible_total),
        "Feasible_Columns_Min": float(feasible_min or 0),
        "Feasible_Columns_Max": float(feasible_max),
        "Feasible_Columns_Average": (feasible_total / decisions_total) if decisions_total else 0.0,
        "Candidates_Rejected_By_Style_Total": float(style_rejected_total),
        "Decisions_Influenced_By_Style": float(style_influenced_total),
        "Forced_By_Feasibility_Count": float(forced_total),
    }
    return True, assigned_exact_counts, dict(used_height_by_column), assignments_by_column, "Style-specific constrained allocation succeeded.", diagnostics


def _normalize_beam_level(level: str) -> str:
    # Normalize level tokens to zero-padded comparable strings.
    token = str(level).strip()
    match = re.match(r"^(\d{1,2})([A-Za-z]?)$", token)
    if not match:
        return token.lower()
    numeric = int(match.group(1))
    suffix = match.group(2).lower()
    return f"{numeric:02d}{suffix}"


def _parse_beam_coordinate_parts(coordinate: str) -> tuple[str, int, int, str] | None:
    """Parse span/single beam coordinate strings into structured components."""
    text = str(coordinate).strip()
    if text == "":
        return None

    span_match = BEAM_SPAN_PATTERN.match(text)
    if span_match:
        return (
            span_match.group(1),
            int(span_match.group(2)),
            int(span_match.group(3)),
            _normalize_beam_level(span_match.group(4)),
        )

    single_match = BEAM_SINGLE_PATTERN.match(text)
    if single_match:
        col = int(single_match.group(2))
        level = _normalize_beam_level(single_match.group(3))
        return single_match.group(1), col, col, level

    return None


def _format_beam_unit(rack: str, c0: int, c1: int, level: str) -> str:
    if c0 == c1:
        return f"{rack}{c0:02d}:{level}"
    return f"{rack}[{c0:02d}-{c1:02d}]:{level}"


def _beam_unit_columns(beam_unit: str) -> set[str]:
    parsed = _parse_beam_coordinate_parts(beam_unit)
    if parsed is None:
        return set()
    rack, c0, c1, _level = parsed
    return {f"{rack}{col:02d}" for col in range(c0, c1 + 1)}


def _beam_level_index(level: str) -> int | None:
    normalized = _normalize_beam_level(level)
    match = re.match(r"^(\d{2})", normalized)
    if not match:
        return None
    return int(match.group(1))


def _column_slot_sizes_from_rows(
    rows: list[dict[str, str]],
    slot_size_field: str,
) -> dict[str, dict[int, float]]:
    # Build per-column slot heights by row index from row-level datasets.
    slot_sizes: dict[str, dict[int, float]] = defaultdict(dict)
    for row in rows:
        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        row_index = _to_int_default(row.get("Row"), 0)
        slot_size = _to_float(row.get(slot_size_field))
        if rack == "" or column == "" or row_index <= 0 or slot_size is None:
            continue
        slot_sizes[f"{rack}{column}"][row_index] = float(slot_size)
    return dict(slot_sizes)


def _beam_unit_heights_from_slot_sizes(
    beam_units: set[str],
    slot_sizes_by_column: dict[str, dict[int, float]],
) -> dict[str, float]:
    # Estimate physical beam elevations (cm) from stacked slot heights below each beam level.
    heights: dict[str, float] = {}
    for beam_unit in beam_units:
        parsed = _parse_beam_coordinate_parts(beam_unit)
        if parsed is None:
            continue
        _rack, _c0, _c1, level = parsed
        level_index = _beam_level_index(level)
        if level_index is None or level_index <= 1:
            heights[beam_unit] = 0.0
            continue

        column_heights: list[float] = []
        for column_key in _beam_unit_columns(beam_unit):
            row_slot_sizes = slot_sizes_by_column.get(column_key, {})
            cumulative = 0.0
            for row_idx in range(1, level_index):
                slot_size = row_slot_sizes.get(row_idx)
                if slot_size is None:
                    cumulative = 0.0
                    break
                cumulative += float(slot_size)
            if cumulative > 0.0:
                column_heights.append(cumulative)

        if column_heights:
            heights[beam_unit] = sum(column_heights) / len(column_heights)

    return heights


def _build_current_beam_units_and_segments(
    beam_map_rows: list[dict[str, str]],
    prepared_rows: list[dict[str, str]] | None = None,
    beam_height_rows: list[dict[str, str]] | None = None,
) -> tuple[set[str], set[tuple[str, int, int]], dict[str, float]]:
    # Build baseline beam units and horizontal segment definitions from Stage 1 map.
    beam_units: set[str] = set()
    segments: set[tuple[str, int, int]] = set()

    for row in beam_map_rows:
        parsed = _parse_beam_coordinate_parts(str(row.get("Beam_Coordinate", "")))
        if parsed is None:
            continue
        rack, c0, c1, level = parsed
        beam_units.add(_format_beam_unit(rack, c0, c1, level))
        segments.add((rack, c0, c1))

    if beam_height_rows is not None:
        mapped_heights: dict[str, list[float]] = defaultdict(list)
        for row in beam_height_rows:
            parsed = _parse_beam_coordinate_parts(str(row.get("Beam_Coordinate", "")))
            if parsed is None:
                continue
            rack, c0, c1, level = parsed
            unit = _format_beam_unit(rack, c0, c1, level)
            bottom = _to_float(row.get("Beam_Bottom_cm"))
            if bottom is None:
                continue
            mapped_heights[unit].append(float(bottom))

        direct_heights = {
            unit: (sum(values) / len(values))
            for unit, values in mapped_heights.items()
            if values
        }
        return beam_units, segments, direct_heights

    if prepared_rows is None:
        return beam_units, segments, {}

    slot_sizes_by_column = _column_slot_sizes_from_rows(prepared_rows, "Location height")
    beam_heights = _beam_unit_heights_from_slot_sizes(beam_units, slot_sizes_by_column)

    return beam_units, segments, beam_heights


def _build_proposed_beam_units_from_layout_rows(
    layout_rows: list[dict[str, str]],
    segments: set[tuple[str, int, int]],
) -> tuple[set[str], dict[str, float]]:
    # Infer proposed beam units by common row depth across each structural segment.
    row_count_by_column: dict[str, int] = defaultdict(int)
    for row in layout_rows:
        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        row_index = _to_int_default(row.get("Row"), 0)
        if rack == "" or column == "" or row_index <= 0:
            continue
        key = f"{rack}{column}"
        if row_index > row_count_by_column.get(key, 0):
            row_count_by_column[key] = row_index

    proposed_units: set[str] = set()
    for rack, c0, c1 in sorted(segments):
        covered_columns = [f"{rack}{col:02d}" for col in range(c0, c1 + 1)]
        max_common_rows = min((row_count_by_column.get(column_key, 0) for column_key in covered_columns), default=0)
        for level in range(2, max_common_rows + 1):
            proposed_units.add(_format_beam_unit(rack, c0, c1, f"{level:02d}"))

    slot_sizes_by_column = _column_slot_sizes_from_rows(layout_rows, "Assigned_Slot_Size_cm")
    proposed_heights = _beam_unit_heights_from_slot_sizes(proposed_units, slot_sizes_by_column)

    return proposed_units, proposed_heights


def _best_slot_order_for_targets(
    slot_sizes: list[float],
    target_heights: list[float],
) -> list[float]:
    # Order slots to maximize prefix-height matches against baseline beam heights.
    if len(slot_sizes) <= 1 or not target_heights:
        return list(slot_sizes)

    rounded_slots = [int(round(value)) for value in slot_sizes]
    unique_sizes = sorted(set(rounded_slots))
    initial_counts = tuple(rounded_slots.count(size) for size in unique_sizes)
    total_sum = sum(rounded_slots)

    def _prefix_score(prefix_height: int) -> tuple[int, float]:
        min_delta = min(abs(prefix_height - float(target)) for target in target_heights)
        matched = 1 if min_delta <= BEAM_RELOCATION_TOLERANCE_CM else 0
        return matched, min_delta

    @lru_cache(maxsize=None)
    def _solve(remaining_counts: tuple[int, ...]) -> tuple[int, float, tuple[int, ...]]:
        remaining_total = sum(remaining_counts)
        if remaining_total <= 0:
            return 0, 0.0, ()

        remaining_sum = sum(count * size for count, size in zip(remaining_counts, unique_sizes))
        current_prefix = total_sum - remaining_sum

        best_matches = -1
        best_distance = float("inf")
        best_sequence: tuple[int, ...] = ()

        for idx, size in enumerate(unique_sizes):
            count = remaining_counts[idx]
            if count <= 0:
                continue

            next_counts = list(remaining_counts)
            next_counts[idx] -= 1
            next_counts_tuple = tuple(next_counts)

            next_prefix = current_prefix + size
            inc_match, inc_distance = _prefix_score(next_prefix)
            sub_matches, sub_distance, sub_sequence = _solve(next_counts_tuple)

            candidate_matches = inc_match + sub_matches
            candidate_distance = inc_distance + sub_distance
            candidate_sequence = (size,) + sub_sequence

            if candidate_matches > best_matches:
                best_matches = candidate_matches
                best_distance = candidate_distance
                best_sequence = candidate_sequence
                continue

            if candidate_matches == best_matches:
                if candidate_distance < best_distance - 1e-9:
                    best_distance = candidate_distance
                    best_sequence = candidate_sequence
                    continue
                if abs(candidate_distance - best_distance) <= 1e-9 and candidate_sequence > best_sequence:
                    best_sequence = candidate_sequence

        return best_matches, best_distance, best_sequence

    _matches, _distance, order = _solve(initial_counts)
    return [float(value) for value in order] if order else list(slot_sizes)


def _optimize_column_slot_order_for_beam_preservation(
    column_assignments: dict[str, list[float]],
    segments: set[tuple[str, int, int]],
    baseline_beam_heights: dict[str, float],
) -> dict[str, list[float]]:
    # Reorder each column's slot stack to preserve as many baseline beam heights
    # as possible within the column's structural segment.
    if not column_assignments:
        return {}

    segment_targets: dict[tuple[str, int, int], list[float]] = defaultdict(list)
    for beam_unit, height in baseline_beam_heights.items():
        parsed = _parse_beam_coordinate_parts(beam_unit)
        if parsed is None:
            continue
        rack, c0, c1, _level = parsed
        segment_targets[(rack, c0, c1)].append(float(height))

    for segment in segment_targets:
        segment_targets[segment] = sorted(segment_targets[segment])

    column_segment: dict[str, tuple[str, int, int]] = {}
    for rack, c0, c1 in segments:
        for col in range(c0, c1 + 1):
            column_segment[f"{rack}{col:02d}"] = (rack, c0, c1)

    optimized: dict[str, list[float]] = {}
    for column_key, slots in column_assignments.items():
        segment = column_segment.get(column_key)
        targets = segment_targets.get(segment, []) if segment is not None else []
        optimized[column_key] = _best_slot_order_for_targets(list(slots), targets)

    return optimized


def _beam_relocations(
    current_units: set[str],
    proposed_units: set[str],
    current_unit_heights: dict[str, float],
    proposed_unit_heights: dict[str, float],
) -> tuple[int, dict[str, int]]:
    # Count relocations from physical beam height shifts within each segment.
    # Matching by segment and nearest height prevents false zeros when row-count
    # is unchanged but slot-height geometry changes.
    by_segment_current: dict[tuple[str, int, int], list[tuple[str, float]]] = defaultdict(list)
    by_segment_proposed: dict[tuple[str, int, int], list[tuple[str, float]]] = defaultdict(list)

    for unit in current_units:
        parsed = _parse_beam_coordinate_parts(unit)
        if parsed is None:
            continue
        rack, c0, c1, _level = parsed
        height = current_unit_heights.get(unit)
        if height is None:
            continue
        by_segment_current[(rack, c0, c1)].append((unit, float(height)))

    for unit in proposed_units:
        parsed = _parse_beam_coordinate_parts(unit)
        if parsed is None:
            continue
        rack, c0, c1, _level = parsed
        height = proposed_unit_heights.get(unit)
        if height is None:
            continue
        by_segment_proposed[(rack, c0, c1)].append((unit, float(height)))

    relocated_units: set[str] = set()

    all_segments = set(by_segment_current.keys()) | set(by_segment_proposed.keys())
    for segment in all_segments:
        current_segment = sorted(by_segment_current.get(segment, []), key=lambda item: item[1])
        proposed_segment = sorted(by_segment_proposed.get(segment, []), key=lambda item: item[1])

        i = 0
        j = 0
        while i < len(current_segment) and j < len(proposed_segment):
            _current_unit, current_height = current_segment[i]
            _proposed_unit, proposed_height = proposed_segment[j]
            delta = current_height - proposed_height
            if abs(delta) <= BEAM_RELOCATION_TOLERANCE_CM:
                i += 1
                j += 1
            elif delta < -BEAM_RELOCATION_TOLERANCE_CM:
                # Current baseline beam is too low to match any later proposed
                # beam (which are sorted increasingly), so it must be relocated.
                relocated_units.add(current_segment[i][0])
                i += 1
            else:
                # Proposed beam is too low for the current baseline beam; advance
                # proposed pointer to seek a potential match at a higher elevation.
                j += 1

        while i < len(current_segment):
            relocated_units.add(current_segment[i][0])
            i += 1

    relocation_total = len(relocated_units)

    per_column: dict[str, int] = defaultdict(int)
    for beam_unit in relocated_units:
        for column_key in _beam_unit_columns(beam_unit):
            per_column[column_key] += 1

    return relocation_total, dict(per_column)


def _material_requirements(
    current_units: set[str],
    proposed_units: set[str],
) -> tuple[int, int, int]:
    # Material deltas between current and proposed beam sets.
    removed_set = current_units - proposed_units
    added_set = proposed_units - current_units
    removed_units = len(removed_set)
    added_units = len(added_set)

    def _grid_span_count(beam_unit: str) -> int:
        parsed = _parse_beam_coordinate_parts(beam_unit)
        if parsed is None:
            return 1
        _rack, c0, c1, _level = parsed
        return max((c1 - c0) + 1, 1)

    additional_beams = added_units
    removed_beams = removed_units
    additional_grids = sum(_grid_span_count(unit) for unit in added_set)
    return additional_beams, additional_grids, removed_beams


def _build_sku_count_scenarios(_scenario_rows: list[dict[str, str]]) -> dict[str, int]:
    # Shared low/base/high SKU demand assumptions used in downstream stages.
    base_count = BASE_SKU_COUNT
    return {
        name: int(round(base_count * factor))
        for name, factor in SKU_SCENARIO_FACTORS.items()
    }


SCRIPT_DIR = Path(__file__).resolve().parent
ORDERED_SCRIPTS = [
    "01_Data_Preparation/01_data_preparation.py",
    "02_Scenario_Generation/02_scenario_generation_weighted_delta.py",
    "03_Slot_Size_Generation/03_slot_size_generation_main.py",
    "04_Candidate_Configuration_Filtering/04_candidate_configuration_filtering.py",
    "05_Capacity_Determination/05_capacity_determination.py",
    "06_Layout_Generation/06_layout_generation.py",
    "07_Robustness_Evaluation/07_robustness_evaluation.py",
    "08_Final_Selection/08_final_selection.py",
]


def _script_path(script_name: str) -> Path:
    # Resolve stage script path relative to pipeline folder.
    return SCRIPT_DIR / script_name


def _run_script(script_name: str) -> None:
    """Execute one stage script as a __main__ module."""
    script_path = _script_path(script_name)
    if not script_path.exists():
        raise FileNotFoundError(f"Pipeline stage script not found: {script_path}")
    print(f"Running: {script_name}")
    runpy.run_path(str(script_path), run_name="__main__")


def run_pipeline() -> None:
    """Run all pipeline stages in deterministic order."""
    for script_name in ORDERED_SCRIPTS:
        _run_script(script_name)


if __name__ == "__main__":
    # Pipeline entrypoint for full ordered execution.
    run_pipeline()
    print("Ordered pipeline complete.")
