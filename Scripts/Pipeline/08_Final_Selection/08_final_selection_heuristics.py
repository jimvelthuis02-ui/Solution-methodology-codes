import csv
import importlib.util
from pathlib import Path
import shutil
import sys

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


OUTPUT_DIR = common.OUTPUT_ROOT / "08_Final_Selection_Comparison_AllHeuristics"
VARIANTS_ROOT = OUTPUT_DIR / "Variants"

VARIANTS = variants_common.VARIANTS
HEURISTIC_FIELDS = variants_common.HEURISTIC_FIELDS


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
    try:
        for variant in VARIANTS:
            print(f"Running Stage 8 variant: {variant['label']}", flush=True)
            variants_common.run_stage6_variant(VARIANTS_ROOT, variant, stage6_impact_rows)
            variants_common.run_stage7_variant(VARIANTS_ROOT, variant)
            variants_common.run_stage8_variant(VARIANTS_ROOT, variant)
            print(f"Completed Stage 8 variant: {variant['label']}", flush=True)
    finally:
        variants_common.restore_bounded_beam_order(original_best_slot_order)

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


def _rank_rows_globally(rows: list[dict[str, str]], metric_name: str, rank_field: str) -> None:
    values: list[float] = []
    for row in rows:
        value = common._to_float(row.get(metric_name))
        values.append(value if value is not None else 0.0)

    ordered_unique = sorted(set(values), reverse=False)
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

    for metric_name, rank_field in [
        ("Beam_Relocations_Total", "Rank_Beam_Relocations_Total"),
        ("Empty_Slot_Space_m3", "Rank_Empty_Slot_Space_m3"),
        ("Total_Slot_Space_m3", "Rank_Total_Slot_Space_m3"),
        ("Space_Utilization_Pct", "Rank_Space_Utilization_Pct"),
        ("Occupancy_Rate", "Rank_Occupancy_Rate"),
    ]:
        if any(metric_name in row for row in merged_rows):
            _rank_rows_globally(merged_rows, metric_name, rank_field)

    _write_csv(output_path, HEURISTIC_FIELDS + base_fieldnames, merged_rows)
    return output_path


def build_merged_final_selection_outputs() -> list[Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stage6_impact_rows = _generate_stage8_variants()
    outputs = [
        _merge_variant_file("Candidate_Layout_Metric_Ranking.csv"),
        _merge_variant_file("Final_Layout_By_Rack_Column.csv"),
        _merge_variant_file("Final_Layout_By_Location.csv"),
        _merge_variant_file("Final_Layout_By_Segment.csv"),
        variants_common.write_stage6_impact_output(OUTPUT_DIR / "Stage6_Heuristic_Impact_All.csv", stage6_impact_rows),
    ]

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