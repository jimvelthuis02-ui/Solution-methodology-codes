import csv
import math
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INPUT_PREPARED = ROOT / "Output" / "01_Data_Preparation" / "Location_Details_Prepared.csv"
INPUT_SCENARIOS = ROOT / "Output" / "02_Scenario_Generation" / "02_Item_Height_Scenarios_Delta_Weighted.csv"
SLOT_SIZE_ROOT = ROOT / "Output" / "03_Slot_Size_Generation"
OUTPUT_DIR = ROOT / "Output" / "04_Slot_Size_Configuration_Model"

METHODS = ("quantile_binning", "hierarchical_clustering", "kmeans_clustering")
MAX_OCCUPANCY_RATE = 0.85
DEFAULT_SKU_COUNT = 843
MIN_HIGH_NON_OCCUPIED_SHARE = 0.50
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


def _method_constraint_rows(
    method: str,
    summaries: list[dict[str, str]],
    sku_count: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    slot_size_rows: list[dict[str, str]] = []
    combos = sorted({(row["Scenario"], row["K"]) for row in summaries})
    min_locations_required = math.ceil(sku_count / MAX_OCCUPANCY_RATE)
    min_high_non_occupied = math.ceil(max(min_locations_required - sku_count, 0) * MIN_HIGH_NON_OCCUPIED_SHARE)

    for scenario, k in combos:
        combo_summaries = [row for row in summaries if row.get("Scenario") == scenario and row.get("K") == k]

        slot_size_counts: list[tuple[float, int]] = []
        for row in combo_summaries:
            slot_size = _to_float(row.get("Representative Slot Size"))
            cluster_count = _to_float(row.get("Cluster Count"))
            if slot_size is None or cluster_count is None:
                continue
            count = int(round(cluster_count))
            slot_size_counts.append((slot_size, count))

        slot_sizes = sorted(slot_size for slot_size, _ in slot_size_counts)
        slot_size_text = ",".join(f"{value:.0f}" for value in slot_sizes)

        running_demand = 0
        cumulative_by_slot_size: dict[float, int] = {}
        for slot_size, count in reversed(slot_size_counts):
            running_demand += count
            cumulative_by_slot_size[slot_size] = running_demand

        for slot_size, count in slot_size_counts:
            eligible_sizes = [candidate for candidate in slot_sizes if candidate >= slot_size]
            decision_terms = " + ".join(_slot_size_variable_name(candidate) for candidate in eligible_sizes)
            slot_size_rows.append(
                {
                    "Method": method,
                    "Scenario": scenario,
                    "K": k,
                    "Representative_Slot_Size": f"{slot_size:.0f}",
                    "Assigned_SKUs_At_Representative_Size": str(count),
                    "Decision_Variable": _slot_size_variable_name(slot_size),
                    "Cumulative_Demand_At_Or_Above_Size": str(cumulative_by_slot_size[slot_size]),
                    "Coverage_Constraint": f"{decision_terms} >= {cumulative_by_slot_size[slot_size]}",
                }
            )

        rows.append(
            {
                "Method": method,
                "Scenario": scenario,
                "K": k,
                "SKU_Count": str(sku_count),
                "Required_Total_Locations_At_85pct": str(min_locations_required),
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


def build_configuration_model_constraints(sku_count: int = DEFAULT_SKU_COUNT) -> Path:
    prepared_rows = _read_csv(INPUT_PREPARED)
    _ = _read_csv(INPUT_SCENARIOS)

    static_rows, invalid_location_rows, violating_column_rows = _static_checks(prepared_rows)
    model_rows: list[dict[str, str]] = []
    slot_size_constraint_rows: list[dict[str, str]] = []

    for method in METHODS:
        summaries = _load_method_rows(method, "Slot_Size_Configuration_Summary.csv")
        method_rows, method_slot_rows = _method_constraint_rows(method, summaries, sku_count)
        model_rows.extend(method_rows)
        slot_size_constraint_rows.extend(method_slot_rows)

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
                "Representative_Slot_Size",
                "Assigned_SKUs_At_Representative_Size",
                "Decision_Variable",
                "Cumulative_Demand_At_Or_Above_Size",
                "Coverage_Constraint",
            ],
        )
        writer.writeheader()
        writer.writerows(slot_size_constraint_rows)

    model_file = OUTPUT_DIR / "Constraint_Model_By_Method_Scenario_K.csv"
    fields = list(model_rows[0].keys()) if model_rows else [
        "Method", "Scenario", "K", "SKU_Count", "Required_Total_Locations_At_85pct",
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
