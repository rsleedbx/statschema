# Learnings

This directory is for **project learnings**: notes, gotchas, and decisions captured while building and running **statschema** (DDL/stats transpiler and synthetic data pipeline).

## Purpose

- Record what worked and what didn’t (dbldatagen, Protobuf, Databricks Connect, Delta writes).
- Capture scaling observations (tables, columns, types, row volume).
- Document decisions and trade-offs for future reference.

## How to use

- Add new files or sections as you go (e.g. `phase1.md`, `connect-setup.md`, `scaling-runs.md`).
- Or append to a single `learnings.md` with dated entries.

## Related docs

- [../plan.md](../plan.md) – Goals and scaling dimensions
- [../implementation.md](../implementation.md) – Build and SDK usage
- [../testing.md](../testing.md) – Test strategy by phase
