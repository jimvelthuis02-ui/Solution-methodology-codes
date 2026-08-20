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
    def test_rack_row_count_consistency_pads_underfilled_columns(self):
        assignments = {
            "R01C01": [10.0, 10.0],
            "R01C02": [10.0],
            "R01C03": [10.0, 10.0, 10.0],
            "R02C01": [8.0],
            "R02C02": [8.0, 8.0],
        }

        adjusted = module._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[10.0, 8.0],
        )

        r01_lengths = [len(adjusted[f"R01C{i:02d}"]) for i in range(1, 4)]
        r02_lengths = [len(adjusted[f"R02C{i:02d}"]) for i in range(1, 3)]

        self.assertTrue(len(set(r01_lengths)) == 1)
        self.assertTrue(len(set(r02_lengths)) == 1)
        self.assertTrue(all(value <= 234.0 for values in adjusted.values() for value in values))
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


if __name__ == "__main__":
    unittest.main()
