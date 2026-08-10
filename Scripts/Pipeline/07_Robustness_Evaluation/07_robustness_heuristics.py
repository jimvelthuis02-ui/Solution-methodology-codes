import csv
import importlib.util
from pathlib import Path
import shutil
import sys
from typing import Any

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


OUTPUT_DIR = common.OUTPUT_ROOT / "07_Robustness_Evaluation_Comparison"
VARIANTS_ROOT = OUTPUT_DIR / "Variants"

VARIANTS = [
    {
        "label": "Baseline_None",
        "construction_method": "baseline",
        "use_beam_optimizer": False,
        "use_local_search": False,
        "improvement_method": "none",
        "beam_preserving_optimizer": "NO",
        "local_search_optimizer": "NO",
    },
    {
        "label": "Baseline_LocalSearch",
        "construction_method": "baseline",
        "use_beam_optimizer": False,
        "use_local_search": True,
        "improvement_method": "local_search",
        "beam_preserving_optimizer": "NO",
        "local_search_optimizer": "YES",
    },
    {
        "label": "Baseline_BeamPreserving",
        "construction_method": "baseline",
        "use_beam_optimizer": True,
        "use_local_search": False,
        "improvement_method": "beam_preserving",
        "beam_preserving_optimizer": "YES",
        "local_search_optimizer": "NO",
    },
    {
        "label": "Baseline_BeamPlusLocalSearch",
        "construction_method": "baseline",
        "use_beam_optimizer": True,
        "use_local_search": True,
        "improvement_method": "beam_preserving_plus_local_search",
        "beam_preserving_optimizer": "YES",
        "local_search_optimizer": "YES",
    },
    {
        "label": "Greedy_None",
        "construction_method": "greedy",
        "use_beam_optimizer": False,
        "use_local_search": False,
        "improvement_method": "none",
        "beam_preserving_optimizer": "NO",
        "local_search_optimizer": "NO",
    },
    {
        "label": "Greedy_LocalSearch",
        "construction_method": "greedy",
        "use_beam_optimizer": False,
        "use_local_search": True,
        "improvement_method": "local_search",
        "beam_preserving_optimizer": "NO",
        "local_search_optimizer": "YES",
    },
    {
        "label": "Greedy_BeamPreserving",
        "construction_method": "greedy",
        "use_beam_optimizer": True,
        "use_local_search": False,
        "improvement_method": "beam_preserving",
        "beam_preserving_optimizer": "YES",
        "local_search_optimizer": "NO",
    },
    {
        "label": "Greedy_BeamPlusLocalSearch",
        "construction_method": "greedy",
        "use_beam_optimizer": True,
        "use_local_search": True,
        "improvement_method": "beam_preserving_plus_local_search",
        "beam_preserving_optimizer": "YES",
        "local_search_optimizer": "YES",
    },
]

HEURISTIC_FIELDS = [
    "Heuristic_Label",
    "Construction_Method",
    "Improvement_Method",
    "Beam_Preserving_Optimizer",
    "Local_Search_Optimizer",
]

# Exact beam-order search can become expensive on long columns.
BEAM_ORDER_EXACT_SLOT_LIMIT = 14


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


def _make_bounded_best_slot_order(original_fn):
    def _bounded(slot_sizes: list[float], target_heights: list[float]) -> list[float]:
        if len(slot_sizes) <= BEAM_ORDER_EXACT_SLOT_LIMIT:
            return original_fn(slot_sizes, target_heights)
        if len(slot_sizes) <= 1 or not target_heights:
            return list(slot_sizes)

        remaining = [int(round(value)) for value in slot_sizes]
        ordered: list[int] = []
        prefix_height = 0
        tolerance = float(getattr(common, "BEAM_RELOCATION_TOLERANCE_CM", 1e-9))

        while remaining:
            best_idx = 0
            best_key: tuple[int, float, int] | None = None
            seen: dict[int, int] = {}

            for idx, size in enumerate(remaining):
                if size in seen:
                    continue
                seen[size] = idx
                next_prefix = prefix_height + size
                min_delta = min(abs(next_prefix - float(target)) for target in target_heights)
                match = 1 if min_delta <= tolerance else 0
                candidate_key = (match, -min_delta, size)
                if best_key is None or candidate_key > best_key:
                    best_key = candidate_key
                    best_idx = idx

            chosen = remaining.pop(best_idx)
            ordered.append(chosen)
            prefix_height += chosen

        return [float(value) for value in ordered]

    return _bounded


def _load_stage9_module() -> Any:
    stage9_path = PIPELINE_ROOT / "09_Heuristic_Comparison" / "09_layout_heuristic_comparison.py"
    spec = importlib.util.spec_from_file_location("stage9_for_stage7_variants", stage9_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load Stage 9 module from {stage9_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage7_variant_file(variant: dict[str, object]) -> Path:
    folder = "07_Robustness_Evaluation_Greedy" if str(variant["construction_method"]) == "greedy" else "07_Robustness_Evaluation"
    return VARIANTS_ROOT / str(variant["label"]) / folder / "Candidate_Layout_Robustness_Summary.csv"


def _generate_stage7_variants() -> None:
    if VARIANTS_ROOT.exists():
        shutil.rmtree(VARIANTS_ROOT)
    VARIANTS_ROOT.mkdir(parents=True, exist_ok=True)

    stage9: Any = _load_stage9_module()
    stage9.VARIANTS_ROOT = VARIANTS_ROOT

    original_best_slot_order = common._best_slot_order_for_targets
    common._best_slot_order_for_targets = _make_bounded_best_slot_order(original_best_slot_order)

    stage6_impact_rows: list[dict[str, str]] = []
    try:
        for variant in VARIANTS:
            print(f"Running Stage 7 variant: {variant['label']}", flush=True)
            stage9._run_stage6_variant(variant, stage6_impact_rows)
            stage9._run_stage7_variant(variant)
            print(f"Completed Stage 7 variant: {variant['label']}", flush=True)
    finally:
        common._best_slot_order_for_targets = original_best_slot_order


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