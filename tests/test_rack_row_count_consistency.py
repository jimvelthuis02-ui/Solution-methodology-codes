import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE4_PATH = ROOT / "Scripts" / "Pipeline" / "04_Candidate_Configuration" / "04_candidate_configuration.py"
STAGE6_PATH = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"

spec4 = importlib.util.spec_from_file_location("stage4_candidate_configuration", STAGE4_PATH)
if spec4 is None or spec4.loader is None:
    raise ImportError(f"Unable to load module spec for {STAGE4_PATH}")
module4 = importlib.util.module_from_spec(spec4)
sys.modules["stage4_candidate_configuration"] = module4
spec4.loader.exec_module(module4)

spec = importlib.util.spec_from_file_location("stage6_layout", STAGE6_PATH)
if spec is None or spec.loader is None:
    raise ImportError(f"Unable to load module spec for {STAGE6_PATH}")
module = importlib.util.module_from_spec(spec)
sys.modules["stage6_layout"] = module
spec.loader.exec_module(module)


class RackRowCountConsistencyTest(unittest.TestCase):
    def test_stage4_accepts_slot_families_that_can_topfill_to_the_physical_limit(self):
        self.assertTrue(module4._legal_slot_profile([69.0, 119.0, 234.0]))
        self.assertTrue(module4._legal_slot_profile([44.0]))
        self.assertTrue(module4._legal_slot_profile([109.0, 189.0, 234.0]))

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

    def test_fixed_doorway_prefix_is_preserved_and_columns_reach_754(self):
        assignments = {
            "R01C19": [39.0, 69.0, 89.0],
            "R01C20": [39.0, 69.0, 89.0],
            "R01C21": [39.0, 69.0, 89.0],
        }
        fixed_prefix = {
            "R01C19": [224.0],
            "R01C20": [224.0],
            "R01C21": [224.0],
        }

        adjusted = module._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[69.0, 89.0, 119.0, 234.0],
            fixed_prefix_by_column=fixed_prefix,
        )

        for key, values in adjusted.items():
            self.assertEqual(values[0], 224.0)
            self.assertAlmostEqual(sum(values) + (len(values) - 1) * 16.0, 754.0, delta=1e-6)
            self.assertLessEqual(max(values), 234.0)
            self.assertTrue(all(int(round(value)) % 10 in (4, 9) for value in values))

    def test_keeps_the_highest_feasible_row_count_when_equalizing_rack(self):
        assignments = {
            "K00": [69.0, 69.0, 69.0, 69.0, 69.0],
            "K01": [69.0, 69.0, 69.0, 69.0, 69.0],
            "K04": [69.0, 69.0, 69.0, 234.0],
            "K05": [69.0, 69.0, 69.0, 234.0],
        }

        adjusted = module._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[69.0, 119.0, 234.0],
        )

        lengths = {key: len(value) for key, value in adjusted.items()}
        self.assertEqual(len(set(lengths.values())), 1)
        self.assertEqual(max(lengths.values()), 4)
        self.assertTrue(all(length == 4 for length in lengths.values()))

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

    def test_ignore_doorgang_relaxes_fixed_prefixes(self):
        original = os.environ.get("PIPELINE_IGNORE_DOORGANG")
        try:
            os.environ["PIPELINE_IGNORE_DOORGANG"] = "1"
            self.assertTrue(module.common._ignore_doorgang_constraints())
            self.assertEqual(module.common._fixed_doorgang_slot_by_column([]), {})
            self.assertEqual(module.common._doorgang_thresholds_by_rack([]), {})
            self.assertEqual(
                module.common._exclude_fixed_doorgang_slot_counts({69: 100, 224: 4, 229: 6, 234: 1}),
                {69: 100},
            )
        finally:
            if original is None:
                os.environ.pop("PIPELINE_IGNORE_DOORGANG", None)
            else:
                os.environ["PIPELINE_IGNORE_DOORGANG"] = original

    def test_allows_same_row_count_with_different_slot_orders_in_same_rack(self):
        assignments = {
            "K00": [234.0, 234.0, 149.0, 89.0],
            "K01": [234.0, 234.0, 119.0, 119.0],
            "K02": [234.0, 234.0, 149.0, 89.0],
        }

        adjusted = module._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[89.0, 119.0, 149.0, 234.0],
        )

        lengths = {key: len(value) for key, value in adjusted.items()}
        self.assertEqual(len(set(lengths.values())), 1)
        self.assertEqual(max(lengths.values()), 4)
        self.assertTrue(all(sum(values) + (len(values) - 1) * 16.0 <= 754.0 for values in adjusted.values()))
        self.assertTrue(adjusted["K00"] != adjusted["K01"])

    def test_accepts_legal_underfilled_columns_within_physical_limit(self):
        valid = {
            "R01C01": [119.0, 179.0, 179.0],
            "R01C02": [119.0, 179.0, 179.0],
            "R01C03": [119.0, 179.0, 179.0],
        }

        self.assertTrue(
            module._layout_assignments_are_feasible(
                valid,
                ["R01C01", "R01C02", "R01C03"],
                64.0,
                [64.0, 119.0, 179.0, 234.0],
            )
        )

    def test_rebuilds_columns_with_larger_slots_lower_and_smaller_slots_above(self):
        assignments = {
            "K00": [89.0, 119.0, 234.0, 234.0],
            "K01": [119.0, 119.0, 234.0, 234.0],
            "K02": [89.0, 119.0, 234.0, 234.0],
        }

        adjusted = module._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[89.0, 119.0, 149.0, 234.0],
        )

        for values in adjusted.values():
            self.assertEqual(values, sorted(values, reverse=True))
            self.assertLessEqual(sum(values) + (len(values) - 1) * 16.0, 754.0)

    def test_rejects_layouts_that_cannot_become_feasible(self):
        impossible = {
            "R01C01": [2.0, 2.0],
            "R01C02": [2.0],
            "R01C03": [2.0, 2.0],
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
