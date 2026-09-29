from __future__ import annotations

import csv
import importlib.util
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = ROOT / "Output" / "Combined_Feasible_Layouts_K3_K4_Strict754"
EXPERIMENT_ROOT = ROOT / "Output" / "06_Layout_Generation_K3_K4_Experiment" / "strict_k3_k4_sweep"
EXPERIMENT_STAGE6 = EXPERIMENT_ROOT
EXPERIMENT_STAGE7 = EXPERIMENT_ROOT / "07_Robustness_Evaluation"
EXPERIMENT_STAGE8 = EXPERIMENT_ROOT / "08_Final_Selection_All_Stage6_Feasible"
EXISTING_STAGE7 = ROOT / "Output" / "07_Robustness_Evaluation"
EXISTING_STAGE8 = ROOT / "Output" / "08_Final_Selection"
CAPACITY_SUMMARY = ROOT / "Output" / "05_Capacity_Determination" / "Capacity_Determination_Summary.csv"
STAGE6_SCRIPT = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"
STAGE8_SCRIPT = ROOT / "Scripts" / "Pipeline" / "08_Final_Selection" / "08_final_selection.py"


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        return list(reader.fieldnames or []), list(reader)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _strict_experiment_file(directory: Path, filename: str) -> Path:
    path = Path(filename)
    return directory / f"{path.stem}_K3_K4{path.suffix}"


def _union_fields(*collections: list[dict[str, str]]) -> list[str]:
    fields: list[str] = []
    for rows in collections:
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
    return fields


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _count_distribution(
    rows: list[dict[str, str]],
    stage6,
    available_slot_sizes: list[int],
) -> tuple[dict[int, int], float, int]:
    slots_by_column: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if str(row.get("Usable_Location", "YES")).strip().upper() == "NO":
            continue
        try:
            value = float(row.get("Assigned_Slot_Size_cm", ""))
            column = f"{str(row.get('Rack', '')).strip()}{int(float(row.get('Column', '0'))):02d}"
        except (TypeError, ValueError):
            continue
        if value > 0:
            slots_by_column[column].append(value)

    effective_counts: Counter[int] = Counter()
    physical_height = 0.0
    for slots in slots_by_column.values():
        ordered = sorted(slots, reverse=True)
        physical_height += sum(ordered) + max(len(ordered) - 1, 0) * stage6.common.BEAM_HEIGHT
        for value in ordered:
            effective = stage6._effective_requirement_slot_size(value, ordered, available_slot_sizes)
            if effective is not None:
                effective_counts[int(effective)] += 1
    return dict(effective_counts), physical_height, len(slots_by_column)


def _distribution_signature(counts: dict[int, int]) -> str:
    return "|".join(f"{size}:{count}" for size, count in sorted(counts.items()))


def _cumulative_signature(counts: dict[int, int]) -> str:
    cumulative: dict[int, int] = {}
    running = 0
    for size in sorted(counts, reverse=True):
        running += counts[size]
        cumulative[size] = running
    return "|".join(f"{size}:{cumulative[size]}" for size in sorted(cumulative))


def _signature_counts(value: str) -> dict[int, int]:
    counts: dict[int, int] = {}
    for token in str(value).split("|"):
        if ":" not in token:
            continue
        size_text, count_text = token.split(":", 1)
        try:
            counts[int(float(size_text))] = int(float(count_text))
        except ValueError:
            continue
    return counts


def _reconstruct_existing_summary(
    stage6,
    existing_rank_rows: list[dict[str, str]],
    existing_robustness_rows: list[dict[str, str]],
    locations_by_config: dict[str, list[dict[str, str]]],
    capacity_by_config: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    robustness_by_id = {row.get("Config_ID", ""): row for row in existing_robustness_rows}
    reconstructed: list[dict[str, str]] = []
    allowed_height = float(stage6.common.MAX_USED_HEIGHT_BASE)

    for rank_row in existing_rank_rows:
        config_id = str(rank_row.get("Config_ID", "")).strip()
        if not config_id:
            continue
        locations = locations_by_config.get(config_id, [])
        robustness = robustness_by_id.get(config_id, {})
        minimum = _signature_counts(capacity_by_config.get(config_id, {}).get("Exact_Count_Distribution", ""))
        distribution, physical_height, column_count = _count_distribution(locations, stage6, sorted(minimum))
        additional = {
            size: max(count - minimum.get(size, 0), 0)
            for size, count in distribution.items()
            if count > minimum.get(size, 0)
        }
        total_allowed_height = column_count * allowed_height
        layout_id = f"LAY_{config_id}"
        row = dict(rank_row)
        row.update(robustness)
        row.update(
            {
                "Layout_ID": layout_id,
                "Config_ID": config_id,
                "Layout_Source": "Existing_Stage8_Final_Export",
                "Runtime_Seconds": "",
                "Runtime_Minutes": "",
                "Profile_Generation_Seconds": "",
                "Profile_Shortlist_Seconds": "",
                "Rack_Search_Seconds": "",
                "Profile_Generation_Timeout": "UNKNOWN",
                "Rack_Search_Timeout": "UNKNOWN",
                "Layout_Feasible": "YES",
                "Allocation_Feasible_Initial": "YES",
                "Required_Locations_Total": str(robustness.get("Required_Locations_Total", "")),
                "Total_Locations": str(robustness.get("Assigned_Locations_Total", rank_row.get("Assigned_Locations_Total", ""))),
                "Assigned_Used_Height_Total": f"{physical_height:.3f}",
                "Total_Allowed_Height": f"{total_allowed_height:.3f}",
                "Space_Left": str(robustness.get("Space_Left", f"{max(total_allowed_height - physical_height, 0.0):.3f}")),
                "Beam_Relocations_Total": str(robustness.get("Beam_Relocations_Total", rank_row.get("Beam_Relocations_Total", "0"))),
                "Initial_Beams_Total": "",
                "Required_Beams_Total": str(robustness.get("Required_Beams_Total", rank_row.get("Required_Beams_Total", ""))),
                "Additional_Beams_Required": str(robustness.get("Additional_Beams_Required", rank_row.get("Additional_Beams_Required", "0"))),
                "Initial_Grids_Total": "",
                "Required_Grids_Total": str(robustness.get("Required_Grids_Total", rank_row.get("Required_Grids_Total", ""))),
                "Additional_Grids_Required": str(robustness.get("Additional_Grids_Required", rank_row.get("Additional_Grids_Required", "0"))),
                "Percentage_Rack_Height_Used": str(rank_row.get("Space_Utilization_Pct", "")),
                "Minimum_Required_Counts": _distribution_signature(minimum),
                "Additional_Fill_Counts": _distribution_signature(additional),
                "Layout_Slot_Size_Distribution": _distribution_signature(distribution),
                "Layout_Slot_Size_Cumulative_Coverage": _cumulative_signature(distribution),
                "Source_Slot_Sizes": str(rank_row.get("Source_Slot_Sizes", "")),
                "Layout_Usable_Alignment_Conversions": "",
                "Feasible_Profiles_Considered_Total": "",
                "Feasible_Profiles_Used_Total": str(len({row.get("Rack_Profile_Signature", "") for row in []})),
            }
        )
        row["Capacity_Margin"] = str(robustness.get("Capacity_Margin", rank_row.get("Capacity_Margin", "")))
        reconstructed.append(row)
    return reconstructed


def _prepare_existing_layout_rows(
    path: Path,
    config_ids: set[str],
    layout_id_by_config: dict[str, str],
) -> list[dict[str, str]]:
    _fields, rows = _read_csv(path)
    selected = [dict(row) for row in rows if str(row.get("Config_ID", "")).strip() in config_ids]
    for row in selected:
        config_id = str(row.get("Config_ID", "")).strip()
        if "Layout_ID" in row:
            row["Layout_ID"] = layout_id_by_config[config_id]
    return selected


def _reconstruct_existing_column_rows(
    stage6,
    old_column_rows: list[dict[str, str]],
    locations_by_config: dict[str, list[dict[str, str]]],
    layout_id_by_config: dict[str, str],
) -> list[dict[str, str]]:
    rows_by_key = {
        (row.get("Config_ID", ""), row.get("Rack_Column", "")): dict(row)
        for row in old_column_rows
    }
    slots_by_key: dict[tuple[str, str], list[float]] = defaultdict(list)
    for config_id, location_rows in locations_by_config.items():
        for row in location_rows:
            try:
                key = (config_id, f"{str(row.get('Rack', '')).strip()}{int(float(row.get('Column', '0'))):02d}")
                slots_by_key[key].append(float(row.get("Assigned_Slot_Size_cm", "")))
            except (TypeError, ValueError):
                continue

    output: list[dict[str, str]] = []
    for (config_id, rack_column), slots in slots_by_key.items():
        row = rows_by_key.get((config_id, rack_column), {}).copy()
        physical = sum(slots) + max(len(slots) - 1, 0) * stage6.common.BEAM_HEIGHT
        row.update(
            {
                "Config_ID": config_id,
                "Rack_Column": rack_column,
                "Beam_Count_Used": str(max(len(slots) - 1, 0)),
                "Allowed_Used_Height_cm": f"{stage6.common.MAX_USED_HEIGHT_BASE:.3f}",
                "Assigned_Used_Height_cm": f"{physical:.3f}",
                "Remaining_Height_cm": f"{max(stage6.common.MAX_USED_HEIGHT_BASE - physical, 0.0):.3f}",
                "Fill_Ratio": f"{physical / stage6.common.MAX_USED_HEIGHT_BASE:.4f}",
                "Layout_ID": layout_id_by_config[config_id],
            }
        )
        output.append(row)
    return output


def _recalculate_column_distributions(
    stage6,
    column_rows: list[dict[str, str]],
    locations_by_config: dict[str, list[dict[str, str]]],
    config_sizes_by_id: dict[str, list[float]],
) -> list[dict[str, str]]:
    slots_by_column: dict[tuple[str, str], list[float]] = defaultdict(list)
    for config_id, location_rows in locations_by_config.items():
        for row in location_rows:
            try:
                rack_column = f"{str(row.get('Rack', '')).strip()}{int(float(row.get('Column', '0'))):02d}"
                slot_size = float(row.get("Assigned_Slot_Size_cm", ""))
            except (TypeError, ValueError):
                continue
            if str(row.get("Usable_Location", "YES")).strip().upper() != "NO" and slot_size > 0.0:
                slots_by_column[(config_id, rack_column)].append(slot_size)

    corrected_rows: list[dict[str, str]] = []
    for original in column_rows:
        row = dict(original)
        config_id = str(row.get("Config_ID", "")).strip()
        rack_column = str(row.get("Rack_Column", "")).strip()
        slots = slots_by_column.get((config_id, rack_column), [])
        effective_counts = stage6._effective_requirement_counts(
            slots,
            config_sizes_by_id.get(config_id, []),
        )
        row["Slot_Size_Distribution"] = _distribution_signature(
            {int(size): int(count) for size, count in effective_counts.items() if int(count) > 0}
        )
        corrected_rows.append(row)
    return corrected_rows


def main() -> int:
    stage6 = _load_module("stage6_merge_k3k4", STAGE6_SCRIPT)
    exp_summary_fields, exp_summary_all = _read_csv(_strict_experiment_file(EXPERIMENT_STAGE6, "Candidate_Layout_Summary.csv"))
    exp_summary = [row for row in exp_summary_all if str(row.get("Layout_Feasible", "")).strip().upper() == "YES"]
    exp_ids = {str(row.get("Config_ID", "")).strip() for row in exp_summary}

    old_rank_fields, old_rank_rows = _read_csv(EXISTING_STAGE8 / "Candidate_Layout_Metric_Ranking.csv")
    old_robust_fields, old_robust_rows = _read_csv(EXISTING_STAGE7 / "Candidate_Layout_Robustness_Summary.csv")
    exp_robust_fields, exp_robust_all = _read_csv(_strict_experiment_file(EXPERIMENT_STAGE7, "Candidate_Layout_Robustness_Summary.csv"))
    exp_robust_rows = [row for row in exp_robust_all if row.get("Config_ID", "") in exp_ids]

    old_ids = {str(row.get("Config_ID", "")).strip() for row in old_rank_rows if row.get("Config_ID")}
    if old_ids & exp_ids:
        raise ValueError(f"Duplicate config IDs across source runs: {sorted(old_ids & exp_ids)}")
    if len(old_ids) != 27 or len(exp_ids) != 30:
        raise ValueError(f"Expected 27 retained configs and 30 strict experiment configs; got {len(old_ids)} and {len(exp_ids)}")

    old_locations = _prepare_existing_layout_rows(
        EXISTING_STAGE8 / "Final_Layout_By_Location.csv",
        old_ids,
        {config_id: f"LAY_{config_id}" for config_id in old_ids},
    )
    old_locations_by_config: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in old_locations:
        old_locations_by_config[str(row.get("Config_ID", ""))].append(row)

    capacity_fields, capacity_rows = _read_csv(CAPACITY_SUMMARY)
    capacity_by_config = {row.get("Config_ID", ""): row for row in capacity_rows}
    existing_summary = _reconstruct_existing_summary(
        stage6,
        old_rank_rows,
        old_robust_rows,
        old_locations_by_config,
        capacity_by_config,
    )
    for row in existing_summary:
        config_id = str(row.get("Config_ID", ""))
        rack_rows = _prepare_existing_layout_rows(
            EXISTING_STAGE8 / "Final_Layout_By_Rack.csv",
            {config_id},
            {config_id: f"LAY_{config_id}"},
        )
        row["Feasible_Profiles_Used_Total"] = str(len({r.get("Rack_Profile_Signature", "") for r in rack_rows if r.get("Rack_Profile_Signature")}))

    old_layout_id_by_config = {config_id: f"LAY_{config_id}" for config_id in old_ids}
    existing_column_source = _read_csv(EXISTING_STAGE8 / "Final_Layout_By_Rack_Column.csv")[1]
    existing_column_rows = _reconstruct_existing_column_rows(
        stage6,
        existing_column_source,
        old_locations_by_config,
        old_layout_id_by_config,
    )

    experiment_location_rows = _read_csv(_strict_experiment_file(EXPERIMENT_STAGE6, "Candidate_Layout_By_Location.csv"))[1]
    experiment_location_rows = [row for row in experiment_location_rows if row.get("Config_ID", "") in exp_ids]
    experiment_column_rows = [
        row for row in _read_csv(_strict_experiment_file(EXPERIMENT_STAGE6, "Candidate_Layout_By_Rack_Column.csv"))[1]
        if row.get("Config_ID", "") in exp_ids
    ]

    all_summary = existing_summary + [
        {**row, "Layout_Source": "K3_K4_Stage6_Experiment"}
        for row in exp_summary
    ]
    all_ids = old_ids | exp_ids
    if len(all_ids) != 57:
        raise ValueError(f"Expected 57 unique feasible layouts; got {len(all_ids)}")

    out6 = OUTPUT_ROOT / "06_Layout_Generation"
    out7 = OUTPUT_ROOT / "07_Robustness_Evaluation"
    out8 = OUTPUT_ROOT / "08_Final_Selection"
    for folder in (out6, out7, out8):
        folder.mkdir(parents=True, exist_ok=True)

    summary_fields = _union_fields([dict.fromkeys(exp_summary_fields, "")] + all_summary)
    _write_csv(out6 / "Candidate_Layout_Summary.csv", summary_fields, all_summary)

    location_fields, _ = _read_csv(_strict_experiment_file(EXPERIMENT_STAGE6, "Candidate_Layout_By_Location.csv"))
    _write_csv(out6 / "Candidate_Layout_By_Location.csv", location_fields, old_locations + experiment_location_rows)

    config_sizes_by_id = {
        config_id: sorted(float(size) for size in _signature_counts(capacity_by_config.get(config_id, {}).get("Exact_Count_Distribution", "")))
        for config_id in all_ids
    }
    all_locations_by_config: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in old_locations + experiment_location_rows:
        all_locations_by_config[str(row.get("Config_ID", ""))].append(row)

    all_rack_rows: list[dict[str, str]] = []
    for config_id in sorted(all_ids):
        all_rack_rows.extend(
            stage6._rack_profile_rows_from_location_rows(
                f"LAY_{config_id}",
                config_id,
                all_locations_by_config.get(config_id, []),
                config_sizes_by_id.get(config_id, []),
            )
        )
    rack_fields = _read_csv(_strict_experiment_file(EXPERIMENT_STAGE6, "Candidate_Layout_By_Rack.csv"))[0]
    _write_csv(out6 / "Candidate_Layout_By_Rack.csv", rack_fields, all_rack_rows)

    column_fields, _ = _read_csv(_strict_experiment_file(EXPERIMENT_STAGE6, "Candidate_Layout_By_Rack_Column.csv"))
    all_column_rows = _recalculate_column_distributions(
        stage6,
        existing_column_rows + experiment_column_rows,
        all_locations_by_config,
        config_sizes_by_id,
    )
    _write_csv(out6 / "Candidate_Layout_By_Rack_Column.csv", column_fields, all_column_rows)

    robustness_rows = old_robust_rows + exp_robust_rows
    robustness_fields = _union_fields([dict.fromkeys(old_robust_fields, "")] + robustness_rows)
    _write_csv(out7 / "Candidate_Layout_Robustness_Summary.csv", robustness_fields, robustness_rows)

    manifest_rows = []
    for row in existing_summary:
        manifest_rows.append({"Config_ID": row["Config_ID"], "Layout_Source": row["Layout_Source"], "Layout_Feasible": "YES"})
    for row in exp_summary:
        manifest_rows.append({"Config_ID": row["Config_ID"], "Layout_Source": "K3_K4_Stage6_Experiment", "Layout_Feasible": "YES"})
    _write_csv(OUTPUT_ROOT / "Config_Source_Manifest.csv", ["Config_ID", "Layout_Source", "Layout_Feasible"], sorted(manifest_rows, key=lambda row: row["Config_ID"]))

    stage8 = _load_module("stage8_merge_k3k4", STAGE8_SCRIPT)
    stage8.common.STAGE8_OUTPUT_DIR = out8
    stage8.ROBUSTNESS_SUMMARY_FILE = out7 / "Candidate_Layout_Robustness_Summary.csv"
    stage8.LAYOUT_SUMMARY_FILE = out6 / "Candidate_Layout_Summary.csv"
    stage8.LAYOUT_BY_COLUMN_FILE = out6 / "Candidate_Layout_By_Rack_Column.csv"
    stage8.LAYOUT_BY_LOCATION_FILE = out6 / "Candidate_Layout_By_Location.csv"
    stage8.LAYOUT_BY_RACK_FILE = out6 / "Candidate_Layout_By_Rack.csv"
    stage8.OUTPUT_FILE = out8 / "Candidate_Layout_Metric_Ranking.csv"
    stage8.WSM_OUTPUT_FILE = out8 / "Weighted_Sum_Method_Ranking.csv"
    stage8.FINAL_LAYOUT_BY_COLUMN_FILE = out8 / "Final_Layout_By_Rack_Column.csv"
    stage8.FINAL_LAYOUT_BY_LOCATION_FILE = out8 / "Final_Layout_By_Location.csv"
    stage8.FINAL_LAYOUT_BY_SEGMENT_FILE = out8 / "Final_Layout_By_Segment.csv"
    stage8.FINAL_LAYOUT_BY_RACK_FILE = out8 / "Final_Layout_By_Rack.csv"
    stage8.LEGACY_OUTPUT_FILES = [out8 / "Objective_Layout_Recommendations.csv", out8 / "Management_Decision_Table.csv"]
    stage8._is_robustness_passing = lambda row: str(row.get("Layout_Feasible", "")).strip().upper() == "YES"
    ranked_rows = stage8.build_final_selection()

    print(f"Combined feasible Stage 6 layouts: {len(all_summary)} (27 retained + 30 strict-754 experimental).")
    print(f"Stage 8 ranking rows: {len(ranked_rows)}.")
    print(f"Combined output root: {OUTPUT_ROOT}")
    return 0 if len(ranked_rows) == 57 else 1


if __name__ == "__main__":
    raise SystemExit(main())