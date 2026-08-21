import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON_PATH = ROOT / "Scripts" / "Pipeline" / "run_ordered_pipeline.py"

spec = importlib.util.spec_from_file_location("run_ordered_pipeline", COMMON_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules["run_ordered_pipeline"] = module
spec.loader.exec_module(module)


class SafeRmtreeTest(unittest.TestCase):
    def test_safe_rmtree_retries_permission_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "variant_root"
            nested = target / "Baseline_LocalSearch" / "06_Layout_Generation"
            nested.mkdir(parents=True)
            file_path = nested / "dummy.csv"
            file_path.write_text("a,b\n1,2\n", encoding="utf-8")
            file_path.chmod(0o444)

            original_walk = module.os.walk
            original_rmtree = shutil.rmtree
            calls = {"count": 0}

            def flaky_walk(path, *args, **kwargs):
                calls["count"] += 1
                if calls["count"] < 3:
                    raise PermissionError("Access is denied")
                return original_walk(path, *args, **kwargs)

            try:
                module.os.walk = flaky_walk
                module.shutil.rmtree = original_rmtree
                module._safe_rmtree(target, retries=5, delay_seconds=0.0)
            finally:
                module.os.walk = original_walk

            self.assertFalse(target.exists())
            self.assertGreaterEqual(calls["count"], 3)


if __name__ == "__main__":
    unittest.main()
