import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = ROOT / "Output" / "1_Initial" / "Location_Details_Slot_Height_Quartiles.csv"
OUTPUT_FILE = ROOT / "Output" / "1_Initial" / "Item_Height_Scenarios_Delta_Weighted.csv"
LOCATION_COLUMN = "Location"
LOCATION_HEIGHT_COLUMN = "Location height"
ITEM_HEIGHT_COLUMN = "Item height"
DELTA_COLUMN = "Delta"


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


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.3f}"


def _calculate_scenarios(item_height: float, slot_height: float, delta: float) -> dict[str, float]:
    return {
        "Scenario_1_Item_Height": item_height,
        "Scenario_2_Item_Height": item_height + 0.2 * delta,
        "Scenario_3_Item_Height": item_height + 0.4 * delta,
        "Scenario_4_Item_Height": item_height + 0.6 * delta,
        "Scenario_5_Item_Height": item_height + 0.8 * delta,
        "Scenario_6_Item_Height": slot_height - 5,
    }


def generate_delta_weighted_item_height_scenarios() -> Path:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")

    with INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Input CSV has no header row.")
        rows = list(reader)

    required_columns = [LOCATION_COLUMN, LOCATION_HEIGHT_COLUMN, ITEM_HEIGHT_COLUMN, DELTA_COLUMN]
    missing_columns = [column for column in required_columns if column not in reader.fieldnames]
    if missing_columns:
        raise KeyError(
            "Missing required columns in input file: " + ", ".join(missing_columns)
        )

    output_fields = [
        LOCATION_COLUMN,
        "Slot Height Group",
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
            slot_height = _to_float(row.get(LOCATION_HEIGHT_COLUMN))
            item_height = _to_float(row.get(ITEM_HEIGHT_COLUMN))
            delta = _to_float(row.get(DELTA_COLUMN))

            if slot_height is None or item_height is None or delta is None:
                continue

            scenarios = _calculate_scenarios(item_height, slot_height, delta)
            writer.writerow(
                {
                    LOCATION_COLUMN: str(row.get(LOCATION_COLUMN, "")).strip(),
                    "Slot Height Group": str(row.get("Slot Height Group", "")).strip(),
                    LOCATION_HEIGHT_COLUMN: _format_number(slot_height),
                    ITEM_HEIGHT_COLUMN: _format_number(item_height),
                    DELTA_COLUMN: _format_number(delta),
                    "Scenario_1_Item_Height": _format_number(scenarios["Scenario_1_Item_Height"]),
                    "Scenario_2_Item_Height": _format_number(scenarios["Scenario_2_Item_Height"]),
                    "Scenario_3_Item_Height": _format_number(scenarios["Scenario_3_Item_Height"]),
                    "Scenario_4_Item_Height": _format_number(scenarios["Scenario_4_Item_Height"]),
                    "Scenario_5_Item_Height": _format_number(scenarios["Scenario_5_Item_Height"]),
                    "Scenario_6_Item_Height": _format_number(scenarios["Scenario_6_Item_Height"]),
                }
            )

    return OUTPUT_FILE


if __name__ == "__main__":
    output_path = generate_delta_weighted_item_height_scenarios()
    print(f"Delta-weighted scenario generation complete. Output written to: {output_path}")
