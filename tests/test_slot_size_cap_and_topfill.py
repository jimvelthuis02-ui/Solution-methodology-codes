import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE6_PATH = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"
COMMON_PATH = ROOT / "Scripts" / "Pipeline" / "run_ordered_pipeline.py"

for module_path, module_name in [(COMMON_PATH, "run_ordered_pipeline"), (STAGE6_PATH, "stage6_layout")]:
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)


class SlotSizeCapTest(unittest.TestCase):
    def test_topfill_caps_below_234(self):
        common = sys.modules["run_ordered_pipeline"]
        stage6 = sys.modules["stage6_layout"]

        topfill = stage6._build_top_filled_layout_set(
            summary_rows=[{"Layout_ID": "L1", "Assigned_Used_Height_Total": "400.0", "Total_Allowed_Height": "800.0"}],
            column_rows=[{"Layout_ID": "L1", "Rack_Column": "D01", "Remaining_Height_cm": "240.0", "Assigned_Used_Height_cm": "100.0", "Allowed_Used_Height_cm": "700.0"}],
            location_rows=[
                {"Layout_ID": "L1", "Rack": "D", "Column": "01", "Row": "01", "Assigned_Slot_Size_cm": "44"},
                {"Layout_ID": "L1", "Rack": "D", "Column": "01", "Row": "02", "Assigned_Slot_Size_cm": "44"},
            ],
        )

        all_slot_values = [float(row["Assigned_Slot_Size_cm"]) for row in topfill[2]]
        self.assertLessEqual(max(all_slot_values), 234.0)
        self.assertEqual(max(all_slot_values), 234.0)
        self.assertLessEqual(common._cap_slot_size(300.0), 234.0)

    def test_columns_fill_exactly_to_physical_limit_without_exceeding_234(self):
        stage6 = sys.modules["stage6_layout"]
        column_assignments = {
            "D01": [69.0, 104.0, 104.0, 69.0, 44.0],
            "D02": [69.0, 104.0, 104.0, 69.0],
            "D03": [69.0, 104.0, 104.0, 69.0, 44.0, 90.0],
        }

        refined = stage6._enforce_rack_row_count_consistency(
            column_assignments,
            available_slot_sizes=[44.0, 69.0, 104.0, 234.0],
            fixed_prefix_by_column={},
        )

        for column_key, slots in refined.items():
            self.assertLessEqual(max(slots), 234.0)
            target_height = 754.0
            physical_total = sum(slots) + (len(slots) - 1) * 16.0
            self.assertAlmostEqual(physical_total, target_height, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
