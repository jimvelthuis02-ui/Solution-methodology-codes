import argparse
import csv
import importlib.util
import itertools
import sys
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STAGE6_PATH = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"
DEFAULT_OUTPUT_DIR = ROOT / "Output" / "06_Layout_Generation" / "Fix_Test_Inventory" / "K3_K4_Experiments"


def _load_stage6():
    spec = importlib.util.spec_from_file_location("stage6_k3_k4_experiment", STAGE6_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Stage 6 from {STAGE6_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse_config_ids(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {item.strip().upper() for item in value.split(",") if item.strip()}


def _profile_pool_generator(stage6, candidate_slot_sizes, timeout_seconds=None, config_deadline=None, required_counts=None):
    family = sorted(set(stage6._config_size_values(candidate_slot_sizes or [])))
    if not family:
        return []

    started = time.perf_counter()
    deadlines = [value for value in (config_deadline,) if value is not None]
    if timeout_seconds is not None:
        deadlines.append(started + float(timeout_seconds))
    deadline = min(deadlines) if deadlines else None
    legal_topfills = stage6._legal_topfill_values(family)
    max_lower_rows = max(
        1,
        min(
            18,
            int(stage6.common.MAX_USED_HEIGHT_BASE // (min(family) + stage6.common.BEAM_HEIGHT)),
        ),
    )
    profiles: set[tuple[float, ...]] = set()
    checks = 0

    for lower_count in range(1, max_lower_rows + 1):
        for lower_combo in itertools.combinations_with_replacement(family, lower_count):
            checks += 1
            if checks % 128 == 0 and deadline is not None and time.perf_counter() >= deadline:
                raise stage6.Stage6ProfileGenerationTimeout("experiment profile generation timed out")

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

            profile = (*lower_stack, float(rounded_topfill))
            if stage6._profile_is_feasible_exact_fill(profile, family):
                profiles.add(profile)

    return [
        list(profile)
        for profile in sorted(profiles, key=lambda values: tuple(-float(value) for value in values))
    ]


def _all_profile_rack_search(stage6, rack_columns, required_counts, slot_sizes, profiles, deadline):
    rack_to_columns = {}
    for column_key in rack_columns:
        rack = stage6._rack_from_column_key(column_key)
        rack_to_columns.setdefault(rack, []).append(column_key)
    rack_order = sorted(rack_to_columns)
    sizes = sorted((float(size) for size in required_counts), reverse=True)
    required_key = tuple(sorted((float(size), int(count)) for size, count in required_counts.items()))

    profile_options = []
    seen_effects = set()
    for profile in profiles:
        effective = stage6._effective_requirement_counts(profile, slot_sizes)
        effect_key = tuple(int(effective.get(int(size), 0)) for size in sizes)
        if effect_key in seen_effects:
            continue
        seen_effects.add(effect_key)
        profile_options.append((list(profile), effect_key))
    profile_options.sort(
        key=lambda item: tuple(-int(value) for value in item[1])
    )

    def objective(remaining):
        satisfied = sum(1 for size in sizes if remaining.get(size, 0) <= 0)
        shortage = sum(max(int(remaining.get(size, 0)), 0) for size in sizes)
        total_need = sum(max(int(value), 0) for value in remaining.values())
        distribution_gap = sum(
            abs(float(max(int(remaining.get(size, 0)), 0)) / float(total_need))
            for size in sizes
            if int(remaining.get(size, 0)) > 0 and total_need > 0
        )
        return satisfied, -shortage, int(-distribution_gap * 1000.0)

    @lru_cache(maxsize=100_000)
    def search(index, remaining_key):
        if time.perf_counter() >= deadline:
            raise stage6.Stage6RackSearchTimeout("experiment all-profile rack search timed out")
        remaining = {float(size): int(count) for size, count in remaining_key}
        if index >= len(rack_order):
            return objective(remaining), ()
        if all(value <= 0 for value in remaining.values()):
            return objective(remaining), tuple(0 for _ in rack_order[index:])

        rack_width = len(rack_to_columns[rack_order[index]])
        best_score = None
        best_choices = None
        for profile_index, (_, effect) in enumerate(profile_options):
            next_remaining = dict(remaining)
            for size_index, size in enumerate(sizes):
                next_remaining[size] = max(
                    int(next_remaining.get(size, 0)) - int(effect[size_index]) * rack_width,
                    0,
                )
            if next_remaining == remaining:
                continue
            child_score, child_choices = search(
                index + 1,
                tuple(sorted((float(size), int(count)) for size, count in next_remaining.items())),
            )
            if best_score is None or child_score > best_score:
                best_score = child_score
                best_choices = (profile_index, *child_choices)

        if best_score is None:
            return objective(remaining), tuple(0 for _ in rack_order[index:])
        return best_score, best_choices

    _, selected_indices = search(0, required_key)
    assignments = {}
    for rack_index, profile_index in enumerate(selected_indices):
        rack = rack_order[rack_index]
        profile = profile_options[profile_index][0]
        for column_key in sorted(rack_to_columns[rack]):
            assignments[column_key] = list(profile)
    return assignments


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    updated_configs = {row.get("Config_ID", "") for row in rows}
    existing_rows = []
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as source:
            existing_rows = [
                row for row in csv.DictReader(source)
                if row.get("Config_ID", "") not in updated_configs
            ]
    merged_rows = [*existing_rows, *rows]
    fieldnames = list(dict.fromkeys(key for row in merged_rows for key in row))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(merged_rows)


def run_experiment(config_ids: set[str] | None, seconds_per_config: float, shortlist_limit: int, output_dir: Path) -> None:
    stage6 = _load_stage6()
    stage6.PROFILE_GENERATION_TIMEOUT_SECONDS = seconds_per_config
    stage6.RACK_SEARCH_TIMEOUT_SECONDS = seconds_per_config

    original_policy = stage6._profile_generation_policy

    def expanded_policy(slot_sizes):
        generation_depth, profile_cap, family_cap, shortlist_cap = original_policy(slot_sizes)
        return generation_depth, profile_cap, family_cap, max(shortlist_cap, shortlist_limit)

    def distinct_profile_shortlist(profiles, required_counts, limit=None):
        ordered = sorted(
            profiles,
            key=lambda profile: stage6._profile_requirement_priority(list(profile), required_counts),
            reverse=True,
        )
        unique = []
        seen = set()
        for profile in ordered:
            key = tuple(float(value) for value in profile)
            if key in seen:
                continue
            seen.add(key)
            unique.append(list(profile))
        return unique[: int(limit or shortlist_limit)]

    stage6._profile_generation_policy = expanded_policy
    stage6._choose_profile_shortlist = distinct_profile_shortlist
    stage6._generate_feasible_rack_profiles = lambda candidate_slot_sizes, timeout_seconds=None, config_deadline=None, required_counts=None: _profile_pool_generator(
        stage6,
        candidate_slot_sizes,
        timeout_seconds=timeout_seconds,
        config_deadline=config_deadline,
        required_counts=required_counts,
    )

    configs = []
    for row in stage6._candidate_configs():
        config_id = str(row.get("Config_ID", "")).strip().upper()
        k_value = stage6.common._to_int_default(row.get("K"), 0)
        if k_value not in {3, 4}:
            continue
        if config_ids is not None and config_id not in config_ids:
            continue
        configs.append((config_id, k_value))

    if config_ids is not None:
        found = {config_id for config_id, _ in configs}
        missing = sorted(config_ids - found)
        if missing:
            raise ValueError(f"Requested configs are not K=3 or K=4 in the active config file: {missing}")

    capacity_by_config = stage6._capacity_rows_by_config()
    prepared_rows = stage6._read_csv(stage6.INPUT_PREPARED)
    layout_columns = stage6.common._build_layout_columns(prepared_rows)
    baseline_path = stage6.LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary.csv"
    baseline_rows = stage6._read_csv(baseline_path) if baseline_path.exists() else []
    baseline_by_config = {row.get("Config_ID", ""): row for row in baseline_rows}

    summary_rows: list[dict[str, str]] = []
    profile_rows: list[dict[str, str]] = []
    assignment_rows: list[dict[str, str]] = []

    for config_id, k_value in configs:
        started = time.perf_counter()
        deadline = started + seconds_per_config
        capacity_rows = capacity_by_config.get(config_id, [])
        required = stage6._base_exact_counts(capacity_rows)
        slot_sizes = stage6._slot_sizes_from_capacity(capacity_rows)
        profiles = _profile_pool_generator(stage6, slot_sizes, timeout_seconds=seconds_per_config)
        for profile_index, profile in enumerate(profiles, start=1):
            profile_rows.append(
                {
                    "Config_ID": config_id,
                    "K": str(k_value),
                    "Profile_Index": str(profile_index),
                    "Profile_Values": ",".join(str(int(round(value))) for value in profile),
                    "Profile_Signature": "|".join(str(int(round(value))) for value in profile),
                }
            )

        status = "NO_FEASIBLE_LAYOUT"
        reason = ""
        assigned_total = 0
        capacity_margin = -sum(required.values())
        assignment = {}
        stage6._LAST_STAGE6_TIMEOUTS["profile_generation"] = False
        stage6._LAST_STAGE6_TIMEOUTS["rack_search"] = False
        try:
            stage6._LAST_STAGE6_CONFIG_DEADLINE = deadline
            assignment = _all_profile_rack_search(
                stage6,
                layout_columns,
                required,
                slot_sizes,
                profiles,
                deadline,
            )
            profile_timed_out = stage6._LAST_STAGE6_TIMEOUTS.get("profile_generation", False)
            rack_search_timed_out = stage6._LAST_STAGE6_TIMEOUTS.get("rack_search", False)
            assigned_total = max(
                sum(len(slots) for slots in assignment.values()) - stage6.common._fixed_layout_location_total(),
                0,
            )
            capacity_margin = assigned_total - sum(required.values())
            minimum_size = min(required) if required else 0.0
            physical_gate = stage6._layout_assignments_are_feasible(
                assignment,
                list(assignment),
                float(minimum_size),
                slot_sizes,
                minimum_required_counts=required,
                enforce_minimum_total_locations=True,
            )
            if physical_gate and capacity_margin >= 0:
                status = "FEASIBLE_FOUND"
            elif profile_timed_out or rack_search_timed_out:
                status = "TIMEOUT"
                reason = "profile generation timed out" if profile_timed_out else "rack search timed out"
            else:
                reason = stage6._layout_feasibility_reason(
                    assignment,
                    list(assignment),
                    float(minimum_size),
                    slot_sizes,
                    minimum_required_counts=required,
                    enforce_minimum_total_locations=True,
                    assigned_locations_total=assigned_total,
                    required_locations_total=sum(required.values()),
                    capacity_margin=capacity_margin,
                )
        except (stage6.Stage6ProfileGenerationTimeout, stage6.Stage6RackSearchTimeout) as error:
            status = "TIMEOUT"
            reason = str(error)

        rack_counts: Counter[str] = Counter()
        layout_slots = [value for profile in assignment.values() for value in profile]
        family_coverage: Counter[int] = Counter()
        for value in layout_slots:
            effective_size = stage6._effective_requirement_slot_size(value, layout_slots, slot_sizes)
            if effective_size is not None:
                family_coverage[int(effective_size)] += 1
        family_coverage_text = "|".join(
            f"{int(size)}:{family_coverage.get(int(round(float(size))), 0)}/{int(count)}"
            for size, count in sorted(required.items())
        )
        for column_key, profile in assignment.items():
            rack_counts[stage6._rack_from_column_key(column_key)] += 1
            assignment_rows.append(
                {
                    "Config_ID": config_id,
                    "K": str(k_value),
                    "Rack": stage6._rack_from_column_key(column_key),
                    "Column": column_key,
                    "Profile_Values": ",".join(str(int(round(value))) for value in profile),
                }
            )

        baseline = baseline_by_config.get(config_id, {})
        summary_rows.append(
            {
                "Config_ID": config_id,
                "K": str(k_value),
                "Slot_Sizes": ",".join(str(int(value)) for value in slot_sizes),
                "Baseline_Layout_Feasible": baseline.get("Layout_Feasible", "NOT_IN_CURRENT_SUMMARY"),
                "Baseline_Profiles_Considered": baseline.get("Feasible_Profiles_Considered_Total", ""),
                "Expanded_Unique_Legal_Profiles": str(len(profiles)),
                "Assigned_Locations": str(assigned_total),
                "Required_Locations": str(sum(required.values())),
                "Capacity_Margin": str(capacity_margin),
                "Family_Coverage": family_coverage_text,
                "Rack_Count": str(len(rack_counts)),
                "Experiment_Result": status,
                "Reason": reason,
                "Runtime_Seconds": f"{time.perf_counter() - started:.3f}",
            }
        )
        _write_csv(output_dir / "K3_K4_Experiment_Summary.csv", summary_rows)
        _write_csv(output_dir / "K3_K4_Experiment_Profiles.csv", profile_rows)
        _write_csv(output_dir / "K3_K4_Experiment_Assignments.csv", assignment_rows)
        print(
            f"{config_id} K={k_value}: {status}; profiles={len(profiles)}; "
            f"assigned={assigned_total}/{sum(required.values())}; "
            f"runtime={summary_rows[-1]['Runtime_Seconds']}s"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Experiment with Stage 6 profile diversity for K=3/K=4 configs.")
    parser.add_argument("--config-ids", help="Optional comma-separated subset, e.g. CFG_003,CFG_004")
    parser.add_argument("--seconds-per-config", type=float, default=30.0)
    parser.add_argument("--shortlist-limit", type=int, default=64)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    run_experiment(_parse_config_ids(args.config_ids), args.seconds_per_config, args.shortlist_limit, args.output_dir)


if __name__ == "__main__":
    main()