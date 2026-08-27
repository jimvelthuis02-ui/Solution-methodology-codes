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

    def test_layout_generation_defaults_to_no_layout_when_env_is_unset(self):
        stage6 = sys.modules["stage6_layout"]
        original = os.environ.get("PIPELINE_IGNORE_LAYOUT")
        try:
            os.environ.pop("PIPELINE_IGNORE_LAYOUT", None)
            self.assertTrue(stage6.common._should_ignore_layout_for_layout_generation())
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
