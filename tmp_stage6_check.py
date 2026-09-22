import runpy
import traceback

path = r"C:/Users/jimve/OneDrive/Documenten/Master IEM Year 2/Thesis Benchmark/Github/Solution-methodology-codes/Scripts/Pipeline/06_Layout_Generation/06_layout_generation.py"
print("START")
try:
    runpy.run_path(path, run_name="__main__")
    print("DONE")
except Exception:
    traceback.print_exc()
    raise
