import csv
import importlib.util
import time
from itertools import product

repo_root = r"c:\Users\jimve\OneDrive\Documenten\Master IEM Year 2\Thesis Benchmark\Github\Solution-methodology-codes"

spec = importlib.util.spec_from_file_location(
    "stage6",
    repo_root + r"\Scripts\Pipeline\06_Layout_Generation\06_layout_generation.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def old_legal_topfill_values(config_values):
    config_values = tuple(sorted(set(config_values)))
    legal_values = set()
    max_lower_rows = min(12, max(2, len(config_values) * 6))
    for lower_count in range(1, max_lower_rows + 1):
        for lower_combo in product(config_values, repeat=lower_count):
            lower_slots = [float(value) for value in lower_combo]
            support_height = sum(lower_slots) + max(len(lower_slots) - 1, 0) * mod.common.BEAM_HEIGHT
            if support_height < 504.0 - 1e-9:
                continue
            final_value = mod.common.MAX_USED_HEIGHT_BASE - sum(lower_slots) - mod.common.BEAM_HEIGHT * len(lower_slots)
            if final_value <= 0.0:
                continue
            if final_value > 214.0:
                continue
            rounded = int(round(final_value))
            if rounded < min(config_values):
                continue
            if rounded % 10 not in (4, 9):
                continue
            legal_values.add(rounded)
    return legal_values

families = [
    (69, 109, 149),
    (69, 109, 149, 189),
    (69, 109, 149, 189, 229),
    (69, 109, 149, 189, 229, 269),
]

for fam in families:
    start = time.perf_counter()
    old = old_legal_topfill_values(fam)
    old_time = time.perf_counter() - start

    start = time.perf_counter()
    new = mod._legal_topfill_values_cached(tuple(sorted(fam)))
    new_time = time.perf_counter() - start

    print(f"family={fam}")
    print(f"old_count={len(old)} old_time={old_time:.6f}s")
    print(f"new_count={len(new)} new_time={new_time:.6f}s")
    print(f"matches={old == new}")
    print("---")
