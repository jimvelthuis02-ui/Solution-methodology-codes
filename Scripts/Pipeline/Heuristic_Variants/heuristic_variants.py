import csv
import importlib.util
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


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
        "label": "Greedy_None",
        "construction_method": "greedy",
        "use_beam_optimizer": False,
        "use_local_search": False,
        "improvement_method": "none",
        "beam_preserving_optimizer": "NO",
        "local_search_optimizer": "NO",
    },
    {
        "label": "ConstructiveBeam_None",
        "construction_method": "constructive_beam",
        "use_beam_optimizer": False,
        "use_local_search": False,
        "improvement_method": "none",
        "beam_preserving_optimizer": "CONSTRUCTIVE",
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
        "label": "Greedy_LocalSearch",
        "construction_method": "greedy",
        "use_beam_optimizer": False,
        "use_local_search": True,
        "improvement_method": "local_search",
        "beam_preserving_optimizer": "NO",
        "local_search_optimizer": "YES",
    },
    {
        "label": "ConstructiveBeam_LocalSearch",
        "construction_method": "constructive_beam",
        "use_beam_optimizer": False,
        "use_local_search": True,
        "improvement_method": "local_search",
        "beam_preserving_optimizer": "CONSTRUCTIVE",
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

BEAM_ORDER_EXACT_SLOT_LIMIT = 14

def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def apply_bounded_beam_order() -> Any:
    original_best_slot_order = common._best_slot_order_for_targets
    common._best_slot_order_for_targets = _make_bounded_best_slot_order(original_best_slot_order)
    return original_best_slot_order


def restore_bounded_beam_order(original_fn: Any) -> None:
    common._best_slot_order_for_targets = original_fn


def variant_root(variants_root: Path, variant: dict[str, object]) -> Path:
    return variants_root / str(variant["label"])


def stage6_dir(variants_root: Path, variant: dict[str, object]) -> Path:
    folder = "06_Layout_Generation_Greedy" if str(variant["construction_method"]) == "greedy" else "06_Layout_Generation"
    return variant_root(variants_root, variant) / folder


def stage7_dir(variants_root: Path, variant: dict[str, object]) -> Path:
    folder = "07_Robustness_Evaluation_Greedy" if str(variant["construction_method"]) == "greedy" else "07_Robustness_Evaluation"
    return variant_root(variants_root, variant) / folder


def stage8_dir(variants_root: Path, variant: dict[str, object]) -> Path:
    folder = "08_Final_Selection_Greedy" if str(variant["construction_method"]) == "greedy" else "08_Final_Selection"
    return variant_root(variants_root, variant) / folder


def _stage8_ranking_file(variants_root: Path, variant: dict[str, object]) -> Path:
    return stage8_dir(variants_root, variant) / "Candidate_Layout_Metric_Ranking.csv"


def _rows_by_config_with_fieldnames(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    if not path.exists():
        return {}, []

    with path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    by_config = {
        str(row.get("Config_ID", "")).strip(): row
        for row in rows
        if str(row.get("Config_ID", "")).strip()
    }
    return by_config, fieldnames


def _load_greedy_helpers():
    pipeline_root = Path(__file__).resolve().parents[1]
    return _load_module(
        pipeline_root / "06_Layout_Generation" / "06_layout_generation_greedy.py",
        "layout_generation_greedy_helpers_module",
    )


def _load_stage6_module(variant: dict[str, object]):
    pipeline_root = Path(__file__).resolve().parents[1]
    return _load_module(
        pipeline_root / "06_Layout_Generation" / "06_layout_generation.py",
        f"stage6_variant_{variant['label']}",
    )


def _load_stage7_module(variant: dict[str, object]):
    pipeline_root = Path(__file__).resolve().parents[1]
    return _load_module(
        pipeline_root / "07_Robustness_Evaluation" / "07_robustness_evaluation.py",
        f"stage7_variant_{variant['label']}",
    )


def _load_stage8_module(variant: dict[str, object]):
    pipeline_root = Path(__file__).resolve().parents[1]
    return _load_module(
        pipeline_root / "08_Final_Selection" / "08_final_selection.py",
        f"stage8_variant_{variant['label']}",
    )


def _make_greedy_allocate_wrapper(stage6: Any, greedy_helpers: Any):
    prepared_rows = common._read_csv(common.STAGE1_OUTPUT_DIR / "Location_Details_Prepared.csv")
    rows_by_column = greedy_helpers._original_rows_by_column(prepared_rows)
    config_ids = [str(row.get("Config_ID", "")).strip() for row in stage6._candidate_configs()]
    anchor_records: list[dict[str, str]] = []
    call_index = 0
    original_allocate = common._allocate_layout_by_column

    def _allocate_with_greedy_anchor(
        target_exact_counts: dict[float, int],
        column_keys: list[str],
        style: str,
        style_context: dict[str, object] | None = None,
    ):
        nonlocal call_index

        config_id = config_ids[call_index] if call_index < len(config_ids) else f"CFG_CALL_{call_index + 1:03d}"
        call_index += 1

        config_slot_sizes = {int(round(size)) for size, count in target_exact_counts.items() if int(count) > 0}
        anchor_prefix = greedy_helpers._build_anchor_prefix_by_column(config_slot_sizes, column_keys, rows_by_column)

        anchor_counts = Counter()
        for values in anchor_prefix.values():
            for value in values:
                anchor_counts[int(round(value))] += 1

        remaining_counts: dict[float, int] = {}
        for size, required in target_exact_counts.items():
            used = anchor_counts.get(int(round(size)), 0)
            remaining_counts[size] = max(int(required) - used, 0)

        context = dict(style_context or {})
        existing_fixed = context.get("fixed_prefix_by_column", {})
        context["fixed_prefix_by_column"] = greedy_helpers._merge_fixed_prefixes(existing_fixed, anchor_prefix)

        anchor_records.append(
            {
                "Config_ID": config_id,
                "Config_Slot_Sizes": common._encode_excel_text(",".join(str(size) for size in sorted(config_slot_sizes))),
                "Anchor_Fixed_Slot_Distribution": "|".join(
                    f"{size}:{count}" for size, count in sorted(anchor_counts.items())
                ),
                "Anchor_Fixed_Slot_Count": str(sum(anchor_counts.values())),
                "Anchor_Fixed_Columns_Count": str(len(anchor_prefix)),
                "Remaining_Required_Counts_After_Anchoring": "|".join(
                    f"{int(size)}:{count}" for size, count in sorted(remaining_counts.items())
                ),
            }
        )

        return original_allocate(
            target_exact_counts=remaining_counts,
            column_keys=column_keys,
            style=style,
            style_context=context,
        )

    return original_allocate, _allocate_with_greedy_anchor, anchor_records


def _make_improvement_wrapper(stage6: Any, variant: dict[str, object], impact_rows: list[dict[str, str]]):
    use_beam = str(variant.get("beam_preserving_optimizer", "NO")).upper() == "CONSTRUCTIVE"
    use_local = bool(variant.get("use_local_search"))
    original_local_search = stage6._local_search_minimize_beam_relocations

    def _wrapped_improvement(
        column_assignments: dict[str, list[float]],
        layout_columns: list[str],
        fixed_prefix_by_column: dict[str, list[float]],
        beam_segments: set[tuple[str, int, int]],
        doorgang_thresholds_by_rack: dict[str, tuple[int, float]],
        smallest_config_slot: float,
        layout_id: str,
        config_id: str,
        style: str,
        current_beam_units: set[str],
        current_beam_heights: dict[str, float],
        current_beam_units_by_column: dict[str, set[str]],
        constructive_slot_sizes: list[float] | None = None,
    ) -> tuple[dict[str, list[float]], int, int, int]:
        working_assignments = column_assignments
        beam_changed = False
        if use_beam:
            working_assignments = common._constructive_beam_preservation_pass(
                column_assignments=column_assignments,
                segments=beam_segments,
                baseline_beam_heights=current_beam_heights,
                fixed_prefix_by_column=fixed_prefix_by_column,
                configuration_slot_sizes=constructive_slot_sizes,
            )
            working_assignments = stage6._enforce_segment_uniform_slot_profiles(
                working_assignments,
                beam_segments,
                fixed_prefix_by_column=fixed_prefix_by_column,
                doorgang_thresholds_by_rack=doorgang_thresholds_by_rack,
            )
            working_assignments = stage6._enforce_rack_row_count_consistency(
                working_assignments,
                available_slot_sizes=constructive_slot_sizes,
                fixed_prefix_by_column=fixed_prefix_by_column,
            )
            working_assignments, _doorgang_conversions = stage6._enforce_doorgang_spanning_beam_alignment(
                working_assignments,
                beam_segments,
                doorgang_thresholds_by_rack,
                fixed_prefix_by_column=fixed_prefix_by_column,
            )
            if smallest_config_slot > 0.0:
                working_assignments = stage6._enforce_min_locations_per_column(
                    working_assignments,
                    layout_columns,
                    float(smallest_config_slot),
                )
            beam_changed = working_assignments != column_assignments

        # The constructive beam pass is a construction heuristic, so the impact
        # baseline starts after construction is complete.
        before_any, _by_col0, _r0, _a0, _rows0, _units0 = stage6._evaluate_relocations_for_assignments(
            working_assignments,
            layout_id,
            config_id,
            style,
            beam_segments,
            doorgang_thresholds_by_rack,
            current_beam_units,
            current_beam_heights,
            current_beam_units_by_column,
        )
        after_beam = before_any

        if use_local:
            final_assignments, reloc_before_search, reloc_after_search, accepted_search_moves = original_local_search(
                column_assignments=working_assignments,
                layout_columns=layout_columns,
                fixed_prefix_by_column=fixed_prefix_by_column,
                beam_segments=beam_segments,
                doorgang_thresholds_by_rack=doorgang_thresholds_by_rack,
                smallest_config_slot=smallest_config_slot,
                layout_id=layout_id,
                config_id=config_id,
                style=style,
                current_beam_units=current_beam_units,
                current_beam_heights=current_beam_heights,
                current_beam_units_by_column=current_beam_units_by_column,
                constructive_slot_sizes=constructive_slot_sizes,
            )
        else:
            final_assignments = working_assignments
            reloc_before_search = after_beam
            reloc_after_search = after_beam
            accepted_search_moves = 0

        impact_rows.append(
            {
                "Variant_Label": str(variant["label"]),
                "Construction_Method": str(variant["construction_method"]),
                "Improvement_Method": str(variant["improvement_method"]),
                "Config_ID": str(config_id),
                "Beam_Relocations_Before_Any_Improvement": str(before_any),
                "Beam_Relocations_After_Beam_Optimizer": str(after_beam),
                "Beam_Relocations_After_Local_Search": str(reloc_after_search),
                "Beam_Optimizer_Delta": "0",
                "Local_Search_Delta": str(reloc_after_search - after_beam),
                "Total_Delta": str(reloc_after_search - before_any),
                "Beam_Optimizer_Changed_Assignments": "YES" if beam_changed else "NO",
                "Local_Search_Enabled": "YES" if use_local else "NO",
                "Local_Search_Accepted_Moves": str(accepted_search_moves),
            }
        )

        return final_assignments, reloc_before_search, reloc_after_search, accepted_search_moves

    return original_local_search, _wrapped_improvement


def run_stage6_variant(variants_root: Path, variant: dict[str, object], all_impact_rows: list[dict[str, str]]) -> Path:
    stage6: Any = _load_stage6_module(variant)
    output_dir = stage6_dir(variants_root, variant)
    output_dir.mkdir(parents=True, exist_ok=True)

    stage6.LAYOUT_OUTPUT_DIR = output_dir
    stage6.LAYOUT_TOPFILLED_DIR = output_dir
    stage6.LAYOUT_DIAGNOSTICS_DIR = output_dir

    original_allocate = None
    greedy_anchor_rows: list[dict[str, str]] = []
    if str(variant["construction_method"]) == "greedy":
        greedy_helpers = _load_greedy_helpers()
        original_allocate, wrapped_allocate, greedy_anchor_rows = _make_greedy_allocate_wrapper(stage6, greedy_helpers)
        common._allocate_layout_by_column = wrapped_allocate

    original_local_search, wrapped_improvement = _make_improvement_wrapper(stage6, variant, all_impact_rows)
    stage6._local_search_minimize_beam_relocations = wrapped_improvement

    try:
        stage6.build_layout_generation()
    finally:
        stage6._local_search_minimize_beam_relocations = original_local_search
        if original_allocate is not None:
            common._allocate_layout_by_column = original_allocate

    if greedy_anchor_rows:
        _write_csv(
            output_dir / "Greedy_Fixed_Slots_Summary.csv",
            [
                "Config_ID",
                "Config_Slot_Sizes",
                "Anchor_Fixed_Slot_Distribution",
                "Anchor_Fixed_Slot_Count",
                "Anchor_Fixed_Columns_Count",
                "Remaining_Required_Counts_After_Anchoring",
            ],
            greedy_anchor_rows,
        )

    return output_dir


def run_stage7_variant(variants_root: Path, variant: dict[str, object]) -> Path:
    stage7: Any = _load_stage7_module(variant)
    output_file = stage7_dir(variants_root, variant) / "Candidate_Layout_Robustness_Summary.csv"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    stage7.LAYOUT_SUMMARY_FILE = stage6_dir(variants_root, variant) / "Candidate_Layout_Summary_TopFilled.csv"
    stage7.ROBUSTNESS_SUMMARY_FILE = output_file
    stage7.NON_FEASIBLE_OUTPUT_FILE = output_file.parent / "Non_Feasible_Layouts.csv"
    stage7.NON_ROBUST_OUTPUT_FILE = output_file.parent / "Non_Robust_Layouts.csv"

    stage7.build_robustness_evaluation()
    return output_file


def run_stage8_variant(variants_root: Path, variant: dict[str, object]) -> Path:
    stage8: Any = _load_stage8_module(variant)
    output_dir = stage8_dir(variants_root, variant)
    output_dir.mkdir(parents=True, exist_ok=True)

    stage8.ROBUSTNESS_SUMMARY_FILE = stage7_dir(variants_root, variant) / "Candidate_Layout_Robustness_Summary.csv"
    stage8.LAYOUT_SUMMARY_FILE = stage6_dir(variants_root, variant) / "Candidate_Layout_Summary_TopFilled.csv"
    stage8.LAYOUT_BY_COLUMN_FILE = stage6_dir(variants_root, variant) / "Candidate_Layout_By_Rack_Column_TopFilled.csv"
    stage8.LAYOUT_BY_LOCATION_FILE = stage6_dir(variants_root, variant) / "Candidate_Layout_By_Location_TopFilled.csv"
    stage8.OUTPUT_FILE = output_dir / "Candidate_Layout_Metric_Ranking.csv"
    stage8.FINAL_LAYOUT_BY_COLUMN_FILE = output_dir / "Final_Layout_By_Rack_Column.csv"
    stage8.FINAL_LAYOUT_BY_LOCATION_FILE = output_dir / "Final_Layout_By_Location.csv"
    stage8.FINAL_LAYOUT_BY_SEGMENT_FILE = output_dir / "Final_Layout_By_Segment.csv"
    stage8.LEGACY_OUTPUT_FILES = [
        output_dir / "Objective_Layout_Recommendations.csv",
        output_dir / "Management_Decision_Table.csv",
    ]

    stage8.build_final_selection()
    return stage8.OUTPUT_FILE


def write_variant_definitions(output_file: Path) -> Path:
    rows = [
        {
            "Variant_Label": str(variant["label"]),
            "Construction_Method": str(variant["construction_method"]),
            "Use_Beam_Optimizer": "YES" if bool(variant["use_beam_optimizer"]) else "NO",
            "Use_Local_Search": "YES" if bool(variant["use_local_search"]) else "NO",
            "Improvement_Method": str(variant["improvement_method"]),
        }
        for variant in VARIANTS
    ]
    _write_csv(
        output_file,
        [
            "Variant_Label",
            "Construction_Method",
            "Use_Beam_Optimizer",
            "Use_Local_Search",
            "Improvement_Method",
        ],
        rows,
    )
    return output_file


def write_stage6_impact_output(output_file: Path, impact_rows: list[dict[str, str]]) -> Path:
    _write_csv(
        output_file,
        [
            "Variant_Label",
            "Construction_Method",
            "Improvement_Method",
            "Config_ID",
            "Beam_Relocations_Before_Any_Improvement",
            "Beam_Relocations_After_Beam_Optimizer",
            "Beam_Relocations_After_Local_Search",
            "Beam_Optimizer_Delta",
            "Local_Search_Delta",
            "Total_Delta",
            "Beam_Optimizer_Changed_Assignments",
            "Local_Search_Enabled",
            "Local_Search_Accepted_Moves",
        ],
        [
            {field: str(row.get(field, "")) for field in [
                "Variant_Label",
                "Construction_Method",
                "Improvement_Method",
                "Config_ID",
                "Beam_Relocations_Before_Any_Improvement",
                "Beam_Relocations_After_Beam_Optimizer",
                "Beam_Relocations_After_Local_Search",
                "Beam_Optimizer_Delta",
                "Local_Search_Delta",
                "Total_Delta",
                "Beam_Optimizer_Changed_Assignments",
                "Local_Search_Enabled",
                "Local_Search_Accepted_Moves",
            ]}
            for row in impact_rows
        ],
    )
    return output_file


def build_wide_comparison(output_file: Path, variants_root: Path) -> Path:
    config_rows = common._read_csv(common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv")
    configs_by_id = {
        str(row.get("Config_ID", "")).strip(): row
        for row in config_rows
        if str(row.get("Config_ID", "")).strip()
    }

    variant_rows_by_label: dict[str, dict[str, dict[str, str]]] = {}
    metric_fieldnames: list[str] = []
    for variant in VARIANTS:
        rows_by_config, fieldnames = _rows_by_config_with_fieldnames(_stage8_ranking_file(variants_root, variant))
        variant_rows_by_label[str(variant["label"])] = rows_by_config
        if fieldnames and not metric_fieldnames:
            metric_fieldnames = [field for field in fieldnames if field != "Config_ID"]

    all_config_ids = sorted(
        set(configs_by_id.keys())
        | {config_id for rows in variant_rows_by_label.values() for config_id in rows.keys()}
    )

    output_rows: list[dict[str, str]] = []
    for config_id in all_config_ids:
        config = configs_by_id.get(config_id, {})
        merged = {
            "Config_ID": config_id,
            "Method": str(config.get("Method", "")),
            "Scenario": str(config.get("Scenario", "")),
            "K": str(config.get("K", "")),
            "Slot_Sizes": str(config.get("Slot_Sizes", "")),
            "Relative Slot Size Distribution": str(config.get("Relative Slot Size Distribution", "")),
            "Source Sample": str(config.get("Source Sample", "")),
        }

        for variant in VARIANTS:
            label = str(variant["label"])
            row = variant_rows_by_label.get(label, {}).get(config_id, {})
            merged[f"Present_{label}"] = "YES" if row else "NO"
            for field in metric_fieldnames:
                merged[f"{label}__{field}"] = str(row.get(field, ""))

        output_rows.append(merged)

    fieldnames = [
        "Config_ID",
        "Method",
        "Scenario",
        "K",
        "Slot_Sizes",
        "Relative Slot Size Distribution",
        "Source Sample",
    ]
    for variant in VARIANTS:
        label = str(variant["label"])
        fieldnames.append(f"Present_{label}")
        fieldnames.extend(f"{label}__{field}" for field in metric_fieldnames)

    _write_csv(output_file, fieldnames, output_rows)
    return output_file
