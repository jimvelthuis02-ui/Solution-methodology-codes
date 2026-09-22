import importlib.util
import sys
import time
from pathlib import Path

print("starting import ...", flush=True)
p = Path("Scripts/Pipeline/06_Layout_Generation/06_layout_generation.py")
spec = importlib.util.spec_from_file_location("stage6", p)
mod = importlib.util.module_from_spec(spec)
sys.modules["stage6"] = mod
spec.loader.exec_module(mod)
print("module loaded", flush=True)

rows = [row for row in mod._read_csv(mod.INPUT_CAPACITY_FILE) if row.get("Config_ID","").strip() == "CFG_046"]
required = {float(size): int(count) for size, count in mod._base_exact_counts(rows).items()}
slot_sizes = mod._slot_sizes_from_capacity(rows)

print("required=", required, flush=True)
print("slot_sizes=", slot_sizes, flush=True)

t0 = time.perf_counter()
print("calling _generate_feasible_rack_profiles ...", flush=True)
profiles = mod._generate_feasible_rack_profiles(slot_sizes, required_counts=required)
t1 = time.perf_counter()
print("profiles ready:", len(profiles), flush=True)

print("calling _build_shortage_vector_completion_profiles ...", flush=True)
completion = mod._build_shortage_vector_completion_profiles(required, slot_sizes, profiles)
t2 = time.perf_counter()
print("completion ready:", len(completion), flush=True)

remaining = {int(round(float(size))): int(required.get(float(size), 0)) for size in required}
for profile in completion:
    for size, count in mod._effective_requirement_counts(profile, slot_sizes).items():
        remaining[int(round(float(size)))] = max(0, remaining.get(int(round(float(size))), 0) - count)

print("generate_seconds=", round(t1 - t0, 6), flush=True)
print("completion_seconds=", round(t2 - t1, 6), flush=True)
print("total_seconds=", round(t2 - t0, 6), flush=True)
print("remaining=", remaining, flush=True)
print("all_closed=", all(v <= 0 for v in remaining.values()), flush=True)
