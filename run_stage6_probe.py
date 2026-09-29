import csv
import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAGE6_PATH = ROOT / 'Scripts' / 'Pipeline' / '06_Layout_Generation' / '06_layout_generation.py'

print('ROOT =', ROOT, flush=True)
print('STAGE6_PATH =', STAGE6_PATH, flush=True)
print('EXISTS =', STAGE6_PATH.exists(), flush=True)

if not STAGE6_PATH.exists():
    raise FileNotFoundError(f'Missing Stage 6 file at {STAGE6_PATH}')

print('Importing Stage 6 module...', flush=True)
spec = importlib.util.spec_from_file_location('stage6_probe_mod', STAGE6_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules['stage6_probe_mod'] = mod
try:
    spec.loader.exec_module(mod)
    print('Import succeeded.', flush=True)
except Exception:
    print('Import failed. Traceback follows:', flush=True)
    traceback.print_exc()
    raise

TARGET_CFGS = ['CFG_077', 'CFG_067', 'CFG_115', 'CFG_094', 'CFG_104']


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


config_rows = []
with open(ROOT / 'Output/04_Candidate_Configuration/Candidate_Configurations.csv', newline='') as f:
    config_rows = list(csv.DictReader(f))
print(f'Loaded config rows: {len(config_rows)}', flush=True)

capacity_rows = []
with open(ROOT / 'Output/05_Capacity_Determination/Constraint_Location_Counts_By_Slot_Size.csv', newline='') as f:
    capacity_rows = list(csv.DictReader(f))
print(f'Loaded capacity rows: {len(capacity_rows)}', flush=True)

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

print(f'Loaded config_slot_map entries: {len(config_slot_map)}', flush=True)
print(f'Loaded required_map entries: {len(required_map)}', flush=True)

for cfg in TARGET_CFGS:
    print(f'\n=== {cfg} ===', flush=True)
    sizes = config_slot_map.get(cfg)
    required = required_map.get(cfg)
    if not sizes:
        print('No slot sizes found', flush=True)
        continue
    if not required:
        print('No required counts found', flush=True)
        continue

    print('slot sizes:', sizes, flush=True)
    print('required counts:', {int(round(float(k))): v for k, v in required.items()}, flush=True)

    try:
        profiles = mod._generate_feasible_rack_profiles(sizes, timeout_seconds=30.0, required_counts=required)
        print('generated profiles:', len(profiles), flush=True)
    except Exception:
        print('Profile generation raised an exception:', flush=True)
        traceback.print_exc()
        continue

    try:
        shortlist = mod._choose_profile_shortlist(profiles, required, limit=12)
        print('shortlist count:', len(shortlist), flush=True)
    except Exception:
        print('Shortlist generation raised an exception:', flush=True)
        traceback.print_exc()
        continue

    required_values = sorted({int(round(float(s))) for s in required.keys()}, reverse=True)
    shortlist_values = sorted({int(round(float(v))) for profile in shortlist for v in profile})
    missing = [v for v in required_values if v not in shortlist_values]
    print('shortlist values:', shortlist_values, flush=True)
    print('missing from shortlist:', missing, flush=True)
    print('top 5 raw profiles:', profiles[:5], flush=True)
    print('top 5 shortlist profiles:', shortlist[:5], flush=True)
