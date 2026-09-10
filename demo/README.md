# Demo: an in-place OOM rescue in under a minute

A self-contained demo — one Deployment, its own namespace and RBAC — that
drives a container past the 80% memory watermark and shows the watchdog
raising the limit **in place**, without a restart.

Deliberately small so the whole rescue is visible in seconds:

| | |
|---|---|
| baseline (target container limit) | 256Mi |
| scale-up step | 128Mi |
| cap (`maxMemoryFactor` 4 x baseline) | 1Gi |
| stress profile | grow to ~700Mi at ~80MiB/s, hold 20s, release |

## Prerequisites

- **Kubernetes >= 1.33** (`setup.sh` checks and refuses otherwise).
- A namespace that allows **read-only hostPath mounts** — the manifest creates
  `watchdog-demo` without a restricted PSA label for exactly this reason.
- The image `ghcr.io/mryhm/k8s-oom-watchdog:edge`, built from master by the
  `publish-image` workflow. **While the GitHub repo is private the GHCR
  package is private too**, so the cluster needs an imagePullSecret — see
  [RECORDING.md](RECORDING.md#2-make-the-image-reachable-from-the-cluster) for
  that and for the build-it-yourself alternatives.
- Managed clusters (EKS/GKE/AKS) and kubeadm nodes are the reliable targets.
  kind/minikube run nodes inside containers, so the cgroup hierarchy is
  nested and `/proc/meminfo` reflects the VM rather than the node — the
  watchdog may need its glob fallback and the host red line will be computed
  against the VM's memory.

## Run it

```bash
./setup.sh    # deploy + wait for the sidecar to locate the target cgroup
./demo.sh     # drive one rescue, printing a live status table
```

`demo.sh` prints one line per second from the watchdog's own `/metrics`, so
the working set is the same number its decisions use (page cache already
subtracted):

```
TIME       WORKING SET        LIMIT   USED%  LAST EVENT
17:04:12         118Mi        256Mi     46%  -
17:04:16         207Mi        256Mi     81%  ScaleUpTriggered
17:04:18         223Mi        384Mi     58%  ResizeApplied
17:04:24         341Mi        512Mi     67%  ResizeApplied
...
restarts: 0
```

Tear down with `kubectl delete -f watchdog-demo.yaml`.

## Record the GIF

**[RECORDING.md](RECORDING.md) is the full checklist** — cluster choice, making
the private image reachable, a trial run to verify before recording, size and
quality gates, and a troubleshooting table. The short version:

**With [vhs](https://github.com/charmbracelet/vhs)** (declarative, repeatable):

```bash
./setup.sh
vhs demo.tape        # -> demo.gif
```

**With asciinema** (more control, needs
[agg](https://github.com/asciinema/agg) to convert):

```bash
asciinema rec demo.cast -c ./demo.sh
agg --speed 1.5 --font-size 16 demo.cast demo.gif
```

Then commit the GIF and reference it from the top of the main README:

```markdown
![In-place OOM rescue](demo/demo.gif)
```

Keep it under ~5MB so GitHub renders it inline; `agg --speed 2` or a shorter
`DURATION=30 ./demo.sh` both help.

## What to point out when sharing it

- The `LIMIT` column climbs while the container keeps running — `restarts: 0`
  at the end is the whole point.
- `USED%` drops right after each resize: that is the rescue headroom coming
  back before the kernel OOM-killer can fire.
- Every step is also a K8s Event on the pod (`LAST EVENT`), so operators see
  the same story in `kubectl describe pod`.
