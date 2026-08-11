import csv
import importlib.util
from pathlib import Path
import shutil
import sys

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))
STAGE8_DIR = PIPELINE_ROOT / "08_Final_Selection"
if str(STAGE8_DIR) not in sys.path:
    sys.path.insert(0, str(STAGE8_DIR))

import run_ordered_pipeline as common


def _load_variants_common_module():
    module_path = STAGE8_DIR / "08_heuristic_variants_common.py"
    spec = importlib.util.spec_from_file_location("heuristic_variants_common_for_stage7", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load shared helper module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


variants_common = _load_variants_common_module()


OUTPUT_DIR = common.OUTPUT_ROOT / "07_Robustness_Evaluation_Comparison"
VARIANTS_ROOT = OUTPUT_DIR / "Variants"

VARIANTS = variants_common.VARIANTS
HEURISTIC_FIELDS = variants_common.HEURISTIC_FIELDS


def _read_csv_with_fieldnames(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing Stage 7 source file: {path}")

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


def _stage7_variant_file(variant: dict[str, object]) -> Path:
    return variants_common.stage7_dir(VARIANTS_ROOT, variant) / "Candidate_Layout_Robustness_Summary.csv"


def _generate_stage7_variants() -> None:
    if VARIANTS_ROOT.exists():
        shutil.rmtree(VARIANTS_ROOT)
    VARIANTS_ROOT.mkdir(parents=True, exist_ok=True)

    original_best_slot_order = variants_common.apply_bounded_beam_order()

    stage6_impact_rows: list[dict[str, str]] = []
    try:
        for variant in VARIANTS:
            print(f"Running Stage 7 variant: {variant['label']}", flush=True)
            variants_common.run_stage6_variant(VARIANTS_ROOT, variant, stage6_impact_rows)
            variants_common.run_stage7_variant(VARIANTS_ROOT, variant)
            print(f"Completed Stage 7 variant: {variant['label']}", flush=True)
    finally:
        variants_common.restore_bounded_beam_order(original_best_slot_order)


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


def build_merged_robustness_outputs() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _generate_stage7_variants()

    merged_rows: list[dict[str, str]] = []
    base_fieldnames: list[str] = []
    used_sources = 0
    for variant in VARIANTS:
        source_path = _stage7_variant_file(variant)
        if not source_path.exists():
            continue
        rows, fieldnames = _read_csv_with_fieldnames(source_path)
        if not base_fieldnames:
            base_fieldnames = fieldnames
        used_sources += 1
        for row in rows:
            merged_rows.append(_decorate_row(row, variant))

    output_path = OUTPUT_DIR / "Candidate_Layout_Robustness_Summary.csv"
    if used_sources == 0:
        if output_path.exists():
            return output_path
        raise FileNotFoundError(
            f"No Stage 7 source files available to build: {output_path}"
        )

    _write_csv(output_path, HEURISTIC_FIELDS + base_fieldnames, merged_rows)

    if VARIANTS_ROOT.exists():
        shutil.rmtree(VARIANTS_ROOT)

    # Keep only merged Stage 7 outputs after comparison build.
    for old_dir in [
        common.OUTPUT_ROOT / "07_Robustness_Evaluation",
        common.OUTPUT_ROOT / "07_Robustness_Evaluation_Greedy",
        common.OUTPUT_ROOT / "07_Robustness_Evaluation_BeamOptimizer_Experimental",
    ]:
        if old_dir.exists():
            shutil.rmtree(old_dir)

    return output_path


if __name__ == "__main__":
    output_path = build_merged_robustness_outputs()
    print(f"Merged Stage 7 heuristic comparison output written to: {output_path}")