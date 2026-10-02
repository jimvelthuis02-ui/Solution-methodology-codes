"""Stage 6 beam-preservation layout variant."""

import csv
import importlib.util
import itertools
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SCRIPT = PIPELINE_ROOT / "06_Layout_Generation" / "06_layout_generation.py"
PROFILE_ORDERING_LIMIT = 10000


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

    for lower_count in range(1, max_lower_rows + 1):
        if deadline is not None and time.perf_counter() >= deadline:
            raise stage6.Stage6ProfileGenerationTimeout("beam profile generation exceeded the config deadline")
        for lower_combo in itertools.combinations_with_replacement(family, lower_count):
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
    return matches, -relocations, -additions


def _beam_assignment(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: object,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]]:
    del config_id
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
        return {column_key: [] for column_key in rack_columns}

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
                best_options[effect] = (list(profile), score)
        options_by_rack[rack] = [
            (profile, effect, score)
            for effect, (profile, score) in best_options.items()
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
        complete = [item for item in next_states.items() if all(value <= 0 for value in item[0])]
        candidates = complete or list(next_states.items())
        candidates.sort(
            key=lambda item: (
                sum(value <= 0 for value in item[0]),
                -sum(max(value, 0) for value in item[0]),
                item[1][0],
            ),
            reverse=True,
        )
        states = dict(candidates[:20000])

    complete = [item for item in states.items() if all(value <= 0 for value in item[0])]
    chosen = max(complete or list(states.items()), key=lambda item: item[1][0])
    choices = chosen[1][1]
    assignments: dict[str, list[float]] = {}
    for rack_index, rack in enumerate(rack_order):
        profile_index = choices[rack_index] if rack_index < len(choices) else 0
        profile = options_by_rack[rack][profile_index][0]
        for column_key in sorted(rack_to_columns[rack]):
            assignments[column_key] = list(profile)
    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - generation_start
    return assignments


_beam_assignment.stage6 = None


def main() -> None:
    stage6 = _load_baseline_module()
    _beam_assignment.stage6 = stage6
    output_dir = stage6.common.OUTPUT_ROOT / "06_Layout_Generation_Beam_Preservation"
    output_dir.mkdir(parents=True, exist_ok=True)
    stage6.LAYOUT_OUTPUT_DIR = output_dir
    stage6.IMPLEMENTATION_STYLE = "beam_preservation"
    stage6.STYLE_PRIORITY = ("beam_preservation",)
    stage6._build_deficit_coverage_layout = _beam_assignment
    layout_rows, column_rows, location_rows = stage6.build_layout_generation()
    print(
        "[Stage 6 Beam] complete. "
        f"Layouts: {len(layout_rows)}, columns: {len(column_rows)}, locations: {len(location_rows)}."
    )


if __name__ == "__main__":
    main()
