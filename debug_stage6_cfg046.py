import importlib.util
import sys
from pathlib import Path

p = Path(r'c:/Users/jimve/OneDrive/Documenten/Master IEM Year 2/Thesis Benchmark/Github/Solution-methodology-codes/Scripts/Pipeline/06_Layout_Generation/06_layout_generation.py')
spec = importlib.util.spec_from_file_location('stage6', p)
mod = importlib.util.module_from_spec(spec)
sys.modules['stage6'] = mod
spec.loader.exec_module(mod)

rows = [r for r in mod._read_csv(mod.INPUT_CAPACITY_FILE) if r.get('Config_ID', '').strip() == 'CFG_046']
base = mod._base_exact_counts(rows)
slot_sizes = mod._slot_sizes_from_capacity(rows)
cols = mod._build_deficit_coverage_layout(
    rack_columns=mod.common._build_layout_columns(mod._read_csv(mod.INPUT_PREPARED)),
    required_counts={float(k): int(v) for k, v in base.items()},
    config_slot_sizes=slot_sizes,
    config_deadline=None,
    config_id='CFG_046',
)

layout_cols = list(cols.keys())
nonempty = [k for k, v in cols.items() if v]
assigned_total = sum(len(v) for v in cols.values())
required_total = sum(base.values())
capacity_margin = assigned_total - required_total
print('BASE:', base)
print('SLOT_SIZES:', slot_sizes)
print('ASSIGNED_TOTAL:', assigned_total)
print('REQUIRED_TOTAL:', required_total)
print('CAPACITY_MARGIN:', capacity_margin)
print('SPACE_UTILIZATION:', sum(mod._column_physical_height_usage(v) for v in cols.values()) / (len(layout_cols) * mod.common.MAX_USED_HEIGHT_BASE))
print('MINIMUM_COUNTS_OK:', mod._minimum_required_counts_are_satisfied(cols, nonempty, slot_sizes, base))
print('UNIFORM_OK:', mod._rack_profiles_are_exactly_uniform(cols, nonempty))
print('EXPLICIT_TARGET:', mod.common._explicit_occupied_target_total())

minimum_value = int(round(float(min(slot_sizes))))
config_values = mod._config_size_values(slot_sizes)
legal_topfill_values = mod._legal_topfill_values(slot_sizes)
print('CONFIG_VALUES:', sorted(config_values))
print('LEGAL_TOPFILL_VALUES:', sorted(legal_topfill_values))

for column_key in nonempty:
    slots = [float(v) for v in cols.get(column_key, []) if float(v) > 0.0]
    print('\nCOLUMN', column_key, 'SLOTS=', slots)
    print('  all_positive=', all(float(v) > 0.0 for v in slots))
    print('  min_ok=', all(int(round(float(v))) >= minimum_value for v in slots))
    print('  all_above_4=', all(int(round(float(v))) >= 4 for v in slots))
    print('  all_below_max=', all(int(round(float(v))) <= int(round(mod.common.MAX_REPRESENTATIVE_SLOT_SIZE_CM)) for v in slots))
    target_total = sum(slots) + (len(slots) - 1) * mod.common.BEAM_HEIGHT
    print('  target_total=', target_total)
    print('  target_below_cap=', target_total <= mod.common.MAX_USED_HEIGHT_BASE + 1e-6)
    if len(slots) >= 1:
        for idx, value in enumerate(slots):
            rounded = int(round(float(value)))
            print('    value=', value, 'rounded=', rounded, 'mod10=', rounded % 10, 'in_config=', rounded in config_values, 'in_topfill=', rounded in legal_topfill_values, 'effective=', mod._effective_requirement_slot_size(rounded, slots, slot_sizes))
    print('  support_band_ok=', mod._column_support_band_is_valid(slots))
    final_value = int(round(float(slots[-1])))
    print('  final_value_in_config=', final_value in config_values)
    print('  final_value_in_topfill=', final_value in legal_topfill_values)
    print('  per_column_call=', mod._layout_assignments_are_feasible({column_key: slots}, [column_key], min(slot_sizes), slot_sizes, minimum_required_counts=base, enforce_minimum_total_locations=True))

print('\nFULL_LAYOUT_FEASIBLE=', mod._layout_assignments_are_feasible(cols, layout_cols, min(slot_sizes), slot_sizes, minimum_required_counts=base, enforce_minimum_total_locations=True))
