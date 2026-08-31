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

    def test_rack_row_count_consistency_allows_different_slot_orders_within_a_rack(self):
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

        self.assertNotEqual(adjusted["R01C01"], adjusted["R01C02"])
        self.assertTrue(all(sum(values) + (len(values) - 1) * 16.0 <= 754.0 for values in adjusted.values()))

    def test_columns_in_a_rack_can_keep_different_legal_orders(self):
        assignments = {
            "R01C19": [39.0, 69.0, 89.0],
            "R01C20": [39.0, 69.0, 89.0],
            "R01C21": [39.0, 69.0, 89.0],
        }

        adjusted = module._enforce_rack_row_count_consistency(
            assignments,
            available_slot_sizes=[69.0, 89.0, 119.0, 234.0],
        )

        self.assertEqual(len(set(tuple(adjusted[key]) for key in sorted(adjusted))), 1)
        self.assertTrue(all(sum(values) + (len(values) - 1) * 16.0 <= 754.0 for values in adjusted.values()))

    def test_rack_columns_need_not_share_one_exact_profile(self):
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

        k_values = [tuple(adjusted[key]) for key in sorted(adjusted) if key.startswith("K")]
        self.assertGreater(len(set(k_values)), 1)
        self.assertTrue(all(sum(profile) + (len(profile) - 1) * 16.0 <= 754.0 for profile in k_values))

    def test_rejects_inconsistent_rack_row_levels_even_without_prefix_memory(self):
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

    def test_ignore_layout_keeps_non_prefix_layout_rules(self):
        original = os.environ.get("PIPELINE_IGNORE_LAYOUT")
        try:
            os.environ["PIPELINE_IGNORE_LAYOUT"] = "1"
            self.assertTrue(module.common._ignore_layout_constraints())
            self.assertEqual(module.common._fixed_layout_slot_by_column([]), {})
            self.assertEqual(module.common._layout_thresholds_by_rack([]), {})
            self.assertEqual(
                module.common._exclude_fixed_layout_slot_counts({69: 100, 224: 4, 229: 6, 234: 1}),
                {69: 100, 224: 4, 229: 6, 234: 1},
            )
        finally:
            if original is None:
                os.environ.pop("PIPELINE_IGNORE_LAYOUT", None)
            else:
                os.environ["PIPELINE_IGNORE_LAYOUT"] = original

    def test_rack_columns_can_use_different_legal_profiles(self):
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
        self.assertNotEqual(adjusted["K00"], adjusted["K01"])

    def test_deficit_coverage_baseline_assigns_uniform_rack_profiles_without_repair(self):
        feasible_profiles = module._generate_feasible_rack_profiles([69.0, 119.0, 179.0, 234.0])
        self.assertTrue(feasible_profiles)
        self.assertTrue(any(profile for profile in feasible_profiles if sum(profile) + (len(profile) - 1) * 16.0 == 754.0))

        column_assignments = module._build_deficit_coverage_layout(
            rack_columns=["R01C01", "R01C02", "R01C03", "R02C01", "R02C02", "R02C03"],
            required_counts={69.0: 4, 119.0: 2, 179.0: 2, 234.0: 1},
            config_slot_sizes=[69.0, 119.0, 179.0, 234.0],
        )

        self.assertEqual(set(column_assignments["R01C01"]), set(column_assignments["R01C02"]))
        self.assertEqual(column_assignments["R01C01"], column_assignments["R01C02"])
        self.assertEqual(column_assignments["R01C02"], column_assignments["R01C03"])
        self.assertTrue(all(sum(values) + (len(values) - 1) * 16.0 == 754.0 for values in column_assignments.values()))

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
            self.assertEqual(values, sorted(values))
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

    def test_rejects_physical_layouts_missing_required_minimum_slot_counts(self):
        layout = {
            "A01": [64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 114.0],
            "A02": [64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 64.0, 114.0],
        }

        self.assertFalse(
            module._layout_assignments_are_feasible(
                layout,
                ["A01", "A02"],
                64.0,
                [64.0, 119.0, 234.0],
                minimum_required_counts={64.0: 16, 119.0: 2},
            )
        )


if __name__ == "__main__":
    unittest.main()
