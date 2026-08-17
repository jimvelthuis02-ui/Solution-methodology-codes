import csv
import math
import re
import runpy
import time
from functools import lru_cache
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = ROOT / "Output"
STAGE1_OUTPUT_DIR = OUTPUT_ROOT / "01_Data_Preparation"
STAGE2_OUTPUT_DIR = OUTPUT_ROOT / "02_Scenario_Generation"
STAGE3_OUTPUT_DIR = OUTPUT_ROOT / "03_Slot_Size_Generation"
STAGE4_OUTPUT_DIR = OUTPUT_ROOT / "04_Candidate_Configuration"
STAGE5_OUTPUT_DIR = OUTPUT_ROOT / "05_Capacity_Determination"
STAGE6_OUTPUT_DIR = OUTPUT_ROOT / "06_Layout_Generation"
STAGE7_OUTPUT_DIR = OUTPUT_ROOT / "07_Robustness_Evaluation"
STAGE8_OUTPUT_DIR = OUTPUT_ROOT / "08_Final_Selection"

SLOT_SIZE_ROOT = STAGE3_OUTPUT_DIR

METHODS = ("quantile_binning", "hierarchical_clustering", "kmeans_clustering")
BASE_OCCUPIED_LOCATIONS_COUNT = 890
OCCUPIED_LOCATION_SCENARIO_FACTORS = {
    "Low_Count": 0.9,
    "Base_Count": 1.0,
    "High_Count": 1.1,
}
CANDIDATE_LAYOUT_STYLES = ("implementation",)

COLUMN_MAX_HEIGHT = 770.0
TOP_BEAM_HEIGHT = 16.0
MAX_USED_HEIGHT_BASE = COLUMN_MAX_HEIGHT - TOP_BEAM_HEIGHT
MIN_BEAMS_PER_COLUMN = 3
MIN_LOCATIONS_PER_COLUMN = 4
BEAM_HEIGHT = 16.0
BEAM_RELOCATION_TOLERANCE_CM = 0.0

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


def _decode_excel_text(value: object | None) -> str:
    # Normalize spreadsheet-forced text values of the form ="..." back to raw text.
    text = "" if value is None else str(value).strip()
    if len(text) >= 4 and text.startswith('="') and text.endswith('"'):
        inner = text[2:-1]
        return inner.replace('""', '"')
    return text


def _encode_excel_text(value: object | None) -> str:
    # Force spreadsheet tools (notably Excel) to keep comma-separated values as text.
    text = _decode_excel_text(value)
    if text == "":
        return ""
    escaped = text.replace('"', '""')
    return f'="{escaped}"'


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


FIXED_DOORGANG_SLOT_COUNTS = {
    224: 4,
    229: 6,
    234: 1,
}


def _fixed_doorgang_location_total() -> int:
    return sum(FIXED_DOORGANG_SLOT_COUNTS.values())


def _explicit_occupied_target_total() -> int:
    # The 890 target is the working occupied-location demand. Physical fixed
    # doorway slots (including the 234 cm slot) remain in the layout but are
    # excluded from the storage-demand total used for matching this target.
    return max(int(BASE_OCCUPIED_LOCATIONS_COUNT), 0)


def _enforce_occupied_location_target(
    counts: dict[float, int],
    target_total: int | None = None,
) -> dict[float, int]:
    """Normalize exact slot counts so the base occupied-demand target is explicit."""
    if not counts:
        return {}

    target = int(BASE_OCCUPIED_LOCATIONS_COUNT if target_total is None else target_total)
    normalized = {float(size): max(int(value), 0) for size, value in counts.items() if float(size) > 0.0}
    if not normalized:
        return {}

    current_total = sum(normalized.values())
    if current_total == target:
        return dict(sorted(normalized.items()))

    delta = target - current_total
    if delta > 0:
        anchor_size = min(normalized)
        normalized[anchor_size] = normalized.get(anchor_size, 0) + delta
    elif delta < 0:
        remaining = -delta
        for size in sorted(normalized.keys(), reverse=True):
            if remaining <= 0:
                break
            remove = min(normalized[size], remaining)
            normalized[size] -= remove
            remaining -= remove
        normalized = {size: count for size, count in normalized.items() if count > 0}
        while remaining > 0 and normalized:
            for size in sorted(normalized.keys()):
                if remaining <= 0:
                    break
                remove = min(normalized[size], remaining)
                normalized[size] -= remove
                remaining -= remove
            normalized = {size: count for size, count in normalized.items() if count > 0}
            if remaining > 0 and not normalized:
                break

    return dict(sorted(normalized.items()))


def _exclude_fixed_doorgang_slot_counts(counts: dict[int, int]) -> dict[int, int]:
    # Physical doorway slots such as 224, 229, and 234 are not active SKU-demand
    # locations. They are removed from the effective storage count, while the
    # base occupied-location target remains the business target to satisfy.
    adjusted = {size: max(int(count), 0) for size, count in counts.items()}
    for size, doorway_count in FIXED_DOORGANG_SLOT_COUNTS.items():
        adjusted[size] = max(adjusted.get(size, 0) - doorway_count, 0)
    return {size: count for size, count in sorted(adjusted.items()) if count > 0}


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


def _doorgang_thresholds_by_rack(prepared_rows: list[dict[str, str]]) -> dict[str, tuple[int, float]]:
    # Capture the physical height of the doorgang location per rack.
    thresholds: dict[str, tuple[int, float]] = {}
    for row in prepared_rows:
        location_type = str(row.get("Location Type", "")).strip().lower()
        if location_type != "doorgang":
            continue

        rack = str(row.get("Rack", "")).strip()
        column = _to_int_default(row.get("Column"), 0)
        height = _to_float(row.get("Location height"))
        if rack and column > 0 and height is not None:
            thresholds[rack] = (column, float(height))

    return thresholds


def _fixed_doorgang_slot_by_column(prepared_rows: list[dict[str, str]]) -> dict[str, float]:
    # Preserve physical doorgang location height as fixed row content per column.
    fixed_slots: dict[str, float] = {}
    for row in prepared_rows:
        location_type = str(row.get("Location Type", "")).strip().lower()
        if location_type != "doorgang":
            continue

        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        height = _to_float(row.get("Location height"))
        if rack and column and height is not None:
            fixed_slots[f"{rack}{column}"] = float(height)

    return fixed_slots


def _build_generated_layout_location_rows(
    layout_id: str,
    config_id: str,
    style: str,
    column_assignments: dict[str, list[float]],
    segments: set[tuple[str, int, int]] | None = None,
    doorgang_thresholds_by_rack: dict[str, tuple[int, float]] | None = None,
) -> list[dict[str, str]]:
    # Expand column-level slot assignments into synthetic location rows.
    location_rows: list[dict[str, str]] = []
    segment_by_column: dict[str, tuple[str, int, int]] = {}
    doorgang_thresholds_by_rack = doorgang_thresholds_by_rack or {}

    if segments:
        for rack, c0, c1 in segments:
            for col in range(c0, c1 + 1):
                segment_by_column[f"{rack}{col:02d}"] = (rack, c0, c1)

    for column_key in sorted(column_assignments.keys()):
        rack = column_key[0]
        column = column_key[1:]
        segment = segment_by_column.get(column_key)
        threshold = doorgang_thresholds_by_rack.get(rack)
        ordered_slots = list(column_assignments[column_key])
        cumulative_height = 0.0
        for row_index, slot_size in enumerate(ordered_slots, start=1):
            if row_index <= 1:
                beam_coordinate = ""
                beam_height_range = ""
            elif segment is None:
                beam_coordinate = f"{rack}{column}:{row_index:02d}"
                beam_count_below = max(row_index - 2, 0)
                beam_bottom = cumulative_height + (beam_count_below * BEAM_HEIGHT)
                beam_top = beam_bottom + BEAM_HEIGHT
                beam_height_range = f"{beam_bottom:.0f}-{beam_top:.0f}"
            else:
                seg_rack, seg_c0, seg_c1 = segment
                beam_count_below = max(row_index - 2, 0)
                beam_bottom = cumulative_height + (beam_count_below * BEAM_HEIGHT)
                beam_top = beam_bottom + BEAM_HEIGHT
                beam_height_range = f"{beam_bottom:.0f}-{beam_top:.0f}"
                if threshold is not None:
                    doorgang_column, doorgang_height = threshold
                    column_num = _to_int_default(column, 0)
                    beam_elevation = beam_bottom
                    # Below the doorgang height, the left-adjacent pair must be bridged with
                    # a 2-column beam, while the doorgang column itself has no beam.
                    # Once the row height exceeds the doorgang threshold, the full
                    # 3-column span can resume.
                    if seg_c1 == doorgang_column and beam_elevation < doorgang_height:
                        if column_num in {doorgang_column - 2, doorgang_column - 1}:
                            beam_coordinate = f"{seg_rack}[{doorgang_column - 2:02d}-{doorgang_column - 1:02d}]:{row_index:02d}"
                        elif column_num == doorgang_column:
                            beam_coordinate = ""
                        else:
                            beam_coordinate = f"{seg_rack}[{seg_c0:02d}-{seg_c1:02d}]:{row_index:02d}"
                    else:
                        beam_coordinate = f"{seg_rack}[{seg_c0:02d}-{seg_c1:02d}]:{row_index:02d}"
                else:
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
                    "Beam_Height_Range_cm": beam_height_range,
                    "Assignment_Unit_ID": f"COL::{column_key}::{row_index:02d}",
                    "Assignment_Unit_Type": "rack_column",
                    "Assigned_Slot_Size_cm": f"{slot_size:.0f}",
                }
            )
            cumulative_height += float(slot_size)

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
    fixed_prefix_by_column = style_context.get("fixed_prefix_by_column", {}) if isinstance(style_context, dict) else {}

    # Seed fixed per-column rows (for example doorgang rows) before allocation so
    # all feasibility checks already account for their occupied height.
    if isinstance(fixed_prefix_by_column, dict):
        for column_key, prefix_values in fixed_prefix_by_column.items():
            if column_key not in column_keys:
                continue
            seeded_slots = [float(value) for value in list(prefix_values)]
            if not seeded_slots:
                continue
            assignments_by_column[column_key].extend(seeded_slots)
            used_height_by_column[column_key] += sum(seeded_slots)
            counts_by_column[column_key] += len(seeded_slots)

    decisions_total = 0
    feasible_total = 0
    feasible_min: int | None = None
    feasible_max = 0
    forced_total = 0

    def _candidate_score(candidate: dict[str, float | int | str]) -> tuple[float, float, float, str]:
        projected_fill = float(candidate["projected_fill"])
        remaining_after = float(candidate["remaining_after"])
        current_count = float(candidate["current_count"])
        column_key = str(candidate["column_key"])
        return (remaining_after, current_count, projected_fill, column_key)

    slot_sizes_desc = sorted(target_exact_counts.keys(), reverse=True)
    smallest_slot_size = min(slot_sizes_desc) if slot_sizes_desc else 0.0
    minimum_locations = max(int(MIN_LOCATIONS_PER_COLUMN), 1)
    for slot_size in slot_sizes_desc:
        needed = int(target_exact_counts.get(slot_size, 0))
        while needed > 0:
            decisions_total += 1
            candidates: list[dict[str, float | int | str]] = []
            for column_key in column_keys:
                current_count = counts_by_column.get(column_key, 0)
                next_count = current_count + 1
                allowed_after = MAX_USED_HEIGHT_BASE - BEAM_HEIGHT * max(next_count - 1, MIN_BEAMS_PER_COLUMN)
                proposed_used = used_height_by_column.get(column_key, 0.0) + slot_size
                if proposed_used > allowed_after + 1e-9:
                    continue

                # Keep room to reach minimum locations in this column by allowing
                # the remaining mandatory slots at the smallest available size.
                remaining_to_min = max(minimum_locations - next_count, 0)
                if remaining_to_min > 0 and smallest_slot_size > 0.0:
                    future_count = next_count + remaining_to_min
                    future_allowed = MAX_USED_HEIGHT_BASE - BEAM_HEIGHT * max(future_count - 1, MIN_BEAMS_PER_COLUMN)
                    future_used = proposed_used + (remaining_to_min * smallest_slot_size)
                    if future_used > future_allowed + 1e-9:
                        continue

                remaining_after = allowed_after - proposed_used
                projected_fill = proposed_used / max(allowed_after, 1e-9)

                candidates.append(
                    {
                        "column_key": column_key,
                        "current_count": current_count,
                        "remaining_after": remaining_after,
                        "projected_fill": projected_fill,
                    }
                )

            if not candidates:
                diagnostics = {
                    "Allocation_Decisions_Total": float(decisions_total),
                    "Feasible_Columns_Considered_Total": float(feasible_total),
                    "Feasible_Columns_Min": float(feasible_min or 0),
                    "Feasible_Columns_Max": float(feasible_max),
                    "Feasible_Columns_Average": (feasible_total / decisions_total) if decisions_total else 0.0,
                    "Forced_By_Feasibility_Count": float(forced_total),
                }
                return False, assigned_exact_counts, dict(used_height_by_column), dict(assignments_by_column), f"No allocatable column found for slot size {slot_size:.0f} with remaining demand {needed}.", diagnostics

            feasible_count = len(candidates)
            feasible_total += feasible_count
            feasible_min = feasible_count if feasible_min is None else min(feasible_min, feasible_count)
            feasible_max = max(feasible_max, feasible_count)
            if feasible_count == 1:
                forced_total += 1

            chosen_sorted = sorted(candidates, key=_candidate_score)
            chosen = chosen_sorted[0]

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
        "Forced_By_Feasibility_Count": float(forced_total),
    }
    return True, assigned_exact_counts, dict(used_height_by_column), assignments_by_column, "Implementation-style constrained allocation succeeded.", diagnostics


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


def _beam_units_by_column(rows: list[dict[str, str]]) -> dict[str, set[str]]:
    # Build explicit beam-unit membership per column from row-level coordinates.
    # This preserves partial spans (for example around doorgang) for per-column
    # relocation/add/remove accounting.
    units_by_column: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        beam_coordinate = str(row.get("Beam_Coordinate", "")).strip()
        if not rack or not column or not beam_coordinate:
            continue

        parsed = _parse_beam_coordinate_parts(beam_coordinate)
        if parsed is None:
            continue

        seg_rack, c0, c1, level = parsed
        unit = _format_beam_unit(seg_rack, c0, c1, level)
        units_by_column[f"{rack}{column}"].add(unit)

    return dict(units_by_column)


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
                beam_count_below = max(level_index - 2, 0)
                column_heights.append(cumulative + (beam_count_below * BEAM_HEIGHT))

        if column_heights:
            heights[beam_unit] = sum(column_heights) / len(column_heights)

    return heights


def _build_current_beam_units_and_segments(
    beam_map_rows: list[dict[str, str]],
    prepared_rows: list[dict[str, str]] | None = None,
    beam_height_rows: list[dict[str, str]] | None = None,
) -> tuple[set[str], set[tuple[str, int, int]], dict[str, float]]:
    # Build baseline beam units and horizontal segment definitions from Stage 1 data.
    beam_units: set[str] = set()
    segments: set[tuple[str, int, int]] = set()

    for row in beam_map_rows:
        parsed = _parse_beam_coordinate_parts(str(row.get("Beam_Coordinate", "")))
        if parsed is None:
            continue
        rack, c0, c1, level = parsed
        beam_units.add(_format_beam_unit(rack, c0, c1, level))
        segments.add((rack, c0, c1))

    # Prefer structural segments from prepared rows so row-level partial beam spans
    # (for example near doorgang) do not overwrite the rack's physical segmentation.
    if prepared_rows is not None:
        rack_columns: dict[str, set[int]] = defaultdict(set)
        for row in prepared_rows:
            rack = str(row.get("Rack", "")).strip()
            col = _to_int_default(row.get("Column"), -1)
            if rack and col >= 0:
                rack_columns[rack].add(col)

        structural_segments: set[tuple[str, int, int]] = set()
        for rack, columns in rack_columns.items():
            if not columns:
                continue
            max_col = max(columns)
            start = 0
            first_end = min(max_col, 3)
            structural_segments.add((rack, start, first_end))
            start = first_end + 1
            while start <= max_col:
                end = min(max_col, start + 2)
                structural_segments.add((rack, start, end))
                start = end + 1

        if structural_segments:
            segments = structural_segments

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
    # Infer proposed beam units directly from explicit generated beam coordinates.
    # This preserves partial spans around doorgang/split areas instead of collapsing
    # each segment to the minimum common row depth.
    proposed_units: set[str] = set()
    proposed_heights_samples: dict[str, list[float]] = defaultdict(list)

    for row in layout_rows:
        beam_coordinate = str(row.get("Beam_Coordinate", "")).strip()
        if not beam_coordinate:
            continue
        parsed = _parse_beam_coordinate_parts(beam_coordinate)
        if parsed is None:
            continue
        rack, c0, c1, level = parsed
        beam_unit = _format_beam_unit(rack, c0, c1, level)
        proposed_units.add(beam_unit)

        beam_height_range = str(row.get("Beam_Height_Range_cm", "")).strip()
        if "-" in beam_height_range:
            bottom_text = beam_height_range.split("-", 1)[0].strip()
            bottom = _to_float(bottom_text)
            if bottom is not None:
                proposed_heights_samples[beam_unit].append(float(bottom))

    proposed_heights = {
        unit: (sum(samples) / len(samples))
        for unit, samples in proposed_heights_samples.items()
        if samples
    }

    # Fallback if height ranges are unexpectedly missing for some generated beams.
    missing_units = [unit for unit in proposed_units if unit not in proposed_heights]
    if missing_units:
        slot_sizes_by_column = _column_slot_sizes_from_rows(layout_rows, "Assigned_Slot_Size_cm")
        inferred = _beam_unit_heights_from_slot_sizes(set(missing_units), slot_sizes_by_column)
        proposed_heights.update(inferred)

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


def _count_prefix_matches(slot_sequence: list[float], target_heights: list[float]) -> int:
    """Return how many baseline beam bottoms are matched by cumulative slot heights."""
    if not slot_sequence or not target_heights:
        return 0

    matched = 0
    seen_targets: set[float] = set()
    cumulative = 0.0
    for slot_size in slot_sequence:
        cumulative += float(slot_size)
        for target in target_heights:
            if abs(cumulative - float(target)) <= BEAM_RELOCATION_TOLERANCE_CM + 1e-9:
                seen_targets.add(float(target))
    return len(seen_targets)


def _constructive_beam_preservation_pass(
    column_assignments: dict[str, list[float]],
    segments: set[tuple[str, int, int]],
    baseline_beam_heights: dict[str, float],
    fixed_prefix_by_column: dict[str, list[float]] | None = None,
) -> dict[str, list[float]]:
    """Constructively reorder each segment to maximize the number of existing beam bottoms that can stay in place."""
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

    fixed_prefix_by_column = fixed_prefix_by_column or {}
    optimized = {column_key: [float(value) for value in values] for column_key, values in column_assignments.items()}

    for segment in sorted(segments):
        segment_columns = [f"{segment[0]}{col:02d}" for col in range(segment[1], segment[2] + 1)]
        targets = segment_targets.get(segment, [])
        if not targets:
            continue

        best_score = -1
        best_orderings: dict[str, list[float]] = {}
        for column_key in segment_columns:
            if column_key not in optimized:
                continue
            slots = [float(value) for value in optimized.get(column_key, [])]
            fixed_prefix = [float(value) for value in fixed_prefix_by_column.get(column_key, [])]
            movable = list(slots)
            if fixed_prefix and len(movable) >= len(fixed_prefix):
                prefix_match = True
                for idx, value in enumerate(fixed_prefix):
                    if abs(float(movable[idx]) - float(value)) > 1e-9:
                        prefix_match = False
                        break
                if prefix_match:
                    movable = movable[len(fixed_prefix):]

            fixed_height = sum(fixed_prefix)
            adjusted_targets = [target - fixed_height for target in targets if target > fixed_height]
            ordered_moves = _best_slot_order_for_targets(movable, adjusted_targets)
            candidate = fixed_prefix + ordered_moves
            score = _count_prefix_matches(candidate, targets)
            if score > best_score:
                best_score = score
                best_orderings = {column_key: candidate}
            elif score == best_score and candidate:
                best_orderings[column_key] = candidate

        if best_score > 0:
            for column_key, candidate in best_orderings.items():
                optimized[column_key] = candidate

    return optimized


def _optimize_column_slot_order_for_beam_preservation(
    column_assignments: dict[str, list[float]],
    segments: set[tuple[str, int, int]],
    baseline_beam_heights: dict[str, float],
    fixed_prefix_by_column: dict[str, list[float]] | None = None,
) -> dict[str, list[float]]:
    # Backward-compatible wrapper around the constructive beam-preservation pass.
    return _constructive_beam_preservation_pass(
        column_assignments=column_assignments,
        segments=segments,
        baseline_beam_heights=baseline_beam_heights,
        fixed_prefix_by_column=fixed_prefix_by_column,
    )


def _beam_relocations(
    current_units: set[str],
    proposed_units: set[str],
    current_unit_heights: dict[str, float],
    proposed_unit_heights: dict[str, float],
    current_units_by_column: dict[str, set[str]] | None = None,
    proposed_units_by_column: dict[str, set[str]] | None = None,
) -> tuple[int, dict[str, int], dict[str, int], dict[str, int]]:
    # Count beam changes by segment.
    # Added/removed beams are driven by the difference in beam counts.
    # Relocations are paired beams whose locations/heights do not match.
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
    removed_units: set[str] = set()
    added_units: set[str] = set()

    all_segments = set(by_segment_current.keys()) | set(by_segment_proposed.keys())
    for segment in all_segments:
        current_segment = sorted(by_segment_current.get(segment, []), key=lambda item: item[1])
        proposed_segment = sorted(by_segment_proposed.get(segment, []), key=lambda item: item[1])

        paired_count = min(len(current_segment), len(proposed_segment))
        for index in range(paired_count):
            current_unit, current_height = current_segment[index]
            _proposed_unit, proposed_height = proposed_segment[index]
            if abs(current_height - proposed_height) > 1e-9:
                relocated_units.add(current_unit)

        if len(current_segment) > paired_count:
            removed_units.update(unit for unit, _height in current_segment[paired_count:])

        if len(proposed_segment) > paired_count:
            added_units.update(unit for unit, _height in proposed_segment[paired_count:])

    relocation_total = len(relocated_units)

    per_column_relocated: dict[str, int] = defaultdict(int)
    for beam_unit in relocated_units:
        if current_units_by_column is not None:
            for column_key, column_units in current_units_by_column.items():
                if beam_unit in column_units:
                    per_column_relocated[column_key] += 1
        else:
            for column_key in _beam_unit_columns(beam_unit):
                per_column_relocated[column_key] += 1

    per_column_removed: dict[str, int] = defaultdict(int)
    for beam_unit in removed_units:
        if current_units_by_column is not None:
            for column_key, column_units in current_units_by_column.items():
                if beam_unit in column_units:
                    per_column_removed[column_key] += 1
        else:
            for column_key in _beam_unit_columns(beam_unit):
                per_column_removed[column_key] += 1

    per_column_added: dict[str, int] = defaultdict(int)
    for beam_unit in added_units:
        if proposed_units_by_column is not None:
            for column_key, column_units in proposed_units_by_column.items():
                if beam_unit in column_units:
                    per_column_added[column_key] += 1
        else:
            for column_key in _beam_unit_columns(beam_unit):
                per_column_added[column_key] += 1

    return relocation_total, dict(per_column_relocated), dict(per_column_removed), dict(per_column_added)


def _initial_beam_grid_counts(
    prepared_rows: list[dict[str, str]] | None,
    current_units: set[str],
) -> tuple[int, int]:
    # Derive baseline counts directly from Stage 1 prepared/beam data. Explicit
    # beam support includes supported 1b/1c and split locations; floor-level
    # 01/1a positions remain excluded.
    baseline_beams = len(current_units)
    baseline_grids = 0
    for row in prepared_rows or []:
        location_type = str(row.get("Location Type", "")).strip().lower()
        if location_type == "doorgang":
            continue
        location = str(row.get("Location", "")).strip()
        row_label = str(row.get("Row", "")).strip().lower()
        beam_column_count = str(row.get("Beam column count", "")).strip()
        if location and beam_column_count and row_label not in {"01", "1a"}:
            baseline_grids += 1

    return baseline_beams, baseline_grids


def _material_requirements(
    initial_beam_count: int,
    initial_grid_count: int,
    proposed_units: set[str],
    proposed_layout_rows: list[dict[str, str]],
) -> tuple[int, int, int, int]:
    # Required materials are based on total required counts in the proposed layout.
    required_beams = len(proposed_units)
    required_grids = sum(1 for row in proposed_layout_rows if _to_int_default(row.get("Row"), 0) > 1)

    additional_beams = max(required_beams - initial_beam_count, 0)
    additional_grids = max(required_grids - initial_grid_count, 0)

    return required_beams, required_grids, additional_beams, additional_grids


def _build_occupied_location_count_scenarios(_scenario_rows: list[dict[str, str]]) -> dict[str, int]:
    # Shared low/base/high occupied-location demand assumptions used downstream.
    base_count = BASE_OCCUPIED_LOCATIONS_COUNT
    return {
        name: int(round(base_count * factor))
        for name, factor in OCCUPIED_LOCATION_SCENARIO_FACTORS.items()
    }


SCRIPT_DIR = Path(__file__).resolve().parent
ORDERED_SCRIPTS = [
    "01_Data_Preparation/01_data_preparation.py",
    "02_Scenario_Generation/02_scenario_generation_weighted_delta.py",
    "03_Slot_Size_Generation/03_slot_size_generation_main.py",
    "04_Candidate_Configuration/04_candidate_configuration.py",
    "05_Capacity_Determination/05_capacity_determination.py",
    "06_Layout_Generation/06_layout_generation.py",
    "06_Layout_Generation/06_layout_generation_greedy.py",
    "07_Robustness_Evaluation/07_robustness_evaluation.py",
    "07_Robustness_Evaluation/07_robustness_evaluation_greedy.py",
    "08_Final_Selection/08_final_selection.py",
    "08_Final_Selection/08_final_selection_greedy.py",
    "Heuristic_Variants/heuristic_variants.py",
    "06_Layout_Generation/06_layout_generation_heuristics.py",
    "07_Robustness_Evaluation/07_robustness_heuristics.py",
    "08_Final_Selection/08_final_selection_heuristics.py",
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
    start_time = time.perf_counter()
    run_pipeline()
    elapsed_seconds = time.perf_counter() - start_time
    hours = int(elapsed_seconds // 3600)
    minutes = int((elapsed_seconds % 3600) // 60)
    seconds = elapsed_seconds % 60
    print("Ordered pipeline complete.")
    print(f"Total runtime: {hours:02d}:{minutes:02d}:{seconds:06.3f} (hh:mm:ss.sss)")
