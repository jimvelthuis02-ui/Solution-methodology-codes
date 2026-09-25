import importlib.util
import sys
from pathlib import Path

path = Path(r'c:/Users/jimve/OneDrive/Documenten/Master IEM Year 2/Thesis Benchmark/Github/Solution-methodology-codes/Scripts/Pipeline/06_Layout_Generation/06_layout_generation.py')
spec = importlib.util.spec_from_file_location('stage6', path)
mod = importlib.util.module_from_spec(spec)
sys.modules['stage6'] = mod
spec.loader.exec_module(mod)

req = {84: 373, 114: 216, 194: 265, 239: 72}
profiles = mod._generate_feasible_rack_profiles([84, 114, 194, 239], required_counts=req)
print('count', len(profiles))
print('coverage', mod._quota_coverage_met(profiles, req))
for p in profiles[:12]:
    print(p, '->', mod._effective_requirement_counts(p, [84, 114, 194, 239]))
