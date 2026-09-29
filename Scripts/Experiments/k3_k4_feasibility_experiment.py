from __future__ import annotations

import argparse
import csv
import importlib.util
import itertools
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[2]
STAGE6_PATH = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"
DEFAULT_OUTPUT_ROOT = ROOT / "Output" / "06_Layout_Generation_K3_K4_Experiment"


def _load_stage6():
    spec = importlib.util.spec_from_file_location("stage6_k3_k4_experiment", STAGE6_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Stage 6 module: {STAGE6_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _slot_sizes_for_config(stage6, row: dict[str, str]) -> list[float]:
    raw = stage6.common._decode_excel_text(row.get("Slot_Sizes", ""))
    return [float(value.strip()) for value in raw.split(",") if value.strip()]


def _load_configs(stage6, requested_k: set[int]) -> list[tuple[str, int]]:
    configs: list[tuple[str, int]] = []
    for row in stage6._candidate_configs():
        config_id = stage6._normalize_config_id(str(row.get("Config_ID", "")))
        family_size = len(set(int(round(size)) for size in _slot_sizes_for_config(stage6, row)))
        if config_id and family_size in requested_k:
            configs.append((config_id, family_size))
    return sorted(configs, key=lambda item: (item[1], item[0]))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an isolated Stage 6 feasibility experiment for K=3 and/or K=4 configurations."
    )
    parser.add_argument("--k", choices=("3", "4", "both"), default="both")
    parser.add_argument("--config-ids", help="Optional comma-separated config IDs to restrict the experiment.")
    parser.add_argument("--max-configs", type=int, default=0, help="Optional cap; zero means all selected configs.")
    parser.add_argument("--config-seconds", type=float, default=1200.0)
    parser.add_argument("--profile-seconds", type=float, default=600.0)
    parser.add_argument("--output-dir", type=Path, help="Output folder; defaults to a timestamped diagnostics folder.")
    parser.add_argument("--list-only", action="store_true", help="List selected configs without running Stage 6.")
    return parser.parse_args()


def _install_experiment_policy(stage6, config_seconds: float, profile_seconds: float) -> None:
    original_hard_family = stage6._hard_residual_search_family
    original_profile_generator = stage6._generate_feasible_rack_profiles
    original_layout_validator = stage6._layout_assignments_are_feasible

    def _wide_k3_k4_family(candidate_slot_sizes: Sequence[float] | None) -> bool:
        sizes = {int(round(float(value))) for value in (candidate_slot_sizes or []) if float(value) > 0.0}
        return len(sizes) in {3, 4} or original_hard_family(candidate_slot_sizes)

    def _budget(total_budget_seconds: float | None = None) -> tuple[float, float, float]:
        total = float(config_seconds if total_budget_seconds is None else total_budget_seconds)
        if total <= 1.0:
            return max(total, 0.0), 0.0, max(total, 0.0)
        profile = min(max(profile_seconds, 1.0), total - 1.0)
        return profile, total - profile, total

    def _beam_assign_layout(
        rack_columns: list[str],
        required_counts: dict[float, int],
        config_slot_sizes: Sequence[float] | None,
        config_deadline: float | None = None,
        config_id: str | None = None,
    ) -> dict[str, list[float]]:
        if not rack_columns:
            return {}

        started = stage6.time.perf_counter()
        profile_budget, _, _ = stage6._stage6_runtime_budget()
        try:
            profiles = stage6._generate_feasible_rack_profiles(
                config_slot_sizes or list(required_counts),
                timeout_seconds=profile_budget,
                config_deadline=config_deadline,
                required_counts=required_counts,
            )
        except (stage6.Stage6ProfileGenerationTimeout, stage6.Stage6RackSearchTimeout):
            stage6._LAST_STAGE6_TIMEOUTS["profile_generation"] = True
            stage6._LAST_STAGE6_TIMEOUTS["rack_search"] = True
            stage6._LAST_STAGE6_PROFILE_POOL = []
            return {column: [] for column in rack_columns}

        stage6._LAST_STAGE6_STEP_TIMINGS["profile_generation"] = stage6.time.perf_counter() - started
        stage6._LAST_STAGE6_PROFILE_POOL = [list(profile) for profile in profiles]
        stage6._LAST_STAGE6_TIMEOUTS["profile_generation"] = False
        stage6._LAST_STAGE6_TIMEOUTS["rack_search"] = False
        if not profiles:
            return {column: [] for column in rack_columns}

        required_sizes = sorted(required_counts, key=lambda size: int(round(float(size))), reverse=True)
        profiles = sorted(
            profiles,
            key=lambda profile: (
                stage6._profile_requirement_priority(profile, required_counts),
                stage6._rare_family_priority(profile, required_counts),
            ),
            reverse=True,
        )
        rack_to_columns: dict[str, list[str]] = {}
        for column in rack_columns:
            rack_to_columns.setdefault(stage6._rack_from_column_key(column), []).append(column)
        racks = sorted(rack_to_columns, key=lambda rack: (-len(rack_to_columns[rack]), rack))

        requirement_keys = [float(size) for size in required_sizes]
        initial_remaining = tuple(int(required_counts.get(size, 0)) for size in requirement_keys)
        profile_counts = [
            stage6._effective_requirement_counts(profile, requirement_keys)
            for profile in profiles
        ]

        suffix_capacity: list[tuple[int, ...]] = [(0,) * len(requirement_keys) for _ in range(len(racks) + 1)]
        for rack_index in range(len(racks) - 1, -1, -1):
            columns_in_rack = len(rack_to_columns[racks[rack_index]])
            suffix_capacity[rack_index] = tuple(
                suffix_capacity[rack_index + 1][size_index]
                + max((counts.get(int(round(size)), 0) * columns_in_rack for counts in profile_counts), default=0)
                for size_index, size in enumerate(requirement_keys)
            )

        def _state_key(remaining: tuple[int, ...]) -> tuple[int, int, int, tuple[int, ...]]:
            deficits = tuple(max(value, 0) for value in remaining)
            total_deficit = sum(deficits)
            weighted_deficit = sum(
                int(round(float(size))) * deficit
                for size, deficit in zip(requirement_keys, deficits)
            )
            satisfied = sum(deficit == 0 for deficit in deficits)
            return (int(total_deficit == 0), satisfied, -total_deficit, (-weighted_deficit, tuple(-value for value in deficits)))

        states: list[tuple[tuple[int, ...], tuple[int, ...]]] = [(initial_remaining, ())]
        beam_width = 512
        search_deadline = config_deadline

        for rack_index, rack in enumerate(racks):
            if search_deadline is not None and stage6.time.perf_counter() >= search_deadline:
                stage6._LAST_STAGE6_TIMEOUTS["rack_search"] = True
                break

            columns_in_rack = sorted(rack_to_columns[rack])
            expanded: dict[tuple[int, ...], tuple[int, ...]] = {}
            for remaining, path in states:
                for profile_index, counts in enumerate(profile_counts):
                    next_remaining = tuple(
                        max(
                            remaining[size_index]
                            - counts.get(int(round(float(size))), 0) * len(columns_in_rack),
                            0,
                        )
                        for size_index, size in enumerate(requirement_keys)
                    )
                    if next_remaining == remaining:
                        continue
                    if any(
                        deficit > suffix_capacity[rack_index + 1][size_index]
                        for size_index, deficit in enumerate(next_remaining)
                    ):
                        continue
                    next_path = path + (profile_index,)
                    if not any(next_remaining):
                        assignments = {column: [] for column in rack_columns}
                        for assigned_rack, selected_profile in zip(racks[: rack_index + 1], next_path):
                            for column in rack_to_columns[assigned_rack]:
                                assignments[column] = list(profiles[selected_profile])
                        stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = stage6.time.perf_counter() - started - stage6._LAST_STAGE6_STEP_TIMINGS["profile_generation"]
                        return assignments
                    if next_remaining not in expanded:
                        expanded[next_remaining] = next_path

            if not expanded:
                states = []
                break
            ranked = sorted(expanded.items(), key=lambda item: _state_key(item[0]), reverse=True)
            states = [(remaining, path) for remaining, path in ranked[:beam_width]]

        if stage6._LAST_STAGE6_TIMEOUTS.get("rack_search"):
            return {column: [] for column in rack_columns}
        if not states:
            stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = stage6.time.perf_counter() - started - stage6._LAST_STAGE6_STEP_TIMINGS["profile_generation"]
            return {column: [] for column in rack_columns}

        best_remaining, best_path = max(states, key=lambda item: _state_key(item[0]))
        assignments = {column: [] for column in rack_columns}
        for assigned_rack, selected_profile in zip(racks[: len(best_path)], best_path):
            for column in rack_to_columns[assigned_rack]:
                assignments[column] = list(profiles[selected_profile])
        stage6._LAST_STAGE6_STEP_TIMINGS["rack_search"] = stage6.time.perf_counter() - started - stage6._LAST_STAGE6_STEP_TIMINGS["profile_generation"]
        return assignments

    def _exhaustive_k3_k4_profiles(
        candidate_slot_sizes: Sequence[float] | None,
        timeout_seconds: float | None = None,
        config_deadline: float | None = None,
        required_counts: dict[float, int] | None = None,
    ) -> list[list[float]]:
        configured_sizes = sorted(set(stage6._config_size_values(candidate_slot_sizes or [])), reverse=True)
        if len(configured_sizes) not in {3, 4}:
            return original_profile_generator(
                candidate_slot_sizes,
                timeout_seconds=timeout_seconds,
                config_deadline=config_deadline,
                required_counts=required_counts,
            )

        started = stage6.time.perf_counter()
        local_deadline = None if timeout_seconds is None else started + float(timeout_seconds)
        deadlines = [value for value in (local_deadline, config_deadline) if value is not None]
        deadline = min(deadlines) if deadlines else None
        max_rows = int(stage6.EXHAUSTIVE_PROFILE_MAX_SLOT_FAMILY_SIZE)
        profiles_by_signature: dict[tuple[tuple[int, int], ...], list[list[float]]] = {}

        def _keep_profile(candidate: list[float]) -> None:
            if len(candidate) < 4:
                return
            physical_height = sum(float(value) for value in candidate) + stage6.common.BEAM_HEIGHT * (len(candidate) - 1)
            if abs(physical_height - stage6.common.MAX_USED_HEIGHT_BASE) > 1e-9:
                return
            if not stage6._profile_is_feasible_exact_fill(candidate, configured_sizes):
                return
            effective_counts = stage6._effective_requirement_counts(candidate, configured_sizes)
            signature = tuple(sorted((int(size), int(count)) for size, count in effective_counts.items() if count > 0))
            variants = profiles_by_signature.setdefault(signature, [])
            if candidate not in variants:
                variants.append(candidate)
                variants.sort(key=stage6._profile_generation_priority, reverse=True)
                del variants[2:]

        for lower_count in range(3, max_rows):
            for lower_slots in itertools.combinations_with_replacement(configured_sizes, lower_count):
                if deadline is not None and stage6.time.perf_counter() >= deadline:
                    raise stage6.Stage6ProfileGenerationTimeout(
                        "exact K=3/K=4 profile generation exceeded its time budget"
                    )
                residual = (
                    stage6.common.MAX_USED_HEIGHT_BASE
                    - sum(lower_slots)
                    - stage6.common.BEAM_HEIGHT * lower_count
                )
                topfill = int(round(float(residual)))
                if topfill <= 0 or abs(float(topfill) - residual) > 1e-9:
                    continue
                candidate = [float(value) for value in sorted(lower_slots, reverse=True)] + [float(topfill)]
                _keep_profile(candidate)

        exact_profiles = original_profile_generator(
            candidate_slot_sizes,
            timeout_seconds=timeout_seconds,
            config_deadline=config_deadline,
            required_counts=required_counts,
        )
        for profile in exact_profiles:
            if len(profile) >= 4:
                _keep_profile(list(profile))

        candidates = [profile for variants in profiles_by_signature.values() for profile in variants]
        candidates.sort(
            key=lambda profile: (
                stage6._profile_requirement_priority(profile, required_counts or {}),
                stage6._rare_family_priority(profile, required_counts or {}),
                stage6._profile_generation_priority(profile),
            ),
            reverse=True,
        )
        if required_counts and len(candidates) > 64:
            candidates = stage6._choose_profile_shortlist(candidates, required_counts, limit=64)
        return candidates

    def _strict_completed_layout_validator(
        column_assignments,
        column_keys,
        minimum_slot_size,
        available_slot_sizes=None,
        minimum_required_counts=None,
        enforce_minimum_total_locations=False,
    ) -> bool:
        for column_key in column_keys:
            profile = [float(value) for value in column_assignments.get(column_key, []) if float(value) > 0.0]
            if not profile:
                continue
            if len(profile) < 4:
                return False
            used_height = sum(profile) + stage6.common.BEAM_HEIGHT * (len(profile) - 1)
            if abs(used_height - stage6.common.MAX_USED_HEIGHT_BASE) > 1e-9:
                return False
        return original_layout_validator(
            column_assignments,
            column_keys,
            minimum_slot_size,
            available_slot_sizes=available_slot_sizes,
            minimum_required_counts=minimum_required_counts,
            enforce_minimum_total_locations=enforce_minimum_total_locations,
        )

    stage6._hard_residual_search_family = _wide_k3_k4_family
    stage6._generate_feasible_rack_profiles = _exhaustive_k3_k4_profiles
    stage6._layout_assignments_are_feasible = _strict_completed_layout_validator
    stage6._stage6_runtime_budget = _budget
    stage6._build_deficit_coverage_layout = _beam_assign_layout
    stage6.MAX_STAGE6_CONFIG_TIMEOUT_SECONDS = float(config_seconds)
    stage6.PROFILE_GENERATION_TIMEOUT_SECONDS = float(profile_seconds)
    stage6.RACK_SEARCH_TIMEOUT_SECONDS = max(float(config_seconds) - float(profile_seconds), 1.0)
    stage6.STAGE6_CONFIG_LIMIT = 2000
    stage6.EXHAUSTIVE_SEARCH_CONFIG_LIMIT = 2000


def main() -> int:
    args = _parse_args()
    requested_k = {3, 4} if args.k == "both" else {int(args.k)}
    stage6 = _load_stage6()
    selected = _load_configs(stage6, requested_k)

    if args.config_ids:
        requested_ids = {
            stage6._normalize_config_id(config_id)
            for config_id in args.config_ids.split(",")
            if config_id.strip()
        }
        selected = [item for item in selected if item[0] in requested_ids]
        missing_ids = requested_ids - {config_id for config_id, _family_size in selected}
        if missing_ids:
            print(f"Ignoring IDs not found in the selected K family: {', '.join(sorted(missing_ids))}")

    if args.max_configs > 0:
        selected = selected[: args.max_configs]
    if not selected:
        print("No matching K=3/K=4 configurations were found.")
        return 2

    print(f"Selected {len(selected)} configurations:")
    for config_id, family_size in selected:
        print(f"  {config_id}: K={family_size}")
    if args.list_only:
        return 0

    if args.config_seconds <= 0 or args.profile_seconds <= 0 or args.profile_seconds >= args.config_seconds:
        print("Require 0 < --profile-seconds < --config-seconds.")
        return 2

    output_dir = args.output_dir or DEFAULT_OUTPUT_ROOT / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    output_dir = output_dir.resolve()
    if output_dir == stage6.LAYOUT_OUTPUT_DIR.resolve():
        print("Refusing to write experiment results into the production Stage 6 output directory.")
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)

    config_ids = [config_id for config_id, _family_size in selected]
    os.environ["PIPELINE_TARGET_CONFIGS"] = ",".join(config_ids)
    stage6.DEFAULT_TARGET_CONFIGS = ",".join(config_ids)
    stage6.LAYOUT_OUTPUT_DIR = output_dir
    stage6.LAYOUT_DIAGNOSTICS_DIR = output_dir
    _install_experiment_policy(stage6, args.config_seconds, args.profile_seconds)

    print(f"Running Stage 6 with {args.config_seconds:.0f}s/config, including {args.profile_seconds:.0f}s profile generation.")
    print(f"Experimental outputs: {output_dir}")
    stage6.build_layout_generation()

    summary_path = output_dir / "Candidate_Layout_Summary.csv"
    if summary_path.exists():
        with summary_path.open("r", newline="", encoding="utf-8-sig") as source:
            rows = list(csv.DictReader(source))
        feasible = [row for row in rows if str(row.get("Layout_Feasible", "")).strip().upper() == "YES"]
        print(f"Finished: {len(feasible)} feasible layouts among {len(rows)} evaluated configurations.")
        print(f"Summary: {summary_path}")
    else:
        print("Stage 6 returned without writing a candidate summary.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())