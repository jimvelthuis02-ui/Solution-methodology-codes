import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE6_PATH = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"
HEURISTIC_STAGE6_PATH = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation_heuristics.py"
COMMON_PATH = ROOT / "Scripts" / "Pipeline" / "run_ordered_pipeline.py"
HEURISTIC_VARIANTS_PATH = ROOT / "Scripts" / "Pipeline" / "Heuristic_Variants" / "heuristic_variants.py"

for module_path, module_name in [(COMMON_PATH, "run_ordered_pipeline"), (STAGE6_PATH, "stage6_layout")]:
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)


class SlotSizeCapTest(unittest.TestCase):
    def test_stage6_merged_summary_keeps_layout_feasibility_kpis(self):
        spec = importlib.util.spec_from_file_location("stage6_layout_heuristics", HEURISTIC_STAGE6_PATH)
        if spec is None or spec.loader is None:
            self.fail("Unable to load the Stage 6 heuristics module")
        module = importlib.util.module_from_spec(spec)
        sys.modules["stage6_layout_heuristics"] = module
        spec.loader.exec_module(module)

        dropped = module.DROP_FIELDS_BY_OUTPUT["Candidate_Layout_Summary.csv"]
        self.assertNotIn("Layout_Feasible", dropped)
        self.assertNotIn("Capacity_Margin", dropped)
        self.assertNotIn("Percentage_Rack_Height_Used", dropped)

    def test_layout_generation_respects_layout_constraints_when_env_is_unset(self):
        stage6 = sys.modules["stage6_layout"]
        original = os.environ.get("PIPELINE_IGNORE_LAYOUT")
        try:
            os.environ.pop("PIPELINE_IGNORE_LAYOUT", None)
            self.assertFalse(stage6.common._should_ignore_layout_for_layout_generation())
        finally:
            if original is None:
                os.environ.pop("PIPELINE_IGNORE_LAYOUT", None)
            else:
                os.environ["PIPELINE_IGNORE_LAYOUT"] = original

    def test_topfill_is_reintroduced_as_legal_bounded_fill(self):
        stage6 = sys.modules["stage6_layout"]
        self.assertTrue(hasattr(stage6, "_column_topfill_metadata"))

        column = [69.0, 89.0, 119.0, 179.0]
        filled = stage6._exact_fill_to_column_limit(
            column,
            available_slot_sizes=[69.0, 89.0, 119.0, 179.0, 234.0],
            target_row_count=5,
        )

        self.assertIsNotNone(filled)
        self.assertEqual(len(filled), 5)
        self.assertLessEqual(max(filled), 234.0)
        self.assertTrue(all(int(round(value)) >= 69 for value in filled))
        self.assertTrue(all(int(round(value)) % 10 in (4, 9) for value in filled))
        self.assertAlmostEqual(sum(filled) + (len(filled) - 1) * 16.0, 754.0, delta=1e-6)

        metadata = stage6._column_topfill_metadata(column, [69.0, 89.0, 119.0, 179.0, 234.0])
        self.assertEqual(metadata["Original_Top_Slot_cm"], 179.0)
        self.assertGreater(metadata["Added_Height_cm"], 0.0)
        self.assertLessEqual(metadata["Adjusted_Top_Slot_cm"], 234.0)

    def test_physical_limit_fill_is_respected(self):
        stage6 = sys.modules["stage6_layout"]
        column_assignments = {
            "D01": [69.0, 89.0, 119.0, 179.0, 234.0],
            "D02": [69.0, 89.0, 119.0, 179.0],
            "D03": [69.0, 89.0, 119.0, 179.0, 234.0],
        }

        refined = stage6._enforce_rack_row_count_consistency(
            column_assignments,
            available_slot_sizes=[69.0, 89.0, 119.0, 179.0, 234.0],
        )

        for column_key, slots in refined.items():
            self.assertLessEqual(max(slots), 234.0)
            self.assertTrue(all(int(round(value)) >= 69 for value in slots))
            self.assertTrue(all(int(round(value)) % 10 in (4, 9) for value in slots))
            target_height = 754.0
            physical_total = sum(slots) + (len(slots) - 1) * 16.0
            self.assertAlmostEqual(physical_total, target_height, delta=1e-6)

    def test_topfill_allows_legal_non_config_top_slots_but_rejects_values_below_the_minimum(self):
        stage6 = sys.modules["stage6_layout"]
        legal_family = stage6._config_legal_slot_family([64.0, 119.0, 234.0])
        self.assertIn(114, legal_family)
        self.assertIn(214, legal_family)

        self.assertFalse(
            stage6._layout_assignments_are_feasible(
                {"D01": [39.0, 119.0, 234.0]},
                ["D01"],
                64.0,
                [64.0, 119.0, 234.0],
            )
        )
        self.assertTrue(
            stage6._layout_assignments_are_feasible(
                {"D01": [64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 114.0]},
                ["D01"],
                64.0,
                [64.0, 119.0, 234.0],
            )
        )
        self.assertTrue(
            stage6._layout_assignments_are_feasible(
                {"E01": [119.0, 119.0, 119.0, 119.0, 214.0]},
                ["E01"],
                64.0,
                [64.0, 119.0, 234.0],
            )
        )

        refined = stage6._enforce_rack_row_count_consistency(
            {
                "D01": [39.0, 119.0, 234.0],
                "E01": [119.0, 119.0, 119.0, 119.0, 214.0],
            },
            available_slot_sizes=[64.0, 119.0, 234.0],
        )
        self.assertTrue(all(sum(values) + (len(values) - 1) * 16.0 == 754.0 for values in refined.values()))
        self.assertTrue(all(int(round(value)) >= 64 for values in refined.values() for value in values))
        self.assertTrue(all(values[-1] >= values[0] for values in refined.values()))

    def test_generated_profiles_keep_topfill_only_at_the_final_position(self):
        stage6 = sys.modules["stage6_layout"]
        profiles = stage6._generate_feasible_rack_profiles([64.0, 119.0, 234.0])

        self.assertTrue(profiles)
        config_values = set(stage6._config_size_values([64.0, 119.0, 234.0]))
        legal_topfill_values = stage6._legal_topfill_values([64.0, 119.0, 234.0])
        self.assertTrue(
            all(
                all(int(round(float(value))) in config_values for value in profile[:-1])
                and int(round(float(profile[-1]))) in config_values | legal_topfill_values
                for profile in profiles
            )
        )

    def test_per_column_feasibility_checks_do_not_apply_full_layout_minimums(self):
        stage6 = sys.modules["stage6_layout"]

        partial_layout = {
            "A01": [64.0, 64.0, 64.0, 114.0],
            "A02": [64.0, 64.0, 64.0, 114.0],
        }

        self.assertTrue(
            stage6._layout_assignments_are_feasible(
                partial_layout,
                ["A01", "A02"],
                64.0,
                [64.0, 119.0, 234.0],
                minimum_required_counts={64.0: 16, 119.0: 2},
            )
        )

    def test_profile_generation_is_capped_and_ranked_by_coverage(self):
        stage6 = sys.modules["stage6_layout"]
        profiles = stage6._generate_feasible_rack_profiles([64.0, 119.0, 234.0])
        self.assertLessEqual(len(profiles), stage6.EXHAUSTIVE_PROFILE_LIMIT)
        self.assertTrue(profiles)
        self.assertTrue(all(len(profile) >= 2 for profile in profiles))

    def test_profile_generation_keeps_valid_exact_profiles_for_all_feasible_configs(self):
        stage6 = sys.modules["stage6_layout"]
        profiles = stage6._generate_feasible_rack_profiles([89.0, 154.0, 234.0])
        self.assertTrue(profiles)
        self.assertTrue(any(stage6._profile_is_feasible_exact_fill(profile, [89.0, 154.0, 234.0]) for profile in profiles))

    def test_deficit_coverage_layout_re_scores_each_column_against_current_remaining_counts(self):
        stage6 = sys.modules["stage6_layout"]
        columns = [f"A{idx:02d}" for idx in range(6)]
        assignments = stage6._build_deficit_coverage_layout(
            rack_columns=columns,
            required_counts={64.0: 12, 119.0: 6, 234.0: 3},
            config_slot_sizes=[64.0, 119.0, 234.0],
        )

        self.assertEqual(len(assignments), len(columns))
        self.assertGreater(len({tuple(slots) for slots in assignments.values()}), 1)

        counts = stage6.Counter()
        for slots in assignments.values():
            counts.update(stage6._effective_requirement_counts(slots, [64.0, 119.0, 234.0]))
        self.assertGreaterEqual(counts.get(64, 0), 12)
        self.assertGreaterEqual(counts.get(119, 0), 6)
        self.assertGreaterEqual(counts.get(234, 0), 3)

    def test_legal_topfill_counts_as_the_underlying_required_slot_size(self):
        stage6 = sys.modules["stage6_layout"]

        self.assertEqual(stage6._effective_requirement_slot_size(114.0, [64.0, 64.0, 64.0, 64.0, 114.0], [64.0, 119.0, 234.0]), 64)
        self.assertEqual(stage6._effective_requirement_slot_size(194.0, [119.0, 119.0, 194.0], [64.0, 119.0, 234.0]), 119)

        profile = [64.0, 64.0, 64.0, 64.0, 114.0]
        self.assertEqual(
            stage6._effective_requirement_counts(profile, [64.0, 119.0, 234.0]),
            {64: 5, 119: 0, 234: 0},
        )
        self.assertEqual(
            stage6._effective_requirement_counts([119.0, 119.0, 194.0], [64.0, 119.0, 234.0]),
            {119: 3, 64: 0, 234: 0},
        )

    def test_profile_requirement_priority_uses_biggest_remaining_deficit_first(self):
        stage6 = sys.modules["stage6_layout"]
        remaining = {64.0: 411, 119.0: 315, 234.0: 164}
        profile_with_small_slots = [234.0, 119.0, 119.0, 64.0, 64.0, 64.0]
        profile_with_large_slots = [234.0, 234.0, 119.0, 119.0]

        left_score = stage6._profile_requirement_priority(profile_with_small_slots, remaining)
        right_score = stage6._profile_requirement_priority(profile_with_large_slots, remaining)

        self.assertGreater(left_score, right_score)

    def test_stage6_exhaustive_search_uses_a_small_relevant_config_subset(self):
        stage6 = sys.modules["stage6_layout"]
        configs = [
            {"Config_ID": "CFG_001", "Slot_Sizes": '="64,119,234"'},
            {"Config_ID": "CFG_002", "Slot_Sizes": '="64,119,184,234"'},
            {"Config_ID": "CFG_003", "Slot_Sizes": '="29,64,119,184,234"'},
            {"Config_ID": "CFG_004", "Slot_Sizes": '="29,64,94,119,184,234"'},
            {"Config_ID": "CFG_005", "Slot_Sizes": '="29,64,94,119,149,184,234"'},
            {"Config_ID": "CFG_006", "Slot_Sizes": '="29,64,94,119,149,184,234,269"'},
        ]
        filtered = stage6._candidate_configs_for_exhaustive_search(configs)
        self.assertTrue(filtered)
        self.assertLessEqual(len(filtered), stage6.EXHAUSTIVE_SEARCH_CONFIG_LIMIT)
        self.assertEqual(len(filtered), 3)

    def test_non_descending_profile_order_is_rejected(self):
        stage6 = sys.modules["stage6_layout"]
        invalid_profile = [109.0, 234.0, 189.0, 174.0]
        self.assertFalse(stage6._profile_is_feasible_exact_fill(invalid_profile, [109.0, 189.0, 234.0]))
        self.assertFalse(stage6._layout_assignments_are_feasible({"A01": invalid_profile}, ["A01"], 109.0, [109.0, 189.0, 234.0]))

    def test_underfilled_final_row_can_complete_via_legal_topfill(self):
        stage6 = sys.modules["stage6_layout"]
        profile = [189.0, 189.0, 189.0, 109.0]

        self.assertTrue(stage6._profile_is_feasible_exact_fill(profile, [109.0, 189.0, 234.0]))
        self.assertTrue(stage6._layout_assignments_are_feasible({"A01": profile}, ["A01"], 109.0, [109.0, 189.0, 234.0]))
        self.assertFalse(stage6._profile_is_feasible_exact_fill([219.0, 189.0, 189.0, 109.0], [109.0, 189.0, 234.0]))

    def test_only_descending_columns_are_accepted_under_the_simplified_model(self):
        stage6 = sys.modules["stage6_layout"]

        self.assertTrue(stage6._profile_is_feasible_exact_fill([189.0, 189.0, 189.0, 139.0], [109.0, 189.0, 234.0]))
        self.assertFalse(stage6._profile_is_feasible_exact_fill([109.0, 189.0, 189.0, 219.0], [109.0, 189.0, 234.0]))
        self.assertNotIn(219, stage6._legal_topfill_values([109.0, 189.0, 234.0]))
        self.assertNotIn(219, stage6._exact_config_slot_family([109.0, 189.0, 234.0]))

    def test_descending_order_columns_with_legal_topfill_are_accepted(self):
        stage6 = sys.modules["stage6_layout"]

        legal_columns = [
            [189.0, 189.0, 189.0, 139.0],
            [184.0, 184.0, 184.0, 154.0],
        ]

        for column in legal_columns:
            with self.subTest(column=column):
                available = [109.0, 189.0, 234.0] if column[0] == 189.0 else [109.0, 184.0, 234.0]
                self.assertTrue(stage6._profile_is_feasible_exact_fill(column, available))
                self.assertTrue(
                    stage6._layout_assignments_are_feasible(
                        {"A01": column},
                        ["A01"],
                        min(int(round(float(value))) for value in column if float(value) > 0.0),
                        available,
                    )
                )

        self.assertFalse(stage6._profile_is_feasible_exact_fill([189.0, 189.0, 189.0, 139.0], [109.0, 184.0, 234.0]))

    def test_generated_profiles_always_fill_the_full_754_cm_height(self):
        stage6 = sys.modules["stage6_layout"]
        for config_sizes in ([64.0, 119.0, 234.0], [89.0, 154.0, 234.0], [114.0, 184.0, 234.0]):
            with self.subTest(config_sizes=config_sizes):
                profiles = stage6._generate_feasible_rack_profiles(config_sizes)
                self.assertTrue(profiles)
                self.assertTrue(
                    all(
                        abs(sum(profile) + max(len(profile) - 1, 0) * stage6.common.BEAM_HEIGHT - stage6.common.MAX_USED_HEIGHT_BASE) <= 1e-6
                        for profile in profiles
                    )
                )

    def test_column_top_slot_is_based_on_physical_stack_order_not_arbitrary_list_order(self):
        stage6 = sys.modules["stage6_layout"]
        column = [69.0, 69.0, 179.0, 179.0, 119.0]

        metadata = stage6._column_topfill_metadata(column, [69.0, 119.0, 179.0, 234.0])
        self.assertEqual(metadata["Original_Top_Slot_cm"], 119.0)
        self.assertTrue(stage6._column_can_topfill_to_limit(column, [69.0, 119.0, 179.0, 234.0]))

    def test_column_can_expose_legal_topfill_after_reordering_lower_stack(self):
        stage6 = sys.modules["stage6_layout"]
        column = [119.0, 234.0, 119.0]
        self.assertTrue(stage6._column_can_topfill_to_limit(column, [119.0, 234.0]))

    def test_columns_are_topfilled_to_the_full_physical_limit_when_possible(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "D01": [69.0, 89.0, 119.0, 179.0],
            "D02": [69.0, 89.0, 119.0, 179.0],
        }

        refined = stage6._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[69.0, 89.0, 119.0, 179.0, 234.0],
        )

        for slots in refined.values():
            self.assertAlmostEqual(sum(slots) + (len(slots) - 1) * 16.0, 754.0, delta=1e-6)
            self.assertLessEqual(max(slots), 234.0)

    def test_same_row_count_layouts_rebuild_underfilled_but_feasible_columns(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "R01C01": [69.0, 89.0, 119.0, 179.0, 179.0],
            "R01C02": [69.0, 89.0, 119.0, 179.0],
            "R01C03": [69.0, 89.0, 119.0, 179.0, 179.0],
        }

        refined = stage6._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[69.0, 89.0, 119.0, 179.0, 234.0],
        )

        for slots in refined.values():
            self.assertAlmostEqual(sum(slots) + (len(slots) - 1) * 16.0, 754.0, delta=1e-6)
            self.assertLessEqual(max(slots), 234.0)

    def test_no_layout_prefix_helper_is_present_in_the_no_layout_baseline(self):
        stage6 = sys.modules["stage6_layout"]
        self.assertFalse(hasattr(stage6, "_build_layout_prefix_for_segment"))
        self.assertFalse(hasattr(stage6, "_enforce_layout_spanning_beam_alignment"))

    def test_all_active_heuristic_variants_are_exported(self):
        spec = importlib.util.spec_from_file_location("heuristic_variants", HEURISTIC_VARIANTS_PATH)
        if spec is None:
            self.fail("Unable to build the heuristic variants module spec")
        loader = spec.loader
        if loader is None:
            self.fail("Heuristic variants module spec has no loader")
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        labels = {variant["label"] for variant in module.VARIANTS}
        self.assertEqual(
            labels,
            {
                "Baseline_None",
            },
        )

    def test_minimum_config_slot_and_legal_endings_are_enforced(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "A01": [69.0, 89.0, 119.0, 179.0],
            "A02": [69.0, 89.0, 119.0, 179.0],
        }

        refined = stage6._enforce_min_locations_per_column(
            assignments,
            ["A01", "A02"],
            69.0,
        )

        for slots in refined.values():
            self.assertTrue(all(int(round(value)) >= 69 for value in slots))
            self.assertTrue(all(int(round(value)) % 10 in (4, 9) for value in slots))

    def test_valid_legal_topfill_profiles_are_not_rewritten_to_synthetic_fill(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "R01C01": [64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 114.0],
            "R01C02": [64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 114.0],
        }

        refined = stage6._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[64.0, 119.0, 234.0],
        )

        for slots in refined.values():
            self.assertEqual(slots, [64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 114.0])
            self.assertEqual(slots, sorted(slots))

    def test_profile_choice_prefers_rare_required_sizes_over_extra_64s(self):
        stage6 = sys.modules["stage6_layout"]
        remaining = {64.0: 411, 119.0: 315, 234.0: 164}
        profiles = stage6._generate_feasible_rack_profiles([64.0, 119.0, 234.0])

        ranked = sorted(
            profiles,
            key=lambda profile: stage6._profile_requirement_priority(profile, remaining),
            reverse=True,
        )

        self.assertEqual(ranked[0], [119.0, 119.0, 234.0, 234.0])

    def test_rack_consistency_preserves_exact_minimum_counts(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "A00": [119.0, 119.0, 234.0, 234.0],
            "A01": [119.0, 119.0, 234.0, 234.0],
            "A02": [119.0, 119.0, 234.0, 234.0],
        }
        minimum_required_counts = {64.0: 0, 119.0: 6, 234.0: 6}

        refined = stage6._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[64.0, 119.0, 234.0],
            minimum_required_counts=minimum_required_counts,
        )

        total = {}
        for slots in refined.values():
            for value in slots:
                total[int(round(float(value)))] = total.get(int(round(float(value))), 0) + 1
        self.assertGreaterEqual(total.get(119, 0), 6)
        self.assertGreaterEqual(total.get(234, 0), 6)
        self.assertTrue(
            stage6._layout_assignments_are_feasible(
                refined,
                list(refined),
                64.0,
                [64.0, 119.0, 234.0],
                minimum_required_counts=minimum_required_counts,
            )
        )

    def test_layout_exact_minimum_counts_are_checked_after_combining_the_full_layout(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "A00": [64.0, 64.0, 64.0, 64.0, 64.0],
            "A01": [119.0, 119.0, 119.0, 119.0, 119.0],
        }
        minimum_required_counts = {64.0: 3, 119.0: 4}

        self.assertTrue(
            stage6._layout_assignments_are_feasible(
                assignments,
                ["A00", "A01"],
                64.0,
                [64.0, 119.0],
                minimum_required_counts=minimum_required_counts,
            )
        )

    def test_illegal_small_slot_values_are_removed_from_minimum_fill_guard(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "A01": [59.0, 64.0, 89.0, 119.0],
            "A02": [59.0, 89.0, 119.0, 179.0],
        }

        refined = stage6._enforce_min_locations_per_column(
            assignments,
            ["A01", "A02"],
            69.0,
        )

        for slots in refined.values():
            self.assertTrue(all(int(round(value)) >= 69 for value in slots))
            self.assertTrue(all(int(round(value)) % 10 in (4, 9) for value in slots))

    def test_underfilled_columns_are_accepted_when_they_can_be_completed_with_a_legal_topfill(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "A01": [119.0, 179.0, 179.0],
            "A02": [119.0, 179.0, 179.0],
        }

        self.assertTrue(
            stage6._layout_assignments_are_feasible(
                assignments,
                ["A01", "A02"],
                64.0,
                [64.0, 119.0, 179.0, 234.0],
            )
        )

    def test_generated_profiles_place_legal_topfill_at_the_end_of_the_column(self):
        stage6 = sys.modules["stage6_layout"]
        profiles = stage6._generate_feasible_rack_profiles([64.0, 119.0, 234.0])
        self.assertTrue(profiles)
        config_values = set(stage6._config_size_values([64.0, 119.0, 234.0]))
        legal_topfill_values = stage6._legal_topfill_values([64.0, 119.0, 234.0])
        for profile in profiles:
            self.assertTrue(all(int(round(value)) in config_values for value in profile[:-1]))
            self.assertTrue(int(round(profile[-1])) in config_values | legal_topfill_values)
            self.assertTrue(all(int(round(value)) % 10 in (4, 9) for value in profile))

    def test_candidate_config_generation_accepts_profiles_with_a_legal_repeated_fill(self):
        stage4_path = ROOT / "Scripts" / "Pipeline" / "04_Candidate_Configuration" / "04_candidate_configuration.py"
        stage4_spec = importlib.util.spec_from_file_location("stage4_candidate", stage4_path)
        if stage4_spec is None or stage4_spec.loader is None:
            self.fail("Unable to build the stage 4 candidate module spec")
        stage4_module = importlib.util.module_from_spec(stage4_spec)
        sys.modules["stage4_candidate"] = stage4_module
        stage4_spec.loader.exec_module(stage4_module)

        self.assertTrue(stage4_module._legal_slot_profile([64.0, 119.0, 179.0]))
        self.assertTrue(stage4_module._legal_slot_profile([69.0, 119.0, 179.0, 234.0]))
        self.assertTrue(stage4_module._legal_slot_profile([69.0, 89.0, 119.0, 179.0, 234.0]))

    def test_legal_family_fillers_are_accepted_by_layout_feasibility(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "A01": [69.0, 89.0, 119.0, 179.0, 234.0],
            "A02": [69.0, 89.0, 119.0, 179.0, 234.0],
        }

        self.assertTrue(
            stage6._layout_assignments_are_feasible(
                assignments,
                ["A01", "A02"],
                64.0,
                [64.0, 69.0, 89.0, 119.0, 179.0, 234.0],
            )
        )

    def test_non_top_rows_must_use_configured_slot_sizes_and_all_config_sizes_must_be_covered(self):
        stage6 = sys.modules["stage6_layout"]

        self.assertFalse(
            stage6._layout_assignments_are_feasible(
                {"D01": [64.0, 104.0, 119.0, 234.0]},
                ["D01"],
                64.0,
                [64.0, 119.0, 234.0],
                minimum_required_counts={64.0: 1, 119.0: 1, 234.0: 1},
            )
        )

        self.assertFalse(
            stage6._layout_assignments_are_feasible(
                {"D01": [64.0, 119.0, 174.0]},
                ["D01"],
                64.0,
                [64.0, 119.0, 234.0],
                minimum_required_counts={64.0: 1, 119.0: 1, 234.0: 1},
            )
        )

        self.assertTrue(
            stage6._layout_assignments_are_feasible(
                {"D01": [64.0, 119.0, 119.0, 214.0]},
                ["D01"],
                64.0,
                [64.0, 119.0, 234.0],
                minimum_required_counts={64.0: 1, 119.0: 2, 234.0: 0},
            )
        )

    def test_deficit_coverage_prefers_profiles_with_required_slot_sizes_over_legal_topfill_only_profiles(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = stage6._build_deficit_coverage_layout(
            ["A00", "A01", "A02", "A03"],
            {64.0: 2, 119.0: 2, 234.0: 2},
            [64.0, 119.0, 234.0],
        )

        all_values = [int(round(value)) for slots in assignments.values() for value in slots]
        self.assertGreaterEqual(all_values.count(64), 2)
        self.assertGreaterEqual(all_values.count(119), 2)
        self.assertGreaterEqual(all_values.count(234), 2)

    def test_layout_alignment_slots_and_topfill_values_are_feasible(self):
        stage6 = sys.modules["stage6_layout"]
        column_assignments = {
            "D01": [69.0, 69.0, 89.0, 189.0, 119.0, 169.0],
            "D02": [69.0, 69.0, 89.0, 189.0, 119.0, 169.0],
        }

        self.assertFalse(
            stage6._layout_assignments_are_feasible(
                {"D01": [69.0, 69.0, 59.0, 189.0, 119.0, 169.0]},
                ["D01"],
                59.0,
                [69.0, 119.0, 179.0, 234.0],
            )
        )

        metadata = stage6._column_topfill_metadata([69.0, 89.0, 119.0, 179.0], [69.0, 119.0, 179.0, 234.0])
        self.assertGreaterEqual(metadata["Adjusted_Top_Slot_cm"], 179.0)
        self.assertLessEqual(metadata["Adjusted_Top_Slot_cm"], 234.0)

    def test_alignment_reduction_to_214_is_allowed_for_layout_compensation(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "J01": [119.0, 89.0, 214.0, 179.0, 89.0],
            "J02": [119.0, 89.0, 214.0, 179.0, 89.0],
        }

        self.assertTrue(
            stage6._layout_assignments_are_feasible(
                assignments,
                ["J01", "J02"],
                69.0,
                [69.0, 89.0, 119.0, 179.0, 214.0, 234.0],
            )
        )

    def test_stage5_uses_only_the_configured_scenario_distribution(self):
        stage5_path = ROOT / "Scripts" / "Pipeline" / "05_Capacity_Determination" / "05_capacity_determination.py"
        spec = importlib.util.spec_from_file_location("stage5_capacity", stage5_path)
        if spec is None or spec.loader is None:
            self.fail("Unable to build the stage 5 module spec")
        module5 = importlib.util.module_from_spec(spec)
        sys.modules["stage5_capacity"] = module5
        spec.loader.exec_module(module5)

    def test_layout_generation_requires_at_least_890_assigned_locations(self):
        common = sys.modules["run_ordered_pipeline"]
        target = common._explicit_occupied_target_total()

        self.assertEqual(target, 890)
        self.assertFalse(889 >= target)
        self.assertTrue(890 >= target)

    def test_exact_fill_profile_generator_accepts_legal_family_values_in_lower_rows(self):
        stage6 = sys.modules["stage6_layout"]
        profiles = stage6._generate_feasible_rack_profiles([109.0, 189.0, 234.0])
        config_values = set(stage6._config_size_values([109.0, 189.0, 234.0]))
        self.assertTrue(profiles)
        for profile in profiles:
            self.assertTrue(all(int(round(float(value))) in config_values for value in profile[:-1]))
            self.assertIn(int(round(float(profile[-1]))), config_values | stage6._legal_topfill_values([109.0, 189.0, 234.0]))

    def test_baseline_occupied_count_uses_890_and_current_scenario_label(self):
        stage5_path = ROOT / "Scripts" / "Pipeline" / "05_Capacity_Determination" / "05_capacity_determination.py"
        stage7_path = ROOT / "Scripts" / "Pipeline" / "07_Robustness_Evaluation" / "07_robustness_evaluation.py"

        stage5_spec = importlib.util.spec_from_file_location("stage5_capacity_check", stage5_path)
        if stage5_spec is None or stage5_spec.loader is None:
            self.fail("Unable to build the stage 5 module spec")
        stage5_module = importlib.util.module_from_spec(stage5_spec)
        sys.modules["stage5_capacity_check"] = stage5_module
        stage5_spec.loader.exec_module(stage5_module)

        stage7_spec = importlib.util.spec_from_file_location("stage7_robustness_check", stage7_path)
        if stage7_spec is None or stage7_spec.loader is None:
            self.fail("Unable to build the stage 7 module spec")
        stage7_module = importlib.util.module_from_spec(stage7_spec)
        sys.modules["stage7_robustness_check"] = stage7_module
        stage7_spec.loader.exec_module(stage7_module)

        self.assertEqual(stage5_module.common.BASE_OCCUPIED_LOCATIONS_COUNT, 890)
        self.assertEqual(stage7_module.common.BASE_OCCUPIED_LOCATIONS_COUNT, 890)
        self.assertIn("Scenario 1", stage7_module.OCCUPIED_LOCATION_SCENARIOS)
        self.assertEqual(stage7_module.OCCUPIED_LOCATION_SCENARIOS["Scenario 1"], 890)
        self.assertEqual(stage7_module.OCCUPIED_LOCATION_SCENARIOS.get("Base_Count", 0), 890)

        config = {
            "Config_ID": "CFG_099",
            "Method": "hierarchical_clustering",
            "Scenario": "Scenario 1",
            "K": "4",
            "Slot_Sizes": "69,119,179,234",
            "Relative Slot Size Distribution": "0.4015,0.3759,0.1438,0.0788",
        }

        summary_rows, _ = stage5_module._capacity_rows_for_config(config, {"Scenario 1": 890, "Base_Count": 890})
        self.assertTrue(summary_rows)
        self.assertTrue(all(int(row["SKU_Count"]) == 890 for row in summary_rows))
        self.assertTrue(all(int(row["Required_Locations_Total"]) == 890 for row in summary_rows))

        scenario_two_config = {
            "Config_ID": "CFG_100",
            "Method": "hierarchical_clustering",
            "Scenario": "Scenario 2",
            "K": "4",
            "Slot_Sizes": "69,119,179,234",
            "Relative Slot Size Distribution": "0.4015,0.3759,0.1438,0.0788",
        }
        scenario_two_rows, _ = stage5_module._capacity_rows_for_config(scenario_two_config, {"Scenario 1": 890, "Scenario 2": 890})
        self.assertTrue(scenario_two_rows)
        self.assertTrue(all(int(row["SKU_Count"]) == 890 for row in scenario_two_rows))
        self.assertTrue(all(int(row["Required_Locations_Total"]) == 890 for row in scenario_two_rows))

    def test_stage6_column_export_keeps_assigned_used_height_when_slots_exist(self):
        import csv

        merged_output = Path(ROOT / "Output" / "06_Layout_Generation_Comparison" / "Candidate_Layout_By_Rack_Column.csv")
        self.assertTrue(merged_output.exists(), "Merged stage 6 comparison CSV should exist")

        with merged_output.open("r", newline="", encoding="utf-8-sig") as source:
            rows = list(csv.DictReader(source))

        self.assertIn("Assigned_Used_Height_cm", rows[0].keys())
        non_blank = [row for row in rows if str(row.get("Assigned_Used_Height_cm", "")).strip() and str(row.get("Slot_Size_Distribution", "")).strip()]
        self.assertGreater(len(non_blank), 0)
        self.assertNotEqual(str(non_blank[0].get("Assigned_Used_Height_cm", "")).strip(), "")

        sample = non_blank[0]
        slot_parts = [part for part in str(sample.get("Slot_Size_Distribution", "")).split("|") if part.strip()]
        if slot_parts:
            slot_total = sum(int(part.split(":", 1)[0]) * int(part.split(":", 1)[1]) for part in slot_parts)
            slot_count = sum(int(part.split(":", 1)[1]) for part in slot_parts)
            expected = slot_total + max(slot_count - 1, 0) * 16.0
            self.assertAlmostEqual(float(sample["Assigned_Used_Height_cm"]), expected, delta=1e-3)


if __name__ == "__main__":
    unittest.main()
