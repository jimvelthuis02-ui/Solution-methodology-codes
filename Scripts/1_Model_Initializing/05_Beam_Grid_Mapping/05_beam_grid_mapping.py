import csv
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INPUT_FILE = ROOT / "Output" / "1_Initial" / "01_Data_Preparation" / "01_Location_Details_Prepared.csv"
OUTPUT_DIR = ROOT / "Output" / "1_Initial" / "05_Beam_Grid_Mapping"
BEAM_HEIGHT_CM = 16.0

ROW_ORDER_PATTERN = re.compile(r"^(\d+)([A-Za-z]?)$")

# Explicit geometry overrides confirmed from warehouse layout notes.
FORCED_BEAM_POINTS: set[tuple[str, int, str]] = {
    ("F", 0, "05"),
    ("F", 1, "05"),
    ("F", 2, "05"),
    ("F", 3, "05"),
    ("F", 4, "06"),
    ("F", 5, "06"),
    ("F", 6, "06"),
    ("K", 7, "2b"),
}

DISABLED_BEAM_POINTS: set[tuple[str, int, str]] = {
    ("K", 7, "02"),
}

BEAM_ALIASES: dict[tuple[str, int, str], tuple[str, int, str]] = {
    ("K", 7, "02"): ("K", 7, "2b"),
}


def _read_rows() -> list[dict[str, str]]:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")
    with INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Input CSV has no header row.")
        return list(reader)


def _row_sort_key(row_label: object) -> tuple[int, str]:
    text = str(row_label).strip()
    match = ROW_ORDER_PATTERN.match(text)
    if match is None:
        return (10**9, text.lower())
    return (int(match.group(1)), match.group(2).lower())


def _location_sort_key(row: dict[str, str]) -> tuple[int, str, str]:
    return (*_row_sort_key(row.get("Row")), str(row.get("Location", "")).strip())


def _to_int(text: object, default: int = -1) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def _to_float(text: object, default: float = 0.0) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def _segment_bounds(max_col: int) -> list[tuple[int, int]]:
    if max_col < 0:
        return []

    bounds: list[tuple[int, int]] = []
    start = 0

    first_end = min(max_col, 3)
    bounds.append((start, first_end))
    start = first_end + 1

    while start <= max_col:
        end = min(max_col, start + 2)
        bounds.append((start, end))
        start = end + 1

    return bounds


def _row_number(row_label: str) -> int | None:
    match = ROW_ORDER_PATTERN.match(row_label.strip())
    if match is None:
        return None
    return int(match.group(1))


def _is_row_without_beam_support(row_label: str) -> bool:
    match = ROW_ORDER_PATTERN.match(row_label.strip())
    if match is None:
        return False
    row_num = int(match.group(1))
    suffix = match.group(2).lower()
    return row_num == 1 and suffix in ("", "a")


def build_beam_grid_map() -> Path:
    rows = _read_rows()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    grouped: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    rack_columns: dict[str, set[int]] = defaultdict(set)
    row_index: dict[tuple[str, int, str], dict[str, str]] = {}

    for row in rows:
        rack = str(row.get("Rack", "")).strip()
        column_num = _to_int(row.get("Column"))
        if rack and column_num >= 0:
            grouped[(rack, column_num)].append(row)
            rack_columns[rack].add(column_num)
            row_label = str(row.get("Row", "")).strip()
            row_index[(rack, column_num, row_label)] = row

    beam_segments: list[dict[str, str]] = []
    location_map: list[dict[str, str]] = []
    beam_for_location: dict[tuple[str, int, str], dict[str, str]] = {}

    for rack in sorted(rack_columns):
        max_col = max(rack_columns[rack]) if rack_columns[rack] else -1
        segments = _segment_bounds(max_col)

        rack_row_labels = {
            str(row.get("Row", "")).strip()
            for (current_rack, _), rack_rows in grouped.items()
            if current_rack == rack
            for row in rack_rows
        }
        rack_row_labels.update(row_label for current_rack, _, row_label in FORCED_BEAM_POINTS if current_rack == rack)

        for row_label in sorted(rack_row_labels, key=_row_sort_key):
            if _is_row_without_beam_support(row_label):
                continue

            for seg_index, (seg_start, seg_end) in enumerate(segments, start=1):
                supported_cols: list[int] = []
                for col_num in range(seg_start, seg_end + 1):
                    key = (rack, col_num, row_label)
                    source_row = row_index.get(key)
                    beam_supported = False
                    if source_row is not None:
                        beam_text = str(source_row.get("Beam column count", "")).strip()
                        beam_supported = beam_text != ""
                    if key in FORCED_BEAM_POINTS:
                        beam_supported = True
                    if key in DISABLED_BEAM_POINTS:
                        beam_supported = False
                    if beam_supported:
                        supported_cols.append(col_num)

                if not supported_cols:
                    continue

                start_col = min(supported_cols)
                end_col = max(supported_cols)
                span_cells = len(supported_cols)
                start_col_str = f"{start_col:02d}"
                end_col_str = f"{end_col:02d}"
                segment_label = f"S{seg_index:02d}"
                beam_id = f"{rack}{start_col_str}-{end_col_str}.{segment_label}"
                beam_coordinate = f"{rack}[{start_col_str}-{end_col_str}]:{row_label}"

                beam_segments.append(
                    {
                        "Beam_Coordinate": beam_coordinate,
                        "Rack": rack,
                        "Column_Start": start_col_str,
                        "Column_End": end_col_str,
                        "Row": row_label,
                        "Spanned_Locations": str(span_cells),
                    }
                )

                for col_num in supported_cols:
                    beam_for_location[(rack, col_num, row_label)] = {
                        "Beam_Coordinate": beam_coordinate,
                        "Spanned_Locations": str(span_cells),
                    }

    for target_key, source_key in BEAM_ALIASES.items():
        beam_data = beam_for_location.get(source_key)
        if beam_data is not None:
            beam_for_location[target_key] = dict(beam_data)

    for (rack, col_num), rack_rows in sorted(grouped.items()):
        column_str = f"{col_num:02d}"
        ordered_rows = sorted(rack_rows, key=_location_sort_key)
        for row in ordered_rows:
            row_label = str(row.get("Row", "")).strip()
            location = str(row.get("Location", "")).strip()
            beam_data = beam_for_location.get((rack, col_num, row_label), {})
            has_beam = "YES" if (beam_data and not _is_row_without_beam_support(row_label)) else "NO"

            location_map.append(
                {
                    "Location": location,
                    "Rack": rack,
                    "Column": column_str,
                    "Row": row_label,
                    "Beam_Supported": has_beam,
                    "Has_Grid": has_beam,
                    "Beam_Coordinate": beam_data.get("Beam_Coordinate", ""),
                    "Spanned_Locations": beam_data.get("Spanned_Locations", ""),
                }
            )

    beam_file = OUTPUT_DIR / "Beam_Segments.csv"
    location_file = OUTPUT_DIR / "Location_Beam_Map.csv"
    beam_height_file = OUTPUT_DIR / "Beam_Height_Coordinates.csv"
    summary_file = OUTPUT_DIR / "Beam_Grid_Summary.csv"

    with beam_file.open("w", newline="", encoding="utf-8") as target:
        fieldnames = [
            "Beam_Coordinate",
            "Rack",
            "Column_Start",
            "Column_End",
            "Row",
            "Spanned_Locations",
        ]
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(beam_segments)

    with location_file.open("w", newline="", encoding="utf-8") as target:
        fieldnames = [
            "Location",
            "Rack",
            "Column",
            "Row",
            "Beam_Supported",
            "Has_Grid",
            "Beam_Coordinate",
            "Spanned_Locations",
        ]
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(location_map)

    elevation_samples: dict[str, list[tuple[float, float]]] = defaultdict(list)
    beam_meta: dict[str, dict[str, str]] = {}

    for (rack, col_num), rack_rows in sorted(grouped.items()):
        ordered_rows = sorted(rack_rows, key=_location_sort_key)
        cumulative_height_cm = 0.0
        beam_count_below = 0

        for row in ordered_rows:
            row_label = str(row.get("Row", "")).strip()
            beam_data = beam_for_location.get((rack, col_num, row_label), {})
            has_beam = bool(beam_data) and not _is_row_without_beam_support(row_label)

            if has_beam:
                beam_coordinate = str(beam_data.get("Beam_Coordinate", "")).strip()
                if beam_coordinate:
                    bottom_cm = cumulative_height_cm + (beam_count_below * BEAM_HEIGHT_CM)
                    top_cm = bottom_cm + BEAM_HEIGHT_CM
                    elevation_samples[beam_coordinate].append((bottom_cm, top_cm))
                    if beam_coordinate not in beam_meta:
                        beam_meta[beam_coordinate] = {
                            "Rack": rack,
                            "Row": row_label,
                            "Spanned_Locations": str(beam_data.get("Spanned_Locations", "")),
                        }

            cumulative_height_cm += _to_float(row.get("Location height"))
            if has_beam:
                beam_count_below += 1

    beam_heights: list[dict[str, str]] = []
    for beam_coordinate in sorted(elevation_samples):
        samples = elevation_samples[beam_coordinate]
        bottoms = [sample[0] for sample in samples]
        tops = [sample[1] for sample in samples]
        meta = beam_meta.get(beam_coordinate, {})
        beam_heights.append(
            {
                "Beam_Coordinate": beam_coordinate,
                "Rack": str(meta.get("Rack", "")),
                "Row": str(meta.get("Row", "")),
                "Spanned_Locations": str(meta.get("Spanned_Locations", "")),
                "Beam_Bottom_cm": f"{(sum(bottoms) / len(bottoms)):.2f}",
                "Beam_Top_cm": f"{(sum(tops) / len(tops)):.2f}",
                "Bottom_Min_cm": f"{min(bottoms):.2f}",
                "Bottom_Max_cm": f"{max(bottoms):.2f}",
                "Top_Min_cm": f"{min(tops):.2f}",
                "Top_Max_cm": f"{max(tops):.2f}",
                "Height_Sample_Columns": str(len(samples)),
            }
        )

    with beam_height_file.open("w", newline="", encoding="utf-8") as target:
        fieldnames = [
            "Beam_Coordinate",
            "Rack",
            "Row",
            "Spanned_Locations",
            "Beam_Bottom_cm",
            "Beam_Top_cm",
            "Bottom_Min_cm",
            "Bottom_Max_cm",
            "Top_Min_cm",
            "Top_Max_cm",
            "Height_Sample_Columns",
        ]
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(beam_heights)

    summary_rows = [
        {"Metric": "Total locations", "Value": str(len(rows))},
        {"Metric": "Beam-supported locations", "Value": str(sum(1 for row in location_map if row["Beam_Supported"] == "YES"))},
        {"Metric": "Non-beam locations (floor)", "Value": str(sum(1 for row in location_map if row["Beam_Supported"] == "NO"))},
        {"Metric": "Beam objects (actual horizontal spans)", "Value": str(len(beam_segments))},
        {"Metric": "Columns with beam support", "Value": str(len({(row["Rack"], row["Column"]) for row in rows if str(row.get("Beam column count", "")).strip()}))},
        {"Metric": "Beam height objects", "Value": str(len(beam_heights))},
        {"Metric": "Beam thickness (cm)", "Value": f"{BEAM_HEIGHT_CM:.0f}"},
    ]

    with summary_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=["Metric", "Value"])
        writer.writeheader()
        writer.writerows(summary_rows)

    return OUTPUT_DIR


if __name__ == "__main__":
    output_path = build_beam_grid_map()
    print(f"Beam/grid map written to: {output_path}")
