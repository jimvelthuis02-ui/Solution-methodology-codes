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


def test_stage6_timeout_budget_is_bounded_per_config():
    assert mod.MAX_STAGE6_CONFIG_TIMEOUT_SECONDS > 0
    assert mod.PROFILE_GENERATION_TIMEOUT_SECONDS > 0
    assert mod.RACK_SEARCH_TIMEOUT_SECONDS > 0
    assert mod.PROFILE_GENERATION_TIMEOUT_SECONDS + mod.RACK_SEARCH_TIMEOUT_SECONDS <= mod.MAX_STAGE6_CONFIG_TIMEOUT_SECONDS + 1e-9


def test_stage6_runtime_budget_uses_full_outer_cap():
    profile_limit, rack_limit, total_limit = mod._stage6_runtime_budget()
    assert total_limit == mod.MAX_STAGE6_CONFIG_TIMEOUT_SECONDS
    assert profile_limit > 0
    assert rack_limit > 0
    assert profile_limit + rack_limit <= total_limit + 1e-9


def test_build_deficit_coverage_layout_handles_profile_timeout(monkeypatch):
    def fake_generate(*args, **kwargs):
        raise mod.Stage6ProfileGenerationTimeout("profile generation exceeded the per-config timeout")

    monkeypatch.setattr(mod, "_generate_feasible_rack_profiles", fake_generate)

    rack_columns = ["A00", "A01", "A02"]
    assignments = mod._build_deficit_coverage_layout(
        rack_columns=rack_columns,
        required_counts={39.0: 1, 54.0: 1},
        config_slot_sizes=[39.0, 54.0],
        config_deadline=0.0,
        config_id="CFG_067",
    )

    assert assignments == {column_key: [] for column_key in rack_columns}
    assert mod._LAST_STAGE6_TIMEOUTS["profile_generation"] is True


def test_stage6_shortlist_limit_is_runtime_aware():
    limit = mod._stage6_shortlist_limit({69.0: 3, 124.0: 2, 189.0: 1}, 60)
    assert limit > 0
    assert limit <= 12
    assert limit <= mod._profile_generation_policy([69, 124, 189])[3]


def test_stage6_hard_residual_configs_keep_wider_shortlist_budget(monkeypatch):
    observed = []

    def fake_shortlist(profiles, required_counts, limit=None):
        observed.append(limit)
        return list(profiles)[:limit] if limit is not None else list(profiles)

    monkeypatch.setattr(mod, "_generate_feasible_rack_profiles", lambda *args, **kwargs: [
        [239, 179, 179, 104],
        [179, 179, 104, 94],
        [179, 139, 94, 94],
        [139, 139, 94, 94],
        [239, 179, 94, 94],
        [239, 139, 94, 94],
        [179, 139, 139, 94],
        [239, 239, 94, 94],
    ])
    monkeypatch.setattr(mod, "_choose_profile_shortlist", fake_shortlist)

    mod._build_deficit_coverage_layout(
        rack_columns=["A00", "A01"],
        required_counts={39.0: 1, 54.0: 1, 79.0: 1, 104.0: 1, 134.0: 1, 179.0: 1, 239.0: 1},
        config_slot_sizes=[39.0, 54.0, 79.0, 104.0, 134.0, 179.0, 239.0],
        config_id="CFG_067",
    )

    assert max(observed) >= 32


def test_stage6_candidate_filter_keeps_k_ge_3_without_slot_focus(monkeypatch):
    monkeypatch.setattr(mod, "_target_config_ids_from_environment", lambda: [])
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


def test_floor_mapping_uses_dominant_family_order():
    config_values = [69, 124, 189, 239]

    assert mod._effective_requirement_slot_size(79, [239, 124, 124, 79], config_values) == 69
    assert mod._effective_requirement_slot_size(114, [239, 124, 124, 114], config_values) == 69
    assert mod._effective_requirement_slot_size(64, [239, 189, 124, 64], config_values) is None


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
    assignments = {
        "A00": [69, 124, 124, 69],
        "A01": [69, 124, 124, 69],
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


def test_build_deficit_coverage_layout_keeps_quota_ready_shortlist_in_search(monkeypatch):
    generated_profiles = [
        [69, 124],
        [124, 124],
        [69, 69, 124],
    ]
    shortlist_calls = []

    monkeypatch.setattr(mod, "_generate_feasible_rack_profiles", lambda *args, **kwargs: generated_profiles)

    def fake_shortlist(profiles, required_counts, limit=None):
        shortlist_calls.append(limit)
        return list(profiles)

    monkeypatch.setattr(mod, "_choose_profile_shortlist", fake_shortlist)

    assignments = mod._build_deficit_coverage_layout(
        rack_columns=["A00", "A01"],
        required_counts={69.0: 1, 124.0: 1},
        config_slot_sizes=[69, 124],
    )

    assert assignments
    assert shortlist_calls


def test_profile_requirement_priority_prefers_rare_required_family():
    required_counts = {69.0: 3, 124.0: 1, 189.0: 1}
    common_profile = [69, 69, 69]
    rare_profile = [124, 124]

    assert mod._profile_requirement_priority(common_profile, required_counts) < mod._profile_requirement_priority(rare_profile, required_counts)
    assert mod._rare_family_priority(rare_profile, required_counts) > mod._rare_family_priority(common_profile, required_counts)


def test_rare_family_priority_handles_empty_remaining_requirements():
    assert mod._rare_family_priority([69, 124], {}) == 0
    assert mod._rare_family_priority([69, 124], {69.0: 0, 124.0: 0}) == 0


def test_k_aware_profile_generation_policy_uses_a_wider_window_for_k4():
    k4 = mod._k_aware_profile_generation_policy(4)

    assert k4[0] >= 20
    assert k4[2] >= 38
    assert k4[3] >= 28


def test_k_aware_profile_generation_policy_expands_small_k_and_limits_large_k():
    k3 = mod._k_aware_profile_generation_policy(3)
    k4 = mod._k_aware_profile_generation_policy(4)
    k5 = mod._k_aware_profile_generation_policy(5)
    k8 = mod._k_aware_profile_generation_policy(8)

    assert k3[3] > k4[3] > k5[3] >= k8[3]
    assert k3[0] > k4[0] > k5[0] >= k8[0]
    assert k3[2] >= k4[2] >= k5[2] >= k8[2]


def test_choose_profile_shortlist_preserves_rare_families_for_large_configs():
    profiles = [
        [69, 69, 69],
        [69, 124],
        [124, 124],
        [189, 189],
        [239, 239],
        [124, 189],
        [189, 239],
    ]
    required_counts = {69.0: 2, 124.0: 1, 189.0: 1, 239.0: 1}

    shortlist = mod._choose_profile_shortlist(profiles, required_counts, limit=4)

    assert any(189 in profile for profile in shortlist)
    assert any(239 in profile for profile in shortlist)


def test_profile_effectively_covers_required_family_for_topfills():
    required_counts = {39.0: 1, 54.0: 1, 104.0: 1, 134.0: 1}
    profile = [154.0, 104.0, 54.0]

    assert mod._profile_effectively_covers_required_size(profile, 134, required_counts) is True
    assert mod._profile_effectively_covers_required_size(profile, 154, required_counts) is False


def test_choose_profile_shortlist_preserves_rare_high_size_profiles_for_cfg067():
    profiles = [
        [39, 39, 39, 39, 39, 39, 39, 39, 39, 39, 39, 39, 39, 39],
        [79, 54, 39, 39, 39, 39, 39, 39, 39, 39, 39, 39, 39],
        [134, 54, 39, 39, 39, 39, 39, 39, 39, 39, 39, 39],
        [179, 104, 54, 39, 39, 39, 39, 39, 39, 39],
        [239, 179, 54, 39, 39, 39, 39, 39, 39, 39],
        [179, 79, 79, 39, 39, 39, 39, 39, 39, 39],
        [239, 239, 104, 54, 54],
    ]
    required_counts = {39.0: 136, 54.0: 159, 79.0: 187, 104.0: 192, 134.0: 107, 179.0: 74, 239.0: 71}

    shortlist = mod._choose_profile_shortlist(profiles, required_counts, limit=6)

    assert any(179 in profile for profile in shortlist)
    assert any(239 in profile for profile in shortlist)


def test_choose_profile_shortlist_reserves_large_required_sizes_before_ranked_fill():
    profiles = [
        [49, 49, 49, 49, 49, 49],
        [64, 64, 49, 49],
        [79, 79, 64, 49],
        [104, 104, 49, 49],
        [134, 49, 49],
        [184, 184, 49],
        [239, 49, 49],
    ]
    required_counts = {49.0: 5, 64.0: 3, 79.0: 2, 104.0: 2, 134.0: 2, 184.0: 1, 239.0: 1}

    shortlist = mod._choose_profile_shortlist(profiles, required_counts, limit=4)

    assert any(184 in profile for profile in shortlist)
    assert any(239 in profile for profile in shortlist)


def test_build_deficit_coverage_layout_prefers_rare_family_branches_for_hard_residuals(monkeypatch):
    generated_profiles = [
        [39, 39, 39, 39, 39, 39],
        [104, 39, 39, 39, 39],
        [179, 39, 39, 39, 39],
        [239, 179, 39, 39, 39],
    ]

    monkeypatch.setattr(mod, "_generate_feasible_rack_profiles", lambda *args, **kwargs: generated_profiles)
    monkeypatch.setattr(mod, "_choose_profile_shortlist", lambda profiles, required_counts, limit=None: list(profiles))

    assignments = mod._build_deficit_coverage_layout(
        rack_columns=["A00", "A01"],
        required_counts={39.0: 1, 104.0: 1, 179.0: 1, 239.0: 1},
        config_slot_sizes=[39, 104, 179, 239],
        config_id="CFG_067",
    )

    assert assignments
    assert any(239 in profile for profile in assignments.values())


def test_branch_selection_prefers_profile_covering_largest_unmet_family():
    remaining = {39.0: 1, 104.0: 1, 179.0: 1, 239.0: 1}
    columns = ["A00", "A01"]
    weak_branch = (
        (1, 1, 1, 1, 1, 1),
        {"A00": [104, 39], "A01": [104, 39]},
    )
    strong_branch = (
        (1, 1, 1, 1, 1, 1),
        {"A00": [239, 39], "A01": [239, 39]},
    )

    weak_key = mod._branch_selection_sort_key(weak_branch, remaining, columns)
    strong_key = mod._branch_selection_sort_key(strong_branch, remaining, columns)

    assert strong_key > weak_key


def test_build_deficit_coverage_layout_rejects_shortfall_profile_mix(monkeypatch):
    generated_profiles = [
        [94, 94],
        [94, 94],
    ]

    monkeypatch.setattr(mod, "_generate_feasible_rack_profiles", lambda *args, **kwargs: generated_profiles)
    monkeypatch.setattr(mod, "_choose_profile_shortlist", lambda profiles, required_counts, limit=None: list(profiles))

    assignments = mod._build_deficit_coverage_layout(
        rack_columns=["A00", "A01"],
        required_counts={74.0: 2, 94.0: 2},
        config_slot_sizes=[74, 94],
    )

    assert assignments == {}


def test_build_deficit_coverage_layout_falls_back_to_covering_shortlist(monkeypatch):
    generated_profiles = [
        [239, 54, 54, 54, 54, 54, 39, 39, 39],
        [179, 104, 54, 39, 39, 39, 39, 39, 39, 39],
        [134, 54, 39, 39, 39, 39, 39, 39, 39, 39, 39, 39],
    ]

    monkeypatch.setattr(mod, "_generate_feasible_rack_profiles", lambda *args, **kwargs: generated_profiles)
    monkeypatch.setattr(mod, "_choose_profile_shortlist", lambda profiles, required_counts, limit=None: list(profiles))

    assignments = mod._build_deficit_coverage_layout(
        rack_columns=["A00", "A01"],
        required_counts={39.0: 1, 54.0: 1, 104.0: 1, 134.0: 1, 179.0: 1, 239.0: 1},
        config_slot_sizes=[39, 54, 104, 134, 179, 239],
        config_id="CFG_067",
    )

    assert assignments
    assert any(profile for profile in assignments.values())


def test_generate_feasible_rack_profiles_allows_profiles_longer_than_family_size(monkeypatch):
    def fake_profile_is_valid(profile, available_slot_sizes=None):
        return len(profile) >= 6 and all(float(value) > 0.0 for value in profile)

    monkeypatch.setattr(mod, "_profile_is_feasible_exact_fill", fake_profile_is_valid)
    monkeypatch.setattr(mod, "_legal_topfill_values", lambda *args, **kwargs: {94, 114, 164, 189, 239})

    profiles = mod._generate_feasible_rack_profiles_cached(
        (94.0, 114.0, 164.0, 189.0, 239.0),
        timeout_seconds=1.0,
        required_counts=((94.0, 1), (114.0, 1), (164.0, 1), (189.0, 1), (239.0, 1)),
    )

    assert any(len(profile) > 5 for profile in profiles)


def test_generate_feasible_rack_profiles_keeps_legal_topfill_even_if_not_in_family(monkeypatch):
    def fake_profile_is_valid(profile, available_slot_sizes=None):
        return tuple(sorted(profile, reverse=True)) == (174.0, 174.0, 124.0, 124.0, 94.0)

    monkeypatch.setattr(mod, "_profile_is_feasible_exact_fill", fake_profile_is_valid)
    monkeypatch.setattr(mod, "_legal_topfill_values", lambda *args, **kwargs: {94, 119, 174})

    profiles = mod._generate_feasible_rack_profiles_cached(
        (84.0, 124.0, 174.0, 239.0),
        timeout_seconds=1.0,
        required_counts=((84.0, 1), (124.0, 1), (174.0, 1), (239.0, 1)),
    )

    assert any(tuple(sorted(profile, reverse=True)) == (174.0, 174.0, 124.0, 124.0, 94.0) for profile in profiles)


def test_generate_feasible_rack_profiles_keeps_repeated_lower_value_residuals_for_k4():
    profiles = mod._generate_feasible_rack_profiles_cached(
        (84.0, 124.0, 174.0, 239.0),
        timeout_seconds=10.0,
        required_counts=((84.0, 312), (124.0, 236), (174.0, 279), (239.0, 99)),
    )

    profile_values = {tuple(int(round(float(value))) for value in profile) for profile in profiles}
    assert any(
        any(value == 174 for value in profile) and profile.count(174) >= 2 and 94 in profile
        for profile in profile_values
    )


def test_generate_feasible_rack_profiles_keeps_mixed_prefix_residuals_for_k5():
    profiles = mod._generate_feasible_rack_profiles_cached(
        (74.0, 94.0, 139.0, 179.0, 239.0),
        timeout_seconds=10.0,
        required_counts=((74.0, 229), (94.0, 85), (139.0, 226), (179.0, 236), (239.0, 150)),
    )

    profile_values = {tuple(int(round(float(value))) for value in profile) for profile in profiles}
    assert any(
        179 in profile and 74 in profile and profile.count(74) >= 3
        for profile in profile_values
    )


def test_hard_residual_search_family_covers_known_trigger_neighborhoods():
    assert mod._hard_residual_search_family((84.0, 124.0, 174.0, 239.0))
    assert mod._hard_residual_search_family((89.0, 134.0, 189.0, 239.0))
    assert mod._hard_residual_search_family((74.0, 94.0, 139.0, 179.0, 239.0))
    assert mod._hard_residual_search_family((39.0, 54.0, 79.0, 104.0, 134.0, 179.0, 239.0))
