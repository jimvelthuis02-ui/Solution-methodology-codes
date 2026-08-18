import csv
import importlib.util
from pathlib import Path
import shutil
import sys
import time

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))
HEURISTIC_DIR = PIPELINE_ROOT / "Heuristic_Variants"
if str(HEURISTIC_DIR) not in sys.path:
    sys.path.insert(0, str(HEURISTIC_DIR))

import run_ordered_pipeline as common


def _load_variants_common_module():
    module_path = HEURISTIC_DIR / "heuristic_variants.py"
    spec = importlib.util.spec_from_file_location("heuristic_variants_for_stage8", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load shared helper module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


variants_common = _load_variants_common_module()


def _load_stage8_common_module():
    module_path = PIPELINE_ROOT / "08_Final_Selection" / "08_final_selection.py"
    spec = importlib.util.spec_from_file_location("stage8_wsm_common", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load Stage 8 module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage8_common = _load_stage8_common_module()


OUTPUT_DIR = common.OUTPUT_ROOT / "08_Final_Selection_Comparison_AllHeuristics"
VARIANTS_ROOT = OUTPUT_DIR / "Variants"
WSM_OUTPUT_FILE = OUTPUT_DIR / "Weighted_Sum_Method_Ranking.csv"

VARIANTS = variants_common.VARIANTS
HEURISTIC_FIELDS = variants_common.HEURISTIC_FIELDS

OUTPUT_PREFIX_FIELDS = [
    "Heuristic_Label",
    "Construction_Method",
    "Improvement_Method",
]

AVERAGE_KPI_FIELDS = [
    "Assigned_Locations_Total",
    "Occupancy_Rate",
    "Empty_Locations",
    "Occupied_Slot_Space_m3",
    "Empty_Slot_Space_m3",
    "Total_Slot_Space_m3",
    "Space_Utilization_Pct",
    "Beam_Relocations_Total",
    "Additional_Beams_Required",
    "Additional_Grids_Required",
    "Standardization_Unique_Slot_Sizes",
    "Additional_Fill_Height_Total_cm",
    "Additional_Fill_Extra_Slot_Size_Variants",
]

DROP_FIELDS_BY_OUTPUT = {
    "Candidate_Layout_Metric_Ranking.csv": {
        "Beam_Preserving_Optimizer",
        "Local_Search_Optimizer",
        "Capacity_Margin",
        "Occupied_Locations",
        "Occupied_Slot_Space_cm",
        "Empty_Slot_Space_cm",
        "Total_Slot_Space_cm",
        "Implementation_Effort_Total",
    },
    "Final_Layout_By_Rack_Column.csv": {
        "Beam_Preserving_Optimizer",
        "Local_Search_Optimizer",
        "Remaining_Height_cm",
        "Fill_Ratio",
        "TopFill_Adjusted_Row",
    },
    "Final_Layout_By_Location.csv": {
        "Beam_Preserving_Optimizer",
        "Local_Search_Optimizer",
    },
    "Final_Layout_By_Segment.csv": {
        "Beam_Preserving_Optimizer",
        "Local_Search_Optimizer",
    },
}


def _read_csv_with_fieldnames(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing Stage 8 source file: {path}")

    with path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return rows, fieldnames


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _stage8_variant_dir(variant: dict[str, object]) -> Path:
    return variants_common.stage8_dir(VARIANTS_ROOT, variant)


def _generate_stage8_variants() -> list[dict[str, str]]:
    if VARIANTS_ROOT.exists():
        shutil.rmtree(VARIANTS_ROOT)
    VARIANTS_ROOT.mkdir(parents=True, exist_ok=True)

    original_best_slot_order = variants_common.apply_bounded_beam_order()

    stage6_impact_rows: list[dict[str, str]] = []
    runtime_rows: list[dict[str, str]] = []
    try:
        for variant in VARIANTS:
            print(f"Running Stage 8 variant: {variant['label']}", flush=True)
            variant_start = time.perf_counter()
            variants_common.run_stage6_variant(VARIANTS_ROOT, variant, stage6_impact_rows)
            variants_common.run_stage7_variant(VARIANTS_ROOT, variant)
            variants_common.run_stage8_variant(VARIANTS_ROOT, variant)
            elapsed_seconds = time.perf_counter() - variant_start
            hours = int(elapsed_seconds // 3600)
            minutes = int((elapsed_seconds % 3600) // 60)
            seconds = elapsed_seconds % 60
            runtime_rows.append(
                {
                    "Heuristic_Label": str(variant["label"]),
                    "Construction_Method": str(variant["construction_method"]),
                    "Improvement_Method": str(variant["improvement_method"]),
                    "Runtime_Seconds": f"{elapsed_seconds:.3f}",
                    "Runtime_HH_MM_SS": f"{hours:02d}:{minutes:02d}:{seconds:06.3f}",
                }
            )
            print(f"Completed Stage 8 variant: {variant['label']}", flush=True)
    finally:
        variants_common.restore_bounded_beam_order(original_best_slot_order)

    _write_csv(
        OUTPUT_DIR / "Heuristic_Combination_Runtime.csv",
        [
            "Heuristic_Label",
            "Construction_Method",
            "Improvement_Method",
            "Runtime_Seconds",
            "Runtime_HH_MM_SS",
        ],
        runtime_rows,
    )
    return stage6_impact_rows


def _decorate_row(row: dict[str, str], variant: dict[str, object]) -> dict[str, str]:
    merged = {
        "Heuristic_Label": str(variant["label"]),
        "Construction_Method": str(variant["construction_method"]),
        "Improvement_Method": str(variant["improvement_method"]),
        "Beam_Preserving_Optimizer": str(variant["beam_preserving_optimizer"]),
        "Local_Search_Optimizer": str(variant["local_search_optimizer"]),
    }
    merged.update({key: str(value) for key, value in row.items()})
    return merged


def _rank_rows_globally(
    rows: list[dict[str, str]],
    metric_name: str,
    rank_field: str,
    higher_is_better: bool,
) -> None:
    values: list[float] = []
    for row in rows:
        value = common._to_float(row.get(metric_name))
        values.append(value if value is not None else 0.0)

    ordered_unique = sorted(set(values), reverse=higher_is_better)
    rank_by_value = {value: index + 1 for index, value in enumerate(ordered_unique)}
    for row in rows:
        value = common._to_float(row.get(metric_name))
        row[rank_field] = str(rank_by_value.get(value if value is not None else 0.0, len(ordered_unique) + 1))


def _merge_variant_file(output_name: str) -> Path:
    merged_rows: list[dict[str, str]] = []
    base_fieldnames: list[str] = []
    used_sources = 0

    for variant in VARIANTS:
        source_path = _stage8_variant_dir(variant) / output_name
        if not source_path.exists():
            continue
        rows, fieldnames = _read_csv_with_fieldnames(source_path)
        if not base_fieldnames:
            base_fieldnames = fieldnames
        used_sources += 1
        for row in rows:
            merged_rows.append(_decorate_row(row, variant))

    output_path = OUTPUT_DIR / output_name
    if used_sources == 0:
        if output_path.exists():
            return output_path
        raise FileNotFoundError(
            f"No Stage 8 source files available to build: {output_path}"
        )

    for metric_name, rank_field, higher_is_better in [
        ("Beam_Relocations_Total", "Rank_Beam_Relocations_Total", False),
        ("Additional_Beams_Required", "Rank_Additional_Required_Beams", False),
        ("Additional_Grids_Required", "Rank_Additional_Required_Grids", False),
        ("Implementation_Effort_Total", "Rank_Implementation_Effort_Total", False),
        ("Standardization_Unique_Slot_Sizes", "Rank_Standardization", False),
        ("Additional_Fill_Height_Total_cm", "Rank_Additional_Fill_Height_Total_cm", False),
        (
            "Additional_Fill_Extra_Slot_Size_Variants",
            "Rank_Additional_Fill_Extra_Slot_Size_Variants",
            False,
        ),
        ("Empty_Slot_Space_m3", "Rank_Empty_Slot_Space_m3", True),
        ("Total_Slot_Space_m3", "Rank_Total_Slot_Space_m3", True),
        ("Space_Utilization_Pct", "Rank_Space_Utilization_Pct", False),
        ("Occupancy_Rate", "Rank_Occupancy_Rate", False),
    ]:
        if any(metric_name in row for row in merged_rows):
            _rank_rows_globally(merged_rows, metric_name, rank_field, higher_is_better)

    drop_fields = DROP_FIELDS_BY_OUTPUT.get(output_name, set())
    if output_name == "Candidate_Layout_Metric_Ranking.csv":
        drop_fields = drop_fields | {field for field in base_fieldnames if field.startswith("Rank_")}
    fieldnames = [
        field
        for field in OUTPUT_PREFIX_FIELDS + base_fieldnames
        if field not in drop_fields
    ]
    _write_csv(
        output_path,
        fieldnames,
        [{field: str(row.get(field, "")) for field in fieldnames} for row in merged_rows],
    )
    return output_path


def _write_merged_weighted_sum_ranking(source_rows: list[dict[str, str]]) -> Path:
    weights = stage8_common._read_wsm_weights()
    metric_specs = stage8_common.WSM_METRIC_SPECS
    metric_values = {
        metric: [common._to_float(row.get(metric)) or 0.0 for row in source_rows]
        for metric, _direction in metric_specs
    }
    scored_rows: list[dict[str, str]] = []
    for row in source_rows:
        scored = {
            field: str(row.get(field, ""))
            for field in OUTPUT_PREFIX_FIELDS + ["Config_ID"]
        }
        scored["Required_Beams_Total_Raw"] = str(row.get("Required_Beams_Total", ""))
        scored["Required_Grids_Total_Raw"] = str(row.get("Required_Grids_Total", ""))
        score = 0.0
        for metric, direction in metric_specs:
            value = common._to_float(row.get(metric)) or 0.0
            normalized = stage8_common._normalise_wsm_value(value, metric_values[metric], direction)
            contribution = normalized * weights[metric]
            scored[f"{metric}_Raw"] = f"{value:.6f}"
            scored[f"{metric}_Normalized"] = f"{normalized:.6f}"
            scored[f"{metric}_Weighted"] = f"{contribution:.6f}"
            score += contribution
        scored["Weighted_Sum_Score"] = f"{score:.6f}"
        scored_rows.append(scored)

    scored_rows.sort(
        key=lambda item: (
            -float(item["Weighted_Sum_Score"]),
            item.get("Heuristic_Label", ""),
            item.get("Config_ID", ""),
        )
    )
    for rank, row in enumerate(scored_rows, start=1):
        row["Weighted_Sum_Rank"] = str(rank)

    fields = ["Weighted_Sum_Rank"] + OUTPUT_PREFIX_FIELDS + ["Config_ID"]
    fields.extend(["Required_Beams_Total_Raw", "Required_Grids_Total_Raw"])
    for metric, _direction in metric_specs:
        fields.extend([f"{metric}_Raw", f"{metric}_Normalized", f"{metric}_Weighted"])
    fields.append("Weighted_Sum_Score")
    _write_csv(WSM_OUTPUT_FILE, fields, scored_rows)
    return WSM_OUTPUT_FILE


def _write_heuristic_combination_averages(source_file: Path, output_file: Path) -> Path:
    if not source_file.exists():
        raise FileNotFoundError(f"Missing source file for heuristic averages: {source_file}")

    with source_file.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        rows = list(reader)

    by_label: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        label = str(row.get("Heuristic_Label", "")).strip()
        if label:
            by_label.setdefault(label, []).append(row)

    average_rows: list[dict[str, str]] = []
    for variant in VARIANTS:
        label = str(variant["label"])
        label_rows = by_label.get(label, [])
        if not label_rows:
            continue

        averaged = {
            "Heuristic_Label": label,
            "Construction_Method": str(variant["construction_method"]),
            "Improvement_Method": str(variant["improvement_method"]),
            "Variant_Count": str(len(label_rows)),
        }
        for field in AVERAGE_KPI_FIELDS:
            values = [common._to_float(row.get(field)) for row in label_rows]
            numeric_values = [value for value in values if value is not None]
            averaged[field] = f"{(sum(numeric_values) / len(numeric_values)) if numeric_values else 0.0:.6f}"

        average_rows.append(averaged)

    fieldnames = ["Heuristic_Label", "Construction_Method", "Improvement_Method", "Variant_Count"] + AVERAGE_KPI_FIELDS
    _write_csv(output_file, fieldnames, average_rows)
    return output_file


def build_merged_final_selection_outputs() -> list[Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stage6_impact_rows = _generate_stage8_variants()
    ranking_file = _merge_variant_file("Candidate_Layout_Metric_Ranking.csv")
    figures_file = _write_merged_weighted_sum_ranking(_read_csv_with_fieldnames(ranking_file)[0])
    figures_module = _load_stage8_common_module()
    outputs = [
        ranking_file,
        figures_file,
        _write_heuristic_combination_averages(ranking_file, OUTPUT_DIR / "Heuristic_Combination_KPI_Averages.csv"),
        OUTPUT_DIR / "Heuristic_Combination_Runtime.csv",
        _merge_variant_file("Final_Layout_By_Rack_Column.csv"),
        _merge_variant_file("Final_Layout_By_Location.csv"),
        _merge_variant_file("Final_Layout_By_Segment.csv"),
        variants_common.write_stage6_impact_output(OUTPUT_DIR / "Stage6_Heuristic_Impact_All.csv", stage6_impact_rows),
    ]

    figures_module_path = PIPELINE_ROOT / "08_Final_Selection" / "08_final_selection_figures.py"
    figures_spec = importlib.util.spec_from_file_location("stage8_figures_for_comparison", figures_module_path)
    if figures_spec is None or figures_spec.loader is None:
        raise ImportError(f"Could not load Stage 8 figures module from {figures_module_path}")
    figures_module = importlib.util.module_from_spec(figures_spec)
    figures_spec.loader.exec_module(figures_module)
    figures_module.generate_figures(figures_file, OUTPUT_DIR / "Figures")

    if VARIANTS_ROOT.exists():
        shutil.rmtree(VARIANTS_ROOT)

    # Keep only merged Stage 8 outputs after comparison build.
    for old_dir in [
        common.OUTPUT_ROOT / "08_Final_Selection",
        common.OUTPUT_ROOT / "08_Final_Selection_Greedy",
        common.OUTPUT_ROOT / "08_Final_Selection_BeamOptimizer_Experimental",
    ]:
        if old_dir.exists():
            shutil.rmtree(old_dir)

    return outputs


if __name__ == "__main__":
    merged_files = build_merged_final_selection_outputs()
    print("Merged Stage 8 heuristic comparison outputs written to:")
    for output_path in merged_files:
        print(f" - {output_path}")