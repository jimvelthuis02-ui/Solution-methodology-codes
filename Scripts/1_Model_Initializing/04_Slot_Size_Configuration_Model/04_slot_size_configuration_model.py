import csv
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INPUT_PREPARED = ROOT / "Output" / "01_Data_Preparation" / "Location_Details_Prepared.csv"
INPUT_SCENARIOS = ROOT / "Output" / "02_Scenario_Generation" / "02_Item_Height_Scenarios_Delta_Weighted.csv"
INPUT_LOCATION_BEAM_MAP = ROOT / "Output" / "01_Data_Preparation" / "Beam_Grid_Mapping" / "Location_Beam_Map.csv"
SLOT_SIZE_ROOT = ROOT / "Output" / "03_Slot_Size_Generation"
OUTPUT_DIR = ROOT / "Output" / "04_Slot_Size_Configuration_Model"

METHODS = ("quantile_binning", "hierarchical_clustering", "kmeans_clustering")
MAX_OCCUPANCY_RATE = 0.85
ENFORCE_OCCUPANCY_RATE_CONSTRAINT = False
BASE_SKU_COUNT = 843
SKU_SCENARIO_FACTORS = {
    "Low_Count": 0.9,
    "Base_Count": 1.0,
    "High_Count": 1.1,
}
MIN_HIGH_NON_OCCUPIED_SHARE = 0.50
ENFORCE_MIN_HIGH_NON_OCCUPIED_CONSTRAINT = False
HIGH_SLOT_THRESHOLD = 99.0

RACK_EXPECTED_COLUMNS = {
    "A": 16,
    "B": 22,
    "D": 22,
    "E": 22,
    "F": 22,
    "G": 22,
    "H": 22,
    "I": 22,
    "J": 22,
    "K": 22,
}
EXPECTED_RACK_COUNT = len(RACK_EXPECTED_COLUMNS)
COLUMN_MAX_HEIGHT = 770.0
TOP_BEAM_HEIGHT = 16.0
MAX_USED_HEIGHT_BASE = COLUMN_MAX_HEIGHT - TOP_BEAM_HEIGHT
MIN_BEAMS_PER_COLUMN = 3
BEAM_HEIGHT = 16.0
LOCATION_CODE_PATTERN = re.compile(r"^([A-Z])(\d{2})(\d{2})([A-Za-z]+)?$")


@dataclass(frozen=True)
class CapacityUnit:
    unit_id: str
    unit_type: str
    size: int
    min_height: float
    rack: str
    columns: tuple[int, ...]
    locations: tuple[str, ...]


def _to_float(value: object | None) -> float | None:
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
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header row: {path}")
        return list(reader)


def _parse_location_code(location: str) -> tuple[str, int, int] | None:
    match = LOCATION_CODE_PATTERN.match(location)
    if not match:
        return None
    return match.group(1), int(match.group(2)), int(match.group(3))


def _is_split_location(location: str) -> bool:
    match = LOCATION_CODE_PATTERN.match(location)
    if not match:
        return False
    suffix = match.group(4)
    return suffix is not None and suffix.strip() != ""


def _static_checks(
    prepared_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    checks: list[dict[str, str]] = []
    invalid_location_rows: list[dict[str, str]] = []
    violating_column_rows: list[dict[str, str]] = []

    parsed = []
    invalid_codes = 0
    for row in prepared_rows:
        location = str(row.get("Location", "")).strip()
        parsed_code = _parse_location_code(location)
        if parsed_code is None:
            invalid_codes += 1
            invalid_location_rows.append({"Location": location})
        else:
            parsed.append((location, parsed_code[0], parsed_code[1], parsed_code[2], row))

    checks.append({"Constraint": "Location code pattern Rccrr", "Status": "PASS" if invalid_codes == 0 else "FAIL", "Details": f"Invalid codes: {invalid_codes}"})

    racks_present = sorted({rack for _, rack, _, _, _ in parsed})
    checks.append({"Constraint": "Rack count fixed", "Status": "PASS" if len(racks_present) == EXPECTED_RACK_COUNT else "FAIL", "Details": f"Observed racks: {racks_present}"})

    for rack, expected_columns in RACK_EXPECTED_COLUMNS.items():
        observed_columns = {column for _, r, column, _, _ in parsed if r == rack}
        checks.append(
            {
                "Constraint": f"Rack {rack} column count",
                "Status": "PASS" if len(observed_columns) == expected_columns else "FAIL",
                "Details": f"Expected={expected_columns}, Observed={len(observed_columns)}",
            }
        )

    column_groups: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for _, rack, column, _, row in parsed:
        column_groups[(rack, column)].append(row)

    violating_columns = 0
    for (rack, column), rows in column_groups.items():
        slot_heights = [_to_float(row.get("Location height")) for row in rows]
        slot_heights = [height for height in slot_heights if height is not None]
        beam_count = max(len(rows) - 1, MIN_BEAMS_PER_COLUMN)
        max_used_height = MAX_USED_HEIGHT_BASE - beam_count * BEAM_HEIGHT
        used_height = sum(slot_heights)
        if used_height > max_used_height + 1e-9:
            violating_columns += 1
            violating_column_rows.append(
                {
                    "Rack": rack,
                    "Column": f"{column:02d}",
                    "Location_Count": str(len(rows)),
                    "Beam_Count_Used": str(beam_count),
                    "Allowed_Used_Height": f"{max_used_height:.3f}",
                    "Actual_Used_Height": f"{used_height:.3f}",
                    "Excess_Height": f"{used_height - max_used_height:.3f}",
                    "Locations": ",".join(str(row.get("Location", "")).strip() for row in rows),
                    "Location_Heights": ",".join(
                        "" if _to_float(row.get("Location height")) is None else f"{_to_float(row.get('Location height')):g}"
                        for row in rows
                    ),
                }
            )

    checks.append({"Constraint": "Column max used height (754 - 16 * beam_count, min 3 beams)", "Status": "PASS" if violating_columns == 0 else "FAIL", "Details": f"Violating columns: {violating_columns}"})

    doorgang_rows = [row for row in prepared_rows if str(row.get("Location Type", "")).strip().lower() == "doorgang"]
    checks.append({"Constraint": "Doorgang heights fixed", "Status": "ASSUMED", "Details": f"Doorgang locations tracked: {len(doorgang_rows)}"})

    return checks, invalid_location_rows, violating_column_rows


def _load_method_rows(method: str, file_name: str) -> list[dict[str, str]]:
    return _read_csv(SLOT_SIZE_ROOT / method / file_name)


def _slot_size_variable_name(slot_size: float) -> str:
    return f"x_{int(round(slot_size))}"


def _to_fraction(value: object | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
        number = _to_float(text)
        return None if number is None else number / 100.0
    return _to_float(text)


def _allocate_counts_from_percentages(total_count: int, percentages: list[float]) -> list[int]:
    """Allocate integer counts from percentages while preserving the exact total."""
    raw = [max(0.0, percentage) * total_count for percentage in percentages]
    floors = [int(math.floor(value)) for value in raw]
    remainder = max(total_count - sum(floors), 0)
    ranked = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)

    for index in ranked[:remainder]:
        floors[index] += 1

    return floors


def _build_capacity_units(
    prepared_rows: list[dict[str, str]],
    beam_map_rows: list[dict[str, str]],
) -> list[CapacityUnit]:
    prepared_by_location = {
        str(row.get("Location", "")).strip(): row
        for row in prepared_rows
    }

    beam_locations: set[str] = set()
    beam_groups: dict[str, list[str]] = defaultdict(list)

    for row in beam_map_rows:
        location = str(row.get("Location", "")).strip()
        if location and _is_split_location(location):
            continue
        beam_supported = str(row.get("Beam_Supported", "")).strip().upper() == "YES"
        has_grid = str(row.get("Has_Grid", "")).strip().upper() == "YES"
        coordinate = str(row.get("Beam_Coordinate", "")).strip()

        if not location or not beam_supported or not has_grid or coordinate == "":
            continue

        prepared = prepared_by_location.get(location)
        if prepared is None:
            continue

        location_type = str(prepared.get("Location Type", "")).strip().lower()
        if location_type == "doorgang":
            continue

        beam_locations.add(location)
        beam_groups[coordinate].append(location)

    units: list[CapacityUnit] = []

    for coordinate, group_locations in beam_groups.items():
        unique_locations = sorted(set(group_locations))
        heights = [_to_float(prepared_by_location[loc].get("Location height")) for loc in unique_locations]
        heights = [height for height in heights if height is not None]
        if not heights:
            continue

        parsed_codes = [_parse_location_code(loc) for loc in unique_locations]
        parsed_codes = [parsed for parsed in parsed_codes if parsed is not None]
        if not parsed_codes:
            continue

        rack = parsed_codes[0][0]
        columns = tuple(sorted({parsed[1] for parsed in parsed_codes}))

        units.append(
            CapacityUnit(
                unit_id=f"BEAM::{coordinate}",
                unit_type="beam",
                size=len(unique_locations),
                min_height=min(heights),
                rack=rack,
                columns=columns,
                locations=tuple(unique_locations),
            )
        )

    for row in prepared_rows:
        location = str(row.get("Location", "")).strip()
        if location == "" or location in beam_locations:
            continue
        if _is_split_location(location):
            continue

        location_type = str(row.get("Location Type", "")).strip().lower()
        if location_type == "doorgang":
            continue

        location_height = _to_float(row.get("Location height"))
        parsed = _parse_location_code(location)
        if location_height is None or parsed is None:
            continue

        rack, column, _ = parsed
        units.append(
            CapacityUnit(
                unit_id=f"FLOOR::{location}",
                unit_type="floor",
                size=1,
                min_height=location_height,
                rack=rack,
                columns=(column,),
                locations=(location,),
            )
        )

    return units


def _build_feasible_solution_for_combo(
    requirement_rows: list[dict[str, str]],
    units: list[CapacityUnit],
    high_slot_threshold: float,
    required_high_non_occupied: int,
) -> tuple[bool, dict[float, int], dict[float, int], int, int, str, dict[str, int]]:
    slot_sizes = sorted(
        {
            value
            for row in requirement_rows
            if (value := _to_float(row.get("Representative_Slot_Size"))) is not None
        }
    )
    if not slot_sizes:
        return False, {}, {}, 0, 0, "No slot sizes found for combination.", {"beam": 0, "floor": 0}

    min_at_or_above = {
        slot_size: int(round(_to_float(next(row for row in requirement_rows if _to_float(row.get("Representative_Slot_Size")) == slot_size).get("Min_Required_Locations_At_Or_Above_Size")) or 0.0))
        for slot_size in slot_sizes
    }

    assigned_by_slot: dict[float, int] = defaultdict(int)
    used_units: set[str] = set()
    unit_by_id = {unit.unit_id: unit for unit in units}

    slot_desc = sorted(slot_sizes, reverse=True)
    for slot_size in slot_desc:
        current_at_or_above = sum(count for size, count in assigned_by_slot.items() if size >= slot_size)
        required = min_at_or_above[slot_size]
        deficit = required - current_at_or_above
        if deficit <= 0:
            continue

        candidates = [
            unit
            for unit in units
            if unit.unit_id not in used_units
        ]
        candidates.sort(key=lambda unit: unit.size)

        for unit in candidates:
            used_units.add(unit.unit_id)
            assigned_by_slot[slot_size] += unit.size
            deficit -= unit.size
            if deficit <= 0:
                break

        if deficit > 0:
            return False, dict(assigned_by_slot), {}, 0, 0, f"Insufficient capacity for slot size >= {slot_size:.0f}.", {"beam": 0, "floor": 0}

    achieved_at_or_above = {
        slot_size: sum(count for size, count in assigned_by_slot.items() if size >= slot_size)
        for slot_size in slot_sizes
    }

    achieved_high_total = sum(count for size, count in assigned_by_slot.items() if size >= high_slot_threshold)

    high_slot_candidates = [size for size in slot_sizes if size >= high_slot_threshold]
    if high_slot_candidates:
        threshold_slot = min(high_slot_candidates)
        sku_high_demand = int(round(_to_float(next(row for row in requirement_rows if _to_float(row.get("Representative_Slot_Size")) == threshold_slot).get("Cumulative_Assigned_SKUs_At_Or_Above_Size")) or 0.0))
    else:
        sku_high_demand = 0

    achieved_high_non_occupied = max(achieved_high_total - sku_high_demand, 0)
    high_constraint_status = "ENFORCED" if ENFORCE_MIN_HIGH_NON_OCCUPIED_CONSTRAINT else "DISABLED"
    note = ""

    if achieved_high_non_occupied < required_high_non_occupied:
        extra_needed = required_high_non_occupied - achieved_high_non_occupied
        extra_candidates = [
            unit
            for unit in units
            if unit.unit_id not in used_units
        ]
        extra_candidates.sort(key=lambda unit: unit.size)

        if high_slot_candidates:
            target_slot = min(high_slot_candidates)
            for unit in extra_candidates:
                used_units.add(unit.unit_id)
                assigned_by_slot[target_slot] += unit.size
                extra_needed -= unit.size
                if extra_needed <= 0:
                    break

            achieved_at_or_above = {
                slot_size: sum(count for size, count in assigned_by_slot.items() if size >= slot_size)
                for slot_size in slot_sizes
            }
            achieved_high_total = sum(count for size, count in assigned_by_slot.items() if size >= high_slot_threshold)
            achieved_high_non_occupied = max(achieved_high_total - sku_high_demand, 0)

        if achieved_high_non_occupied < required_high_non_occupied:
            high_constraint_status = "RELAXED_INFEASIBLE"
            note = "High non-occupied big-location minimum could not be met and was relaxed."

    unit_usage = {"beam": 0, "floor": 0}
    for unit_id in used_units:
        unit_type = unit_by_id[unit_id].unit_type
        unit_usage[unit_type] = unit_usage.get(unit_type, 0) + 1

    return True, dict(assigned_by_slot), achieved_at_or_above, achieved_high_non_occupied, achieved_high_total, high_constraint_status if note == "" else f"{high_constraint_status}: {note}", unit_usage


def _build_location_metadata(prepared_rows: list[dict[str, str]], beam_map_rows: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    location_meta: dict[str, dict[str, str]] = {}
    for row in prepared_rows:
        location = str(row.get("Location", "")).strip()
        if location == "":
            continue
        location_meta[location] = {
            "Rack": str(row.get("Rack", "")).strip(),
            "Column": str(row.get("Column", "")).strip(),
            "Row": str(row.get("Row", "")).strip(),
        }

    beam_by_location: dict[str, str] = {}
    for row in beam_map_rows:
        location = str(row.get("Location", "")).strip()
        if location == "":
            continue
        beam_by_location[location] = str(row.get("Beam_Coordinate", "")).strip()

    return location_meta, beam_by_location


def _build_column_capacity(prepared_rows: list[dict[str, str]]) -> tuple[dict[str, float], dict[str, int], dict[str, int]]:
    rows_by_column: dict[str, int] = defaultdict(int)
    for row in prepared_rows:
        location_type = str(row.get("Location Type", "")).strip().lower()
        if location_type == "doorgang":
            continue
        location = str(row.get("Location", "")).strip()
        if location and _is_split_location(location):
            continue
        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        if rack == "" or column == "":
            continue
        key = f"{rack}{column}"
        rows_by_column[key] += 1

    allowed_used_height: dict[str, float] = {}
    beam_count_by_column: dict[str, int] = {}
    for key, row_count in rows_by_column.items():
        beam_count = max(row_count - 1, MIN_BEAMS_PER_COLUMN)
        beam_count_by_column[key] = beam_count
        allowed_used_height[key] = MAX_USED_HEIGHT_BASE - beam_count * BEAM_HEIGHT

    return allowed_used_height, beam_count_by_column, rows_by_column


def _unit_column_counts(unit: CapacityUnit) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for location in unit.locations:
        parsed = _parse_location_code(location)
        if parsed is None:
            continue
        rack, column, _ = parsed
        counts[f"{rack}{column:02d}"] += 1
    return dict(counts)


def _build_rack_row_groups(prepared_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    group_locations: dict[tuple[str, str], list[str]] = defaultdict(list)
    group_column_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    group_row_labels: dict[tuple[str, str], str] = {}

    for row in prepared_rows:
        location_type = str(row.get("Location Type", "")).strip().lower()
        if location_type == "doorgang":
            continue

        location = str(row.get("Location", "")).strip()
        if location and _is_split_location(location):
            continue
        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        row_label = str(row.get("Row", "")).strip()
        if location == "" or rack == "" or column == "":
            continue

        effective_row = row_label if row_label else f"LOC::{location}"
        key = (rack, effective_row)
        group_row_labels[key] = row_label
        group_locations[key].append(location)
        group_column_counts[key][f"{rack}{column}"] += 1

    result: list[dict[str, object]] = []
    for (rack, effective_row), locations_list in group_locations.items():
        locations = sorted({str(loc) for loc in locations_list})
        counts_map = group_column_counts.get((rack, effective_row), {})
        column_counts = {str(k): int(v) for k, v in counts_map.items()}

        result.append(
            {
                "Group_ID": f"RACKROW::{rack}::{effective_row}",
                "Rack": rack,
                "Row": group_row_labels.get((rack, effective_row), ""),
                "Locations": locations,
                "Column_Counts": column_counts,
                "Capacity": len(locations),
            }
        )

    result.sort(key=lambda group: (_to_int_default(group.get("Capacity"), 0), str(group.get("Group_ID", ""))))
    return result


def _allocate_layout_rack_standardized(
    target_exact_counts: dict[float, int],
    rack_row_groups: list[dict[str, object]],
    allowed_used_height: dict[str, float],
) -> tuple[bool, dict[float, int], dict[str, float], list[tuple[int, float]], str]:
    remaining_groups = set(range(len(rack_row_groups)))
    assigned_exact_counts: dict[float, int] = {slot_size: 0 for slot_size in target_exact_counts}
    used_height_by_column: dict[str, float] = defaultdict(float)
    assignments: list[tuple[int, float]] = []
    unmet_targets: dict[float, int] = {}

    for slot_size in sorted(target_exact_counts.keys(), reverse=True):
        target = int(target_exact_counts.get(slot_size, 0))
        if target <= 0:
            continue

        covered = 0
        while covered < target:
            remaining_need = target - covered
            candidates: list[tuple[int, int, float, int]] = []

            for group_index in remaining_groups:
                group = rack_row_groups[group_index]
                capacity = _to_int_default(group.get("Capacity"), 0)
                if capacity <= 0:
                    continue

                counts_obj = group.get("Column_Counts")
                if not isinstance(counts_obj, dict):
                    continue

                fits = True
                post_slack_sum = 0.0
                for column_key, count in counts_obj.items():
                    allowed = allowed_used_height.get(str(column_key))
                    if allowed is None:
                        fits = False
                        break
                    proposed = used_height_by_column[str(column_key)] + slot_size * _to_int_default(count, 0)
                    if proposed > allowed + 1e-9:
                        fits = False
                        break
                    post_slack_sum += allowed - proposed

                if not fits:
                    continue

                # Primary objective: close remaining demand. Secondary: avoid overshoot. Tertiary: preserve slack.
                candidates.append((abs(remaining_need - capacity), 1 if capacity > remaining_need else 0, int(post_slack_sum), group_index))

            if not candidates:
                unmet_targets[slot_size] = target - covered
                break

            candidates.sort(key=lambda item: (item[0], item[1], item[2]))
            chosen_index = candidates[0][3]
            chosen_group = rack_row_groups[chosen_index]
            chosen_capacity = _to_int_default(chosen_group.get("Capacity"), 0)

            counts_obj = chosen_group.get("Column_Counts")
            if isinstance(counts_obj, dict):
                for column_key, count in counts_obj.items():
                    used_height_by_column[str(column_key)] += slot_size * _to_int_default(count, 0)

            assigned_exact_counts[slot_size] += chosen_capacity
            assignments.append((chosen_index, slot_size))
            remaining_groups.remove(chosen_index)
            covered += chosen_capacity

    success = all(assigned_exact_counts.get(slot, 0) >= int(target_exact_counts.get(slot, 0)) for slot in target_exact_counts)
    if success:
        return True, assigned_exact_counts, dict(used_height_by_column), assignments, "Rack-standardized allocation succeeded."

    deviation = sum(abs(assigned_exact_counts.get(slot, 0) - int(target_exact_counts.get(slot, 0))) for slot in target_exact_counts)
    unmet_text = ", ".join(f"{int(slot)}:{count}" for slot, count in sorted(unmet_targets.items(), reverse=True))
    note = f"Rack-standardized best effort; unmet slot counts [{unmet_text}] ; total absolute deviation={deviation}."
    return False, assigned_exact_counts, dict(used_height_by_column), assignments, note


def _allocate_layout_for_combo(
    target_exact_counts: dict[float, int],
    units: list[CapacityUnit],
    allowed_used_height: dict[str, float],
) -> tuple[bool, dict[float, int], dict[str, float], list[tuple[int, float]], str]:
    remaining_units = set(range(len(units)))
    assigned_exact_counts: dict[float, int] = {slot_size: 0 for slot_size in target_exact_counts}
    used_height_by_column: dict[str, float] = defaultdict(float)
    unit_assignments: list[tuple[int, float]] = []

    slot_sizes_desc = sorted(target_exact_counts.keys(), reverse=True)
    for slot_size in slot_sizes_desc:
        needed = int(target_exact_counts.get(slot_size, 0))
        while needed > 0:
            candidates: list[tuple[float, int, int]] = []
            for unit_index in remaining_units:
                unit = units[unit_index]
                if unit.size > needed:
                    continue

                unit_columns = _unit_column_counts(unit)
                fits = True
                post_slack_sum = 0.0
                for column_key, count in unit_columns.items():
                    allowed = allowed_used_height.get(column_key)
                    if allowed is None:
                        fits = False
                        break
                    proposed = used_height_by_column[column_key] + slot_size * count
                    if proposed > allowed + 1e-9:
                        fits = False
                        break
                    post_slack_sum += allowed - proposed

                if fits:
                    # Prefer larger unit sizes first, then best fit (lower remaining slack after placement).
                    candidates.append((post_slack_sum, -unit.size, unit_index))

            if not candidates:
                return False, assigned_exact_counts, dict(used_height_by_column), unit_assignments, f"No allocatable unit found for slot size {slot_size:.0f} with remaining demand {needed}."

            candidates.sort(key=lambda item: (item[1], item[0]))
            chosen_index = candidates[0][2]
            chosen_unit = units[chosen_index]

            for column_key, count in _unit_column_counts(chosen_unit).items():
                used_height_by_column[column_key] += slot_size * count

            assigned_exact_counts[slot_size] += chosen_unit.size
            unit_assignments.append((chosen_index, slot_size))
            remaining_units.remove(chosen_index)
            needed -= chosen_unit.size

    return True, assigned_exact_counts, dict(used_height_by_column), unit_assignments, "OK"


def _generate_layout_distribution_outputs(
    feasible_slot_rows: list[dict[str, str]],
    units: list[CapacityUnit],
    prepared_rows: list[dict[str, str]],
    beam_map_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    location_meta, beam_by_location = _build_location_metadata(prepared_rows, beam_map_rows)
    allowed_used_height, beam_count_by_column, _ = _build_column_capacity(prepared_rows)
    rack_row_groups = _build_rack_row_groups(prepared_rows)

    grouped_targets: dict[tuple[str, str, str, str], dict[float, int]] = defaultdict(dict)
    for row in feasible_slot_rows:
        key = (
            str(row.get("Method", "")),
            str(row.get("Scenario", "")),
            str(row.get("K", "")),
            str(row.get("SKU_Scenario", "")),
        )
        slot_size = _to_float(row.get("Representative_Slot_Size"))
        exact_count = int(round(_to_float(row.get("Assigned_Locations_Exact_Size")) or 0.0))
        if slot_size is None:
            continue
        grouped_targets[key][slot_size] = exact_count

    summary_rows: list[dict[str, str]] = []
    column_rows: list[dict[str, str]] = []
    location_rows: list[dict[str, str]] = []

    for key, target_counts in grouped_targets.items():
        method, scenario, k, sku_scenario = key
        success, assigned_exact, used_by_column, unit_assignments, note = _allocate_layout_rack_standardized(
            target_exact_counts=target_counts,
            rack_row_groups=rack_row_groups,
            allowed_used_height=allowed_used_height,
        )

        assignment_by_location: dict[str, tuple[str, str, str, float]] = {}
        slot_mix_by_column: dict[str, dict[float, int]] = defaultdict(lambda: defaultdict(int))
        for unit_index, slot_size in unit_assignments:
            group = rack_row_groups[unit_index]
            locations_obj = group.get("Locations")
            locations = locations_obj if isinstance(locations_obj, list) else []
            group_id = str(group.get("Group_ID", ""))

            for location in locations:
                meta = location_meta.get(location, {"Rack": "", "Column": "", "Row": ""})
                assignment_by_location[location] = (
                    group_id,
                    "rack_row",
                    beam_by_location.get(location, ""),
                    slot_size,
                )
                rack = meta.get("Rack", "")
                column = meta.get("Column", "")
                if rack and column:
                    slot_mix_by_column[f"{rack}{column}"][slot_size] += 1

        total_assigned = sum(assigned_exact.values())
        total_target = sum(target_counts.values())
        avg_fill = 0.0
        if used_by_column:
            avg_fill = sum((used_by_column.get(col, 0.0) / cap) for col, cap in allowed_used_height.items() if cap > 0) / max(len(allowed_used_height), 1)

        summary_rows.append(
            {
                "Method": method,
                "Scenario": scenario,
                "K": k,
                "SKU_Scenario": sku_scenario,
                "Layout_Feasible": "YES" if success else "NO",
                "Assigned_Locations_Total": str(total_assigned),
                "Target_Locations_Total": str(total_target),
                "Average_Column_Fill_Ratio": f"{avg_fill:.4f}",
                "Notes": note,
            }
        )

        for column_key, allowed in sorted(allowed_used_height.items()):
            used = used_by_column.get(column_key, 0.0)
            mix = slot_mix_by_column.get(column_key, {})
            mix_text = "|".join(
                f"{int(slot)}:{count}" for slot, count in sorted(mix.items())
            )
            column_rows.append(
                {
                    "Method": method,
                    "Scenario": scenario,
                    "K": k,
                    "SKU_Scenario": sku_scenario,
                    "Rack_Column": column_key,
                    "Beam_Count_Used": str(beam_count_by_column.get(column_key, 0)),
                    "Allowed_Used_Height_cm": f"{allowed:.3f}",
                    "Assigned_Used_Height_cm": f"{used:.3f}",
                    "Fill_Ratio": f"{(used / allowed) if allowed > 0 else 0.0:.4f}",
                    "Remaining_Height_cm": f"{max(allowed - used, 0.0):.3f}",
                    "Slot_Size_Distribution": mix_text,
                }
            )

        for location, (unit_id, unit_type, beam_coordinate, slot_size) in sorted(assignment_by_location.items()):
            meta = location_meta.get(location, {"Rack": "", "Column": "", "Row": ""})
            location_rows.append(
                {
                    "Method": method,
                    "Scenario": scenario,
                    "K": k,
                    "SKU_Scenario": sku_scenario,
                    "Location": location,
                    "Rack": meta.get("Rack", ""),
                    "Column": meta.get("Column", ""),
                    "Row": meta.get("Row", ""),
                    "Beam_Coordinate": beam_coordinate,
                    "Assignment_Unit_ID": unit_id,
                    "Assignment_Unit_Type": unit_type,
                    "Assigned_Slot_Size_cm": f"{slot_size:.0f}",
                }
            )

    return summary_rows, column_rows, location_rows


def _load_capacity_units(prepared_rows: list[dict[str, str]]) -> tuple[list[dict[str, object]], dict[str, int]]:
    """
    Build allocatable capacity units.
    - Beam-supported locations are coupled by Beam_Coordinate (assigned as indivisible blocks).
    - Non-beam locations are single-location units.
    - Doorgang locations are excluded from capacity.
    """
    beam_map_rows = _read_csv(INPUT_LOCATION_BEAM_MAP)
    prepared_by_location = {
        str(row.get("Location", "")).strip(): row
        for row in prepared_rows
        if str(row.get("Location", "")).strip()
    }

    grouped_locations: dict[str, list[str]] = defaultdict(list)
    for map_row in beam_map_rows:
        location = str(map_row.get("Location", "")).strip()
        if location == "" or location not in prepared_by_location:
            continue

        prepared_row = prepared_by_location[location]
        location_type = str(prepared_row.get("Location Type", "")).strip().lower()
        if location_type == "doorgang":
            continue

        beam_supported = str(map_row.get("Beam_Supported", "")).strip().upper() == "YES"
        has_grid = str(map_row.get("Has_Grid", "")).strip().upper() == "YES"
        beam_coordinate = str(map_row.get("Beam_Coordinate", "")).strip()

        if beam_supported and has_grid and beam_coordinate:
            unit_id = f"BEAM::{beam_coordinate}"
        else:
            unit_id = f"LOC::{location}"
        grouped_locations[unit_id].append(location)

    units: list[dict[str, object]] = []
    rack_column_capacity: dict[str, int] = defaultdict(int)

    for unit_id, locations in grouped_locations.items():
        heights = []
        rack_column_counts: dict[str, int] = defaultdict(int)

        for location in locations:
            row = prepared_by_location[location]
            location_height = _to_float(row.get("Location height"))
            if location_height is not None:
                heights.append(location_height)

            rack = str(row.get("Rack", "")).strip()
            column = str(row.get("Column", "")).strip()
            if rack and column:
                rack_column_key = f"{rack}{column}"
                rack_column_counts[rack_column_key] += 1
                rack_column_capacity[rack_column_key] += 1

        if not heights:
            continue

        # Use the minimum location height in the unit as the safe assignable bound.
        units.append(
            {
                "Unit_ID": unit_id,
                "Height": min(heights),
                "Capacity": len(locations),
                "Locations": sorted(locations),
                "Rack_Column_Counts": dict(rack_column_counts),
                "Is_Beam_Coupled": unit_id.startswith("BEAM::"),
            }
        )

    units.sort(key=lambda row: (_to_float(row.get("Height")) or 0.0, _to_int_default(row.get("Capacity"), 0)))
    return units, dict(rack_column_capacity)


def _cumulative_count_at_or_above(counts_by_slot_size: dict[float, int], slot_sizes: list[float], slot_size: float) -> int:
    return sum(counts_by_slot_size.get(candidate, 0) for candidate in slot_sizes if candidate >= slot_size)


def _smallest_eligible_slot(slot_sizes: list[float], min_slot_size: float) -> float | None:
    candidates = [slot for slot in slot_sizes if slot >= min_slot_size]
    return min(candidates) if candidates else None


def _attempt_constructive_allocation(
    units: list[dict[str, object]],
    slot_sizes: list[float],
    min_required_by_slot_size: dict[float, int],
    high_slot_threshold: float,
    required_high_non_occupied: int,
    enforce_high_non_occupied: bool,
) -> tuple[bool, dict[float, int], list[tuple[int, float]], str]:
    assigned_counts = {slot_size: 0 for slot_size in slot_sizes}
    assigned_units: list[tuple[int, float]] = []
    remaining_indices = set(range(len(units)))

    for slot_size in sorted(slot_sizes, reverse=True):
        already_covered = _cumulative_count_at_or_above(assigned_counts, slot_sizes, slot_size)
        needed = max(0, min_required_by_slot_size.get(slot_size, 0) - already_covered)
        if needed <= 0:
            continue

        eligible = [
            index
            for index in remaining_indices
        ]
        eligible.sort(
            key=lambda index: (
                _to_int_default(units[index].get("Capacity"), 0),
            )
        )

        covered = 0
        for index in eligible:
            assigned_counts[slot_size] += _to_int_default(units[index].get("Capacity"), 0)
            assigned_units.append((index, slot_size))
            remaining_indices.remove(index)
            covered += _to_int_default(units[index].get("Capacity"), 0)
            if covered >= needed:
                break

        if covered < needed:
            return False, assigned_counts, assigned_units, f"Insufficient capacity for slot size >= {slot_size:.0f}"

    if enforce_high_non_occupied and required_high_non_occupied > 0:
        currently_high = sum(count for slot_size, count in assigned_counts.items() if slot_size >= high_slot_threshold)
        need_high = max(0, required_high_non_occupied - currently_high)

        if need_high > 0:
            extra_candidates: list[tuple[int, float]] = []
            for index in remaining_indices:
                target_slot = _smallest_eligible_slot(slot_sizes, high_slot_threshold)
                if target_slot is not None:
                    extra_candidates.append((index, target_slot))

            extra_candidates.sort(
                key=lambda item: (
                    _to_int_default(units[item[0]].get("Capacity"), 0),
                )
            )

            covered = 0
            for index, target_slot in extra_candidates:
                assigned_counts[target_slot] += _to_int_default(units[index].get("Capacity"), 0)
                assigned_units.append((index, target_slot))
                remaining_indices.remove(index)
                covered += _to_int_default(units[index].get("Capacity"), 0)
                if covered >= need_high:
                    break

            if covered < need_high:
                return False, assigned_counts, assigned_units, "Insufficient high-slot capacity"

    for slot_size in slot_sizes:
        if _cumulative_count_at_or_above(assigned_counts, slot_sizes, slot_size) < min_required_by_slot_size.get(slot_size, 0):
            return False, assigned_counts, assigned_units, f"Coverage check failed at slot {slot_size:.0f}"

    return True, assigned_counts, assigned_units, "OK"


def _generate_feasible_solutions(
    slot_size_constraint_rows: list[dict[str, str]],
    model_rows: list[dict[str, str]],
    units: list[dict[str, object]],
    rack_column_capacity: dict[str, int],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    model_index = {
        (
            str(row.get("Method", "")),
            str(row.get("Scenario", "")),
            str(row.get("K", "")),
            str(row.get("SKU_Scenario", "")),
        ): row
        for row in model_rows
    }

    grouped_constraints: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in slot_size_constraint_rows:
        key = (
            str(row.get("Method", "")),
            str(row.get("Scenario", "")),
            str(row.get("K", "")),
            str(row.get("SKU_Scenario", "")),
        )
        grouped_constraints[key].append(row)

    summary_rows: list[dict[str, str]] = []
    slot_solution_rows: list[dict[str, str]] = []
    rack_column_rows: list[dict[str, str]] = []

    total_capacity = sum(_to_int_default(unit.get("Capacity"), 0) for unit in units)

    for key, constraint_rows in grouped_constraints.items():
        method, scenario, k, sku_scenario = key
        model_row = model_index.get(key, {})

        high_threshold = _to_float(model_row.get("High_Slot_Threshold_cm")) or HIGH_SLOT_THRESHOLD
        required_high_non_occupied = int(round(_to_float(model_row.get("Required_High_Non_Occupied_Count_At_Minimum")) or 0.0))

        slot_rows_sorted = sorted(
            constraint_rows,
            key=lambda row: _to_float(row.get("Representative_Slot_Size")) or 0.0,
        )
        slot_sizes = [
            (_to_float(row.get("Representative_Slot_Size")) or 0.0)
            for row in slot_rows_sorted
        ]
        min_required_by_slot_size = {
            (_to_float(row.get("Representative_Slot_Size")) or 0.0): int(round(_to_float(row.get("Min_Required_Locations_At_Or_Above_Size")) or 0.0))
            for row in slot_rows_sorted
        }

        success, assigned_counts, assigned_units, note = _attempt_constructive_allocation(
            units=units,
            slot_sizes=slot_sizes,
            min_required_by_slot_size=min_required_by_slot_size,
            high_slot_threshold=high_threshold,
            required_high_non_occupied=required_high_non_occupied,
            enforce_high_non_occupied=True,
        )

        high_constraint_applied = "YES"
        if not success:
            success, assigned_counts, assigned_units, fallback_note = _attempt_constructive_allocation(
                units=units,
                slot_sizes=slot_sizes,
                min_required_by_slot_size=min_required_by_slot_size,
                high_slot_threshold=high_threshold,
                required_high_non_occupied=required_high_non_occupied,
                enforce_high_non_occupied=False,
            )
            if success:
                high_constraint_applied = "NO (relaxed)"
                note = f"High-slot rule relaxed: {note}"
            else:
                note = f"{note}; fallback without high-slot rule failed: {fallback_note}"

        used_rack_columns: dict[str, int] = defaultdict(int)
        for unit_index, _slot_size in assigned_units:
            unit = units[unit_index]
            rack_column_counts = unit.get("Rack_Column_Counts", {})
            if isinstance(rack_column_counts, dict):
                for rack_column, count in rack_column_counts.items():
                    used_rack_columns[str(rack_column)] += int(count)

        total_assigned = sum(assigned_counts.values())
        summary_rows.append(
            {
                "Method": method,
                "Scenario": scenario,
                "K": k,
                "SKU_Scenario": sku_scenario,
                "Feasible": "YES" if success else "NO",
                "High_Non_Occupied_Constraint_Applied": high_constraint_applied if success else "N/A",
                "Assigned_Total_Locations": str(total_assigned),
                "Available_Total_Locations": str(total_capacity),
                "Assigned_Beam_Coupled_Units": str(sum(1 for unit_index, _ in assigned_units if bool(units[unit_index].get("Is_Beam_Coupled")))),
                "Assigned_Units_Total": str(len(assigned_units)),
                "Notes": note,
            }
        )

        for slot_size in slot_sizes:
            min_required = min_required_by_slot_size.get(slot_size, 0)
            cumulative_assigned = _cumulative_count_at_or_above(assigned_counts, slot_sizes, slot_size)
            slot_solution_rows.append(
                {
                    "Method": method,
                    "Scenario": scenario,
                    "K": k,
                    "SKU_Scenario": sku_scenario,
                    "Representative_Slot_Size": f"{slot_size:.0f}",
                    "Assigned_Locations_At_Exact_Size": str(assigned_counts.get(slot_size, 0)),
                    "Min_Required_Locations_At_Or_Above_Size": str(min_required),
                    "Assigned_Locations_At_Or_Above_Size": str(cumulative_assigned),
                    "Slack_At_Or_Above_Size": str(cumulative_assigned - min_required),
                    "Coverage_Constraint_Met": "YES" if cumulative_assigned >= min_required else "NO",
                }
            )

        for rack_column, capacity in sorted(rack_column_capacity.items()):
            used = used_rack_columns.get(rack_column, 0)
            rack_column_rows.append(
                {
                    "Method": method,
                    "Scenario": scenario,
                    "K": k,
                    "SKU_Scenario": sku_scenario,
                    "Rack_Column": rack_column,
                    "Used_Locations": str(used),
                    "Capacity_Locations": str(capacity),
                    "Within_Capacity": "YES" if used <= capacity else "NO",
                }
            )

    return summary_rows, slot_solution_rows, rack_column_rows


def _build_sku_count_scenarios(scenario_rows: list[dict[str, str]]) -> dict[str, int]:
    """Build low/base/high SKU-count scenarios from a fixed base SKU count."""
    _ = scenario_rows
    base_count = BASE_SKU_COUNT
    return {
        name: int(round(base_count * factor))
        for name, factor in SKU_SCENARIO_FACTORS.items()
    }


def _method_constraint_rows(
    method: str,
    summaries: list[dict[str, str]],
    sku_count_scenarios: dict[str, int],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    slot_size_rows: list[dict[str, str]] = []
    combos = sorted({(row["Scenario"], row["K"]) for row in summaries})

    for scenario, k in combos:
        combo_summaries = [row for row in summaries if row.get("Scenario") == scenario and row.get("K") == k]

        slot_size_clusters: list[tuple[float, int, float]] = []
        for row in combo_summaries:
            slot_size = _to_float(row.get("Representative Slot Size"))
            cluster_count = _to_float(row.get("Cluster Count"))
            cluster_percentage = _to_fraction(row.get("Cluster Count Percentage"))
            if slot_size is None or cluster_count is None:
                continue
            if cluster_percentage is None:
                # Backward compatibility: derive percentages when absent.
                cluster_percentage = 0.0
            count = int(round(cluster_count))
            slot_size_clusters.append((slot_size, count, cluster_percentage))

        if not slot_size_clusters:
            continue

        # Normalize percentages if they are missing or do not sum to 1 exactly.
        percentage_sum = sum(percentage for _, _, percentage in slot_size_clusters)
        if percentage_sum <= 0:
            total_cluster_count = sum(count for _, count, _ in slot_size_clusters)
            if total_cluster_count <= 0:
                continue
            slot_size_clusters = [
                (slot_size, count, count / total_cluster_count)
                for slot_size, count, _ in slot_size_clusters
            ]
        else:
            slot_size_clusters = [
                (slot_size, count, percentage / percentage_sum)
                for slot_size, count, percentage in slot_size_clusters
            ]

        slot_size_clusters.sort(key=lambda item: item[0])
        slot_sizes = [slot_size for slot_size, _, _ in slot_size_clusters]
        slot_size_text = ",".join(f"{value:.0f}" for value in slot_sizes)

        for sku_scenario_name, sku_count in sku_count_scenarios.items():
            allocated_counts = _allocate_counts_from_percentages(
                sku_count,
                [percentage for _, _, percentage in slot_size_clusters],
            )

            running_demand = 0
            cumulative_skus_by_slot_size: dict[float, int] = {}
            min_locations_by_slot_size: dict[float, int] = {}
            for index in range(len(slot_size_clusters) - 1, -1, -1):
                slot_size = slot_size_clusters[index][0]
                running_demand += allocated_counts[index]
                cumulative_skus_by_slot_size[slot_size] = running_demand
                if ENFORCE_OCCUPANCY_RATE_CONSTRAINT:
                    min_locations_by_slot_size[slot_size] = math.ceil(running_demand / MAX_OCCUPANCY_RATE)
                else:
                    min_locations_by_slot_size[slot_size] = running_demand

            for index, (slot_size, _, percentage) in enumerate(slot_size_clusters):
                eligible_sizes = [candidate for candidate in slot_sizes if candidate >= slot_size]
                decision_terms = " + ".join(_slot_size_variable_name(candidate) for candidate in eligible_sizes)
                assigned_skus = allocated_counts[index]
                cumulative_skus = cumulative_skus_by_slot_size[slot_size]
                min_locations_at_or_above = min_locations_by_slot_size[slot_size]

                slot_size_rows.append(
                    {
                        "Method": method,
                        "Scenario": scenario,
                        "K": k,
                        "SKU_Scenario": sku_scenario_name,
                        "SKU_Count": str(sku_count),
                        "Representative_Slot_Size": f"{slot_size:.0f}",
                        "Cluster_Count_Percentage": f"{percentage * 100:.2f}%",
                        "Assigned_SKUs_At_Representative_Size": str(assigned_skus),
                        "Decision_Variable": _slot_size_variable_name(slot_size),
                        "Cumulative_Assigned_SKUs_At_Or_Above_Size": str(cumulative_skus),
                        "Min_Required_Locations_At_Or_Above_Size": str(min_locations_at_or_above),
                        "Coverage_Constraint": f"{decision_terms} >= {min_locations_at_or_above}",
                    }
                )

            if ENFORCE_OCCUPANCY_RATE_CONSTRAINT:
                min_locations_required = math.ceil(sku_count / MAX_OCCUPANCY_RATE)
            else:
                min_locations_required = sku_count

            if ENFORCE_MIN_HIGH_NON_OCCUPIED_CONSTRAINT and ENFORCE_OCCUPANCY_RATE_CONSTRAINT:
                min_high_non_occupied = math.ceil(max(min_locations_required - sku_count, 0) * MIN_HIGH_NON_OCCUPIED_SHARE)
            else:
                min_high_non_occupied = 0

            rows.append(
                {
                    "Method": method,
                    "Scenario": scenario,
                    "K": k,
                    "SKU_Scenario": sku_scenario_name,
                    "SKU_Count": str(sku_count),
                    "Max_Occupancy_Rate": f"{MAX_OCCUPANCY_RATE:.2f}" if ENFORCE_OCCUPANCY_RATE_CONSTRAINT else "DISABLED",
                    "Required_Total_Locations_At_Max_Occupancy": str(min_locations_required),
                    "Total_Location_Decision": "sum(x_s)",
                    "Occupancy_Constraint": f"sum(x_s) >= {min_locations_required}",
                    "High_Slot_Threshold_cm": f"{HIGH_SLOT_THRESHOLD:.0f}",
                    "Required_High_Non_Occupied_Count_At_Minimum": str(min_high_non_occupied),
                    "High_Slot_Location_Decision": f"sum(x_s for s >= {HIGH_SLOT_THRESHOLD:.0f})",
                    "High_Non_Occupied_Constraint": f"sum(x_s for s >= {HIGH_SLOT_THRESHOLD:.0f}) >= {min_high_non_occupied}",
                    "Slot_Sizes": slot_size_text,
                    "Rack_Column_Division": "FIXED (validated in static checks)",
                    "Location_Coding": "FIXED (validated in static checks)",
                    "Beam_Coupling_Constraint": "REGISTERED (not solved)",
                    "Doorgang_Fixed_Heights": "REGISTERED (not solved)",
                }
            )

    return rows, slot_size_rows


def build_configuration_model_constraints() -> Path:
    prepared_rows = _read_csv(INPUT_PREPARED)
    scenario_rows = _read_csv(INPUT_SCENARIOS)
    beam_map_rows = _read_csv(ROOT / "Output" / "01_Data_Preparation" / "Beam_Grid_Mapping" / "Location_Beam_Map.csv")
    sku_count_scenarios = _build_sku_count_scenarios(scenario_rows)
    capacity_units = _build_capacity_units(prepared_rows, beam_map_rows)

    static_rows, invalid_location_rows, violating_column_rows = _static_checks(prepared_rows)
    model_rows: list[dict[str, str]] = []
    slot_size_constraint_rows: list[dict[str, str]] = []

    for method in METHODS:
        summaries = _load_method_rows(method, "Slot_Size_Configuration_Summary.csv")
        method_rows, method_slot_rows = _method_constraint_rows(method, summaries, sku_count_scenarios)
        model_rows.extend(method_rows)
        slot_size_constraint_rows.extend(method_slot_rows)

    # Construct feasible location-count solutions per Method/Scenario/K/SKU_Scenario.
    model_row_by_key = {
        (row.get("Method", ""), row.get("Scenario", ""), row.get("K", ""), row.get("SKU_Scenario", "")): row
        for row in model_rows
    }
    requirement_groups: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in slot_size_constraint_rows:
        key = (
            str(row.get("Method", "")),
            str(row.get("Scenario", "")),
            str(row.get("K", "")),
            str(row.get("SKU_Scenario", "")),
        )
        requirement_groups[key].append(row)

    feasible_summary_rows: list[dict[str, str]] = []
    feasible_slot_rows: list[dict[str, str]] = []

    for key, requirement_rows in requirement_groups.items():
        method, scenario, k, sku_scenario = key
        model_row = model_row_by_key.get(key, {})
        required_total_locations = int(round(_to_float(model_row.get("Required_Total_Locations_At_Max_Occupancy")) or 0.0))
        required_high_non_occupied = int(round(_to_float(model_row.get("Required_High_Non_Occupied_Count_At_Minimum")) or 0.0))

        feasible, assigned_by_slot, achieved_at_or_above, achieved_high_non_occupied, achieved_high_total, high_status, unit_usage = _build_feasible_solution_for_combo(
            requirement_rows,
            capacity_units,
            HIGH_SLOT_THRESHOLD,
            required_high_non_occupied,
        )

        total_assigned = sum(assigned_by_slot.values())
        min_slot_size = min(assigned_by_slot.keys()) if assigned_by_slot else None
        achieved_total_for_min_slot = achieved_at_or_above.get(min_slot_size, 0) if min_slot_size is not None else 0

        for requirement_row in sorted(requirement_rows, key=lambda row: _to_float(row.get("Representative_Slot_Size")) or 0.0):
            slot_size = _to_float(requirement_row.get("Representative_Slot_Size"))
            if slot_size is None:
                continue
            min_required = int(round(_to_float(requirement_row.get("Min_Required_Locations_At_Or_Above_Size")) or 0.0))
            achieved = achieved_at_or_above.get(slot_size, 0)

            feasible_slot_rows.append(
                {
                    "Method": method,
                    "Scenario": scenario,
                    "K": k,
                    "SKU_Scenario": sku_scenario,
                    "Representative_Slot_Size": f"{slot_size:.0f}",
                    "Assigned_Locations_Exact_Size": str(assigned_by_slot.get(slot_size, 0)),
                    "Min_Required_Locations_At_Or_Above_Size": str(min_required),
                    "Achieved_Locations_At_Or_Above_Size": str(achieved),
                    "Constraint_Satisfied": "YES" if achieved >= min_required else "NO",
                }
            )

        feasible_summary_rows.append(
            {
                "Method": method,
                "Scenario": scenario,
                "K": k,
                "SKU_Scenario": sku_scenario,
                "Feasible": "YES" if feasible else "NO",
                "Total_Assigned_Locations": str(total_assigned),
                "Required_Total_Locations_At_Max_Occupancy": str(required_total_locations),
                "Total_Occupancy_Constraint_Satisfied": (
                    "YES" if achieved_total_for_min_slot >= required_total_locations else "NO"
                ) if ENFORCE_OCCUPANCY_RATE_CONSTRAINT else "DISABLED",
                "High_Non_Occupied_Status": high_status,
                "Achieved_High_Locations_At_Or_Above_99": str(achieved_high_total),
                "Achieved_High_Non_Occupied_Count": str(achieved_high_non_occupied),
                "Required_High_Non_Occupied_Count": str(required_high_non_occupied),
                "Beam_Units_Used": str(unit_usage.get("beam", 0)),
                "Floor_Units_Used": str(unit_usage.get("floor", 0)),
            }
        )

    layout_summary_rows, layout_column_rows, layout_location_rows = _generate_layout_distribution_outputs(
        feasible_slot_rows=feasible_slot_rows,
        units=capacity_units,
        prepared_rows=prepared_rows,
        beam_map_rows=beam_map_rows,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    static_file = OUTPUT_DIR / "Constraint_Static_Checks.csv"
    with static_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=["Constraint", "Status", "Details"])
        writer.writeheader()
        writer.writerows(static_rows)

    invalid_codes_file = OUTPUT_DIR / "Constraint_Invalid_Location_Codes.csv"
    with invalid_codes_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=["Location"])
        writer.writeheader()
        writer.writerows(invalid_location_rows)

    violating_columns_file = OUTPUT_DIR / "Constraint_Violating_Columns_Detail.csv"
    with violating_columns_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "Rack",
                "Column",
                "Location_Count",
                "Beam_Count_Used",
                "Allowed_Used_Height",
                "Actual_Used_Height",
                "Excess_Height",
                "Locations",
                "Location_Heights",
            ],
        )
        writer.writeheader()
        writer.writerows(violating_column_rows)

    slot_size_constraints_file = OUTPUT_DIR / "Constraint_Location_Counts_By_Slot_Size.csv"
    with slot_size_constraints_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "Method",
                "Scenario",
                "K",
                "SKU_Scenario",
                "SKU_Count",
                "Representative_Slot_Size",
                "Cluster_Count_Percentage",
                "Assigned_SKUs_At_Representative_Size",
                "Decision_Variable",
                "Cumulative_Assigned_SKUs_At_Or_Above_Size",
                "Min_Required_Locations_At_Or_Above_Size",
                "Coverage_Constraint",
            ],
        )
        writer.writeheader()
        writer.writerows(slot_size_constraint_rows)

    feasible_slot_file = OUTPUT_DIR / "Feasible_Slot_Size_Counts_By_Method_Scenario_K_v2.csv"
    with feasible_slot_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "Method",
                "Scenario",
                "K",
                "SKU_Scenario",
                "Representative_Slot_Size",
                "Assigned_Locations_Exact_Size",
                "Min_Required_Locations_At_Or_Above_Size",
                "Achieved_Locations_At_Or_Above_Size",
                "Constraint_Satisfied",
            ],
        )
        writer.writeheader()
        writer.writerows(feasible_slot_rows)

    feasible_summary_file = OUTPUT_DIR / "Feasible_Solution_Summary_By_Method_Scenario_K_v2.csv"
    with feasible_summary_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "Method",
                "Scenario",
                "K",
                "SKU_Scenario",
                "Feasible",
                "Total_Assigned_Locations",
                "Required_Total_Locations_At_Max_Occupancy",
                "Total_Occupancy_Constraint_Satisfied",
                "High_Non_Occupied_Status",
                "Achieved_High_Locations_At_Or_Above_99",
                "Achieved_High_Non_Occupied_Count",
                "Required_High_Non_Occupied_Count",
                "Beam_Units_Used",
                "Floor_Units_Used",
            ],
        )
        writer.writeheader()
        writer.writerows(feasible_summary_rows)

    layout_summary_file = OUTPUT_DIR / "Layout_Distribution_Summary_By_Method_Scenario_K_v2.csv"
    with layout_summary_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "Method",
                "Scenario",
                "K",
                "SKU_Scenario",
                "Layout_Feasible",
                "Assigned_Locations_Total",
                "Target_Locations_Total",
                "Average_Column_Fill_Ratio",
                "Notes",
            ],
        )
        writer.writeheader()
        writer.writerows(layout_summary_rows)

    layout_column_file = OUTPUT_DIR / "Layout_Distribution_By_Rack_Column_v2.csv"
    with layout_column_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "Method",
                "Scenario",
                "K",
                "SKU_Scenario",
                "Rack_Column",
                "Beam_Count_Used",
                "Allowed_Used_Height_cm",
                "Assigned_Used_Height_cm",
                "Fill_Ratio",
                "Remaining_Height_cm",
                "Slot_Size_Distribution",
            ],
        )
        writer.writeheader()
        writer.writerows(layout_column_rows)

    layout_location_file = OUTPUT_DIR / "Layout_Distribution_By_Location_v2.csv"
    with layout_location_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "Method",
                "Scenario",
                "K",
                "SKU_Scenario",
                "Location",
                "Rack",
                "Column",
                "Row",
                "Beam_Coordinate",
                "Assignment_Unit_ID",
                "Assignment_Unit_Type",
                "Assigned_Slot_Size_cm",
            ],
        )
        writer.writeheader()
        writer.writerows(layout_location_rows)

    model_file = OUTPUT_DIR / "Constraint_Model_By_Method_Scenario_K.csv"
    fields = list(model_rows[0].keys()) if model_rows else [
        "Method", "Scenario", "K", "SKU_Scenario", "SKU_Count", "Max_Occupancy_Rate", "Required_Total_Locations_At_Max_Occupancy",
        "Total_Location_Decision", "Occupancy_Constraint", "High_Slot_Threshold_cm",
        "Required_High_Non_Occupied_Count_At_Minimum", "High_Slot_Location_Decision",
        "High_Non_Occupied_Constraint",
        "Slot_Sizes", "Rack_Column_Division", "Location_Coding", "Beam_Coupling_Constraint", "Doorgang_Fixed_Heights",
    ]

    with model_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(model_rows)

    return OUTPUT_DIR


if __name__ == "__main__":
    output_path = build_configuration_model_constraints()
    print(f"Slot-size configuration model (constraints only, no solving) written to: {output_path}")
