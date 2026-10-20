"""Merge baseline and heuristic Stage 6-8 outputs into Power BI-ready combined CSV files."""

import csv
import importlib.util
import re
from pathlib import Path

import run_ordered_pipeline as common

# Method labels, variant folders, and stage-specific file stems for combined exports.
METHODS = ("Baseline", "SU", "PE", "BP")
VARIANT_NAMES = {"SU": "Space_Utilization", "PE": "Picking_Efficiency", "BP": "Beam_Preservation"}
HEURISTIC_DESCRIPTIONS = {
    "Baseline": ("Baseline", "Baseline layout generation"),
    "SU": ("Space_Utilization", "Maximize empty slot space"),
    "PE": ("Picking_Efficiency", "Place demanded slot sizes at easily reachable rows"),
    "BP": ("Beam_Preservation", "Keep profiles close to the current beam heights"),
}
STAGE_PREFIX = {6: "06_Layout_Generation", 7: "07_Robustness_Evaluation", 8: "08_Final_Selection"}
STAGE_FILES = {
    6: [
        "Candidate_Layout_Summary",
        "Candidate_Layout_By_Location",
        "Candidate_Layout_By_Rack_Column",
        "Candidate_Layout_By_Rack",
        "Empty_Locations_By_Slot_Size",
        "Generated_Profiles_By_Config",
    ],
    7: [
        "Candidate_Layout_Robustness_Details",
    ],
    8: [
        "Candidate_Layout_Metric_Ranking",
        "Weighted_Sum_Method_Ranking",
        "Final_Layout_By_Location",
        "Final_Layout_By_Rack_Column",
        "Final_Layout_By_Rack",
    ],
}
# Combined-table schema additions and source files used to enrich method outputs.
_HEURISTIC_CFG_PATTERN = re.compile(r"(CFG_\d+)_(?:SU|PE|BP)\b")
_TRAILING_ZEROS_PATTERN = re.compile(r"^(-?\d+)\.(\d+)$")
# Source label columns replaced by the combined Heuristic column ("Method" is the pre-rename name).
LEGACY_LABEL_COLUMNS = {"Heuristic", "Method"}
RANKING_STEMS = {"Candidate_Layout_Metric_Ranking", "Weighted_Sum_Method_Ranking"}
SHARED_PICKING_COLUMNS = ("Shared_Picking_Score", "Shared_Picking_Locations", "Shared_Picking_Score_Per_Location")
PE_ONLY_PICKING_COLUMNS = {
    "Picking_Covered_Item_Count",
    "Picking_Covered_Pick_Count",
    "Picking_Uncovered_Item_Count",
    "Picking_Uncovered_Pick_Count",
    "Picking_Score",
}
PICKING_COMPARISON_FILE = common.OUTPUT_ROOT / "06_Picking_Score_Comparison" / "Picking_Score_Comparison.csv"
PE_LAYOUT_SUMMARY_FILE = common.OUTPUT_ROOT / "06_Layout_Generation_Picking_Efficiency" / "Candidate_Layout_Summary_PE.csv"
CANDIDATE_CONFIG_FILE = common.OUTPUT_ROOT / "04_Candidate_Configuration" / "Candidate_Configurations.csv"
WSM_WEIGHTS_FILE = common.ROOT / "Input files" / "WSM_Weights.csv"
COMBINED_ROOT = common.OUTPUT_ROOT / "Combined_Outputs"
STAGE6_COMPARISON_FIELDS = [
    "Occupancy_Rate",
    "Occupied_Slot_Space_cm",
    "Empty_Slot_Space_cm",
    "Total_Slot_Space_cm",
    "Occupied_Slot_Space_m3",
    "Empty_Slot_Space_m3",
    "Total_Slot_Space_m3",
    "Space_Utilization_Pct",
    "Picking_Covered_Item_Count",
    "Picking_Covered_Pick_Count",
    "Picking_Uncovered_Item_Count",
    "Picking_Uncovered_Pick_Count",
    "Picking_Score",
]
_STAGE8_METRICS_MODULE = None


def _clean_cell(value: str) -> str:
    """Make a cell Power BI friendly: no Excel text wrapper, no heuristic CFG suffix, no padded decimals."""
    text = _HEURISTIC_CFG_PATTERN.sub(r"\1", common._decode_excel_text(value))
    match = _TRAILING_ZEROS_PATTERN.match(text)
    if match:
        decimals = match.group(2).rstrip("0")
        return f"{match.group(1)}.{decimals}" if decimals else match.group(1)
    return text


def _shared_picking_scores() -> dict[tuple[str, str], tuple[str, str, str]]:
    """Load shared Stage 6 picking scores by heuristic and configuration."""
    if not PICKING_COMPARISON_FILE.exists():
        return {}
    scores: dict[tuple[str, str], tuple[str, str, str]] = {}
    with PICKING_COMPARISON_FILE.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            for method in METHODS:
                scores[(method, row["Config_ID"])] = (
                    _clean_cell(row[f"{method}_Picking_Score"]),
                    _clean_cell(row[f"{method}_Locations"]),
                    _clean_cell(row[f"{method}_Score_Per_Location"]),
                )
    return scores


def _picking_coverage_by_config() -> dict[str, dict[str, str]]:
    """Load PE-only coverage fields keyed by the unsuffixed configuration ID."""
    if not PE_LAYOUT_SUMMARY_FILE.exists():
        return {}
    fields = PE_ONLY_PICKING_COLUMNS - {"Picking_Score"}
    coverage: dict[str, dict[str, str]] = {}
    with PE_LAYOUT_SUMMARY_FILE.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            config_id = _HEURISTIC_CFG_PATTERN.sub(r"\1", str(row.get("Config_ID", "")))
            coverage[config_id] = {field: _clean_cell(row.get(field, "")) for field in fields}
    return coverage


def _stage8_space_metrics(layout_row: dict[str, str]) -> tuple[int, int, int, int, int, float, float]:
    """Reuse Stage 8 space calculations when enriching a combined Stage 6 row."""
    global _STAGE8_METRICS_MODULE
    if _STAGE8_METRICS_MODULE is None:
        stage8_path = Path(__file__).resolve().parent / "08_final_selection.py"
        spec = importlib.util.spec_from_file_location("stage8_combined_metrics", stage8_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load Stage 8 KPI helpers from {stage8_path}")
        _STAGE8_METRICS_MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_STAGE8_METRICS_MODULE)
    return _STAGE8_METRICS_MODULE._space_utilization_metrics(layout_row)


def _enrich_stage6_summary(
    row: dict[str, str],
    method: str,
    config_id: str,
    shared_scores: dict[tuple[str, str], tuple[str, str, str]],
    coverage_by_config: dict[str, dict[str, str]],
) -> None:
    """Add shared picking and derived space metrics to one combined Stage 6 row."""
    row.update(coverage_by_config.get(config_id, {}))
    picking = shared_scores.get((method, config_id))
    row["Picking_Score"] = picking[0] if picking else ""

    if str(row.get("Layout_Feasible", "")).strip().upper() != "YES":
        for field in STAGE6_COMPARISON_FIELDS[:8]:
            row[field] = ""
        return
    empty_space, total_space, utilization, occupancy = _stage8_space_metrics(row)
    occupied_space = total_space - empty_space
    row.update(
        {
            "Occupancy_Rate": f"{occupancy:.6f}",
            "Occupied_Slot_Space_cm": str(occupied_space),
            "Empty_Slot_Space_cm": str(empty_space),
            "Total_Slot_Space_cm": str(total_space),
            "Occupied_Slot_Space_m3": f"{occupied_space / 100.0:.6f}",
            "Empty_Slot_Space_m3": f"{empty_space / 100.0:.6f}",
            "Total_Slot_Space_m3": f"{total_space / 100.0:.6f}",
            "Space_Utilization_Pct": f"{utilization:.4f}",
        }
    )


def _stage_dir(stage: int, method: str) -> Path:
    """Resolve the output directory for a pipeline stage and heuristic method."""
    if method == "Baseline":
        return {6: common.STAGE6_OUTPUT_DIR, 7: common.STAGE7_OUTPUT_DIR, 8: common.STAGE8_OUTPUT_DIR}[stage]
    return common.OUTPUT_ROOT / f"{STAGE_PREFIX[stage]}_{VARIANT_NAMES[method]}"


def _source_path(stage: int, method: str, stem: str) -> Path:
    """Resolve a baseline or suffixed CSV path within its method's stage directory."""
    name = f"{stem}.csv" if method == "Baseline" else f"{stem}_{method}.csv"
    return _stage_dir(stage, method) / name


def _refresh_method_wsm_picking_scores() -> None:
    """Backfill shared picking scores into method WSM files after Stage 6 comparison."""
    shared_scores = _shared_picking_scores()
    if not shared_scores or not WSM_WEIGHTS_FILE.exists():
        return

    with WSM_WEIGHTS_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        picking_weight = next(
            (
                common._to_float(row.get("Weight"))
                for row in csv.DictReader(source)
                if str(row.get("Metric", "")).strip() == "Shared_Picking_Score"
            ),
            None,
        )
    if picking_weight is None:
        return

    metric_fields = [
        "Shared_Picking_Score_Raw",
        "Shared_Picking_Score_Normalized",
        "Shared_Picking_Score_Weighted",
    ]
    for method in METHODS:
        ranking_path = _source_path(8, method, "Weighted_Sum_Method_Ranking")
        if not ranking_path.exists():
            continue
        with ranking_path.open("r", newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            rows = list(reader)
        if not rows:
            continue

        scores: list[float] = []
        for row in rows:
            config_id = _HEURISTIC_CFG_PATTERN.sub(r"\1", str(row.get("Config_ID", "")))
            score_text = shared_scores.get((method, config_id), ("0", "", ""))[0]
            scores.append(common._to_float(score_text) or 0.0)
        maximum = max(scores, default=0.0)
        for field in metric_fields:
            if field not in fields:
                fields.append(field)
        for row, score in zip(rows, scores):
            normalized = score / maximum if maximum > 1e-12 else 1.0
            row.update(
                {
                    "Shared_Picking_Score_Raw": f"{score:.6f}",
                    "Shared_Picking_Score_Normalized": f"{normalized:.6f}",
                    "Shared_Picking_Score_Weighted": f"{normalized * picking_weight:.6f}",
                }
            )
        with ranking_path.open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def _normalize_combined_wsm_rows(
    rows: list[dict[str, str]],
    metric_specs: list[tuple[str, str, float]],
) -> list[dict[str, str]]:
    """Normalize and rank all WSM rows against the full combined candidate pool."""
    for metric, direction, _weight in metric_specs:
        raw_field = f"{metric}_Raw"
        values = [common._to_float(row.get(raw_field)) or 0.0 for row in rows]
        if not values:
            continue
        reference = max(values) if direction == "max" else min(values)
        for row, value in zip(rows, values):
            if abs(reference) <= 1e-12:
                normalized = 1.0 if abs(value) <= 1e-12 else 0.0
            else:
                normalized = value / reference if direction == "max" else reference / value
            contribution = normalized * _weight
            row[f"{metric}_Normalized"] = f"{normalized:.6f}"
            row[f"{metric}_Weighted"] = f"{contribution:.6f}"

    for row in rows:
        row["Normalization_Scope"] = "All_Combined_Heuristics"
        row["Weighted_Sum_Score"] = f"{sum(float(row.get(f'{metric}_Weighted', 0.0) or 0.0) for metric, _direction, _weight in metric_specs):.6f}"
    rows.sort(key=lambda row: (-float(row["Weighted_Sum_Score"]), row.get("Heuristic", ""), row.get("Config_ID", "")))
    for rank, row in enumerate(rows, start=1):
        row["Weighted_Sum_Rank"] = str(rank)
    return rows


def _normalize_combined_wsm(path: Path) -> None:
    """Normalize and rerank a combined WSM file using the configured metric weights."""
    if not path.exists() or not WSM_WEIGHTS_FILE.exists():
        return
    with WSM_WEIGHTS_FILE.open("r", newline="", encoding="utf-8-sig") as handle:
        weights = [
            (str(row.get("Metric", "")).strip(), str(row.get("Direction", "")).strip().lower(), common._to_float(row.get("Weight")))
            for row in csv.DictReader(handle)
        ]
    metric_specs = [
        (metric, direction, weight)
        for metric, direction, weight in weights
        if metric and direction in {"min", "max"} and weight is not None
    ]
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not rows or not metric_specs:
        return
    normalized_rows = _normalize_combined_wsm_rows(rows, metric_specs)
    if "Normalization_Scope" not in fieldnames:
        fieldnames.append("Normalization_Scope")
    try:
        with path.open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(normalized_rows)
    except PermissionError:
        print(f"[Combine] could not normalize {path}; close it if it is open elsewhere.")


def _write_dimensions() -> None:
    """Write Dim_Heuristic and Dim_Config, the lookup tables the combined files relate to in Power BI."""
    COMBINED_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with (COMBINED_ROOT / "Dim_Heuristic.csv").open("w", newline="", encoding="utf-8") as target:
            writer = csv.writer(target)
            writer.writerow(["Heuristic", "Heuristic_Name", "Description"])
            for code in METHODS:
                writer.writerow([code, *HEURISTIC_DESCRIPTIONS[code]])

        if not CANDIDATE_CONFIG_FILE.exists():
            return
        with CANDIDATE_CONFIG_FILE.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        fields = [
            "Config_ID",
            "Clustering_Method",
            "Scenario",
            "K",
            "Unique_Slot_Size_Count",
            "Slot_Sizes",
            "Slot_Size_Count",
            "Relative_Slot_Size_Distribution",
            "Source_Sample",
        ]
        with (COMBINED_ROOT / "Dim_Config.csv").open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                slot_sizes = _clean_cell(row.get("Slot_Sizes", ""))
                writer.writerow(
                    {
                        "Config_ID": row.get("Config_ID", ""),
                        "Clustering_Method": row.get("Clustering_Method", row.get("Clustering Method", row.get("Method", ""))),
                        "Scenario": row.get("Scenario", ""),
                        "K": row.get("K", ""),
                        "Unique_Slot_Size_Count": row.get(
                            "Unique_Slot_Size_Count",
                            str(len(set(part.strip() for part in slot_sizes.split(",") if part.strip()))),
                        ),
                        "Slot_Sizes": slot_sizes,
                        "Slot_Size_Count": len([part for part in slot_sizes.split(",") if part.strip()]),
                        "Relative_Slot_Size_Distribution": _clean_cell(
                            row.get("Relative_Slot_Size_Distribution", row.get("Relative Slot Size Distribution", ""))
                        ),
                        "Source_Sample": row.get("Source_Sample", row.get("Source Sample", "")),
                    }
                )
    except PermissionError:
        print("[Combine] could not write the dimension files; close them if they are open elsewhere.")


def combine_stage(stage: int) -> list[Path]:
    """Write one `<name>_All.csv` per output type, keyed by Heuristic and the unsuffixed Config_ID."""
    if stage == 8:
        _refresh_method_wsm_picking_scores()
    out_dir = COMBINED_ROOT / STAGE_PREFIX[stage]
    written: list[Path] = []
    shared_scores = _shared_picking_scores() if stage in {6, 8} else {}
    coverage_by_config = _picking_coverage_by_config() if stage == 6 else {}
    _write_dimensions()
    for stem in STAGE_FILES[stage]:
        sources = [
            (method, path)
            for method in METHODS
            if (path := _source_path(stage, method, stem)).exists()
        ]
        if not sources:
            continue

        fields: list[str] = []
        for _method, path in sources:
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                header = next(csv.reader(handle), [])
            header = [name for name in header if name not in LEGACY_LABEL_COLUMNS | {"Base_Config_ID"}]
            fields.extend(name for name in header if name not in fields)
        has_config = "Config_ID" in fields
        enrich_stage6 = stage == 6 and stem == "Candidate_Layout_Summary"
        if enrich_stage6:
            fields.extend(name for name in STAGE6_COMPARISON_FIELDS if name not in fields)
        add_shared = bool(shared_scores) and stem in RANKING_STEMS
        if add_shared:
            fields = [name for name in fields if name not in PE_ONLY_PICKING_COLUMNS]
        if has_config:
            fields = [name for name in fields if name != "Config_ID"]
            header_fields = ["Heuristic", "Config_ID", "Layout_Key", "Heuristic_Config_ID"] + fields
        else:
            header_fields = ["Heuristic"] + fields
        if add_shared:
            header_fields += list(SHARED_PICKING_COLUMNS)

        out_dir.mkdir(parents=True, exist_ok=True)
        target_path = out_dir / f"{stem}_All.csv"
        try:
            with target_path.open("w", newline="", encoding="utf-8") as target:
                writer = csv.DictWriter(target, fieldnames=header_fields, restval="", extrasaction="ignore")
                writer.writeheader()
                for method, path in sources:
                    with path.open("r", newline="", encoding="utf-8-sig") as handle:
                        for row in csv.DictReader(handle):
                            raw_config_id = str(row.get("Config_ID", ""))
                            row = {key: _clean_cell(value) for key, value in row.items() if value is not None}
                            row["Heuristic"] = method
                            if has_config:
                                row["Heuristic_Config_ID"] = raw_config_id
                                row["Layout_Key"] = f"{method}|{row.get('Config_ID', '')}"
                            if enrich_stage6:
                                _enrich_stage6_summary(
                                    row,
                                    method,
                                    row.get("Config_ID", ""),
                                    shared_scores,
                                    coverage_by_config,
                                )
                            if add_shared:
                                values = shared_scores.get((method, row.get("Config_ID", "")), ("", "", ""))
                                row.update(zip(SHARED_PICKING_COLUMNS, values))
                            writer.writerow(row)
        except PermissionError:
            print(f"[Combine] could not write {target_path}; close it if it is open elsewhere.")
            continue
        if stage == 8 and stem == "Weighted_Sum_Method_Ranking":
            _normalize_combined_wsm(target_path)
        written.append(target_path)
    print(f"[Combine] Stage {stage}: wrote {len(written)} combined files to {out_dir}")
    return written
