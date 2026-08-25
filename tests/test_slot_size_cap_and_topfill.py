import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE6_PATH = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"
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
            fixed_prefix_by_column={},
        )

        for column_key, slots in refined.items():
            self.assertLessEqual(max(slots), 234.0)
            self.assertTrue(all(int(round(value)) >= 69 for value in slots))
            self.assertTrue(all(int(round(value)) % 10 in (4, 9) for value in slots))
            target_height = 754.0
            physical_total = sum(slots) + (len(slots) - 1) * 16.0
            self.assertAlmostEqual(physical_total, target_height, delta=1e-6)

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
            fixed_prefix_by_column={},
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
            fixed_prefix_by_column={},
        )

        for slots in refined.values():
            self.assertAlmostEqual(sum(slots) + (len(slots) - 1) * 16.0, 754.0, delta=1e-6)
            self.assertLessEqual(max(slots), 234.0)

    def test_doorway_prefix_prefers_exact_reference_profile_then_legal_fallback(self):
        stage6 = sys.modules["stage6_layout"]
        reference = {
            "R00": [64.0, 64.0, 64.0],
            "R01": [64.0, 64.0, 64.0],
            "R20": [119.0, 234.0],
            "R21": [119.0, 234.0],
        }

        exact_prefix = stage6._build_doorway_prefix_for_segment(
            reference,
            rack="R",
            segment=("R", 0, 21),
            doorgang_column=21,
            doorgang_height=224.0,
            available_sizes=[64.0, 119.0, 234.0],
        )
        self.assertIsNotNone(exact_prefix)
        self.assertEqual(exact_prefix, [64.0, 64.0, 64.0])

        fallback = stage6._build_doorway_prefix_for_segment(
            {"R00": [119.0, 234.0], "R01": [119.0, 234.0], "R20": [119.0, 234.0], "R21": [119.0, 234.0]},
            rack="R",
            segment=("R", 0, 21),
            doorgang_column=21,
            doorgang_height=224.0,
            available_sizes=[64.0, 119.0, 234.0],
        )
        self.assertIsNotNone(fallback)
        self.assertAlmostEqual(sum(fallback) + (len(fallback) - 1) * 16.0, 224.0, delta=1e-9)

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

    def test_legal_family_fillers_are_accepted_by_layout_feasibility(self):
        stage6 = sys.modules["stage6_layout"]
        assignments = {
            "A01": [64.0, 119.0, 224.0, 234.0],
            "A02": [64.0, 119.0, 224.0, 234.0],
        }

        self.assertTrue(
            stage6._layout_assignments_are_feasible(
                assignments,
                ["A01", "A02"],
                64.0,
                [64.0, 119.0, 234.0],
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

        config = {
            "Config_ID": "CFG_099",
            "Method": "hierarchical_clustering",
            "Scenario": "Scenario 2",
            "K": "4",
            "Slot_Sizes": "69,119,179,234",
            "Relative Slot Size Distribution": "0.4015,0.3759,0.1438,0.0788",
        }

        summary_rows, _ = module5._capacity_rows_for_config(config, {"Base_Count": 100})
        scenario_labels = {row["SKU_Scenario"] for row in summary_rows}
        self.assertEqual(scenario_labels, {"Scenario 2"})

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
