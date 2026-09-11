# Demo: an in-place OOM rescue in under a minute

A self-contained demo — one Deployment, its own namespace and RBAC — that
drives a container past the 80% memory watermark and shows the watchdog
raising the limit **in place**, without a restart.

Deliberately small so the whole rescue is visible in seconds:

| | |
|---|---|
| baseline (target container limit) | 256Mi |
| scale-up step | 192Mi |
| cap (`maxMemoryFactor` 4 x baseline) | 1Gi |
| stress profile | grow to ~660Mi at ~46MiB/s, hold 6s, release |

The stress rate is deliberately well below what the rescue path can absorb.
At ~80MiB/s a 192Mi step buys under 2.5s, which is inside the detect + PATCH +
kubelet-apply latency, and the container starts losing the race — a real
demonstration of the [rescue window's upper bound](../docs/design.md#known-limitations-with-reasoning),
but not what you want in a demo.

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

If your cluster cannot pull from ghcr.io, push the image to a registry it can
reach and point `IMAGE` at it (`PULL_POLICY=IfNotPresent` for images loaded
straight into the node):

```bash
IMAGE=<your-registry>/oom-watchdog:demo ./setup.sh
```

On a cluster with mixed node architectures, a single-arch image also needs
`ARCH` so the pod cannot land on a node it was never built for:

```bash
ARCH=arm64 IMAGE=<your-registry>/oom-watchdog:demo ./setup.sh
```

`demo.sh` prints one line per second from the watchdog's own `/metrics`, so
the working set is the same number its decisions use (page cache already
subtracted):

```
TIME      WORKING SET   REQUESTS        LIMIT  USED%  RESTARTS  LAST EVENT
11:34:15        166Mi      256Mi     256Mi      65%         0  Started
11:34:17        246Mi      448Mi     448Mi <<   55%         0  ResizeCompleted
11:34:20        406Mi      640Mi     640Mi <<   64%         0  ResizeCompleted
11:34:22        486Mi      832Mi     832Mi <<   76%         0  ResizeStarted
11:34:26        647Mi        1Gi       1Gi <<   78%         0  ResizeStarted
```

`REQUESTS` and `LIMIT` move together on purpose: borrowed memory has to be
visible to scheduler accounting, which is what keeps the node and its
neighbours safe. `RESTARTS` stays at 0 throughout — the point of resizing in
place — and the run ends by printing the `kubectl` command that reads that
counter straight from the pod status, so the number is reproducible rather
than something the demo asserts.

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

Both READMEs already embed `demo/demo.gif`, so re-recording is just a matter
of replacing the file and committing it.

Keep it under ~5MB so GitHub renders it inline; a higher `SPEED` argument to
`render_gif.py`, `agg --speed 2`, or a shorter `DURATION=20 ./demo.sh` all
help.

## What to point out when sharing it

- The `LIMIT` column climbs while the container keeps running — `restarts: 0`
  at the end is the whole point.
- `USED%` drops right after each resize: that is the rescue headroom coming
  back before the kernel OOM-killer can fire.
- Every step is also a K8s Event on the pod (`LAST EVENT`), so operators see
  the same story in `kubectl describe pod`.
