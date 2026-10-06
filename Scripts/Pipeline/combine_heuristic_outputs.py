"""Merge baseline and heuristic Stage 6-8 outputs into Power BI-ready combined CSV files."""

import csv
import re
from pathlib import Path

import run_ordered_pipeline as common

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
        "Candidate_Layout_Feasibility_Report",
        "Empty_Locations_By_Slot_Size",
        "Generated_Profiles_By_Config",
    ],
    7: [
        "Candidate_Layout_Robustness_Summary",
        "Candidate_Layout_Robustness_Details",
        "Non_Feasible_Layouts",
        "Non_Robust_Layouts",
    ],
    8: [
        "Candidate_Layout_Metric_Ranking",
        "Weighted_Sum_Method_Ranking",
        "Final_Layout_By_Location",
        "Final_Layout_By_Rack_Column",
        "Final_Layout_By_Rack",
        "Final_Layout_By_Segment",
    ],
}
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
CANDIDATE_CONFIG_FILE = common.OUTPUT_ROOT / "04_Candidate_Configuration" / "Candidate_Configurations.csv"
COMBINED_ROOT = common.OUTPUT_ROOT / "Combined_Outputs"


def _clean_cell(value: str) -> str:
    """Make a cell Power BI friendly: no Excel text wrapper, no heuristic CFG suffix, no padded decimals."""
    text = _HEURISTIC_CFG_PATTERN.sub(r"\1", common._decode_excel_text(value))
    match = _TRAILING_ZEROS_PATTERN.match(text)
    if match:
        decimals = match.group(2).rstrip("0")
        return f"{match.group(1)}.{decimals}" if decimals else match.group(1)
    return text


def _shared_picking_scores() -> dict[tuple[str, str], tuple[str, str, str]]:
    """Load the shared picking score of every (heuristic, Config_ID) from compare_picking_scores output."""
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


def _stage_dir(stage: int, method: str) -> Path:
    if method == "Baseline":
        return {6: common.STAGE6_OUTPUT_DIR, 7: common.STAGE7_OUTPUT_DIR, 8: common.STAGE8_OUTPUT_DIR}[stage]
    return common.OUTPUT_ROOT / f"{STAGE_PREFIX[stage]}_{VARIANT_NAMES[method]}"


def _source_path(stage: int, method: str, stem: str) -> Path:
    name = f"{stem}.csv" if method == "Baseline" else f"{stem}_{method}.csv"
    return _stage_dir(stage, method) / name


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
    out_dir = COMBINED_ROOT / STAGE_PREFIX[stage]
    written: list[Path] = []
    shared_scores = _shared_picking_scores() if stage == 8 else {}
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
                            if add_shared:
                                values = shared_scores.get((method, row.get("Config_ID", "")), ("", "", ""))
                                row.update(zip(SHARED_PICKING_COLUMNS, values))
                            writer.writerow(row)
        except PermissionError:
            print(f"[Combine] could not write {target_path}; close it if it is open elsewhere.")
            continue
        written.append(target_path)
    print(f"[Combine] Stage {stage}: wrote {len(written)} combined files to {out_dir}")
    return written
