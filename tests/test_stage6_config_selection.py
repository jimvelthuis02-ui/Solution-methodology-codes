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


def test_stage6_output_dir_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_STAGE6_OUTPUT_DIR", str(tmp_path / "stage6_debug"))
    spec = importlib.util.spec_from_file_location("stage6_override", STAGE6_PATH)
    override_module = importlib.util.module_from_spec(spec)
    sys.modules["stage6_override"] = override_module
    spec.loader.exec_module(override_module)
    assert override_module.LAYOUT_OUTPUT_DIR == tmp_path / "stage6_debug"


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


def test_layout_feasibility_reason_reports_minimum_count_shortage():
    assignments = {
        "A00": [69, 124, 69, 69],
        "A01": [69, 124, 69, 69],
    }

    reason = mod._layout_feasibility_reason(
        assignments,
        ["A00", "A01"],
        69,
        [69, 124],
        minimum_required_counts={69.0: 8, 124.0: 4},
        enforce_minimum_total_locations=False,
        assigned_locations_total=16,
        required_locations_total=16,
        capacity_margin=-4,
        space_utilization=0.8,
    )

    assert "minimum family counts" in reason.lower()
    assert "not satisfied" in reason.lower()


def test_layout_feasibility_reason_defers_minimum_count_check_until_layout_complete():
    assignments = {
        "A00": [69, 124, 124, 69],
        "A01": [69, 124, 124, 69],
    }

    reason = mod._layout_feasibility_reason(
        assignments,
        ["A00", "A01"],
        69,
        [69, 124],
        minimum_required_counts={69.0: 10, 124.0: 8},
        enforce_minimum_total_locations=False,
        assigned_locations_total=16,
        required_locations_total=20,
        capacity_margin=-4,
        space_utilization=0.8,
    )

    assert "minimum family counts" not in reason.lower()
    assert "assigned locations" in reason.lower()


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


def test_profile_requirement_priority_prefers_largest_unmet_family_shortage():
    remaining = {69.0: 6, 124.0: 3}
    shortage_prioritized = [124, 124, 124]
    generic_69_heavy = [69, 69, 69, 69, 69]

    assert mod._profile_requirement_priority(shortage_prioritized, remaining) > mod._profile_requirement_priority(generic_69_heavy, remaining)


def test_profile_requirement_priority_prefers_biggest_shortage_even_when_requirements_are_equal_counted():
    remaining = {69.0: 5, 124.0: 8}
    shortage_prioritized = [124, 124, 124]
    generic_69_heavy = [69, 69, 69]

    assert mod._profile_requirement_priority(shortage_prioritized, remaining) < mod._profile_requirement_priority(generic_69_heavy, remaining)


def test_layout_slot_distribution_uses_actual_assigned_slot_values():
    rows = [
        {"Rack": "A", "Column": "1", "Row": "1", "Assigned_Slot_Size_cm": "239", "Usable_Location": "YES"},
        {"Rack": "A", "Column": "1", "Row": "2", "Assigned_Slot_Size_cm": "189", "Usable_Location": "YES"},
        {"Rack": "A", "Column": "1", "Row": "3", "Assigned_Slot_Size_cm": "124", "Usable_Location": "YES"},
        {"Rack": "A", "Column": "1", "Row": "4", "Assigned_Slot_Size_cm": "69", "Usable_Location": "YES"},
        {"Rack": "A", "Column": "1", "Row": "5", "Assigned_Slot_Size_cm": "69", "Usable_Location": "YES"},
    ]

    distribution, cumulative = mod._slot_signatures_from_location_rows(rows, [69, 124, 189, 239])
    assert distribution == "69:2|124:1|189:1|239:1"
    assert cumulative == "69:5|124:3|189:2|239:1"

    rack_rows = mod._rack_profile_rows_from_location_rows("LAY_001", "CFG_004", rows, [69, 124, 189, 239])
    assert rack_rows[0]["Slot_Size_Distribution"] == "69:2|124:1|189:1|239:1"
    assert rack_rows[0]["Rack_Profile_Order"] == "239,189,124,69,69"


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


def test_profile_generator_keeps_valid_profiles_longer_than_config_family():
    profiles = mod._generate_feasible_rack_profiles([69, 124, 189, 239])
    profile_values = {tuple(sorted((int(round(float(value))) for value in profile), reverse=True)) for profile in profiles}

    assert (239, 124, 124, 124, 79) in profile_values


def test_profile_generator_allows_repeated_top_slot_values():
    profiles = mod._generate_feasible_rack_profiles([84, 114, 194, 239])
    profile_values = {tuple(sorted((int(round(float(value))) for value in profile), reverse=True)) for profile in profiles}

    assert (239, 239, 114, 114) in profile_values


def test_profile_generator_expands_for_constrained_target_family():
    profiles = mod._generate_feasible_rack_profiles([84, 114, 194, 239])
    profile_values = {tuple(sorted((int(round(float(value))) for value in profile), reverse=True)) for profile in profiles}

    assert len(profile_values) >= 5
    assert any(194 in profile and 84 in profile for profile in profile_values)


def test_profile_generator_targets_required_family_pool_for_hard_families():
    required_counts = {89.0: 42, 119.0: 31, 164.0: 29, 189.0: 18, 239.0: 14}
    targeted_pool = mod._generate_required_family_cover_profiles([89, 119, 164, 189, 239], required_counts)

    assert targeted_pool
    assert mod._quota_coverage_met(targeted_pool, required_counts)


def test_profile_generator_meets_quota_coverage_for_3_family_exact_case():
    required_counts = {69.0: 428, 124.0: 327, 239.0: 171}
    profiles = mod._generate_feasible_rack_profiles([69, 124, 239], required_counts=required_counts)

    assert profiles
    assert mod._quota_coverage_met(profiles, required_counts)


def test_profile_generator_meets_quota_coverage_for_hard_exact_family():
    required_counts = {84.0: 373, 114.0: 216, 194.0: 265, 239.0: 72}
    profiles = mod._generate_feasible_rack_profiles([84, 114, 194, 239], required_counts=required_counts)

    assert mod._quota_coverage_met(profiles, required_counts)
