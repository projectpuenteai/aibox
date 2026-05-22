# Project Puente AI — System Health Checks

Long-term reliability checks for the AIBox stack.

## Quick start

```powershell
# From C:\AIBox
.venv-rag\Scripts\activate
pip install -r aibox\checks\requirements.txt

# Dry-run every check (no side effects, writes placeholder metrics)
python -m aibox.checks.harness.runner --dry-run all

# Run one suite for real
python -m aibox.checks.harness.runner --suite hardware

# Run one specific check
python -m aibox.checks.harness.runner --id 6.1

# Generate the HTML report for the latest run
python -m aibox.checks.harness.report --latest
```

## What's real, what's a stub

Each check file has a `safe_in_live_stack` flag and a `STATUS` constant:
- `STATUS = "real"` — the check executes against the live stack or a read-only snapshot. Safe to run any time.
- `STATUS = "stub"` — placeholder that records what it *would* do but does not perform the work. Common for destructive (kill containers, fill disk) or hardware-dependent (second-device probe) checks.

Run `python -m aibox.checks.harness.runner --status` to see the current real/stub matrix.

## Architecture

- `harness/store.py` — SQLite result store at `results.db`
- `harness/base.py` — `Check` base class + `@register` decorator
- `harness/context.py` — per-run context (git sha, env hash, GPU info, snapshot helper)
- `harness/runner.py` — CLI: `--suite`, `--id`, `--dry-run`, `--quick`, `--full`, `--status`
- `harness/report.py` — HTML + Markdown report generator
- `<suite>/` — one file per numbered check (e.g. `hardware/check_6_1_gpu_telemetry.py`)
- `baselines/current.yaml` — pinned thresholds; reporter flags regressions
- `fixtures/` — seeded queries and synthetic docs
- `reports/` — generated output (gitignored)
- `scratch/` — snapshot scratch space (gitignored)

## Safety

Destructive checks must call `ctx.snapshot(path)` to hardlink-copy data into `scratch/<run_id>/` and operate on the copy. The base class refuses to run a check marked `destructive=True` against the live data path unless `--i-mean-it` is passed.
