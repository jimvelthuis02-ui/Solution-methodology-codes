# Data Contract

## Purpose

Define immutable input specifications and validation criteria for all methodology stages.

## Input Sources

| Source | Directory | Owner | Refresh Cadence | Immutable |
|---|---|---|---|---|
| Locations | Input files/Locations | TBD | TBD | Yes |
| Frozen Context Outputs | Input files/Frozen Context Outputs | TBD | TBD | Yes |
| Scenario | Input files/Scenario | TBD | TBD | Yes |

## Schema Specification

| Field | Type | Unit | Nullable | Allowed Range/Values | Notes |
|---|---|---|---|---|---|
| example_field | float | unitless | No | [0, 1] | Replace with real field definitions |

## Validation Rules

- Required fields must exist before stage execution.
- Data types and units must match schema definitions.
- Null, duplicate, and out-of-range rates must be logged.
- Any schema drift must fail the run unless explicitly approved.

## Assumptions

- Raw input files are immutable and never overwritten.
- Stage outputs are generated only in stage-specific Output folders.
- Data transformations are documented and reproducible.
