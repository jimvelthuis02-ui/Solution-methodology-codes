"""Stage 6 space-flexibility layout variant.

This variant reuses the baseline Stage 6 implementation and replaces only the
rack-profile assignment objective with maximum total slot space.
"""

import csv
import importlib.util
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from heuristic_output_utils import suffix_output_tree

BASELINE_SCRIPT = PIPELINE_ROOT / "06_Layout_Generation" / "06_layout_generation.py"
MAX_REFINEMENT_SWAPS_PER_RACK = 3


def _load_baseline_module() -> Any:
    spec = importlib.util.spec_from_file_location("stage6_baseline", BASELINE_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load baseline Stage 6 module from {BASELINE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cached_feasible_assignment(
    stage6: Any,
    config_id: str,
    rack_columns: list[str],
    required: dict[float, int],
    slot_sizes: object,
) -> dict[str, list[float]] | None:
    output_dir = stage6.common.STAGE6_OUTPUT_DIR
    summary_path = output_dir / "Candidate_Layout_Summary.csv"
    locations_path = output_dir / "Candidate_Layout_By_Location.csv"
    if not summary_path.exists() or not locations_path.exists():
        return None

    summaries = stage6._read_csv(summary_path)
    matching = next(
        (
            row for row in summaries
            if str(row.get("Config_ID", "")).strip() == config_id
            and str(row.get("Layout_Feasible", "")).strip().upper() == "YES"
        ),
        None,
    )
    if matching is None:
        return None

    assignments: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for row in stage6._read_csv(locations_path):
        if str(row.get("Config_ID", "")).strip() != config_id:
            continue
        if str(row.get("Usable_Location", "YES")).strip().upper() == "NO":
            continue
        try:
            row_index = int(str(row.get("Row", "0")).strip())
            slot_size = float(str(row.get("Assigned_Slot_Size_cm", "")).strip())
            column_index = str(row.get("Column", "")).strip()
        except ValueError:
            continue
        rack = str(row.get("Rack", "")).strip()
        if rack and column_index and slot_size > 0:
            assignments[f"{rack}{int(column_index):02d}"].append((row_index, slot_size))

    ordered_assignments = {
        column: [size for _row, size in sorted(values)]
        for column, values in assignments.items()
        if values
    }
    if not ordered_assignments or not stage6._layout_assignments_are_feasible(
        ordered_assignments,
        list(ordered_assignments),
        float(min(slot_sizes or required)),
        slot_sizes,
        minimum_required_counts=required,
        enforce_minimum_total_locations=True,
    ):
        return None

    return ordered_assignments


def _cached_profiles(stage6: Any, config_id: str, slot_sizes: object) -> list[list[float]]:
    profiles_path = stage6.common.STAGE6_OUTPUT_DIR / "Generated_Profiles_By_Config.csv"
    if not profiles_path.exists():
        return []
    profiles: list[list[float]] = []
    seen: set[tuple[float, ...]] = set()
    for row in stage6._read_csv(profiles_path):
        if str(row.get("Config_ID", "")).strip() != config_id:
            continue
        if str(row.get("Status", "GENERATED")).strip().upper() != "GENERATED":
            continue
        try:
            profile = [float(value) for value in str(row.get("Profile_Values", "")).split(",") if value.strip()]
        except ValueError:
            continue
        key = tuple(profile)
        if key in seen or not stage6._profile_is_feasible_exact_fill(profile, slot_sizes):
            continue
        seen.add(key)
        profiles.append(profile)
    return profiles


def _counted_space(stage6: Any, profile: list[float], slot_sizes: object) -> float:
    # Results count a topfill as its slot-size family, so score space the same way.
    total = 0.0
    for value in profile:
        family = stage6._effective_requirement_slot_size(value, profile, slot_sizes)
        total += float(family if family is not None else value)
    return total


def _space_maximizing_assignment(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: object,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]]:
    """Assign one legal profile per rack while maximizing total slot space."""
    stage6 = _space_maximizing_assignment.stage6
    if not rack_columns:
        return {}

    required = {float(size): int(count) for size, count in required_counts.items()}
    generation_start = time.perf_counter()
    assignments = _cached_feasible_assignment(stage6, str(config_id or "").strip(), rack_columns, required, config_slot_sizes)
    profiles = _cached_profiles(stage6, str(config_id or "").strip(), config_slot_sizes)
    if assignments is None or not profiles:
        assignments = _space_maximizing_assignment.baseline_builder(
            rack_columns,
            required,
            config_slot_sizes,
            config_deadline=config_deadline,
            config_id=config_id,
        )
        profiles = [list(profile) for profile in stage6._LAST_STAGE6_PROFILE_POOL]
    if not assignments or not profiles:
        return assignments

    sizes = sorted(required)
    best_profile_by_effect: dict[tuple[int, ...], tuple[list[float], float, dict[float, int]]] = {}
    for profile in profiles:
        raw_effect = stage6._effective_requirement_counts(profile, config_slot_sizes)
        effect = tuple(int(raw_effect.get(int(size), 0)) for size in sizes)
        effect_by_size = {size: int(raw_effect.get(int(size), 0)) for size in sizes}
        profile_space = _counted_space(stage6, profile, config_slot_sizes)
        previous = best_profile_by_effect.get(effect)
        if previous is None or profile_space > previous[1]:
            best_profile_by_effect[effect] = (list(profile), profile_space, effect_by_size)
    profile_options = list(best_profile_by_effect.values())

    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[stage6._rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns)
    assigned_counts = {size: 0 for size in sizes}
    current_profiles: dict[str, list[float]] = {}
    for rack in rack_order:
        columns = rack_to_columns[rack]
        profile = list(assignments[columns[0]]) if columns else []
        current_profiles[rack] = profile
        effect = stage6._effective_requirement_counts(profile, config_slot_sizes)
        for size in sizes:
            assigned_counts[size] += int(effect.get(int(size), 0)) * len(columns)

    if any(assigned_counts[size] < required[size] for size in sizes):
        stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - generation_start
        return assignments

    for _ in range(max(len(rack_order) * MAX_REFINEMENT_SWAPS_PER_RACK, 1)):
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            break
        best_swap: tuple[float, str, list[float], dict[int, int]] | None = None
        for rack in rack_order:
            width = len(rack_to_columns[rack])
            current_profile = current_profiles[rack]
            current_effect_raw = stage6._effective_requirement_counts(current_profile, config_slot_sizes)
            current_effect = {size: int(current_effect_raw.get(int(size), 0)) for size in sizes}
            current_space = _counted_space(stage6, current_profile, config_slot_sizes)
            for candidate, candidate_space, candidate_effect in profile_options:
                gain = (candidate_space - current_space) * width
                if gain <= 1e-9:
                    continue
                if any(
                    assigned_counts[size] + (candidate_effect[size] - current_effect[size]) * width < required[size]
                    for size in sizes
                ):
                    continue
                if best_swap is None or gain > best_swap[0]:
                    best_swap = (gain, rack, candidate, candidate_effect)
        if best_swap is None:
            break

        _gain, rack, candidate, candidate_effect = best_swap
        current_profile = current_profiles[rack]
        current_raw = stage6._effective_requirement_counts(current_profile, config_slot_sizes)
        current_effect = {size: int(current_raw.get(int(size), 0)) for size in sizes}
        width = len(rack_to_columns[rack])
        for size in sizes:
            assigned_counts[size] += (candidate_effect[size] - current_effect[size]) * width
        current_profiles[rack] = candidate
        for column_key in rack_to_columns[rack]:
            assignments[column_key] = list(candidate)

    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - generation_start
    return assignments


_space_maximizing_assignment.stage6 = None
_space_maximizing_assignment.baseline_builder = None


def _parse_size_counts(value: str) -> dict[int, int]:
    counts: dict[int, int] = {}
    for token in str(value).split("|"):
        if ":" not in token:
            continue
        size_text, count_text = token.split(":", 1)
        try:
            size = int(round(float(size_text)))
            count = int(round(float(count_text)))
        except ValueError:
            continue
        if size > 0 and count > 0:
            counts[size] = counts.get(size, 0) + count
    return counts


def _add_space_metrics(output_dir: Path) -> None:
    summary_path = output_dir / "Candidate_Layout_Summary.csv"
    with summary_path.open("r", newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
        fields = list(rows[0].keys()) if rows else []

    metric_fields = [
        "Occupied_Slot_Space_cm",
        "Total_Slot_Space_cm",
        "Empty_Slot_Space_cm",
        "Space_Utilization_Pct",
    ]
    for field in metric_fields:
        if field not in fields:
            fields.append(field)

    for row in rows:
        occupied_counts = _parse_size_counts(row.get("Minimum_Required_Counts", ""))
        total_counts = _parse_size_counts(row.get("Layout_Slot_Size_Distribution", ""))
        occupied_space = sum(size * count for size, count in occupied_counts.items())
        total_space = sum(size * count for size, count in total_counts.items())
        empty_space = max(total_space - occupied_space, 0)
        utilization = (occupied_space / total_space * 100.0) if total_space else 0.0
        row["Occupied_Slot_Space_cm"] = f"{occupied_space:.3f}"
        row["Total_Slot_Space_cm"] = f"{total_space:.3f}"
        row["Empty_Slot_Space_cm"] = f"{empty_space:.3f}"
        row["Space_Utilization_Pct"] = f"{utilization:.6f}"

    with summary_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    stage6 = _load_baseline_module()
    _space_maximizing_assignment.stage6 = stage6

    output_dir = Path(
        os.environ.get(
            "PIPELINE_SPACE_UTILIZATION_OUTPUT_DIR",
            stage6.common.OUTPUT_ROOT / "06_Layout_Generation_Space_Utilization",
        )
    ).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stage6.LAYOUT_OUTPUT_DIR = output_dir
    stage6.IMPLEMENTATION_STYLE = "space_utilization"
    stage6.STYLE_PRIORITY = ("space_utilization",)
    _space_maximizing_assignment.baseline_builder = stage6._build_deficit_coverage_layout
    stage6._build_deficit_coverage_layout = _space_maximizing_assignment

    print(f"[Stage 6 Space] output directory: {output_dir}")
    layout_rows, column_rows, location_rows = stage6.build_layout_generation()
    _add_space_metrics(output_dir)
    suffix_output_tree(output_dir, "SU")
    print(
        "[Stage 6 Space] complete. "
        f"Layouts: {len(layout_rows)}, columns: {len(column_rows)}, locations: {len(location_rows)}."
    )


if __name__ == "__main__":
    main()
