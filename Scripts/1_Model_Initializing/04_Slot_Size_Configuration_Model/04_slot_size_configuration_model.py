import csv
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[3]
INPUT_PREPARED = ROOT / "Output" / "01_Data_Preparation" / "Location_Details_Prepared.csv"
INPUT_SCENARIOS = ROOT / "Output" / "02_Scenario_Generation" / "02_Item_Height_Scenarios_Delta_Weighted.csv"
INPUT_LOCATION_BEAM_MAP = ROOT / "Output" / "01_Data_Preparation" / "Beam_Grid_Mapping" / "Location_Beam_Map.csv"
SLOT_SIZE_ROOT = ROOT / "Output" / "03_Slot_Size_Generation"
OUTPUT_DIR = ROOT / "Output" / "04_Slot_Size_Configuration_Model"
CONSTRAINT_OUTPUT_DIR = OUTPUT_DIR / "constraint"
FEASIBLE_OUTPUT_DIR = OUTPUT_DIR / "feasible"
LAYOUT_OUTPUT_DIR = OUTPUT_DIR / "layout"

METHODS = ("quantile_binning", "hierarchical_clustering", "kmeans_clustering")
BASE_SKU_COUNT = 843
SKU_SCENARIO_FACTORS = {
    "Low_Count": 0.9,
    "Base_Count": 1.0,
    "High_Count": 1.1,
}
MAX_CONFIGURATION_CANDIDATES = 6
CANDIDATE_LAYOUT_STYLES = ("utilization", "relocation", "material", "balanced")
MAX_REPRESENTATIVE_LAYOUTS = 10
FINAL_LAYOUT_COUNT = 6

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
BEAM_SPAN_PATTERN = re.compile(r"^([A-Z])\[(\d{2})-(\d{2})\]:([0-9]{1,2}[A-Za-z]?)$")
BEAM_SINGLE_PATTERN = re.compile(r"^([A-Z])(\d{2}):([0-9]{1,2}[A-Za-z]?)$")


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
    raw = [max(0.0, percentage) * total_count for percentage in percentages]
    floors = [int(math.floor(value)) for value in raw]
    remainder = max(total_count - sum(floors), 0)
    ranked = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)

    for index in ranked[:remainder]:
        floors[index] += 1

    return floors


def _read_csv(path: Path) -> list[dict[str, str]]:
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
    if not fieldnames:
        return fieldnames, rows
    if not rows:
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
    cleaned_fields, cleaned_rows = _deduplicate_output_columns(fieldnames, rows)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=cleaned_fields)
        writer.writeheader()
        writer.writerows(cleaned_rows)


def _slot_size_variable_name(slot_size: float) -> str:
    return f"x_{int(round(slot_size))}"


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


def _load_method_rows(method: str, file_name: str) -> list[dict[str, str]]:
    return _read_csv(SLOT_SIZE_ROOT / method / file_name)


def _static_checks(
    prepared_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    checks: list[dict[str, str]] = []
    invalid_location_rows: list[dict[str, str]] = []
    violating_column_rows: list[dict[str, str]] = []

    parsed: list[tuple[str, str, int, int, dict[str, str]]] = []
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


def _build_layout_columns(prepared_rows: list[dict[str, str]]) -> list[str]:
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
) -> list[dict[str, str]]:
    location_rows: list[dict[str, str]] = []

    for column_key in sorted(column_assignments.keys()):
        rack = column_key[0]
        column = column_key[1:]
        ordered_slots = sorted(column_assignments[column_key], reverse=True)
        for row_index, slot_size in enumerate(ordered_slots, start=1):
            location_rows.append(
                {
                    "Layout_ID": layout_id,
                    "Config_ID": config_id,
                    "Style": style,
                    "Location": f"{rack}{column}{row_index:02d}",
                    "Rack": rack,
                    "Column": column,
                    "Row": f"{row_index:02d}",
                    "Beam_Coordinate": "" if row_index <= 1 else f"{rack}{column}:{row_index:02d}",
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
) -> tuple[bool, dict[float, int], dict[str, float], dict[str, list[float]], str]:
    assigned_exact_counts: dict[float, int] = {slot_size: 0 for slot_size in target_exact_counts}
    used_height_by_column: dict[str, float] = defaultdict(float)
    counts_by_column: dict[str, int] = defaultdict(int)
    assignments_by_column: dict[str, list[float]] = defaultdict(list)

    slot_sizes_desc = sorted(target_exact_counts.keys(), reverse=True)
    for slot_size in slot_sizes_desc:
        needed = int(target_exact_counts.get(slot_size, 0))
        while needed > 0:
            candidates: list[tuple[float, int, str]] = []
            for column_key in column_keys:
                current_count = counts_by_column.get(column_key, 0)
                next_count = current_count + 1
                allowed_after = MAX_USED_HEIGHT_BASE - BEAM_HEIGHT * max(next_count - 1, MIN_BEAMS_PER_COLUMN)
                proposed_used = used_height_by_column.get(column_key, 0.0) + slot_size
                if proposed_used > allowed_after + 1e-9:
                    continue

                remaining_after = allowed_after - proposed_used
                if style == "relocation":
                    style_term = float(current_count)
                elif style == "material":
                    style_term = float(slot_size)
                elif style == "balanced":
                    style_term = abs(remaining_after - slot_size)
                else:
                    style_term = float(remaining_after)

                candidates.append((remaining_after + style_term * 1e-3, current_count, column_key))

            if not candidates:
                return False, assigned_exact_counts, dict(used_height_by_column), dict(assignments_by_column), f"No allocatable column found for slot size {slot_size:.0f} with remaining demand {needed}."

            candidates.sort(key=lambda item: (item[0], item[1], item[2]))
            chosen_column = candidates[0][2]
            assignments_by_column[chosen_column].append(slot_size)
            used_height_by_column[chosen_column] += slot_size
            counts_by_column[chosen_column] += 1
            assigned_exact_counts[slot_size] += 1
            needed -= 1

    assignments_by_column = {column_key: slots for column_key, slots in assignments_by_column.items() if slots}
    used_height_by_column = {column_key: used_height_by_column[column_key] for column_key in assignments_by_column}
    return True, assigned_exact_counts, dict(used_height_by_column), assignments_by_column, "Synthesized column allocation succeeded."


def _normalize_beam_level(level: str) -> str:
    token = str(level).strip()
    match = re.match(r"^(\d{1,2})([A-Za-z]?)$", token)
    if not match:
        return token.lower()
    numeric = int(match.group(1))
    suffix = match.group(2).lower()
    return f"{numeric:02d}{suffix}"


def _parse_beam_coordinate_parts(coordinate: str) -> tuple[str, int, int, str] | None:
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


def _build_current_beam_units_and_segments(
    beam_map_rows: list[dict[str, str]],
) -> tuple[set[str], set[tuple[str, int, int]]]:
    beam_units: set[str] = set()
    segments: set[tuple[str, int, int]] = set()

    for row in beam_map_rows:
        parsed = _parse_beam_coordinate_parts(str(row.get("Beam_Coordinate", "")))
        if parsed is None:
            continue
        rack, c0, c1, level = parsed
        beam_units.add(_format_beam_unit(rack, c0, c1, level))
        segments.add((rack, c0, c1))

    return beam_units, segments


def _build_proposed_beam_units_from_layout_rows(
    layout_rows: list[dict[str, str]],
    segments: set[tuple[str, int, int]],
) -> set[str]:
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

    return proposed_units


def _beam_relocations(
    current_units: set[str],
    proposed_units: set[str],
) -> tuple[int, dict[str, int]]:
    removed_units = current_units - proposed_units
    added_units = proposed_units - current_units

    # Count relocations as beams from the initial configuration that are no longer at the same coordinate.
    relocation_total = len(removed_units)

    per_column: dict[str, int] = defaultdict(int)
    for beam_unit in removed_units:
        for column_key in _beam_unit_columns(beam_unit):
            per_column[column_key] += 1

    return relocation_total, dict(per_column)


def _coverage_check_for_requirements(
    assigned_exact: dict[float, int],
    requirement_rows: list[dict[str, str]],
) -> tuple[bool, int]:
    shortfall = 0
    for row in requirement_rows:
        slot_size = _to_float(row.get("Representative_Slot_Size"))
        required = int(round(_to_float(row.get("Min_Required_Locations_At_Or_Above_Size")) or 0.0))
        if slot_size is None:
            continue
        achieved = sum(count for size, count in assigned_exact.items() if size >= slot_size)
        if achieved < required:
            shortfall += required - achieved
    return shortfall == 0, shortfall


def _material_requirements(
    current_units: set[str],
    proposed_units: set[str],
) -> tuple[int, int, int]:
    removed_units = len(current_units - proposed_units)
    added_units = len(proposed_units - current_units)

    # Additional beams are counted separately from moved beams.
    additional_beams = added_units
    removed_beams = removed_units
    additional_grids = additional_beams
    return additional_beams, additional_grids, removed_beams


def _select_representative_layouts(candidate_layouts: list[dict[str, object]]) -> list[dict[str, object]]:
    if not candidate_layouts:
        return []

    feasible = [layout for layout in candidate_layouts if bool(layout.get("Feasible", False))]
    pool = feasible if feasible else candidate_layouts

    selected: list[dict[str, object]] = []

    def _take_best(key_func: Callable[[dict[str, object]], tuple[float, ...]]) -> None:
        if not pool:
            return
        best = min(pool, key=key_func)
        if best not in selected:
            selected.append(best)

    _take_best(lambda row: (-(_to_float(row.get("Occupancy_Rate")) or 0.0), _to_int_default(row.get("Beam_Relocations_Total"), 0)))
    _take_best(lambda row: (_to_int_default(row.get("Beam_Relocations_Total"), 0), _to_int_default(row.get("Space_Left"), 0)))
    _take_best(
        lambda row: (
            _to_int_default(row.get("Additional_Beams_Required"), 0) + _to_int_default(row.get("Additional_Grids_Required"), 0),
            _to_int_default(row.get("Beam_Relocations_Total"), 0),
        )
    )

    occ_values = [(_to_float(row.get("Occupancy_Rate")) or 0.0) for row in pool]
    reloc_values = [_to_int_default(row.get("Beam_Relocations_Total"), 0) for row in pool]
    space_left_values = [_to_int_default(row.get("Space_Left"), 0) for row in pool]
    material_values = [
        _to_int_default(row.get("Additional_Beams_Required"), 0) + _to_int_default(row.get("Additional_Grids_Required"), 0)
        for row in pool
    ]
    occ_min, occ_max = min(occ_values), max(occ_values)
    reloc_min, reloc_max = min(reloc_values), max(reloc_values)
    space_left_min, space_left_max = min(space_left_values), max(space_left_values)
    mat_min, mat_max = min(material_values), max(material_values)

    def _norm(value: float, min_value: float, max_value: float) -> float:
        if max_value == min_value:
            return 0.0
        return (value - min_value) / (max_value - min_value)

    _take_best(
        lambda row: (
            _norm(occ_max - (_to_float(row.get("Occupancy_Rate")) or 0.0), occ_min, occ_max)
            + _norm(_to_int_default(row.get("Beam_Relocations_Total"), 0), reloc_min, reloc_max)
            + _norm(_to_int_default(row.get("Space_Left"), 0), space_left_min, space_left_max)
            + _norm(
                _to_int_default(row.get("Additional_Beams_Required"), 0) + _to_int_default(row.get("Additional_Grids_Required"), 0),
                mat_min,
                mat_max,
            ),
        )
    )

    for row in sorted(
        pool,
        key=lambda r: (
            -(_to_float(r.get("Occupancy_Rate")) or 0.0),
            _to_int_default(r.get("Beam_Relocations_Total"), 0),
            _to_int_default(r.get("Space_Left"), 0),
            _to_int_default(r.get("Additional_Beams_Required"), 0) + _to_int_default(r.get("Additional_Grids_Required"), 0),
        ),
    ):
        if row not in selected:
            selected.append(row)
        if len(selected) >= MAX_REPRESENTATIVE_LAYOUTS:
            break

    return selected


def _is_dominated_configuration(candidate: dict[str, object], others: list[dict[str, object]]) -> bool:
    cand_occ = _to_float(candidate.get("Proxy_Occupancy_Rate")) or 0.0
    cand_reloc = _to_int_default(candidate.get("Proxy_Beam_Relocations"), 0)
    cand_space_left = _to_int_default(candidate.get("Proxy_Space_Left"), 0)

    for other in others:
        if other is candidate:
            continue
        other_occ = _to_float(other.get("Proxy_Occupancy_Rate")) or 0.0
        other_reloc = _to_int_default(other.get("Proxy_Beam_Relocations"), 0)
        other_space_left = _to_int_default(other.get("Proxy_Space_Left"), 0)

        if (
            other_occ >= cand_occ
            and other_reloc <= cand_reloc
            and other_space_left <= cand_space_left
            and (other_occ > cand_occ or other_reloc < cand_reloc or other_space_left < cand_space_left)
        ):
            return True

    return False




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

            rows.append(
                {
                    "Method": method,
                    "Scenario": scenario,
                    "K": k,
                    "SKU_Scenario": sku_scenario_name,
                    "SKU_Count": str(sku_count),
                    "Total_Location_Decision": "sum(x_s)",
                    "Coverage_Basis": "Direct cumulative SKU demand",
                    "Slot_Sizes": slot_size_text,
                    "Rack_Column_Division": "FIXED (validated in static checks)",
                    "Location_Coding": "FIXED (validated in static checks)",
                    "Beam_Coupling_Constraint": "REGISTERED (not solved)",
                    "Doorgang_Fixed_Heights": "REGISTERED (not solved)",
                }
            )

    return rows, slot_size_rows


def _emit_static_check_messages(
    invalid_location_rows: list[dict[str, str]],
    violating_column_rows: list[dict[str, str]],
) -> None:
    invalid_count = len(invalid_location_rows)
    violating_count = len(violating_column_rows)

    if invalid_count > 0:
        sample = ", ".join(row.get("Location", "") for row in invalid_location_rows[:5])
        suffix = "..." if invalid_count > 5 else ""
        print(f"WARNING: Invalid location codes found ({invalid_count}). Sample: {sample}{suffix}")

    if violating_count > 0:
        sample = ", ".join(
            f"{row.get('Rack', '')}{row.get('Column', '')}"
            for row in violating_column_rows[:5]
        )
        suffix = "..." if violating_count > 5 else ""
        print(f"WARNING: Column-height violations found ({violating_count}). Sample columns: {sample}{suffix}")


def _derive_exact_min_counts(requirement_rows: list[dict[str, str]]) -> dict[float, int]:
    slot_sizes = sorted(
        {
            value
            for row in requirement_rows
            if (value := _to_float(row.get("Representative_Slot_Size"))) is not None
        }
    )
    cumulative = {
        slot_size: int(
            round(
                _to_float(
                    next(
                        row
                        for row in requirement_rows
                        if _to_float(row.get("Representative_Slot_Size")) == slot_size
                    ).get("Min_Required_Locations_At_Or_Above_Size")
                )
                or 0.0
            )
        )
        for slot_size in slot_sizes
    }

    exact: dict[float, int] = {}
    for index, slot_size in enumerate(slot_sizes):
        next_higher = slot_sizes[index + 1] if index + 1 < len(slot_sizes) else None
        next_cumulative = cumulative.get(next_higher, 0) if next_higher is not None else 0
        exact[slot_size] = max(cumulative[slot_size] - next_cumulative, 0)

    return exact


def _group_fits_slot(
    group: dict[str, object],
    slot_size: float,
    used_height_by_column: dict[str, float],
    allowed_used_height: dict[str, float],
) -> bool:
    counts_obj = group.get("Column_Counts")
    if not isinstance(counts_obj, dict):
        return False

    for column_key, count in counts_obj.items():
        allowed = allowed_used_height.get(str(column_key))
        if allowed is None:
            return False
        proposed = used_height_by_column.get(str(column_key), 0.0) + slot_size * _to_int_default(count, 0)
        if proposed > allowed + 1e-9:
            return False
    return True


def _apply_group_assignment(
    group: dict[str, object],
    slot_size: float,
    used_height_by_column: dict[str, float],
) -> None:
    counts_obj = group.get("Column_Counts")
    if not isinstance(counts_obj, dict):
        return
    for column_key, count in counts_obj.items():
        key = str(column_key)
        used_height_by_column[key] = used_height_by_column.get(key, 0.0) + slot_size * _to_int_default(count, 0)


def _can_promote_group(
    group: dict[str, object],
    from_slot: float,
    to_slot: float,
    used_height_by_column: dict[str, float],
    allowed_used_height: dict[str, float],
) -> bool:
    counts_obj = group.get("Column_Counts")
    if not isinstance(counts_obj, dict):
        return False

    for column_key, count in counts_obj.items():
        key = str(column_key)
        allowed = allowed_used_height.get(key)
        if allowed is None:
            return False
        count_int = _to_int_default(count, 0)
        proposed = used_height_by_column.get(key, 0.0) - from_slot * count_int + to_slot * count_int
        if proposed > allowed + 1e-9:
            return False
    return True


def _promote_groups_to_fill_height(
    assignments: list[tuple[int, float]],
    assigned_exact: dict[float, int],
    rack_row_groups: list[dict[str, object]],
    used_height_by_column: dict[str, float],
    allowed_used_height: dict[str, float],
    slot_sizes: list[float],
) -> None:
    assignment_map = {group_index: slot for group_index, slot in assignments}

    changed = True
    while changed:
        changed = False
        current_counts = assigned_exact.copy()
        ordered_groups = sorted(
            assignment_map.items(),
            key=lambda item: (item[1], -_to_int_default(rack_row_groups[item[0]].get("Capacity"), 0)),
        )

        best_choice: tuple[int, float, float] | None = None
        for group_index, current_slot in ordered_groups:
            group = rack_row_groups[group_index]
            current_pos = slot_sizes.index(current_slot)
            for target_slot in reversed(slot_sizes[current_pos + 1 :]):
                if current_counts.get(target_slot, 0) > current_counts.get(current_slot, 0):
                    continue
                if not _can_promote_group(group, current_slot, target_slot, used_height_by_column, allowed_used_height):
                    continue

                delta_height = target_slot - current_slot
                balance_score = abs((current_counts.get(current_slot, 0) - _to_int_default(group.get("Capacity"), 0)) - current_counts.get(target_slot, 0))
                choice = (group_index, target_slot, delta_height)
                if best_choice is None or choice[2] > best_choice[2] or (choice[2] == best_choice[2] and balance_score < 0):
                    best_choice = choice
                break

        if best_choice is None:
            break

        group_index, target_slot, _delta = best_choice
        current_slot = assignment_map[group_index]
        group = rack_row_groups[group_index]
        counts_obj = group.get("Column_Counts")
        if not isinstance(counts_obj, dict):
            continue

        for column_key, count in counts_obj.items():
            key = str(column_key)
            count_int = _to_int_default(count, 0)
            used_height_by_column[key] = used_height_by_column.get(key, 0.0) - current_slot * count_int + target_slot * count_int

        capacity = _to_int_default(group.get("Capacity"), 0)
        assigned_exact[current_slot] = max(0, assigned_exact.get(current_slot, 0) - capacity)
        assigned_exact[target_slot] = assigned_exact.get(target_slot, 0) + capacity

        assignment_map[group_index] = target_slot
        for idx, (existing_group, _) in enumerate(assignments):
            if existing_group == group_index:
                assignments[idx] = (group_index, target_slot)
                break

        changed = True




def build_configuration_model_constraints() -> Path:
    prepared_rows = _read_csv(INPUT_PREPARED)
    scenario_rows = _read_csv(INPUT_SCENARIOS)
    beam_map_rows = _read_csv(ROOT / "Output" / "01_Data_Preparation" / "Beam_Grid_Mapping" / "Location_Beam_Map.csv")
    sku_count_scenarios = _build_sku_count_scenarios(scenario_rows)
    static_rows, invalid_location_rows, violating_column_rows = _static_checks(prepared_rows)
    _emit_static_check_messages(invalid_location_rows, violating_column_rows)
    model_rows: list[dict[str, str]] = []
    slot_size_constraint_rows: list[dict[str, str]] = []

    for method in METHODS:
        summaries = _load_method_rows(method, "Slot_Size_Configuration_Summary.csv")
        method_rows, method_slot_rows = _method_constraint_rows(method, summaries, sku_count_scenarios)
        model_rows.extend(method_rows)
        slot_size_constraint_rows.extend(method_slot_rows)

    # Build requirement groups for scenario-wise evaluation.
    requirement_groups: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in slot_size_constraint_rows:
        key = (
            str(row.get("Method", "")),
            str(row.get("Scenario", "")),
            str(row.get("K", "")),
            str(row.get("SKU_Scenario", "")),
        )
        requirement_groups[key].append(row)

    # STEP 1: Build candidate slot-size configurations from all combinations.
    # Dedupe only exact duplicates (same method, K, slot sizes, and required exact distribution).
    config_map: dict[tuple[str, str, tuple[int, ...], tuple[int, ...]], dict[str, object]] = {}
    for row in model_rows:
        method = str(row.get("Method", ""))
        scenario = str(row.get("Scenario", ""))
        k = str(row.get("K", ""))
        sku_scenario = str(row.get("SKU_Scenario", ""))
        source_key = (method, scenario, k, sku_scenario)
        req_rows = requirement_groups.get(source_key, [])
        if not req_rows:
            continue

        min_exact_counts_raw = _derive_exact_min_counts(req_rows)
        slot_sizes = sorted(min_exact_counts_raw.keys())
        if not slot_sizes:
            continue

        signature = tuple(int(round(value)) for value in slot_sizes)
        distribution_signature = tuple(_to_int_default(min_exact_counts_raw.get(size), 0) for size in slot_sizes)
        dedupe_key = (method, k, signature, distribution_signature)

        if dedupe_key not in config_map:
            config_map[dedupe_key] = {
                "Config_ID": f"CFG_{len(config_map) + 1:03d}",
                "Method": method,
                "K": k,
                "Slot_Sizes": signature,
                "Min_Exact_Distribution": distribution_signature,
                "Representative_Source": source_key,
                "Sources": [],
            }

        sources_obj = config_map[dedupe_key].get("Sources")
        if isinstance(sources_obj, list):
            sources_obj.append(f"{method}|{scenario}|K={k}")

    candidate_configs = list(config_map.values())

    # STEP 1 continued: quick proxy scoring for dominated-configuration removal.
    layout_columns = _build_layout_columns(prepared_rows)
    current_beam_units, beam_segments = _build_current_beam_units_and_segments(beam_map_rows)
    total_physical_locations = len(
        [
            row
            for row in prepared_rows
            if str(row.get("Location Type", "")).strip().lower() != "doorgang"
            and not _is_split_location(str(row.get("Location", "")).strip())
        ]
    )

    for cfg in candidate_configs:
        slot_sizes_obj = cfg.get("Slot_Sizes")
        distribution_obj = cfg.get("Min_Exact_Distribution")
        slot_sizes = [float(value) for value in slot_sizes_obj] if isinstance(slot_sizes_obj, tuple) else []
        distribution = list(distribution_obj) if isinstance(distribution_obj, tuple) else []
        min_exact_counts = {
            slot_sizes[index]: _to_int_default(distribution[index], 0)
            for index in range(min(len(slot_sizes), len(distribution)))
        }

        feasible_proxy, assigned_proxy, proxy_used_by_column, proxy_assignments, _ = _allocate_layout_by_column(
            target_exact_counts=min_exact_counts,
            column_keys=layout_columns,
            style="balanced",
        )
        proxy_layout_rows = _build_generated_layout_location_rows(
            layout_id=f"PROXY::{cfg.get('Config_ID', '')}",
            config_id=str(cfg.get("Config_ID", "")),
            style="balanced",
            column_assignments=proxy_assignments,
        )
        proposed_beam_units = _build_proposed_beam_units_from_layout_rows(proxy_layout_rows, beam_segments)
        proxy_relocations, _ = _beam_relocations(current_beam_units, proposed_beam_units)
        assigned_total = sum(assigned_proxy.values())
        required_locations_total = sum(min_exact_counts.values())
        proxy_total_used_height = sum(proxy_used_by_column.values())
        proxy_occupancy_rate = (assigned_total / max(total_physical_locations, 1))
        proxy_space_left = sum(
            max(
                (MAX_USED_HEIGHT_BASE - BEAM_HEIGHT * max(len(slots) - 1, MIN_BEAMS_PER_COLUMN)) - proxy_used_by_column.get(column_key, 0.0),
                0.0,
            )
            for column_key, slots in proxy_assignments.items()
        )
        required_equals_assigned = assigned_total == required_locations_total

        cfg["Proxy_Feasible"] = feasible_proxy and required_equals_assigned
        cfg["Proxy_Assigned_Total"] = assigned_total
        cfg["Proxy_Required_Total"] = required_locations_total
        cfg["Proxy_Used_Height_Total"] = proxy_total_used_height
        cfg["Proxy_Beam_Relocations"] = proxy_relocations
        cfg["Proxy_Occupancy_Rate"] = proxy_occupancy_rate
        cfg["Proxy_Space_Left"] = proxy_space_left

    nondominated_configs = [cfg for cfg in candidate_configs if not _is_dominated_configuration(cfg, candidate_configs)]

    def _cfg_sort_key(cfg: dict[str, object]) -> tuple[int, int, float, int]:
        feasible_priority = 0 if bool(cfg.get("Proxy_Feasible", False)) else 1
        reloc = _to_int_default(cfg.get("Proxy_Beam_Relocations"), 0)
        occupancy = -(_to_float(cfg.get("Proxy_Occupancy_Rate")) or 0.0)
        space_left = _to_int_default(cfg.get("Proxy_Space_Left"), 0)
        return (feasible_priority, reloc, occupancy, space_left)

    def _best_by_group(configs: list[dict[str, object]], group_key: str) -> list[dict[str, object]]:
        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        for cfg in configs:
            grouped[str(cfg.get(group_key, ""))].append(cfg)
        picked: list[dict[str, object]] = []
        for group_value in sorted(grouped.keys()):
            picked.append(min(grouped[group_value], key=_cfg_sort_key))
        return picked

    mandatory_configs: list[dict[str, object]] = []
    for cfg in _best_by_group(candidate_configs, "Method") + _best_by_group(candidate_configs, "K"):
        if cfg not in mandatory_configs:
            mandatory_configs.append(cfg)

    shortlist_target = max(MAX_CONFIGURATION_CANDIDATES, len(mandatory_configs))
    shortlisted_configs = list(mandatory_configs)
    for cfg in sorted(nondominated_configs, key=_cfg_sort_key):
        if cfg in shortlisted_configs:
            continue
        shortlisted_configs.append(cfg)
        if len(shortlisted_configs) >= shortlist_target:
            break

    candidate_configuration_rows: list[dict[str, str]] = []
    for cfg in candidate_configs:
        slot_sizes_tuple = cfg.get("Slot_Sizes", tuple())
        slot_sizes = list(slot_sizes_tuple) if isinstance(slot_sizes_tuple, tuple) else []
        distribution_tuple = cfg.get("Min_Exact_Distribution", tuple())
        min_exact_distribution = distribution_tuple if isinstance(distribution_tuple, tuple) else tuple()
        sources = cfg.get("Sources", [])
        source_count = len(sources) if isinstance(sources, list) else 0
        candidate_configuration_rows.append(
            {
                "Config_ID": str(cfg.get("Config_ID", "")),
                "Method": str(cfg.get("Method", "")),
                "K": str(cfg.get("K", "")),
                "Slot_Sizes": ",".join(str(size) for size in slot_sizes),
                "Min_Exact_Distribution": ",".join(str(value) for value in min_exact_distribution),
                "Source_Combination_Count": str(source_count),
                "Selection_Status": "SHORTLISTED" if cfg in shortlisted_configs else "PRUNED",
                "Prune_Reason": (
                    "Dominated or lower-ranked in shortlist"
                    if cfg not in shortlisted_configs
                    else ""
                ),
                "Mandatory_Coverage_Selected": "YES" if cfg in mandatory_configs else "NO",
                "Source_Sample": ";".join((sources[:5] if isinstance(sources, list) else [])),
            }
        )

    # STEP 2: generate feasible rack layouts for shortlisted configurations.

    candidate_layout_rows: list[dict[str, str]] = []
    candidate_layout_location_rows: list[dict[str, str]] = []
    candidate_layout_column_rows: list[dict[str, str]] = []
    scenario_evaluation_rows: list[dict[str, str]] = []

    candidate_layouts: list[dict[str, object]] = []
    layout_counter = 1
    for cfg in shortlisted_configs:
        rep_source = cfg.get("Representative_Source")
        if not isinstance(rep_source, tuple) or len(rep_source) != 4:
            continue

        requirement_rows = requirement_groups.get(rep_source, [])
        if not requirement_rows:
            continue

        min_exact_counts = _derive_exact_min_counts(requirement_rows)
        slot_sizes = sorted(min_exact_counts.keys())

        for style in CANDIDATE_LAYOUT_STYLES:
            layout_id = f"LAY_{layout_counter:03d}"
            layout_counter += 1

            feasible_layout, assigned_exact, used_by_column, column_assignments, note = _allocate_layout_by_column(
                target_exact_counts=min_exact_counts,
                column_keys=layout_columns,
                style=style,
            )

            generated_location_rows = _build_generated_layout_location_rows(
                layout_id=layout_id,
                config_id=str(cfg.get("Config_ID", "")),
                style=style,
                column_assignments=column_assignments,
            )
            proposed_beam_units = _build_proposed_beam_units_from_layout_rows(generated_location_rows, beam_segments)
            relocation_total, relocation_by_column = _beam_relocations(current_beam_units, proposed_beam_units)
            additional_beams, additional_grids, removed_beams = _material_requirements(current_beam_units, proposed_beam_units)

            assigned_total = sum(assigned_exact.values())
            total_used_height = sum(used_by_column.values())
            total_allowed_height = sum(
                MAX_USED_HEIGHT_BASE - BEAM_HEIGHT * max(len(slots) - 1, MIN_BEAMS_PER_COLUMN)
                for slots in column_assignments.values()
            )
            required_locations_total = sum(min_exact_counts.values())
            required_equals_assigned = assigned_total == required_locations_total
            occupancy_rate = (assigned_total / max(total_physical_locations, 1))
            space_left = sum(
                max(
                    (MAX_USED_HEIGHT_BASE - BEAM_HEIGHT * max(len(slots) - 1, MIN_BEAMS_PER_COLUMN)) - used_by_column.get(column_key, 0.0),
                    0.0,
                )
                for column_key, slots in column_assignments.items()
            )
            layout_feasible_status = feasible_layout and required_equals_assigned
            candidate_layouts.append(
                {
                    "Layout_ID": layout_id,
                    "Config_ID": str(cfg.get("Config_ID", "")),
                    "Style": style,
                    "Feasible": layout_feasible_status,
                    "Assigned_Exact": assigned_exact,
                    "Assignments": column_assignments,
                    "Used_By_Column": used_by_column,
                    "Total_Used_Height": total_used_height,
                    "Total_Allowed_Height": total_allowed_height,
                    "Beam_Relocations_Total": relocation_total,
                    "Beam_Relocations_By_Column": relocation_by_column,
                    "Additional_Beams_Required": additional_beams,
                    "Additional_Grids_Required": additional_grids,
                    "Removed_Beams": removed_beams,
                    "Required_Locations_Total": required_locations_total,
                    "Assigned_Locations_Total": assigned_total,
                    "Occupancy_Rate_Base": occupancy_rate,
                    "Space_Left": space_left,
                    "Notes": note,
                }
            )

            candidate_layout_rows.append(
                {
                    "Layout_ID": layout_id,
                    "Config_ID": str(cfg.get("Config_ID", "")),
                    "Style": style,
                    "Layout_Feasible": "YES" if layout_feasible_status else "NO",
                    "Required_Locations_Total": str(required_locations_total),
                    "Assigned_Locations_Total": str(assigned_total),
                    "Total_Physical_Locations": str(total_physical_locations),
                    "Assigned_Used_Height_Total": f"{total_used_height:.3f}",
                    "Total_Allowed_Height": f"{total_allowed_height:.3f}",
                    "Occupancy_Rate": f"{occupancy_rate:.6f}",
                    "Space_Left": str(space_left),
                    "Beam_Relocations_Total": str(relocation_total),
                    "Additional_Beams_Required": str(additional_beams),
                    "Additional_Grids_Required": str(additional_grids),
                    "Slot_Size_Distribution": "|".join(
                        f"{int(slot)}:{count}" for slot, count in sorted(assigned_exact.items())
                    ),
                    "Notes": note,
                }
            )

            slot_mix_by_column: dict[str, dict[float, int]] = defaultdict(lambda: defaultdict(int))
            for row in generated_location_rows:
                rack = str(row.get("Rack", "")).strip()
                column = str(row.get("Column", "")).strip()
                if not rack or not column:
                    continue
                slot = _to_float(row.get("Assigned_Slot_Size_cm"))
                if slot is None:
                    continue
                slot_mix_by_column[f"{rack}{column}"][slot] += 1

            for column_key, slots in sorted(column_assignments.items()):
                used = used_by_column.get(column_key, 0.0)
                beam_count = max(len(slots) - 1, MIN_BEAMS_PER_COLUMN)
                allowed = MAX_USED_HEIGHT_BASE - beam_count * BEAM_HEIGHT
                mix = slot_mix_by_column.get(column_key, {})
                candidate_layout_column_rows.append(
                    {
                        "Layout_ID": layout_id,
                        "Config_ID": str(cfg.get("Config_ID", "")),
                        "Style": style,
                        "Rack_Column": column_key,
                        "Beam_Count_Used": str(beam_count),
                        "Allowed_Used_Height_cm": f"{allowed:.3f}",
                        "Assigned_Used_Height_cm": f"{used:.3f}",
                        "Remaining_Height_cm": f"{max(allowed - used, 0.0):.3f}",
                        "Fill_Ratio": f"{(used / allowed) if allowed > 0 else 0.0:.4f}",
                        "Beam_Relocations_In_Column": str(relocation_by_column.get(column_key, 0)),
                        "Slot_Size_Distribution": "|".join(
                            f"{int(slot_size)}:{count}" for slot_size, count in sorted(mix.items())
                        ),
                    }
                )

            candidate_layout_location_rows.extend(generated_location_rows)

    # STEP 3: keep a limited set of representative candidate layouts.
    representative_layouts = _select_representative_layouts(candidate_layouts)
    representative_ids = {str(layout.get("Layout_ID", "")) for layout in representative_layouts}

    candidate_layout_rows = [
        row for row in candidate_layout_rows if str(row.get("Layout_ID", "")) in representative_ids
    ]
    candidate_layout_column_rows = [
        row for row in candidate_layout_column_rows if str(row.get("Layout_ID", "")) in representative_ids
    ]
    candidate_layout_location_rows = [
        row for row in candidate_layout_location_rows if str(row.get("Layout_ID", "")) in representative_ids
    ]

    # STEP 4: evaluate each representative layout against all scenario combinations.
    aggregate_metrics: dict[str, dict[str, float]] = {}
    for layout in representative_layouts:
        layout_id = str(layout.get("Layout_ID", ""))
        assigned_exact = layout.get("Assigned_Exact", {})
        if not isinstance(assigned_exact, dict):
            continue

        pass_count = 0
        total_count = 0
        occupancy_values: list[float] = []
        space_left_values: list[float] = []
        total_shortfall = 0

        for key, requirement_rows in requirement_groups.items():
            method, scenario, k, sku_scenario = key
            sku_count = _to_int_default(next((row.get("SKU_Count") for row in requirement_rows), 0), 0)
            coverage_satisfied, shortfall = _coverage_check_for_requirements(assigned_exact, requirement_rows)
            assigned_locations_total = _to_int_default(layout.get("Assigned_Locations_Total"), 0)
            occupancy = (sku_count / max(assigned_locations_total, 1))
            demand_assignment_ratio = (sku_count / max(assigned_locations_total, 1))
            demand_match = sku_count == assigned_locations_total
            is_satisfied = coverage_satisfied and demand_match
            scenario_shortfall = shortfall + abs(sku_count - assigned_locations_total)
            space_left = _to_float(layout.get("Space_Left")) or 0.0

            scenario_evaluation_rows.append(
                {
                    "Layout_ID": layout_id,
                    "Config_ID": str(layout.get("Config_ID", "")),
                    "Style": str(layout.get("Style", "")),
                    "Method": method,
                    "Scenario": scenario,
                    "K": k,
                    "SKU_Scenario": sku_scenario,
                    "SKU_Count": str(sku_count),
                    "Constraint_Satisfied": "YES" if is_satisfied else "NO",
                    "Total_Shortfall": str(scenario_shortfall),
                    "Occupancy_Rate": f"{occupancy:.6f}",
                    "Space_Left": str(int(space_left)),
                    "Demand_Assignment_Ratio": f"{demand_assignment_ratio:.6f}",
                    "Demand_Assignment_Exact_Match": "YES" if demand_match else "NO",
                    "Beam_Relocations_Total": str(_to_int_default(layout.get("Beam_Relocations_Total"), 0)),
                    "Additional_Beams_Required": str(_to_int_default(layout.get("Additional_Beams_Required"), 0)),
                    "Additional_Grids_Required": str(_to_int_default(layout.get("Additional_Grids_Required"), 0)),
                }
            )

            total_count += 1
            pass_count += 1 if is_satisfied else 0
            total_shortfall += scenario_shortfall
            occupancy_values.append(occupancy)
            space_left_values.append(space_left)

        mean_occupancy = (sum(occupancy_values) / len(occupancy_values)) if occupancy_values else 0.0
        worst_occupancy = max(occupancy_values) if occupancy_values else 0.0
        mean_space_left = (sum(space_left_values) / len(space_left_values)) if space_left_values else 0.0
        worst_space_left = max(space_left_values) if space_left_values else 0.0
        robustness = (pass_count / total_count) if total_count > 0 else 0.0
        aggregate_metrics[layout_id] = {
            "Mean_Occupancy_Rate": mean_occupancy,
            "Worst_Occupancy_Rate": worst_occupancy,
            "Mean_Space_Left": mean_space_left,
            "Worst_Space_Left": worst_space_left,
            "Robustness": robustness,
            "Scenario_Pass_Count": float(pass_count),
            "Scenario_Total_Count": float(total_count),
            "Total_Shortfall": float(total_shortfall),
        }

    aggregate_layout_metrics = {
        layout_id: metrics
        for layout_id, metrics in aggregate_metrics.items()
    }

    for row in candidate_layout_rows:
        layout_id = str(row.get("Layout_ID", ""))
        metrics = aggregate_layout_metrics.get(layout_id)
        if not metrics:
            continue
        row["Mean_Occupancy_Rate"] = f"{metrics.get('Mean_Occupancy_Rate', 0.0):.6f}"
        row["Worst_Occupancy_Rate"] = f"{metrics.get('Worst_Occupancy_Rate', 0.0):.6f}"
        row["Mean_Space_Left"] = f"{metrics.get('Mean_Space_Left', 0.0):.6f}"
        row["Worst_Space_Left"] = f"{metrics.get('Worst_Space_Left', 0.0):.6f}"

    final_ranking_rows: list[dict[str, str]] = []
    # STEP 5: final ranking over representative layouts.
    ranked_layouts = sorted(
        representative_layouts,
        key=lambda layout: (
            0 if bool(layout.get("Feasible", False)) else 1,
            -(aggregate_metrics.get(str(layout.get("Layout_ID", "")), {}).get("Robustness", 0.0)),
            -(aggregate_metrics.get(str(layout.get("Layout_ID", "")), {}).get("Mean_Occupancy_Rate", 0.0)),
            _to_int_default(layout.get("Space_Left"), 0),
            aggregate_metrics.get(str(layout.get("Layout_ID", "")), {}).get("Total_Shortfall", 0.0),
            _to_int_default(layout.get("Beam_Relocations_Total"), 0),
            _to_int_default(layout.get("Additional_Beams_Required"), 0) + _to_int_default(layout.get("Additional_Grids_Required"), 0),
            -_to_int_default(layout.get("Assigned_Locations_Total"), 0),
        ),
    )
    finalists = ranked_layouts[:FINAL_LAYOUT_COUNT]
    finalist_ids = {str(layout.get("Layout_ID", "")) for layout in finalists}

    for rank, layout in enumerate(ranked_layouts, start=1):
        layout_id = str(layout.get("Layout_ID", ""))
        metrics = aggregate_metrics.get(layout_id, {})
        final_ranking_rows.append(
            {
                "Rank": str(rank),
                "Selection": "FINAL" if layout_id in finalist_ids else "NON_FINAL",
                "Layout_ID": layout_id,
                "Config_ID": str(layout.get("Config_ID", "")),
                "Style": str(layout.get("Style", "")),
                "Layout_Feasible": "YES" if bool(layout.get("Feasible", False)) else "NO",
                "Robustness": f"{metrics.get('Robustness', 0.0):.6f}",
                "Scenario_Pass_Count": str(int(metrics.get("Scenario_Pass_Count", 0.0))),
                "Scenario_Total_Count": str(int(metrics.get("Scenario_Total_Count", 0.0))),
                "Mean_Occupancy_Rate": f"{metrics.get('Mean_Occupancy_Rate', 0.0):.6f}",
                "Worst_Occupancy_Rate": f"{metrics.get('Worst_Occupancy_Rate', 0.0):.6f}",
                "Mean_Space_Left": f"{metrics.get('Mean_Space_Left', 0.0):.6f}",
                "Worst_Space_Left": f"{metrics.get('Worst_Space_Left', 0.0):.6f}",
                "Total_Shortfall": str(int(metrics.get("Total_Shortfall", 0.0))),
                "Beam_Relocations_Total": str(_to_int_default(layout.get("Beam_Relocations_Total"), 0)),
                "Additional_Beams_Required": str(_to_int_default(layout.get("Additional_Beams_Required"), 0)),
                "Additional_Grids_Required": str(_to_int_default(layout.get("Additional_Grids_Required"), 0)),
                "Assigned_Locations_Total": str(_to_int_default(layout.get("Assigned_Locations_Total"), 0)),
                "Notes": str(layout.get("Notes", "")),
            }
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CONSTRAINT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    FEASIBLE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LAYOUT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    static_file = CONSTRAINT_OUTPUT_DIR / "Constraint_Static_Checks.csv"
    _write_csv_clean(static_file, ["Constraint", "Status", "Details"], static_rows)

    # Clean up legacy detail outputs that are now replaced by console messages.
    for stale_file in (
        OUTPUT_DIR / "Constraint_Invalid_Location_Codes.csv",
        OUTPUT_DIR / "Constraint_Violating_Columns_Detail.csv",
        CONSTRAINT_OUTPUT_DIR / "Constraint_Invalid_Location_Codes.csv",
        CONSTRAINT_OUTPUT_DIR / "Constraint_Violating_Columns_Detail.csv",
        FEASIBLE_OUTPUT_DIR / "Feasible_Slot_Size_Counts_By_Method_Scenario_K_v2.csv",
        FEASIBLE_OUTPUT_DIR / "Feasible_Solution_Summary_By_Method_Scenario_K_v2.csv",
        LAYOUT_OUTPUT_DIR / "Layout_Distribution_By_Location_v2.csv",
        LAYOUT_OUTPUT_DIR / "Layout_Distribution_By_Rack_Column_v2.csv",
        LAYOUT_OUTPUT_DIR / "Layout_Distribution_Summary_By_Method_Scenario_K_v2.csv",
    ):
        if stale_file.exists():
            stale_file.unlink()

    slot_size_constraints_file = CONSTRAINT_OUTPUT_DIR / "Constraint_Location_Counts_By_Slot_Size.csv"
    _write_csv_clean(
        slot_size_constraints_file,
        [
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
        slot_size_constraint_rows,
    )

    candidate_configs_file = CONSTRAINT_OUTPUT_DIR / "Candidate_Configurations.csv"
    _write_csv_clean(
        candidate_configs_file,
        [
            "Config_ID",
            "Method",
            "K",
            "Slot_Sizes",
            "Min_Exact_Distribution",
            "Source_Combination_Count",
            "Selection_Status",
            "Prune_Reason",
            "Mandatory_Coverage_Selected",
            "Source_Sample",
        ],
        candidate_configuration_rows,
    )

    scenario_eval_file = FEASIBLE_OUTPUT_DIR / "Candidate_Layout_Scenario_Evaluation.csv"
    _write_csv_clean(
        scenario_eval_file,
        [
            "Layout_ID",
            "Config_ID",
            "Style",
            "Method",
            "Scenario",
            "K",
            "SKU_Scenario",
            "SKU_Count",
            "Constraint_Satisfied",
            "Total_Shortfall",
            "Occupancy_Rate",
            "Space_Left",
            "Demand_Assignment_Ratio",
            "Demand_Assignment_Exact_Match",
            "Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Additional_Grids_Required",
        ],
        scenario_evaluation_rows,
    )

    final_ranking_file = FEASIBLE_OUTPUT_DIR / "Final_Layout_Ranking.csv"
    _write_csv_clean(
        final_ranking_file,
        [
            "Rank",
            "Selection",
            "Layout_ID",
            "Config_ID",
            "Style",
            "Layout_Feasible",
            "Robustness",
            "Scenario_Pass_Count",
            "Scenario_Total_Count",
            "Mean_Occupancy_Rate",
            "Worst_Occupancy_Rate",
            "Mean_Space_Left",
            "Worst_Space_Left",
            "Total_Shortfall",
            "Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Additional_Grids_Required",
            "Assigned_Locations_Total",
            "Notes",
        ],
        final_ranking_rows,
    )

    candidate_layout_summary_file = LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary.csv"
    _write_csv_clean(
        candidate_layout_summary_file,
        [
            "Layout_ID",
            "Config_ID",
            "Style",
            "Layout_Feasible",
            "Required_Locations_Total",
            "Assigned_Locations_Total",
            "Total_Physical_Locations",
            "Mean_Occupancy_Rate",
            "Worst_Occupancy_Rate",
            "Space_Left",
            "Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Additional_Grids_Required",
            "Slot_Size_Distribution",
            "Notes",
        ],
        candidate_layout_rows,
    )

    finalist_column_rows = [
        row for row in candidate_layout_column_rows if str(row.get("Layout_ID", "")) in finalist_ids
    ]
    finalist_location_rows = [
        row for row in candidate_layout_location_rows if str(row.get("Layout_ID", "")) in finalist_ids
    ]

    final_layout_column_file = LAYOUT_OUTPUT_DIR / "Final_Layout_By_Rack_Column.csv"
    _write_csv_clean(
        final_layout_column_file,
        [
            "Layout_ID",
            "Config_ID",
            "Style",
            "Rack_Column",
            "Beam_Count_Used",
            "Allowed_Used_Height_cm",
            "Assigned_Used_Height_cm",
            "Remaining_Height_cm",
            "Fill_Ratio",
            "Beam_Relocations_In_Column",
            "Slot_Size_Distribution",
        ],
        finalist_column_rows,
    )

    final_layout_location_file = LAYOUT_OUTPUT_DIR / "Final_Layout_By_Location.csv"
    _write_csv_clean(
        final_layout_location_file,
        [
            "Layout_ID",
            "Config_ID",
            "Style",
            "Location",
            "Rack",
            "Column",
            "Row",
            "Beam_Coordinate",
            "Assignment_Unit_ID",
            "Assignment_Unit_Type",
            "Assigned_Slot_Size_cm",
        ],
        finalist_location_rows,
    )

    model_file = CONSTRAINT_OUTPUT_DIR / "Constraint_Model_By_Method_Scenario_K.csv"
    fields = list(model_rows[0].keys()) if model_rows else [
        "Method", "Scenario", "K", "SKU_Scenario", "SKU_Count",
        "Total_Location_Decision", "Coverage_Basis",
        "Slot_Sizes", "Rack_Column_Division", "Location_Coding", "Beam_Coupling_Constraint", "Doorgang_Fixed_Heights",
    ]

    _write_csv_clean(model_file, fields, model_rows)

    return OUTPUT_DIR


if __name__ == "__main__":
    output_path = build_configuration_model_constraints()
    print(f"Slot-size configuration model (constraints only, no solving) written to: {output_path}")
