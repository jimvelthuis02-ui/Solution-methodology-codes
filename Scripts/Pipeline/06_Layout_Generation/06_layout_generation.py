import csv
from collections import defaultdict
import sys
from pathlib import Path
from typing import TypeAlias

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


INPUT_CONFIG_FILE = common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv"
INPUT_CAPACITY_FILE = common.STAGE5_OUTPUT_DIR / "Constraint_Location_Counts_By_Slot_Size.csv"
INPUT_PREPARED = common.STAGE1_OUTPUT_DIR / "Location_Details_Prepared.csv"
INPUT_LOCATION_BEAM_MAP = common.STAGE1_OUTPUT_DIR / "Beam_Grid_Mapping" / "Location_Beam_Map.csv"
INPUT_BEAM_HEIGHT_COORDS = common.STAGE1_OUTPUT_DIR / "Beam_Grid_Mapping" / "Beam_Height_Coordinates.csv"
LAYOUT_OUTPUT_DIR = common.STAGE6_OUTPUT_DIR
PRE_ROBUST_LAYOUT_LIMIT = 8
IMPLEMENTATION_STYLE = "implementation"
STYLE_PRIORITY = (IMPLEMENTATION_STYLE,)

SummaryRow: TypeAlias = dict[str, str]
DetailRows: TypeAlias = list[dict[str, str]]
LayoutSignature: TypeAlias = tuple[str, ...]
StyleCandidate: TypeAlias = tuple[SummaryRow, DetailRows, DetailRows, LayoutSignature]
ConfigStyleBundle: TypeAlias = tuple[str, list[StyleCandidate]]


def _read_csv(path: Path) -> list[dict[str, str]]:
    # Keep CSV read behavior consistent with shared pipeline helpers.
    return common._read_csv(path)


def _write_csv_preserve(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    # Keep full schema for diagnostic/interpretability columns.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _shortlisted_configs() -> list[dict[str, str]]:
    """Read Stage 4 configurations and keep only shortlisted candidates."""
    configs = _read_csv(INPUT_CONFIG_FILE)
    return [row for row in configs if str(row.get("Selection_Status", "")).strip() == "SHORTLISTED"]


def _capacity_rows_by_config() -> dict[str, list[dict[str, str]]]:
    # Group Stage 5 constraint rows by configuration ID.
    rows = _read_csv(INPUT_CAPACITY_FILE)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("Config_ID", "")).strip()].append(row)
    return grouped


def _slot_sizes_from_capacity(rows: list[dict[str, str]]) -> list[float]:
    # Extract unique representative slot sizes for reporting fields.
    sizes = {
        common._to_float(row.get("Representative_Slot_Size"))
        for row in rows
        if common._to_float(row.get("Representative_Slot_Size")) is not None
    }
    return sorted(float(value) for value in sizes if value is not None)


def _worst_case_exact_counts(rows: list[dict[str, str]]) -> dict[float, int]:
    """Derive exact required counts by slot size from worst-case cumulative constraints."""
    cumulative_by_size: dict[float, int] = defaultdict(int)
    for row in rows:
        slot_size = common._to_float(row.get("Representative_Slot_Size"))
        required = common._to_float(
            row.get("Min_Required_Locations_At_Or_Above_Size")
            or row.get("Cumulative_Assigned_SKUs_At_Or_Above_Size")
        )
        if slot_size is None or required is None:
            continue
        cumulative_by_size[slot_size] = max(cumulative_by_size.get(slot_size, 0), int(round(required)))

    # Convert cumulative at-or-above counts into exact per-size requirements.
    ordered_sizes = sorted(cumulative_by_size)
    exact_counts: dict[float, int] = {}
    for index, slot_size in enumerate(ordered_sizes):
        next_size = ordered_sizes[index + 1] if index + 1 < len(ordered_sizes) else None
        next_required = cumulative_by_size.get(next_size, 0) if next_size is not None else 0
        exact_counts[slot_size] = max(cumulative_by_size[slot_size] - next_required, 0)

    return exact_counts


def _base_exact_counts(rows: list[dict[str, str]]) -> dict[float, int]:
    # Layout generation should be independent of SKU-scenario stress tests.
    # Use base demand rows when available; robustness checks handle low/high demand.
    base_rows = [
        row
        for row in rows
        if str(row.get("SKU_Scenario", "")).strip() == "Base_Count"
    ]
    if base_rows:
        return _worst_case_exact_counts(base_rows)
    return _worst_case_exact_counts(rows)


def _layout_signature(generated_location_rows: list[dict[str, str]]) -> tuple[str, ...]:
    # Signature used to detect whether styles produce materially different layouts.
    ordered = sorted(
        generated_location_rows,
        key=lambda row: (
            str(row.get("Rack", "")),
            str(row.get("Column", "")),
            str(row.get("Row", "")),
            str(row.get("Assigned_Slot_Size_cm", "")),
        ),
    )
    return tuple(
        f"{row.get('Rack','')}{row.get('Column','')}:{row.get('Row','')}:{row.get('Assigned_Slot_Size_cm','')}"
        for row in ordered
    )


def _style_rank(style: str) -> int:
    try:
        return STYLE_PRIORITY.index(style)
    except ValueError:
        return len(STYLE_PRIORITY)


def _signature_similarity(sig_a: LayoutSignature, sig_b: LayoutSignature) -> float:
    if not sig_a and not sig_b:
        return 1.0
    if not sig_a or not sig_b:
        return 0.0
    length = min(len(sig_a), len(sig_b))
    if length <= 0:
        return 0.0
    matches = sum(1 for idx in range(length) if sig_a[idx] == sig_b[idx])
    return matches / length


def _beam_preference_by_column(current_beam_units: set[str]) -> dict[str, int]:
    preference: dict[str, int] = defaultdict(int)
    for beam_unit in current_beam_units:
        for column_key in common._beam_unit_columns(beam_unit):
            preference[column_key] += 1
    return dict(preference)


def _column_order_for_style(column_keys: list[str], used_by_column: dict[str, float], style: str) -> list[str]:
    _ = style
    return sorted(column_keys, key=lambda key: (-used_by_column.get(key, 0.0), key))


def _expand_layout_capacity(
    column_assignments: dict[str, list[float]],
    used_by_column: dict[str, float],
    column_keys: list[str],
    slot_sizes: list[float],
    style: str,
    beam_preference: dict[str, int],
) -> tuple[dict[str, list[float]], dict[str, float]]:
    # Fill remaining feasible height with a balanced mix across configured slot
    # sizes so additional capacity does not collapse to only the smallest slots.
    expanded_assignments: dict[str, list[float]] = {
        column_key: list(column_assignments.get(column_key, []))
        for column_key in column_keys
    }
    expanded_used: dict[str, float] = {
        column_key: float(used_by_column.get(column_key, 0.0))
        for column_key in column_keys
    }
    candidate_slot_sizes = sorted(
        {
            float(size)
            for size in slot_sizes
            if common._to_float(size) is not None and float(size) > 0.0
        },
        reverse=True,
    )
    if not candidate_slot_sizes:
        compact_assignments = {
            column_key: slots
            for column_key, slots in expanded_assignments.items()
            if slots
        }
        compact_used = {
            column_key: expanded_used[column_key]
            for column_key in compact_assignments
        }
        return compact_assignments, compact_used

    added_counts: dict[float, int] = {slot_size: 0 for slot_size in candidate_slot_sizes}

    while True:
        placed = False
        tried_slot_sizes: set[float] = set()
        _ = beam_preference
        expansion_columns = _column_order_for_style(column_keys, expanded_used, style)

        # Place one slot at a time, always picking the currently least-used slot
        # size (ties -> larger size first) to keep expansion close to equal mix.
        while len(tried_slot_sizes) < len(candidate_slot_sizes):
            remaining_sizes = [size for size in candidate_slot_sizes if size not in tried_slot_sizes]
            target_size = min(remaining_sizes, key=lambda size: (added_counts.get(size, 0), -size))

            placed_target = False
            for column_key in expansion_columns:
                current_count = len(expanded_assignments[column_key])
                next_count = current_count + 1
                allowed_after = common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(next_count - 1, common.MIN_BEAMS_PER_COLUMN)
                proposed_used = expanded_used[column_key] + target_size
                if proposed_used > allowed_after + 1e-9:
                    continue

                expanded_assignments[column_key].append(target_size)
                expanded_used[column_key] = proposed_used
                added_counts[target_size] = added_counts.get(target_size, 0) + 1
                placed_target = True
                placed = True
                break

            if placed_target:
                break

            tried_slot_sizes.add(target_size)

        if not placed:
            break

    compact_assignments = {
        column_key: slots
        for column_key, slots in expanded_assignments.items()
        if slots
    }
    compact_used = {
        column_key: expanded_used[column_key]
        for column_key in compact_assignments
    }
    return compact_assignments, compact_used


def _slot_distribution_signature(column_assignments: dict[str, list[float]]) -> str:
    counts: dict[int, int] = defaultdict(int)
    for slots in column_assignments.values():
        for slot_size in slots:
            counts[int(round(slot_size))] += 1
    return "|".join(f"{size}:{count}" for size, count in sorted(counts.items()))


def _cumulative_coverage_signature(column_assignments: dict[str, list[float]]) -> str:
    exact_counts: dict[int, int] = defaultdict(int)
    for slots in column_assignments.values():
        for slot_size in slots:
            exact_counts[int(round(slot_size))] += 1

    running = 0
    cumulative: dict[int, int] = {}
    for size in sorted(exact_counts.keys(), reverse=True):
        running += exact_counts[size]
        cumulative[size] = running

    return "|".join(f"{size}:{cumulative[size]}" for size in sorted(cumulative.keys()))


def _slot_signatures_from_location_rows(location_rows: list[dict[str, str]]) -> tuple[str, str]:
    # Recompute slot-distribution signatures directly from location-level rows.
    exact_counts: dict[int, int] = defaultdict(int)
    for row in location_rows:
        slot_size = common._to_float(row.get("Assigned_Slot_Size_cm"))
        if slot_size is None:
            continue
        exact_counts[int(round(slot_size))] += 1

    distribution = "|".join(f"{size}:{count}" for size, count in sorted(exact_counts.items()))

    running = 0
    cumulative: dict[int, int] = {}
    for size in sorted(exact_counts.keys(), reverse=True):
        running += exact_counts[size]
        cumulative[size] = running
    cumulative_signature = "|".join(f"{size}:{cumulative[size]}" for size in sorted(cumulative.keys()))

    return distribution, cumulative_signature


def _build_top_filled_layout_set(
    summary_rows: list[dict[str, str]],
    column_rows: list[dict[str, str]],
    location_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    # Create a separate variant where each column's remaining height is added
    # to its top location so no practical column space is left unused.
    remaining_by_layout_column: dict[tuple[str, str], float] = {}
    for row in column_rows:
        layout_id = str(row.get("Layout_ID", "")).strip()
        rack_column = str(row.get("Rack_Column", "")).strip()
        remaining = common._to_float(row.get("Remaining_Height_cm")) or 0.0
        if layout_id and rack_column:
            remaining_by_layout_column[(layout_id, rack_column)] = max(remaining, 0.0)

    grouped_locations: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in location_rows:
        layout_id = str(row.get("Layout_ID", "")).strip()
        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        if not layout_id or not rack or not column:
            continue
        grouped_locations[(layout_id, f"{rack}{column}")].append(dict(row))

    top_filled_location_rows: list[dict[str, str]] = []
    space_added_by_layout: dict[str, float] = defaultdict(float)
    for (layout_id, rack_column), rows in grouped_locations.items():
        rows_sorted = sorted(rows, key=lambda item: common._to_int_default(item.get("Row"), 0))
        remaining = remaining_by_layout_column.get((layout_id, rack_column), 0.0)
        if rows_sorted and remaining > 1e-9:
            top_row = rows_sorted[-1]
            top_slot = common._to_float(top_row.get("Assigned_Slot_Size_cm")) or 0.0
            top_row["Assigned_Slot_Size_cm"] = f"{(top_slot + remaining):.0f}"
            space_added_by_layout[layout_id] += remaining
        top_filled_location_rows.extend(rows_sorted)

    top_filled_location_rows = sorted(
        top_filled_location_rows,
        key=lambda row: (
            str(row.get("Layout_ID", "")),
            str(row.get("Rack", "")),
            str(row.get("Column", "")),
            common._to_int_default(row.get("Row"), 0),
        ),
    )

    top_filled_column_rows: list[dict[str, str]] = []
    for row in column_rows:
        updated = dict(row)
        layout_id = str(updated.get("Layout_ID", "")).strip()
        rack_column = str(updated.get("Rack_Column", "")).strip()
        remaining = remaining_by_layout_column.get((layout_id, rack_column), 0.0)

        assigned_used = common._to_float(updated.get("Assigned_Used_Height_cm")) or 0.0
        allowed = common._to_float(updated.get("Allowed_Used_Height_cm")) or 0.0
        new_used = assigned_used + remaining

        updated["Assigned_Used_Height_cm"] = f"{new_used:.3f}"
        updated["Remaining_Height_cm"] = "0.000"
        updated["Fill_Ratio"] = f"{(new_used / allowed) if allowed > 0 else 0.0:.4f}"
        top_filled_column_rows.append(updated)

    location_rows_by_layout: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in top_filled_location_rows:
        location_rows_by_layout[str(row.get("Layout_ID", "")).strip()].append(row)

    top_filled_summary_rows: list[dict[str, str]] = []
    for row in summary_rows:
        layout_id = str(row.get("Layout_ID", "")).strip()
        if layout_id not in location_rows_by_layout:
            continue

        updated = dict(row)
        total_used = common._to_float(updated.get("Assigned_Used_Height_Total")) or 0.0
        total_allowed = common._to_float(updated.get("Total_Allowed_Height")) or 0.0
        added = space_added_by_layout.get(layout_id, 0.0)
        new_total_used = total_used + added

        dist_sig, cum_sig = _slot_signatures_from_location_rows(location_rows_by_layout[layout_id])

        updated["Assigned_Used_Height_Total"] = f"{new_total_used:.3f}"
        updated["Space_Left"] = "0.000"
        updated["Percentage_Rack_Height_Used"] = f"{((new_total_used / total_allowed) * 100.0) if total_allowed > 0 else 0.0:.2f}"
        updated["Layout_Slot_Size_Distribution"] = dist_sig
        updated["Layout_Slot_Size_Cumulative_Coverage"] = cum_sig
        top_filled_summary_rows.append(updated)

    return top_filled_summary_rows, top_filled_column_rows, top_filled_location_rows


def _pre_robust_sort_key(summary_row: dict[str, str]) -> tuple[int, int, int, float, int, int]:
    feasible_penalty = 0 if str(summary_row.get("Layout_Feasible", "")).strip().upper() == "YES" else 1
    additional_beams = common._to_int_default(summary_row.get("Additional_Beams_Required"), 0)
    return (
        feasible_penalty,
        common._to_int_default(summary_row.get("Beam_Relocations_Total"), 0),
        additional_beams,
        common._to_float(summary_row.get("Space_Left")) or 0.0,
        -common._to_int_default(summary_row.get("Assigned_Locations_Total"), 0),
        _style_rank(str(summary_row.get("Style", ""))),
    )


def _percent_diff(value_a: float, value_b: float) -> float:
    scale = max(abs(value_a), abs(value_b), 1e-9)
    return abs(value_a - value_b) / scale


def _kpi_winner(util_value: float, reloc_value: float, higher_is_better: bool) -> str:
    if abs(util_value - reloc_value) <= 1e-9:
        return "tie"
    if higher_is_better:
        return "utilization" if util_value > reloc_value else "relocation"
    return "utilization" if util_value < reloc_value else "relocation"


def build_layout_generation() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Generate candidate layouts and emit summary, column, and location-level outputs."""
    prepared_rows = _read_csv(INPUT_PREPARED)
    beam_map_rows = _read_csv(INPUT_LOCATION_BEAM_MAP)
    beam_height_rows = _read_csv(INPUT_BEAM_HEIGHT_COORDS)
    configs = _shortlisted_configs()
    capacity_rows = _capacity_rows_by_config()
    doorgang_thresholds_by_rack = common._doorgang_thresholds_by_rack(prepared_rows)
    fixed_doorgang_slot_by_column = common._fixed_doorgang_slot_by_column(prepared_rows)
    layout_columns = common._build_layout_columns(prepared_rows)
    current_beam_units, beam_segments, current_beam_heights = common._build_current_beam_units_and_segments(
        beam_map_rows,
        prepared_rows,
        beam_height_rows,
    )
    initial_beam_count, initial_grid_count = common._initial_beam_grid_counts(prepared_rows, current_beam_units)
    beam_preference = _beam_preference_by_column(current_beam_units)
    total_physical_locations = len(
        [
            row
            for row in prepared_rows
            if str(row.get("Location Type", "")).strip().lower() != "doorgang"
            and not common._is_split_location(str(row.get("Location", "")).strip())
        ]
    )

    config_style_bundles: list[ConfigStyleBundle] = []

    candidate_layout_rows: list[dict[str, str]] = []
    candidate_layout_location_rows: list[dict[str, str]] = []
    candidate_layout_column_rows: list[dict[str, str]] = []

    # Build layout variants for every shortlisted config and layout style.
    layout_counter = 1
    for config in configs:
        config_id = str(config.get("Config_ID", "")).strip()
        rows = capacity_rows.get(config_id, [])
        if not rows:
            continue

        base_exact_counts = _base_exact_counts(rows)
        if not base_exact_counts:
            continue

        config_slot_sizes = _slot_sizes_from_capacity(rows)
        style_candidates: list[StyleCandidate] = []
        style = IMPLEMENTATION_STYLE
        layout_id = f"LAY_{layout_counter:03d}"
        layout_counter += 1

        # Allocate slot sizes to rack-columns for a single implementation layout.
        feasible_layout, assigned_exact, used_by_column, column_assignments, note, allocation_diagnostics = common._allocate_layout_by_column(
            target_exact_counts=base_exact_counts,
            column_keys=layout_columns,
            style=style,
            style_context={
                "beam_preference": beam_preference,
                "fixed_prefix_by_column": {
                    column_key: [float(height)]
                    for column_key, height in fixed_doorgang_slot_by_column.items()
                },
            },
        )

        expansion_slot_sizes = sorted(float(slot_size) for slot_size in base_exact_counts)
        column_assignments, used_by_column = _expand_layout_capacity(
            column_assignments=column_assignments,
            used_by_column=used_by_column,
            column_keys=layout_columns,
            slot_sizes=expansion_slot_sizes,
            style=style,
            beam_preference=beam_preference,
        )

        column_assignments = common._optimize_column_slot_order_for_beam_preservation(
            column_assignments,
            beam_segments,
            current_beam_heights,
            fixed_prefix_by_column={
                column_key: [float(height)]
                for column_key, height in fixed_doorgang_slot_by_column.items()
                if column_key in column_assignments
            },
        )

        generated_location_rows = common._build_generated_layout_location_rows(
            layout_id=layout_id,
            config_id=config_id,
            style=style,
            column_assignments=column_assignments,
            segments=beam_segments,
            doorgang_thresholds_by_rack=doorgang_thresholds_by_rack,
        )
        layout_signature = _layout_signature(generated_location_rows)
        proposed_beam_units, proposed_beam_heights = common._build_proposed_beam_units_from_layout_rows(
            generated_location_rows,
            beam_segments,
        )
        relocation_total, relocation_by_column = common._beam_relocations(
            current_beam_units,
            proposed_beam_units,
            current_beam_heights,
            proposed_beam_heights,
        )
        required_beams, required_grids, additional_beams, additional_grids = common._material_requirements(
            initial_beam_count,
            initial_grid_count,
            proposed_beam_units,
            generated_location_rows,
        )

        # Compute utilization and implementation-effort KPIs per configuration.
        assigned_total = len(generated_location_rows)
        layout_physical_locations = assigned_total
        required_locations_total = sum(base_exact_counts.values())
        total_used_height = sum(used_by_column.values())
        total_allowed_height = sum(
            common.MAX_USED_HEIGHT_BASE
            - common.BEAM_HEIGHT * max(len(column_assignments.get(column_key, [])) - 1, common.MIN_BEAMS_PER_COLUMN)
            for column_key in layout_columns
        )
        space_utilization = (total_used_height / total_allowed_height) if total_allowed_height > 0 else 0.0
        capacity_margin = assigned_total - required_locations_total
        required_beam_moves = relocation_total
        pct_rack_height_used = space_utilization * 100.0
        space_left = sum(
            max(
                (
                    common.MAX_USED_HEIGHT_BASE
                    - common.BEAM_HEIGHT * max(len(column_assignments.get(column_key, [])) - 1, common.MIN_BEAMS_PER_COLUMN)
                )
                - used_by_column.get(column_key, 0.0),
                0.0,
            )
            for column_key in layout_columns
        )

        summary_row = {
                "Layout_ID": layout_id,
                "Config_ID": config_id,
                "Layout_Feasible": "YES" if feasible_layout else "NO",
                "Required_Locations_Total": str(required_locations_total),
                "Total_Locations": str(assigned_total),
                "Capacity_Margin": str(capacity_margin),
                "Assigned_Used_Height_Total": f"{total_used_height:.3f}",
                "Total_Allowed_Height": f"{total_allowed_height:.3f}",
                "Space_Left": f"{space_left:.3f}",
                "Beam_Relocations_Total": str(relocation_total),
                "Initial_Beams_Total": str(initial_beam_count),
                "Required_Beams_Total": str(required_beams),
                "Additional_Beams_Required": str(additional_beams),
                "Initial_Grids_Total": str(initial_grid_count),
                "Required_Grids_Total": str(required_grids),
                "Additional_Grids_Required": str(additional_grids),
                "Percentage_Rack_Height_Used": f"{pct_rack_height_used:.2f}",
                "Worst_Case_Exact_Counts": "|".join(f"{int(size)}:{count}" for size, count in sorted(base_exact_counts.items())),
                "Slot_Composition_Signature": "|".join(f"{int(size)}:{count}" for size, count in sorted(base_exact_counts.items())),
                "Layout_Slot_Size_Distribution": _slot_distribution_signature(column_assignments),
                "Layout_Slot_Size_Cumulative_Coverage": _cumulative_coverage_signature(column_assignments),
                "Source_Slot_Sizes": ",".join(f"{int(size)}" for size in config_slot_sizes),
                "Feasible_Columns_Considered_Total": str(int(allocation_diagnostics.get("Feasible_Columns_Considered_Total", 0.0))),
                "Feasible_Columns_Min": str(int(allocation_diagnostics.get("Feasible_Columns_Min", 0.0))),
                "Feasible_Columns_Max": str(int(allocation_diagnostics.get("Feasible_Columns_Max", 0.0))),
                "Feasible_Columns_Average": f"{allocation_diagnostics.get('Feasible_Columns_Average', 0.0):.3f}",
            }

        # Capture per-column slot mix and beam movement details.
        slot_mix_by_column: dict[str, dict[float, int]] = defaultdict(lambda: defaultdict(int))
        for row in generated_location_rows:
            rack = str(row.get("Rack", "")).strip()
            column = str(row.get("Column", "")).strip()
            slot = common._to_float(row.get("Assigned_Slot_Size_cm"))
            if rack and column and slot is not None:
                slot_mix_by_column[f"{rack}{column}"][slot] += 1

        style_column_rows: list[dict[str, str]] = []
        for column_key, slots in sorted(column_assignments.items()):
            used = used_by_column.get(column_key, 0.0)
            beam_count = max(len(slots) - 1, common.MIN_BEAMS_PER_COLUMN)
            allowed = common.MAX_USED_HEIGHT_BASE - beam_count * common.BEAM_HEIGHT
            mix = slot_mix_by_column.get(column_key, {})
            style_column_rows.append(
                {
                    "Layout_ID": layout_id,
                    "Config_ID": config_id,
                    "Rack_Column": column_key,
                    "Beam_Count_Used": str(beam_count),
                    "Allowed_Used_Height_cm": f"{allowed:.3f}",
                    "Assigned_Used_Height_cm": f"{used:.3f}",
                    "Remaining_Height_cm": f"{max(allowed - used, 0.0):.3f}",
                    "Fill_Ratio": f"{(used / allowed) if allowed > 0 else 0.0:.4f}",
                    "Beam_Relocations_In_Column": str(relocation_by_column.get(column_key, 0)),
                    "Slot_Size_Distribution": "|".join(
                        f"{int(slot_size)}:{count}" for slot_size, count in sorted(mix.items())
                    ),
                }
            )

        style_candidates.append((summary_row, generated_location_rows, style_column_rows, layout_signature))

        if not style_candidates:
            continue

        for summary, _loc, _col, _signature in style_candidates:
            summary["Pre_Robustness_Status"] = "PENDING"
            summary["Pre_Robustness_Rank"] = ""
            summary["Pre_Robustness_Prune_Reason"] = ""

        config_style_bundles.append((config_id, style_candidates))

    if config_style_bundles:
        def _bundle_score(bundle: ConfigStyleBundle) -> tuple[int, int, int, float, int, int]:
            _config_id, candidates = bundle
            return min(_pre_robust_sort_key(candidate[0]) for candidate in candidates)

        by_composition: dict[str, list[ConfigStyleBundle]] = defaultdict(list)
        for bundle in config_style_bundles:
            _config_id, candidates = bundle
            signature = str(candidates[0][0].get("Slot_Composition_Signature", "")).strip() if candidates else ""
            by_composition[signature].append(bundle)

        composition_winners: list[ConfigStyleBundle] = []
        for bundles in by_composition.values():
            composition_winners.append(min(bundles, key=_bundle_score))

        target_count = min(PRE_ROBUST_LAYOUT_LIMIT, len(config_style_bundles))
        finalists = sorted(composition_winners, key=_bundle_score)[:target_count]
        selected_config_ids = {config_id for config_id, _candidates in finalists}

        if len(finalists) < target_count:
            ranked_remaining = sorted(
                [bundle for bundle in config_style_bundles if bundle[0] not in selected_config_ids],
                key=_bundle_score,
            )
            needed = target_count - len(finalists)
            finalists.extend(ranked_remaining[:needed])
            selected_config_ids = {config_id for config_id, _candidates in finalists}

        finalists = sorted(finalists, key=_bundle_score)
        config_rank = {
            config_id: index
            for index, (config_id, _candidates) in enumerate(finalists, start=1)
        }

        for config_id, candidates in config_style_bundles:
            for summary, location_rows, column_rows, _signature in candidates:
                if config_id in selected_config_ids:
                    summary["Pre_Robustness_Status"] = "SELECTED"
                    summary["Pre_Robustness_Rank"] = str(config_rank.get(config_id, ""))
                    summary["Pre_Robustness_Prune_Reason"] = ""
                    candidate_layout_column_rows.extend(column_rows)
                    candidate_layout_location_rows.extend(location_rows)
                else:
                    summary["Pre_Robustness_Status"] = "PRUNED"
                    summary["Pre_Robustness_Rank"] = ""
                    summary["Pre_Robustness_Prune_Reason"] = "Outside pre-robust top configuration set"

                candidate_layout_rows.append({key: str(value) for key, value in summary.items()})

    # Write layout-level KPIs.
    summary_fieldnames = [
        "Layout_ID",
        "Config_ID",
        "Pre_Robustness_Status",
        "Pre_Robustness_Rank",
        "Pre_Robustness_Prune_Reason",
        "Layout_Feasible",
        "Required_Locations_Total",
        "Total_Locations",
        "Capacity_Margin",
        "Assigned_Used_Height_Total",
        "Total_Allowed_Height",
        "Space_Left",
        "Beam_Relocations_Total",
        "Initial_Beams_Total",
        "Required_Beams_Total",
        "Additional_Beams_Required",
        "Initial_Grids_Total",
        "Required_Grids_Total",
        "Additional_Grids_Required",
        "Percentage_Rack_Height_Used",
        "Worst_Case_Exact_Counts",
        "Layout_Slot_Size_Distribution",
        "Layout_Slot_Size_Cumulative_Coverage",
        "Source_Slot_Sizes",
        "Feasible_Columns_Considered_Total",
        "Feasible_Columns_Min",
        "Feasible_Columns_Max",
        "Feasible_Columns_Average",
    ]
    summary_output_rows = [
        {field: str(row.get(field, "")) for field in summary_fieldnames}
        for row in candidate_layout_rows
    ]
    _write_csv_preserve(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary.csv",
        summary_fieldnames,
        summary_output_rows,
    )

    # Write rack-column level breakdown.
    common._write_csv_clean(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_By_Rack_Column.csv",
        [
            "Layout_ID",
            "Config_ID",
            "Rack_Column",
            "Beam_Count_Used",
            "Allowed_Used_Height_cm",
            "Assigned_Used_Height_cm",
            "Remaining_Height_cm",
            "Fill_Ratio",
            "Beam_Relocations_In_Column",
            "Slot_Size_Distribution",
        ],
        candidate_layout_column_rows,
    )

    # Write location-level assignments.
    common._write_csv_clean(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_By_Location.csv",
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
        candidate_layout_location_rows,
    )

    # Write a separate practical variant where each column's residual space is
    # absorbed by its highest assigned location.
    top_filled_summary_rows, top_filled_column_rows, top_filled_location_rows = _build_top_filled_layout_set(
        summary_output_rows,
        candidate_layout_column_rows,
        candidate_layout_location_rows,
    )

    _write_csv_preserve(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary_TopFilled.csv",
        summary_fieldnames,
        [{field: str(row.get(field, "")) for field in summary_fieldnames} for row in top_filled_summary_rows],
    )

    common._write_csv_clean(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_By_Rack_Column_TopFilled.csv",
        [
            "Layout_ID",
            "Config_ID",
            "Rack_Column",
            "Beam_Count_Used",
            "Allowed_Used_Height_cm",
            "Assigned_Used_Height_cm",
            "Remaining_Height_cm",
            "Fill_Ratio",
            "Beam_Relocations_In_Column",
            "Slot_Size_Distribution",
        ],
        top_filled_column_rows,
    )

    common._write_csv_clean(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_By_Location_TopFilled.csv",
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
        top_filled_location_rows,
    )

    return candidate_layout_rows, candidate_layout_column_rows, candidate_layout_location_rows


if __name__ == "__main__":
    # Stage 6 entrypoint: build and persist all candidate layouts.
    layout_rows, column_rows, location_rows = build_layout_generation()
    print(
        "Layout generation complete. "
        f"Layouts: {len(layout_rows)}, columns: {len(column_rows)}, locations: {len(location_rows)}."
    )
