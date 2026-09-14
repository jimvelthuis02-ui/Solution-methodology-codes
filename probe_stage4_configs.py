import csv
import importlib.util
import time
from pathlib import Path

csv_path = Path(r'.\Output\04_Candidate_Configuration\Candidate_Configurations.csv')
mod_path = Path(r'.\Scripts\Pipeline\06_Layout_Generation\06_layout_generation.py')

rows = list(csv.DictReader(open(csv_path, newline='', encoding='utf-8')))
real_cfgs = []
for r in rows[:4]:
    raw = str(r.get('Slot_Sizes', '')).strip()
    raw = raw.replace('=', '').replace('"', '')
    sizes = tuple(int(x.strip()) for x in raw.split(',') if x.strip())
    real_cfgs.append((r.get('Config_ID', ''), sizes))

spec = importlib.util.spec_from_file_location('layout_gen', mod_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

for name, cfg in real_cfgs:
    t0 = time.perf_counter()
    profiles = mod._generate_feasible_rack_profiles_cached(cfg)
    t1 = time.perf_counter()
    print(name, 'slot_sizes=', cfg)
    print('  raw_profile_generation_count=', len(profiles))
    print('  raw_profile_generation_seconds=', round(t1 - t0, 3))

    # Run a lightweight rack-assignment-style phase using the same exact family to give a downstream
    # "pipeline-style" timing estimate without depending on the full external data deck.
    t2 = time.perf_counter()
    shortlist = mod._choose_profile_shortlist(
        [list(p) for p in profiles],
        {float(size): 1 for size in cfg},
        limit=min(5, max(1, len(profiles))),
    )
    t3 = time.perf_counter()
    print('  shortlist_seconds=', round(t3 - t2, 3))
    print('  shortlist_count=', len(shortlist))
    print('  profiles=', profiles)
    print()
