from functools import lru_cache
from importlib import util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent
LEGACY_STAGE4_PATH = ROOT / "Scripts" / "1_Model_Initializing" / "04_Slot_Size_Configuration_Model" / "04_slot_size_configuration_model.py"
STAGE3_ROOT = ROOT / "Output" / "03_Slot_Size_Generation"
STAGE4_OUTPUT_ROOT = ROOT / "Output" / "04_Slot_Size_Configuration_Model"


@lru_cache(maxsize=1)
def load_legacy_pipeline() -> ModuleType:
    spec = util.spec_from_file_location("slot_size_legacy_pipeline", LEGACY_STAGE4_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load legacy pipeline module from {LEGACY_STAGE4_PATH}")

    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage3_summary_paths() -> list[Path]:
    legacy = load_legacy_pipeline()
    return [legacy.SLOT_SIZE_ROOT / method / "Slot_Size_Configuration_Summary.csv" for method in legacy.METHODS]
