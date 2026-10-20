import csv
import os
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common
from heuristic_output_utils import suffix_output_tree


# Stage 8 input/output paths and legacy artifacts to clean up.
ROBUSTNESS_DETAILS_FILE = common.STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Details.csv"
LAYOUT_SUMMARY_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_Summary.csv"
LAYOUT_BY_COLUMN_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Rack_Column.csv"
LAYOUT_BY_LOCATION_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Location.csv"
LAYOUT_BY_RACK_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Rack.csv"
OUTPUT_FILE = common.STAGE8_OUTPUT_DIR / "Candidate_Layout_Metric_Ranking.csv"
WSM_WEIGHTS_FILE = common.ROOT / "Input files" / "WSM_Weights.csv"
WSM_OUTPUT_FILE = common.STAGE8_OUTPUT_DIR / "Weighted_Sum_Method_Ranking.csv"
FINAL_LAYOUT_BY_COLUMN_FILE = common.STAGE8_OUTPUT_DIR / "Final_Layout_By_Rack_Column.csv"
FINAL_LAYOUT_BY_LOCATION_FILE = common.STAGE8_OUTPUT_DIR / "Final_Layout_By_Location.csv"
FINAL_LAYOUT_BY_RACK_FILE = common.STAGE8_OUTPUT_DIR / "Final_Layout_By_Rack.csv"
LEGACY_OUTPUT_FILES = [
    common.STAGE8_OUTPUT_DIR / "Objective_Layout_Recommendations.csv",
    common.STAGE8_OUTPUT_DIR / "Management_Decision_Table.csv",
]


_read_csv = common._read_csv


_write_csv_preserve = common._write_csv_preserve


def _layout_map() -> dict[str, dict[str, str]]:
    """Load Stage 6 layout summary rows keyed by config ID."""
    layouts: dict[str, dict[str, str]] = {}
    if LAYOUT_SUMMARY_FILE.exists():
        for row in _read_csv(LAYOUT_SUMMARY_FILE):
            config_id = str(row.get("Config_ID", "")).strip()
            if config_id:
                layouts[config_id] = row
    return layouts


def _robustness_rows() -> list[dict[str, str]]:
    """Load the retained Stage 7 robustness details, which include the summary KPIs."""
    if not ROBUSTNESS_DETAILS_FILE.exists():
        raise FileNotFoundError(f"Missing robustness details file: {ROBUSTNESS_DETAILS_FILE}")
    return _read_csv(ROBUSTNESS_DETAILS_FILE)


def _feasible_stage6_layouts_for_stage8(layout_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep all feasible Stage 6 layouts eligible for Stage 8 ranking even if Stage 7 robustness is weak."""
    return [
        row for row in layout_rows
        if str(row.get("Layout_Feasible", "")).strip().upper() == "YES"
    ]


def _final_column_fieldnames(rows: list[dict[str, str]]) -> list[str]:
    """Return the stable column schema used by final-layout exports."""
    base = [
        "Config_ID",
        "Rack_Column",
        "Beam_Count_Used",
        "Assigned_Used_Height_cm",
        "Beam_Relocations_In_Column",
        "Removed_Beams_In_Column",
        "Added_Beams_In_Column",
        "Slot_Size_Distribution",
    ]
    return base


def _count_unique_slot_sizes(layout_row: dict[str, str]) -> int:
    """Count distinct configured slot sizes for the standardization metric."""
    # Standardization proxy: fewer unique slot sizes means higher standardization.
    source = common._decode_excel_text(layout_row.get("Source_Slot_Sizes", ""))
    if source:
        values = {token.strip() for token in source.split(",") if token.strip()}
        return len(values)

    signature = str(layout_row.get("Slot_Composition_Signature", "")).strip()
    if not signature:
        return 0
    values = {
        token.split(":", 1)[0].strip()
        for token in signature.split("|")
        if ":" in token and token.split(":", 1)[0].strip()
    }
    return len(values)


# Weighted-sum criteria, normalization directions, and optional PE metric columns.
WSM_METRIC_SPECS = [
    ("Assigned_Locations_Total", "max"),
    ("Occupancy_Rate", "min"),
    ("Total_Slot_Space_m3", "max"),
    ("Empty_Slot_Space_m3", "max"),
    ("Space_Utilization_Pct", "min"),
    ("Beam_Relocations_Total", "min"),
    ("Additional_Beams_Required", "min"),
    ("Additional_Grids_Required", "min"),
    ("Standardization_Unique_Slot_Sizes", "min"),
    ("Shared_Picking_Score", "max"),
]
PE_PICKING_METRIC_FIELDS = [
    "Picking_Covered_Item_Count",
    "Picking_Covered_Pick_Count",
    "Picking_Uncovered_Item_Count",
    "Picking_Uncovered_Pick_Count",
    "Picking_Score",
]


def _read_wsm_weights() -> dict[str, float]:
    """Load and validate complete WSM weights against the configured metric set."""
    if not WSM_WEIGHTS_FILE.exists():
        raise FileNotFoundError(f"Missing WSM weights file: {WSM_WEIGHTS_FILE}")

    rows = _read_csv(WSM_WEIGHTS_FILE)
    weights: dict[str, float] = {}
    for row in rows:
        metric = str(row.get("Metric", "")).strip()
        if not metric:
            continue
        if metric in weights:
            raise ValueError(f"Duplicate WSM metric in {WSM_WEIGHTS_FILE}: {metric}")
        if metric not in {name for name, _direction in WSM_METRIC_SPECS}:
            continue
        weight = common._to_float(row.get("Weight"))
        if weight is None or not 0.0 <= weight <= 1.0:
            raise ValueError(f"WSM weight for {metric} must be between 0 and 1")
        weights[metric] = weight

    expected_metrics = {metric for metric, _direction in WSM_METRIC_SPECS}
    missing = sorted(expected_metrics - set(weights))
    if missing:
        raise ValueError(f"WSM weights file is missing required metrics: {missing}")
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"WSM weights must sum to 1.0; got {total:.12f}")
    return weights


def _normalise_wsm_value(value: float, values: list[float], direction: str) -> float:
    """Normalize a metric against its observed extreme in the preferred direction."""
    reference = max(values) if direction == "max" else min(values)
    if abs(reference) <= 1e-12:
        return 1.0 if abs(value) <= 1e-12 else 0.0
    return value / reference if direction == "max" else reference / value


def _write_weighted_sum_ranking(candidate_rows: list[dict[str, str]]) -> None:
    """Write normalized metric contributions and weighted-sum ranks for each candidate."""
    weights = _read_wsm_weights()
    metric_values = {
        metric: [common._to_float(row.get(metric)) or 0.0 for row in candidate_rows]
        for metric, _direction in WSM_METRIC_SPECS
    }
    scored_rows: list[dict[str, str]] = []
    for row in candidate_rows:
        scored = {"Config_ID": str(row.get("Config_ID", "")), "Robustness": str(row.get("Robustness", ""))}
        score = 0.0
        for metric, direction in WSM_METRIC_SPECS:
            value = common._to_float(row.get(metric)) or 0.0
            normalized = _normalise_wsm_value(value, metric_values[metric], direction)
            contribution = normalized * weights[metric]
            scored[f"{metric}_Raw"] = f"{value:.6f}"
            scored[f"{metric}_Normalized"] = f"{normalized:.6f}"
            scored[f"{metric}_Weighted"] = f"{contribution:.6f}"
            score += contribution
        scored["Weighted_Sum_Score"] = f"{score:.6f}"
        scored_rows.append(scored)

    scored_rows.sort(key=lambda item: (-float(item["Weighted_Sum_Score"]), item["Config_ID"]))
    for rank, row in enumerate(scored_rows, start=1):
        row["Weighted_Sum_Rank"] = str(rank)

    fields = ["Weighted_Sum_Rank", "Config_ID", "Robustness"]
    for metric, _direction in WSM_METRIC_SPECS:
        fields.extend([f"{metric}_Raw", f"{metric}_Normalized", f"{metric}_Weighted"])
    fields.append("Weighted_Sum_Score")
    _write_csv_preserve(WSM_OUTPUT_FILE, fields, scored_rows)


def _add_picking_metrics_to_output(path: Path, picking_by_config: dict[str, dict[str, str]]) -> None:
    """Append PE coverage and score fields to a ranking file by configuration ID."""
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
        fields = list(rows[0].keys()) if rows else []
    for field in PE_PICKING_METRIC_FIELDS:
        if field not in fields:
            fields.append(field)
    for row in rows:
        metrics = picking_by_config.get(str(row.get("Config_ID", "")).strip(), {})
        for field in PE_PICKING_METRIC_FIELDS:
            row[field] = metrics.get(field, "")
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _parse_size_count_signature(value: str) -> dict[int, int]:
    """Parse a pipe-delimited ``size:count`` summary into nonnegative integer counts."""
    counts: dict[int, int] = {}
    text = str(value).strip()
    if not text:
        return counts

    for token in text.split("|"):
        token = token.strip()
        if not token or ":" not in token:
            continue
        size_text, count_text = token.split(":", 1)
        size = common._to_int_default(size_text, -1)
        count = common._to_int_default(count_text, 0)
        if size < 0:
            continue
        counts[size] = max(count, 0)

    return counts


def _allocate_occupied_by_size(minimum_by_size: dict[int, int], total_by_size: dict[int, int]) -> dict[int, int]:
    """Assign demand to the smallest available slot sizes that can accommodate it."""
    remaining_capacity = {size: max(int(count), 0) for size, count in total_by_size.items()}
    occupied_by_size: dict[int, int] = {}
    capacity_sizes = sorted(remaining_capacity.keys())

    for demand_size in sorted(minimum_by_size.keys(), reverse=True):
        remaining_demand = max(int(minimum_by_size.get(demand_size, 0)), 0)
        if remaining_demand <= 0:
            continue

        for capacity_size in capacity_sizes:
            if capacity_size < demand_size:
                continue
            available = remaining_capacity.get(capacity_size, 0)
            if available <= 0:
                continue

            used = min(available, remaining_demand)
            occupied_by_size[capacity_size] = occupied_by_size.get(capacity_size, 0) + used
            remaining_capacity[capacity_size] = available - used
            remaining_demand -= used

            if remaining_demand <= 0:
                break

    return occupied_by_size


def _actual_occupied_by_size(layout_row: dict[str, str]) -> dict[int, int]:
    """Allocate base-case demand into the smallest fitting realized slot families."""
    minimum_by_size = _parse_size_count_signature(str(layout_row.get("Minimum_Required_Counts", "")))
    total_by_size = _parse_size_count_signature(str(layout_row.get("Layout_Slot_Size_Distribution", "")))
    return _allocate_occupied_by_size(minimum_by_size, total_by_size)


def _space_utilization_metrics(
    layout_row: dict[str, str],
    demand_by_size: dict[int, int] | None = None,
) -> tuple[int, int, float, float]:
    """Measure occupied and total slot space, utilization, and occupancy for a layout."""
    # Compare actual occupied layout space against the realized layout, including any
    # extra slot sizes that appear in the final layout.
    total_by_size = _parse_size_count_signature(str(layout_row.get("Layout_Slot_Size_Distribution", "")))
    occupied_by_size = (
        _allocate_occupied_by_size(demand_by_size, total_by_size)
        if demand_by_size is not None
        else _actual_occupied_by_size(layout_row)
    )

    all_sizes = sorted(set(occupied_by_size.keys()) | set(total_by_size.keys()))
    occupied_locations = 0
    occupied_space = 0
    total_locations = 0
    total_space = 0

    for size in all_sizes:
        occupied_count = max(occupied_by_size.get(size, 0), 0)
        total_count = max(total_by_size.get(size, 0), 0)

        occupied_locations += occupied_count
        total_locations += total_count
        occupied_space += size * occupied_count
        total_space += size * total_count

    empty_space = max(total_space - occupied_space, 0)
    utilization_pct = (occupied_space / total_space * 100.0) if total_space > 0 else 0.0
    occupancy_rate = occupied_locations / total_locations if total_locations > 0 else 0.0

    return empty_space, total_space, utilization_pct, occupancy_rate


def build_final_selection() -> list[dict[str, str]]:
    """Build a full all-candidate metric table with per-metric ranks for weighted-sum analysis."""
    layouts = _layout_map()
    stage6_feasible_rows = _feasible_stage6_layouts_for_stage8(list(layouts.values()))
    robustness_by_config = {
        str(row.get("Config_ID", "")).strip(): row
        for row in _robustness_rows()
        if str(row.get("Config_ID", "")).strip()
    }

    joined_rows: list[dict[str, str]] = []
    # Join Stage 6 layout metadata with Stage 7 robustness metrics when available,
    # but do not reject feasible layouts just because Stage 7 robustness is weak.
    for row in stage6_feasible_rows:
        config_id = str(row.get("Config_ID", "")).strip()
        if not config_id:
            continue
        layout = layouts.get(config_id, {})
        merged = dict(layout)
        merged.update(robustness_by_config.get(config_id, {}))
        merged["Unique_Slot_Sizes_Count"] = str(_count_unique_slot_sizes(layout))
        joined_rows.append(merged)

    candidate_rows: list[dict[str, str]] = []
    for row in joined_rows:
        empty_space, total_space, utilization_pct, occupancy_rate = _space_utilization_metrics(row)

        assigned_locations_total = common._to_int_default(
            row.get("Assigned_Locations_Total") or row.get("Total_Locations"),
            0,
        )
        capacity_margin = common._to_int_default(
            row.get("Capacity_Margin"),
            assigned_locations_total - int(common.BASE_OCCUPIED_LOCATIONS_COUNT),
        )

        empty_space_m3 = empty_space / 100.0
        total_space_m3 = total_space / 100.0

        candidate_rows.append(
            {
                "Config_ID": str(row.get("Config_ID", "")),
                "Source_Slot_Sizes": common._encode_excel_text(
                    common._decode_excel_text(row.get("Source_Slot_Sizes", ""))
                ),
                "Assigned_Locations_Total": str(assigned_locations_total),
                "Capacity_Margin": str(capacity_margin),
                "Occupancy_Rate": f"{occupancy_rate:.6f}",
                "Empty_Slot_Space_m3": f"{empty_space_m3:.6f}",
                "Total_Slot_Space_m3": f"{total_space_m3:.6f}",
                "Space_Utilization_Pct": f"{utilization_pct:.4f}",
                "Beam_Relocations_Total": str(common._to_int_default(row.get("Beam_Relocations_Total"), 0)),
                "Required_Beams_Total": str(common._to_int_default(row.get("Required_Beams_Total"), 0)),
                "Additional_Beams_Required": str(common._to_int_default(row.get("Additional_Beams_Required"), 0)),
                "Required_Grids_Total": str(common._to_int_default(row.get("Required_Grids_Total"), 0)),
                "Additional_Grids_Required": str(common._to_int_default(row.get("Additional_Grids_Required"), 0)),
                "Standardization_Unique_Slot_Sizes": str(common._to_int_default(row.get("Unique_Slot_Sizes_Count"), 0)),
                "Robustness": f"{common._to_float(row.get('Robustness')) or 0.0:.6f}",
            }
        )

    _write_csv_preserve(
        OUTPUT_FILE,
        [
            "Config_ID",
            "Source_Slot_Sizes",
            "Assigned_Locations_Total",
            "Capacity_Margin",
            "Occupancy_Rate",
            "Empty_Slot_Space_m3",
            "Total_Slot_Space_m3",
            "Space_Utilization_Pct",
            "Beam_Relocations_Total",
            "Required_Beams_Total",
            "Additional_Beams_Required",
            "Required_Grids_Total",
            "Additional_Grids_Required",
            "Standardization_Unique_Slot_Sizes",
            "Robustness",
        ],
        candidate_rows,
    )
    _write_weighted_sum_ranking(candidate_rows)

    candidate_ids = {
        str(row.get("Config_ID", "")).strip()
        for row in candidate_rows
        if str(row.get("Config_ID", "")).strip()
    }

    finalist_column_rows: list[dict[str, str]] = []
    if LAYOUT_BY_COLUMN_FILE.exists():
        for row in _read_csv(LAYOUT_BY_COLUMN_FILE):
            if str(row.get("Config_ID", "")).strip() in candidate_ids:
                finalist_column_rows.append(row)

    finalist_location_rows: list[dict[str, str]] = []
    if LAYOUT_BY_LOCATION_FILE.exists():
        for row in _read_csv(LAYOUT_BY_LOCATION_FILE):
            if str(row.get("Config_ID", "")).strip() in candidate_ids:
                finalist_location_rows.append(row)

    finalist_rack_rows: list[dict[str, str]] = []
    if LAYOUT_BY_RACK_FILE.exists():
        for row in _read_csv(LAYOUT_BY_RACK_FILE):
            if str(row.get("Config_ID", "")).strip() in candidate_ids:
                finalist_rack_rows.append(row)

    slot_height_by_column: dict[tuple[str, str], float] = {}
    slot_count_by_column: dict[tuple[str, str], int] = {}
    for row in finalist_location_rows:
        config_id = str(row.get("Config_ID", "")).strip()
        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        slot_size = common._to_float(row.get("Assigned_Slot_Size_cm"))
        if not config_id or not rack or not column or slot_size is None:
            continue
        key = (config_id, f"{rack}{int(column):02d}")
        slot_height_by_column[key] = slot_height_by_column.get(key, 0.0) + slot_size
        slot_count_by_column[key] = slot_count_by_column.get(key, 0) + 1

    for row in finalist_column_rows:
        config_id = str(row.get("Config_ID", "")).strip()
        column = str(row.get("Rack_Column", "")).strip()
        key = (config_id, column)
        slot_count = slot_count_by_column.get(key, 0)
        used_height = slot_height_by_column.get(key, 0.0) + max(slot_count - 1, 0) * common.BEAM_HEIGHT
        row["Assigned_Used_Height_cm"] = f"{used_height:.3f}"

    final_column_fields = _final_column_fieldnames(finalist_column_rows)
    common._write_csv_clean(
        FINAL_LAYOUT_BY_COLUMN_FILE,
        final_column_fields,
        [
            {field: str(row.get(field, "")) for field in final_column_fields}
            for row in finalist_column_rows
        ],
    )

    common._write_csv_clean(
        FINAL_LAYOUT_BY_LOCATION_FILE,
        [
            "Config_ID",
            "Location",
            "Rack",
            "Column",
            "Row",
            "Beam_Coordinate",
            "Beam_Height_Range_cm",
            "Assigned_Slot_Size_cm",
        ],
        [
            {
                field: str(row.get(field, ""))
                for field in [
                    "Config_ID",
                    "Location",
                    "Rack",
                    "Column",
                    "Row",
                    "Beam_Coordinate",
                    "Beam_Height_Range_cm",
                    "Assigned_Slot_Size_cm",
                ]
            }
            for row in finalist_location_rows
        ],
    )

    rack_fields = [
        "Config_ID",
        "Rack",
        "Rack_Column_Count",
        "Assigned_Locations_Total",
        "Slot_Size_Distribution",
        "Rack_Profile_Order",
    ]
    common._write_csv_clean(
        FINAL_LAYOUT_BY_RACK_FILE,
        rack_fields,
        [{field: str(row.get(field, "")) for field in rack_fields} for row in finalist_rack_rows],
    )

    for legacy_file in LEGACY_OUTPUT_FILES:
        if legacy_file.exists():
            legacy_file.unlink()

    return candidate_rows


def run_final_selection(method: str = "Baseline") -> list[dict[str, str]]:
    """Rank baseline or method-specific feasible layouts and emit final selections."""
    method = str(method or "Baseline").strip().upper()
    if method in {"BASELINE", "BASE"}:
        return build_final_selection()

    method_directories = {
        "SU": "Space_Utilization",
        "PE": "Picking_Efficiency",
        "BP": "Beam_Preservation",
    }
    if method not in method_directories:
        raise ValueError(f"Unknown Stage 8 method: {method}. Expected Baseline, SU, PE, or BP.")

    suffix = method
    directory = method_directories[method]
    stage6_dir = common.OUTPUT_ROOT / f"06_Layout_Generation_{directory}"
    stage7_dir = common.OUTPUT_ROOT / f"07_Robustness_Evaluation_{directory}"
    stage8_dir = common.OUTPUT_ROOT / f"08_Final_Selection_{directory}"
    path_names = (
        "ROBUSTNESS_DETAILS_FILE",
        "LAYOUT_SUMMARY_FILE",
        "LAYOUT_BY_COLUMN_FILE",
        "LAYOUT_BY_LOCATION_FILE",
        "LAYOUT_BY_RACK_FILE",
        "OUTPUT_FILE",
        "WSM_OUTPUT_FILE",
        "FINAL_LAYOUT_BY_COLUMN_FILE",
        "FINAL_LAYOUT_BY_LOCATION_FILE",
        "FINAL_LAYOUT_BY_RACK_FILE",
        "LEGACY_OUTPUT_FILES",
    )
    original_paths = {name: globals()[name] for name in path_names}
    try:
        globals().update(
            {
                "ROBUSTNESS_DETAILS_FILE": stage7_dir / f"Candidate_Layout_Robustness_Details_{suffix}.csv",
                "LAYOUT_SUMMARY_FILE": stage6_dir / f"Candidate_Layout_Summary_{suffix}.csv",
                "LAYOUT_BY_COLUMN_FILE": stage6_dir / f"Candidate_Layout_By_Rack_Column_{suffix}.csv",
                "LAYOUT_BY_LOCATION_FILE": stage6_dir / f"Candidate_Layout_By_Location_{suffix}.csv",
                "LAYOUT_BY_RACK_FILE": stage6_dir / f"Candidate_Layout_By_Rack_{suffix}.csv",
                "OUTPUT_FILE": stage8_dir / "Candidate_Layout_Metric_Ranking.csv",
                "WSM_OUTPUT_FILE": stage8_dir / "Weighted_Sum_Method_Ranking.csv",
                "FINAL_LAYOUT_BY_COLUMN_FILE": stage8_dir / "Final_Layout_By_Rack_Column.csv",
                "FINAL_LAYOUT_BY_LOCATION_FILE": stage8_dir / "Final_Layout_By_Location.csv",
                "FINAL_LAYOUT_BY_RACK_FILE": stage8_dir / "Final_Layout_By_Rack.csv",
                "LEGACY_OUTPUT_FILES": [
                    stage8_dir / "Objective_Layout_Recommendations.csv",
                    stage8_dir / "Management_Decision_Table.csv",
                ],
            }
        )
        stage8_dir.mkdir(parents=True, exist_ok=True)
        rows = build_final_selection()
        if method == "PE":
            picking_summary = common._read_csv(stage6_dir / f"Candidate_Layout_Summary_{suffix}.csv")
            picking_by_config = {str(row.get("Config_ID", "")).strip(): row for row in picking_summary}
            _add_picking_metrics_to_output(OUTPUT_FILE, picking_by_config)
            _add_picking_metrics_to_output(WSM_OUTPUT_FILE, picking_by_config)
        suffix_output_tree(stage8_dir, suffix)
        return rows
    finally:
        globals().update(original_paths)


if __name__ == "__main__":
    # Stage 8 entrypoint: emit full candidate scorecard for weighted-sum analysis.
    method = os.environ.get("PIPELINE_ACTIVE_METHOD", "Baseline")
    candidate_rows = run_final_selection(method)
    print(
        f"{method} decision-support candidate ranking complete. "
        f"Candidate rows: {len(candidate_rows)}."
    )
