import importlib.util
p = r'.\Scripts\Pipeline\06_Layout_Generation\06_layout_generation.py'
spec = importlib.util.spec_from_file_location('layout6', p)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
profiles = mod._generate_feasible_rack_profiles_cached((69.0, 124.0, 234.0), timeout_seconds=30.0, config_deadline=999999.0)
print('COUNT', len(profiles))
print('PROFILES', profiles)
print('CANDIDATE_POOL_SIZE', len(mod._generate_feasible_rack_profiles_cached((69.0,124.0,234.0), timeout_seconds=30.0, config_deadline=999999.0)))
