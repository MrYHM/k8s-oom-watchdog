#!/usr/bin/env python3
# Copyright 2026 HY
# SPDX-License-Identifier: Apache-2.0
"""In-place vertical scaling OOM watchdog sidecar.

Runs next to the heavy-worker container, samples its cgroup v2 memory
working set at high frequency and uses the Kubernetes in-place pod resize
subresource (K8s >= 1.33 / EKS >= 1.34) to raise the container memory limit
before the kernel OOM-killer fires, then lowers it back once the burst is
over.

Design decisions (see README.md for rationale):
- ``requests`` and ``limits`` are patched together so the borrowed memory is
  always visible to scheduler accounting: no new pod can be scheduled into
  the headroom, and when node allocatable is insufficient the kubelet marks
  the resize Infeasible -- the pod may OOM but the host and its neighbours
  stay safe, which is the preferred failure direction. The host-ceiling
  check guards against *actual usage* overcommit on top of that.
- All decision logic lives in pure functions (``decide_scale_up`` /
  ``decide_scale_down``) operating on a ``Sample`` snapshot, and the control
  loop itself is the ``Watchdog`` class whose clock/API/filesystem access all
  enter through injectable seams -- both layers are unit testable without a
  cluster.
- The watchdog must fail loudly: missing host stats, Infeasible resizes and
  persistent loop errors all raise alerts instead of being silently skipped.
"""

import dataclasses
import glob
import http.server
import json
import logging
import os
import queue
import random
import signal
import sys
import threading
import time
import urllib.request
from typing import Dict, Optional, Tuple

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.client.exceptions import ApiException
except ImportError:  # unit tests exercise the pure functions without the package
    k8s_client = None
    k8s_config = None

    class ApiException(Exception):  # type: ignore[no-redef]
        status = 0
        body = ""

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout,
)
logger = logging.getLogger("watchdog")

# Name of the container this sidecar supervises. Set per deployment;
# the watchdog is workload-agnostic (Celery heavy workers were merely
# the original use case).
TARGET_CONTAINER = os.environ.get("WATCHDOG_TARGET_CONTAINER", "main")
BASELINE_ANNOTATION = "oom-watchdog.io/baseline-memory"
BASELINE_REQUESTS_ANNOTATION = "oom-watchdog.io/baseline-requests"
HOST_CGROUP_ROOT = "/host/sys/fs/cgroup"

GIB = 1024 ** 3
MIB = 1024 ** 2
KIB = 1024


# ---------------------------------------------------------------------------
# Signal handling (python3 runs as PID 1 in the sidecar and would otherwise
# ignore SIGTERM, leaving the pod stuck in Terminating)
# ---------------------------------------------------------------------------
def _handle_exit(signum: int, frame) -> None:
    logger.info("Received termination signal (%s). Exiting watchdog gracefully...", signum)
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_exit)
signal.signal(signal.SIGINT, _handle_exit)


# ---------------------------------------------------------------------------
# Unit conversion (pure)
# ---------------------------------------------------------------------------
# Order matters: two-character binary suffixes must be tried before their
# one-character decimal counterparts ("Gi" before "G").
_MEMORY_SUFFIXES: Tuple[Tuple[str, int], ...] = (
    ("Ki", 1024), ("Mi", 1024 ** 2), ("Gi", 1024 ** 3), ("Ti", 1024 ** 4),
    ("k", 1000), ("K", 1000), ("M", 1000 ** 2), ("G", 1000 ** 3), ("T", 1000 ** 4),
)


def parse_memory_to_bytes(mem_str: str) -> int:
    """Parse a Kubernetes memory quantity into bytes.

    Follows K8s semantics: Ki/Mi/Gi/Ti are binary, K/M/G/T are decimal.
    Supports fractional values such as "1.5Gi".
    """
    s = str(mem_str).strip()
    if not s:
        raise ValueError("empty memory quantity")
    for suffix, factor in _MEMORY_SUFFIXES:
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * factor)
    return int(float(s))


def bytes_to_k8s_str(num_bytes: int) -> str:
    """Render bytes as a K8s quantity, using binary suffixes only.

    Never emits decimal suffixes ("K" == 1000 in K8s semantics), so the value
    round-trips exactly through the API server.
    """
    if num_bytes % GIB == 0:
        return f"{num_bytes // GIB}Gi"
    if num_bytes % MIB == 0:
        return f"{num_bytes // MIB}Mi"
    return f"{num_bytes // KIB}Ki"


def align_up(num_bytes: int, step: int) -> int:
    return ((num_bytes + step - 1) // step) * step


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Config:
    threshold: float = 0.8            # scale-up watermark (fraction of limit)
    poll_interval: float = 0.1        # cgroup sampling period, seconds
    step_bytes: int = 4 * GIB         # preferred scale-up step
    max_limit_bytes: int = 48 * GIB   # resolved cap; see resolve_max_limit()
    max_factor: float = 2.0           # cap = factor x baseline
    host_ceiling: float = 0.85        # host physical memory red line
    min_step_bytes: int = 512 * MIB   # granularity / minimum worthwhile step
    scale_down_ratio: float = 0.4     # scale-down watermark (fraction of limit)
    scale_down_cooldown: float = 180.0
    pending_timeout: float = 60.0     # give up waiting for kubelet after this
    circuit_cooldown: float = 600.0   # pause scale-ups after a failed resize
    allow_blind_scaleup: bool = False  # scale up without host stats (half step)
    metrics_port: int = 8090
    max_consecutive_errors: int = 100


def load_config() -> Config:
    def _flag(name: str, default: str = "false") -> bool:
        return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")

    return Config(
        threshold=float(os.environ.get("WATCHDOG_THRESHOLD", "0.8")),
        poll_interval=float(os.environ.get("WATCHDOG_INTERVAL", "0.1")),
        step_bytes=parse_memory_to_bytes(os.environ.get("WATCHDOG_STEP", "4Gi")),
        max_factor=float(os.environ.get("WATCHDOG_MAX_FACTOR", "2.0") or 2.0),
        host_ceiling=float(os.environ.get("WATCHDOG_HOST_CEILING", "0.85")),
        allow_blind_scaleup=_flag("WATCHDOG_ALLOW_BLIND_SCALEUP"),
        metrics_port=int(os.environ.get("WATCHDOG_METRICS_PORT", "8090")),
    )


def resolve_max_limit(cfg: Config, baseline: int) -> int:
    """Resolve the scale-up cap once the baseline is known.

    The cap is always ``WATCHDOG_MAX_FACTOR`` x the pod's own baseline, so
    one chart-wide parameter adapts to every tenant's limits tier; there is
    deliberately no absolute cap (runtime borrowing is already bounded by
    the host ceiling and kubelet allocatable admission). A factor below 1
    would place the cap under the starting limit (the watchdog could then
    never act) -- clamped to 1.0 with a warning; the resulting inertness
    still surfaces through BLOCKED_MAX_LIMIT alerts.
    """
    factor = cfg.max_factor
    if factor < 1.0:
        logger.warning("WATCHDOG_MAX_FACTOR %.2f is below 1.0; clamping to 1.0 "
                       "(cap = baseline, scale-ups will be blocked).", factor)
        factor = 1.0
    return max(baseline, int(baseline * factor) // MIB * MIB)


# ---------------------------------------------------------------------------
# Decision layer (pure, unit-testable)
# ---------------------------------------------------------------------------
class Action:
    NOOP = "noop"
    SCALE_UP = "scale_up"
    SCALE_UP_ADAPTIVE = "scale_up_adaptive"
    BLOCKED_MAX_LIMIT = "blocked_max_limit"
    BLOCKED_HOST_FULL = "blocked_host_full"
    BLOCKED_NO_HOST_STATS = "blocked_no_host_stats"
    SCALE_DOWN = "scale_down"


@dataclasses.dataclass
class Sample:
    """One consistent snapshot of everything a decision needs."""
    working_set: int                  # memory.current - inactive_file
    cgroup_max: int                   # limit currently enforced by the kernel
    spec_limit: int                   # limit currently declared in pod spec
    baseline: int                     # original limit to return to
    host_total: Optional[int]         # None means /proc/meminfo unreadable
    host_available: Optional[int]
    # spec requests still above the pod's original requests while the limit
    # already sits at baseline (a failed scale-up rolled back with honest,
    # inflated requests) -- the scale-down machinery must restore them.
    requests_inflated: bool = False


@dataclasses.dataclass
class Decision:
    action: str
    target_bytes: int = 0
    reason: str = ""


def decide_scale_up(s: Sample, cfg: Config) -> Decision:
    """Decide whether (and how far) to raise the memory limit.

    Host safety invariant: new_limit + memory used by everything
    else on the host must stay below host_ceiling * host_total, i.e. even if
    the container consumes its whole new limit the host cannot be driven into
    global OOM by this decision alone.
    """
    if s.cgroup_max <= 0 or s.working_set < int(s.cgroup_max * cfg.threshold):
        return Decision(Action.NOOP)

    if s.spec_limit >= cfg.max_limit_bytes:
        return Decision(
            Action.BLOCKED_MAX_LIMIT,
            reason=f"spec limit {bytes_to_k8s_str(s.spec_limit)} already at cap "
                   f"{bytes_to_k8s_str(cfg.max_limit_bytes)}",
        )

    proposed = min(s.spec_limit + cfg.step_bytes, cfg.max_limit_bytes)

    if s.host_total is None or s.host_available is None:
        # Never decide blindly by default: without host stats the safety
        # invariant cannot be evaluated, and silence here would defeat the
        # watchdog's purpose. The caller must alert on this decision.
        if not cfg.allow_blind_scaleup:
            return Decision(
                Action.BLOCKED_NO_HOST_STATS,
                reason="host memory statistics unavailable; refusing unguarded scale-up",
            )
        half_step = max(
            cfg.min_step_bytes,
            (cfg.step_bytes // 2) // cfg.min_step_bytes * cfg.min_step_bytes,
        )
        return Decision(
            Action.SCALE_UP_ADAPTIVE,
            target_bytes=min(s.spec_limit + half_step, cfg.max_limit_bytes),
            reason="host stats unavailable; blind half-step scale-up (explicitly enabled)",
        )

    host_used = max(0, s.host_total - s.host_available)
    other_used = max(0, host_used - s.working_set)
    ceiling = int(s.host_total * cfg.host_ceiling)

    if proposed + other_used <= ceiling:
        return Decision(Action.SCALE_UP, target_bytes=proposed)

    # Adaptive step-down: grab whatever room is still safely available.
    max_safe_step = ceiling - s.spec_limit - other_used
    if max_safe_step >= cfg.min_step_bytes:
        adaptive = max_safe_step // cfg.min_step_bytes * cfg.min_step_bytes
        target = min(s.spec_limit + adaptive, cfg.max_limit_bytes)
        if target > s.spec_limit:
            return Decision(
                Action.SCALE_UP_ADAPTIVE,
                target_bytes=target,
                reason=f"full step would breach the {int(cfg.host_ceiling * 100)}% host "
                       f"ceiling; degraded to {bytes_to_k8s_str(target - s.spec_limit)}",
            )

    return Decision(
        Action.BLOCKED_HOST_FULL,
        reason=f"host exhausted: even {bytes_to_k8s_str(cfg.min_step_bytes)} more would "
               f"breach the {int(cfg.host_ceiling * 100)}% ceiling "
               f"(total={bytes_to_k8s_str(s.host_total)}, "
               f"available={bytes_to_k8s_str(s.host_available)})",
    )


def decide_scale_down(s: Sample, cfg: Config) -> Decision:
    """Decide whether to lower the limit and to what value.

    Triggers on working set relative to the *current* limit (not baseline), so
    an oversized limit is reclaimed even when steady-state usage sits above
    baseline. Steps down to max(baseline, 2x working set) instead of jumping
    straight to baseline; together with the 80% scale-up threshold this gives
    a wide hysteresis band, so the two paths cannot oscillate.
    Timing (debounce) is the caller's responsibility.
    """
    if s.cgroup_max <= s.baseline:
        # Limit already home, but a failed scale-up may have rolled back with
        # requests still equal to the limit (honest while the memory was
        # genuinely in use). Once the working set clears the same low
        # watermark, emit a scale-down to the baseline itself: the limit is
        # unchanged and do_patch restores the original requests.
        if s.requests_inflated and s.working_set < int(s.cgroup_max * cfg.scale_down_ratio):
            return Decision(Action.SCALE_DOWN, target_bytes=s.baseline,
                            reason="restore-requests")
        return Decision(Action.NOOP)
    if s.working_set >= int(s.cgroup_max * cfg.scale_down_ratio):
        return Decision(Action.NOOP)
    target = max(s.baseline, align_up(s.working_set * 2, cfg.min_step_bytes))
    if target >= s.cgroup_max:
        return Decision(Action.NOOP)
    return Decision(Action.SCALE_DOWN, target_bytes=target)


# ---------------------------------------------------------------------------
# cgroup v2 access (pure-ish: filesystem only, unit-testable with a tempdir)
# ---------------------------------------------------------------------------
def read_cgroup_memory(cgroup_dir: str) -> Tuple[int, Optional[int]]:
    """Return (working_set_bytes, limit_bytes or None when unlimited).

    Working set excludes ``inactive_file`` (reclaimable page cache), matching
    the kubelet's OOM accounting. Raw ``memory.current`` would count file
    cache and cause spurious scale-ups for IO-heavy tasks.
    """
    with open(os.path.join(cgroup_dir, "memory.current"), "r") as f:
        current = int(f.read().strip())

    inactive_file = 0
    try:
        with open(os.path.join(cgroup_dir, "memory.stat"), "r") as f:
            for line in f:
                if line.startswith("inactive_file "):
                    inactive_file = int(line.split()[1])
                    break
    except OSError:
        pass  # stat file missing: fall back to raw current (conservative)

    with open(os.path.join(cgroup_dir, "memory.max"), "r") as f:
        max_str = f.read().strip()
    limit = None if max_str == "max" else int(max_str)

    return max(0, current - inactive_file), limit


def find_pod_cgroup_dir(pod_uid: Optional[str], host_root: str = HOST_CGROUP_ROOT,
                        mountinfo_path: str = "/proc/self/mountinfo") -> Optional[str]:
    """Locate this pod's cgroup slice as seen from the host mount.

    Fast path: parse mountinfo. It only works in a HOST cgroup namespace,
    where the mount entry's "root" field (4th column) carries the real
    host-relative path of this container's cgroup (systemd driver layout:
    kubepods.slice/.../cri-containerd-<id>.scope) -- its parent directory is
    then the pod slice. Under a PRIVATE cgroup namespace (default on our EKS
    nodes) the root field is rendered relative to the container's own cgroup:
    "/" for its own mount, a "/../.." chain for the /host/sys/fs/cgroup
    hostPath mount. Neither carries usable path information, so only the
    entry whose mountpoint is exactly /sys/fs/cgroup is considered and any
    relative root is rejected -- the pod-UID glob fallback handles those
    clusters.
    """
    try:
        with open(mountinfo_path, "r") as f:
            for line in f:
                parts = line.strip().split()
                # Fields: id parent major:minor root mountpoint ...
                if len(parts) < 5 or "cgroup2" not in line:
                    continue
                root_field, mount_point = parts[3], parts[4]
                if mount_point != "/sys/fs/cgroup":
                    continue  # e.g. our own /host/sys/fs/cgroup hostPath mount
                if root_field == "/" or ".." in root_field:
                    logger.info("Private cgroup namespace detected (mountinfo root %r); "
                                "using the pod-UID glob fallback.", root_field)
                    break
                parent = os.path.dirname(root_field).lstrip("/")
                candidate = os.path.join(host_root, parent)
                if os.path.isdir(candidate):
                    logger.info("Fast-path matched pod cgroup via mountinfo: %s", candidate)
                    return candidate
                break
    except OSError as e:
        logger.debug("Failed parsing mountinfo: %s", e)

    if pod_uid:
        logger.info("Falling back to glob scanning for pod UID under %s ...", host_root)
        uid_normalized = pod_uid.replace("-", "_")
        for pattern in (f"**/*{uid_normalized}*", f"**/*{pod_uid}*"):
            for match in glob.glob(os.path.join(host_root, pattern), recursive=True):
                if os.path.isdir(match) and match.endswith(".slice"):
                    return match
    return None


def find_target_cgroup_dir(
    pod_slice_abs: str,
    container_id: Optional[str] = None,
    min_limit_bytes: int = 500 * MIB,
) -> Optional[str]:
    """Locate the target container's cgroup directory inside the pod slice.

    Preferred: exact match on the container ID taken from pod status (robust
    against additional sidecars). Fallback heuristic: the only container with
    a memory limit above ``min_limit_bytes`` (the watchdog itself is 200Mi).
    """
    try:
        entries = sorted(os.listdir(pod_slice_abs))
    except OSError:
        return None
    dirs = [e for e in entries if os.path.isdir(os.path.join(pod_slice_abs, e))]

    if container_id:
        cid = container_id.rsplit("://", 1)[-1]
        for entry in dirs:
            if cid and cid in entry:
                return os.path.join(pod_slice_abs, entry)

    for entry in dirs:
        mem_max_path = os.path.join(pod_slice_abs, entry, "memory.max")
        if not os.path.exists(mem_max_path):
            continue
        try:
            with open(mem_max_path, "r") as f:
                max_val = f.read().strip()
            if max_val != "max" and int(max_val) > min_limit_bytes:
                return os.path.join(pod_slice_abs, entry)
        except (OSError, ValueError) as e:
            logger.debug("Error parsing memory.max in %s: %s", entry, e)
    return None


def get_host_memory_stats(
    meminfo_path: str = "/host/proc/meminfo",
) -> Tuple[Optional[int], Optional[int]]:
    """Return (total_bytes, available_bytes); (None, None) when unreadable."""
    path = meminfo_path if os.path.exists(meminfo_path) else "/proc/meminfo"
    total = available = None
    try:
        with open(path, "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                if total is not None and available is not None:
                    break
    except OSError as e:
        logger.warning("Error reading host memory info from %s: %s", path, e)
        return None, None
    return total, available


# ---------------------------------------------------------------------------
# Kubernetes API wrapper
# ---------------------------------------------------------------------------
class PodApi:
    # Explicit per-call timeout: the kubernetes client default is no timeout,
    # and a wedged TCP connection would otherwise hang the monitoring loop.
    REQUEST_TIMEOUT = 5.0

    # kubectl-describe events repeat fast in blocked loops; one per reason
    # per minute keeps the pod's event list readable.
    EVENT_COOLDOWN = 60.0

    def __init__(self, v1, pod_name: str, namespace: str, events_v1=None,
                 pod_uid: Optional[str] = None) -> None:
        self.v1 = v1
        self.events_v1 = events_v1
        self.pod_name = pod_name
        self.namespace = namespace
        self.pod_uid = pod_uid
        self._event_last: Dict[str, float] = {}

    def get_pod(self):
        return self.v1.read_namespaced_pod(
            name=self.pod_name, namespace=self.namespace,
            _request_timeout=self.REQUEST_TIMEOUT,
        )

    @staticmethod
    def get_container_limit(pod_obj) -> Optional[str]:
        if not pod_obj or not pod_obj.spec:
            return None
        for container in pod_obj.spec.containers:
            if container.name == TARGET_CONTAINER:
                if container.resources and container.resources.limits:
                    return container.resources.limits.get("memory")
        return None

    @staticmethod
    def get_container_requests(pod_obj) -> Optional[str]:
        if not pod_obj or not pod_obj.spec:
            return None
        for container in pod_obj.spec.containers:
            if container.name == TARGET_CONTAINER:
                if container.resources and container.resources.requests:
                    return container.resources.requests.get("memory")
        return None

    @staticmethod
    def get_container_id(pod_obj) -> Optional[str]:
        statuses = getattr(pod_obj.status, "container_statuses", None) or []
        for status in statuses:
            if status.name == TARGET_CONTAINER:
                return status.container_id
        return None

    @staticmethod
    def get_resize_pending(pod_obj) -> Tuple[Optional[str], str]:
        """Return (reason, message) of the PodResizePending condition, if any."""
        for cond in getattr(pod_obj.status, "conditions", None) or []:
            if cond.type == "PodResizePending":
                return cond.reason or "", cond.message or ""
        return None, ""

    def patch_limits(self, new_memory_str: str, new_requests_str: Optional[str] = None):
        """PATCH the /resize subresource, moving requests together with limits.

        Raising requests reserves the borrowed memory in scheduler/kubelet
        accounting, so other pods cannot be packed into it. When node
        allocatable is insufficient the kubelet reports Infeasible, which the
        main loop handles (rollback + alert + circuit breaker) -- the
        deliberate failure direction: the pod may starve, the host never.

        ``new_requests_str`` defaults to the limit (requests == limits while
        borrowing); the scale-down-to-baseline path passes the pod's original
        requests instead, so oversubscribed tenants (requests << limits) get
        their scheduling headroom back once the borrow is over.
        """
        body = {
            "spec": {
                "containers": [
                    {
                        "name": TARGET_CONTAINER,
                        "resources": {
                            "limits": {"memory": new_memory_str},
                            "requests": {"memory": new_requests_str or new_memory_str},
                        },
                    }
                ]
            }
        }
        # kubernetes>=33 generates a dedicated method for the resize
        # subresource; keep a raw call_api fallback for older clients where
        # patch_namespaced_pod() has no subresource support at all.
        fn = getattr(self.v1, "patch_namespaced_pod_resize", None)
        if fn is not None:
            return fn(name=self.pod_name, namespace=self.namespace, body=body,
                      _request_timeout=self.REQUEST_TIMEOUT)
        return self.v1.api_client.call_api(
            "/api/v1/namespaces/{namespace}/pods/{name}/resize",
            "PATCH",
            {"name": self.pod_name, "namespace": self.namespace},
            [],
            {"Content-Type": "application/strategic-merge-patch+json"},
            body=body,
            response_type="object",
            auth_settings=["BearerToken"],
            _request_timeout=self.REQUEST_TIMEOUT,
        )

    def annotate(self, key: str, value: str) -> None:
        self.v1.patch_namespaced_pod(
            name=self.pod_name,
            namespace=self.namespace,
            body={"metadata": {"annotations": {key: value}}},
            _request_timeout=self.REQUEST_TIMEOUT,
        )

    def emit_event(self, reason: str, message: str, event_type: str = "Normal") -> None:
        """Attach a K8s Event to this pod so `kubectl describe pod` shows the
        resize history. Uses events.k8s.io/v1 (core v1 Events are deprecated).
        Best-effort: failures are logged, never raised."""
        if self.events_v1 is None:
            return
        now = time.monotonic()
        if now - self._event_last.get(reason, float("-inf")) < self.EVENT_COOLDOWN:
            return
        self._event_last[reason] = now
        # events.k8s.io/v1 requires a MicroTime (exactly 6 fractional digits).
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"
        regarding = {
            "apiVersion": "v1",
            "kind": "Pod",
            "name": self.pod_name,
            "namespace": self.namespace,
            "fieldPath": f"spec.containers{{{TARGET_CONTAINER}}}",
        }
        if self.pod_uid:
            regarding["uid"] = self.pod_uid
        body = {
            # Trailing DASH on purpose: the API server validates generateName
            # as a DNS-1123 subdomain and masks only a trailing dash
            # (deployment-style "foo-"); a trailing dot is rejected with 422.
            "metadata": {"generateName": f"{self.pod_name}-watchdog-"},
            "eventTime": timestamp,
            "action": "Resize",
            "reason": reason,
            "note": message[:1000],
            "type": event_type,
            "regarding": regarding,
            "reportingController": "oom-watchdog.io/k8s-oom-watchdog",
            "reportingInstance": self.pod_name[:128],
        }
        try:
            self.events_v1.create_namespaced_event(
                namespace=self.namespace, body=body,
                _request_timeout=self.REQUEST_TIMEOUT,
            )
        except Exception as e:
            # WARNING, not DEBUG: a validation error here means the whole
            # event trail silently disappears (it did once, see the
            # generateName comment above) -- that must be visible in logs.
            logger.warning("Could not emit K8s event %s: %s", reason, e)


# ---------------------------------------------------------------------------
# Feishu notifier (dedicated thread; never blocks the monitoring loop)
# ---------------------------------------------------------------------------
class FeishuNotifier(threading.Thread):
    HTTP_TIMEOUT = 5.0
    TOKEN_REFRESH_MARGIN = 300.0

    def __init__(self, cooldown_seconds: float = 300.0) -> None:
        super().__init__(name="feishu-notifier", daemon=True)
        self.app_id = os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        self.chat_id = os.environ.get("FEISHU_CHAT_ID", "")
        self.enabled = bool(self.app_id and self.app_secret and self.chat_id)
        self.pod_name = os.environ.get("POD_NAME", "unknown-pod")
        self.pod_namespace = os.environ.get("POD_NAMESPACE", "unknown-ns")
        self.cooldown_seconds = cooldown_seconds
        self._queue: "queue.Queue[Tuple[str, str, str]]" = queue.Queue(maxsize=100)
        self._last_sent: Dict[str, float] = {}
        self._token: Optional[str] = None
        self._token_expiry = 0.0
        if not self.enabled:
            logger.info("Feishu credentials not fully configured; notifications disabled.")

    def notify(self, event_key: str, title: str, color: str, markdown: str) -> None:
        """Enqueue a card; same event_key is rate-limited to one per cooldown."""
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._last_sent.get(event_key, float("-inf")) < self.cooldown_seconds:
            logger.debug("Feishu event '%s' suppressed by cooldown.", event_key)
            return
        try:
            self._queue.put_nowait((title, color, markdown))
        except queue.Full:
            logger.warning("Feishu notification queue full; dropping event '%s'.", event_key)
            return
        # Recorded only after a successful enqueue: a dropped event must not
        # consume the cooldown window and silence its retry.
        self._last_sent[event_key] = now

    def run(self) -> None:
        while True:
            title, color, markdown = self._queue.get()
            try:
                self._send_card(title, color, markdown)
            except Exception as e:  # notifier must never kill the process
                logger.warning("Error sending Feishu card '%s': %s", title, e)

    def _post_json(self, url: str, payload: dict, token: Optional[str] = None) -> dict:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        # Default (verified) TLS context on purpose: the payload carries app
        # credentials, never disable certificate validation here.
        with urllib.request.urlopen(req, timeout=self.HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get_token(self) -> Optional[str]:
        if self._token and time.monotonic() < self._token_expiry - self.TOKEN_REFRESH_MARGIN:
            return self._token
        res = self._post_json(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret},
        )
        if res.get("code") != 0:
            logger.warning("Failed to fetch Feishu tenant_access_token: %s", res)
            return None
        self._token = res.get("tenant_access_token")
        self._token_expiry = time.monotonic() + float(res.get("expire", 3600))
        return self._token

    def _send_card(self, title: str, color: str, markdown: str) -> None:
        token = self._get_token()
        if not token:
            return
        annotated = (
            f"**命名空间:** `{self.pod_namespace}`\n"
            f"**Pod 实例:** `{self.pod_name}`\n"
            f"**时间:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"----------------------------------------\n"
            f"{markdown}"
        )
        card = {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": annotated}}],
        }
        res = self._post_json(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            {"receive_id": self.chat_id, "msg_type": "interactive", "content": json.dumps(card)},
            token=token,
        )
        if res.get("code") != 0:
            logger.warning("Failed to send Feishu interactive card: %s", res)


# ---------------------------------------------------------------------------
# Prometheus metrics + liveness endpoint (stdlib-only)
# ---------------------------------------------------------------------------
class Heartbeat:
    """Fed by the main loop; lets /healthz detect a stuck loop.

    The metrics HTTP server runs in its own thread and would answer probes
    even with the main loop wedged -- liveness must therefore check this
    heartbeat, not mere TCP reachability.
    """

    # Largest legitimate gap between beats is the 10s blocked-path sleep;
    # 30s leaves ample margin without masking a real hang for long.
    STALE_AFTER = 30.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def beat(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    def healthy(self) -> bool:
        with self._lock:
            return time.monotonic() - self._last < self.STALE_AFTER


# Exposition metadata (# HELP / # TYPE) keyed by metric family name; families
# not listed here render as untyped.
_METRIC_META: Dict[str, Tuple[str, str]] = {
    "watchdog_scale_up_total": ("counter", "In-place scale-up requests accepted by the API server"),
    "watchdog_scale_down_total": ("counter", "In-place scale-down requests accepted by the API server"),
    "watchdog_scale_down_withdrawn_total": ("counter", "In-flight scale-downs withdrawn because memory pressure returned"),
    "watchdog_resize_failed_total": ("counter", "Resize attempts that failed, by stage (patch/kubelet)"),
    "watchdog_blocked_total": ("counter", "Scale-up decisions blocked, by reason"),
    "watchdog_host_stats_errors_total": ("counter", "Failures reading host memory statistics"),
    "watchdog_spec_read_errors_total": ("counter", "Pod spec reads that failed while memory pressure was critical"),
    "watchdog_cgroup_relocated_total": ("counter", "Target cgroup relocations after a container restart"),
    "watchdog_working_set_bytes": ("gauge", "Target container working set (memory.current - inactive_file)"),
    "watchdog_memory_limit_bytes": ("gauge", "Memory limit currently enforced by the kernel"),
    "watchdog_baseline_bytes": ("gauge", "Baseline memory limit the watchdog returns to"),
}


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = {}
        self._gauges: Dict[str, float] = {}

    @staticmethod
    def _key(name: str, labels: Optional[Dict[str, str]]) -> str:
        if not labels:
            return name
        rendered = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{rendered}}}"

    def inc(self, name: str, labels: Optional[Dict[str, str]] = None, amount: float = 1) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def render(self) -> str:
        with self._lock:
            samples = sorted(self._counters.items()) + sorted(self._gauges.items())
        families: Dict[str, list] = {}
        for key, value in samples:
            families.setdefault(key.split("{", 1)[0], []).append((key, value))
        lines = []
        for family in sorted(families):
            mtype, help_text = _METRIC_META.get(family, ("untyped", ""))
            if help_text:
                lines.append(f"# HELP {family} {help_text}")
            lines.append(f"# TYPE {family} {mtype}")
            lines += [f"{k} {v}" for k, v in families[family]]
        return "\n".join(lines) + "\n"


def start_metrics_server(metrics: Metrics, port: int, heartbeat: Optional[Heartbeat] = None) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path == "/healthz":
                alive = heartbeat.healthy() if heartbeat else True
                body = b"ok" if alive else b"main loop stalled"
                self.send_response(200 if alive else 503)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            body = metrics.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:  # silence access logs
            pass

    # A failed bind must propagate: the liveness probe depends on /healthz,
    # so a silently missing server means a kubelet restart loop with no
    # explanation in the logs. main() turns this into fatal().
    server = http.server.ThreadingHTTPServer(("", port), Handler)
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    logger.info("Metrics endpoint listening on :%d/metrics", port)


# ---------------------------------------------------------------------------
# Main control loop (state machine)
# ---------------------------------------------------------------------------
class Watchdog:
    """The monitoring state machine; ``tick()`` runs one poll iteration.

    Every side effect (K8s API, cgroup reads, host stats, clock, sleeps)
    enters through an injectable seam, so the pending-resize supervision,
    rollback and circuit-breaker logic is unit-testable without a cluster
    (see test_watchdog.py::TestWatchdogStateMachine).
    """

    SPEC_REFRESH_INTERVAL = 2.0   # rate limit for pod spec reads
    PENDING_CHECK_INTERVAL = 2.0  # rate limit for resize-condition polls
    BLOCKED_PATH_SLEEP = 10.0     # back-off while a scale-up stays blocked

    def __init__(self, cfg: Config, api: PodApi, notifier: FeishuNotifier,
                 metrics: Metrics, heartbeat: Heartbeat, target_dir: str,
                 pod_slice: str, baseline: int, fatal_fn, *,
                 baseline_requests: Optional[str] = None,
                 current_requests: Optional[str] = None,
                 now_fn=time.monotonic, sleep_fn=time.sleep,
                 read_cgroup=read_cgroup_memory,
                 host_stats=get_host_memory_stats,
                 find_container_dir=find_target_cgroup_dir,
                 jitter_fn=None) -> None:
        self.cfg = cfg
        self.api = api
        self.notifier = notifier
        self.metrics = metrics
        self.heartbeat = heartbeat
        self.target_dir = target_dir
        self.pod_slice = pod_slice
        self.baseline = baseline
        # Original requests to restore when the limit returns to baseline.
        # Must parse and must not exceed the baseline limit (requests >
        # limits is rejected by the API server); anything else falls back to
        # the requests == limits behaviour.
        self.baseline_requests: Optional[str] = None
        self._baseline_req_bytes: Optional[int] = None
        if baseline_requests:
            try:
                req_bytes = parse_memory_to_bytes(baseline_requests)
                if req_bytes <= baseline:
                    self.baseline_requests = baseline_requests
                    self._baseline_req_bytes = req_bytes
                else:
                    logger.warning("Ignoring baseline requests %s: above the baseline "
                                   "limit %s.", baseline_requests, bytes_to_k8s_str(baseline))
            except ValueError:
                logger.warning("Ignoring unparseable baseline requests %r.", baseline_requests)
        # Spec requests as last written/observed. A failed scale-up rolls back
        # with requests == limits (deliberate: the memory is genuinely held,
        # honest requests keep the pod low in eviction ranking); the
        # low-watermark path uses this to know a restore is still owed even
        # though the limit never left baseline. Survives sidecar restarts via
        # the startup spec read (current_requests).
        self.requests_current: Optional[int] = None
        if current_requests:
            try:
                self.requests_current = parse_memory_to_bytes(current_requests)
            except ValueError:
                logger.warning("Ignoring unparseable spec requests %r.", current_requests)
        self._fatal = fatal_fn
        self._now = now_fn
        self._sleep = sleep_fn
        self._read_cgroup = read_cgroup
        self._host_stats = host_stats
        self._find_container_dir = find_container_dir
        self._jitter = jitter_fn or (lambda: random.uniform(0.0, 0.3))

        self.spec_limit = baseline
        self.pending: Optional[dict] = None  # {"target","is_down","started","last_check"}
        self.circuit_open_until = 0.0
        self.low_since: Optional[float] = None
        # Starting at "now" also suppresses scale-downs for one cooldown after
        # a (re)start and avoids comparing against the monotonic epoch 0.0.
        self.last_scale_up = now_fn()
        self.last_spec_read = 0.0

    # ------------------------- resize plumbing -------------------------
    def do_patch(self, target_bytes: int, is_down: bool) -> bool:
        """PATCH the resize subresource; returns True when accepted."""
        # Canonicalize to a 1Mi-aligned value: for a non-aligned target (a
        # decimal spec quantity like "1500M") the rendered string, the
        # kernel's page-rounded memory.max and pending["target"] would all
        # disagree, and supervision could never observe equality -- every
        # such resize would "fail" by timeout and roll back.
        target_bytes = max(MIB, target_bytes // MIB * MIB)
        target_str = bytes_to_k8s_str(target_bytes)
        # Landing back on the baseline ends the borrow: hand the scheduler
        # headroom back by restoring the pod's original requests. Any target
        # still above baseline keeps requests == limits (the borrowed memory
        # must stay visible to scheduler accounting). "<=" not "==": a
        # non-Mi-aligned baseline canonicalizes slightly below itself.
        requests_str = None
        if is_down and self.baseline_requests and target_bytes <= self.baseline:
            requests_str = self.baseline_requests
        try:
            self.api.patch_limits(target_str, requests_str)
        except ApiException as e:
            if e.status in (404, 405):
                self._fatal(
                    "The pod resize subresource is not available on this cluster "
                    "(requires Kubernetes >= 1.33 / EKS >= 1.34). Disable "
                    "worker.watchdog or upgrade the cluster."
                )
            action = "缩容" if is_down else "扩容"
            logger.error("Resize PATCH rejected (status %s): %s", e.status, e.body)
            self.notifier.notify(
                "patch-rejected", f"💥 原地{action}请求被拒绝", "red",
                f"K8s API 异常 (Status {e.status}):\n```\n{e.body}\n```",
            )
            self.api.emit_event("ResizeFailed",
                                f"Resize PATCH to {target_str} rejected by API (status {e.status})",
                                event_type="Warning")
            self.metrics.inc("watchdog_resize_failed_total", {"stage": "patch"})
            self.circuit_open_until = self._now() + 60
            return False
        except Exception as e:
            logger.error("Resize PATCH failed with unexpected error: %s", e)
            self.notifier.notify("patch-error", "💥 原地扩缩容网络异常", "red", f"`{e}`")
            self.metrics.inc("watchdog_resize_failed_total", {"stage": "patch"})
            self.circuit_open_until = self._now() + 60
            return False
        self.spec_limit = target_bytes
        self.requests_current = self._baseline_req_bytes if requests_str else target_bytes
        self.pending = {
            "target": target_bytes,
            "is_down": is_down,
            "started": self._now(),
            "last_check": 0.0,
        }
        return True

    def _requests_inflated(self) -> bool:
        """True while the spec requests exceed the pod's original requests."""
        return (self._baseline_req_bytes is not None
                and self.requests_current is not None
                and self.requests_current > self._baseline_req_bytes)

    def abort_pending(self, reason: str, cgroup_max: int) -> None:
        """Roll the spec back to the actually-enforced limit and open the circuit."""
        target = self.pending["target"] if self.pending else 0
        logger.error("Resize to %s failed: %s. Rolling spec back to %s.",
                     bytes_to_k8s_str(target), reason, bytes_to_k8s_str(cgroup_max))
        try:
            # Requests deliberately stay == limits here: the pod genuinely
            # holds this much memory right now, and honest requests keep it
            # low in the eviction ranking. Once the pressure clears, the
            # scale-down low-watermark path restores the original requests
            # (see decide_scale_down's restore-requests branch).
            self.api.patch_limits(bytes_to_k8s_str(cgroup_max))
            self.spec_limit = cgroup_max
            self.requests_current = cgroup_max
        except Exception as e:
            logger.error("Rollback PATCH failed (spec and cgroup now diverge): %s", e)
        self.notifier.notify(
            "resize-failed", "🔴 [高危] 原地扩容未能生效", "red",
            f"**原因:** {reason}\n"
            f"**期望配额:** `{bytes_to_k8s_str(target)}`\n"
            f"**实际配额:** `{bytes_to_k8s_str(cgroup_max)}`\n"
            f"**处置:** spec 已回滚，{int(self.cfg.circuit_cooldown / 60)} 分钟内暂停扩容。"
            f"若为节点容量不足，请扩容节点或横向分流。",
        )
        self.api.emit_event(
            "ResizeFailed",
            f"In-place resize to {bytes_to_k8s_str(target)} did not apply ({reason}); "
            f"spec rolled back to {bytes_to_k8s_str(cgroup_max)}, scale-ups paused "
            f"for {int(self.cfg.circuit_cooldown)}s",
            event_type="Warning",
        )
        self.metrics.inc("watchdog_resize_failed_total", {"stage": "kubelet"})
        self.pending = None
        self.circuit_open_until = self._now() + self.cfg.circuit_cooldown

    def _relocate_cgroup(self) -> bool:
        """Rebind after the target container restarted (its old cgroup scope,
        named by container ID, disappears and a new one is created)."""
        container_id = None
        try:
            container_id = self.api.get_container_id(self.api.get_pod())
        except Exception as e:
            logger.warning("Could not refresh container ID during cgroup relocation: %s", e)
        new_dir = self._find_container_dir(self.pod_slice, container_id)
        if not new_dir or new_dir == self.target_dir:
            return False
        logger.warning("Target container cgroup moved (container restarted?): %s -> %s",
                       self.target_dir, new_dir)
        self.target_dir = new_dir
        self.metrics.inc("watchdog_cgroup_relocated_total")
        self.api.emit_event(
            "CgroupRelocated",
            f"Target container cgroup relocated to {new_dir} after a container restart",
        )
        # A restarted container comes back at its declared spec limit: drop
        # stale in-flight supervision and observe fresh state next tick (a
        # genuinely divergent spec is re-adopted by the scale-up path).
        self.pending = None
        self.low_since = None
        return True

    # ----------------------------- one tick -----------------------------
    def tick(self) -> None:
        now = self._now()
        self.heartbeat.beat()

        try:
            working_set, cgroup_max = self._read_cgroup(self.target_dir)
        except FileNotFoundError:
            if self._relocate_cgroup():
                return  # rebound; observe fresh state next tick
            raise
        if cgroup_max is None or cgroup_max <= 0:
            return
        self.metrics.set_gauge("watchdog_working_set_bytes", working_set)
        self.metrics.set_gauge("watchdog_memory_limit_bytes", cgroup_max)

        if self.pending:
            self._supervise_pending(now, working_set, cgroup_max)
            return  # while pending, never start a new decision

        if working_set >= int(cgroup_max * self.cfg.threshold):
            self._scale_up_path(now, working_set, cgroup_max)
        else:
            self._scale_down_path(now, working_set, cgroup_max)

    # ---------------------- pending resize supervision ----------------------
    def _supervise_pending(self, now: float, working_set: int, cgroup_max: int) -> None:
        pending = self.pending
        if cgroup_max == pending["target"]:
            is_down = pending["is_down"]
            action = "自动回缩" if is_down else "原地垂直扩容"
            logger.info("SUCCESS: kubelet applied the in-place resize; limit is now %s.",
                        bytes_to_k8s_str(cgroup_max))
            self.notifier.notify(
                f"resize-ok-{'down' if is_down else 'up'}",
                f"✨ Kubelet 原地{action}生效", "blue" if is_down else "green",
                f"🎉 **原地资源调整已在内核生效！**\n\n"
                f"**变更类型:** `{action}`\n"
                f"**生效 Cgroup 限额:** `{bytes_to_k8s_str(cgroup_max)}`",
            )
            self.api.emit_event(
                "ResizeApplied",
                f"In-place {'scale-down' if is_down else 'scale-up'} applied by "
                f"kubelet; memory limit is now {bytes_to_k8s_str(cgroup_max)}",
            )
            self.pending = None
            return

        # A burst arriving while a scale-down is still in flight must not be
        # locked out of OOM rescue for up to pending_timeout: withdraw the
        # shrink by re-declaring the currently enforced limit, then let the
        # next tick take the normal scale-up path.
        if pending["is_down"] and working_set >= int(cgroup_max * self.cfg.threshold):
            try:
                self.api.patch_limits(bytes_to_k8s_str(cgroup_max))
            except Exception as e:
                # Keep supervising; pending_timeout still bounds this state.
                logger.warning("Could not withdraw the in-flight scale-down: %s", e)
                return
            logger.warning("Scale-down to %s withdrawn: working set is back above the "
                           "scale-up watermark while the shrink was in flight.",
                           bytes_to_k8s_str(pending["target"]))
            self.api.emit_event(
                "ScaleDownWithdrawn",
                f"In-flight scale-down to {bytes_to_k8s_str(pending['target'])} withdrawn "
                f"because memory pressure returned; limit stays {bytes_to_k8s_str(cgroup_max)}",
            )
            self.metrics.inc("watchdog_scale_down_withdrawn_total")
            self.spec_limit = cgroup_max
            self.pending = None
            self.low_since = None
            return

        if now - pending["last_check"] >= self.PENDING_CHECK_INTERVAL:
            pending["last_check"] = now
            try:
                reason, message = self.api.get_resize_pending(self.api.get_pod())
            except Exception as e:
                logger.warning("Could not check resize conditions: %s", e)
                reason, message = None, ""
            if reason == "Infeasible":
                self.abort_pending(f"kubelet 判定 Infeasible: {message}", cgroup_max)
                return
            if reason == "Deferred":
                logger.info("Resize deferred by kubelet (%s); waiting...", message)
            if self.pending and now - pending["started"] > self.cfg.pending_timeout:
                self.abort_pending(
                    f"等待 kubelet 超过 {int(self.cfg.pending_timeout)}s 未生效"
                    + (f"（最后状态: {reason}）" if reason else ""),
                    cgroup_max,
                )

    # ---------------------------- scale-up path ----------------------------
    def _scale_up_path(self, now: float, working_set: int, cgroup_max: int) -> None:
        self.low_since = None
        if now < self.circuit_open_until:
            self.metrics.inc("watchdog_blocked_total", {"reason": "circuit_open"})
            return

        logger.warning(
            "Memory watermark critical! working_set=%.1fMB limit=%.1fMB usage=%.1f%%",
            working_set / MIB, cgroup_max / MIB, working_set * 100.0 / cgroup_max,
        )

        # Refresh the declared spec limit (rate-limited): an external actor
        # may have changed it, or kubelet may still be applying an earlier
        # change made before a watchdog restart.
        if now - self.last_spec_read >= self.SPEC_REFRESH_INTERVAL:
            try:
                spec_str = self.api.get_container_limit(self.api.get_pod())
                if spec_str:
                    self.spec_limit = parse_memory_to_bytes(spec_str)
                self.last_spec_read = now
            except Exception as e:
                # OOM rescue is inoperative without the spec; surface it as
                # a metric + notification, not just a log line.
                logger.error("Failed to fetch pod spec: %s", e)
                self.metrics.inc("watchdog_spec_read_errors_total")
                self.notifier.notify(
                    "spec-read-failed", "🔴 [高危] 内存高压期间无法读取 pod spec", "red",
                    f"扩容决策被阻塞，读取 pod spec 失败：`{e}`\n"
                    "若 API Server 持续不可用，OOM 抢救将无法执行。",
                )
                return
        if self.spec_limit != cgroup_max:
            # spec > cgroup: an earlier scale-up is still being applied.
            # spec < cgroup: a shrink is in flight (own pre-restart
            # scale-down or an external actor). Computing a "scale-up" from
            # the lower spec could patch a limit BELOW the enforced one and
            # shrink the container under memory pressure -- adopt and
            # supervise instead; the withdraw path rescues within one tick.
            is_down = self.spec_limit < cgroup_max
            logger.info("Resize already in flight (spec %s, cgroup %s); supervising...",
                        bytes_to_k8s_str(self.spec_limit), bytes_to_k8s_str(cgroup_max))
            self.pending = {"target": self.spec_limit, "is_down": is_down,
                            "started": now, "last_check": 0.0}
            return

        host_total, host_available = self._host_stats()
        if host_total is None:
            self.metrics.inc("watchdog_host_stats_errors_total")
        sample = Sample(working_set, cgroup_max, self.spec_limit, self.baseline,
                        host_total, host_available)
        decision = decide_scale_up(sample, self.cfg)

        if decision.action == Action.BLOCKED_NO_HOST_STATS:
            logger.critical("Host memory stats unavailable; scale-up refused. "
                            "Check the /host/proc/meminfo hostPath mount!")
            self.notifier.notify(
                "no-host-stats", "🔴 [高危] 看门狗失去宿主机视野", "red",
                "无法读取宿主机内存信息，安全校验无法执行，扩容已被拒绝。\n"
                "请检查 `/host/proc/meminfo` hostPath 挂载。此状态下看门狗**无法抢救 OOM**。",
            )
            self.api.emit_event("WatchdogBlind",
                                "Host memory stats unavailable; scale-up refused and "
                                "OOM rescue is inoperative", event_type="Warning")
            self.metrics.inc("watchdog_blocked_total", {"reason": "no_host_stats"})
            self._sleep(self.BLOCKED_PATH_SLEEP)
            return
        if decision.action == Action.BLOCKED_MAX_LIMIT:
            self.notifier.notify(
                "max-limit", "❌ 扩容达到集群配额上限", "orange",
                f"**当前配额:** `{bytes_to_k8s_str(self.spec_limit)}`\n"
                f"**拦截原因:** 已达集群声明的最大容忍上限 (`{bytes_to_k8s_str(self.cfg.max_limit_bytes)}`)。",
            )
            self.api.emit_event("ScaleUpBlocked",
                                f"Memory pressure at {bytes_to_k8s_str(self.spec_limit)} but the "
                                f"cluster cap ({bytes_to_k8s_str(self.cfg.max_limit_bytes)}) is reached",
                                event_type="Warning")
            self.metrics.inc("watchdog_blocked_total", {"reason": "max_limit"})
            self._sleep(self.BLOCKED_PATH_SLEEP)
            return
        if decision.action == Action.BLOCKED_HOST_FULL:
            logger.critical("HOST CAPACITY EXHAUSTED: %s", decision.reason)
            self.notifier.notify(
                "host-full", "🔴 [高危] 宿主机内存枯竭，扩容熔断", "red",
                f"{decision.reason}\n**决策:** 拒绝扩容，守护节点稳定。请扩容节点或横向分流。",
            )
            self.api.emit_event("ScaleUpBlocked",
                                f"Scale-up refused to protect the node: {decision.reason}",
                                event_type="Warning")
            self.metrics.inc("watchdog_blocked_total", {"reason": "host_full"})
            self._sleep(self.BLOCKED_PATH_SLEEP)
            return

        # SCALE_UP / SCALE_UP_ADAPTIVE: re-verify right before the PATCH
        # after a short random jitter, de-synchronizing concurrent watchdogs
        # on the same node. Kept small on purpose: it eats into the rescue
        # window, and the kubelet's allocatable admission is the
        # authoritative arbiter of races anyway -- this only reduces
        # pointless Infeasible round-trips.
        self._sleep(self._jitter())
        host_total, host_available = self._host_stats()
        fresh_ws, fresh_max = self._read_cgroup(self.target_dir)
        recheck = decide_scale_up(
            Sample(fresh_ws, fresh_max or cgroup_max, self.spec_limit, self.baseline,
                   host_total, host_available),
            self.cfg,
        )
        if recheck.action not in (Action.SCALE_UP, Action.SCALE_UP_ADAPTIVE):
            logger.warning("Scale-up cancelled on re-check: %s (%s)",
                           recheck.action, recheck.reason)
            self.metrics.inc("watchdog_blocked_total", {"reason": "recheck"})
            return

        target = recheck.target_bytes
        if recheck.action == Action.SCALE_UP_ADAPTIVE:
            self.notifier.notify(
                "adaptive-step", "⚠️ 触发宿主机容量自适应降级扩容", "orange",
                f"**原因:** {recheck.reason}\n"
                f"**原地垂直扩容计划:** `{bytes_to_k8s_str(self.spec_limit)}` ──► `{bytes_to_k8s_str(target)}`",
            )
        logger.info("TRIGGERING OOM RESCUE: in-place resize %s -> %s",
                    bytes_to_k8s_str(self.spec_limit), bytes_to_k8s_str(target))
        self.notifier.notify(
            "scale-up", "🚨 [OOM 紧急抢救] 触发原地垂直扩容", "red",
            f"**容器工作集:** `{working_set / MIB:.1f}MB` / `{cgroup_max / MIB:.1f}MB` "
            f"(`{working_set * 100.0 / cgroup_max:.1f}%`)\n"
            f"**原地垂直扩容申请:** `{bytes_to_k8s_str(self.spec_limit)}` ──► `{bytes_to_k8s_str(target)}`\n"
            f"（requests 与 limits 同步调整，借用内存全程纳入调度器记账）",
        )
        old_limit = self.spec_limit
        if self.do_patch(target, is_down=False):
            self.last_scale_up = now
            self.metrics.inc("watchdog_scale_up_total")
            self.api.emit_event(
                "ScaleUp",
                f"OOM rescue: in-place scale-up requested "
                f"{bytes_to_k8s_str(old_limit)} -> {bytes_to_k8s_str(target)} "
                f"(working set {working_set // MIB}Mi at "
                f"{working_set * 100 // cgroup_max}% of limit"
                f"{'; adaptive step' if recheck.action == Action.SCALE_UP_ADAPTIVE else ''})",
            )
            logger.info("PATCH accepted; supervising kubelet application...")

    # --------------------------- scale-down path ---------------------------
    def _scale_down_path(self, now: float, working_set: int, cgroup_max: int) -> None:
        decision = decide_scale_down(
            Sample(working_set, cgroup_max, self.spec_limit, self.baseline, None, None,
                   requests_inflated=self._requests_inflated()),
            self.cfg,
        )
        if decision.action != Action.SCALE_DOWN:
            if self.low_since is not None:
                logger.info("Working set rose above the scale-down watermark; "
                            "cancelling cooldown.")
                self.low_since = None
            return
        if self.low_since is None:
            self.low_since = now
            logger.info(
                "Working set (%.1fMB) below %.0f%% of the current limit; starting "
                "scale-down cooldown (%.0fs)...",
                working_set / MIB, self.cfg.scale_down_ratio * 100, self.cfg.scale_down_cooldown,
            )
            return
        if (now - self.low_since < self.cfg.scale_down_cooldown
                or now - self.last_scale_up < self.cfg.scale_down_cooldown):
            return

        # Scale-downs are rare (cooldown-gated), so afford a spec refresh
        # first: an in-flight external resize must be adopted and supervised,
        # not silently overwritten by our shrink.
        try:
            pod = self.api.get_pod()
            spec_str = self.api.get_container_limit(pod)
            if spec_str:
                self.spec_limit = parse_memory_to_bytes(spec_str)
            req_str = self.api.get_container_requests(pod)
            if req_str:
                try:
                    self.requests_current = parse_memory_to_bytes(req_str)
                except ValueError:
                    pass
            self.last_spec_read = now
        except Exception as e:
            logger.warning("Skipping scale-down: could not refresh the pod spec: %s", e)
            return
        is_restore_only = decision.reason == "restore-requests"
        if is_restore_only and not self._requests_inflated():
            # The refreshed spec says the requests are already home (someone
            # else restored them, or in-memory tracking was stale): done.
            self.low_since = None
            return
        if self.spec_limit != cgroup_max:
            logger.info("External resize in flight (spec %s, cgroup %s); supervising it "
                        "instead of scaling down.",
                        bytes_to_k8s_str(self.spec_limit), bytes_to_k8s_str(cgroup_max))
            self.pending = {"target": self.spec_limit,
                            "is_down": self.spec_limit < cgroup_max,
                            "started": now, "last_check": 0.0}
            return

        target = decision.target_bytes
        if is_restore_only:
            # Limit is already at baseline; this PATCH only hands the
            # scheduler headroom back (requests -> original) after a failed
            # scale-up rolled back with honest, inflated requests.
            logger.info("COOLDOWN ELAPSED: restoring original requests %s "
                        "(limit stays at %s)",
                        self.baseline_requests, bytes_to_k8s_str(cgroup_max))
            self.notifier.notify(
                "requests-restore", "📉 [资源回交] 恢复初始 requests", "blue",
                f"扩容失败回滚后 requests 曾如实抬升至 `{bytes_to_k8s_str(cgroup_max)}`；"
                f"工作集已稳定处于低水位 `{working_set / MIB:.1f}MB` 超过 "
                f"{int(now - self.low_since)}s，恢复初始 requests "
                f"`{self.baseline_requests}`（limit 不变）。",
            )
            if self.do_patch(target, is_down=True):
                self.metrics.inc("watchdog_requests_restored_total")
                self.api.emit_event(
                    "RequestsRestored",
                    f"Scheduler headroom returned: requests restored to "
                    f"{self.baseline_requests} after a rolled-back scale-up "
                    f"(limit unchanged at {bytes_to_k8s_str(cgroup_max)}, "
                    f"working set stable at {working_set // MIB}Mi)",
                )
                self.low_since = None
            return
        logger.info("COOLDOWN ELAPSED: scaling down %s -> %s",
                    bytes_to_k8s_str(cgroup_max), bytes_to_k8s_str(target))
        self.notifier.notify(
            "scale-down", "📉 [资源回交] 触发容器自动原地缩容", "blue",
            f"工作集已稳定处于低水位 `{working_set / MIB:.1f}MB` 超过 "
            f"{int(now - self.low_since)}s，防抖期满。\n"
            f"**就地缩容回退:** `{bytes_to_k8s_str(cgroup_max)}` ──► `{bytes_to_k8s_str(target)}`",
        )
        if self.do_patch(target, is_down=True):
            self.metrics.inc("watchdog_scale_down_total")
            self.api.emit_event(
                "ScaleDown",
                f"Borrowed memory returned: in-place scale-down requested "
                f"{bytes_to_k8s_str(cgroup_max)} -> {bytes_to_k8s_str(target)} "
                f"(working set stable at {working_set // MIB}Mi)",
            )
            self.low_since = None


def main() -> None:
    cfg = load_config()
    pod_name = os.environ.get("POD_NAME")
    pod_namespace = os.environ.get("POD_NAMESPACE")
    pod_uid = os.environ.get("POD_UID")

    notifier = FeishuNotifier()
    notifier.start()
    metrics = Metrics()
    heartbeat = Heartbeat()

    def fatal(message: str) -> None:
        logger.critical(message)
        notifier.notify("fatal", "💥 Watchdog 启动/运行失败", "red", message)
        time.sleep(3)  # give the notifier thread a chance to flush
        sys.exit(1)

    # The liveness probe targets /healthz on this server: without it the
    # kubelet restarts the sidecar anyway, so fail fast with a clear message.
    try:
        start_metrics_server(metrics, cfg.metrics_port, heartbeat)
    except OSError as e:
        fatal(f"Could not bind the metrics/liveness endpoint on port "
              f"{cfg.metrics_port}: {e}")

    logger.info("=" * 70)
    logger.info("Starting OOM watchdog sidecar (limits-only in-place resize).")
    logger.info("Target pod: %s | namespace: %s | uid: %s", pod_name, pod_namespace, pod_uid)
    logger.info(
        "Config - threshold: %.0f%%, interval: %.2fs, step: %s, cap: %g x baseline, "
        "host ceiling: %.0f%%",
        cfg.threshold * 100, cfg.poll_interval, bytes_to_k8s_str(cfg.step_bytes),
        cfg.max_factor, cfg.host_ceiling * 100,
    )
    logger.info("=" * 70)

    if not pod_name or not pod_namespace:
        fatal("POD_NAME / POD_NAMESPACE environment variables are required (downward API).")
    if k8s_client is None:
        fatal("The 'kubernetes' package is not installed in this image.")

    try:
        k8s_config.load_incluster_config()
        api = PodApi(k8s_client.CoreV1Api(), pod_name, pod_namespace,
                     events_v1=k8s_client.EventsV1Api(), pod_uid=pod_uid)
        logger.info("Loaded in-cluster Kubernetes configuration.")
    except Exception as e:
        fatal(f"Failed to load in-cluster Kubernetes configuration: {e}")
        return

    pod_slice = find_pod_cgroup_dir(pod_uid)
    if not pod_slice:
        fatal(f"Could not locate the pod cgroup slice under {HOST_CGROUP_ROOT}.")
        return
    logger.info("Bound to pod cgroup path: %s", pod_slice)

    # Container ID from pod status makes cgroup matching exact; the >500Mi
    # size heuristic remains as fallback while the status is not ready yet.
    container_id = None
    try:
        container_id = api.get_container_id(api.get_pod())
    except Exception as e:
        logger.warning("Could not read container ID from pod status yet: %s", e)

    # As a native sidecar the watchdog starts before the main container even
    # exists; on a fresh node pulling the main image alone can take minutes,
    # so wait generously (heartbeat.beat() keeps the liveness probe green).
    target_dir = None
    for attempt in range(1, 301):
        heartbeat.beat()
        if not container_id and attempt % 5 == 0:
            try:
                container_id = api.get_container_id(api.get_pod())
            except Exception as e:
                logger.debug("Container ID still unavailable: %s", e)
        target_dir = find_target_cgroup_dir(pod_slice, container_id)
        if target_dir:
            break
        if attempt == 1 or attempt % 10 == 0:
            logger.info("[%d/300] Waiting for %s container cgroup to initialize...",
                        attempt, TARGET_CONTAINER)
        time.sleep(1)
    if not target_dir:
        fatal(f"Could not locate the {TARGET_CONTAINER} cgroup directory after 300 seconds.")
        return
    logger.info("Monitoring %s cgroup: %s", TARGET_CONTAINER, target_dir)

    # Baseline limit AND baseline requests: persisted in pod annotations so a
    # watchdog restart after a resize does not adopt the inflated limit (or
    # the requests it dragged along) as the new baseline.
    try:
        pod = api.get_pod()
        annotations = pod.metadata.annotations or {}
        baseline_str = annotations.get(BASELINE_ANNOTATION)
        if baseline_str:
            logger.info("Restored baseline from pod annotation: %s", baseline_str)
        else:
            baseline_str = api.get_container_limit(pod)
            if not baseline_str:
                fatal("Could not determine the baseline memory limit from the pod spec.")
                return
            try:
                api.annotate(BASELINE_ANNOTATION, baseline_str)
                logger.info("Persisted baseline %s to pod annotation.", baseline_str)
            except Exception as e:
                logger.warning("Could not persist baseline annotation (continuing): %s", e)
        baseline = parse_memory_to_bytes(baseline_str)

        # Spec requests as they are right now -- may still be inflated from a
        # scale-up that rolled back before this (re)start; the low-watermark
        # path restores them once the pressure clears.
        current_req_str = api.get_container_requests(pod)
        baseline_req_str = annotations.get(BASELINE_REQUESTS_ANNOTATION)
        if baseline_req_str:
            logger.info("Restored baseline requests from pod annotation: %s", baseline_req_str)
        else:
            baseline_req_str = api.get_container_requests(pod)
            if baseline_req_str:
                try:
                    api.annotate(BASELINE_REQUESTS_ANNOTATION, baseline_req_str)
                    logger.info("Persisted baseline requests %s to pod annotation.",
                                baseline_req_str)
                except Exception as e:
                    logger.warning("Could not persist baseline requests annotation "
                                   "(continuing): %s", e)
    except SystemExit:
        raise
    except Exception as e:
        fatal(f"Fatal error fetching the initial pod specification: {e}")
        return
    metrics.set_gauge("watchdog_baseline_bytes", baseline)

    # The cap derives from the baseline, so it is only final here.
    cfg.max_limit_bytes = resolve_max_limit(cfg, baseline)
    logger.info("Scale-up cap resolved: %s (%g x baseline)",
                bytes_to_k8s_str(cfg.max_limit_bytes), cfg.max_factor)

    watchdog_loop = Watchdog(
        cfg, api, notifier, metrics, heartbeat,
        target_dir=target_dir, pod_slice=pod_slice, baseline=baseline,
        baseline_requests=baseline_req_str,
        current_requests=current_req_str,
        fatal_fn=fatal,
    )

    consecutive_errors = 0
    while True:
        try:
            time.sleep(cfg.poll_interval)
            watchdog_loop.tick()
            consecutive_errors = 0
        except SystemExit:
            raise
        except Exception as e:
            consecutive_errors += 1
            logger.error("Exception in watchdog loop (%d consecutive): %s",
                         consecutive_errors, e)
            if consecutive_errors >= cfg.max_consecutive_errors:
                fatal(f"Watchdog loop failed {consecutive_errors} times in a row; "
                      f"last error: {e}. Exiting so kubelet restarts the sidecar.")
            time.sleep(2)


if __name__ == "__main__":
    main()
