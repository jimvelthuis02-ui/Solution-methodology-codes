from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
PIPELINE_SCRIPT = ROOT / "Scripts" / "Pipeline" / "run_ordered_pipeline.py"
INPUT_ROOT = ROOT / "Input files"

EXPECTED_INPUT_FILES = [
    "Locations/Location details.xlsx",
    "WSM_Weights.csv",
    "SKU/Merged_Warehouse_SKUs_Aggregated.csv",
    "SKU/Merged_Warehouse_SKUs_Full.csv",
    "SKU/SKU_Analysis_File.csv",
]


def _actual_input_summary() -> list[str]:
    if not INPUT_ROOT.exists():
        return []
    files: list[str] = []
    for path in sorted(INPUT_ROOT.rglob("*")):
        if path.is_file():
            relative = path.relative_to(ROOT).as_posix()
            if relative.startswith("Input files/"):
                files.append(relative.replace("Input files/", ""))
    return files


def _missing_expected_files() -> list[str]:
    discovered = set(_actual_input_summary())
    missing: list[str] = []
    for expected in EXPECTED_INPUT_FILES:
        if expected not in discovered:
            missing.append(expected)
    return missing


st.set_page_config(page_title="Warehouse Layout Pipeline", page_icon="📦", layout="wide")
st.title("Warehouse Layout Pipeline")
st.caption("Run the complete warehouse layout pipeline from the project Input files folder without using VS Code or Python directly.")

with st.sidebar:
    st.header("Input folder")
    st.info("The app will use the repository input folder by default: Input files")

    input_root_exists = INPUT_ROOT.exists()
    if input_root_exists:
        discovered_files = _actual_input_summary()
        st.success(f"Found {len(discovered_files)} files under the project input folder.")
        with st.expander("View input files"):
            for item in discovered_files[:40]:
                st.write(item)
    else:
        st.warning("The project Input files folder was not found.")

    st.markdown("---")
    st.subheader("Expected inputs")
    for expected in EXPECTED_INPUT_FILES:
        state = "✅" if expected in set(_actual_input_summary()) else "⚠️"
        st.write(f"{state} {expected}")

left_col, right_col = st.columns([1.3, 1.0])

with left_col:
    st.subheader("Workflow")
    st.markdown(
        """
        1. Use the project Input files folder.
        2. Click the full pipeline button.
        3. Review the generated Output files.
        4. Download or use the result files directly from Output/.
        """
    )

    full_pipeline_button = st.button("Run full pipeline", type="primary")

with right_col:
    st.subheader("Status")
    missing_files = _missing_expected_files()
    if missing_files:
        st.warning("Some expected input files are still missing from the project input folder.")
        for missing in missing_files:
            st.write(f"- {missing}")
    else:
        st.success("The required project input files are available.")

if full_pipeline_button:
    missing_files = _missing_expected_files()
    if missing_files:
        st.error("The required input files are missing. Please add them to the project Input files folder before running the pipeline.")
    elif not PIPELINE_SCRIPT.exists():
        st.error(f"Pipeline entrypoint was not found: {PIPELINE_SCRIPT}")
    else:
        with st.spinner("Running the full warehouse layout pipeline... This may take several minutes."):
            completed = subprocess.run(
                [sys.executable, str(PIPELINE_SCRIPT)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=1800,
            )

        if completed.returncode == 0:
            st.success("Full pipeline finished successfully.")
            st.caption(f"Outputs were written to: {ROOT / 'Output'}")
            if completed.stdout.strip():
                st.code(completed.stdout.strip(), language="text")
        else:
            st.error("The full pipeline failed.")
            st.code(completed.stdout.strip() or "No stdout captured.", language="text")
            if completed.stderr.strip():
                st.code(completed.stderr.strip(), language="text")

st.markdown("---")
st.caption("This app is intentionally focused on the real pipeline inputs and the full ordered run across all stages, rather than a narrow Stage 6-only probe.")
