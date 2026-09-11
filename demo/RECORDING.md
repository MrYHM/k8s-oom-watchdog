# Recording the README GIF — checklist

The GIF is the single highest-leverage asset in the README: it is the only
thing that survives being skimmed, and it is what gets reposted. Budget an
hour for the first attempt, mostly for cluster access and the trial run.

Target: **15–25 seconds**, **under 5MB**, showing three moments — memory
crossing the 80% watermark, the limit jumping while the container keeps
running, and `restarts: 0` at the end.

---

## 1. Pick the cluster

- [ ] **Kubernetes >= 1.33** (`kubectl version`). The `pods/resize`
      subresource does not exist before that; `setup.sh` refuses to continue.
- [ ] A node with **~2GiB of free memory** — the demo borrows up to 1Gi and
      the host red line needs headroom above that.
- [ ] A namespace allowing **read-only hostPath mounts**. The manifest creates
      `watchdog-demo` with no PSA label for this reason; if your cluster
      enforces `restricted` cluster-wide, label the namespace explicitly:
      `kubectl label ns watchdog-demo pod-security.kubernetes.io/enforce=baseline`.

Which cluster:

| Option | Verdict |
|---|---|
| Managed (EKS/GKE/AKS) or kubeadm/k3s node | **Reliable.** Real node cgroups and a real `/proc/meminfo`. |
| k3s on a small cloud VM | **Cheapest reliable option** if you don't want to use a work cluster for a personal project. |
| kind / minikube | **Try, but don't count on it.** Nodes run inside containers: the cgroup hierarchy is nested (the watchdog falls back to its POD_UID glob) and `/proc/meminfo` reflects the VM, so the host red line is computed against VM memory. Verify with the trial run in step 4 before planning to record here. |

## 2. Make the image reachable from the cluster

`demo/watchdog-demo.yaml` pulls `ghcr.io/mryhm/k8s-oom-watchdog:edge`.

> **While the GitHub repo is private, the GHCR package is private too.**
> Do **not** flip the package to public to make this easier — that publishes
> the code before your employer IP approval lands. Use a pull secret instead.

Pick one:

- [ ] **Private GHCR + imagePullSecret** (recommended, nothing else changes).
      Create a classic PAT with `read:packages`, then:
      ```bash
      kubectl create secret docker-registry ghcr \
        --docker-server=ghcr.io \
        --docker-username=MrYHM \
        --docker-password=<PAT with read:packages> \
        -n watchdog-demo
      kubectl patch serviceaccount watchdog -n watchdog-demo \
        -p '{"imagePullSecrets":[{"name":"ghcr"}]}'
      ```
      Order matters: apply the manifest first (it creates the namespace and
      SA), then create the secret and patch, then delete the pod so it is
      rescheduled with the secret.
- [ ] **Build it yourself into a registry the nodes can already reach** — no
      manifest edit needed, pass `IMAGE` to `setup.sh`. For ECR:
      ```bash
      REGION=<region>; ACCOUNT=<account-id>
      REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com   # add .cn in AWS China
      aws ecr create-repository --repository-name oom-watchdog --region $REGION
      aws ecr get-login-password --region $REGION \
        | docker login --username AWS --password-stdin $REGISTRY
      # Match your node architecture; --platform can list both.
      docker buildx build --platform linux/arm64 \
        -t $REGISTRY/oom-watchdog:demo --push .
      # ARCH is required when the image is single-arch and the cluster has
      # mixed nodes, or the pod may land where the image cannot run.
      ARCH=arm64 IMAGE=$REGISTRY/oom-watchdog:demo ./setup.sh
      ```
- [ ] **kind only**: `docker build -t oom-watchdog:demo . && kind load
      docker-image oom-watchdog:demo`, then set `image: oom-watchdog:demo` and
      `imagePullPolicy: IfNotPresent`.

## 3. Install the tools

- [ ] `kubectl`, and `curl` on the machine you record from (`demo.sh` reads
      metrics through a port-forward).
- [ ] **[vhs](https://github.com/charmbracelet/vhs)** — `brew install vhs`.
      Declarative and repeatable; this is the recommended path.
- [ ] *Or* **asciinema + [agg](https://github.com/asciinema/agg)** —
      `brew install asciinema agg`. More control, hand-timed.
- [ ] Optional: `gifsicle` (`brew install gifsicle`) to shrink the result.

## 4. Trial run — do this before recording

```bash
cd demo
./setup.sh          # deploys, waits for the sidecar to find the target cgroup
./demo.sh           # one full rescue, ~45s
```

Check all of these before you bother recording:

- [ ] `setup.sh` ends by printing watchdog log lines, not a timeout.
- [ ] The watchdog log says it located the target cgroup — no
      `Could not locate the pod cgroup slice` (that is the kind/minikube
      failure mode).
- [ ] `demo.sh`'s table has a non-empty `LIMIT` column (empty means the
      port-forward or `curl` failed).
- [ ] `USED%` climbs past 80% and `LIMIT` **increases** at least twice.
- [ ] The run ends with `restarts: 0`.
- [ ] `kubectl describe pod -n watchdog-demo` shows `ScaleUpTriggered` /
      `ResizeApplied` events.

If the limit never moves, read `kubectl logs -n watchdog-demo <pod> -c
watchdog` — every refusal is logged with its reason (host red line, cap,
missing host stats).

## 5. Record

> **Reset before every take.** A previous run leaves the limit at the 1Gi cap
> and scale-down needs 180s of idle, so a second recording would show no
> scale-up at all. Recreate the pod first:
> `kubectl -n watchdog-demo delete pod -l app=bursty-worker --wait=true && ./setup.sh`

**vhs (recommended, when it works)**

```bash
cd demo
vhs demo.tape       # -> demo.gif
```

The tape is already set to 1300x720, font size 16, `PlaybackSpeed 1.5`,
`Sleep 55s`. Adjust:

- [ ] Raise `Sleep` if your cluster applies resizes slowly (it must outlast
      `demo.sh`, which runs `DURATION` seconds — 45 by default).
- [ ] Shorten the whole thing with `DURATION=30 ./demo.sh` inside the tape's
      `Type` line, or raise `PlaybackSpeed` to 2.
- [ ] Keep `Width`/`Height` at a 16:9-ish ratio; GitHub scales the GIF to the
      README column width, so anything narrower than ~1100px gets blurry.

**asciinema (alternative)**

```bash
cd demo
asciinema rec demo.cast -c ./demo.sh
agg --speed 1.5 --font-size 16 --theme asciinema demo.cast demo.gif
```

`demo/*.cast` is gitignored; only the rendered GIF gets committed.

**render_gif.py (fallback, no browser or ffmpeg)**

vhs drives a headless Chromium that go-rod downloads on first use. Where that
download cannot reach its host, **vhs exits 0 and writes nothing** — no error,
no partial file, and ffmpeg is never even invoked (`~/.cache/rod/browser/`
staying empty is the tell). Capture the run as text and render it instead:

```bash
pip install pillow
./demo.sh | while IFS= read -r l; do \
    printf '%s\t%s\n' "$(python3 -c 'import time;print(time.time())')" "$l"; \
  done > frames.tsv
python3 render_gif.py frames.tsv demo.gif 2.0
```

Frames follow the real timing of the run, so this is a replay of the actual
output rather than a re-enactment — the same principle `agg` uses. It also
produces a far smaller file (~150KB against several MB for a screen capture).

## 6. Quality gate

- [ ] **Size under 5MB** — `ls -lh demo.gif`. If over: `agg --speed 2`, a
      shorter `DURATION`, or
      `gifsicle -O3 --lossy=60 --colors 128 demo.gif -o demo.gif`.
- [ ] **Readable at README width** — open the GIF and shrink the window to
      ~700px wide. If the columns blur, raise the font size and re-record
      rather than upscaling.
- [ ] **The three moments are visible**: 80% crossing, `LIMIT` jump,
      `restarts: 0`. If the limit jump is easy to miss, that is worth fixing
      in `demo.sh` (marking changed values) before re-recording.
- [ ] **No leaked identifiers** — pod names, namespace and cluster context all
      appear on screen. The demo uses its own namespace, but check the shell
      prompt: a prompt showing a work cluster context or internal hostname
      ends up in the GIF forever. Record with a clean prompt
      (`PS1='$ '` inside the tape's shell, or a fresh terminal profile).
- [ ] **Dark background** — renders acceptably in both GitHub themes; a light
      terminal on GitHub's dark theme glares.

## 7. Ship it

- [ ] Put the GIF at `demo/demo.gif`. Both READMEs already embed that path,
      so replacing the file is all it takes.
- [ ] Commit it and check the rendering on github.com — a GIF that fails to
      inline usually means it exceeded the size cap.
- [ ] Tear the demo down: `kubectl delete -f watchdog-demo.yaml`, and delete
      the `ghcr` pull secret if you created it in a shared cluster.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ImagePullBackOff` | Private GHCR package, no pull secret | Step 2 |
| `no matching manifest for linux/amd64` | Single-arch image on a mixed-architecture cluster | `ARCH=arm64 ./setup.sh`, or rebuild with both platforms |
| Pod stuck `Init:0/1` | Sidecar crash-looping | `kubectl logs <pod> -c watchdog` — CRITICAL line says which prerequisite failed |
| `Could not locate the pod cgroup slice` | Nested cgroups (kind/minikube), or cgroup v1 | Use a real node; confirm cgroup v2 with `stat -fc %T /sys/fs/cgroup` (expect `cgroup2fs`) |
| `LIMIT` column empty | Port-forward or curl failed | Check `curl -s localhost:18090/metrics`; the port-forward runs in the background of `demo.sh` |
| `resize is not available on this cluster` | Cluster below 1.33 | Step 1 |
| Limit never rises, log says host red line | Node too full for a 128Mi step | Free memory on the node, or pick a bigger one |
| Limit rises then rolls back (`Infeasible`) | Node allocatable exhausted | Same — the by-design failure direction |
| Resize applied but the container restarted | `resizePolicy` missing | The manifest sets `restartPolicy: NotRequired` for memory; don't drop it |
| GIF over 5MB | Too long, or too many colors | `gifsicle -O3 --lossy=60 --colors 128`, or `DURATION=30` |
