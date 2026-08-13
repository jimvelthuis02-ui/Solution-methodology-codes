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
FINAL_LAYOUT_BY_SEGMENT_FILE = common.STAGE8_OUTPUT_DIR / "Final_Layout_By_Segment.csv"
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
    """Load Stage 6 layout summary rows keyed by config ID."""
    layouts: dict[str, dict[str, str]] = {}
    if LAYOUT_SUMMARY_FILE.exists():
        for row in _read_csv(LAYOUT_SUMMARY_FILE):
            config_id = str(row.get("Config_ID", "")).strip()
            if config_id:
                layouts[config_id] = row
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
        config_id = str(row.get("Config_ID", "")).strip()
        if not config_id:
            continue
        totals[config_id] = totals.get(config_id, 0.0) + (common._to_float(row.get("TopFill_Added_Height_cm")) or 0.0)
    return totals


def _source_slot_sizes_by_layout(layouts: dict[str, dict[str, str]]) -> dict[str, set[int]]:
    # Parse the original configured slot sizes per layout before topfill adjustments.
    by_layout: dict[str, set[int]] = {}
    for layout_id, row in layouts.items():
        source_text = common._decode_excel_text(row.get("Source_Slot_Sizes", ""))
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
        layout_id = str(row.get("Config_ID", "")).strip()
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
        layout_id = str(row.get("Config_ID", "")).strip()
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
    source = common._decode_excel_text(layout_row.get("Source_Slot_Sizes", ""))
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


def _parse_size_count_signature(value: str) -> dict[int, int]:
    counts: dict[int, int] = {}
    text = str(value).strip()
    if not text:
        return counts

    for token in text.split("|"):
        token = token.strip()
        if not token or ":" not in token:
            continue
        size_text, count_text = token.split(":", 1)
        size = common._to_int_default(size_text, -1)
        count = common._to_int_default(count_text, 0)
        if size < 0:
            continue
        counts[size] = max(count, 0)

    return counts


def _actual_occupied_by_size(layout_row: dict[str, str]) -> dict[int, int]:
    """Use the same best-fit exact-slot allocation used during layout generation so
    extra occupied slot sizes in the realized layout are included in KPI math."""
    minimum_by_size = _parse_size_count_signature(str(layout_row.get("Minimum_Required_Counts", "")))
    total_by_size = common._exclude_fixed_doorgang_slot_counts(
        _parse_size_count_signature(str(layout_row.get("TopFill_Layout_Slot_Size_Distribution", "")))
    )

    remaining_capacity = {size: max(int(count), 0) for size, count in total_by_size.items()}
    occupied_by_size: dict[int, int] = {}
    capacity_sizes = sorted(remaining_capacity.keys())

    for demand_size in sorted(minimum_by_size.keys(), reverse=True):
        remaining_demand = max(int(minimum_by_size.get(demand_size, 0)), 0)
        if remaining_demand <= 0:
            continue

        for capacity_size in capacity_sizes:
            if capacity_size < demand_size:
                continue
            available = remaining_capacity.get(capacity_size, 0)
            if available <= 0:
                continue

            used = min(available, remaining_demand)
            occupied_by_size[capacity_size] = occupied_by_size.get(capacity_size, 0) + used
            remaining_capacity[capacity_size] = available - used
            remaining_demand -= used

            if remaining_demand <= 0:
                break

    return occupied_by_size


def _space_utilization_metrics(layout_row: dict[str, str]) -> tuple[int, int, int, int, int, float]:
    # Compare actual occupied layout space against the realized top-filled layout,
    # including any extra slot sizes that appear in the final layout.
    occupied_by_size = _actual_occupied_by_size(layout_row)
    total_by_size = common._exclude_fixed_doorgang_slot_counts(
        _parse_size_count_signature(str(layout_row.get("TopFill_Layout_Slot_Size_Distribution", "")))
    )

    all_sizes = sorted(set(occupied_by_size.keys()) | set(total_by_size.keys()))
    occupied_locations = 0
    occupied_space = 0
    total_locations = 0
    total_space = 0

    for size in all_sizes:
        occupied_count = max(occupied_by_size.get(size, 0), 0)
        total_count = max(total_by_size.get(size, 0), 0)

        occupied_locations += occupied_count
        total_locations += total_count
        occupied_space += size * occupied_count
        total_space += size * total_count

    empty_locations = max(total_locations - occupied_locations, 0)
    empty_space = max(total_space - occupied_space, 0)
    utilization_pct = (occupied_space / total_space * 100.0) if total_space > 0 else 0.0

    return occupied_locations, empty_locations, occupied_space, empty_space, total_space, utilization_pct


def _segment_label(segment: tuple[str, int, int]) -> str:
    rack, c0, c1 = segment
    return f"{rack}[{c0:02d}-{c1:02d}]"


def _beam_change_rows_by_segment(
    config_id: str,
    current_units: set[str],
    proposed_units: set[str],
    current_unit_heights: dict[str, float],
    proposed_unit_heights: dict[str, float],
) -> list[dict[str, str]]:
    # Compute actual beam relocations/additions/removals per segment (not per-column duplication).
    by_segment_current: dict[tuple[str, int, int], list[float]] = {}
    by_segment_proposed: dict[tuple[str, int, int], list[float]] = {}

    for unit in current_units:
        height = current_unit_heights.get(unit)
        if height is None:
            continue
        parsed = common._parse_beam_coordinate_parts(unit)
        if parsed is None:
            continue
        rack, c0, c1, _level = parsed
        by_segment_current.setdefault((rack, c0, c1), []).append(float(height))

    for unit in proposed_units:
        height = proposed_unit_heights.get(unit)
        if height is None:
            continue
        parsed = common._parse_beam_coordinate_parts(unit)
        if parsed is None:
            continue
        rack, c0, c1, _level = parsed
        by_segment_proposed.setdefault((rack, c0, c1), []).append(float(height))

    all_segments = sorted(set(by_segment_current.keys()) | set(by_segment_proposed.keys()))
    rows: list[dict[str, str]] = []
    for segment in all_segments:
        current_heights = sorted(by_segment_current.get(segment, []))
        proposed_heights = sorted(by_segment_proposed.get(segment, []))

        paired_count = min(len(current_heights), len(proposed_heights))
        relocations = 0
        for index in range(paired_count):
            if abs(current_heights[index] - proposed_heights[index]) > 1e-9:
                relocations += 1

        removed = max(len(current_heights) - paired_count, 0)
        added = max(len(proposed_heights) - paired_count, 0)

        rows.append(
            {
                "Config_ID": config_id,
                "Segment_ID": _segment_label(segment),
                "Initial_Beams_In_Segment": str(len(current_heights)),
                "Required_Beams_In_Segment": str(len(proposed_heights)),
                "Beam_Relocations_In_Segment": str(relocations),
                "Removed_Beams_In_Segment": str(removed),
                "Added_Beams_In_Segment": str(added),
            }
        )

    return rows


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
        config_id = str(row.get("Config_ID", "")).strip()
        if not config_id:
            continue
        layout = layouts.get(config_id, {})
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
        occupied_locations, empty_locations, occupied_space, empty_space, total_space, utilization_pct = (
            _space_utilization_metrics(row)
        )
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

        occupied_space_m3 = occupied_space / 100.0
        empty_space_m3 = empty_space / 100.0
        total_space_m3 = total_space / 100.0

        candidate_rows.append(
            {
                "Config_ID": str(row.get("Config_ID", "")),
                "Source_Slot_Sizes": common._encode_excel_text(
                    common._decode_excel_text(row.get("Source_Slot_Sizes", ""))
                ),
                "Assigned_Locations_Total": str(assigned_locations_total),
                "Capacity_Margin": str(assigned_locations_total - required_locations_total),
                "Occupancy_Rate": f"{common._to_float(row.get('Mean_Occupancy_Rate')) or 0.0:.6f}",
                "Occupied_Locations": str(occupied_locations),
                "Empty_Locations": str(empty_locations),
                "Occupied_Slot_Space_cm": str(occupied_space),
                "Empty_Slot_Space_cm": str(empty_space),
                "Total_Slot_Space_cm": str(total_space),
                "Occupied_Slot_Space_m3": f"{occupied_space_m3:.6f}",
                "Empty_Slot_Space_m3": f"{empty_space_m3:.6f}",
                "Total_Slot_Space_m3": f"{total_space_m3:.6f}",
                "Space_Utilization_Pct": f"{utilization_pct:.4f}",
                "Beam_Relocations_Total": str(common._to_int_default(row.get("Beam_Relocations_Total"), 0)),
                "Additional_Beams_Required": str(common._to_int_default(row.get("Additional_Beams_Required"), 0)),
                "Additional_Grids_Required": str(common._to_int_default(row.get("Additional_Grids_Required"), 0)),
                "Implementation_Effort_Total": str(implementation_effort_total),
                "Standardization_Unique_Slot_Sizes": str(common._to_int_default(row.get("Unique_Slot_Sizes_Count"), 0)),
                "Additional_Fill_Height_Total_cm": f"{topfill_added_height.get(str(row.get('Config_ID', '')).strip(), 0.0):.0f}",
                "Additional_Fill_Extra_Slot_Size_Variants": str(
                    topfill_extra_slot_size_variants.get(str(row.get("Config_ID", "")).strip(), 0)
                ),
                "Additional_Fill_Extra_Slot_Sizes": common._encode_excel_text(
                    str(topfill_extra_slot_sizes.get(str(row.get("Config_ID", "")).strip(), ""))
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
        ("Empty_Slot_Space_m3", "Rank_Empty_Slot_Space_m3", False),
        ("Total_Slot_Space_m3", "Rank_Total_Slot_Space_m3", False),
        ("Space_Utilization_Pct", "Rank_Space_Utilization_Pct", False),
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
            str(row.get("Config_ID", "")),
        ),
    )

    _write_csv_preserve(
        OUTPUT_FILE,
        [
            "Config_ID",
            "Source_Slot_Sizes",
            "Assigned_Locations_Total",
            "Capacity_Margin",
            "Occupancy_Rate",
            "Rank_Occupancy_Rate",
            "Occupied_Locations",
            "Empty_Locations",
            "Occupied_Slot_Space_cm",
            "Empty_Slot_Space_cm",
            "Total_Slot_Space_cm",
            "Occupied_Slot_Space_m3",
            "Empty_Slot_Space_m3",
            "Rank_Empty_Slot_Space_m3",
            "Total_Slot_Space_m3",
            "Rank_Total_Slot_Space_m3",
            "Space_Utilization_Pct",
            "Rank_Space_Utilization_Pct",
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
        str(row.get("Config_ID", "")).strip()
        for row in candidate_rows
        if str(row.get("Config_ID", "")).strip()
    }

    finalist_column_rows: list[dict[str, str]] = []
    if LAYOUT_BY_COLUMN_FILE.exists():
        for row in _read_csv(LAYOUT_BY_COLUMN_FILE):
            if str(row.get("Config_ID", "")).strip() in candidate_ids:
                finalist_column_rows.append(row)

    finalist_location_rows: list[dict[str, str]] = []
    if LAYOUT_BY_LOCATION_FILE.exists():
        for row in _read_csv(LAYOUT_BY_LOCATION_FILE):
            if str(row.get("Config_ID", "")).strip() in candidate_ids:
                finalist_location_rows.append(row)

    prepared_rows = _read_csv(common.STAGE1_OUTPUT_DIR / "Location_Details_Prepared.csv")
    beam_map_rows = _read_csv(common.STAGE1_OUTPUT_DIR / "Location_Beam_Map.csv")
    beam_height_rows = _read_csv(common.STAGE1_OUTPUT_DIR / "Beam_Height_Coordinates.csv")
    _current_units, beam_segments, current_unit_heights = common._build_current_beam_units_and_segments(
        beam_map_rows,
        prepared_rows,
        beam_height_rows,
    )

    location_rows_by_config: dict[str, list[dict[str, str]]] = {}
    for row in finalist_location_rows:
        config_id = str(row.get("Config_ID", "")).strip()
        if not config_id:
            continue
        location_rows_by_config.setdefault(config_id, []).append(row)

    segment_rows: list[dict[str, str]] = []
    for config_id, config_location_rows in sorted(location_rows_by_config.items()):
        proposed_units, proposed_unit_heights = common._build_proposed_beam_units_from_layout_rows(
            config_location_rows,
            beam_segments,
        )
        segment_rows.extend(
            _beam_change_rows_by_segment(
                config_id,
                _current_units,
                proposed_units,
                current_unit_heights,
                proposed_unit_heights,
            )
        )

    final_column_fields = _final_column_fieldnames(finalist_column_rows)
    common._write_csv_clean(
        FINAL_LAYOUT_BY_COLUMN_FILE,
        final_column_fields,
        [
            {field: str(row.get(field, "")) for field in final_column_fields}
            for row in finalist_column_rows
        ],
    )

    common._write_csv_clean(
        FINAL_LAYOUT_BY_LOCATION_FILE,
        [
            "Config_ID",
            "Location",
            "Rack",
            "Column",
            "Row",
            "Beam_Coordinate",
            "Beam_Height_Range_cm",
            "Assigned_Slot_Size_cm",
        ],
        [
            {
                field: str(row.get(field, ""))
                for field in [
                    "Config_ID",
                    "Location",
                    "Rack",
                    "Column",
                    "Row",
                    "Beam_Coordinate",
                    "Beam_Height_Range_cm",
                    "Assigned_Slot_Size_cm",
                ]
            }
            for row in finalist_location_rows
        ],
    )

    common._write_csv_clean(
        FINAL_LAYOUT_BY_SEGMENT_FILE,
        [
            "Config_ID",
            "Segment_ID",
            "Initial_Beams_In_Segment",
            "Required_Beams_In_Segment",
            "Beam_Relocations_In_Segment",
            "Removed_Beams_In_Segment",
            "Added_Beams_In_Segment",
        ],
        segment_rows,
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
