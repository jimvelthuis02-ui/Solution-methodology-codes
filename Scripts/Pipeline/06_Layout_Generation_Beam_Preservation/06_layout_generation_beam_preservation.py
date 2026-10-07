"""Stage 6 beam-preservation layout variant."""

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
PROFILE_ORDERING_LIMIT = 10000
MAX_SEARCH_STATES = 256
MAX_PROFILE_OPTIONS = 256


def _state_rank(
    item: tuple[tuple[int, ...], tuple[tuple[int, int, int], tuple[int, ...]]],
) -> tuple[object, ...]:
    remaining, (score, _choices) = item
    return (
        sum(value <= 0 for value in remaining),
        -sum(max(value, 0) for value in remaining),
        tuple(-value for value in remaining),
        score,
    )


def _prune_states(
    states: dict[tuple[int, ...], tuple[tuple[int, int, int], tuple[int, ...]]],
) -> dict[tuple[int, ...], tuple[tuple[int, int, int], tuple[int, ...]]]:
    if len(states) <= MAX_SEARCH_STATES:
        return states
    return dict(heapq.nlargest(MAX_SEARCH_STATES, states.items(), key=_state_rank))


def _load_baseline_module() -> Any:
    spec = importlib.util.spec_from_file_location("stage6_baseline_beam", BASELINE_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load baseline Stage 6 module from {BASELINE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    if any(int(round(value)) not in config_values for value in slots[:-1]):
        return False
    final_slot = int(round(slots[-1]))
    if final_slot in config_values:
        return True
    if final_slot not in legal_topfills:
        return False
    effective_final = stage6._effective_requirement_slot_size(final_slot, slots, slot_sizes)
    return effective_final in config_values


def _ordered_profiles(slot_sizes: list[float], stage6: Any, deadline: float | None) -> list[list[float]]:
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
            raise stage6.Stage6ProfileGenerationTimeout("beam profile generation exceeded the config deadline")
        for lower_combo in itertools.combinations_with_replacement(family, lower_count):
            combination_checks += 1
            if combination_checks % 256 == 0 and deadline is not None and time.perf_counter() >= deadline:
                raise stage6.Stage6ProfileGenerationTimeout("beam profile generation exceeded the config deadline")
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
                candidates = itertools.permutations((*lower_stack, rounded_topfill))
            else:
                candidates = ((*ordering, float(rounded_topfill)) for ordering in itertools.permutations(lower_stack))
            for candidate in candidates:
                if len(profiles) >= PROFILE_ORDERING_LIMIT:
                    break
                normalized = tuple(int(round(value)) for value in candidate)
                if normalized in seen:
                    continue
                values = [float(value) for value in normalized]
                if not _profile_is_feasible_unordered(values, slot_sizes, stage6):
                    continue
                seen.add(normalized)
                profiles.append(values)
            if len(profiles) >= PROFILE_ORDERING_LIMIT:
                break
        if len(profiles) >= PROFILE_ORDERING_LIMIT:
            break
    return profiles


def _current_beams_by_rack(stage6: Any) -> dict[str, list[float]]:
    beam_map = stage6._read_csv(stage6.INPUT_LOCATION_BEAM_MAP)
    prepared = stage6._read_csv(stage6.INPUT_PREPARED)
    beam_heights = stage6._read_csv(stage6.INPUT_BEAM_HEIGHT_COORDS)
    current_units, _segments, current_unit_heights = stage6.common._build_current_beam_units_and_segments(
        beam_map,
        prepared,
        beam_heights,
    )
    by_rack: dict[str, list[float]] = defaultdict(list)
    for unit in current_units:
        parsed = stage6.common._parse_beam_coordinate_parts(unit)
        height = current_unit_heights.get(unit)
        if parsed is None or height is None:
            continue
        by_rack[parsed[0]].append(float(height))
    return {rack: sorted(values) for rack, values in by_rack.items()}


def _profile_beam_levels(profile: list[float], stage6: Any) -> list[float]:
    levels: list[float] = []
    cumulative = 0.0
    for index, value in enumerate(profile[:-1]):
        cumulative += float(value)
        levels.append(cumulative + index * stage6.common.BEAM_HEIGHT)
    return levels


def _beam_profile_score(profile: list[float], current_heights: list[float], stage6: Any) -> tuple[int, int, int]:
    proposed = _profile_beam_levels(profile, stage6)
    available = list(current_heights)
    matches = 0
    for height in proposed:
        matching_index = next(
            (index for index, current in enumerate(available) if abs(current - height) <= stage6.common.BEAM_RELOCATION_TOLERANCE_CM + 1e-9),
            None,
        )
        if matching_index is not None:
            matches += 1
            available.pop(matching_index)
    relocations = max(len(proposed), len(current_heights)) - matches
    additions = max(len(proposed) - len(current_heights), 0)
    return -relocations, matches, -additions


def _beam_search_assignment(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: object,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]] | None:
    """Beam-aware rack search; returns None when it cannot produce its own feasible layout."""
    stage6 = _beam_assignment.stage6
    if not rack_columns:
        return {}
    slot_sizes = [float(value) for value in (config_slot_sizes or list(required_counts))]
    generation_start = time.perf_counter()
    profiles = _ordered_profiles(slot_sizes, stage6, config_deadline)
    stage6._LAST_STAGE6_PROFILE_POOL = [list(profile) for profile in profiles]
    stage6._LAST_STAGE6_STEP_TIMINGS["profile_generation"] = time.perf_counter() - generation_start
    stage6._LAST_STAGE6_STEP_TIMINGS["profile_shortlist"] = 0.0
    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = 0.0
    if not profiles:
        return None

    required = {float(size): int(count) for size, count in required_counts.items()}
    ordered_sizes = sorted(required, reverse=True)
    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[stage6._rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns)
    current_by_rack = _current_beams_by_rack(stage6)

    options_by_rack: dict[str, list[tuple[list[float], tuple[int, ...], tuple[int, int, int]]]] = {}
    for rack in rack_order:
        best_options: dict[tuple[int, ...], tuple[list[float], tuple[int, int, int]]] = {}
        for profile in profiles:
            counts = stage6._effective_requirement_counts(profile, slot_sizes)
            effect = tuple(int(counts.get(int(size), 0)) for size in ordered_sizes)
            score = _beam_profile_score(profile, current_by_rack.get(rack, []), stage6)
            previous = best_options.get(effect)
            if previous is None or score > previous[1]:
                best_options[effect] = (profile, score)
        ranked_options = sorted(
            best_options.items(),
            key=lambda item: (
                sum(min(item[0][index], int(required[size])) > 0 for index, size in enumerate(ordered_sizes)),
                sum(min(item[0][index], int(required[size])) for index, size in enumerate(ordered_sizes)),
                sum(min(item[0][index], int(required[size])) * size for index, size in enumerate(ordered_sizes)),
                item[1][1][0],
            ),
            reverse=True,
        )[:MAX_PROFILE_OPTIONS]
        options_by_rack[rack] = [
            (profile, effect, score)
            for effect, (profile, score) in ranked_options
        ]

    states: dict[tuple[int, ...], tuple[tuple[int, int, int], tuple[int, ...]]] = {
        tuple(int(required[size]) for size in ordered_sizes): ((0, 0, 0), ())
    }
    for rack in rack_order:
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            raise stage6.Stage6RackSearchTimeout("beam rack search exceeded the config deadline")
        rack_width = len(rack_to_columns[rack])
        next_states: dict[tuple[int, ...], tuple[tuple[int, int, int], tuple[int, ...]]] = {}
        for remaining, (score, choices) in states.items():
            for profile_index, (_profile, effect, profile_score) in enumerate(options_by_rack[rack]):
                next_remaining = tuple(
                    max(remaining[index] - effect[index] * rack_width, 0)
                    for index in range(len(ordered_sizes))
                )
                next_score = tuple(score[index] + profile_score[index] * rack_width for index in range(3))
                previous = next_states.get(next_remaining)
                if previous is None or next_score > previous[0]:
                    next_states[next_remaining] = (next_score, (*choices, profile_index))
                if len(next_states) > MAX_SEARCH_STATES * 2:
                    next_states = _prune_states(next_states)
        states = _prune_states(next_states)

    chosen = max(states.items(), key=_state_rank)
    choices = chosen[1][1]
    assignments: dict[str, list[float]] = {}
    for rack_index, rack in enumerate(rack_order):
        profile_index = choices[rack_index] if rack_index < len(choices) else 0
        profile = options_by_rack[rack][profile_index][0]
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
        return None
    return assignments


_RELOCATION_CONTEXT: dict[str, Any] = {}


def _relocation_total(assignments: dict[str, list[float]], config_id: str, stage6: Any) -> int:
    """Beam relocations of a layout, computed exactly as the Stage 6 summary reports them."""
    common = stage6.common
    if not _RELOCATION_CONTEXT:
        beam_map_rows = stage6._read_csv(stage6.INPUT_LOCATION_BEAM_MAP)
        units, segments, unit_heights = common._build_current_beam_units_and_segments(
            beam_map_rows,
            stage6._read_csv(stage6.INPUT_PREPARED),
            stage6._read_csv(stage6.INPUT_BEAM_HEIGHT_COORDS),
        )
        _RELOCATION_CONTEXT.update(
            units=units,
            segments=segments,
            unit_heights=unit_heights,
            units_by_column=common._beam_units_by_column(beam_map_rows),
        )
    context = _RELOCATION_CONTEXT
    location_rows = common._build_generated_layout_location_rows(
        "LAY_BEAM_EVAL",
        config_id,
        stage6.IMPLEMENTATION_STYLE,
        assignments,
        segments=context["segments"],
        layout_thresholds_by_rack={},
    )
    proposed_units, proposed_heights = common._build_proposed_beam_units_from_layout_rows(location_rows, context["segments"])
    total, _by_column, _removed, _added = common._beam_relocations(
        context["units"],
        proposed_units,
        context["unit_heights"],
        proposed_heights,
        current_units_by_column=context["units_by_column"],
        proposed_units_by_column=common._beam_units_by_column(location_rows),
    )
    return int(total)


def _beam_assignment(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: object,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]]:
    """Return the beam-aware layout only when it relocates fewer beams than the baseline layout."""
    stage6 = _beam_assignment.stage6
    baseline = _beam_assignment.baseline_builder(
        rack_columns,
        required_counts,
        config_slot_sizes,
        config_deadline=config_deadline,
        config_id=config_id,
    )
    baseline_pool = [list(profile) for profile in stage6._LAST_STAGE6_PROFILE_POOL]
    candidate = _beam_search_assignment(
        rack_columns,
        required_counts,
        config_slot_sizes,
        config_deadline=config_deadline,
        config_id=config_id,
    )
    if not baseline or not candidate:
        return candidate or baseline

    label = str(config_id or "")
    if _relocation_total(candidate, label, stage6) < _relocation_total(baseline, label, stage6):
        return candidate
    stage6._LAST_STAGE6_PROFILE_POOL = baseline_pool
    return baseline


_beam_assignment.stage6 = None
_beam_assignment.baseline_builder = None


def main() -> None:
    stage6 = _load_baseline_module()
    _beam_assignment.stage6 = stage6
    output_dir = Path(
        os.environ.get(
            "PIPELINE_BEAM_PRESERVATION_OUTPUT_DIR",
            stage6.common.OUTPUT_ROOT / "06_Layout_Generation_Beam_Preservation",
        )
    ).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stage6.LAYOUT_OUTPUT_DIR = output_dir
    stage6.IMPLEMENTATION_STYLE = "beam_preservation"
    stage6.STYLE_PRIORITY = ("beam_preservation",)
    _beam_assignment.baseline_builder = stage6._build_deficit_coverage_layout
    stage6._build_deficit_coverage_layout = _beam_assignment
    layout_rows, column_rows, location_rows = stage6.build_layout_generation()
    suffix_output_tree(output_dir, "BP")
    print(
        "[Stage 6 Beam] complete. "
        f"Layouts: {len(layout_rows)}, columns: {len(column_rows)}, locations: {len(location_rows)}."
    )


if __name__ == "__main__":
    main()
