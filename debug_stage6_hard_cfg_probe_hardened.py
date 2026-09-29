import csv
import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAGE6_PATH = ROOT / 'Scripts' / 'Pipeline' / '06_Layout_Generation' / '06_layout_generation.py'

print('Loading Stage 6 module...')
spec = importlib.util.spec_from_file_location('stage6_debug', STAGE6_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules['stage6_debug'] = mod
spec.loader.exec_module(mod)
print('Stage 6 module loaded OK.')


def read_csv(path: Path):
    print(f'Reading CSV: {path}')
    with open(path, newline='') as f:
        rows = list(csv.DictReader(f))
    print(f'  rows read: {len(rows)}')
    return rows


def parse_slot_sizes(raw: str) -> list[float]:
    raw = raw.strip()
    if raw.startswith('="') and raw.endswith('"'):
        raw = raw[2:-1]
    if not raw:
        return []
    return [float(x.strip()) for x in raw.split(',') if x.strip()]


def parse_required_counts(raw: str) -> dict[float, int]:
    raw = raw.strip()
    if raw.startswith('="') and raw.endswith('"'):
        raw = raw[2:-1]
    if not raw:
        return {}
    out: dict[float, int] = {}
    for part in raw.split('|'):
        if not part:
            continue
        try:
            size_text, count_text = part.split(':', 1)
            out[float(size_text.strip())] = int(count_text.strip())
        except ValueError:
            continue
    return out


print('Loading config rows...')
config_rows = read_csv(ROOT / 'Output/04_Candidate_Configuration/Candidate_Configurations.csv')
print('Loading capacity rows...')
capacity_rows = read_csv(ROOT / 'Output/05_Capacity_Determination/Constraint_Location_Counts_By_Slot_Size.csv')

config_slot_map = {}
for row in config_rows:
    cfg = str(row.get('Config_ID', '')).strip()
    if not cfg:
        continue
    config_slot_map[cfg] = parse_slot_sizes(str(row.get('Slot_Sizes', '')))

required_map = {}
for row in capacity_rows:
    cfg = str(row.get('Config_ID', '')).strip()
    if not cfg:
        continue
    required_map[cfg] = parse_required_counts(str(row.get('Capacity_Configuration_Exact_Counts', '')))

print(f'Loaded configs: {len(config_slot_map)}')
print(f'Loaded required maps: {len(required_map)}')

TARGET_CFGS = ['CFG_077', 'CFG_067', 'CFG_115', 'CFG_094', 'CFG_104']
for cfg in TARGET_CFGS:
    print(f'\n==== Starting cfg: {cfg} ====')
    sizes = config_slot_map.get(cfg)
    required = required_map.get(cfg)
    if not sizes:
        print(f'No slot sizes found for {cfg}.')
        continue
    if not required:
        print(f'No required counts found for {cfg}.')
        continue

    print('slot sizes:', sizes)
    print('required sizes:', sorted({int(round(float(s))) for s in required.keys()}, reverse=True))
    print('required counts:', {int(round(float(k))): v for k, v in required.items()})

    try:
        print('Calling _generate_feasible_rack_profiles...')
        profiles = mod._generate_feasible_rack_profiles(sizes, timeout_seconds=30.0, required_counts=required)
        print(f'Generated profile count: {len(profiles)}')
    except Exception as exc:
        print('PROFILE GENERATION ERROR:')
        traceback.print_exc()
        continue

    try:
        print('Calling _choose_profile_shortlist...')
        shortlist = mod._choose_profile_shortlist(profiles, required, limit=12)
        print(f'Shortlist count: {len(shortlist)}')
    except Exception as exc:
        print('SHORTLIST ERROR:')
        traceback.print_exc()
        continue

    required_values = sorted({int(round(float(s))) for s in required.keys()}, reverse=True)
    shortlist_values = sorted({int(round(float(v))) for p in shortlist for v in p})
    missing = [v for v in required_values if v not in shortlist_values]
    print('shortlist values:', shortlist_values)
    print('missing from shortlist:', missing)
    print('top raw profiles:')
    for i, profile in enumerate(profiles[:8], start=1):
        print(f'  {i}: {profile}')
    print('top shortlist profiles:')
    for i, profile in enumerate(shortlist[:8], start=1):
        print(f'  {i}: {profile}')
