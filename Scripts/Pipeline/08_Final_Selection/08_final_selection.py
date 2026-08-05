import csv
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
OUTPUT_FILE = common.STAGE8_OUTPUT_DIR / "Candidate_Layout_Metric_Ranking.csv"
FINAL_LAYOUT_BY_COLUMN_FILE = common.STAGE8_OUTPUT_DIR / "Final_Layout_By_Rack_Column.csv"
FINAL_LAYOUT_BY_LOCATION_FILE = common.STAGE8_OUTPUT_DIR / "Final_Layout_By_Location.csv"
LEGACY_OUTPUT_FILES = [
    common.STAGE8_OUTPUT_DIR / "Objective_Layout_Recommendations.csv",
    common.STAGE8_OUTPUT_DIR / "Management_Decision_Table.csv",
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    # Keep CSV read behavior consistent with shared pipeline helpers.
    return common._read_csv(path)


def _write_csv_preserve(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    # Preserve explicit field order and duplicate-by-value rank columns for weighted-sum analysis.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


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


def _is_robustness_passing(row: dict[str, str]) -> bool:
    # Final selection should only include layouts that pass all evaluated
    # robustness scenarios (currently Base_Count only).
    pass_count = common._to_int_default(row.get("Scenario_Pass_Count"), 0)
    total_count = common._to_int_default(row.get("Scenario_Total_Count"), 0)
    robustness_value = common._to_float(row.get("Robustness")) or 0.0
    if total_count <= 0:
        return False
    return pass_count >= total_count and robustness_value > 0.0


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
    has_topfill = any(any(str(row.get(field, "")).strip() for field in topfill) for row in rows)
    if has_topfill:
        return [field for field in base if field != "Slot_Size_Distribution"] + topfill + ["Slot_Size_Distribution"]
    return base


def _topfill_added_height_by_layout() -> dict[str, float]:
    totals: dict[str, float] = {}
    if not LAYOUT_BY_COLUMN_FILE.exists():
        return totals

    for row in _read_csv(LAYOUT_BY_COLUMN_FILE):
        layout_id = str(row.get("Layout_ID", "")).strip()
        if not layout_id:
            continue
        totals[layout_id] = totals.get(layout_id, 0.0) + (common._to_float(row.get("TopFill_Added_Height_cm")) or 0.0)
    return totals


def _source_slot_sizes_by_layout(layouts: dict[str, dict[str, str]]) -> dict[str, set[int]]:
    # Parse the original configured slot sizes per layout before topfill adjustments.
    by_layout: dict[str, set[int]] = {}
    for layout_id, row in layouts.items():
        source_text = str(row.get("Source_Slot_Sizes", "")).strip()
        sizes: set[int] = set()
        if source_text:
            for token in source_text.split(","):
                token = token.strip()
                if not token:
                    continue
                size = common._to_int_default(token, -1)
                if size >= 0:
                    sizes.add(size)
        by_layout[layout_id] = sizes
    return by_layout


def _topfill_extra_slot_size_variants_by_layout(
    source_slot_sizes_by_layout: dict[str, set[int]],
) -> dict[str, int]:
    # Count distinct adjusted top slot sizes introduced by topfill that are not
    # part of the original configured slot-size set for each layout.
    extra_sizes_by_layout: dict[str, set[int]] = {}
    if not LAYOUT_BY_COLUMN_FILE.exists():
        return {}

    for row in _read_csv(LAYOUT_BY_COLUMN_FILE):
        layout_id = str(row.get("Layout_ID", "")).strip()
        if not layout_id:
            continue

        added_height = common._to_float(row.get("TopFill_Added_Height_cm")) or 0.0
        if added_height <= 0.0:
            continue

        adjusted_size = common._to_int_default(row.get("TopFill_Adjusted_Top_Slot_cm"), -1)
        if adjusted_size < 0:
            continue

        original_sizes = source_slot_sizes_by_layout.get(layout_id, set())
        if adjusted_size in original_sizes:
            continue

        if layout_id not in extra_sizes_by_layout:
            extra_sizes_by_layout[layout_id] = set()
        extra_sizes_by_layout[layout_id].add(adjusted_size)

    return {layout_id: len(sizes) for layout_id, sizes in extra_sizes_by_layout.items()}


def _topfill_extra_slot_sizes_by_layout(
    source_slot_sizes_by_layout: dict[str, set[int]],
) -> dict[str, str]:
    # List distinct adjusted top slot sizes introduced by topfill that are not
    # part of the original configured slot-size set for each layout.
    extra_sizes_by_layout: dict[str, set[int]] = {}
    if not LAYOUT_BY_COLUMN_FILE.exists():
        return {}

    for row in _read_csv(LAYOUT_BY_COLUMN_FILE):
        layout_id = str(row.get("Layout_ID", "")).strip()
        if not layout_id:
            continue

        added_height = common._to_float(row.get("TopFill_Added_Height_cm")) or 0.0
        if added_height <= 0.0:
            continue

        adjusted_size = common._to_int_default(row.get("TopFill_Adjusted_Top_Slot_cm"), -1)
        if adjusted_size < 0:
            continue

        original_sizes = source_slot_sizes_by_layout.get(layout_id, set())
        if adjusted_size in original_sizes:
            continue

        if layout_id not in extra_sizes_by_layout:
            extra_sizes_by_layout[layout_id] = set()
        extra_sizes_by_layout[layout_id].add(adjusted_size)

    return {
        layout_id: ",".join(str(size) for size in sorted(sizes))
        for layout_id, sizes in extra_sizes_by_layout.items()
    }


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


def _dense_ranks(values: list[float], higher_is_better: bool) -> list[int]:
    if not values:
        return []
    ordered_unique = sorted(set(values), reverse=higher_is_better)
    value_to_rank = {value: index for index, value in enumerate(ordered_unique, start=1)}
    return [value_to_rank[value] for value in values]


def build_final_selection() -> list[dict[str, str]]:
    """Build a full all-candidate metric table with per-metric ranks for weighted-sum analysis."""
    robustness_rows = [row for row in _robustness_rows() if _is_robustness_passing(row)]
    layouts = _layout_map()
    topfill_added_height = _topfill_added_height_by_layout()
    source_slot_sizes = _source_slot_sizes_by_layout(layouts)
    topfill_extra_slot_size_variants = _topfill_extra_slot_size_variants_by_layout(source_slot_sizes)
    topfill_extra_slot_sizes = _topfill_extra_slot_sizes_by_layout(source_slot_sizes)

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

    candidate_rows: list[dict[str, str]] = []
    for row in joined_rows:
        implementation_effort_total = (
            common._to_int_default(row.get("Beam_Relocations_Total"), 0)
            + common._to_int_default(row.get("Additional_Beams_Required"), 0)
            + common._to_int_default(row.get("Additional_Grids_Required"), 0)
        )

        assigned_locations_total = common._to_int_default(
            row.get("Assigned_Locations_Total") or row.get("Total_Locations"),
            0,
        )
        required_locations_total = common._to_int_default(row.get("Required_Locations_Total"), 0)

        candidate_rows.append(
            {
                "Layout_ID": str(row.get("Layout_ID", "")),
                "Config_ID": str(row.get("Config_ID", "")),
                "Source_Slot_Sizes": str(row.get("Source_Slot_Sizes", "")).strip(),
                "Assigned_Locations_Total": str(assigned_locations_total),
                "Capacity_Margin": str(assigned_locations_total - required_locations_total),
                "Occupancy_Rate": f"{common._to_float(row.get('Mean_Occupancy_Rate')) or 0.0:.6f}",
                "Beam_Relocations_Total": str(common._to_int_default(row.get("Beam_Relocations_Total"), 0)),
                "Additional_Beams_Required": str(common._to_int_default(row.get("Additional_Beams_Required"), 0)),
                "Additional_Grids_Required": str(common._to_int_default(row.get("Additional_Grids_Required"), 0)),
                "Implementation_Effort_Total": str(implementation_effort_total),
                "Standardization_Unique_Slot_Sizes": str(common._to_int_default(row.get("Unique_Slot_Sizes_Count"), 0)),
                "Additional_Fill_Height_Total_cm": f"{topfill_added_height.get(str(row.get('Layout_ID', '')).strip(), 0.0):.0f}",
                "Additional_Fill_Extra_Slot_Size_Variants": str(
                    topfill_extra_slot_size_variants.get(str(row.get("Layout_ID", "")).strip(), 0)
                ),
                "Additional_Fill_Extra_Slot_Sizes": str(
                    topfill_extra_slot_sizes.get(str(row.get("Layout_ID", "")).strip(), "")
                ),
            }
        )

    metric_rank_specs = [
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
        ("Occupancy_Rate", "Rank_Occupancy_Rate", False),
    ]

    for metric_field, rank_field, higher_is_better in metric_rank_specs:
        values = [common._to_float(row.get(metric_field)) or 0.0 for row in candidate_rows]
        ranks = _dense_ranks(values, higher_is_better=higher_is_better)
        for row, rank in zip(candidate_rows, ranks):
            row[rank_field] = str(rank)

    # Keep output order stable: best robustness first, then implementation effort.
    candidate_rows = sorted(
        candidate_rows,
        key=lambda row: (
            common._to_int_default(row.get("Rank_Implementation_Effort_Total"), 10**9),
            str(row.get("Layout_ID", "")),
        ),
    )

    _write_csv_preserve(
        OUTPUT_FILE,
        [
            "Layout_ID",
            "Config_ID",
            "Source_Slot_Sizes",
            "Assigned_Locations_Total",
            "Capacity_Margin",
            "Occupancy_Rate",
            "Rank_Occupancy_Rate",
            "Beam_Relocations_Total",
            "Rank_Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Rank_Additional_Required_Beams",
            "Additional_Grids_Required",
            "Rank_Additional_Required_Grids",
            "Implementation_Effort_Total",
            "Rank_Implementation_Effort_Total",
            "Standardization_Unique_Slot_Sizes",
            "Rank_Standardization",
            "Additional_Fill_Height_Total_cm",
            "Rank_Additional_Fill_Height_Total_cm",
            "Additional_Fill_Extra_Slot_Size_Variants",
            "Additional_Fill_Extra_Slot_Sizes",
            "Rank_Additional_Fill_Extra_Slot_Size_Variants",
        ],
        candidate_rows,
    )

    candidate_ids = {
        str(row.get("Layout_ID", "")).strip()
        for row in candidate_rows
        if str(row.get("Layout_ID", "")).strip()
    }

    finalist_column_rows: list[dict[str, str]] = []
    if LAYOUT_BY_COLUMN_FILE.exists():
        for row in _read_csv(LAYOUT_BY_COLUMN_FILE):
            if str(row.get("Layout_ID", "")).strip() in candidate_ids:
                finalist_column_rows.append(row)

    finalist_location_rows: list[dict[str, str]] = []
    if LAYOUT_BY_LOCATION_FILE.exists():
        for row in _read_csv(LAYOUT_BY_LOCATION_FILE):
            if str(row.get("Layout_ID", "")).strip() in candidate_ids:
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

    for legacy_file in LEGACY_OUTPUT_FILES:
        if legacy_file.exists():
            legacy_file.unlink()

    return candidate_rows


if __name__ == "__main__":
    # Stage 8 entrypoint: emit full candidate scorecard for weighted-sum analysis.
    candidate_rows = build_final_selection()
    print(
        "Decision-support candidate ranking complete (TopFilled). "
        f"Candidate rows: {len(candidate_rows)}."
    )
