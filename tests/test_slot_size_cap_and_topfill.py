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

    def test_all_active_heuristic_variants_are_exported(self):
        spec = importlib.util.spec_from_file_location("heuristic_variants", HEURISTIC_VARIANTS_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        labels = {variant["label"] for variant in module.VARIANTS}
        self.assertEqual(
            labels,
            {
                "ConstructiveBeam_NoLocalSearch",
                "ConstructiveBeam_LocalSearch",
                "Greedy_NoLocalSearch",
                "Greedy_LocalSearch",
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


if __name__ == "__main__":
    unittest.main()
