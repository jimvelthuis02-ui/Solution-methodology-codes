import importlib.util, time
from itertools import product

spec = importlib.util.spec_from_file_location('stage6', r'c:\Users\jimve\OneDrive\Documenten\Master IEM Year 2\Thesis Benchmark\Github\Solution-methodology-codes\Scripts\Pipeline\06_Layout_Generation\06_layout_generation.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fam = (69, 109, 149, 189, 229)

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

start = time.perf_counter(); old = old_legal_topfill_values(fam); old_t = time.perf_counter() - start
start = time.perf_counter(); new = mod._legal_topfill_values_cached(tuple(sorted(fam))); new_t = time.perf_counter() - start
with open(r'c:\Users\jimve\OneDrive\Documenten\Master IEM Year 2\Thesis Benchmark\Github\Solution-methodology-codes\tmp_stage6_single_probe.txt', 'w', encoding='utf-8') as f:
    f.write(f'family={fam}\n')
    f.write(f'old_count={len(old)} old_time={old_t:.6f}s\n')
    f.write(f'new_count={len(new)} new_time={new_t:.6f}s\n')
    f.write(f'matches={old == new}\n')
