from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = ROOT / "Scripts" / "Pipeline"
PIPELINE_SCRIPT = PIPELINE_DIR / "run_ordered_pipeline.py"

STAGES = [
    ("Stage 1", "Data Preparation", Path("01_Data_Preparation/01_data_preparation.py")),
    ("Stage 2", "Scenario Generation", Path("02_Scenario_Generation/02_scenario_generation_weighted_delta.py")),
    ("Stage 3", "Slot Size Generation", Path("03_Slot_Size_Generation/03_slot_size_generation_main.py")),
    ("Stage 4", "Candidate Configuration", Path("04_Candidate_Configuration/04_candidate_configuration.py")),
    ("Stage 5", "Capacity Determination", Path("05_Capacity_Determination/05_capacity_determination.py")),
    ("Stage 6", "Layout Generation", Path("06_Layout_Generation/06_layout_generation.py")),
    ("Stage 7", "Robustness Evaluation", Path("07_Robustness_Evaluation/07_robustness_evaluation.py")),
    ("Stage 8", "Final Selection", Path("08_Final_Selection/08_final_selection.py")),
]
CLUSTERING_METHODS = {
    "Quantile binning": "quantile_binning",
    "Hierarchical clustering": "hierarchical_clustering",
    "K-means clustering": "kmeans_clustering",
}
SCENARIOS = [f"Scenario {number}" for number in range(1, 7)]
K_VALUES = list(range(1, 11))


def _start_process(
    label: str,
    command: list[str],
    working_directory: Path,
    environment_overrides: dict[str, str] | None = None,
) -> None:
    log_path = Path(tempfile.gettempdir()) / f"warehouse_pipeline_{uuid.uuid4().hex}.log"
    environment = os.environ.copy()
    environment.update(environment_overrides or {})
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(PIPELINE_DIR) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(working_directory),
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    st.session_state["pipeline_process"] = process
    st.session_state["pipeline_log_path"] = str(log_path)
    st.session_state["pipeline_run_label"] = label


def _configuration_environment(
    selected_methods: list[str],
    selected_scenarios: list[str],
    selected_k_values: list[int],
) -> dict[str, str]:
    return {
        "PIPELINE_CONFIG_METHODS": ",".join(CLUSTERING_METHODS[label] for label in selected_methods),
        "PIPELINE_CONFIG_SCENARIOS": ",".join(selected_scenarios),
        "PIPELINE_CONFIG_K_VALUES": ",".join(str(value) for value in selected_k_values),
    }


def _read_log_tail(log_path: str, max_chars: int = 16000) -> str:
    path = Path(log_path)
    if not path.exists():
        return "No log output is available yet."
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Unable to read run log: {exc}"
    return text[-max_chars:] if text else "The process has not written output yet."


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


st.set_page_config(page_title="Warehouse Layout Pipeline", page_icon="📦", layout="wide")
st.title("Warehouse Layout Pipeline")
st.caption("Run the complete pipeline or launch a single stage using the current project inputs and outputs.")

process = st.session_state.get("pipeline_process")
log_path = st.session_state.get("pipeline_log_path", "")
run_label = st.session_state.get("pipeline_run_label", "")
is_running = process is not None and process.poll() is None

status_col, action_col = st.columns([3, 1])
with status_col:
    if is_running:
        st.warning(f"Running: {run_label}")
    elif process is not None:
        return_code = process.returncode
        if return_code == 0:
            st.success(f"Completed: {run_label}")
        elif return_code is not None:
            st.error(f"Stopped or failed: {run_label} (exit code {return_code})")
with action_col:
    if is_running:
        if st.button("Stop current run", type="secondary", use_container_width=True):
            _stop_process(process)
            st.rerun()
    elif process is not None:
        if st.button("Refresh status", use_container_width=True):
            st.rerun()

st.subheader("Full pipeline")
st.write("Choose the clustering configurations to generate. Stage 4 keeps at most one config for each selected method, scenario, and K combination, excluding illegal slot profiles.")
with st.expander("Configuration scope", expanded=True):
    selected_methods = st.multiselect(
        "Clustering methods",
        options=list(CLUSTERING_METHODS),
        default=list(CLUSTERING_METHODS),
    )
    selected_k_values = st.multiselect(
        "K values",
        options=K_VALUES,
        default=[3, 4],
    )
    selected_scenarios = st.multiselect(
        "Scenarios",
        options=SCENARIOS,
        default=SCENARIOS,
    )
    requested_combinations = len(selected_methods) * len(selected_k_values) * len(selected_scenarios)
    st.metric("Maximum candidate configurations", requested_combinations)
    if selected_methods and selected_k_values and selected_scenarios:
        st.caption(
            "Potential configs by method: "
            + ", ".join(
                f"{label}: {len(selected_k_values) * len(selected_scenarios)}"
                for label in selected_methods
            )
        )
        st.caption(
            "Potential configs by K: "
            + ", ".join(
                f"K={k_value}: {len(selected_methods) * len(selected_scenarios)}"
                for k_value in selected_k_values
            )
        )
        st.caption(
            "Potential configs by scenario: "
            + ", ".join(
                f"{scenario}: {len(selected_methods) * len(selected_k_values)}"
                for scenario in selected_scenarios
            )
        )

if st.button(
    "Run full pipeline",
    type="primary",
    disabled=is_running or requested_combinations == 0,
):
    if not PIPELINE_SCRIPT.exists():
        st.error(f"Pipeline entrypoint not found: {PIPELINE_SCRIPT}")
    else:
        _start_process(
            "Full pipeline",
            [sys.executable, str(PIPELINE_SCRIPT)],
            ROOT,
            environment_overrides=_configuration_environment(
                selected_methods,
                selected_scenarios,
                selected_k_values,
            ),
        )
        st.rerun()

st.divider()
st.subheader("Run one stage")
st.caption("Individual stages use outputs already present from earlier stages. Run them in order when upstream inputs have changed.")

button_columns = st.columns(2)
for index, (stage_number, stage_name, relative_script) in enumerate(STAGES):
    script_path = PIPELINE_DIR / relative_script
    with button_columns[index % 2]:
        if st.button(
            f"Run {stage_number}: {stage_name}",
            disabled=is_running,
            use_container_width=True,
            key=f"run_{stage_number.lower().replace(' ', '_')}",
        ):
            if not script_path.exists():
                st.error(f"Stage script not found: {script_path}")
            else:
                stage_environment = (
                    _configuration_environment(selected_methods, selected_scenarios, selected_k_values)
                    if stage_number == "Stage 4"
                    else None
                )
                _start_process(
                    f"{stage_number}: {stage_name}",
                    [sys.executable, str(script_path)],
                    PIPELINE_DIR,
                    environment_overrides=stage_environment,
                )
                st.rerun()

if log_path:
    st.divider()
    st.subheader("Run output")
    if st.button("Refresh output", disabled=not is_running):
        st.rerun()
    st.code(_read_log_tail(log_path), language="text")

st.caption(f"Pipeline outputs are written to {ROOT / 'Output'}.")