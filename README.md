# k8s-oom-watchdog — Kubernetes 原地垂直扩缩容内存看门狗 Sidecar

[English](README.en.md) | [简体中文](README.md)

**通用**的容器内存看门狗 Sidecar，适用于任何长周期、内存尖刺型的工作负载（批处理、数据导入、报表聚合、Celery/RQ worker、ETL 任务等）：高频采样目标容器的 cgroup v2 **工作集内存**，在内核 OOM-kill 之前，通过 Kubernetes **原地垂直扩缩容（In-Place Pod Resize，`/resize` 子资源）** 抬高内存上限，任务结束后自动缩回，全程不重启容器、不打断长周期任务。

监督哪个容器由 `WATCHDOG_TARGET_CONTAINER` 环境变量指定（chart 参数 `targetContainer`），与业务技术栈完全解耦——本文与示例模板以 `heavy-worker`（一个 Celery heavy worker，本项目的起源场景）作为目标容器示例。

## 前提条件

| 条件 | 要求 | 不满足时的行为 |
|---|---|---|
| Kubernetes 版本 | **≥ 1.33**（EKS ≥ 1.34），`/resize` 子资源与原生 sidecar（initContainer `restartPolicy: Always`）均 GA | watchdog 首次 PATCH 收到 404/405 时打出明确 CRITICAL 日志并退出，反复重启触发 `MemoryWatchdogSidecarRestarting` 告警 |
| Cgroup | v2（systemd driver，EKS AL2023 默认） | 启动时定位 cgroup 失败并退出 |
| Pod Security | 命名空间需允许 hostPath 只读挂载（`/sys/fs/cgroup`、`/proc/meminfo`），PSA `restricted` 档位会拒绝 | Pod 无法创建 |
| 告警（监控栈） | 由监控栈统一承载：`deploy/monitoring/prometheus-rules.yaml`（6 条 PrometheusRule）+ `deploy/monitoring/alertmanager-config.yaml`（经告警网关送达 IM 告警群（示例通道名 your-alert-channel）），随监控栈发布生效 | 未部署规则则只有指标与 K8s 事件，无主动告警——启用 watchdog 前必须先确认规则已下发 |

## 工作机制

1. **定位 cgroup**：优先解析 `/proc/self/mountinfo` —— 仅在**主机 cgroup namespace** 下可用（root 字段携带宿主机绝对路径）；在**私有 cgroup namespace**（EKS 默认）下 root 字段是相对渲染（自身挂载为 `/`、hostPath 挂载为 `/../..` 链），watchdog 会识别并自动改走 **POD_UID glob 兜底**（实测约 0.3s，仅启动时执行一次）；主容器目录**优先用 pod status 里的 containerID 精确匹配**，">500Mi limit" 启发式仅作兜底。运行期间主容器若被重启（cgroup scope 目录随 containerID 更换），看门狗在下个采样周期**自动重新定位并继续监控**（指标 `watchdog_cgroup_relocated_total`，事件 `CgroupRelocated`），不会陷入长时间盲区。
2. **工作集口径**：每 100ms 读 `memory.current` 并**扣除 `memory.stat` 的 `inactive_file`**（可回收的 page cache），与 kubelet 的 OOM 记账口径一致，避免 IO 密集任务因缓存虚高触发误扩容。
3. **扩容（工作集 ≥ 80% limit）**：按步长（默认 2Gi，超出上限时截断）PATCH `/resize` 子资源，**requests 与 limits 同步抬升** —— 借用的内存全程纳入调度器/kubelet 记账：新 pod 无法被调度进这块空间；节点 allocatable 不足时 kubelet 判 `Infeasible`，由失败状态机处置。失败方向是"目标容器拿不到内存（可能 pod OOM）"，而**宿主机与邻居 pod 始终安全**。
4. **宿主机安全红线（公式二）**：`新 limit + 宿主机其他负载 < 90% × 物理内存` 才放行；空间不足时**自适应降级步长**（512Mi 颗粒度，能借多少借多少）；彻底没空间则熔断告警。PATCH 前带随机抖动**二次校验**，压缩同节点多看门狗的竞态窗口。
5. **失败处理**：PATCH 后监督 pod 的 `PodResizePending` condition —— `Infeasible` 或超时 60s 未生效 → **spec 回滚到实际生效值** + 高危告警 + 10 分钟扩容熔断。绝不无限等待。回滚时 requests **有意保持与 limits 相等**（此刻 pod 真实占着这么多内存，如实的 requests 让它在驱逐排序中垫底）；待工作集回落进低水位并满足缩容防抖后，由缩容机制补发一次 requests-only 恢复（事件 `RequestsRestored`，指标 `watchdog_requests_restored_total`），调度余量归还。
6. **缩容**：工作集 < 40% × **当前 limit** 且稳定 180s（且距上次扩容 ≥180s）→ 回落到 `max(baseline, 2×工作集)`（逐级，非一步到底）。与 80% 扩容线形成宽迟滞带，不会震荡。缩容 PATCH 前会**刷新 pod spec**——若发现外部 resize 在途则转入监督而非覆盖；缩容 PATCH 在途期间若工作集重新越过 80% 水位，**立即撤回缩容**（回写当前生效值，事件 `ScaleDownWithdrawn`），下个周期即可正常扩容，OOM 抢救不会被 pending 状态锁住。扩容路径同样对称处理：若刷新后发现 spec 低于当前生效 limit（自身重启前的缩容在途、或外部 actor 缩了 spec），**先收编监督而非从低值起算扩容**——否则算出的"扩容"目标可能低于当前生效值，等于在高压下反向缩容；收编后由撤回逻辑在下个周期完成抢救。所有 PATCH 目标值都会**规范化到 1Mi 对齐**，避免十进制配额（如 `1500M`）导致内核生效值与监督目标永不相等、误判超时回滚。
7. **Baseline 持久化**：首次启动把初始 limit 与初始 requests 分别写入 pod annotation `oom-watchdog.io/baseline-memory` / `oom-watchdog.io/baseline-requests`；sidecar 重启后从 annotation 恢复，不会把扩容后的值误认成 baseline。缩容回落到 baseline 时同时恢复初始 requests（借用结束，调度余量归还）。
8. **失效自省**：读不到宿主机内存信息时**拒绝扩容并持续高危告警**（绝不静默跳过）；主循环连续 100 次异常则退出交给 kubelet 重启。可通过 `WATCHDOG_ALLOW_BLIND_SCALEUP=true` 显式允许"盲扩半步长"。
9. **原生 sidecar 形态**：watchdog 以原生 sidecar（initContainer + `restartPolicy: Always`，排在 load-app-code 之后）注入——保证先于主容器启动、晚于主容器终止，主容器的整个生命周期（含 `terminationGracePeriodSeconds` 长达 6h 的优雅退出期）都处于监控之下；自身崩溃由 kubelet 按 Always 策略独立重启，不影响主容器。由于 sidecar 启动时主容器尚未创建（新节点拉主镜像可能需要数分钟），启动阶段对主容器 cgroup 的等待窗口为 300s，期间持续喂心跳保持 liveness 通过。容器以最小权限运行：非 root、只读根文件系统、drop 全部 capabilities、RuntimeDefault seccomp。
10. **可观测性**：`:8090/metrics` 暴露 Prometheus 指标（`watchdog_scale_up_total`、`watchdog_blocked_total{reason}`、`watchdog_working_set_bytes` 等），chart 会同时创建 headless Service + ServiceMonitor（`watchdog.serviceMonitor: true`，默认开），监控栈的 Prometheus 自动抓取入库；扩缩容触发/生效/失败/拦截均以 **K8s Event** 挂到 pod 上（`kubectl describe pod` 直接可见，同类事件 60s 去重）；`:8090/healthz` 由主循环心跳驱动（停跳 30s 返回 503），liveness 探针探它，主循环卡死时由 kubelet 自动重启。**告警由监控栈统一承载**（PrometheusRule `memory-watchdog-rules` 共 6 条：resize 失败、宿主机枯竭、spec 读取失败、到达 cap、sidecar 反复重启、自身内存贴线；经 Alertmanager → 告警网关送达 IM 告警群）——watchdog 只负责"做"（秒级闭环处置）与打点，不自己发通知，"报警器自己死了"的盲区由外部观测覆盖。代码中保留的飞书通知器为历史遗留，chart 不再注入凭证，处于永久静默状态。

## 已知限制

- **抢救窗口有物理上界（分配速率极限）**：可用抢救余量 = `(1 - threshold) × 当前 limit`（默认 20%，16Gi baseline 即 3.2Gi），而完整抢救链路耗时 = 采样周期（≤0.1s）+ PATCH RTT（约 0.1~0.5s）+ kubelet 实际应用 resize（通常 1~5s，节点繁忙时更久）。**持续分配速率超过"余量 ÷ 链路耗时"（16Gi baseline 下约 ≥1GiB/s）的突发，仍会在扩容生效前触发 OOM** —— 这是原地 resize 机制的固有上界，不是配置问题。对已知尖刺型任务应调低 `threshold`（换取更早触发）或直接抬高 baseline（requests），而不是指望 watchdog 追上任意速率的分配。
- **同节点多看门狗竞态**：各 Pod 的看门狗独立做公式二校验，极端情况下会同时申请扩容。由于 requests 同步抬升，**kubelet 的 allocatable 准入是权威且串行的第二道闸**——竞态的后果从"宿主机超卖"降级为"后到的 resize 被判 Deferred/Infeasible 并回滚"。公式二红线（90%）继续防御"邻居 pod 实际用量超出其 requests"这类 allocatable 记账覆盖不到的场景。缓解补充：PATCH 前抖动复核；chart 已默认为 heavy worker 配置按节点打散的软约束（`worker.heavyTopologySpreadConstraints`，maxSkew 1 / `ScheduleAnyway`），从源头降低多个 heavy pod 同节点竞争借用空间的概率。
- **RBAC 粒度**：Role 无法限定"只能改自己"，watchdog SA 名义上可 patch 命名空间内任意 pod。两层缓解：① SA token 通过 projected volume 只挂给 watchdog 容器（pod 级 `automountServiceAccountToken: false`）；② chart 随附 **ValidatingAdmissionPolicy**（`watchdog-vap.yaml`，开关 `watchdog.admissionGuard`，默认开），越权请求被 API server 直接 Deny。`failurePolicy: Fail`（fail-closed）是安全的：matchConditions 限定只影响该 SA，策略故障的最坏结果是 resize 被拒、触发 `MemoryWatchdogResizeFailed` 告警。七条校验及各自封堵的越权向量：

  | # | 校验 | 封堵的越权向量 |
  |---|---|---|
  | 1 | 目标 pod 必须带 `name=heavy-worker` 标签 | resize / annotate 命名空间内的邻居 pod |
  | 2 | labels 逐字段不可变 | 篡改 labels 把 pod 摘出/塞进 Service selector（流量劫持/摘除） |
  | 3 | annotation 仅允许两个 baseline 键增改，其余键不得改/不得删 | 篡改任意 pod 元数据 |
  | 4 | 主资源 UPDATE 时 `spec` 必须完全不变 | 借 `pods` patch 修改镜像等可变 spec 字段 |
  | 5 | resize 请求中必须恰好存在一个 heavy-worker 容器 | 畸形请求绕过后续容器级校验 |
  | 6 | resize 时仅 heavy-worker 容器的 resources 可变 | 缩小同 pod 其他容器（如 watchdog 自身）制造定向 OOM |
  | 7 | heavy-worker 的 cpu requests/limits 不可变 | 越权调整 CPU（watchdog 只被授权动 memory） |
- **Infeasible 概率**：requests 抬升会占用节点 allocatable，节点紧张时扩容请求会被 kubelet 拒绝（Infeasible）——这是设计内的失败方向（目标容器可能 OOM，宿主机安全），watchdog 会自动回滚 spec、告警并熔断 10 分钟。频繁出现说明节点容量真的不足，应扩节点或分流。
- **不触发节点自动扩容**：Infeasible 不产生 Pending 状态的 pod，cluster-autoscaler / Karpenter **不会**因此扩节点。节点容量不足只会体现为持续的 Infeasible / 宿主机熔断告警，必须人工扩节点或横向分流。
- **镜像仅支持 arm64**：基础镜像为 arch-specific 的 arm64 tag，与 heavy worker 所在的 ARM 节点组匹配。基础镜像为多架构官方镜像，需要 amd64 时在构建命令中调整 --platform 即可（见 Dockerfile 注释）。
- **sidecar 失效 = 失去弹性上探能力**：主容器按 `heavyResources` 原样启动（初始 limits 即 baseline），watchdog 负责在突发时把 limit 借升至 `maxMemoryFactor × baseline`。若 sidecar 持续不可用（集群版本不满足、hostPath 挂载损坏、CrashLoop），heavy worker 的内存上限就**停留在 baseline**（与不启用 watchdog 相同，不会更低），但突发任务失去 OOM 抢救。对应告警已随监控栈落地：`MemoryWatchdogSidecarRestarting`（重启率）、`MemoryWatchdogResizeFailed`、`MemoryWatchdogHostMemoryExhausted`、`MemoryWatchdogSpecReadErrors` 等 6 条规则见 `deploy/monitoring/prometheus-rules.yaml`。
- **借用期间 requests 会临时抬升**：resize 时 requests 与 limits 同步写成同一值（借用内存必须纳入调度记账，这是宿主机安全的第一道闸）；**回落到 baseline 时自动恢复初始 requests**（初始值持久化在 annotation `oom-watchdog.io/baseline-requests`），超卖形态（requests << limits）的调度余量在借用结束后完整归还。仅逐级缩容的中间档位（仍高于 baseline，即仍在借用）保持 requests = limits。**扩容失败回滚是一个特殊档位**：limit 回到 baseline 但 requests 保持抬高（如实反映真实占用），此时即使 limit 已在 baseline，缩容检查仍会运行——工作集稳定低于 40% 水位并满足 180s 防抖后，自动补发 requests-only 恢复；sidecar 重启也不丢这笔账（启动时从 pod spec 与 annotation 对比得知 requests 仍被抬高）。注意：在**没有** baseline-requests annotation 的存量 pod 上升级 sidecar，watchdog 会把当时 spec 里的 requests（可能已被此前版本抬高过）当作初始值——重建 pod 即可归零。
- **主容器不再挂载 SA token**：启用 watchdog 会在 pod 级设置 `automountServiceAccountToken: false`（token 仅通过 projected volume 挂给 watchdog 容器）。业务代码目前不调用 K8s API，若未来引入此类依赖需重新评估。
- **watchdog 自身内存画像与 exec 纪律**：sidecar 常驻约 70Mi（Python + kubernetes 客户端导入足迹为主），**不随目标容器内存规模或时间增长**，100Mi limit 实测余量充足。但**严禁在 watchdog 容器内 exec 会 import kubernetes 包的诊断脚本**——第二个解释器会再吃 60~80Mi，瞬间挤爆整个容器触发 OOMKilled；需要容器内诊断时只用轻量 stdlib 脚本，或直接看日志/指标。自身内存告警已落地（`MemoryWatchdogSelfMemoryHigh`：working set / limit > 90% 持续 10 分钟）。

## 部署参数（`values.yaml`）

```yaml
worker:
  watchdog:
    enabled: false           # 默认关闭；开启前确认集群版本满足前提
    threshold: 0.8           # 扩容水位（工作集/limit）
    pollInterval: 0.1        # 采样周期（秒）
    memoryStep: "2Gi"        # 扩容步长
    maxMemoryFactor: 2.0     # 扩容上限 = 该倍数 × baseline（各租户自动适配），须 > 1.0
    hostCeiling: 0.90        # 宿主机物理内存红线（公式二）
    allowBlindScaleup: false # 读不到宿主机内存时是否允许盲扩半步长
    metricsPort: 8090        # /metrics 与 /healthz 端口
    repository: "registry.example.com/memory-watchdog"
    tag: "v1.8"
```

告警不在 chart 内配置——由监控栈统一下发（见"前提条件"表）。

扩容上限**只有倍数一种模式**：`cap = maxMemoryFactor × baseline`（baseline = 部署时的 `heavyResources.limits.memory`，主容器按其原样启动；默认倍数 2.0）。同一份 chart 参数自动适配不同规格的租户（4Gi 档封顶 8Gi、16Gi 档封顶 32Gi），个别租户需要不同上限时在租户 values 覆盖倍数即可。刻意**不提供绝对值上限**——运行期实际能借到多少由宿主机红线（公式二）和 kubelet allocatable 准入约束。`maxMemoryFactor ≤ 1` 会被钳制为 1（cap = baseline，watchdog 无法动作，通过 `BLOCKED_MAX_LIMIT` 告警暴露）。

## 构建与发布

镜像推送到你的私有镜像仓库，arm64 架构：

```bash
docker buildx build --platform linux/arm64 \
  -t registry.example.com/memory-watchdog:<tag> \
  --push .
```

镜像内通过 pip 安装了固定版本的 `kubernetes` 客户端（resize 子资源方法需要 ≥33 版本，旧版本走 raw API 兜底路径）。

## 压测演练

压测脚本随本目录提供（`trigger_oom_test.py`），先拷进 `heavy-worker` 容器再运行（目标 24G、每 1s 增长 100M、保持 30s 后释放）：

```bash
kubectl cp trigger_oom_test.py \
  <namespace>/<pod-name>:/tmp/trigger_oom_test.py -c heavy-worker
kubectl exec -it <pod-name> -c heavy-worker -n <namespace> -- \
  python3 /tmp/trigger_oom_test.py 24 100 1 30
```

注意：若业务镜像的 Python 不在 exec 的 PATH 中（如 uv 管理的 venv），需使用解释器的完整路径。

默认参数的分配速率（100MiB/s）远低于抢救窗口的物理上界（见"已知限制"第一条），验证的是正常路径。若要探测极限行为，可把速率提到上界附近（如 `24 2048 0.5 30` ≈ 4GiB/s）——此时部分场景在扩容生效前 OOM 属于设计内结果，不是缺陷。

另开终端观察：

```bash
kubectl logs -f <pod-name> -c watchdog -n <namespace>
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.status.conditions}'  # 观察 resize conditions
curl <pod-ip>:8090/metrics                                                    # 指标
```

## 失败模式处置

| 现象 | 含义 | 处置 |
|---|---|---|
| 告警 `MemoryWatchdogResizeFailed` | 节点容量不足，kubelet 拒绝（Infeasible）或超时未应用 | 扩容节点或横向分流；watchdog 已自动回滚 spec 并熔断 10 分钟 |
| 日志 CRITICAL "看门狗失去宿主机视野"（指标 `watchdog_blocked_total{reason="no_host_stats"}`） | `/host/proc/meminfo` 挂载异常 | 检查 hostPath 挂载与节点状态；此状态下不会执行任何扩容 |
| 告警 `MemoryWatchdogHostMemoryExhausted` | 节点整体拥塞，无安全空间 | 扩容节点；这是设计内的保护行为 |
| 告警 `MemoryWatchdogScaleUpBlockedAtCap` | 内存压力持续但已达 cap（factor × baseline） | 若为常态，上调该租户 baseline 或 maxMemoryFactor |
| 告警 `MemoryWatchdogSidecarRestarting` | sidecar 反复重启（前提不满足 / 端口占用 / 主循环持续异常 / 自身 OOM） | 看容器日志 CRITICAL 行；期间 heavy worker 上限停留在 baseline，无 OOM 抢救 |
| 告警 `MemoryWatchdogSpecReadErrors` | API Server 不可达，扩容决策被阻塞 | 检查 API Server / 网络；此状态下 OOM 抢救无法执行 |
| 告警 `MemoryWatchdogSelfMemoryHigh` | watchdog 自身内存贴近 100Mi limit | 排查是否有人 exec 了重型诊断进程；包升级抬高基线则提 limit 至 128Mi |
| watchdog 容器自身 OOMKilled（`last_terminated_reason` 指标可查） | 常驻 ~70Mi 是常数，OOMKilled 几乎必是有人 exec 了重型诊断进程，或依赖包升级抬高了基线 | 排查是否有人在容器内 exec 过 import kubernetes 的脚本（禁止）；若是包升级导致基线抬升，将 limit 提至 128Mi 并复核内存曲线 |

## 单元测试

测试直接 import 生产模块（无需集群、无需 kubernetes 包），覆盖单位换算、扩缩容决策（含公式二拦截、自适应降级、盲扩开关、迟滞防震荡）、cgroup 解析与容器定位，以及**主循环状态机**（pending 监督、Infeasible/超时回滚、熔断、缩容撤回、cgroup 重定位、外部 resize 收编——通过注入 fake 时钟/API/文件读取驱动 `Watchdog` 类）：

```bash
python3 test_watchdog.py
```

## License

[Apache License 2.0](LICENSE)
