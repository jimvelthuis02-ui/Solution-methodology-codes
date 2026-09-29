import csv
import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAGE6_PATH = ROOT / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"
CONFIG_PATH = ROOT / "Output" / "04_Candidate_Configuration" / "Candidate_Configurations.csv"
CAPACITY_PATH = ROOT / "Output" / "05_Capacity_Determination" / "Constraint_Location_Counts_By_Slot_Size.csv"
TARGET_CFGS = ["CFG_077", "CFG_067", "CFG_115", "CFG_094", "CFG_104"]


def read_csv(path: Path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def parse_slot_sizes(raw_value: str) -> list[float]:
    value = str(raw_value or "").strip()
    if value.startswith('="') and value.endswith('"'):
        value = value[2:-1]
    if not value:
        return []
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def _derive_required_counts(rows: list[dict[str, str]]) -> dict[float, int]:
    """Mirror the Stage 6 logic: convert cumulative requirements into exact slot-size quotas."""
    cumulative_by_size: dict[float, int] = {}
    for row in rows:
        slot_size = common._to_float(row.get("Representative_Slot_Size"))
        required_value = common._to_float(
            row.get("Min_Required_Locations_At_Or_Above_Size")
            or row.get("Cumulative_Assigned_SKUs_At_Or_Above_Size")
        )
        if slot_size is None or required_value is None:
            continue
        cumulative_by_size[slot_size] = max(cumulative_by_size.get(slot_size, 0), int(round(required_value)))

    ordered_sizes = sorted(cumulative_by_size)
    exact_counts: dict[float, int] = {}
    for index, slot_size in enumerate(ordered_sizes):
        next_size = ordered_sizes[index + 1] if index + 1 < len(ordered_sizes) else None
        next_required = cumulative_by_size.get(next_size, 0) if next_size is not None else 0
        exact_counts[slot_size] = max(cumulative_by_size[slot_size] - next_required, 0)

    return exact_counts


def load_stage6_module():
    spec = importlib.util.spec_from_file_location("stage6_probe", STAGE6_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not create module spec for {STAGE6_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["stage6_probe"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    print("=== Stage 6 hard-config probe ===", flush=True)
    print(f"Stage 6 file: {STAGE6_PATH}", flush=True)
    print(f"Output config file: {CONFIG_PATH}", flush=True)
    print(f"Capacity file: {CAPACITY_PATH}", flush=True)

    try:
        stage6 = load_stage6_module()
        print("Stage 6 module loaded successfully.", flush=True)
    except Exception:
        print("Failed to import Stage 6 module:", flush=True)
        traceback.print_exc()
        return

    try:
        config_rows = read_csv(CONFIG_PATH)
        capacity_rows = read_csv(CAPACITY_PATH)
        print(f"Loaded {len(config_rows)} config rows and {len(capacity_rows)} capacity rows.", flush=True)
    except Exception:
        print("Failed to read CSV inputs:", flush=True)
        traceback.print_exc()
        return

    config_slot_map: dict[str, list[float]] = {}
    for row in config_rows:
        cfg = str(row.get("Config_ID", "")).strip()
        if not cfg:
            continue
        config_slot_map[cfg] = parse_slot_sizes(str(row.get("Slot_Sizes", "")))

    capacity_by_cfg: dict[str, list[dict[str, str]]] = {}
    for row in capacity_rows:
        cfg = str(row.get("Config_ID", "")).strip()
        if not cfg:
            continue
        capacity_by_cfg.setdefault(cfg, []).append(row)

    global common
    common = stage6.common

    for cfg in TARGET_CFGS:
        print(f"\n===== {cfg} =====", flush=True)
        sizes = config_slot_map.get(cfg, [])
        rows = capacity_by_cfg.get(cfg, [])
        if not sizes:
            print("No slot sizes found in Candidate_Configurations.csv", flush=True)
            continue
        if not rows:
            print("No capacity rows found for this config", flush=True)
            continue

        required_counts = _derive_required_counts(rows)
        print(f"slot sizes = {sizes}", flush=True)
        print(f"derived required counts = { {round(float(k), 2): v for k, v in required_counts.items()} }", flush=True)

        if not required_counts:
            print("No required counts could be derived from the cumulative capacity rows.", flush=True)
            continue

        try:
            profiles = stage6._generate_feasible_rack_profiles(sizes, timeout_seconds=30.0, required_counts=required_counts)
            print(f"raw profile count = {len(profiles)}", flush=True)
        except Exception:
            print("Profile generation failed with traceback:", flush=True)
            traceback.print_exc()
            continue

        try:
            shortlist = stage6._choose_profile_shortlist(profiles, required_counts, limit=12)
            print(f"shortlist count = {len(shortlist)}", flush=True)
        except Exception:
            print("Shortlist generation failed with traceback:", flush=True)
            traceback.print_exc()
            continue

        shortlist_values = sorted({int(round(float(value))) for profile in shortlist for value in profile})
        required_values = sorted({int(round(float(size))) for size in required_counts.keys()})
        missing = [value for value in required_values if value not in shortlist_values]

        print(f"required values = {required_values}", flush=True)
        print(f"shortlist values = {shortlist_values}", flush=True)
        print(f"missing from shortlist = {missing}", flush=True)
        print("top raw profiles =")
        for index, profile in enumerate(profiles[:5], start=1):
            print(f"  {index}: {profile}", flush=True)
        print("top shortlist profiles =")
        for index, profile in enumerate(shortlist[:5], start=1):
            print(f"  {index}: {profile}", flush=True)


if __name__ == "__main__":
    main()
