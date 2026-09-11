# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this is

A single-purpose sidecar: it samples a target container's cgroup v2 working set
every 100ms and raises the memory limit through the Kubernetes `pods/resize`
subresource before the kernel OOM-killer fires, then gives the memory back.
`watchdog.py` is the whole program — one file, standard library plus the
Kubernetes client.

Read [README.md](README.md) for behaviour and
[docs/design.md](docs/design.md) for why each mechanism is shaped the way it
is. This file covers the conventions those documents do not state.

## Layout

| Path | Role |
|---|---|
| `watchdog.py` | The sidecar. Decision logic lives in pure functions (`decide_scale_up` / `decide_scale_down`); the control loop is the `Watchdog` class, whose clock, API and file access all enter through injectable seams. |
| `test_watchdog.py` | Unit tests. **Standard library only** — see the constraint below. |
| `examples/` | Complete applyable manifests, the primary adoption path. Placeholders are `my-namespace` and `my-worker`. |
| `demo/` | One-command end-to-end demo plus the README GIF and its renderer. |
| `deploy/monitoring/` | PrometheusRule + AlertmanagerConfig. Ships with a monitoring stack, not with the workload. |
| `docs/` | Design deep-dive, bilingual. |

## Hard constraints

**Tests need nothing installed.** `python3 test_watchdog.py` must keep working
on a bare interpreter: no cluster, no `kubernetes` package, no pytest, no
fixtures. Every dependency enters through an injectable seam, so if a change
cannot be tested this way, the seam is usually in the wrong place. Do not
introduce a test framework to make a change testable.

**Bilingual docs come in pairs.** Four files, two pairs:

- `README.md` ↔ `README.zh-CN.md`
- `docs/design.md` ↔ `docs/design.zh-CN.md`

Changing behaviour means editing both halves of any pair you touch. This is
the single easiest thing to get wrong here. English is the primary language;
Chinese is a full translation, not a summary.

**Fail loudly.** New failure paths surface as a metric, a K8s Event and a log
line — never a silent `continue`. Silent degradation is the failure mode this
project exists to avoid: when host memory stats are unreadable, the watchdog
refuses to scale up and alerts rather than guessing.

**Comments explain why, not what.** The code is full of decisions that look
arbitrary until you know the cluster behaviour behind them: why `inactive_file`
is subtracted, why requests move in lockstep with limits, why a failed
scale-up's rollback deliberately keeps requests high. Preserve that, and add to
it when you introduce a new decision.

## Values that must stay in step

Changing one of these without the other leaves the repo self-contradictory:

| Value | Lives in |
|---|---|
| `kubernetes==33.1.0` | `Dockerfile` **and** `pyproject.toml` |
| `hostCeiling` default `0.90` | `watchdog.py` (dataclass default **and** the env fallback), `examples/*.yaml`, both READMEs — `TestConfigDefaults` guards this pair, so a drift fails the tests |
| Alert rule count (currently **7**) | `deploy/monitoring/prometheus-rules.yaml` and four prose mentions across both READMEs and both design docs |
| VAP check count (currently **7**) | `examples/admission-policy.yaml` and the table in both design docs |
| Target container placeholder `my-worker` | `examples/`, both READMEs, both design docs |
| Python `3.12` | `Dockerfile`, `pyproject.toml`, `.github/workflows/publish-image.yml` |

## What not to build

[CONTRIBUTING.md](CONTRIBUTING.md#what-makes-a-good-change-here) has the full
list with reasoning. The four that come up most:

- **No Helm chart or Kustomize base.** A sidecar has nothing to install on its
  own. `examples/` is the answer; `examples/README.md` explains it, citing
  git-sync and cloud-sql-proxy as precedent.
- **No mutating webhook injector.** It would need a cluster-scoped component
  with certificate management, trading away the property that makes this safe
  to adopt: no cluster-level state, blast radius of one pod.
- **No CPU scaling.** Memory overruns kill; CPU pressure throttles. The
  admission policy actively forbids this ServiceAccount from touching CPU.
- **No new runtime dependencies.** The sidecar stays at a constant ~70Mi
  resident next to the workload because it is one file over the standard
  library.

## Working on the demo

`demo/setup.sh` and `demo/demo.sh` are the real thing, run against a real
cluster — treat them as code, not scripts.

- **State persists between runs.** A completed run leaves the limit at the cap,
  and scale-down needs 180s of idle, so a second run shows no scale-up at all.
  Recreate the pod first: `kubectl -n watchdog-demo delete pod -l
  app=bursty-worker --wait=true && ./setup.sh`.
- **Never a bare `wait`.** The port-forward and the stress job never exit, so
  `wait` without PIDs hangs the loop forever. Wait on specific PIDs.
- **Selecting the pod by label also returns terminating pods.** During a
  rollout that makes every later step fail with NotFound. Both scripts filter
  on `deletionTimestamp` absent and `phase == Running`.
- **The stress rate must stay below what the rescue path absorbs.** At ~80MiB/s
  a 192Mi step buys under 2.5s, inside the detect + PATCH + kubelet-apply
  latency, and the container gets OOM-killed — a faithful demonstration of the
  documented upper bound, but not what a demo should show.
- **vhs does not work everywhere.** It drives a headless Chromium that go-rod
  downloads on first use; where that download fails, vhs exits 0 having
  written nothing. `demo/render_gif.py` is the browser-free fallback. See
  [demo/RECORDING.md](demo/RECORDING.md).

## Before committing

```bash
python3 test_watchdog.py                          # 63 tests, no deps, < 1s
kubectl apply --dry-run=server -f examples/       # if you touched examples/
```

CI runs the tests and a multi-arch image build on every push and PR. Editing
anything under `.github/workflows/` requires a token with the `workflow` scope.

Never commit local operational details — registry hostnames, account IDs,
cluster names, namespaces from a real environment. `examples/` and `demo/` use
neutral placeholders throughout; keep it that way.
