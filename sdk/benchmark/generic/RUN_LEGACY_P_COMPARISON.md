# Legacy / P cross-revision comparison

`run_legacy_p_comparison.py` compares the pre-refactor Legacy runtime with the
current passthrough policy in separate Git worktrees and Python environments.
It is independent of `run_context_manager_comparison.py`, whose P/C protocol and
artifacts are unchanged.

The standard arms are:

| Arm | Source | Context configuration |
|---|---|---|
| L | Legacy worktree | `--disable-context-manager` |
| P | Candidate worktree | `--context-processing-mode passthrough` |

This is a cross-revision regression comparison. If prompt or tool hashes differ,
the report sets `causal_scope=end_to_end_revision`; the result must not be
described as a ContextManager-only effect. Use `--require-assembly-parity` when
such drift should fail the comparison.

## Environment check

Before a formal run, verify that each interpreter imports its own worktree:

```bash
/home/feiran/nexent-benchmark0720-legacy-managed-compress/backend/.venv/bin/python \
  -c "import sys; sys.path.insert(0, '/home/feiran/nexent-benchmark0720-legacy-managed-compress/sdk/benchmark'); import paths, nexent; print(nexent.__file__)"

/home/feiran/nexent/backend/.venv/bin/python \
  -c "import sys; sys.path.insert(0, '/home/feiran/nexent/sdk/benchmark'); import paths, nexent; print(nexent.__file__)"
```

## Smoke

The candidate budget options are P-only because the Legacy runner does not
support the new budget CLI:

```bash
backend/.venv/bin/python \
  sdk/benchmark/generic/run_legacy_p_comparison.py \
  --dataset gaia-level1-reasoning \
  --run-prefix gaia-reasoning-lp-smoke-YYYYMMDD \
  --legacy-root /home/feiran/nexent-benchmark0720-legacy-managed-compress \
  --smoke-only \
  --smoke-items 1 \
  --candidate-soft-input-budget 10000 \
  --candidate-hard-input-budget 900000 \
  --candidate-context-window-tokens 1000000 \
  --candidate-budget-profile synthetic_trigger \
  --runner-args \
    --agent-config /home/feiran/nexent/sdk/benchmark/generic/configs/gaia_solver.yaml \
    --language zh \
    --evaluators gaia_exact_match \
    --max-steps 15 \
    --temperature 0
```

## Formal run

Use a new immutable prefix and select the desired repeat count:

```bash
backend/.venv/bin/python \
  sdk/benchmark/generic/run_legacy_p_comparison.py \
  --dataset gaia-level1-reasoning \
  --run-prefix gaia-reasoning-lp-formal-YYYYMMDD \
  --legacy-root /home/feiran/nexent-benchmark0720-legacy-managed-compress \
  --skip-smoke \
  --repeat 5 \
  --candidate-soft-input-budget 10000 \
  --candidate-hard-input-budget 900000 \
  --candidate-context-window-tokens 1000000 \
  --candidate-budget-profile synthetic_trigger \
  --runner-args \
    --agent-config /home/feiran/nexent/sdk/benchmark/generic/configs/gaia_solver.yaml \
    --language zh \
    --evaluators gaia_exact_match \
    --max-steps 15 \
    --temperature 0
```

Reports are written under `artifacts/legacy_p_comparisons/`, separate from the
existing P/C reports. The paired matrix uses:

- `PP`: both arms pass
- `PF`: Legacy passes and P fails
- `FP`: Legacy fails and P passes
- `FF`: both arms fail
