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
LAYOUT_OUTPUT_DIR = common.STAGE6_OUTPUT_DIR
PRE_ROBUST_LAYOUT_LIMIT = 8
STYLE_PRIORITY = ("utilization", "balanced", "relocation", "material")

SummaryRow: TypeAlias = dict[str, str]
DetailRows: TypeAlias = list[dict[str, str]]
LayoutSignature: TypeAlias = tuple[str, ...]
StyleCandidate: TypeAlias = tuple[SummaryRow, DetailRows, DetailRows, LayoutSignature]
SelectedCandidate: TypeAlias = tuple[SummaryRow, DetailRows, DetailRows]


def _read_csv(path: Path) -> list[dict[str, str]]:
    # Keep CSV read behavior consistent with shared pipeline helpers.
    return common._read_csv(path)


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


def _column_order_for_style(column_keys: list[str], used_by_column: dict[str, float], style: str) -> list[str]:
    if style == "relocation":
        return sorted(column_keys)
    if style == "material":
        return sorted(column_keys, key=lambda key: (used_by_column.get(key, 0.0), key))
    if style == "balanced":
        return sorted(column_keys, key=lambda key: (abs(used_by_column.get(key, 0.0)), key))
    return sorted(column_keys, key=lambda key: (-used_by_column.get(key, 0.0), key))


def _expand_layout_capacity(
    column_assignments: dict[str, list[float]],
    used_by_column: dict[str, float],
    column_keys: list[str],
    min_slot_size: float,
    style: str,
) -> tuple[dict[str, list[float]], dict[str, float]]:
    # Fill remaining feasible height with the smallest slot size so each layout
    # carries its own physical capacity independent of SKU scenarios.
    expanded_assignments: dict[str, list[float]] = {
        column_key: list(column_assignments.get(column_key, []))
        for column_key in column_keys
    }
    expanded_used: dict[str, float] = {
        column_key: float(used_by_column.get(column_key, 0.0))
        for column_key in column_keys
    }

    while True:
        placed = False
        for column_key in _column_order_for_style(column_keys, expanded_used, style):
            current_count = len(expanded_assignments[column_key])
            next_count = current_count + 1
            allowed_after = common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(next_count - 1, common.MIN_BEAMS_PER_COLUMN)
            proposed_used = expanded_used[column_key] + min_slot_size
            if proposed_used > allowed_after + 1e-9:
                continue

            expanded_assignments[column_key].append(min_slot_size)
            expanded_used[column_key] = proposed_used
            placed = True

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


def build_layout_generation() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Generate candidate layouts and emit summary, column, and location-level outputs."""
    prepared_rows = _read_csv(INPUT_PREPARED)
    beam_map_rows = _read_csv(INPUT_LOCATION_BEAM_MAP)
    configs = _shortlisted_configs()
    capacity_rows = _capacity_rows_by_config()
    layout_columns = common._build_layout_columns(prepared_rows)
    current_beam_units, beam_segments = common._build_current_beam_units_and_segments(beam_map_rows)
    total_physical_locations = len(
        [
            row
            for row in prepared_rows
            if str(row.get("Location Type", "")).strip().lower() != "doorgang"
            and not common._is_split_location(str(row.get("Location", "")).strip())
        ]
    )

    selected_per_config: list[SelectedCandidate] = []

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

        for style in common.CANDIDATE_LAYOUT_STYLES:
            layout_id = f"LAY_{layout_counter:03d}"
            layout_counter += 1

            # Allocate slot sizes to rack-columns under style-specific ordering rules.
            feasible_layout, assigned_exact, used_by_column, column_assignments, note = common._allocate_layout_by_column(
                target_exact_counts=base_exact_counts,
                column_keys=layout_columns,
                style=style,
            )

            min_slot_size = min(base_exact_counts)
            column_assignments, used_by_column = _expand_layout_capacity(
                column_assignments=column_assignments,
                used_by_column=used_by_column,
                column_keys=layout_columns,
                min_slot_size=min_slot_size,
                style=style,
            )

            generated_location_rows = common._build_generated_layout_location_rows(
                layout_id=layout_id,
                config_id=config_id,
                style=style,
                column_assignments=column_assignments,
            )
            layout_signature = _layout_signature(generated_location_rows)
            proposed_beam_units = common._build_proposed_beam_units_from_layout_rows(generated_location_rows, beam_segments)
            relocation_total, relocation_by_column = common._beam_relocations(current_beam_units, proposed_beam_units)
            additional_beams, additional_grids, removed_beams = common._material_requirements(current_beam_units, proposed_beam_units)
            _ = removed_beams

            # Compute utilization and change-effort KPIs for ranking downstream.
            assigned_total = sum(len(slots) for slots in column_assignments.values())
            required_locations_total = sum(base_exact_counts.values())
            total_used_height = sum(used_by_column.values())
            total_allowed_height = sum(
                common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(len(slots) - 1, common.MIN_BEAMS_PER_COLUMN)
                for slots in column_assignments.values()
            )
            space_left = sum(
                max(
                    (common.MAX_USED_HEIGHT_BASE - common.BEAM_HEIGHT * max(len(slots) - 1, common.MIN_BEAMS_PER_COLUMN))
                    - used_by_column.get(column_key, 0.0),
                    0.0,
                )
                for column_key, slots in column_assignments.items()
            )

            summary_row = {
                "Layout_ID": layout_id,
                "Config_ID": config_id,
                "Style": style,
                "Layout_Feasible": "YES" if feasible_layout else "NO",
                "Required_Locations_Total": str(required_locations_total),
                "Assigned_Locations_Total": str(assigned_total),
                "Total_Physical_Locations": str(total_physical_locations),
                "Assigned_Used_Height_Total": f"{total_used_height:.3f}",
                "Total_Allowed_Height": f"{total_allowed_height:.3f}",
                "Space_Left": f"{space_left:.3f}",
                "Beam_Relocations_Total": str(relocation_total),
                "Additional_Beams_Required": str(additional_beams),
                "Additional_Grids_Required": str(additional_grids),
                "Worst_Case_Exact_Counts": "|".join(f"{int(size)}:{count}" for size, count in sorted(base_exact_counts.items())),
                "Slot_Composition_Signature": "|".join(f"{int(size)}:{count}" for size, count in sorted(base_exact_counts.items())),
                "Layout_Slot_Size_Distribution": _slot_distribution_signature(column_assignments),
                "Source_Slot_Sizes": ",".join(f"{int(size)}" for size in config_slot_sizes),
                "Notes": note,
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
                        "Style": style,
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

        unique_signatures = {candidate[3] for candidate in style_candidates}
        if len(unique_signatures) == 1:
            chosen = min(
                style_candidates,
                key=lambda candidate: _style_rank(str(candidate[0].get("Style", ""))),
            )
            diversity_label = "NO_STYLE_DIFFERENCE"
            selection_reason = "All optimization styles produced the same layout; kept priority style"
        else:
            chosen = min(
                style_candidates,
                key=lambda candidate: _pre_robust_sort_key(candidate[0]),
            )
            diversity_label = "STYLE_DIFFERENCE_FOUND"
            selection_reason = "Styles differed; kept best pre-robust score by relocations, materials, and space left"

        chosen_summary = dict(chosen[0])
        chosen_summary["Optimization_Diversity"] = diversity_label
        chosen_summary["Style_Selection_Reason"] = selection_reason
        chosen_summary["Pre_Robustness_Status"] = "PENDING"
        chosen_summary["Pre_Robustness_Rank"] = ""
        chosen_summary["Pre_Robustness_Prune_Reason"] = ""
        selected_per_config.append((chosen_summary, list(chosen[1]), list(chosen[2])))

    if selected_per_config:
        by_composition: dict[str, list[SelectedCandidate]] = defaultdict(list)
        for entry in selected_per_config:
            summary = entry[0]
            signature = str(summary.get("Slot_Composition_Signature", "")).strip()
            by_composition[signature].append(entry)

        composition_winners: list[SelectedCandidate] = []
        for entries in by_composition.values():
            winner = min(entries, key=lambda entry: _pre_robust_sort_key(entry[0]))
            composition_winners.append(winner)

        target_count = min(PRE_ROBUST_LAYOUT_LIMIT, len(selected_per_config))
        finalists = sorted(composition_winners, key=lambda entry: _pre_robust_sort_key(entry[0]))[:target_count]

        finalist_ids = {
            str(entry[0].get("Layout_ID", "")).strip()
            for entry in finalists
        }
        if len(finalists) < target_count:
            ranked_remaining = sorted(
                [
                    entry
                    for entry in selected_per_config
                    if str(entry[0].get("Layout_ID", "")).strip() not in finalist_ids
                ],
                key=lambda entry: _pre_robust_sort_key(entry[0]),
            )
            needed = target_count - len(finalists)
            finalists.extend(ranked_remaining[:needed])

        finalists = sorted(finalists, key=lambda entry: _pre_robust_sort_key(entry[0]))
        finalist_ids = {
            str(entry[0].get("Layout_ID", "")).strip()
            for entry in finalists
        }
        finalist_rank: dict[str, int] = {
            str(entry[0].get("Layout_ID", "")).strip(): index
            for index, entry in enumerate(finalists, start=1)
        }

        for entry in selected_per_config:
            summary = entry[0]
            layout_id = str(summary.get("Layout_ID", "")).strip()
            if layout_id in finalist_ids:
                summary["Pre_Robustness_Status"] = "SELECTED"
                summary["Pre_Robustness_Rank"] = str(finalist_rank.get(layout_id, ""))
                summary["Pre_Robustness_Prune_Reason"] = ""
                candidate_layout_column_rows.extend(entry[2])
                candidate_layout_location_rows.extend(entry[1])
            else:
                summary["Pre_Robustness_Status"] = "PRUNED"
                summary["Pre_Robustness_Rank"] = ""
                summary["Pre_Robustness_Prune_Reason"] = "Outside pre-robust top set after composition-aware comparison"

            candidate_layout_rows.append({key: str(value) for key, value in summary.items()})

    # Write layout-level KPIs.
    common._write_csv_clean(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary.csv",
        [
            "Layout_ID",
            "Config_ID",
            "Style",
            "Optimization_Diversity",
            "Style_Selection_Reason",
            "Pre_Robustness_Status",
            "Pre_Robustness_Rank",
            "Pre_Robustness_Prune_Reason",
            "Layout_Feasible",
            "Required_Locations_Total",
            "Assigned_Locations_Total",
            "Total_Physical_Locations",
            "Assigned_Used_Height_Total",
            "Total_Allowed_Height",
            "Space_Left",
            "Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Additional_Grids_Required",
            "Worst_Case_Exact_Counts",
            "Slot_Composition_Signature",
            "Layout_Slot_Size_Distribution",
            "Source_Slot_Sizes",
            "Notes",
        ],
        candidate_layout_rows,
    )

    # Write rack-column level breakdown.
    common._write_csv_clean(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_By_Rack_Column.csv",
        [
            "Layout_ID",
            "Config_ID",
            "Style",
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
            "Style",
            "Location",
            "Rack",
            "Column",
            "Row",
            "Beam_Coordinate",
            "Assignment_Unit_ID",
            "Assignment_Unit_Type",
            "Assigned_Slot_Size_cm",
        ],
        candidate_layout_location_rows,
    )

    return candidate_layout_rows, candidate_layout_column_rows, candidate_layout_location_rows


if __name__ == "__main__":
    # Stage 6 entrypoint: build and persist all candidate layouts.
    layout_rows, column_rows, location_rows = build_layout_generation()
    print(
        "Layout generation complete. "
        f"Layouts: {len(layout_rows)}, columns: {len(column_rows)}, locations: {len(location_rows)}."
    )
