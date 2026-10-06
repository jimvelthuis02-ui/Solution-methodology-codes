"""Stage 6 picking-efficiency layout variant."""

import csv
import heapq
import importlib.util
import itertools
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
PICKING_INPUT_FILE = PIPELINE_ROOT.parent.parent / "Input files" / "Locations" / "Most frequently picked items heights 24-9 good.csv"
PROFILE_ORDERING_LIMIT = 10000
MAX_SEARCH_STATES = 256
MAX_PROFILE_OPTIONS = 256


def _state_rank(item: tuple[tuple[int, ...], tuple[float, tuple[int, ...]]]) -> tuple[object, ...]:
    remaining, (score, _choices) = item
    return (
        sum(value <= 0 for value in remaining),
        -sum(max(value, 0) for value in remaining),
        tuple(-value for value in remaining),
        score,
    )


def _prune_states(
    states: dict[tuple[int, ...], tuple[float, tuple[int, ...]]],
) -> dict[tuple[int, ...], tuple[float, tuple[int, ...]]]:
    if len(states) <= MAX_SEARCH_STATES:
        return states
    return dict(heapq.nlargest(MAX_SEARCH_STATES, states.items(), key=_state_rank))


def _load_baseline_module() -> Any:
    spec = importlib.util.spec_from_file_location("stage6_baseline_picking", BASELINE_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load baseline Stage 6 module from {BASELINE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _picking_demand(slot_sizes: list[float]) -> tuple[dict[int, int], int, int, int, int]:
    demand: dict[int, int] = defaultdict(int)
    covered_items = 0
    covered_picks = 0
    uncovered_items = 0
    uncovered_picks = 0
    if not PICKING_INPUT_FILE.exists():
        return {}, 0, 0, 0, 0

    with PICKING_INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            try:
                item_height = float(str(row.get("Item Height", "")).strip())
                picks = int(float(str(row.get("Count of Assembly", "0")).strip()))
            except ValueError:
                continue
            if item_height <= 0 or picks < 0:
                continue
            fitting_sizes = [int(round(size)) for size in slot_sizes if size >= item_height]
            if not fitting_sizes:
                uncovered_items += 1
                uncovered_picks += picks
                continue
            assigned_size = min(fitting_sizes)
            demand[assigned_size] += picks
            covered_items += 1
            covered_picks += picks

    return dict(demand), covered_items, covered_picks, uncovered_items, uncovered_picks


def _profile_is_feasible_unordered(profile: list[float], slot_sizes: list[float], stage6: Any) -> bool:
    slots = [float(value) for value in profile if float(value) > 0.0]
    if not slots:
        return False
    if any(int(round(value)) > int(round(stage6.common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)) for value in slots):
        return False
    if any(int(round(value)) % 10 not in (4, 9) for value in slots):
        return False
    physical_total = sum(slots) + (len(slots) - 1) * stage6.common.BEAM_HEIGHT
    if abs(physical_total - stage6.common.MAX_USED_HEIGHT_BASE) > 1e-9:
        return False

    config_values = set(stage6._config_size_values(slot_sizes))
    legal_topfills = set(stage6._legal_topfill_values(slot_sizes))
    lower_slots = [int(round(value)) for value in slots[:-1]]
    if any(value not in config_values for value in lower_slots):
        return False

    final_slot = int(round(slots[-1]))
    if final_slot in config_values:
        return True
    if final_slot not in legal_topfills:
        return False
    effective_final = stage6._effective_requirement_slot_size(final_slot, slots, slot_sizes)
    return effective_final in config_values


def _profile_picking_score(profile: list[float], demand: dict[int, int], slot_sizes: list[float], stage6: Any) -> float:
    if not profile:
        return 0.0
    score = 0.0
    profile_length = len(profile)
    for index, value in enumerate(profile):
        family = stage6._effective_requirement_slot_size(value, profile, slot_sizes)
        picks = demand.get(int(family), 0) if family is not None else 0
        position_quality = (profile_length - index) / profile_length
        score += picks * position_quality
    return score


def _generate_ordered_profiles(slot_sizes: list[float], demand: dict[int, int], stage6: Any, deadline: float | None) -> list[list[float]]:
    family = sorted(set(stage6._config_size_values(slot_sizes)))
    if not family:
        return []
    legal_topfills = stage6._legal_topfill_values(family)
    max_lower_rows = max(1, min(18, int(stage6.common.MAX_USED_HEIGHT_BASE // (min(family) + stage6.common.BEAM_HEIGHT))))
    profiles: list[list[float]] = []
    seen: set[tuple[int, ...]] = set()
    combination_checks = 0

    for lower_count in range(1, max_lower_rows + 1):
        if deadline is not None and time.perf_counter() >= deadline:
            raise stage6.Stage6ProfileGenerationTimeout("picking profile generation exceeded the config deadline")
        for lower_combo in itertools.combinations_with_replacement(family, lower_count):
            combination_checks += 1
            if combination_checks % 256 == 0 and deadline is not None and time.perf_counter() >= deadline:
                raise stage6.Stage6ProfileGenerationTimeout("picking profile generation exceeded the config deadline")
            lower_stack = tuple(sorted(lower_combo, reverse=True))
            support_height = sum(lower_stack) + (len(lower_stack) - 1) * stage6.common.BEAM_HEIGHT
            if support_height < 504.0 - 1e-9:
                continue
            topfill = stage6.common.MAX_USED_HEIGHT_BASE - sum(lower_stack) - stage6.common.BEAM_HEIGHT * len(lower_stack)
            if topfill <= 0.0 or topfill > 214.0:
                continue
            rounded_topfill = int(round(topfill))
            if rounded_topfill not in family and rounded_topfill not in legal_topfills:
                continue

            if rounded_topfill in family:
                ordered_candidates = itertools.permutations((*lower_stack, rounded_topfill))
            else:
                ordered_candidates = (
                    (*ordering, float(rounded_topfill))
                    for ordering in itertools.permutations(lower_stack)
                )

            for candidate in ordered_candidates:
                if len(profiles) >= PROFILE_ORDERING_LIMIT:
                    break
                normalized = tuple(int(round(value)) for value in candidate)
                if normalized in seen:
                    continue
                candidate_values = [float(value) for value in normalized]
                if not _profile_is_feasible_unordered(candidate_values, slot_sizes, stage6):
                    continue
                seen.add(normalized)
                profiles.append(candidate_values)
            if len(profiles) >= PROFILE_ORDERING_LIMIT:
                break
        if len(profiles) >= PROFILE_ORDERING_LIMIT:
            break

    return sorted(
        profiles,
        key=lambda profile: _profile_picking_score(profile, demand, slot_sizes, stage6),
        reverse=True,
    )


def _picking_assignment(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: object,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]]:
    stage6 = _picking_assignment.stage6
    if not rack_columns:
        return {}
    slot_sizes = [float(value) for value in (config_slot_sizes or list(required_counts))]
    demand, *_ = _picking_demand(slot_sizes)
    generation_start = time.perf_counter()
    profiles = _generate_ordered_profiles(slot_sizes, demand, stage6, config_deadline)
    stage6._LAST_STAGE6_PROFILE_POOL = [list(profile) for profile in profiles]
    stage6._LAST_STAGE6_STEP_TIMINGS["profile_generation"] = time.perf_counter() - generation_start
    stage6._LAST_STAGE6_STEP_TIMINGS["profile_shortlist"] = 0.0
    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = 0.0
    if not profiles:
        return _picking_assignment.baseline_builder(
            rack_columns,
            required_counts,
            config_slot_sizes,
            config_deadline=config_deadline,
            config_id=config_id,
        )

    required = {float(size): int(count) for size, count in required_counts.items()}
    ordered_sizes = sorted(required, reverse=True)
    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[stage6._rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns)

    best_effect: dict[tuple[int, ...], tuple[list[float], float]] = {}
    for profile in profiles:
        counts = stage6._effective_requirement_counts(profile, slot_sizes)
        effect = tuple(int(counts.get(int(size), 0)) for size in ordered_sizes)
        score = _profile_picking_score(profile, demand, slot_sizes, stage6)
        previous = best_effect.get(effect)
        if previous is None or score > previous[1]:
            best_effect[effect] = (list(profile), score)
    ranked_effects = sorted(
        best_effect.items(),
        key=lambda item: (
            sum(min(item[0][index], int(required[size])) > 0 for index, size in enumerate(ordered_sizes)),
            sum(min(item[0][index], int(required[size])) for index, size in enumerate(ordered_sizes)),
            sum(min(item[0][index], int(required[size])) * size for index, size in enumerate(ordered_sizes)),
            item[1][1],
        ),
        reverse=True,
    )[:MAX_PROFILE_OPTIONS]
    profile_options = [(profile, effect, score) for effect, (profile, score) in ranked_effects]

    states: dict[tuple[int, ...], tuple[float, tuple[int, ...]]] = {
        tuple(int(required[size]) for size in ordered_sizes): (0.0, ())
    }
    for rack in rack_order:
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            raise stage6.Stage6RackSearchTimeout("picking rack search exceeded the config deadline")
        rack_width = len(rack_to_columns[rack])
        next_states: dict[tuple[int, ...], tuple[float, tuple[int, ...]]] = {}
        for remaining, (score, choices) in states.items():
            for profile_index, (_profile, effect, profile_score) in enumerate(profile_options):
                next_remaining = tuple(
                    max(remaining[index] - effect[index] * rack_width, 0)
                    for index in range(len(ordered_sizes))
                )
                next_value = (score + profile_score * rack_width, (*choices, profile_index))
                previous = next_states.get(next_remaining)
                if previous is None or next_value[0] > previous[0]:
                    next_states[next_remaining] = next_value
                if len(next_states) > MAX_SEARCH_STATES * 2:
                    next_states = _prune_states(next_states)
        states = _prune_states(next_states)

    chosen = max(states.items(), key=_state_rank)
    choices = chosen[1][1]
    assignments: dict[str, list[float]] = {}
    for rack_index, rack in enumerate(rack_order):
        profile_index = choices[rack_index] if rack_index < len(choices) else 0
        profile = profile_options[profile_index][0]
        for column_key in sorted(rack_to_columns[rack]):
            assignments[column_key] = list(profile)
    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - generation_start
    if not stage6._layout_assignments_are_feasible(
        assignments,
        list(assignments),
        float(min(slot_sizes)),
        slot_sizes,
        minimum_required_counts=required,
        enforce_minimum_total_locations=True,
    ):
        return _picking_assignment.baseline_builder(
            rack_columns,
            required,
            config_slot_sizes,
            config_deadline=config_deadline,
            config_id=config_id,
        )
    return assignments


_picking_assignment.stage6 = None
_picking_assignment.baseline_builder = None


def _add_picking_metrics(output_dir: Path, stage6: Any) -> None:
    summary_path = output_dir / "Candidate_Layout_Summary.csv"
    locations_path = output_dir / "Candidate_Layout_By_Location.csv"
    with summary_path.open("r", newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
        fields = list(rows[0].keys()) if rows else []
    with locations_path.open("r", newline="", encoding="utf-8-sig") as source:
        locations = list(csv.DictReader(source))

    for field in (
        "Picking_Covered_Item_Count",
        "Picking_Covered_Pick_Count",
        "Picking_Uncovered_Item_Count",
        "Picking_Uncovered_Pick_Count",
        "Picking_Score",
    ):
        if field not in fields:
            fields.append(field)

    location_groups: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for location in locations:
        location_groups[str(location.get("Config_ID", ""))][
            f"{location.get('Rack', '')}{location.get('Column', '')}"
        ].append(location)

    for row in rows:
        config_id = str(row.get("Config_ID", ""))
        slot_sizes = [float(value) for value in str(row.get("Source_Slot_Sizes", "")).replace('="', '').replace('"', '').split(",") if value.strip()]
        demand, covered_items, covered_picks, uncovered_items, uncovered_picks = _picking_demand(slot_sizes)
        score = 0.0
        for column_rows in location_groups.get(config_id, {}).values():
            ordered = sorted(column_rows, key=lambda item: int(item.get("Row", "0") or 0))
            profile_values = [float(item.get("Assigned_Slot_Size_cm", "0") or 0) for item in ordered]
            for index, location in enumerate(ordered):
                size = float(location.get("Assigned_Slot_Size_cm", "0") or 0)
                family = stage6._effective_requirement_slot_size(size, profile_values, slot_sizes)
                score += demand.get(int(family), 0) * ((len(ordered) - index) / len(ordered)) if family is not None else 0.0
        row["Picking_Covered_Item_Count"] = str(covered_items)
        row["Picking_Covered_Pick_Count"] = str(covered_picks)
        row["Picking_Uncovered_Item_Count"] = str(uncovered_items)
        row["Picking_Uncovered_Pick_Count"] = str(uncovered_picks)
        row["Picking_Score"] = f"{score:.6f}"

    with summary_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    stage6 = _load_baseline_module()
    _picking_assignment.stage6 = stage6
    output_dir = Path(
        os.environ.get(
            "PIPELINE_PICKING_EFFICIENCY_OUTPUT_DIR",
            stage6.common.OUTPUT_ROOT / "06_Layout_Generation_Picking_Efficiency",
        )
    ).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stage6.LAYOUT_OUTPUT_DIR = output_dir
    stage6.IMPLEMENTATION_STYLE = "picking_efficiency"
    stage6.STYLE_PRIORITY = ("picking_efficiency",)
    _picking_assignment.baseline_builder = stage6._build_deficit_coverage_layout
    stage6._build_deficit_coverage_layout = _picking_assignment
    layout_rows, column_rows, location_rows = stage6.build_layout_generation()
    _add_picking_metrics(output_dir, stage6)
    suffix_output_tree(output_dir, "PE")
    print(
        "[Stage 6 Picking] complete. "
        f"Layouts: {len(layout_rows)}, columns: {len(column_rows)}, locations: {len(location_rows)}."
    )


if __name__ == "__main__":
    main()
