import csv
import math
import re
import runpy
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = ROOT / "Output"
STAGE1_OUTPUT_DIR = OUTPUT_ROOT / "01_Data_Preparation"
STAGE2_OUTPUT_DIR = OUTPUT_ROOT / "02_Scenario_Generation"
STAGE3_OUTPUT_DIR = OUTPUT_ROOT / "03_Slot_Size_Generation"
STAGE4_OUTPUT_DIR = OUTPUT_ROOT / "04_Candidate_Configuration_Filtering"
STAGE5_OUTPUT_DIR = OUTPUT_ROOT / "05_Capacity_Determination"
STAGE6_OUTPUT_DIR = OUTPUT_ROOT / "06_Layout_Generation"
STAGE7_OUTPUT_DIR = OUTPUT_ROOT / "07_Robustness_Evaluation"
STAGE8_OUTPUT_DIR = OUTPUT_ROOT / "08_Final_Selection"

SLOT_SIZE_ROOT = STAGE3_OUTPUT_DIR

METHODS = ("quantile_binning", "hierarchical_clustering", "kmeans_clustering")
BASE_SKU_COUNT = 843
SKU_SCENARIO_FACTORS = {
    "Low_Count": 0.9,
    "Base_Count": 1.0,
    "High_Count": 1.1,
}
CANDIDATE_LAYOUT_STYLES = ("utilization", "relocation", "balanced", "future_flexibility")

COLUMN_MAX_HEIGHT = 770.0
TOP_BEAM_HEIGHT = 16.0
MAX_USED_HEIGHT_BASE = COLUMN_MAX_HEIGHT - TOP_BEAM_HEIGHT
MIN_BEAMS_PER_COLUMN = 3
BEAM_HEIGHT = 16.0

LOCATION_CODE_PATTERN = re.compile(r"^([A-Z])(\d{2})(\d{2})([A-Za-z]+)?$")
BEAM_SPAN_PATTERN = re.compile(r"^([A-Z])\[(\d{2})-(\d{2})\]:([0-9]{1,2}[A-Za-z]?)$")
BEAM_SINGLE_PATTERN = re.compile(r"^([A-Z])(\d{2}):([0-9]{1,2}[A-Za-z]?)$")


def _to_float(value: object | None) -> float | None:
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


def _to_int_default(value: object | None, default: int = 0) -> int:
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


def _allocate_counts_from_percentages(total_count: int, percentages: list[float]) -> list[int]:
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


def _deduplicate_output_columns(
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> tuple[list[str], list[dict[str, str]]]:
    # Remove duplicate output columns that carry identical values row-by-row.
    if not fieldnames or not rows:
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
    # Standardized CSV writer with parent-folder creation and column cleanup.
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_fields, cleaned_rows = _deduplicate_output_columns(fieldnames, rows)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=cleaned_fields)
        writer.writeheader()
        writer.writerows(cleaned_rows)


def _slot_size_variable_name(slot_size: float) -> str:
    # Canonical variable label used in capacity constraints.
    return f"x_{int(round(slot_size))}"


def _is_split_location(location: str) -> bool:
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
    # Expand column-level slot assignments into synthetic location rows.
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
    style_context: dict[str, object] | None = None,
) -> tuple[bool, dict[float, int], dict[str, float], dict[str, list[float]], str, dict[str, float]]:
    """Allocate exact slot-size demand using style-specific candidate filtering rules."""
    assigned_exact_counts: dict[float, int] = {slot_size: 0 for slot_size in target_exact_counts}
    used_height_by_column: dict[str, float] = defaultdict(float)
    counts_by_column: dict[str, int] = defaultdict(int)
    assignments_by_column: dict[str, list[float]] = defaultdict(list)
    beam_preference = style_context.get("beam_preference", {}) if isinstance(style_context, dict) else {}

    decisions_total = 0
    feasible_total = 0
    feasible_min: int | None = None
    feasible_max = 0
    style_rejected_total = 0
    style_influenced_total = 0
    forced_total = 0

    def _rack_loads() -> dict[str, int]:
        loads: dict[str, int] = defaultdict(int)
        for column_key, count in counts_by_column.items():
            if count > 0:
                loads[column_key[0]] += count
        return dict(loads)

    def _candidate_score(candidate: dict[str, float | int | str], selected_style: str) -> tuple[float, float, float, str]:
        projected_fill = float(candidate["projected_fill"])
        remaining_after = float(candidate["remaining_after"])
        current_count = float(candidate["current_count"])
        relocation_proxy = float(candidate["relocation_proxy"])
        imbalance_after = float(candidate["imbalance_after"])
        future_adaptability = float(candidate["future_adaptability"])
        column_key = str(candidate["column_key"])

        if selected_style == "utilization":
            return (-projected_fill, -current_count, remaining_after, column_key)
        if selected_style == "relocation":
            return (relocation_proxy, current_count * -1.0, projected_fill, column_key)
        if selected_style == "balanced":
            return (imbalance_after, projected_fill, remaining_after, column_key)
        if selected_style == "future_flexibility":
            return (-future_adaptability, projected_fill, current_count, column_key)
        return (remaining_after, current_count, projected_fill, column_key)

    def _style_filter(candidates: list[dict[str, float | int | str]], selected_style: str) -> list[dict[str, float | int | str]]:
        if not candidates:
            return []

        if selected_style == "utilization":
            ranked = sorted(candidates, key=lambda c: (-float(c["projected_fill"]), str(c["column_key"])))
            keep = max(1, len(ranked) // 4)
            return ranked[:keep]

        if selected_style == "relocation":
            ranked = sorted(candidates, key=lambda c: (float(c["relocation_proxy"]), str(c["column_key"])))
            keep = max(1, len(ranked) // 5)
            return ranked[:keep]

        if selected_style == "balanced":
            ranked = sorted(candidates, key=lambda c: (float(c["imbalance_after"]), str(c["column_key"])))
            keep = max(1, len(ranked) // 4)
            return ranked[:keep]

        if selected_style == "future_flexibility":
            flexibility_ready = [
                candidate
                for candidate in candidates
                if float(candidate["projected_fill"]) <= 0.80
            ]
            ranked = sorted(
                flexibility_ready if flexibility_ready else candidates,
                key=lambda c: (-float(c["future_adaptability"]), str(c["column_key"])),
            )
            keep = max(1, len(ranked) // 4)
            return ranked[:keep]

        return list(candidates)

    slot_sizes_desc = sorted(target_exact_counts.keys(), reverse=True)
    for slot_size in slot_sizes_desc:
        needed = int(target_exact_counts.get(slot_size, 0))
        while needed > 0:
            decisions_total += 1
            candidates: list[dict[str, float | int | str]] = []
            rack_loads_before = _rack_loads()
            avg_fill_before = (
                sum(used_height_by_column.get(column, 0.0) / max(MAX_USED_HEIGHT_BASE, 1e-9) for column in column_keys)
                / max(len(column_keys), 1)
            )
            for column_key in column_keys:
                current_count = counts_by_column.get(column_key, 0)
                next_count = current_count + 1
                allowed_after = MAX_USED_HEIGHT_BASE - BEAM_HEIGHT * max(next_count - 1, MIN_BEAMS_PER_COLUMN)
                proposed_used = used_height_by_column.get(column_key, 0.0) + slot_size
                if proposed_used > allowed_after + 1e-9:
                    continue

                remaining_after = allowed_after - proposed_used
                projected_fill = proposed_used / max(allowed_after, 1e-9)
                beam_pref = float(beam_preference.get(column_key, 0)) if isinstance(beam_preference, dict) else 0.0
                relocation_proxy = (0.0 if current_count > 0 else 1.0) + (1.0 / (beam_pref + 1.0))

                rack_key = column_key[0]
                rack_load_after = rack_loads_before.get(rack_key, 0) + 1
                rack_mean_after = (sum(rack_loads_before.values()) + 1) / max(len({key[0] for key in column_keys}), 1)
                imbalance_after = abs(rack_load_after - rack_mean_after) + abs(projected_fill - avg_fill_before)
                future_adaptability = remaining_after / max(allowed_after, 1e-9)

                candidates.append(
                    {
                        "column_key": column_key,
                        "current_count": current_count,
                        "remaining_after": remaining_after,
                        "projected_fill": projected_fill,
                        "relocation_proxy": relocation_proxy,
                        "imbalance_after": imbalance_after,
                        "future_adaptability": future_adaptability,
                    }
                )

            if not candidates:
                diagnostics = {
                    "Allocation_Decisions_Total": float(decisions_total),
                    "Feasible_Columns_Considered_Total": float(feasible_total),
                    "Feasible_Columns_Min": float(feasible_min or 0),
                    "Feasible_Columns_Max": float(feasible_max),
                    "Feasible_Columns_Average": (feasible_total / decisions_total) if decisions_total else 0.0,
                    "Candidates_Rejected_By_Style_Total": float(style_rejected_total),
                    "Decisions_Influenced_By_Style": float(style_influenced_total),
                    "Forced_By_Feasibility_Count": float(forced_total),
                }
                return False, assigned_exact_counts, dict(used_height_by_column), dict(assignments_by_column), f"No allocatable column found for slot size {slot_size:.0f} with remaining demand {needed}.", diagnostics

            feasible_count = len(candidates)
            feasible_total += feasible_count
            feasible_min = feasible_count if feasible_min is None else min(feasible_min, feasible_count)
            feasible_max = max(feasible_max, feasible_count)
            if feasible_count == 1:
                forced_total += 1

            baseline_sorted = sorted(
                candidates,
                key=lambda c: (float(c["remaining_after"]), int(c["current_count"]), str(c["column_key"])),
            )
            baseline_choice = baseline_sorted[0]

            style_candidates = _style_filter(candidates, style)
            style_rejected_total += max(feasible_count - len(style_candidates), 0)
            chosen_pool = style_candidates if style_candidates else candidates

            chosen_sorted = sorted(chosen_pool, key=lambda c: _candidate_score(c, style))
            chosen = chosen_sorted[0]

            if str(chosen["column_key"]) != str(baseline_choice["column_key"]):
                style_influenced_total += 1

            chosen_column = str(chosen["column_key"])
            assignments_by_column[chosen_column].append(slot_size)
            used_height_by_column[chosen_column] += slot_size
            counts_by_column[chosen_column] += 1
            assigned_exact_counts[slot_size] += 1
            needed -= 1

    assignments_by_column = {column_key: slots for column_key, slots in assignments_by_column.items() if slots}
    used_height_by_column = {column_key: used_height_by_column[column_key] for column_key in assignments_by_column}
    diagnostics = {
        "Allocation_Decisions_Total": float(decisions_total),
        "Feasible_Columns_Considered_Total": float(feasible_total),
        "Feasible_Columns_Min": float(feasible_min or 0),
        "Feasible_Columns_Max": float(feasible_max),
        "Feasible_Columns_Average": (feasible_total / decisions_total) if decisions_total else 0.0,
        "Candidates_Rejected_By_Style_Total": float(style_rejected_total),
        "Decisions_Influenced_By_Style": float(style_influenced_total),
        "Forced_By_Feasibility_Count": float(forced_total),
    }
    return True, assigned_exact_counts, dict(used_height_by_column), assignments_by_column, "Style-specific constrained allocation succeeded.", diagnostics


def _normalize_beam_level(level: str) -> str:
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
    # Build baseline beam units and horizontal segment definitions from Stage 1 map.
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
    # Infer proposed beam units by common row depth across each structural segment.
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
    # Relocations are counted as beams present now but absent in proposal.
    removed_units = current_units - proposed_units

    relocation_total = len(removed_units)

    per_column: dict[str, int] = defaultdict(int)
    for beam_unit in removed_units:
        for column_key in _beam_unit_columns(beam_unit):
            per_column[column_key] += 1

    return relocation_total, dict(per_column)


def _material_requirements(
    current_units: set[str],
    proposed_units: set[str],
) -> tuple[int, int, int]:
    # Material deltas between current and proposed beam sets.
    removed_units = len(current_units - proposed_units)
    added_units = len(proposed_units - current_units)

    additional_beams = added_units
    removed_beams = removed_units
    additional_grids = additional_beams
    return additional_beams, additional_grids, removed_beams


def _build_sku_count_scenarios(_scenario_rows: list[dict[str, str]]) -> dict[str, int]:
    # Shared low/base/high SKU demand assumptions used in downstream stages.
    base_count = BASE_SKU_COUNT
    return {
        name: int(round(base_count * factor))
        for name, factor in SKU_SCENARIO_FACTORS.items()
    }


SCRIPT_DIR = Path(__file__).resolve().parent
ORDERED_SCRIPTS = [
    "01_Data_Preparation/01_data_preparation.py",
    "02_Scenario_Generation/02_scenario_generation_weighted_delta.py",
    "03_Slot_Size_Generation/03_slot_size_generation_main.py",
    "04_Candidate_Configuration_Filtering/04_candidate_configuration_filtering.py",
    "05_Capacity_Determination/05_capacity_determination.py",
    "06_Layout_Generation/06_layout_generation.py",
    "07_Robustness_Evaluation/07_robustness_evaluation.py",
    "08_Final_Selection/08_final_selection.py",
]


def _script_path(script_name: str) -> Path:
    # Resolve stage script path relative to pipeline folder.
    return SCRIPT_DIR / script_name


def _run_script(script_name: str) -> None:
    """Execute one stage script as a __main__ module."""
    script_path = _script_path(script_name)
    if not script_path.exists():
        raise FileNotFoundError(f"Pipeline stage script not found: {script_path}")
    print(f"Running: {script_name}")
    runpy.run_path(str(script_path), run_name="__main__")


def run_pipeline() -> None:
    """Run all pipeline stages in deterministic order."""
    for script_name in ORDERED_SCRIPTS:
        _run_script(script_name)


if __name__ == "__main__":
    # Pipeline entrypoint for full ordered execution.
    run_pipeline()
    print("Ordered pipeline complete.")
