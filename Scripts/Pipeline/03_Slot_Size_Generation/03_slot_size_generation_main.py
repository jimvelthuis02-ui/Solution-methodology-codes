import runpy
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
METHOD_SCRIPTS = [
    "03a_quantile_model.py",
    "03b_hierarchical_model.py",
    "03c_kmeans_model.py",
]


def run_slot_size_generation() -> None:
    """Run each Stage 3 slot-size method script in sequence."""
    for script in METHOD_SCRIPTS:
        script_path = SCRIPT_DIR / script
        print(f"Running slot-size method model: {script}")
        runpy.run_path(str(script_path), run_name="__main__")


if __name__ == "__main__":
    # Stage 3 entrypoint: execute all method variants (quantile, hierarchical, k-means).
    run_slot_size_generation()
    print("Slot-size method generation complete.")
