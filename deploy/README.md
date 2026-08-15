# 部署参考文件说明

本目录展示 watchdog 的完整部署形态。文件取自一个生产级 Helm chart 与监控栈配置，
已做通用化处理（应用名、镜像仓库、告警通道等均为占位符）：

## helm/

Helm 模板而非可直接 apply 的清单——其中引用了源 chart 的模板助手
（`app.worker.fullname`、`app.namespace` 等）与 values 键，
移植到你的 chart 时需替换这些引用。

| 文件 | 作用 |
|---|---|
| `deployment_h.yaml` | heavy worker 的 Deployment：watchdog 以原生 sidecar（initContainer + `restartPolicy: Always`）注入，含 resizePolicy、只读 hostPath 挂载、projected SA token |
| `rbac.yaml` | watchdog 专用 ServiceAccount + Role（pods/pods-resize get+patch、events create）+ RoleBinding |
| `watchdog-vap.yaml` | ValidatingAdmissionPolicy：七条 CEL 校验把该 SA 的写权限收窄到"仅目标容器的 memory resize + 两个 baseline annotation" |
| `watchdog-metrics.yaml` | headless Service + ServiceMonitor，供 Prometheus 抓取 `:8090/metrics` |
| `values-example.yaml` | chart values 中 watchdog 参数块示例 |

## monitoring/

按 kube-prometheus-stack 的 CRD 编写；`${environment}` 是模板变量（源环境经
Terraform `templatefile` 渲染），直接使用时需自行替换：

| 文件 | 作用 |
|---|---|
| `prometheus-rules.yaml` | 6 条 PrometheusRule：resize 失败、宿主机枯竭、spec 读取失败、到达 cap、sidecar 反复重启、自身内存贴线 |
| `alertmanager-config.yaml` | AlertmanagerConfig：MemoryWatchdog.* 告警路由到 IM 告警通道（示例为 Opsgenie 风格 webhook） |
