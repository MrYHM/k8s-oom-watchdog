# Contributing

Thanks for looking. This is a small, focused project: a single-purpose sidecar
that borrows memory before the kernel OOM-killer fires. Contributions that keep
it small are the ones most likely to land.

## Running the tests

No cluster, no dependencies, no fixtures:

```bash
python3 test_watchdog.py
```

62 tests, well under a second. They import the production module directly and
use only the standard library — the `kubernetes` package is **not** needed,
because every clock, API call and file read enters `Watchdog` through an
injectable seam. This is the project's main design constraint: **if a change
cannot be tested this way, it usually means the seam is in the wrong place.**

Coverage today: unit conversion, the scale-up/scale-down decision functions,
cgroup parsing and container location, and the main-loop state machine (pending
supervision, Infeasible/timeout rollback, circuit breaking, scale-down
withdrawal, cgroup relocation, external-resize adoption).

## Running it for real

```bash
pip install -e .          # just the kubernetes client
```

For an end-to-end run on a cluster (Kubernetes >= 1.33), see
[`demo/`](demo/) — two commands, self-contained namespace, tears down cleanly.
[`demo/RECORDING.md`](demo/RECORDING.md) covers the gotchas if things do not
work on your cluster type.

CI runs the same tests plus a multi-arch image build on every push and PR.

## What makes a good change here

**Welcome:**

- bug fixes with a test that fails before and passes after
- handling a real cluster behaviour the state machine gets wrong — these are the
  most valuable reports, and a `kubectl describe pod` plus watchdog logs from
  the incident is usually enough to start
- documentation fixes, especially where a doc claims something the code no
  longer does
- examples for workload shapes not covered in [`examples/`](examples/)

**Likely to be declined, and why:**

- **A Helm chart or Kustomize base.** A sidecar has nothing to install on its
  own — see the reasoning in [`examples/README.md`](examples/README.md).
- **A mutating webhook injector.** It would require a cluster-scoped component
  with certificate management, trading away the property that makes this safe
  to adopt: no cluster-level state, blast radius of one pod.
- **CPU scaling.** Memory is where an overrun is fatal and immediate. CPU
  pressure throttles; it does not kill. The admission policy actively forbids
  this ServiceAccount from touching CPU.
- **New runtime dependencies.** The sidecar is deliberately one file over the
  standard library plus the Kubernetes client, so it stays at a constant ~70Mi
  resident next to your workload.
- **Making failures quieter.** Silent degradation is the failure mode this
  project exists to avoid: unreadable host stats refuse the scale-up and alert,
  rather than guessing.

## Style

Match the file you are editing. Two things are not negotiable:

1. **Comments explain *why*, not *what*.** The code is full of decisions that
   look arbitrary until you know the cluster behaviour behind them (why
   `inactive_file` is subtracted, why requests move with limits, why the
   rollback keeps requests high). Preserve that, and add to it when you
   introduce a new decision.
2. **Fail loudly.** New failure paths should surface as a metric, a K8s Event
   and a log line — never a silent `continue`.

Keep commits focused, and describe the behaviour change rather than the diff.

`main` is protected: changes land through pull requests, and both CI checks
(`test` and the multi-arch image build) have to pass. External pull requests
also need a maintainer approval before they can be merged.

## Reporting bugs

Use the [issue templates](.github/ISSUE_TEMPLATE). For anything
security-relevant, follow [SECURITY.md](SECURITY.md) instead of opening an
issue.
