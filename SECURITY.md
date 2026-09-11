# Security Policy

## Reporting a vulnerability

Report privately through GitHub's
[**Report a vulnerability**](https://github.com/MrYHM/k8s-oom-watchdog/security/advisories/new)
form (Security → Advisories). It opens a channel visible only to you and the
maintainer.

**Please do not open a public issue for a vulnerability.** This sidecar holds a
token that can patch pods in its namespace, so a disclosure gap has real
consequences for anyone running it.

What helps:

- what an attacker can achieve, and what access they need to start
- Kubernetes version, watchdog image tag, and whether
  [`examples/admission-policy.yaml`](examples/admission-policy.yaml) is applied
- a manifest or command sequence that reproduces it

Expect an acknowledgement within a few days. This is a personal project with no
paid support: fixes are best-effort, and I will tell you plainly if something
will not be fixed rather than leave it open indefinitely.

## Supported versions

The latest release receives fixes. Older tags do not — there is no backporting.
Pin a version for reproducibility, but plan on upgrading to get fixes.

## Threat model

Worth reading before deploying, because the design deliberately accepts one
risk and mitigates it in two layers.

**What the watchdog can do.** It needs `get` + `patch` on `pods` and
`pods/resize` in its namespace. Kubernetes RBAC **cannot** express "only your
own pod" or "only these fields", so the token is nominally able to resize any
pod in the namespace and to write pod annotations.

**Two mitigations, both shipped:**

1. **The token is not in the workload.** The pod sets
   `automountServiceAccountToken: false` and the token reaches only the
   watchdog container, through a projected volume. A compromise of your
   application container does not hand over this token.
2. **Admission-time narrowing.** [`examples/admission-policy.yaml`](examples/admission-policy.yaml)
   is a ValidatingAdmissionPolicy with seven CEL checks that pin every write by
   this ServiceAccount to: the labelled target pod only, labels immutable, two
   baseline annotations only, no spec changes outside the resize subresource,
   exactly one target container, only that container's resources, and memory
   only — never CPU. **Applying it is strongly recommended**;
   [docs/design.md](docs/design.md#known-limitations-with-reasoning) lists the
   abuse vector each check blocks.

**What it reads from the host.** Two read-only hostPath mounts:
`/sys/fs/cgroup` (to sample the target container's working set) and
`/proc/meminfo` (for the host red-line check). Both are world-readable, so the
container runs as non-root, read-only root filesystem, all capabilities
dropped, `RuntimeDefault` seccomp. It writes nothing to the host. Note that
hostPath mounts are themselves a privilege: a namespace under the PSA
`restricted` profile will — correctly — refuse to run this pod.

**What it can do to your workload.** In the worst case, raise the target
container's memory limit up to `maxMemoryFactor × baseline`, bounded further by
the host red line and the kubelet's allocatable admission. It cannot change
images, commands, CPU, or any other container. If the sidecar dies, the
workload keeps its baseline limit — never less.

**Out of scope.** The watchdog does not authenticate callers on `:8090`: that
port serves Prometheus metrics and `/healthz` in the clear, on the pod network.
Treat the metrics as non-sensitive (memory figures and counters) and restrict
network access if your threat model requires it.
