import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INPUT_FILE = ROOT / "Output" / "01_Data_Preparation" / "Location_Details_Prepared.csv"
OUTPUT_FILE = ROOT / "Output" / "02_Scenario_Generation" / "02_Item_Height_Scenarios_Delta_Weighted.csv"

LOCATION_COLUMN = "Location"
LOCATION_HEIGHT_COLUMN = "Location height"
ITEM_HEIGHT_COLUMN = "Item height"
DELTA_COLUMN = "Delta"
LOCATION_TYPE_COLUMN = "Location Type"

# Locations with fixed items that must be picked entirely at once (delta does not change)
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


def _format(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def _eligible(row: dict[str, str]) -> bool:
    item_height = _to_float(row.get(ITEM_HEIGHT_COLUMN))
    location_type = str(row.get(LOCATION_TYPE_COLUMN, "")).strip().lower()
    return not (item_height == 0.0 or location_type == "doorgang")


def generate_weighted_delta_scenarios() -> Path:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")

    with INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Input CSV has no header row.")
        rows = list(reader)

    required = [LOCATION_COLUMN, LOCATION_HEIGHT_COLUMN, ITEM_HEIGHT_COLUMN, DELTA_COLUMN]
    missing = [column for column in required if column not in reader.fieldnames]
    if missing:
        raise KeyError("Missing required columns: " + ", ".join(missing))

    output_fields = [
        LOCATION_COLUMN,
        LOCATION_HEIGHT_COLUMN,
        ITEM_HEIGHT_COLUMN,
        DELTA_COLUMN,
        "Scenario_1_Item_Height",
        "Scenario_2_Item_Height",
        "Scenario_3_Item_Height",
        "Scenario_4_Item_Height",
        "Scenario_5_Item_Height",
        "Scenario_6_Item_Height",
    ]

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=output_fields)
        writer.writeheader()

        for row in rows:
            if not _eligible(row):
                continue

            location = str(row.get(LOCATION_COLUMN, "")).strip()
            slot_height = _to_float(row.get(LOCATION_HEIGHT_COLUMN))
            item_height = _to_float(row.get(ITEM_HEIGHT_COLUMN))
            delta = _to_float(row.get(DELTA_COLUMN))
            if item_height is None:
                continue

            # Hard-fix locations: all scenarios at current item height
            if location in FIXED_HEIGHT_LOCATIONS:
                writer.writerow(
                    {
                        LOCATION_COLUMN: location,
                        LOCATION_HEIGHT_COLUMN: _format(slot_height),
                        ITEM_HEIGHT_COLUMN: _format(item_height),
                        DELTA_COLUMN: _format(delta),
                        "Scenario_1_Item_Height": _format(item_height),
                        "Scenario_2_Item_Height": _format(item_height),
                        "Scenario_3_Item_Height": _format(item_height),
                        "Scenario_4_Item_Height": _format(item_height),
                        "Scenario_5_Item_Height": _format(item_height),
                        "Scenario_6_Item_Height": _format(item_height),
                    }
                )
                continue

            if slot_height is None or delta is None:
                continue

            # Locations with delta < 5: all scenarios at current item height
            if delta < 5:
                writer.writerow(
                    {
                        LOCATION_COLUMN: location,
                        LOCATION_HEIGHT_COLUMN: _format(slot_height),
                        ITEM_HEIGHT_COLUMN: _format(item_height),
                        DELTA_COLUMN: _format(delta),
                        "Scenario_1_Item_Height": _format(item_height),
                        "Scenario_2_Item_Height": _format(item_height),
                        "Scenario_3_Item_Height": _format(item_height),
                        "Scenario_4_Item_Height": _format(item_height),
                        "Scenario_5_Item_Height": _format(item_height),
                        "Scenario_6_Item_Height": _format(item_height),
                    }
                )
                continue

            # Locations with delta >= 5: weighted deltas with clearance constraint (ratchet)
            # Scenarios 1-5 freeze when clearance drops below 5; Scenario 6 always uses slot_height - 5
            scenario_values = [item_height]  # Scenario 1

            for weight in [0.2, 0.4, 0.6, 0.8]:  # Scenarios 2-5
                candidate = item_height + weight * delta
                clearance = slot_height - candidate
                if clearance >= 5:
                    scenario_values.append(candidate)
                else:
                    # Freeze at previous scenario value
                    scenario_values.append(scenario_values[-1])

            # Scenario 6: always apply slot_height - 5 (no freezing)
            scenario_values.append(slot_height - 5.0)

            writer.writerow(
                {
                    LOCATION_COLUMN: location,
                    LOCATION_HEIGHT_COLUMN: _format(slot_height),
                    ITEM_HEIGHT_COLUMN: _format(item_height),
                    DELTA_COLUMN: _format(delta),
                    "Scenario_1_Item_Height": _format(scenario_values[0]),
                    "Scenario_2_Item_Height": _format(scenario_values[1]),
                    "Scenario_3_Item_Height": _format(scenario_values[2]),
                    "Scenario_4_Item_Height": _format(scenario_values[3]),
                    "Scenario_5_Item_Height": _format(scenario_values[4]),
                    "Scenario_6_Item_Height": _format(scenario_values[5]),
                }
            )

    return OUTPUT_FILE


if __name__ == "__main__":
    output_path = generate_weighted_delta_scenarios()
    print(f"Scenario generation complete. Output written to: {output_path}")
