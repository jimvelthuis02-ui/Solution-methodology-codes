import csv
import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAGE6_PATH = ROOT / 'Scripts' / 'Pipeline' / '06_Layout_Generation' / '06_layout_generation.py'

TARGET_CFGS = ['CFG_077', 'CFG_067', 'CFG_115', 'CFG_094', 'CFG_104']


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
            counts[float(size_text.strip())] = int(count_text.strip())
        except ValueError:
            continue
    return counts


def main():
    print('=== Stage 6 hard-config diagnostic ===', flush=True)
    print(f'Loading Stage 6 module from: {STAGE6_PATH}', flush=True)
    spec = importlib.util.spec_from_file_location('stage6_debug_final', STAGE6_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['stage6_debug_final'] = mod
    try:
        spec.loader.exec_module(mod)
        print('Stage 6 module loaded successfully.', flush=True)
    except Exception:
        print('Failed to import Stage 6 module. Traceback below:', flush=True)
        traceback.print_exc()
        return

    print('Loading config CSV...', flush=True)
    config_rows = read_csv(ROOT / 'Output/04_Candidate_Configuration/Candidate_Configurations.csv')
    print('Loading capacity CSV...', flush=True)
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

    print(f'Loaded {len(config_slot_map)} config entries and {len(required_map)} required-count entries.', flush=True)

    for cfg in TARGET_CFGS:
        print(f'\n===== CONFIG {cfg} =====', flush=True)
        sizes = config_slot_map.get(cfg)
        required = required_map.get(cfg)
        if not sizes:
            print('  -> no slot sizes found', flush=True)
            continue
        if not required:
            print('  -> no required counts found', flush=True)
            continue

        required_values = sorted({int(round(float(s))) for s in required.keys()}, reverse=True)
        print(f'  required sizes: {required_values}', flush=True)
        print(f'  required counts: { {int(round(float(k))): v for k, v in required.items()} }', flush=True)

        try:
            print('  -> generating raw profiles...', flush=True)
            profiles = mod._generate_feasible_rack_profiles(sizes, timeout_seconds=30.0, required_counts=required)
            print(f'  -> raw profile count: {len(profiles)}', flush=True)
        except Exception:
            print('  -> raw profile generation failed with traceback:', flush=True)
            traceback.print_exc()
            continue

        try:
            print('  -> generating shortlist...', flush=True)
            shortlist = mod._choose_profile_shortlist(profiles, required, limit=12)
            print(f'  -> shortlist count: {len(shortlist)}', flush=True)
        except Exception:
            print('  -> shortlist generation failed with traceback:', flush=True)
            traceback.print_exc()
            continue

        shortlist_values = sorted({int(round(float(v))) for profile in shortlist for v in profile})
        missing = [v for v in required_values if v not in shortlist_values]
        print(f'  shortlist values: {shortlist_values}', flush=True)
        print(f'  missing from shortlist: {missing}', flush=True)
        print('  raw top profiles:')
        for i, profile in enumerate(profiles[:8], start=1):
            print(f'    {i}: {profile}', flush=True)
        print('  shortlist top profiles:')
        for i, profile in enumerate(shortlist[:8], start=1):
            print(f'    {i}: {profile}', flush=True)

        if not profiles:
            print('  verdict: profile generation issue', flush=True)
        elif missing:
            print('  verdict: shortlist collapse / rare-family starvation', flush=True)
        else:
            print('  verdict: shortlist covers required families', flush=True)


if __name__ == '__main__':
    main()
