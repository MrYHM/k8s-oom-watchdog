# Monitoring stack files

Alerting configuration only. These objects ship with your **monitoring stack**,
not with the workload — typically rolled out by whoever operates Prometheus,
separately from whoever deploys the watchdog.

For the workload side — sidecar injection, RBAC, ValidatingAdmissionPolicy,
ServiceMonitor — see [`../examples/`](../examples): complete manifests that
apply as-is.

## monitoring/

| File | What it is |
|---|---|
| `prometheus-rules.yaml` | PrometheusRule with 7 alerts: resize failure, host memory exhausted, host stats unreadable, pod spec unreadable, scale-up cap reached, sidecar restarting, watchdog's own memory high. Each carries a `runbook_url`. |
| `alertmanager-config.yaml` | AlertmanagerConfig: routing and grouping for `MemoryWatchdog.*` — grouped per alert per namespace, criticals repeat hourly (others every 6h), resolved notifications on. **The receiver is a webhook placeholder you replace with your own destination.** |

Both apply directly:

```bash
kubectl apply -f monitoring/prometheus-rules.yaml
kubectl apply -f monitoring/alertmanager-config.yaml -n <monitoring-namespace>
```

Two things to adjust when adopting them:

- **The receiver** in `alertmanager-config.yaml`. It is deliberately a
  credential-free webhook; swap in `opsgenieConfigs`, `pagerdutyConfigs`,
  `slackConfigs` or whatever your stack uses. The file's header comment notes
  the two details worth carrying over.
- **`runbook_url`** in `prometheus-rules.yaml`, if you keep your own fork of
  the docs.

The rules are unscoped by namespace on purpose: the watchdog metrics only exist
where the sidecar runs, so installing cluster-wide is safe. Add a `namespace=~"..."`
matcher to every `expr` if you want them limited.

## Without these rules

The watchdog still works and still exposes metrics and K8s Events — but nothing
alerts. That matters more than it sounds: an `Infeasible` resize never creates a
Pending pod, so **no other part of Kubernetes surfaces it**. Confirm the rules
are deployed before enabling the watchdog (see the Prerequisites table in the
[main README](../README.md)).
