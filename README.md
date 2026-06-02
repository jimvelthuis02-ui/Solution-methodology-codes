# Solution Methodology Thesis Repository

This repository is structured for the Solution Methodology phase of a thesis.
It is stage-driven, reproducible, and traceable by default.

## Project Overview

The workflow is organized into six phases:

1. `1_Initial`: problem setup, assumptions, and initial context.
2. `2_Model_Design`: design alternatives, rationale, and constraints.
3. `3_Model_Implementation`: implementation of candidate methods.
4. `4_Testing_Evaluation`: tests, metrics, and interpretation.
5. `5_Baselines`: baseline implementations for comparison.
6. `6_Data_Quality`: schema and quality checks for inputs.

## Stage Structure

- `Scripts/<stage>` contains stage logic.
- `Output/<stage>/<run_id>` contains stage-specific artifacts.
- `Scripts/Shared` contains reusable utilities.
- `Tests/unit` and `Tests/integration` contain tests.
- `Docs/Methodology` and `Docs/Experiments` contain methodology and run records.
- `Input files/*` contains immutable input folders by source type.

## How To Run Each Stage

1. Create and activate a Python environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Execute any stage runner from repo root:
   - `python Run_1_Initial.py`
   - `python Run_2_Model_Design.py`
   - `python Run_3_Model_Implementation.py`
   - `python Run_4_Testing_Evaluation.py`
   - `python Run_5_Baselines.py`
   - `python Run_6_Data_Quality.py`
4. Run tests:
   - `pytest -q`

## Reproducibility Rules

- Every execution must generate a unique `run_id`.
- Fix and record random seeds whenever stochastic logic is added.
- Write machine-readable metadata and metric files for every run.
- Maintain a run registry entry in `Docs/Experiments/Run_Registry.csv`.
- Use experiment logs based on `Docs/Experiments/Experiment_Log_Template.md`.

## Output Naming Convention

Stage outputs use this structure:

- `Output/<stage_name>/<run_id>/`

Recommended artifact names inside each run folder:

- `run_metadata.json`
- `metrics.json`
- `predictions.csv`
- `notes.md` (optional)

## Data Immutability Principle

- Raw inputs in `Input files/` are immutable and never overwritten.
- Generated artifacts must only be written under stage-specific `Output/` folders.
- Do not mix source inputs and generated outputs in the same directory.

## Documentation Entry Points

- Data contract: `Docs/Methodology/Data_Contract.md`
- Model design decisions: `Docs/Methodology/Model_Design_Decisions.md`
- Experiment log template: `Docs/Experiments/Experiment_Log_Template.md`
- Run registry: `Docs/Experiments/Run_Registry.csv`
