import csv
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAGE6_PATH = ROOT / 'Scripts' / 'Pipeline' / '06_Layout_Generation' / '06_layout_generation.py'

spec = importlib.util.spec_from_file_location('stage6_debug', STAGE6_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules['stage6_debug'] = mod
spec.loader.exec_module(mod)


def read_csv(path: Path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


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
    counts: dict[float, int] = {}
    for part in raw.split('|'):
        if not part:
            continue
        try:
            size_text, count_text = part.split(':', 1)
            size = float(size_text.strip())
            count = int(count_text.strip())
            counts[size] = count
        except ValueError:
            continue
    return counts


config_rows = read_csv(ROOT / 'Output/04_Candidate_Configuration/Candidate_Configurations.csv')
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

TARGET_CFGS = ['CFG_077', 'CFG_067', 'CFG_115', 'CFG_094', 'CFG_104']

for cfg in TARGET_CFGS:
    sizes = config_slot_map.get(cfg)
    required = required_map.get(cfg)
    if not sizes or not required:
        print(f'CFG {cfg}: missing slot sizes or required counts')
        continue

    print(f'\n=== {cfg} ===')
    print('slot sizes:', sizes)
    print('required sizes:', sorted({int(round(float(s))) for s in required.keys()}, reverse=True))
    print('required counts:', {int(round(float(k))): v for k, v in required.items()})

    try:
        profiles = mod._generate_feasible_rack_profiles(sizes, timeout_seconds=30.0, required_counts=required)
    except Exception as exc:
        print('PROFILE GENERATION ERROR:', type(exc).__name__, str(exc))
        continue

    shortlist = mod._choose_profile_shortlist(profiles, required, limit=12)
    required_values = sorted({int(round(float(s))) for s in required.keys()}, reverse=True)
    shortlist_values = sorted({int(round(float(v))) for p in shortlist for v in p})
    missing = [v for v in required_values if v not in shortlist_values]

    print('raw profiles:', len(profiles))
    print('shortlist count:', len(shortlist))
    print('shortlist sizes:', shortlist_values)
    print('missing from shortlist:', missing)
    print('top raw profiles:')
    for i, profile in enumerate(profiles[:8], start=1):
        print(f'  {i}: {profile}')
    print('top shortlist profiles:')
    for i, profile in enumerate(shortlist[:8], start=1):
        print(f'  {i}: {profile}')
