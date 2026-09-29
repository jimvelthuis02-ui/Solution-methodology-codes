import importlib.util
import sys
from pathlib import Path

root = Path(r'c:\Users\jimve\OneDrive\Documenten\Master IEM Year 2\Thesis Benchmark\Github\Solution-methodology-codes')
module_path = root / 'Scripts' / 'Pipeline' / '06_Layout_Generation' / '06_layout_generation.py'

spec = importlib.util.spec_from_file_location('stage6_debug', module_path)
mod = importlib.util.module_from_spec(spec)
sys.modules['stage6_debug'] = mod
spec.loader.exec_module(mod)

# Force the exact two hard configs and print the live evidence.
config_ids = {'CFG_067', 'CFG_115'}
rows = mod._candidate_configs_for_exhaustive_search()
selected = [row for row in rows if mod._normalize_config_id(str(row.get('Config_ID', ''))) in config_ids]
print('SELECTED_CONFIGS', [row.get('Config_ID') for row in selected])

for row in selected:
    cfg = mod._normalize_config_id(str(row.get('Config_ID', '')))
    raw_slot_sizes = mod.common._decode_excel_text(row.get('Slot_Sizes', ''))
    slot_sizes = [float(v) for v in str(raw_slot_sizes).split(',') if str(v).strip()]
    capacity_rows = mod._capacity_rows_by_config().get(cfg, [])
    required_counts = mod._base_exact_counts(capacity_rows)
    print(f'CFG={cfg} SLOT_SIZES={slot_sizes}')
    print(f'CFG={cfg} REQUIRED_COUNTS={required_counts}')

    try:
        profiles = mod._generate_feasible_rack_profiles(slot_sizes, timeout_seconds=30.0, required_counts=required_counts)
        print(f'CFG={cfg} PROFILE_COUNT={len(profiles)}')
        for i, profile in enumerate(profiles[:12]):
            print(f'CFG={cfg} PROFILE[{i}]={profile}')
    except Exception as exc:
        print(f'CFG={cfg} PROFILE_EXCEPTION={type(exc).__name__}: {exc}')

    try:
        shortlist = mod._choose_profile_shortlist(
            mod._generate_feasible_rack_profiles(slot_sizes, timeout_seconds=30.0, required_counts=required_counts),
            required_counts,
            limit=20,
        )
        print(f'CFG={cfg} SHORTLIST_COUNT={len(shortlist)}')
        for i, profile in enumerate(shortlist[:12]):
            print(f'CFG={cfg} SHORTLIST[{i}]={profile}')
    except Exception as exc:
        print(f'CFG={cfg} SHORTLIST_EXCEPTION={type(exc).__name__}: {exc}')

    try:
        assignments = mod._build_deficit_coverage_layout(
            rack_columns=[f'A{i:02d}' for i in range(4)],
            required_counts=required_counts,
            config_slot_sizes=slot_sizes,
            config_id=cfg,
        )
        print(f'CFG={cfg} ASSIGNMENT_COUNT={sum(len(v) for v in assignments.values())}')
        print(f'CFG={cfg} ASSIGNMENTS={assignments}')
    except Exception as exc:
        print(f'CFG={cfg} ASSIGNMENT_EXCEPTION={type(exc).__name__}: {exc}')
