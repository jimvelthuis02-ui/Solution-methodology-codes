import runpy
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
METHOD_SCRIPTS = [
    "03a_quantile_model.py",
    "03b_hierarchical_model.py",
    "03c_kmeans_model.py",
]


def run_slot_size_generation() -> None:
    for script in METHOD_SCRIPTS:
        script_path = SCRIPT_DIR / script
        print(f"Running slot-size method model: {script}")
        runpy.run_path(str(script_path), run_name="__main__")


if __name__ == "__main__":
    run_slot_size_generation()
    print("Slot-size method generation complete.")
