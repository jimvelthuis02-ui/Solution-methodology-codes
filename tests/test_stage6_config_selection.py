import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE6_PATH = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"

spec = importlib.util.spec_from_file_location("stage6", STAGE6_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules["stage6"] = mod
spec.loader.exec_module(mod)


def _row(config_id: str, slot_sizes: str) -> dict[str, str]:
    return {"Config_ID": config_id, "Slot_Sizes": slot_sizes}


def test_stage6_candidate_filter_keeps_k_ge_3_without_slot_focus(monkeypatch):
    monkeypatch.setattr(mod, "_target_config_ids_from_environment", list)
    monkeypatch.setattr(mod, "STAGE6_CONFIG_SLOT_SIZE_FOCUS", None)

    rows = [
        _row("CFG_001", "69, 124"),
        _row("CFG_002", "69, 124, 189"),
        _row("CFG_003", "69, 124, 189, 239"),
        _row("CFG_004", "69, 124, 189, 239, 314"),
        _row("CFG_005", "69, 124, 189, 239, 314, 460"),
    ]

    selected = mod._candidate_configs_for_exhaustive_search(rows)
    selected_ids = [row["Config_ID"] for row in selected]

    assert selected_ids == ["CFG_001", "CFG_002", "CFG_003", "CFG_004", "CFG_005"]


<<<<<<< Updated upstream
def test_feasible_generator_keeps_valid_repeated_family_profiles():
    profiles = mod._generate_feasible_rack_profiles([124, 239], timeout_seconds=5.0)

    assert profiles
    assert any(
        tuple(sorted(int(round(float(value))) for value in profile)) == (124, 124, 124, 124, 194)
        for profile in profiles
    )


def test_feasible_generator_keeps_valid_239_family_profiles():
    expected = {
        (239, 239, 124, 104),
        (239, 239, 69, 69, 74),
        (239, 124, 124, 124, 79),
        (239, 124, 69, 69, 69, 104),
        (239, 69, 69, 69, 69, 69, 74),
        (124, 124, 124, 124, 194),
        (124, 124, 124, 124, 69, 109),
        (124, 124, 124, 69, 69, 69, 79),
        (124, 69, 69, 69, 69, 69, 69, 104),
        (69, 69, 69, 69, 69, 69, 69, 69, 74),
    }
    profiles = mod._generate_feasible_rack_profiles([69, 124, 239], timeout_seconds=5.0)
    generated = {
        tuple(int(round(float(value))) for value in profile)
        for profile in profiles
    }

    assert expected & generated


def test_build_deficit_coverage_layout_keeps_partial_assignment_for_short_family(monkeypatch):
    generated_profiles = [[124.0, 124.0, 124.0, 124.0, 194.0]]

    monkeypatch.setattr(mod, "_generate_feasible_rack_profiles", lambda *args, **kwargs: generated_profiles)
    monkeypatch.setattr(mod, "_choose_profile_shortlist", lambda profiles, required_counts, limit=None: generated_profiles)

    assignments = mod._build_deficit_coverage_layout(
        rack_columns=[f"A{index:02d}" for index in range(10)],
        required_counts={124.0: 15, 239.0: 3},
        config_slot_sizes=[124.0, 239.0],
    )

    assert assignments
    assert any(values for values in assignments.values())
=======
def test_generate_feasible_rack_profiles_accepts_runtime_keywords():
    profiles = mod._generate_feasible_rack_profiles(
        [114.0, 239.0],
        timeout_seconds=10.0,
        config_deadline=123.0,
        required_counts={114.0: 486, 239.0: 440},
    )

    assert isinstance(profiles, list)
    assert all(isinstance(profile, list) for profile in profiles)


def test_build_layout_generation_emits_summary_rows_for_selected_config(monkeypatch):
    monkeypatch.setenv("PIPELINE_TARGET_CONFIGS", "CFG_002")
    layout_rows, _, _ = mod.build_layout_generation()

    assert layout_rows
    assert all(str(row.get("Config_ID", "")).strip() == "CFG_002" for row in layout_rows)


def test_pipeline_target_override_accepts_random_samples(monkeypatch):
    run_ordered_pipeline = __import__("run_ordered_pipeline", fromlist=["_resolve_pipeline_target_override"])
    monkeypatch.setattr("sys.argv", ["run_ordered_pipeline.py", "random:10"])

    assert run_ordered_pipeline._resolve_pipeline_target_override() == "random:10"
>>>>>>> Stashed changes


def test_floor_mapping_uses_dominant_family_order():
    config_values = [69, 124, 189, 239]

    assert mod._effective_requirement_slot_size(79, [239, 124, 124, 79], config_values) == 69
    assert mod._effective_requirement_slot_size(114, [239, 124, 124, 114], config_values) == 69
    assert mod._effective_requirement_slot_size(64, [239, 189, 124, 64], config_values) is None
    assert mod._effective_requirement_slot_size(79, [239, 124, 124, 124, 79], [124, 239]) == 124


def test_profile_keeps_missing_family_alive_allows_any_unmet_family_match():
    remaining = {69.0: 1, 99.0: 1, 124.0: 1}
    profile = [239.0, 239.0, 99.0, 69.0, 44.0]

    assert mod._profile_keeps_missing_family_alive(profile, remaining) is True


def test_profile_requirement_priority_prefers_distribution_match_over_raw_shortage_cover():
    remaining = {69.0: 5, 124.0: 3, 239.0: 2}
    shape_match = [239.0, 124.0, 69.0, 69.0, 69.0, 104.0]
    shortage_cover = [239.0, 239.0, 69.0, 69.0, 74.0]

    assert mod._profile_requirement_priority(shape_match, remaining) > mod._profile_requirement_priority(shortage_cover, remaining)


def test_run_search_with_profiles_assigns_same_profile_to_all_columns_in_each_rack():
    profile = [124.0, 124.0, 124.0, 124.0, 69.0, 109.0]
    rack_columns = ["A00", "A01", "A02", "B00", "B01"]
    rack_to_columns = {"A": ["A00", "A01", "A02"], "B": ["B00", "B01"]}

    assignments = mod._run_search_with_profiles(
        [profile],
        required_counts={69.0: 1, 124.0: 1},
        rack_columns=rack_columns,
        rack_to_columns=rack_to_columns,
    )

    assert all(assignments[column] for column in rack_columns)
    assert assignments["A00"] == assignments["A01"] == assignments["A02"] == profile
    assert assignments["B00"] == assignments["B01"] == profile


def test_run_search_with_profiles_accepts_positional_required_counts_and_rack_columns():
    profile = [124.0, 124.0, 124.0, 124.0, 69.0, 109.0]
    rack_columns = ["A00", "A01", "A02", "B00", "B01"]
    rack_to_columns = {"A": ["A00", "A01", "A02"], "B": ["B00", "B01"]}

    assignments = mod._run_search_with_profiles(
        [profile],
        {69.0: 1, 124.0: 1},
        rack_columns,
        rack_to_columns,
    )

    assert all(assignments[column] for column in rack_columns)
    assert assignments["A00"] == assignments["A01"] == assignments["A02"] == profile
    assert assignments["B00"] == assignments["B01"] == profile


def test_full_layout_minimum_counts_are_checked_across_the_completed_layout():
    column_assignments = {
        "A00": [239, 124, 124, 124, 79],
        "A01": [239, 124, 124, 124, 79],
    }
    minimum_required_counts = {69.0: 2, 124.0: 6, 239.0: 2}

    assert mod._minimum_required_counts_are_satisfied(
        column_assignments,
        ["A00", "A01"],
        [69, 124, 239],
        minimum_required_counts,
    )


def test_final_layout_feasibility_ignores_empty_columns():
    valid_profile = [124, 124, 124, 124, 69, 109]
    assignments = {
        "A00": valid_profile,
        "A01": valid_profile,
    }
    all_layout_columns = [f"A{index:02d}" for index in range(20)]

    feasible = mod._layout_assignments_are_feasible(
        assignments,
        all_layout_columns,
        69,
        [69, 124],
        minimum_required_counts={69.0: 2, 124.0: 4},
        enforce_minimum_total_locations=False,
    )

    assert feasible is True


<<<<<<< Updated upstream
def test_final_layout_feasibility_rejects_topfill_below_family_minimum():
    assignments = {
        "A00": [124, 69, 69, 69, 69, 69, 69, 69, 19],
        "A01": [124, 69, 69, 69, 69, 69, 69, 69, 19],
    }
    all_layout_columns = [f"A{index:02d}" for index in range(10)]

    feasible = mod._layout_assignments_are_feasible(
        assignments,
        all_layout_columns,
        69,
        [69, 124, 239],
        minimum_required_counts={69.0: 8, 124.0: 2},
        enforce_minimum_total_locations=False,
    )

    assert feasible is False


def test_profile_requirement_priority_prefers_closing_larger_family_shortage():
    remaining = {69.0: 8, 124.0: 4, 239.0: 1}
    seventy_heavy = [69, 69, 69, 69, 69, 69, 69, 69, 79]
    larger_family = [124, 124, 124, 124, 69, 69, 69, 69, 79]

    assert mod._profile_requirement_priority(larger_family, remaining) > mod._profile_requirement_priority(seventy_heavy, remaining)
=======
def test_stage6_minimum_exact_family_contract_is_checked_on_completed_layout_only():
    completed = {
        "A00": [69, 124, 124, 69],
        "A01": [69, 124, 124, 69],
    }
    all_layout_columns = [f"A{index:02d}" for index in range(20)]
    empty_layout = {column_key: [] for column_key in all_layout_columns}

    assert mod._layout_assignments_are_feasible(
        completed,
        all_layout_columns,
        69,
        [69, 124],
        minimum_required_counts={69.0: 2, 124.0: 4},
        enforce_minimum_total_locations=False,
    ) is True
    assert mod._layout_assignments_are_feasible(
        empty_layout,
        all_layout_columns,
        69,
        [69, 124],
        minimum_required_counts={69.0: 1},
        enforce_minimum_total_locations=False,
    ) is False


def test_layout_feasibility_requires_exact_754cm_profile_height_only_after_completion():
    partial_assignments = {
        "A00": [69, 124, 124, 69],
        "A01": [69, 124, 124, 69],
    }
    all_layout_columns = [f"A{index:02d}" for index in range(20)]

    assert mod._layout_assignments_are_feasible(
        partial_assignments,
        all_layout_columns,
        69,
        [69, 124],
        minimum_required_counts={69.0: 2, 124.0: 4},
        enforce_minimum_total_locations=False,
    ) is True

    assert mod._layout_assignments_are_feasible(
        partial_assignments,
        all_layout_columns,
        69,
        [69, 124],
        minimum_required_counts={69.0: 2, 124.0: 4},
        enforce_minimum_total_locations=True,
    ) is False


def test_profile_is_feasible_exact_fill_uses_underlying_family_for_topfill_order_check():
    profile = [124.0, 124.0, 124.0, 69.0, 69.0, 69.0, 79.0]

    assert mod._profile_is_feasible_exact_fill(profile, [69, 124]) is True


def test_repeated_239_profile_is_rejected_as_non_exact_fill():
    profile = [239.0] * 12

    assert mod._profile_is_feasible_exact_fill(profile, [239]) is False


def test_synthesized_family_cover_profiles_must_remain_physically_legal():
    profiles = mod._synthesize_family_cover_profiles({239.0: 1}, max_columns=1, max_profile_size=12)

    assert not profiles or all(mod._profile_is_feasible_exact_fill(profile, [239]) for profile in profiles)


def test_build_deficit_coverage_layout_rejects_invalid_nonempty_output():
    monkeypatch = None

    def fake_generate(*args, **kwargs):
        return []

    def fake_run(*args, **kwargs):
        return {"A00": [239.0] * 12, "A01": []}

    original_generate = mod._generate_feasible_rack_profiles
    original_run = mod._run_search_with_profiles
    mod._generate_feasible_rack_profiles = fake_generate
    mod._run_search_with_profiles = fake_run
    try:
        assignments = mod._build_deficit_coverage_layout(
            rack_columns=["A00", "A01"],
            required_counts={239.0: 1},
            config_slot_sizes=[239],
        )
    finally:
        mod._generate_feasible_rack_profiles = original_generate
        mod._run_search_with_profiles = original_run

    assert assignments == {"A00": [], "A01": []}


def test_exact_family_capacity_upper_bound_rejects_impossible_layouts():
    configs = {74.0: 460, 239.0: 466}
    profiles = [
        [239.0, 239.0, 74.0, 154.0],
        [239.0, 74.0, 74.0, 74.0, 74.0, 139.0],
        [74.0, 74.0, 74.0, 74.0, 74.0, 74.0, 214.0],
    ]

    assert mod._exact_family_capacity_is_feasible(profiles, configs, total_columns=214) is False
    assert mod._exact_family_capacity_is_feasible(profiles, {74.0: 100, 239.0: 100}, total_columns=214) is True


def test_exact_cover_completion_subset_allows_profile_overfill_for_required_family_minima():
    required_counts = {74.0: 15, 239.0: 3}
    profiles = [
        [239.0, 74.0, 74.0, 74.0, 74.0, 139.0],
        [239.0, 74.0, 74.0, 74.0, 139.0],
    ]

    subset = mod._exact_cover_completion_subset(profiles, required_counts, max_columns=10)

    assert subset


def test_generate_feasible_rack_profiles_keeps_topfill_last_for_valid_exact_fill():
    profiles = mod._generate_feasible_rack_profiles([69, 124], timeout_seconds=5, required_counts={69.0: 1, 124.0: 1})

    assert any(profile == [124.0, 124.0, 124.0, 69.0, 69.0, 69.0, 79.0] for profile in profiles)


def test_build_quota_cover_profiles_generates_small_requirement_driven_pool():
    profiles = mod._build_quota_cover_profiles(
        {69.0: 1, 124.0: 1},
        [69, 124],
        max_profiles=5,
    )

    assert len(profiles) <= 5


def test_build_quota_cover_profiles_covers_real_stage6_exact_families():
    for required_counts, slot_sizes in [
        ({124.0: 755, 239.0: 171}, [124, 239]),
        ({69.0: 428, 124.0: 327, 239.0: 171}, [69, 124, 239]),
    ]:
        profiles = mod._build_quota_cover_profiles(required_counts, slot_sizes, max_profiles=40)

        assert profiles
        assert mod._quota_coverage_met(profiles, required_counts)
    assert profiles
    assert all(mod._profile_is_feasible_exact_fill(profile, [69, 124]) for profile in profiles)
    assert any(69 in profile and 124 in profile for profile in profiles)


def test_generate_feasible_rack_profiles_rejects_large_family_before_expensive_search():
    oversized_family = [54, 64, 79, 94, 114, 134, 159, 194, 239]

    profiles = mod._generate_feasible_rack_profiles(
        oversized_family,
        timeout_seconds=2.0,
        required_counts={54.0: 1, 64.0: 1, 79.0: 1, 94.0: 1, 114.0: 1, 134.0: 1, 159.0: 1, 194.0: 1, 239.0: 1},
    )

    assert profiles == []
>>>>>>> Stashed changes


def test_build_deficit_coverage_layout_uses_profile_shortlist(monkeypatch):
    generated_profiles = [
        [69, 124],
        [124, 124],
        [69, 69, 124],
        [124, 124, 124],
    ]
    call_count = {"shortlist": 0}

    monkeypatch.setattr(mod, "_generate_feasible_rack_profiles", lambda *args, **kwargs: generated_profiles)

    def fake_shortlist(profiles, required_counts, limit=None):
        call_count["shortlist"] += 1
        return [[69, 124]]

    monkeypatch.setattr(mod, "_choose_profile_shortlist", fake_shortlist)

    assignments = mod._build_deficit_coverage_layout(
        rack_columns=["A00", "A01"],
        required_counts={69.0: 1, 124.0: 1},
        config_slot_sizes=[69, 124],
    )

    assert assignments
    assert call_count["shortlist"] == 1


<<<<<<< Updated upstream
=======
def test_build_deficit_coverage_layout_keeps_nonempty_partial_assignment(monkeypatch):
    monkeypatch.setattr(
        mod,
        "_generate_feasible_rack_profiles",
        lambda *args, **kwargs: [[69, 124], [124, 124]],
    )
    monkeypatch.setattr(mod, "_choose_profile_shortlist", lambda profiles, required_counts, limit=None: [[69, 124]])
    monkeypatch.setattr(mod, "_run_search_with_profiles", lambda profiles: {"A00": [69.0, 124.0], "A01": []})
    monkeypatch.setattr(mod, "_minimum_required_counts_are_satisfied", lambda *args, **kwargs: False)

    assignments = mod._build_deficit_coverage_layout(
        rack_columns=["A00", "A01"],
        required_counts={69.0: 1, 124.0: 2},
        config_slot_sizes=[69, 124],
    )

    assert assignments["A00"] == [69.0, 124.0]
    assert assignments["A01"] == []


def test_build_deficit_coverage_layout_keeps_partial_completion_pool_assignment(monkeypatch):
    monkeypatch.setattr(mod, "_generate_feasible_rack_profiles", lambda *args, **kwargs: [[69, 124]])
    monkeypatch.setattr(mod, "_choose_profile_shortlist", lambda profiles, required_counts, limit=None: [[69, 124]])
    monkeypatch.setattr(mod, "_run_search_with_profiles", lambda *args, **kwargs: {"A00": [], "A01": [], "B00": [], "B01": []})
    monkeypatch.setattr(mod, "_build_shortage_vector_completion_profiles", lambda *args, **kwargs: [[69, 124]])
    monkeypatch.setattr(mod, "_select_completion_pool_for_layout_columns", lambda pool, required_counts, max_columns: [[69, 124]])

    assignments = mod._build_deficit_coverage_layout(
        rack_columns=["A00", "A01", "B00", "B01"],
        required_counts={69.0: 1, 124.0: 1},
        config_slot_sizes=[69, 124],
    )

    assert assignments["A00"] == [69.0, 124.0]
    assert assignments["A01"] == [69.0, 124.0]
    assert assignments["B00"] == []
    assert assignments["B01"] == []


def test_build_deficit_coverage_layout_filters_overheight_profiles(monkeypatch):
    illegal_profile = [239.0] * 50
    legal_profile = [69.0, 69.0, 69.0]

    monkeypatch.setattr(mod, "_generate_feasible_rack_profiles", lambda *args, **kwargs: [illegal_profile, legal_profile])
    monkeypatch.setattr(mod, "_choose_profile_shortlist", lambda profiles, required_counts, limit=None: [legal_profile])
    monkeypatch.setattr(mod, "_run_search_with_profiles", lambda *args, **kwargs: {"A00": legal_profile, "A01": []})

    assignments = mod._build_deficit_coverage_layout(
        rack_columns=["A00", "A01"],
        required_counts={69.0: 1},
        config_slot_sizes=[69],
    )

    assert assignments["A00"] == legal_profile
    assert assignments["A01"] == []


def test_choose_profile_shortlist_expands_when_initial_pool_misses_required_family():
    profiles = [
        [69, 69],
        [69, 69],
        [69, 69],
        [124, 124],
    ]

    shortlist = mod._choose_profile_shortlist(profiles, {124.0: 1}, limit=2)

    assert any(set(profile) == {124} for profile in shortlist)


def test_choose_profile_shortlist_keeps_profile_for_each_required_family():
    profiles = [
        [239, 239],
        [119, 119],
        [89, 89],
        [89, 89],
    ]

    shortlist = mod._choose_profile_shortlist(profiles, {89.0: 1, 119.0: 1}, limit=2)

    covered_sizes = {int(round(float(size))) for profile in shortlist for size in profile}
    assert 89 in covered_sizes
    assert 119 in covered_sizes


def test_generate_feasible_rack_profiles_keeps_required_family_coverage_before_quota_cap():
    required_counts = {89.0: 2, 119.0: 2, 139.0: 1, 164.0: 2, 189.0: 1, 239.0: 1}

    profiles = mod._generate_feasible_rack_profiles([89, 119, 139, 164, 189, 239], required_counts=required_counts)
    covered = {
        int(round(float(size)))
        for profile in profiles
        for size in mod._effective_requirement_counts(profile, list(required_counts.keys())).keys()
    }

    assert all(int(round(float(size))) in covered for size in required_counts)


def test_generate_feasible_rack_profiles_keeps_multiple_completion_paths_per_required_family():
    required_counts = {89.0: 306, 119.0: 193, 139.0: 52, 164.0: 198, 189.0: 92, 239.0: 85}
    profiles = mod._generate_feasible_rack_profiles([89, 119, 139, 164, 189, 239], required_counts=required_counts)
    for required_size in required_counts:
        size = int(round(float(required_size)))
        assert sum(
            1
            for profile in profiles
            if mod._effective_requirement_counts(profile, list(required_counts.keys())).get(size, 0) > 0
        ) >= 2


def test_profile_keeps_missing_family_alive_until_its_quota_is_met():
    remaining = {89.0: 4, 119.0: 2, 239.0: 1}
    assert mod._profile_keeps_missing_family_alive([89, 89, 89], remaining) is True
    assert mod._profile_keeps_missing_family_alive([239, 239, 239], remaining) is False


def test_remaining_quota_is_pruned_when_completion_is_impossible():
    remaining = {89.0: 2, 119.0: 1}
    pool = [[239, 239], [239, 239]]
    assert mod._remaining_quota_is_still_completable(remaining, pool) is False
    assert mod._remaining_quota_is_still_completable(remaining, [[89, 89], [119, 119]]) is True


def test_shortage_completion_objective_prefers_closing_shortage_vector():
    remaining_a = {89.0: 2, 119.0: 0, 239.0: 1}
    remaining_b = {89.0: 0, 119.0: 2, 239.0: 1}
    assert mod._shortage_completion_objective(remaining_a, [89.0, 119.0, 239.0]) > mod._shortage_completion_objective(remaining_b, [89.0, 119.0, 239.0])


def test_generate_feasible_rack_profiles_keeps_baseline_before_exact_cover_fallback(monkeypatch):
    required_counts = {89.0: 1, 119.0: 1, 139.0: 1}

    def fail_exact_cover(*args, **kwargs):
        raise AssertionError("exact-cover fallback should not be used when baseline coverage already satisfies the required families")

    monkeypatch.setattr(mod, "_build_full_exact_family_completion_pool", fail_exact_cover)

    profiles = mod._generate_feasible_rack_profiles([89, 119, 139], required_counts=required_counts)

    assert profiles
    covered = {
        int(round(float(size)))
        for profile in profiles
        for size in mod._effective_requirement_counts(profile, [89, 119, 139]).keys()
    }
    assert {89, 119, 139}.issubset(covered)


def test_cfg046_layout_assignment_must_satisfy_exact_family_minimums():
    rows = [row for row in mod._read_csv(mod.INPUT_CAPACITY_FILE) if row.get("Config_ID", "").strip() == "CFG_046"]
    required_counts = {float(size): int(count) for size, count in mod._base_exact_counts(rows).items()}
    slot_sizes = mod._slot_sizes_from_capacity(rows)
    assignments = mod._build_deficit_coverage_layout(
        mod.common._build_layout_columns(mod._read_csv(mod.INPUT_PREPARED)),
        required_counts,
        slot_sizes,
        config_id="CFG_046",
    )
    nonempty = [key for key, values in assignments.items() if values]

    assert mod._minimum_required_counts_are_satisfied(
        assignments,
        nonempty,
        slot_sizes,
        required_counts,
    )


def test_exact_cover_completion_pool_selects_nonempty_family_cover():
    required_counts = {69.0: 2, 124.0: 2}
    completion_pool = [
        [69.0, 69.0],
        [124.0, 124.0],
        [69.0, 124.0],
    ]

    selected = mod._select_completion_pool_for_layout_columns(completion_pool, required_counts, max_columns=2)

    assert selected
    assert mod._quota_coverage_met(selected, required_counts)


def test_cfg046_shortage_vector_completion_builder_discovers_required_families():
    rows = [row for row in mod._read_csv(mod.INPUT_CAPACITY_FILE) if row.get("Config_ID", "").strip() == "CFG_046"]
    required_counts = {float(size): int(count) for size, count in mod._base_exact_counts(rows).items()}
    slot_sizes = mod._slot_sizes_from_capacity(rows)
    profiles = mod._generate_feasible_rack_profiles(slot_sizes, required_counts=required_counts)
    completion_profiles = mod._build_shortage_vector_completion_profiles(required_counts, slot_sizes, profiles)

    assert completion_profiles
    covered = {
        int(round(float(size)))
        for profile in completion_profiles
        for size in mod._effective_requirement_counts(profile, slot_sizes).keys()
    }
    assert {89, 119, 139, 164, 189, 239}.issubset(covered)


def test_cfg046_exact_cover_solver_closes_remaining_shortage_vector():
    rows = [row for row in mod._read_csv(mod.INPUT_CAPACITY_FILE) if row.get("Config_ID", "").strip() == "CFG_046"]
    required_counts = {float(size): int(count) for size, count in mod._base_exact_counts(rows).items()}
    slot_sizes = mod._slot_sizes_from_capacity(rows)
    profiles = mod._generate_feasible_rack_profiles(slot_sizes, required_counts=required_counts)
    completion_profiles = mod._build_shortage_vector_completion_profiles(required_counts, slot_sizes, profiles)

    remaining = {
        int(round(float(size))): int(required_counts.get(float(size), 0))
        for size in required_counts
    }
    for profile in completion_profiles:
        for size, count in mod._effective_requirement_counts(profile, slot_sizes).items():
            remaining[int(round(float(size)))] = max(0, remaining.get(int(round(float(size))), 0) - count)

    assert all(value <= 0 for value in remaining.values())


def test_deduplicate_output_columns_keeps_stage6_feasibility_contract_fields():
    fieldnames = [
        "Config_ID",
        "Profile_Generation_Timeout",
        "Rack_Search_Timeout",
        "Layout_Feasible",
        "Allocation_Feasible_Initial",
    ]
    rows = [
        {
            "Config_ID": "CFG_001",
            "Profile_Generation_Timeout": "NO",
            "Rack_Search_Timeout": "NO",
            "Layout_Feasible": "NO",
            "Allocation_Feasible_Initial": "NO",
        },
        {
            "Config_ID": "CFG_002",
            "Profile_Generation_Timeout": "NO",
            "Rack_Search_Timeout": "NO",
            "Layout_Feasible": "NO",
            "Allocation_Feasible_Initial": "NO",
        },
    ]

    cleaned_fields, _ = mod.common._deduplicate_output_columns(fieldnames, rows)

    assert "Rack_Search_Timeout" in cleaned_fields
    assert "Layout_Feasible" in cleaned_fields
    assert "Allocation_Feasible_Initial" in cleaned_fields
>>>>>>> Stashed changes
