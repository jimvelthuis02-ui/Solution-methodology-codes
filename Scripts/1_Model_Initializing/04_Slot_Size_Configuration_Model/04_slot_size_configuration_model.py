import csv
import math
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INPUT_PREPARED = ROOT / "Output" / "1_Initial" / "01_Data_Preparation" / "01_Location_Details_Prepared.csv"
INPUT_SCENARIOS = ROOT / "Output" / "1_Initial" / "02_Scenario_Generation" / "02_Item_Height_Scenarios_Delta_Weighted.csv"
SLOT_SIZE_ROOT = ROOT / "Output" / "1_Initial" / "03_Slot_Size_Generation"
OUTPUT_DIR = ROOT / "Output" / "1_Initial" / "04_Slot_Size_Configuration_Model"

METHODS = ("quantile_binning", "hierarchical_clustering", "kmeans_clustering")
MAX_OCCUPANCY_RATE = 0.85
DEFAULT_SKU_COUNT = 843
MIN_HIGH_NON_OCCUPIED_SHARE = 0.50
HIGH_SLOT_THRESHOLD = 99.0

RACK_EXPECTED_COLUMNS = {
    "A": 15,
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
LOCATION_CODE_PATTERN = re.compile(r"^([A-Z])(\d{2})(\d{2})$")


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


def _static_checks(prepared_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    parsed = []
    invalid_codes = 0
    for row in prepared_rows:
        location = str(row.get("Location", "")).strip()
        parsed_code = _parse_location_code(location)
        if parsed_code is None:
            invalid_codes += 1
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
    for rows in column_groups.values():
        slot_heights = [_to_float(row.get("Location height")) for row in rows]
        slot_heights = [height for height in slot_heights if height is not None]
        beam_count = len(rows)
        max_used_height = MAX_USED_HEIGHT_BASE - beam_count
        used_height = sum(slot_heights)
        if used_height > max_used_height + 1e-9:
            violating_columns += 1

    checks.append({"Constraint": "Column max used height (754 - beam_count)", "Status": "PASS" if violating_columns == 0 else "FAIL", "Details": f"Violating columns: {violating_columns}"})

    doorgang_rows = [row for row in prepared_rows if str(row.get("Location Type", "")).strip().lower() == "doorgang"]
    checks.append({"Constraint": "Doorgang heights fixed", "Status": "ASSUMED", "Details": f"Baseline doorgang locations tracked: {len(doorgang_rows)}"})

    return checks


def _load_method_rows(method: str, file_name: str) -> list[dict[str, str]]:
    return _read_csv(SLOT_SIZE_ROOT / method / file_name)


def _method_constraint_rows(method: str, assignments: list[dict[str, str]], summaries: list[dict[str, str]], sku_count: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    combos = sorted({(row["Scenario"], row["K"]) for row in summaries})
    min_locations_required = math.ceil(sku_count / MAX_OCCUPANCY_RATE)

    for scenario, k in combos:
        combo_assignments = [row for row in assignments if row.get("Scenario") == scenario and row.get("K") == k]
        total_locations = len(combo_assignments)
        occupancy_ok = total_locations >= min_locations_required

        non_occupied = max(total_locations - sku_count, 0)
        required_high_non_occupied = math.ceil(non_occupied * MIN_HIGH_NON_OCCUPIED_SHARE)

        high_slot_locations = 0
        for row in combo_assignments:
            slot_size = _to_float(row.get("Representative Slot Size"))
            if slot_size is not None and slot_size >= HIGH_SLOT_THRESHOLD:
                high_slot_locations += 1

        high_non_occupied_ok = high_slot_locations >= required_high_non_occupied

        slot_sizes = sorted({_to_float(row.get("Representative Slot Size")) for row in combo_assignments if _to_float(row.get("Representative Slot Size")) is not None})
        slot_size_text = ",".join(f"{value:.0f}" for value in slot_sizes)

        rows.append(
            {
                "Method": method,
                "Scenario": scenario,
                "K": k,
                "SKU_Count": str(sku_count),
                "Total_Locations": str(total_locations),
                "Min_Locations_Required_At_85pct": str(min_locations_required),
                "Occupancy_Constraint_OK": "PASS" if occupancy_ok else "FAIL",
                "Non_Occupied_Locations": str(non_occupied),
                "High_Slot_Threshold_cm": f"{HIGH_SLOT_THRESHOLD:.0f}",
                "Required_High_Non_Occupied_Count": str(required_high_non_occupied),
                "High_Slot_Locations_Available": str(high_slot_locations),
                "High_Non_Occupied_Constraint_OK_Proxy": "PASS" if high_non_occupied_ok else "FAIL",
                "Slot_Sizes": slot_size_text,
                "Rack_Column_Division": "FIXED (validated in static checks)",
                "Location_Coding": "FIXED (validated in static checks)",
                "Beam_Coupling_Constraint": "REGISTERED (not solved)",
                "Doorgang_Fixed_Heights": "REGISTERED (not solved)",
            }
        )

    return rows


def build_configuration_model_constraints(sku_count: int = DEFAULT_SKU_COUNT) -> Path:
    prepared_rows = _read_csv(INPUT_PREPARED)
    _ = _read_csv(INPUT_SCENARIOS)

    static_rows = _static_checks(prepared_rows)
    model_rows: list[dict[str, str]] = []

    for method in METHODS:
        assignments = _load_method_rows(method, "Slot_Size_Configuration_Assignments.csv")
        summaries = _load_method_rows(method, "Slot_Size_Configuration_Summary.csv")
        model_rows.extend(_method_constraint_rows(method, assignments, summaries, sku_count))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    static_file = OUTPUT_DIR / "Constraint_Static_Checks.csv"
    with static_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=["Constraint", "Status", "Details"])
        writer.writeheader()
        writer.writerows(static_rows)

    model_file = OUTPUT_DIR / "Constraint_Model_By_Method_Scenario_K.csv"
    fields = list(model_rows[0].keys()) if model_rows else [
        "Method", "Scenario", "K", "SKU_Count", "Total_Locations", "Min_Locations_Required_At_85pct",
        "Occupancy_Constraint_OK", "Non_Occupied_Locations", "High_Slot_Threshold_cm",
        "Required_High_Non_Occupied_Count", "High_Slot_Locations_Available", "High_Non_Occupied_Constraint_OK_Proxy",
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
