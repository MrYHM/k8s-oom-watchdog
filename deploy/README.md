# 监控栈配套文件

本目录只放**随监控栈下发**的告警配置——它们不属于工作负载，通常由平台团队统一发布。

工作负载侧的接入清单（sidecar 注入、RBAC、ValidatingAdmissionPolicy、ServiceMonitor）
见 [`../examples/`](../examples)：那里是可直接 apply 的完整 YAML。

## monitoring/

| 文件 | 作用 |
|---|---|
| `prometheus-rules.yaml` | PrometheusRule：7 条告警（resize 失败、宿主机枯竭、宿主机视野丢失、spec 读取失败、到达 cap、sidecar 反复重启、自身内存贴线），每条带 runbook_url |
| `alertmanager-config.yaml` | AlertmanagerConfig：`MemoryWatchdog.*` 的路由与聚合——按 alertname+namespace 分组、critical 每小时重复（其余 6h）、开启恢复通知。接收方是一个 webhook 占位符，需替换为你自己的告警去向 |

两份都是可直接 apply 的对象，按需替换命名空间过滤与接收方配置：

```bash
kubectl apply -f monitoring/prometheus-rules.yaml
kubectl apply -f monitoring/alertmanager-config.yaml -n <monitoring-ns>
```

未部署这些规则时，watchdog 仍然照常工作并暴露指标与 K8s 事件，但**没有主动告警**——
启用 watchdog 前应先确认规则已下发（见主 README 的前提条件表）。
