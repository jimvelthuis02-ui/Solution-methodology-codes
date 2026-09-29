import importlib.util, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location('stage6', Path('Scripts/Pipeline/06_Layout_Generation/06_layout_generation.py'))
mod = importlib.util.module_from_spec(spec)
sys.modules['stage6'] = mod
spec.loader.exec_module(mod)

configs = {
    'CFG_094': ((84.0,124.0,174.0,239.0), {84.0:312,124.0:236,174.0:279,239.0:99}),
    'CFG_104': ((89.0,134.0,189.0,239.0), {89.0:310,134.0:223,189.0:307,239.0:86}),
    'CFG_115': ((74.0,94.0,139.0,179.0,239.0), {74.0:229,94.0:85,139.0:226,179.0:236,239.0:150}),
}

for cfg, (sizes, req) in configs.items():
    profiles = mod._generate_feasible_rack_profiles_cached(
        tuple(sorted(set(sizes))),
        timeout_seconds=30.0,
        required_counts=tuple((float(k), v) for k, v in req.items()),
    )
    print(f'=== {cfg} count={len(profiles)} ===')
    for p in profiles[:30]:
        print(list(p))
    print()

# show representative expansion paths
print('=== prefix probe ===')
for prefix in [(174,174), (174,124), (179,179), (189,189)]:
    print(prefix, '->', mod._generate_feasible_rack_profiles_cached(tuple(sorted(set((84.0,124.0,174.0,239.0)))), timeout_seconds=5.0, required_counts=((84.0,1),(124.0,1),(174.0,1),(239.0,1)))[:5])
