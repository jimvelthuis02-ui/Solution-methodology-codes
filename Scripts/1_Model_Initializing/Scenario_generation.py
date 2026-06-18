import csv
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = ROOT / "Output" / "1_Initial" / "Location_Details_Preliminary_Delta.csv"
OUTPUT_FILE = ROOT / "Output" / "1_Initial" / "Location_Details_Slot_Height_Quartiles.csv"
DELTA_PERCENTILES_OUTPUT_FILE = ROOT / "Output" / "1_Initial" / "Delta_Percentiles_By_Slot_Height_Group.csv"
ITEM_HEIGHT_SCENARIOS_OUTPUT_FILE = ROOT / "Output" / "1_Initial" / "Item_Height_Scenarios.csv"
SLOT_HEIGHT_COLUMN = "Location height"
STATUS_COLUMN = "Status"
ITEM_HEIGHT_COLUMN = "Item height"
LOCATION_TYPE_COLUMN = "Location Type"
LOCATION_COLUMN = "Location"
DELTA_COLUMN = "Delta"
SLOT_HEIGHT_GROUP_COLUMN = "Slot Height Group"

FIXED_HEIGHT_LOCATIONS = {
    "A0002", "A0101", "A0102", "A0201", "A0202", "A0301", "A0302", "A0401",
    "A0402", "A0502", "A0601", "A0602", "A0702", "A0802", "A0902", "A1001",
    "A1002", "A1101", "A1102", "A1201", "A1202", "B0001", "B0002", "B0101",
    "B0102", "B0201", "B0202", "B0302", "B0401", "B0402", "B0501", "B0502",
    "B0601", "B0602", "B0701", "B0702", "B0801", "B0802", "B0902", "B1001",
    "B1102", "B1602", "B1702", "D0005", "D0105", "D0205", "D0305", "D0405",
    "D0505", "D0605", "D0705", "D0905", "D1005", "D1105", "D1205", "D1305",
    "D1405", "D1505", "D1605", "D1705", "D1805", "D2005", "H0203"
}

PERCENTILES = [0.10, 0.25, 0.50, 0.75, 0.90]
GROUP_ORDER = ["Group 1", "Group 2", "Group 3", "Group 4"]


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


def _percentile_linear(values: list[float], percentile: float) -> float:
    """Compute percentile using linear interpolation between closest ranks."""
    if not values:
        raise ValueError("Cannot compute percentiles from an empty list.")

    if len(values) == 1:
        return values[0]

    sorted_values = sorted(values)
    rank = (len(sorted_values) - 1) * percentile
    lower_index = int(math.floor(rank))
    upper_index = int(math.ceil(rank))

    if lower_index == upper_index:
        return sorted_values[lower_index]

    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    weight = rank - lower_index
    return lower_value + (upper_value - lower_value) * weight


def _quartile_group(value: float, p25: float, p50: float, p75: float) -> str:
    if value <= p25:
        return "Group 1"
    if value <= p50:
        return "Group 2"
    if value <= p75:
        return "Group 3"
    return "Group 4"


def _is_excluded_location(row: dict[str, object | None]) -> bool:
    status = str(row.get(STATUS_COLUMN, "")).strip().lower()
    item_height = _to_float(row.get(ITEM_HEIGHT_COLUMN))
    location_type = str(row.get(LOCATION_TYPE_COLUMN, "")).strip().lower()
    return status == "empty" or item_height == 0.0 or location_type == "doorgang"


def generate_slot_height_quartiles() -> Path:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")

    with INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Input CSV has no header row.")

        rows = list(reader)

    if SLOT_HEIGHT_COLUMN not in reader.fieldnames:
        raise KeyError(
            f"Missing required column: {SLOT_HEIGHT_COLUMN}. "
            f"Expected it in {INPUT_FILE.name}."
        )

    required_columns = [STATUS_COLUMN, ITEM_HEIGHT_COLUMN, LOCATION_TYPE_COLUMN]
    missing_required = [
        column for column in required_columns if column not in reader.fieldnames
    ]
    if missing_required:
        raise KeyError(
            "Missing required columns for exclusion rule: "
            + ", ".join(missing_required)
        )

    filtered_rows = [row for row in rows if not _is_excluded_location(row)]

    slot_heights = [
        parsed
        for row in filtered_rows
        for parsed in [_to_float(row.get(SLOT_HEIGHT_COLUMN))]
        if parsed is not None
    ]

    if not slot_heights:
        raise ValueError("No valid slot height values found for quartile calculation.")

    p25 = _percentile_linear(slot_heights, 0.25)
    p50 = _percentile_linear(slot_heights, 0.50)
    p75 = _percentile_linear(slot_heights, 0.75)

    output_fields = list(reader.fieldnames)
    if "Slot Height Group" not in output_fields:
        output_fields.append("Slot Height Group")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=output_fields)
        writer.writeheader()

        for row in filtered_rows:
            value = _to_float(row.get(SLOT_HEIGHT_COLUMN))
            if value is None:
                row["Slot Height Group"] = ""
            else:
                row["Slot Height Group"] = _quartile_group(value, p25, p50, p75)
            writer.writerow(row)

    print(
        "Quartile thresholds (slot height): "
        f"P25={p25:.3f}, P50={p50:.3f}, P75={p75:.3f}"
    )
    return OUTPUT_FILE


def _normalized_location_code(row: dict[str, object | None]) -> str:
    return str(row.get(LOCATION_COLUMN, "")).strip().upper()


def calculate_delta_percentiles_per_slot_height_group() -> Path:
    if not OUTPUT_FILE.exists():
        raise FileNotFoundError(f"Missing quartile input file: {OUTPUT_FILE}")

    with OUTPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Quartile CSV has no header row.")

        rows = list(reader)

    required_columns = [LOCATION_COLUMN, DELTA_COLUMN, SLOT_HEIGHT_GROUP_COLUMN]
    missing_required = [
        column for column in required_columns if column not in reader.fieldnames
    ]
    if missing_required:
        raise KeyError(
            "Missing required columns for delta percentile step: "
            + ", ".join(missing_required)
        )

    group_to_deltas: dict[str, list[float]] = {group: [] for group in GROUP_ORDER}
    for row in rows:
        group = str(row.get(SLOT_HEIGHT_GROUP_COLUMN, "")).strip()
        if group not in group_to_deltas:
            continue

        location_code = _normalized_location_code(row)
        if location_code in FIXED_HEIGHT_LOCATIONS:
            continue

        delta_value = _to_float(row.get(DELTA_COLUMN))
        if delta_value is None or delta_value < 5.0:
            continue

        group_to_deltas[group].append(delta_value)

    output_fields = [
        "Slot Height Group",
        "Location Count",
        "P10_Delta",
        "P25_Delta",
        "P50_Delta",
        "P75_Delta",
        "P90_Delta",
    ]

    DELTA_PERCENTILES_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DELTA_PERCENTILES_OUTPUT_FILE.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=output_fields)
        writer.writeheader()

        for group in GROUP_ORDER:
            deltas = group_to_deltas[group]
            row = {
                "Slot Height Group": group,
                "Location Count": str(len(deltas)),
                "P10_Delta": "",
                "P25_Delta": "",
                "P50_Delta": "",
                "P75_Delta": "",
                "P90_Delta": "",
            }

            if deltas:
                percentile_values = [
                    _percentile_linear(deltas, percentile) for percentile in PERCENTILES
                ]
                row["P10_Delta"] = f"{percentile_values[0]:.3f}"
                row["P25_Delta"] = f"{percentile_values[1]:.3f}"
                row["P50_Delta"] = f"{percentile_values[2]:.3f}"
                row["P75_Delta"] = f"{percentile_values[3]:.3f}"
                row["P90_Delta"] = f"{percentile_values[4]:.3f}"

            writer.writerow(row)

    return DELTA_PERCENTILES_OUTPUT_FILE


def generate_item_height_scenarios() -> Path:
    """Step 3: for every location produce item heights for scenarios 1-7.

    Scenario 1 (current): item_height = item_height
    Scenario 2-6:         item_height = slot_height - Px_Delta  (per group)
    Scenario 7:           item_height = slot_height - 5
    """
    if not OUTPUT_FILE.exists():
        raise FileNotFoundError(f"Missing quartile input file: {OUTPUT_FILE}")
    if not DELTA_PERCENTILES_OUTPUT_FILE.exists():
        raise FileNotFoundError(
            f"Missing delta percentile file: {DELTA_PERCENTILES_OUTPUT_FILE}"
        )

    # Load per-group delta percentile lookup.
    group_percentiles: dict[str, dict[str, float | None]] = {}
    with DELTA_PERCENTILES_OUTPUT_FILE.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            group = row["Slot Height Group"].strip()
            group_percentiles[group] = {
                "P10": _to_float(row.get("P10_Delta")),
                "P25": _to_float(row.get("P25_Delta")),
                "P50": _to_float(row.get("P50_Delta")),
                "P75": _to_float(row.get("P75_Delta")),
                "P90": _to_float(row.get("P90_Delta")),
            }

    with OUTPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Quartile CSV has no header row.")
        rows = list(reader)

    output_fields = [
        "Location",
        "Slot Height Group",
        "Location height",
        "Scenario_1_Item_Height",
        "Scenario_2_Item_Height",
        "Scenario_3_Item_Height",
        "Scenario_4_Item_Height",
        "Scenario_5_Item_Height",
        "Scenario_6_Item_Height",
        "Scenario_7_Item_Height",
    ]

    ITEM_HEIGHT_SCENARIOS_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ITEM_HEIGHT_SCENARIOS_OUTPUT_FILE.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=output_fields)
        writer.writeheader()

        for row in rows:
            location = str(row.get(LOCATION_COLUMN, "")).strip()
            group = str(row.get(SLOT_HEIGHT_GROUP_COLUMN, "")).strip()
            slot_height = _to_float(row.get(SLOT_HEIGHT_COLUMN))
            item_height = _to_float(row.get(ITEM_HEIGHT_COLUMN))
            gp = group_percentiles.get(group, {})

            def _scenario(delta: float | None) -> str:
                if slot_height is None or delta is None:
                    return ""
                return f"{slot_height - delta:.3f}"

            writer.writerow({
                "Location": location,
                "Slot Height Group": group,
                "Location height": "" if slot_height is None else f"{slot_height:g}",
                "Scenario_1_Item_Height": "" if item_height is None else f"{item_height:g}",
                "Scenario_2_Item_Height": _scenario(gp.get("P90")),
                "Scenario_3_Item_Height": _scenario(gp.get("P75")),
                "Scenario_4_Item_Height": _scenario(gp.get("P50")),
                "Scenario_5_Item_Height": _scenario(gp.get("P25")),
                "Scenario_6_Item_Height": _scenario(gp.get("P10")),
                "Scenario_7_Item_Height": _scenario(5.0),
            })

    return ITEM_HEIGHT_SCENARIOS_OUTPUT_FILE


if __name__ == "__main__":
    quartile_output_path = generate_slot_height_quartiles()
    print(f"Scenario generation quartile step complete. Output written to: {quartile_output_path}")

    delta_percentile_output_path = calculate_delta_percentiles_per_slot_height_group()
    print(
        "Delta percentile step complete. Output written to: "
        f"{delta_percentile_output_path}"
    )

    scenarios_output_path = generate_item_height_scenarios()
    print(
        "Item height scenario step complete. Output written to: "
        f"{scenarios_output_path}"
    )
