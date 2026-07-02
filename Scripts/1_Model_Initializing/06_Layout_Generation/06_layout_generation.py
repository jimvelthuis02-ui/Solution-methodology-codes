import csv
from collections import defaultdict
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stage_pipeline_common import load_legacy_pipeline


legacy = load_legacy_pipeline()
INPUT_CONFIG_FILE = legacy.CONSTRAINT_OUTPUT_DIR / "Candidate_Configurations.csv"
INPUT_CAPACITY_FILE = legacy.CONSTRAINT_OUTPUT_DIR / "Constraint_Location_Counts_By_Slot_Size.csv"
INPUT_PREPARED = legacy.ROOT / "Output" / "01_Data_Preparation" / "Location_Details_Prepared.csv"
INPUT_LOCATION_BEAM_MAP = legacy.ROOT / "Output" / "01_Data_Preparation" / "Beam_Grid_Mapping" / "Location_Beam_Map.csv"
LAYOUT_OUTPUT_DIR = legacy.LAYOUT_OUTPUT_DIR


def _read_csv(path: object) -> list[dict[str, str]]:
    return legacy._read_csv(path)


def _shortlisted_configs() -> list[dict[str, str]]:
    configs = _read_csv(INPUT_CONFIG_FILE)
    return [row for row in configs if str(row.get("Selection_Status", "")).strip() == "SHORTLISTED"]


def _capacity_rows_by_config() -> dict[str, list[dict[str, str]]]:
    rows = _read_csv(INPUT_CAPACITY_FILE)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("Config_ID", "")).strip()].append(row)
    return grouped


def _slot_sizes_from_capacity(rows: list[dict[str, str]]) -> list[float]:
    sizes = {
        legacy._to_float(row.get("Representative_Slot_Size"))
        for row in rows
        if legacy._to_float(row.get("Representative_Slot_Size")) is not None
    }
    return sorted(float(value) for value in sizes if value is not None)


def _worst_case_exact_counts(rows: list[dict[str, str]]) -> dict[float, int]:
    cumulative_by_size: dict[float, int] = defaultdict(int)
    for row in rows:
        slot_size = legacy._to_float(row.get("Representative_Slot_Size"))
        required = legacy._to_float(
            row.get("Min_Required_Locations_At_Or_Above_Size")
            or row.get("Cumulative_Assigned_SKUs_At_Or_Above_Size")
        )
        if slot_size is None or required is None:
            continue
        cumulative_by_size[slot_size] = max(cumulative_by_size.get(slot_size, 0), int(round(required)))

    ordered_sizes = sorted(cumulative_by_size)
    exact_counts: dict[float, int] = {}
    for index, slot_size in enumerate(ordered_sizes):
        next_size = ordered_sizes[index + 1] if index + 1 < len(ordered_sizes) else None
        next_required = cumulative_by_size.get(next_size, 0) if next_size is not None else 0
        exact_counts[slot_size] = max(cumulative_by_size[slot_size] - next_required, 0)

    return exact_counts


def build_layout_generation() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    prepared_rows = _read_csv(INPUT_PREPARED)
    beam_map_rows = _read_csv(INPUT_LOCATION_BEAM_MAP)
    configs = _shortlisted_configs()
    capacity_rows = _capacity_rows_by_config()
    layout_columns = legacy._build_layout_columns(prepared_rows)
    current_beam_units, beam_segments = legacy._build_current_beam_units_and_segments(beam_map_rows)
    total_physical_locations = len(
        [
            row
            for row in prepared_rows
            if str(row.get("Location Type", "")).strip().lower() != "doorgang"
            and not legacy._is_split_location(str(row.get("Location", "")).strip())
        ]
    )

    candidate_layout_rows: list[dict[str, str]] = []
    candidate_layout_location_rows: list[dict[str, str]] = []
    candidate_layout_column_rows: list[dict[str, str]] = []

    layout_counter = 1
    for config in configs:
        config_id = str(config.get("Config_ID", "")).strip()
        rows = capacity_rows.get(config_id, [])
        if not rows:
            continue

        worst_case_exact_counts = _worst_case_exact_counts(rows)
        if not worst_case_exact_counts:
            continue

        config_slot_sizes = _slot_sizes_from_capacity(rows)
        slot_size_distribution = ",".join(f"{int(size)}:{count}" for size, count in sorted(worst_case_exact_counts.items()))

        for style in legacy.CANDIDATE_LAYOUT_STYLES:
            layout_id = f"LAY_{layout_counter:03d}"
            layout_counter += 1

            feasible_layout, assigned_exact, used_by_column, column_assignments, note = legacy._allocate_layout_by_column(
                target_exact_counts=worst_case_exact_counts,
                column_keys=layout_columns,
                style=style,
            )

            generated_location_rows = legacy._build_generated_layout_location_rows(
                layout_id=layout_id,
                config_id=config_id,
                style=style,
                column_assignments=column_assignments,
            )
            proposed_beam_units = legacy._build_proposed_beam_units_from_layout_rows(generated_location_rows, beam_segments)
            relocation_total, relocation_by_column = legacy._beam_relocations(current_beam_units, proposed_beam_units)
            additional_beams, additional_grids, removed_beams = legacy._material_requirements(current_beam_units, proposed_beam_units)

            assigned_total = sum(assigned_exact.values())
            required_locations_total = sum(worst_case_exact_counts.values())
            total_used_height = sum(used_by_column.values())
            total_allowed_height = sum(
                legacy.MAX_USED_HEIGHT_BASE - legacy.BEAM_HEIGHT * max(len(slots) - 1, legacy.MIN_BEAMS_PER_COLUMN)
                for slots in column_assignments.values()
            )
            space_left = sum(
                max(
                    (legacy.MAX_USED_HEIGHT_BASE - legacy.BEAM_HEIGHT * max(len(slots) - 1, legacy.MIN_BEAMS_PER_COLUMN))
                    - used_by_column.get(column_key, 0.0),
                    0.0,
                )
                for column_key, slots in column_assignments.items()
            )

            candidate_layout_rows.append(
                {
                    "Layout_ID": layout_id,
                    "Config_ID": config_id,
                    "Style": style,
                    "Layout_Feasible": "YES" if feasible_layout and assigned_total == required_locations_total else "NO",
                    "Required_Locations_Total": str(required_locations_total),
                    "Assigned_Locations_Total": str(assigned_total),
                    "Total_Physical_Locations": str(total_physical_locations),
                    "Assigned_Used_Height_Total": f"{total_used_height:.3f}",
                    "Total_Allowed_Height": f"{total_allowed_height:.3f}",
                    "Space_Left": f"{space_left:.3f}",
                    "Beam_Relocations_Total": str(relocation_total),
                    "Additional_Beams_Required": str(additional_beams),
                    "Additional_Grids_Required": str(additional_grids),
                    "Worst_Case_Exact_Counts": "|".join(f"{int(size)}:{count}" for size, count in sorted(worst_case_exact_counts.items())),
                    "Source_Slot_Sizes": ",".join(f"{int(size)}" for size in config_slot_sizes),
                    "Notes": note,
                }
            )

            slot_mix_by_column: dict[str, dict[float, int]] = defaultdict(lambda: defaultdict(int))
            for row in generated_location_rows:
                rack = str(row.get("Rack", "")).strip()
                column = str(row.get("Column", "")).strip()
                slot = legacy._to_float(row.get("Assigned_Slot_Size_cm"))
                if rack and column and slot is not None:
                    slot_mix_by_column[f"{rack}{column}"][slot] += 1

            for column_key, slots in sorted(column_assignments.items()):
                used = used_by_column.get(column_key, 0.0)
                beam_count = max(len(slots) - 1, legacy.MIN_BEAMS_PER_COLUMN)
                allowed = legacy.MAX_USED_HEIGHT_BASE - beam_count * legacy.BEAM_HEIGHT
                mix = slot_mix_by_column.get(column_key, {})
                candidate_layout_column_rows.append(
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

            candidate_layout_location_rows.extend(generated_location_rows)

    legacy._write_csv_clean(
        LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary.csv",
        [
            "Layout_ID",
            "Config_ID",
            "Style",
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
            "Source_Slot_Sizes",
            "Notes",
        ],
        candidate_layout_rows,
    )

    legacy._write_csv_clean(
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

    legacy._write_csv_clean(
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
    layout_rows, column_rows, location_rows = build_layout_generation()
    print(
        "Layout generation complete. "
        f"Layouts: {len(layout_rows)}, columns: {len(column_rows)}, locations: {len(location_rows)}."
    )
