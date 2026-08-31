import importlib.util
root = 'C:/Users/jimve/OneDrive/Documenten/Master IEM Year 2/Thesis Benchmark/Github/Solution-methodology-codes'
path = root + '/Scripts/Pipeline/06_Layout_Generation/06_layout_generation.py'
spec = importlib.util.spec_from_file_location('stage6_layout', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
profiles = mod._generate_feasible_rack_profiles([64.0, 119.0, 234.0])
print('count', len(profiles))
for p in profiles[:20]:
    print(p)
print('top_ranked')
ranked = sorted(profiles, key=lambda p: mod._profile_requirement_priority(p, {64.0: 411, 119.0: 315, 234.0: 164}), reverse=True)
for p in ranked[:10]:
    print(p)
