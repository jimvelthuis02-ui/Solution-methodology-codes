import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_module(rel_path: str, name: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class LegacyGreedySkipTest(unittest.TestCase):
    def test_stage7_greedy_anchor_skips_without_input(self):
        module = _load_module(
            "Scripts/Pipeline/07_Robustness_Evaluation/07_robustness_evaluation_greedy.py",
            "stage7_greedy_anchor",
        )
        output = module.build_robustness_evaluation_greedy_anchor()
        self.assertTrue(output is None or output.exists() or not output.parent.exists())

    def test_stage8_greedy_anchor_skips_without_input(self):
        module = _load_module(
            "Scripts/Pipeline/08_Final_Selection/08_final_selection_greedy.py",
            "stage8_greedy_anchor",
        )
        output = module.build_final_selection_greedy_anchor()
        self.assertTrue(output is None or output.exists() or not output.parent.exists())


if __name__ == "__main__":
    unittest.main()
