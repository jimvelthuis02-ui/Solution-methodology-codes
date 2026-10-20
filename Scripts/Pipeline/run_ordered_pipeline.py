import csv
import math
import os
import re
import runpy
import sys
import time
from collections import defaultdict
from pathlib import Path


def _resolve_project_root() -> Path:
    """Find the repository root in both source-tree and PyInstaller execution modes."""
    env_root = os.environ.get("PIPELINE_PROJECT_ROOT")
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        if candidate.exists():
            return candidate

    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            exe_dir,
            exe_dir / "_internal",
            exe_dir.parent,
            Path.cwd(),
        ])
    else:
        candidates.extend([
            Path.cwd(),
            Path(__file__).resolve().parents[2],
            Path(__file__).resolve().parents[1],
            Path(__file__).resolve().parent,
        ])

    for candidate in candidates:
        if not candidate.exists():
            continue
        if (candidate / "Input files").exists() or (candidate / "Scripts" / "Pipeline").exists():
            return candidate.resolve()

    return Path(__file__).resolve().parents[2]


# Shared output locations and stage-to-stage scenario metadata.
ROOT = _resolve_project_root()
OUTPUT_ROOT = Path(os.environ.get("PIPELINE_OUTPUT_DIR", ROOT / "Output")).expanduser().resolve()
STAGE1_OUTPUT_DIR = OUTPUT_ROOT / "01_Data_Preparation"
STAGE2_OUTPUT_DIR = OUTPUT_ROOT / "02_Scenario_Generation"
STAGE3_OUTPUT_DIR = OUTPUT_ROOT / "03_Slot_Size_Generation"
STAGE4_OUTPUT_DIR = OUTPUT_ROOT / "04_Candidate_Configuration"
STAGE5_OUTPUT_DIR = OUTPUT_ROOT / "05_Capacity_Determination"
STAGE6_OUTPUT_DIR = Path(os.environ.get("PIPELINE_STAGE6_OUTPUT_DIR", OUTPUT_ROOT / "06_Layout_Generation"))
STAGE7_OUTPUT_DIR = OUTPUT_ROOT / "07_Robustness_Evaluation"
STAGE8_OUTPUT_DIR = OUTPUT_ROOT / "08_Final_Selection"
SCENARIO_LABELS = {
    "Scenario_1_Item_Height": "Scenario 1",
    "Scenario_2_Item_Height": "Scenario 2",
    "Scenario_3_Item_Height": "Scenario 3",
    "Scenario_4_Item_Height": "Scenario 4",
    "Scenario_5_Item_Height": "Scenario 5",
    "Scenario_6_Item_Height": "Scenario 6",
}

# Stage 3 summary location and supported clustering methods.
SLOT_SIZE_ROOT = STAGE3_OUTPUT_DIR
METHODS = ("quantile_binning", "hierarchical_clustering", "kmeans_clustering")

# Physical geometry limits and structured beam/location coordinate patterns.
COLUMN_MAX_HEIGHT = 770.0
TOP_BEAM_HEIGHT = 16.0
MAX_USED_HEIGHT_BASE = COLUMN_MAX_HEIGHT - TOP_BEAM_HEIGHT
BEAM_HEIGHT = 16.0
BEAM_RELOCATION_TOLERANCE_CM = 0.0

LOCATION_CODE_PATTERN = re.compile(r"^([A-Z])(\d{2})(\d{2})([A-Za-z]+)?$")
BEAM_SPAN_PATTERN = re.compile(r"^([A-Z])\[(\d{2})-(\d{2})\]:([0-9]{1,2}[A-Za-z]?)$")
BEAM_SINGLE_PATTERN = re.compile(r"^([A-Z])(\d{2}):([0-9]{1,2}[A-Za-z]?)$")


def _to_float(value: object | None) -> float | None:
    """Parse a numeric cell, returning ``None`` for blanks and invalid values."""
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


def _format_number(value: float | None) -> str:
    """Format an optional numeric value to three decimal places."""
    return "" if value is None else f"{value:.3f}"


def _to_int_default(value: object | None, default: int = 0) -> int:
    """Parse an integer-like cell, accepting decimal-formatted numeric strings."""
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


def _normalize_distribution_to_target(distribution: dict[int, int], target_total: int) -> dict[int, int]:
    """Scale integer bucket counts to a target total with largest-remainder balancing."""
    if not distribution or target_total <= 0:
        return {}

    current_total = sum(distribution.values())
    if current_total <= 0:
        return {}
    if current_total == target_total:
        return dict(distribution)

    raw_scaled = {size: count * (target_total / current_total) for size, count in distribution.items()}
    normalized = {size: int(math.floor(value)) for size, value in raw_scaled.items()}
    remainder = target_total - sum(normalized.values())
    if remainder > 0:
        ranked = sorted(
            distribution,
            key=lambda size: (raw_scaled[size] - normalized[size], -size),
            reverse=True,
        )
        for size in ranked[:remainder]:
            normalized[size] += 1
    elif remainder < 0:
        ranked = sorted(
            distribution,
            key=lambda size: (normalized[size] - raw_scaled[size], size),
            reverse=True,
        )
        for size in ranked[:abs(remainder)]:
            if normalized[size] > 0:
                normalized[size] -= 1

    return {size: count for size, count in normalized.items() if count > 0}


def _decode_excel_text(value: object | None) -> str:
    """Unwrap Excel's ``=\"...\"`` encoding for values that should remain text."""
    # Normalize spreadsheet-forced text values of the form ="..." back to raw text.
    text = "" if value is None else str(value).strip()
    if len(text) >= 4 and text.startswith('="') and text.endswith('"'):
        inner = text[2:-1]
        return inner.replace('""', '"')
    return text


def _encode_excel_text(value: object | None) -> str:
    """Escape and wrap text so spreadsheet applications preserve it as text."""
    # Force spreadsheet tools (notably Excel) to keep comma-separated values as text.
    text = _decode_excel_text(value)
    if text == "":
        return ""
    escaped = text.replace('"', '""')
    return f'="{escaped}"'


def _allocate_counts_from_percentages(total_count: int, percentages: list[float]) -> list[int]:
    """Convert shares to integer counts with largest-remainder balancing."""
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


def _write_csv_preserve(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    """Write a CSV with the requested field order, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _derived_occupied_location_target() -> int:
    """Derive the shared occupied-location target from Stage 1 status snapshots."""
    path = STAGE1_OUTPUT_DIR / "Current_Layout_Space_Utilization.csv"
    rows = _read_csv(path)
    if not rows or "Occupied_Locations" not in rows[0]:
        raise ValueError(f"{path} must contain an Occupied_Locations column with numeric values")
    values = [
        value
        for row in rows
        if (value := _to_float(row.get("Occupied_Locations"))) is not None
    ]
    if not values:
        raise ValueError(f"{path} contains no numeric Occupied_Locations values")
    return math.ceil(sum(values) / len(values))


def _derived_max_representative_slot_size() -> float:
    """Find the largest representative slot size in the merged Stage 3 summary."""
    path = SLOT_SIZE_ROOT / "Stage3_Slot_Size_Configuration_Summary_All.csv"
    rows = _read_csv(path)
    if not rows or "Representative Slot Size" not in rows[0]:
        raise ValueError(f"{path} must contain a Representative Slot Size column with numeric values")
    values = [
        value
        for row in rows
        if (value := _to_float(row.get("Representative Slot Size"))) is not None
    ]
    if not values:
        raise ValueError(f"{path} contains no numeric Representative Slot Size values")
    return max(values)


def refresh_derived_constants() -> None:
    """Refresh shared targets from the latest Stage 1 and Stage 3 outputs."""
    global BASE_OCCUPIED_LOCATIONS_COUNT, MAX_REPRESENTATIVE_SLOT_SIZE_CM
    occupied_locations_path = STAGE1_OUTPUT_DIR / "Current_Layout_Space_Utilization.csv"
    slot_size_summary_path = SLOT_SIZE_ROOT / "Stage3_Slot_Size_Configuration_Summary_All.csv"
    if occupied_locations_path.exists():
        BASE_OCCUPIED_LOCATIONS_COUNT = _derived_occupied_location_target()
    if slot_size_summary_path.exists():
        MAX_REPRESENTATIVE_SLOT_SIZE_CM = _derived_max_representative_slot_size()


BASE_OCCUPIED_LOCATIONS_COUNT: int | None = None
MAX_REPRESENTATIVE_SLOT_SIZE_CM: float | None = None
refresh_derived_constants()


def _deduplicate_output_columns(
    fieldnames: list[str],
    rows: list[dict[str, str]],
    preserve_fields: set[str] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Drop row-identical columns unless their names are explicitly preserved."""
    # Remove duplicate output columns that carry identical values row-by-row.
    # Some reporting columns are intentionally kept even when they are constant across a shortlist
    # because they are diagnostic KPIs and should remain available in the exported summary.
    if not fieldnames or not rows:
        return fieldnames, rows

    preserve = {str(field) for field in (preserve_fields or set())}
    column_values: dict[str, list[str]] = {
        field: [str(row.get(field, "")) for row in rows]
        for field in fieldnames
    }

    kept_fields: list[str] = []
    seen_signatures: set[tuple[str, ...]] = set()
    for field in fieldnames:
        if field in preserve:
            kept_fields.append(field)
            continue
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


def _write_csv_clean(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
    preserve_fields: set[str] | None = None,
) -> None:
    """Write a CSV after removing redundant columns and creating its parent path."""
    # Standardized CSV writer with parent-folder creation and column cleanup.
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_fields, cleaned_rows = _deduplicate_output_columns(fieldnames, rows, preserve_fields=preserve_fields)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=cleaned_fields)
        writer.writeheader()
        writer.writerows(cleaned_rows)



def _fixed_layout_location_total() -> int:
    """Return the fixed-layout location contribution, currently defined as zero."""
    return 0


def _explicit_occupied_target_total() -> int:
    """Return the nonnegative shared occupied-location demand target."""
    # The 890 target is the working occupied-location demand. Physical fixed
    # layout slots (including the 234 cm slot) remain in the layout but are
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


def _exclude_fixed_layout_slot_counts(counts: dict[int, int]) -> dict[int, int]:
    """Keep positive demand counts while excluding non-demand layout slots."""
    # The active baseline does not carry layout slots in the demand model.
    return {
        size: max(int(count), 0)
        for size, count in sorted(counts.items())
        if max(int(count), 0) > 0
    }


def _is_split_location(location: str) -> bool:
    """Check whether a location code has a split-position suffix."""
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
        if location_type == "layout":
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
    segments: set[tuple[str, int, int]] | None = None,
    layout_thresholds_by_rack: dict[str, tuple[int, float]] | None = None,
) -> list[dict[str, str]]:
    """Expand per-column slot assignments into location rows with beam coordinates."""
    # Expand column-level slot assignments into synthetic location rows.
    location_rows: list[dict[str, str]] = []
    segment_by_column: dict[str, tuple[str, int, int]] = {}
    layout_thresholds_by_rack = layout_thresholds_by_rack or {}

    if segments:
        for rack, c0, c1 in segments:
            for col in range(c0, c1 + 1):
                segment_by_column[f"{rack}{col:02d}"] = (rack, c0, c1)

    for column_key in sorted(column_assignments.keys()):
        rack = column_key[0]
        column = column_key[1:]
        segment = segment_by_column.get(column_key)
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


def _normalize_beam_level(level: str) -> str:
    """Normalize a beam-level token to a comparable two-digit form."""
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
    """Format a single-column or spanning beam as its canonical unit ID."""
    if c0 == c1:
        return f"{rack}{c0:02d}:{level}"
    return f"{rack}[{c0:02d}-{c1:02d}]:{level}"


def _beam_unit_columns(beam_unit: str) -> set[str]:
    """Expand a beam unit into the rack-column keys covered by its span."""
    parsed = _parse_beam_coordinate_parts(beam_unit)
    if parsed is None:
        return set()
    rack, c0, c1, _level = parsed
    return {f"{rack}{col:02d}" for col in range(c0, c1 + 1)}


def _beam_units_by_column(rows: list[dict[str, str]]) -> dict[str, set[str]]:
    """Map each layout column to the canonical beam units crossing that column."""
    # Build explicit beam-unit membership per column from row-level coordinates.
    # This preserves partial spans (for example around layout) for per-column
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
    """Return the numeric part of a normalized beam-level token."""
    normalized = _normalize_beam_level(level)
    match = re.match(r"^(\d{2})", normalized)
    if not match:
        return None
    return int(match.group(1))


def _column_slot_sizes_from_rows(
    rows: list[dict[str, str]],
    slot_size_field: str,
) -> dict[str, dict[int, float]]:
    """Index slot heights by rack-column key and row number."""
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
    """Estimate beam elevations by summing the slots below each beam level."""
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
    """Build current beam IDs, structural spans, and elevations from Stage 1 data."""
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
    # (for example near layout) do not overwrite the rack's physical segmentation.
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
    """Extract proposed beam IDs and elevations from generated location rows."""
    # Infer proposed beam units directly from explicit generated beam coordinates.
    # This preserves partial spans around layout/split areas instead of collapsing
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


def _beam_relocations(
    current_units: set[str],
    proposed_units: set[str],
    current_unit_heights: dict[str, float],
    proposed_unit_heights: dict[str, float],
    current_units_by_column: dict[str, set[str]] | None = None,
    proposed_units_by_column: dict[str, set[str]] | None = None,
) -> tuple[int, dict[str, int], dict[str, int], dict[str, int]]:
    """Pair beam units by segment and report relocations, removals, and additions."""
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
    """Count existing beams and supported storage grids from prepared warehouse rows."""
    # Derive baseline counts directly from Stage 1 prepared/beam data. Explicit
    # beam support includes supported 1b/1c and split locations; floor-level
    # 01/1a positions remain excluded.
    baseline_beams = len(current_units)
    baseline_grids = 0
    for row in prepared_rows or []:
        location_type = str(row.get("Location Type", "")).strip().lower()
        if location_type == "layout":
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
    """Return required and additional beam/grid counts for a proposed layout."""
    # Required materials are based on total required counts in the proposed layout.
    required_beams = len(proposed_units)
    required_grids = sum(1 for row in proposed_layout_rows if _to_int_default(row.get("Row"), 0) > 1)

    additional_beams = max(required_beams - initial_beam_count, 0)
    additional_grids = max(required_grids - initial_grid_count, 0)

    return required_beams, required_grids, additional_beams, additional_grids


if getattr(sys, "frozen", False):
    # PyInstaller can place the generated app at either the bundle root or the
    # sibling _internal folder, depending on the build layout and the copied
    # project files. Probe the actual runtime locations so we resolve the right
    # stage-script directory instead of assuming a single fixed tree.
    bundle_root = Path(sys.executable).resolve().parent
    script_candidates = [
        bundle_root / "_internal" / "Scripts" / "Pipeline",
        bundle_root / "_internal" / "Pipeline",
        bundle_root / "Scripts" / "Pipeline",
        bundle_root / "Pipeline",
        Path(__file__).resolve().parent,
    ]
    SCRIPT_DIR = next((candidate for candidate in script_candidates if candidate.exists()), script_candidates[0])
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

ORDERED_SCRIPTS = [
    "01_data_preparation.py",
    "02_scenario_generation_weighted_delta.py",
    "03_slot_size_generation_main.py",
    "04_candidate_configuration.py",
    "05_capacity_determination.py",
    "06_layout_generation.py",
    "07_robustness_evaluation.py",
    "08_final_selection.py",
]
ACTIVE_METHOD_ENV = "PIPELINE_ACTIVE_METHOD"


def ordered_scripts() -> list[str]:
    """Return the configured stage entrypoints in execution order."""
    return list(ORDERED_SCRIPTS)


def _script_path(script_name: str) -> Path:
    """Resolve a pipeline entrypoint relative to the active script directory."""
    # Resolve stage script path relative to pipeline folder.
    return SCRIPT_DIR / script_name


def _run_script(script_name: str) -> None:
    """Execute one stage script as a __main__ module."""
    script_path = _script_path(script_name)
    if not script_path.exists():
        raise FileNotFoundError(f"Pipeline stage script not found: {script_path}")
    print(f"Running: {script_name}")
    runpy.run_path(str(script_path), run_name="__main__")


def _run_method_script(script_name: str, method: str) -> None:
    """Run a shared Stage 6-8 entry point for one layout method."""
    previous = os.environ.get(ACTIVE_METHOD_ENV)
    os.environ[ACTIVE_METHOD_ENV] = method
    try:
        _run_script(script_name)
    finally:
        if previous is None:
            os.environ.pop(ACTIVE_METHOD_ENV, None)
        else:
            os.environ[ACTIVE_METHOD_ENV] = previous


def _run_space_utilization_variant() -> bool:
    """Check whether the SU variant is enabled by environment or command line."""
    requested = os.environ.get("PIPELINE_RUN_SPACE_UTILIZATION", "").strip().upper()
    return requested in {"1", "TRUE", "YES", "ON"} or "--include-space-utilization" in sys.argv[1:]


def _run_picking_efficiency_variant() -> bool:
    """Check whether the PE variant is enabled by environment or command line."""
    requested = os.environ.get("PIPELINE_RUN_PICKING_EFFICIENCY", "").strip().upper()
    return requested in {"1", "TRUE", "YES", "ON"} or "--include-picking-efficiency" in sys.argv[1:]


def _run_beam_preservation_variant() -> bool:
    """Check whether the BP variant is enabled by environment or command line."""
    requested = os.environ.get("PIPELINE_RUN_BEAM_PRESERVATION", "").strip().upper()
    return requested in {"1", "TRUE", "YES", "ON"} or "--include-beam-preservation" in sys.argv[1:]


def run_pipeline() -> None:
    """Run all pipeline stages in deterministic order."""
    for script_name in ordered_scripts():
        if script_name in {
            "06_layout_generation.py",
            "07_robustness_evaluation.py",
            "08_final_selection.py",
        }:
            _run_method_script(script_name, "Baseline")
        else:
            _run_script(script_name)
        if script_name in {
            "01_data_preparation.py",
            "03_slot_size_generation_main.py",
        }:
            refresh_derived_constants()
        if script_name == "06_layout_generation.py":
            for method, enabled in (
                ("SU", _run_space_utilization_variant()),
                ("PE", _run_picking_efficiency_variant()),
                ("BP", _run_beam_preservation_variant()),
            ):
                if enabled:
                    for method_stage in (
                        "06_layout_generation.py",
                        "07_robustness_evaluation.py",
                        "08_final_selection.py",
                    ):
                        _run_method_script(method_stage, method)

    if _run_space_utilization_variant() or _run_picking_efficiency_variant() or _run_beam_preservation_variant():
        _run_method_script("06_layout_generation.py", "PICKING_SCORE_COMPARISON")

    from combine_heuristic_outputs import combine_stage

    # Refresh combined outputs after all requested methods are ready.
    for stage in (6, 7, 8):
        combine_stage(stage)


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
