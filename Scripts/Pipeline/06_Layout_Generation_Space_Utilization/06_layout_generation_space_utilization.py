"""Stage 6 space-flexibility layout variant.

This variant reuses the baseline Stage 6 implementation and replaces only the
rack-profile assignment objective with maximum total slot space.
"""

import csv
import importlib.util
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SCRIPT = PIPELINE_ROOT / "06_Layout_Generation" / "06_layout_generation.py"


def _load_baseline_module() -> Any:
    spec = importlib.util.spec_from_file_location("stage6_baseline", BASELINE_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load baseline Stage 6 module from {BASELINE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _space_maximizing_assignment(
    rack_columns: list[str],
    required_counts: dict[float, int],
    config_slot_sizes: object,
    config_deadline: float | None = None,
    config_id: str | None = None,
) -> dict[str, list[float]]:
    """Assign one legal profile per rack while maximizing total slot space."""
    del config_id
    stage6 = _space_maximizing_assignment.stage6
    if not rack_columns:
        return {}

    required = {float(size): int(count) for size, count in required_counts.items()}
    generation_start = time.perf_counter()
    profiles = stage6._generate_feasible_rack_profiles(
        config_slot_sizes or list(required),
        timeout_seconds=stage6.PROFILE_GENERATION_TIMEOUT_SECONDS,
        config_deadline=config_deadline,
        required_counts=None,
    )
    stage6._LAST_STAGE6_PROFILE_POOL = [list(profile) for profile in profiles]
    stage6._LAST_STAGE6_STEP_TIMINGS["profile_generation"] = time.perf_counter() - generation_start
    stage6._LAST_STAGE6_STEP_TIMINGS["profile_shortlist"] = 0.0
    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = 0.0

    if not profiles:
        return {column_key: [] for column_key in rack_columns}

    rack_to_columns: dict[str, list[str]] = defaultdict(list)
    for column_key in rack_columns:
        rack_to_columns[stage6._rack_from_column_key(column_key)].append(column_key)
    rack_order = sorted(rack_to_columns)
    ordered_sizes = sorted(required, reverse=True)

    # Keep the highest-space representative when profiles have the same quota effect.
    profile_options: list[tuple[list[float], tuple[int, ...], float]] = []
    best_by_effect: dict[tuple[int, ...], tuple[list[float], float]] = {}
    for profile in profiles:
        effective_counts = stage6._effective_requirement_counts(profile, config_slot_sizes)
        effect = tuple(int(effective_counts.get(int(size), 0)) for size in ordered_sizes)
        slot_space = sum(float(value) for value in profile)
        previous = best_by_effect.get(effect)
        if previous is None or slot_space > previous[1]:
            best_by_effect[effect] = (list(profile), slot_space)
    for effect, (profile, slot_space) in best_by_effect.items():
        profile_options.append((profile, effect, slot_space))

    if not profile_options:
        return {column_key: [] for column_key in rack_columns}

    # State value: accumulated slot space and the selected profile indexes.
    states: dict[tuple[int, ...], tuple[float, tuple[int, ...]]] = {
        tuple(int(required[size]) for size in ordered_sizes): (0.0, ())
    }
    beam_limit = 20000

    for rack_index, rack in enumerate(rack_order):
        if config_deadline is not None and time.perf_counter() >= config_deadline:
            raise stage6.Stage6RackSearchTimeout("space-utilization rack search exceeded the config deadline")

        rack_width = len(rack_to_columns[rack])
        next_states: dict[tuple[int, ...], tuple[float, tuple[int, ...]]] = {}
        for remaining, (space, choices) in states.items():
            for profile_index, (_profile, effect, profile_space) in enumerate(profile_options):
                next_remaining = tuple(
                    max(remaining[index] - effect[index] * rack_width, 0)
                    for index in range(len(ordered_sizes))
                )
                next_space = space + profile_space * rack_width
                next_choices = (*choices, profile_index)
                previous = next_states.get(next_remaining)
                if previous is None or next_space > previous[0]:
                    next_states[next_remaining] = (next_space, next_choices)

        if not next_states:
            break

        # Keep all complete states when possible; otherwise retain the best quota/space states.
        complete = [
            (remaining, value)
            for remaining, value in next_states.items()
            if all(value_left <= 0 for value_left in remaining)
        ]
        candidates = complete if complete else list(next_states.items())
        if len(candidates) > beam_limit:
            candidates.sort(
                key=lambda item: (
                    sum(value_left <= 0 for value_left in item[0]),
                    -sum(max(value_left, 0) for value_left in item[0]),
                    item[1][0],
                ),
                reverse=True,
            )
            candidates = candidates[:beam_limit]
        states = dict(candidates)

    complete_states = [
        (remaining, value)
        for remaining, value in states.items()
        if all(value_left <= 0 for value_left in remaining)
    ]
    if complete_states:
        _remaining, (_space, choices) = max(complete_states, key=lambda item: item[1][0])
    else:
        _remaining, (_space, choices) = max(
            states.items(),
            key=lambda item: (
                sum(value_left <= 0 for value_left in item[0]),
                -sum(max(value_left, 0) for value_left in item[0]),
                item[1][0],
            ),
        )

    assignments: dict[str, list[float]] = {}
    for rack_index, rack in enumerate(rack_order):
        profile_index = choices[rack_index] if rack_index < len(choices) else 0
        profile = profile_options[profile_index][0]
        for column_key in sorted(rack_to_columns[rack]):
            assignments[column_key] = list(profile)

    stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = time.perf_counter() - generation_start
    return assignments


_space_maximizing_assignment.stage6 = None


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

    output_dir = stage6.common.OUTPUT_ROOT / "06_Layout_Generation_Space_Utilization"
    output_dir.mkdir(parents=True, exist_ok=True)
    stage6.LAYOUT_OUTPUT_DIR = output_dir
    stage6.IMPLEMENTATION_STYLE = "space_utilization"
    stage6.STYLE_PRIORITY = ("space_utilization",)
    stage6._build_deficit_coverage_layout = _space_maximizing_assignment

    print(f"[Stage 6 Space] output directory: {output_dir}")
    layout_rows, column_rows, location_rows = stage6.build_layout_generation()
    _add_space_metrics(output_dir)
    print(
        "[Stage 6 Space] complete. "
        f"Layouts: {len(layout_rows)}, columns: {len(column_rows)}, locations: {len(location_rows)}."
    )


if __name__ == "__main__":
    main()
