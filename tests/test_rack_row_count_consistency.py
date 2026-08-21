import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE6_PATH = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"

spec = importlib.util.spec_from_file_location("stage6_layout", STAGE6_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules["stage6_layout"] = module
spec.loader.exec_module(module)


class RackRowCountConsistencyTest(unittest.TestCase):
    def test_no_minimum_beam_floor_is_applied(self):
        self.assertEqual(module.common.MIN_BEAMS_PER_COLUMN, 0)
        self.assertEqual(max(4 - 1, module.common.MIN_BEAMS_PER_COLUMN), 3)
        self.assertEqual(max(2 - 1, module.common.MIN_BEAMS_PER_COLUMN), 1)

    def test_rack_row_count_consistency_pads_underfilled_columns(self):
        assignments = {
            "R01C01": [69.0, 89.0, 119.0],
            "R01C02": [69.0, 89.0],
            "R01C03": [69.0, 89.0, 119.0],
            "R02C01": [69.0],
            "R02C02": [69.0, 89.0],
        }

        adjusted = module._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[69.0, 89.0, 119.0, 179.0, 234.0],
        )

        r01_lengths = [len(adjusted[f"R01C{i:02d}"]) for i in range(1, 4)]
        r02_lengths = [len(adjusted[f"R02C{i:02d}"]) for i in range(1, 3)]

        self.assertTrue(len(set(r01_lengths)) == 1)
        self.assertTrue(len(set(r02_lengths)) == 1)
        self.assertTrue(all(value <= 234.0 for values in adjusted.values() for value in values))
        self.assertTrue(not any(abs(value - 1.0) < 1e-9 for values in adjusted.values() for value in values))
        self.assertTrue(
            all(
                sum(adjusted[f"R01C{i:02d}"]) + (len(adjusted[f"R01C{i:02d}"]) - 1) * 16.0 == 754.0
                for i in range(1, 4)
            )
        )
        self.assertTrue(
            all(
                sum(adjusted[f"R02C{i:02d}"]) + (len(adjusted[f"R02C{i:02d}"]) - 1) * 16.0 == 754.0
                for i in range(1, 3)
            )
        )

    def test_rejects_inconsistent_rack_row_levels_even_with_fixed_prefix_columns(self):
        assignments = {
            "A00": [64.0, 64.0, 119.0, 234.0, 234.0],
            "A01": [64.0, 64.0, 119.0, 234.0, 234.0],
            "A02": [64.0, 64.0, 119.0, 234.0, 234.0],
            "A03": [64.0, 64.0, 119.0, 234.0, 234.0],
            "A04": [64.0, 64.0, 119.0, 234.0],
            "A05": [64.0, 64.0, 119.0, 234.0],
            "A06": [64.0, 64.0, 119.0, 234.0],
        }

        self.assertFalse(
            module._layout_assignments_are_feasible(
                assignments,
                [f"A{i:02d}" for i in range(7)],
                64.0,
                [64.0, 119.0, 234.0],
            )
        )

    def test_accepts_legal_underfilled_columns_within_physical_limit(self):
        valid = {
            "R01C01": [64.0, 64.0, 234.0, 234.0],
            "R01C02": [64.0, 64.0, 234.0, 234.0],
            "R01C03": [64.0, 64.0, 234.0, 234.0],
        }

        self.assertTrue(
            module._layout_assignments_are_feasible(
                valid,
                ["R01C01", "R01C02", "R01C03"],
                64.0,
                [64.0, 119.0, 234.0],
            )
        )

    def test_rejects_layouts_that_cannot_become_feasible(self):
        impossible = {
            "R01C01": [59.0, 69.0],
            "R01C02": [69.0],
            "R01C03": [69.0, 89.0],
        }

        self.assertFalse(
            module._layout_assignments_are_feasible(
                impossible,
                ["R01C01", "R01C02", "R01C03"],
                69.0,
                [69.0, 89.0, 119.0, 179.0, 234.0],
            )
        )


if __name__ == "__main__":
    unittest.main()
