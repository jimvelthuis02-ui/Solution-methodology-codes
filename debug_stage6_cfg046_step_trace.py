import importlib.util
import sys
import time
from pathlib import Path

p = Path(r"c:/Users/jimve/OneDrive/Documenten/Master IEM Year 2/Thesis Benchmark/Github/Solution-methodology-codes/Scripts/Pipeline/06_Layout_Generation/06_layout_generation.py")
spec = importlib.util.spec_from_file_location("stage6", p)
mod = importlib.util.module_from_spec(spec)
sys.modules["stage6"] = mod
spec.loader.exec_module(mod)

rows = [r for r in mod._read_csv(mod.INPUT_CAPACITY_FILE) if r.get("Config_ID", "").strip() == "CFG_046"]
required = {float(k): int(v) for k, v in mod._base_exact_counts(rows).items()}
slot_sizes = mod._slot_sizes_from_capacity(rows)

print("STEP 1: start profile generation")
start = time.perf_counter()
profiles = mod._generate_feasible_rack_profiles(slot_sizes, required_counts=required)
print("STEP 2: profile generation finished in", round(time.perf_counter() - start, 3), "secs")
print("PROFILE_COUNT:", len(profiles))
print("PROFILE_SAMPLE:", profiles[:10])

print("STEP 3: start completion pool")
start = time.perf_counter()
completion = mod._build_shortage_vector_completion_profiles(required, slot_sizes, seed_profiles=profiles)
print("STEP 4: completion pool finished in", round(time.perf_counter() - start, 3), "secs")
print("COMPLETION_COUNT:", len(completion))
print("COMPLETION_SAMPLE:", completion[:10])

print("STEP 5: start full layout build")
start = time.perf_counter()
assignments = mod._build_deficit_coverage_layout(
    rack_columns=mod.common._build_layout_columns(mod._read_csv(mod.INPUT_PREPARED)),
    required_counts=required,
    config_slot_sizes=slot_sizes,
    config_deadline=None,
    config_id="CFG_046",
)
print("STEP 6: full layout build finished in", round(time.perf_counter() - start, 3), "secs")
print("NONEMPTY_COLUMNS:", len([k for k, v in assignments.items() if v]))
print("MIN_OK:", mod._minimum_required_counts_are_satisfied(
    assignments,
    [k for k, v in assignments.items() if v],
    slot_sizes,
    required,
))
print("SAMPLE_ASSIGNMENTS:", list(assignments.items())[:5])
