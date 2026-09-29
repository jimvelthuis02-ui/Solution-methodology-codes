import importlib.util
import sys
from pathlib import Path

root = Path(r'c:\Users\jimve\OneDrive\Documenten\Master IEM Year 2\Thesis Benchmark\Github\Solution-methodology-codes')
module_path = root / 'Scripts' / 'Pipeline' / '06_Layout_Generation' / '06_layout_generation.py'

spec = importlib.util.spec_from_file_location('stage6', module_path)
mod = importlib.util.module_from_spec(spec)
sys.modules['stage6'] = mod
spec.loader.exec_module(mod)

families = {
    'CFG_094': (84.0, 124.0, 174.0, 239.0),
    'CFG_104': (89.0, 134.0, 189.0, 239.0),
    'CFG_115': (74.0, 94.0, 139.0, 179.0, 239.0),
}

lines = []
for cfg, sizes in families.items():
    legal = sorted(mod._legal_topfill_values(sizes))
    profiles = mod._generate_feasible_rack_profiles_cached(tuple(sorted(set(sizes))), timeout_seconds=5.0, required_counts=tuple((float(size), 1) for size in sizes))
    lines.append(f'{cfg}: legal_topfills={legal}')
    lines.append(f'{cfg}: profile_count={len(profiles)}')
    for profile in profiles[:12]:
        lines.append(f'{cfg}: {profile}')
    lines.append('')

out = root / 'debug_stage6_probe_out.txt'
out.write_text('\n'.join(lines), encoding='utf-8')
print('WROTE', out)
