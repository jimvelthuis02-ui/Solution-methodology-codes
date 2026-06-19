import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INPUT_FILE = ROOT / "Output" / "1_Initial" / "01_Data_Preparation" / "01_Location_Details_Prepared.csv"
OUTPUT_FILE = ROOT / "Output" / "1_Initial" / "02_Scenario_Generation" / "02_Item_Height_Scenarios_Delta_Weighted.csv"

LOCATION_COLUMN = "Location"
LOCATION_HEIGHT_COLUMN = "Location height"
ITEM_HEIGHT_COLUMN = "Item height"
DELTA_COLUMN = "Delta"
STATUS_COLUMN = "Status"
LOCATION_TYPE_COLUMN = "Location Type"


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
    status = str(row.get(STATUS_COLUMN, "")).strip().lower()
    item_height = _to_float(row.get(ITEM_HEIGHT_COLUMN))
    location_type = str(row.get(LOCATION_TYPE_COLUMN, "")).strip().lower()
    return not (status == "empty" or item_height == 0.0 or location_type == "doorgang")


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

            slot_height = _to_float(row.get(LOCATION_HEIGHT_COLUMN))
            item_height = _to_float(row.get(ITEM_HEIGHT_COLUMN))
            delta = _to_float(row.get(DELTA_COLUMN))
            if slot_height is None or item_height is None or delta is None:
                continue

            writer.writerow(
                {
                    LOCATION_COLUMN: str(row.get(LOCATION_COLUMN, "")).strip(),
                    LOCATION_HEIGHT_COLUMN: _format(slot_height),
                    ITEM_HEIGHT_COLUMN: _format(item_height),
                    DELTA_COLUMN: _format(delta),
                    "Scenario_1_Item_Height": _format(item_height),
                    "Scenario_2_Item_Height": _format(item_height + 0.2 * delta),
                    "Scenario_3_Item_Height": _format(item_height + 0.4 * delta),
                    "Scenario_4_Item_Height": _format(item_height + 0.6 * delta),
                    "Scenario_5_Item_Height": _format(item_height + 0.8 * delta),
                    "Scenario_6_Item_Height": _format(slot_height - 5.0),
                }
            )

    return OUTPUT_FILE


if __name__ == "__main__":
    output_path = generate_weighted_delta_scenarios()
    print(f"Scenario generation complete. Output written to: {output_path}")
