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


def test_floor_mapping_uses_dominant_family_order():
    config_values = [69, 124, 189, 239]

    assert mod._effective_requirement_slot_size(79, [239, 124, 124, 79], config_values) == 69
    assert mod._effective_requirement_slot_size(114, [239, 124, 124, 114], config_values) == 69
    assert mod._effective_requirement_slot_size(64, [239, 189, 124, 64], config_values) is None
    assert mod._effective_requirement_slot_size(79, [239, 124, 124, 124, 79], [124, 239]) == 124


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


