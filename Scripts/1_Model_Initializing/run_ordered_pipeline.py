import runpy
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ORDERED_SCRIPTS = [
    "01_Data_Preparation/01_data_preparation.py",
    "02_Scenario_Generation/02_scenario_generation_weighted_delta.py",
    "03_Slot_Size_Generation/03_slot_size_generation_common.py",
    "04_Slot_Size_Configuration_Model/04_slot_size_configuration_model.py",
]


def run_pipeline() -> None:
    for script_name in ORDERED_SCRIPTS:
        script_path = SCRIPT_DIR / script_name
        print(f"Running: {script_name}")
        runpy.run_path(str(script_path), run_name="__main__")


if __name__ == "__main__":
    run_pipeline()
    print("Ordered pipeline complete.")
