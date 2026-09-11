# Examples

Complete, applyable manifests. Each file runs as-is once you replace the
placeholders, so you can get the pattern working first and adapt it second.

There is no Helm chart or Kustomize base here on purpose: a sidecar has
nothing to install on its own — it lives inside *your* workload, and the
useful artifact is a manifest you can read and copy rather than a template
that hides the details. The same reason [git-sync](https://github.com/kubernetes/git-sync/blob/master/docs/kubernetes.md)
and [cloud-sql-proxy](https://github.com/GoogleCloudPlatform/cloud-sql-proxy/tree/main/examples/k8s-sidecar)
ship examples instead of charts.

| File | What it is | Needed? |
|---|---|---|
| [`rbac.yaml`](rbac.yaml) | ServiceAccount + Role + RoleBinding | **Yes** — apply once per namespace, before the workload |
| [`deployment-with-sidecar.yaml`](deployment-with-sidecar.yaml) | The watchdog injected into a Deployment, every piece annotated | **Start here** |
| [`job-with-sidecar.yaml`](job-with-sidecar.yaml) | The same for a Job — batch work, where an OOM kill throws away hours | If you run batch jobs |
| [`admission-policy.yaml`](admission-policy.yaml) | ValidatingAdmissionPolicy narrowing what the SA can write | Recommended |
| [`servicemonitor.yaml`](servicemonitor.yaml) | Headless Service + ServiceMonitor for Prometheus | If you run prometheus-operator |

Alerting rules live separately in [`../deploy/monitoring/`](../deploy/monitoring)
— they ship with your monitoring stack, not with the workload.

## Placeholders

Every file uses the same two, so a find-and-replace is enough:

| Placeholder | Meaning |
|---|---|
| `my-namespace` | Your namespace |
| `my-worker` / `my-job` | Your workload **and** its main container name — they must match, because the sidecar supervises a container by name |

```bash
sed -e 's/my-namespace/prod/g' -e 's/my-worker/report-builder/g' \
  rbac.yaml deployment-with-sidecar.yaml | kubectl apply -f -
```

## The four things people get wrong

1. **`resizePolicy` with `restartPolicy: NotRequired` on the target container.**
   Without it the kubelet restarts the container on every resize, which
   defeats the whole point.
2. **`restartPolicy: Always` on the watchdog initContainer.** That flag is
   what makes it a *native sidecar*. Without it, the pod waits for the
   watchdog to exit before starting your workload — and it never exits.
3. **`WATCHDOG_TARGET_CONTAINER` must equal the main container's name.** It
   is how the watchdog finds the cgroup to sample.
4. **Grant events through `events.k8s.io`, not just the core group.** The
   client writes through the newer Events API; granting only `""` leaves
   every scale event rejected with 403 and the pod's event log empty.

## Verifying

```bash
kubectl logs <pod> -c watchdog        # startup: cgroup located, baseline recorded, cap resolved
kubectl describe pod <pod>            # scale actions appear as events
kubectl port-forward <pod> 8090:8090 && curl localhost:8090/metrics
```

Prerequisites, tuning and failure modes are covered in the
[main README](../README.md); for a demo you can run end to end in one command,
see [`../demo/`](../demo).
