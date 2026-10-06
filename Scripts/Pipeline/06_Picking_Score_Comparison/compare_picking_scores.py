"""Score baseline, SU, PE and BP Stage 6 layouts with one shared picking metric."""

import csv
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common

PICKING_SCRIPT = PIPELINE_ROOT / "06_Layout_Generation_Picking_Efficiency" / "06_layout_generation_picking_efficiency.py"
OUTPUT_FILE = common.OUTPUT_ROOT / "06_Picking_Score_Comparison" / "Picking_Score_Comparison.csv"
METHODS = {
    "Baseline": (common.OUTPUT_ROOT / "06_Layout_Generation", ""),
    "SU": (common.OUTPUT_ROOT / "06_Layout_Generation_Space_Utilization", "_SU"),
    "PE": (common.OUTPUT_ROOT / "06_Layout_Generation_Picking_Efficiency", "_PE"),
    "BP": (common.OUTPUT_ROOT / "06_Layout_Generation_Beam_Preservation", "_BP"),
}


def _load_picking_module():
    spec = importlib.util.spec_from_file_location("picking_variant", PICKING_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_id(config_id: str, suffix: str) -> str:
    return config_id[: -len(suffix)] if suffix and config_id.endswith(suffix) else config_id


def _family(size: float, config_sizes: list[float]) -> float | None:
    fitting = [value for value in config_sizes if value <= size + 1e-9]
    return max(fitting) if fitting else None


def _score_method(name: str, picking) -> dict[str, dict[str, float]]:
    folder, suffix = METHODS[name]
    summary = common._read_csv(folder / f"Candidate_Layout_Summary{suffix}.csv")
    locations = common._read_csv(folder / f"Candidate_Layout_By_Location{suffix}.csv")

    config_sizes: dict[str, list[float]] = {}
    for row in summary:
        if str(row.get("Layout_Feasible", "")).strip().upper() != "YES":
            continue
        sizes = common._decode_excel_text(row.get("Source_Slot_Sizes", ""))
        config_sizes[_base_id(row["Config_ID"], suffix)] = [float(v) for v in sizes.split(",") if v.strip()]

    columns: dict[str, dict[str, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
    for row in locations:
        if str(row.get("Usable_Location", "YES")).strip().upper() == "NO":
            continue
        config_id = _base_id(row["Config_ID"], suffix)
        if config_id in config_sizes:
            columns[config_id][f"{row['Rack']}{row['Column']}"].append(
                (int(row["Row"]), float(row["Assigned_Slot_Size_cm"]))
            )

    results: dict[str, dict[str, float]] = {}
    for config_id, sizes in config_sizes.items():
        demand = picking._picking_demand(sizes)[0]
        score = 0.0
        location_count = 0
        for slots in columns[config_id].values():
            ordered = [size for _row, size in sorted(slots)]
            location_count += len(ordered)
            for index, size in enumerate(ordered):
                family = _family(size, sizes)
                if family is not None:
                    score += demand.get(int(family), 0) * (len(ordered) - index) / len(ordered)
        results[config_id] = {"score": score, "locations": location_count}
    return results


def main() -> None:
    picking = _load_picking_module()
    scored = {name: _score_method(name, picking) for name in METHODS}
    common_ids = sorted(set.intersection(*(set(values) for values in scored.values())))

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fields = ["Config_ID"]
    for name in METHODS:
        fields += [f"{name}_Picking_Score", f"{name}_Locations", f"{name}_Score_Per_Location"]
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for config_id in common_ids:
            row = {"Config_ID": config_id}
            for name in METHODS:
                entry = scored[name][config_id]
                row[f"{name}_Picking_Score"] = f"{entry['score']:.3f}"
                row[f"{name}_Locations"] = str(int(entry["locations"]))
                row[f"{name}_Score_Per_Location"] = f"{entry['score'] / entry['locations']:.3f}"
            writer.writerow(row)
    print(f"Wrote {len(common_ids)} configurations to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
