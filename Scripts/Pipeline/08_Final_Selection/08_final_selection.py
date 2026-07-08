import csv
from collections import defaultdict
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


ROBUSTNESS_SUMMARY_FILE = common.STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Summary.csv"
LAYOUT_SUMMARY_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_Summary_TopFilled.csv"
LAYOUT_BY_COLUMN_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Rack_Column_TopFilled.csv"
LAYOUT_BY_LOCATION_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Location_TopFilled.csv"
OUTPUT_FILE = common.STAGE8_OUTPUT_DIR / "Objective_Layout_Recommendations.csv"
MANAGEMENT_DECISION_FILE = common.STAGE8_OUTPUT_DIR / "Management_Decision_Table.csv"
FINAL_LAYOUT_BY_COLUMN_FILE = common.STAGE8_OUTPUT_DIR / "Final_Layout_By_Rack_Column.csv"
FINAL_LAYOUT_BY_LOCATION_FILE = common.STAGE8_OUTPUT_DIR / "Final_Layout_By_Location.csv"

TOP_PER_OBJECTIVE = 3


def _read_csv(path: Path) -> list[dict[str, str]]:
    # Keep CSV read behavior consistent with shared pipeline helpers.
    return common._read_csv(path)


def _layout_map() -> dict[str, dict[str, str]]:
    """Load Stage 6 layout summary rows keyed by layout ID."""
    layouts: dict[str, dict[str, str]] = {}
    if LAYOUT_SUMMARY_FILE.exists():
        for row in _read_csv(LAYOUT_SUMMARY_FILE):
            layouts[str(row.get("Layout_ID", "")).strip()] = row
    return layouts


def _robustness_rows() -> list[dict[str, str]]:
    """Load Stage 7 robustness summary rows."""
    if not ROBUSTNESS_SUMMARY_FILE.exists():
        raise FileNotFoundError(f"Missing robustness summary file: {ROBUSTNESS_SUMMARY_FILE}")
    return _read_csv(ROBUSTNESS_SUMMARY_FILE)


def _final_column_fieldnames(rows: list[dict[str, str]]) -> list[str]:
    base = [
        "Layout_ID",
        "Config_ID",
        "Rack_Column",
        "Beam_Count_Used",
        "Allowed_Used_Height_cm",
        "Assigned_Used_Height_cm",
        "Remaining_Height_cm",
        "Fill_Ratio",
        "Beam_Relocations_In_Column",
        "Removed_Beams_In_Column",
        "Added_Beams_In_Column",
        "Slot_Size_Distribution",
    ]
    topfill = [
        "TopFill_Adjusted_Row",
        "TopFill_Original_Top_Slot_cm",
        "TopFill_Added_Height_cm",
        "TopFill_Adjusted_Top_Slot_cm",
    ]
    has_topfill = any(
        any(str(row.get(field, "")).strip() for field in topfill)
        for row in rows
    )
    if has_topfill:
        return [field for field in base if field != "Slot_Size_Distribution"] + topfill + ["Slot_Size_Distribution"]
    return base


def _count_unique_slot_sizes(layout_row: dict[str, str]) -> int:
    # Standardization proxy: fewer unique slot sizes means higher standardization.
    source = str(layout_row.get("Source_Slot_Sizes", "")).strip()
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


def _to_recommendation_view(row: dict[str, str]) -> dict[str, str]:
    space_utilization = common._to_float(row.get("Space_Utilization"))
    if space_utilization is None:
        space_utilization = common._to_float(row.get("Mean_Utilization_Rate")) or 0.0

    worst_case_capacity_margin = common._to_int_default(
        row.get("Worst_Case_Capacity_Margin") or row.get("Worst_Capacity_Margin"),
        0,
    )

    return {
        "Layout_ID": str(row.get("Layout_ID", "")),
        "Config_ID": str(row.get("Config_ID", "")),
        "Space_Utilization": f"{space_utilization:.6f}",
        "Mean_Occupancy_Rate": f"{common._to_float(row.get('Mean_Occupancy_Rate')) or 0.0:.6f}",
        "Worst_Occupancy_Rate": f"{common._to_float(row.get('Worst_Occupancy_Rate')) or 0.0:.6f}",
        "Robustness": f"{common._to_float(row.get('Robustness')) or 0.0:.6f}",
        "Minimum_Normalized_Slack": f"{common._to_float(row.get('Minimum_Normalized_Slack')) or 0.0:.6f}",
        "Worst_Case_Capacity_Margin": str(worst_case_capacity_margin),
        "Minimum_Capacity_Ratio": f"{common._to_float(row.get('Minimum_Capacity_Ratio')) or 0.0:.6f}",
        "Beam_Relocations_Total": str(common._to_int_default(row.get("Beam_Relocations_Total"), 0)),
        "Initial_Beams_Total": str(common._to_int_default(row.get("Initial_Beams_Total"), 0)),
        "Required_Beams_Total": str(common._to_int_default(row.get("Required_Beams_Total"), 0)),
        "Additional_Beams_Required": str(common._to_int_default(row.get("Additional_Beams_Required"), 0)),
        "Initial_Grids_Total": str(common._to_int_default(row.get("Initial_Grids_Total"), 0)),
        "Required_Grids_Total": str(common._to_int_default(row.get("Required_Grids_Total"), 0)),
        "Additional_Grids_Required": str(common._to_int_default(row.get("Additional_Grids_Required"), 0)),
        "Unique_Slot_Sizes_Count": str(common._to_int_default(row.get("Unique_Slot_Sizes_Count"), 0)),
        "Assigned_Locations_Total": str(
            common._to_int_default(row.get("Assigned_Locations_Total") or row.get("Total_Locations"), 0)
        ),
        "Space_Left": f"{common._to_float(row.get('Space_Left')) or 0.0:.3f}",
    }


def _normalize(values: list[float], higher_is_better: bool) -> list[float]:
    if not values:
        return []
    min_value = min(values)
    max_value = max(values)
    if abs(max_value - min_value) <= 1e-12:
        return [1.0 for _ in values]
    if higher_is_better:
        return [(value - min_value) / (max_value - min_value) for value in values]
    return [(max_value - value) / (max_value - min_value) for value in values]


def build_final_selection() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Build objective-specific recommendations instead of one strict overall winner."""
    robustness_rows = _robustness_rows()
    layouts = _layout_map()

    recommendation_rows: list[dict[str, str]] = []
    management_rows: list[dict[str, str]] = []
    joined_rows: list[dict[str, str]] = []
    # Join Stage 6 layout metadata with Stage 7 robustness metrics.
    for row in robustness_rows:
        layout_id = str(row.get("Layout_ID", "")).strip()
        if not layout_id:
            continue
        layout = layouts.get(layout_id, {})
        merged = dict(layout)
        merged.update(row)
        merged["Unique_Slot_Sizes_Count"] = str(_count_unique_slot_sizes(layout))
        merged["Implementation_Effort_Total"] = str(
            common._to_int_default(merged.get("Beam_Relocations_Total"), 0)
            + common._to_int_default(merged.get("Additional_Beams_Required"), 0)
            + common._to_int_default(merged.get("Additional_Grids_Required"), 0)
        )
        joined_rows.append(merged)

    eligible_rows = [
        row
        for row in joined_rows
        if str(row.get("Layout_Feasible", "")).strip().upper() == "YES"
    ]

    def _top_rows(
        rows: list[dict[str, str]],
        sort_key,
        objective_name: str,
        rationale: str,
    ) -> list[dict[str, str]]:
        chosen = sorted(rows, key=sort_key)[:TOP_PER_OBJECTIVE]
        formatted: list[dict[str, str]] = []
        for idx, row in enumerate(chosen, start=1):
            view = _to_recommendation_view(row)
            view.update(
                {
                    "Objective": objective_name,
                    "Objective_Rank": str(idx),
                    "Selection_Rationale": rationale,
                }
            )
            formatted.append(view)
        return formatted

    util_rows = _top_rows(
        eligible_rows,
        lambda row: (
            -(common._to_float(row.get("Space_Utilization")) or 0.0),
            common._to_int_default(row.get("Beam_Relocations_Total"), 0),
            common._to_float(row.get("Space_Left")) or 0.0,
        ),
        "A_Maximum_Space_Utilization",
        "Highest vertical-space utilization with feasibility preserved",
    )

    effort_rows = _top_rows(
        eligible_rows,
        lambda row: (
            common._to_int_default(row.get("Beam_Relocations_Total"), 0),
            common._to_int_default(row.get("Additional_Beams_Required"), 0),
            common._to_int_default(row.get("Additional_Grids_Required"), 0),
            -(common._to_float(row.get("Space_Utilization")) or 0.0),
        ),
        "B_Minimum_Implementation_Effort",
        "Lowest relocation and material-change burden",
    )

    standardization_rows = _top_rows(
        eligible_rows,
        lambda row: (
            common._to_int_default(row.get("Unique_Slot_Sizes_Count"), 0),
            common._to_int_default(row.get("Beam_Relocations_Total"), 0),
            -(common._to_float(row.get("Space_Utilization")) or 0.0),
        ),
        "C_Maximum_Standardization",
        "Fewest unique slot sizes while maintaining feasible performance",
    )

    robust_rows = _top_rows(
        eligible_rows,
        lambda row: (
            -(common._to_float(row.get("Minimum_Normalized_Slack")) or 0.0),
            -common._to_int_default(row.get("Worst_Case_Capacity_Margin") or row.get("Worst_Capacity_Margin"), 0),
            -(common._to_float(row.get("Minimum_Capacity_Ratio")) or 0.0),
            common._to_int_default(row.get("Beam_Relocations_Total"), 0),
        ),
        "D_Robust_Layout",
        "Strongest worst-case capacity buffer under SKU-count scenarios",
    )

    if eligible_rows:
        util_values = [common._to_float(row.get("Space_Utilization")) or 0.0 for row in eligible_rows]
        effort_values = [common._to_int_default(row.get("Implementation_Effort_Total"), 0) for row in eligible_rows]
        robust_values = [common._to_float(row.get("Minimum_Normalized_Slack")) or 0.0 for row in eligible_rows]
        std_values = [common._to_int_default(row.get("Unique_Slot_Sizes_Count"), 0) for row in eligible_rows]

        util_norm = _normalize(util_values, higher_is_better=True)
        effort_norm = _normalize([float(value) for value in effort_values], higher_is_better=False)
        robust_norm = _normalize(robust_values, higher_is_better=True)
        std_norm = _normalize([float(value) for value in std_values], higher_is_better=False)

        for idx, row in enumerate(eligible_rows):
            balanced_score = (util_norm[idx] + effort_norm[idx] + robust_norm[idx] + std_norm[idx]) / 4.0
            row["Balanced_Score"] = f"{balanced_score:.6f}"

    balanced_rows = _top_rows(
        eligible_rows,
        lambda row: (
            -(common._to_float(row.get("Balanced_Score")) or 0.0),
            -(common._to_float(row.get("Space_Utilization")) or 0.0),
            common._to_int_default(row.get("Implementation_Effort_Total"), 0),
        ),
        "E_Best_Balanced_Alternative",
        "Best compromise across utilization, implementation effort, robustness, and standardization",
    )

    recommendation_rows.extend(util_rows)
    recommendation_rows.extend(effort_rows)
    recommendation_rows.extend(standardization_rows)
    recommendation_rows.extend(robust_rows)
    recommendation_rows.extend(balanced_rows)

    objective_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in recommendation_rows:
        objective_groups[str(row.get("Objective", ""))].append(row)

    for objective, rows in objective_groups.items():
        layout_labels = [
            f"{row.get('Layout_ID', '')} ({row.get('Config_ID', '')})"
            for row in rows
        ]
        key_advantages = []
        key_disadvantages = []
        if objective.startswith("A_"):
            key_advantages.append("Highest space utilization")
            key_disadvantages.append("Can require higher implementation effort")
        elif objective.startswith("B_"):
            key_advantages.append("Lowest relocation and added-material burden")
            key_disadvantages.append("May sacrifice capacity efficiency")
        elif objective.startswith("C_"):
            key_advantages.append("Higher operational standardization")
            key_disadvantages.append("May not maximize utilization or robustness buffer")
        elif objective.startswith("D_"):
            key_advantages.append("Strongest worst-case capacity safety margin")
            key_disadvantages.append("May not minimize implementation effort")
        else:
            key_advantages.append("Balanced trade-off across major objectives")
            key_disadvantages.append("Not best on any single KPI extreme")

        relevant_kpis = []
        for row in rows:
            relevant_kpis.append(
                " | ".join(
                    [
                        f"{row.get('Layout_ID', '')}",
                        f"SU={row.get('Space_Utilization', '')}",
                        f"OccM={row.get('Mean_Occupancy_Rate', '')}",
                        f"Rob={row.get('Robustness', '')}",
                        f"Slack={row.get('Minimum_Normalized_Slack', '')}",
                        f"WCM={row.get('Worst_Case_Capacity_Margin', '')}",
                        f"Reloc={row.get('Beam_Relocations_Total', '')}",
                        f"InitBeams={row.get('Initial_Beams_Total', '')}",
                        f"ReqBeams={row.get('Required_Beams_Total', '')}",
                        f"Beams={row.get('Additional_Beams_Required', '')}",
                        f"InitGrids={row.get('Initial_Grids_Total', '')}",
                        f"ReqGrids={row.get('Required_Grids_Total', '')}",
                        f"Grids={row.get('Additional_Grids_Required', '')}",
                        f"Std={row.get('Unique_Slot_Sizes_Count', '')}",
                        f"Assign={row.get('Assigned_Locations_Total', '')}",
                        f"SpaceLeft={row.get('Space_Left', '')}",
                    ]
                )
            )

        lead = rows[0] if rows else {}

        management_rows.append(
            {
                "Objective": objective,
                "Recommended_Layout_ID": str(lead.get("Layout_ID", "")),
                "Recommended_Config_ID": str(lead.get("Config_ID", "")),
                "Recommended_Assigned_Locations_Total": str(lead.get("Assigned_Locations_Total", "")),
                "Initial_Beams_Total": str(lead.get("Initial_Beams_Total", "")),
                "Required_Beams_Total": str(lead.get("Required_Beams_Total", "")),
                "Additional_Beams_Required": str(lead.get("Additional_Beams_Required", "")),
                "Initial_Grids_Total": str(lead.get("Initial_Grids_Total", "")),
                "Required_Grids_Total": str(lead.get("Required_Grids_Total", "")),
                "Additional_Grids_Required": str(lead.get("Additional_Grids_Required", "")),
                "Recommended_Layouts": "; ".join(layout_labels),
                "Key_Advantages": "; ".join(key_advantages),
                "Key_Disadvantages": "; ".join(key_disadvantages),
                "Relevant_KPI_Values": " || ".join(relevant_kpis),
            }
        )

    # Write objective-specific recommendation table.
    common._write_csv_clean(
        OUTPUT_FILE,
        [
            "Objective",
            "Objective_Rank",
            "Layout_ID",
            "Config_ID",
            "Selection_Rationale",
            "Space_Utilization",
            "Mean_Occupancy_Rate",
            "Worst_Occupancy_Rate",
            "Robustness",
            "Minimum_Normalized_Slack",
            "Worst_Case_Capacity_Margin",
            "Minimum_Capacity_Ratio",
            "Beam_Relocations_Total",
            "Initial_Beams_Total",
            "Required_Beams_Total",
            "Additional_Beams_Required",
            "Initial_Grids_Total",
            "Required_Grids_Total",
            "Additional_Grids_Required",
            "Unique_Slot_Sizes_Count",
            "Assigned_Locations_Total",
            "Space_Left",
        ],
        recommendation_rows,
    )

    common._write_csv_clean(
        MANAGEMENT_DECISION_FILE,
        [
            "Objective",
            "Recommended_Layout_ID",
            "Recommended_Config_ID",
            "Recommended_Assigned_Locations_Total",
            "Initial_Beams_Total",
            "Required_Beams_Total",
            "Additional_Beams_Required",
            "Initial_Grids_Total",
            "Required_Grids_Total",
            "Additional_Grids_Required",
            "Recommended_Layouts",
            "Key_Advantages",
            "Key_Disadvantages",
            "Relevant_KPI_Values",
        ],
        management_rows,
    )

    recommended_ids = {
        str(row.get("Layout_ID", "")).strip()
        for row in recommendation_rows
    }

    finalist_column_rows: list[dict[str, str]] = []
    # Export details for all recommended layouts across objectives.
    if LAYOUT_BY_COLUMN_FILE.exists():
        for row in _read_csv(LAYOUT_BY_COLUMN_FILE):
            if str(row.get("Layout_ID", "")).strip() in recommended_ids:
                finalist_column_rows.append(row)

    finalist_location_rows: list[dict[str, str]] = []
    # Export location assignments for all recommended layouts.
    if LAYOUT_BY_LOCATION_FILE.exists():
        for row in _read_csv(LAYOUT_BY_LOCATION_FILE):
            if str(row.get("Layout_ID", "")).strip() in recommended_ids:
                finalist_location_rows.append(row)

    common._write_csv_clean(
        FINAL_LAYOUT_BY_COLUMN_FILE,
        _final_column_fieldnames(finalist_column_rows),
        finalist_column_rows,
    )

    common._write_csv_clean(
        FINAL_LAYOUT_BY_LOCATION_FILE,
        [
            "Layout_ID",
            "Config_ID",
            "Location",
            "Rack",
            "Column",
            "Row",
            "Beam_Coordinate",
            "Beam_Height_Range_cm",
            "Assigned_Slot_Size_cm",
        ],
        finalist_location_rows,
    )

    return recommendation_rows, finalist_column_rows, finalist_location_rows, management_rows


if __name__ == "__main__":
    # Stage 8 entrypoint: TopFilled-only recommendations for practical implementation output.
    recommendation_rows, finalist_column_rows, finalist_location_rows, management_rows = build_final_selection()
    print(
        "Decision-support selection complete (TopFilled). "
        f"Recommendation rows: {len(recommendation_rows)}, decision objectives: {len(management_rows)}, "
        f"recommended details: {len(finalist_column_rows)} columns / {len(finalist_location_rows)} locations."
    )
