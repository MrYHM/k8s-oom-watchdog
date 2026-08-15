#!/usr/bin/env python3
"""Offline unit tests for the watchdog sidecar.

Imports the real production module (no kubernetes package or cluster needed:
the module guards that import) and exercises the pure decision layer plus the
cgroup/file parsing helpers against temp directories.
"""

import os
import queue
import shutil
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import watchdog
from watchdog import (
    Action,
    Config,
    Sample,
    align_up,
    bytes_to_k8s_str,
    decide_scale_down,
    decide_scale_up,
    find_target_cgroup_dir,
    find_pod_cgroup_dir,
    parse_memory_to_bytes,
    read_cgroup_memory,
)

GIB = 1024 ** 3
MIB = 1024 ** 2


def make_cfg(**overrides) -> Config:
    cfg = Config()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class TestUnitConversion(unittest.TestCase):
    def test_binary_suffixes(self):
        self.assertEqual(parse_memory_to_bytes("16Gi"), 16 * GIB)
        self.assertEqual(parse_memory_to_bytes("512Mi"), 512 * MIB)
        self.assertEqual(parse_memory_to_bytes("4Ki"), 4096)
        self.assertEqual(parse_memory_to_bytes("1024"), 1024)

    def test_decimal_suffixes_follow_k8s_semantics(self):
        self.assertEqual(parse_memory_to_bytes("2G"), 2 * 10 ** 9)
        self.assertEqual(parse_memory_to_bytes("256M"), 256 * 10 ** 6)
        self.assertEqual(parse_memory_to_bytes("1K"), 1000)

    def test_fractional_quantities(self):
        self.assertEqual(parse_memory_to_bytes("1.5Gi"), int(1.5 * GIB))
        self.assertEqual(parse_memory_to_bytes("0.5Mi"), 512 * 1024)

    def test_invalid_quantity_raises(self):
        with self.assertRaises(ValueError):
            parse_memory_to_bytes("")
        with self.assertRaises(ValueError):
            parse_memory_to_bytes("abc")

    def test_bytes_to_k8s_str_binary_only(self):
        self.assertEqual(bytes_to_k8s_str(16 * GIB), "16Gi")
        self.assertEqual(bytes_to_k8s_str(512 * MIB), "512Mi")
        self.assertEqual(bytes_to_k8s_str(1536 * MIB), "1536Mi")
        # never emits decimal "K" (which K8s reads as 1000)
        self.assertEqual(bytes_to_k8s_str(1024), "1Ki")

    def test_round_trip(self):
        for value in (2 * GIB, 6 * GIB + 512 * MIB, 48 * GIB):
            self.assertEqual(parse_memory_to_bytes(bytes_to_k8s_str(value)), value)

    def test_align_up(self):
        step = 512 * MIB
        self.assertEqual(align_up(1, step), step)
        self.assertEqual(align_up(step, step), step)
        self.assertEqual(align_up(step + 1, step), 2 * step)


class TestDecideScaleUp(unittest.TestCase):
    def sample(self, **overrides) -> Sample:
        defaults = dict(
            working_set=int(1.7 * GIB),
            cgroup_max=2 * GIB,
            spec_limit=2 * GIB,
            baseline=2 * GIB,
            host_total=32 * GIB,
            host_available=20 * GIB,
        )
        defaults.update(overrides)
        return Sample(**defaults)

    def test_below_threshold_is_noop(self):
        decision = decide_scale_up(self.sample(working_set=1 * GIB), make_cfg())
        self.assertEqual(decision.action, Action.NOOP)

    def test_normal_scale_up_adds_full_step(self):
        decision = decide_scale_up(self.sample(), make_cfg())
        self.assertEqual(decision.action, Action.SCALE_UP)
        self.assertEqual(decision.target_bytes, 6 * GIB)

    def test_step_clamped_to_max_limit(self):
        decision = decide_scale_up(
            self.sample(working_set=int(45 * GIB * 0.9), cgroup_max=45 * GIB,
                        spec_limit=45 * GIB, host_total=128 * GIB,
                        host_available=100 * GIB),
            make_cfg(),
        )
        self.assertEqual(decision.action, Action.SCALE_UP)
        self.assertEqual(decision.target_bytes, 48 * GIB)

    def test_blocked_when_already_at_max_limit(self):
        decision = decide_scale_up(
            self.sample(working_set=int(48 * GIB * 0.9), cgroup_max=48 * GIB,
                        spec_limit=48 * GIB),
            make_cfg(),
        )
        self.assertEqual(decision.action, Action.BLOCKED_MAX_LIMIT)

    def test_host_ceiling_triggers_adaptive_step_down(self):
        # 20GiB host, ceiling 85% = 17GiB. Pod uses 8GiB of the host's 14GiB
        # used => others use 6GiB. Full step 10+4=14GiB projects 20GiB > 17GiB.
        # Max safe step = 17 - 10 - 6 = 1GiB (512Mi-aligned).
        decision = decide_scale_up(
            Sample(working_set=8 * GIB, cgroup_max=10 * GIB, spec_limit=10 * GIB,
                   baseline=2 * GIB, host_total=20 * GIB, host_available=6 * GIB),
            make_cfg(),
        )
        self.assertEqual(decision.action, Action.SCALE_UP_ADAPTIVE)
        self.assertEqual(decision.target_bytes, 11 * GIB)

    def test_host_exhausted_blocks_even_minimal_step(self):
        # Others use 11GiB on a 12GiB host: no room for 512Mi more.
        decision = decide_scale_up(
            Sample(working_set=int(1.8 * GIB), cgroup_max=2 * GIB, spec_limit=2 * GIB,
                   baseline=2 * GIB, host_total=12 * GIB,
                   host_available=int(0.5 * GIB)),
            make_cfg(),
        )
        self.assertEqual(decision.action, Action.BLOCKED_HOST_FULL)

    def test_missing_host_stats_blocks_by_default(self):
        decision = decide_scale_up(
            self.sample(host_total=None, host_available=None), make_cfg()
        )
        self.assertEqual(decision.action, Action.BLOCKED_NO_HOST_STATS)

    def test_missing_host_stats_half_step_when_blind_allowed(self):
        decision = decide_scale_up(
            self.sample(host_total=None, host_available=None),
            make_cfg(allow_blind_scaleup=True),
        )
        self.assertEqual(decision.action, Action.SCALE_UP_ADAPTIVE)
        self.assertEqual(decision.target_bytes, 4 * GIB)  # 2Gi + half of 4Gi


class TestDecideScaleDown(unittest.TestCase):
    def test_noop_at_baseline(self):
        decision = decide_scale_down(
            Sample(working_set=100 * MIB, cgroup_max=2 * GIB, spec_limit=2 * GIB,
                   baseline=2 * GIB, host_total=None, host_available=None),
            make_cfg(),
        )
        self.assertEqual(decision.action, Action.NOOP)

    def test_noop_when_working_set_still_high(self):
        # 3GiB of a 6GiB limit = 50% >= the 40% watermark
        decision = decide_scale_down(
            Sample(working_set=3 * GIB, cgroup_max=6 * GIB, spec_limit=6 * GIB,
                   baseline=2 * GIB, host_total=None, host_available=None),
            make_cfg(),
        )
        self.assertEqual(decision.action, Action.NOOP)

    def test_steps_down_to_twice_working_set(self):
        # 1.5GiB working set, 6GiB limit, 2GiB baseline -> target 3GiB
        decision = decide_scale_down(
            Sample(working_set=int(1.5 * GIB), cgroup_max=6 * GIB, spec_limit=6 * GIB,
                   baseline=2 * GIB, host_total=None, host_available=None),
            make_cfg(),
        )
        self.assertEqual(decision.action, Action.SCALE_DOWN)
        self.assertEqual(decision.target_bytes, 3 * GIB)

    def test_never_goes_below_baseline(self):
        decision = decide_scale_down(
            Sample(working_set=100 * MIB, cgroup_max=6 * GIB, spec_limit=6 * GIB,
                   baseline=2 * GIB, host_total=None, host_available=None),
            make_cfg(),
        )
        self.assertEqual(decision.action, Action.SCALE_DOWN)
        self.assertEqual(decision.target_bytes, 2 * GIB)

    def test_hysteresis_no_oscillation(self):
        # After stepping down to 2x working set, usage sits at 50% of the new
        # limit -- below the 80% scale-up threshold, so no immediate re-up.
        cfg = make_cfg()
        working_set = int(1.5 * GIB)
        down = decide_scale_down(
            Sample(working_set=working_set, cgroup_max=6 * GIB, spec_limit=6 * GIB,
                   baseline=2 * GIB, host_total=None, host_available=None),
            cfg,
        )
        up_after = decide_scale_up(
            Sample(working_set=working_set, cgroup_max=down.target_bytes,
                   spec_limit=down.target_bytes, baseline=2 * GIB,
                   host_total=32 * GIB, host_available=20 * GIB),
            cfg,
        )
        self.assertEqual(up_after.action, Action.NOOP)


class TestCgroupParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write_container(self, name, mem_max, current=0, inactive_file=None):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        with open(os.path.join(path, "memory.max"), "w") as f:
            f.write(str(mem_max) + "\n")
        with open(os.path.join(path, "memory.current"), "w") as f:
            f.write(str(current) + "\n")
        if inactive_file is not None:
            with open(os.path.join(path, "memory.stat"), "w") as f:
                f.write(f"anon 1024\ninactive_file {inactive_file}\nactive_file 0\n")
        return path

    def test_working_set_excludes_inactive_file_cache(self):
        path = self._write_container(
            "c1.scope", mem_max=2 * GIB, current=int(1.8 * GIB),
            inactive_file=1 * GIB,
        )
        working_set, limit = read_cgroup_memory(path)
        self.assertEqual(working_set, int(1.8 * GIB) - 1 * GIB)
        self.assertEqual(limit, 2 * GIB)

    def test_unlimited_cgroup_returns_none(self):
        path = self._write_container("c1.scope", mem_max="max", current=100)
        working_set, limit = read_cgroup_memory(path)
        self.assertIsNone(limit)
        self.assertEqual(working_set, 100)

    def test_find_target_cgroup_by_container_id(self):
        self._write_container("cri-containerd-aaa111.scope", mem_max=16 * GIB)
        expected = self._write_container("cri-containerd-bbb222.scope", mem_max=200 * MIB)
        found = find_target_cgroup_dir(self.tmp, container_id="containerd://bbb222")
        self.assertEqual(found, expected)

    def test_find_target_cgroup_falls_back_to_size_heuristic(self):
        self._write_container("cri-containerd-watchdog.scope", mem_max=200 * MIB)
        expected = self._write_container("cri-containerd-app.scope", mem_max=16 * GIB)
        found = find_target_cgroup_dir(self.tmp, container_id=None)
        self.assertEqual(found, expected)

    def test_find_target_cgroup_returns_none_when_absent(self):
        self._write_container("cri-containerd-watchdog.scope", mem_max=200 * MIB)
        self.assertIsNone(find_target_cgroup_dir(self.tmp))


class TestFindPodCgroupDir(unittest.TestCase):
    """The mountinfo fast path only works in a HOST cgroup namespace (the
    root field then carries the real host path). Under a PRIVATE cgroup
    namespace -- the default on the EKS clusters we deploy to -- the root
    field is rendered relative to the container's own cgroup ("/" for its
    own mount, "/.." chains for the hostPath bind mount) and must be
    rejected so the pod-UID glob fallback can run."""

    UID = "6090bb22-294a-4772-9548-80563e81eab9"
    UID_SLICE = "kubepods-burstable-pod6090bb22_294a_4772_9548_80563e81eab9.slice"

    def setUp(self):
        self.host_root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.host_root)

    def _write_mountinfo(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile("w", suffix="mountinfo", delete=False)
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def _make_pod_slice(self) -> str:
        path = os.path.join(self.host_root, "kubepods.slice",
                            "kubepods-burstable.slice", self.UID_SLICE)
        os.makedirs(path)
        return path

    def test_host_cgroupns_fast_path(self):
        pod_slice = self._make_pod_slice()
        scope = f"/kubepods.slice/kubepods-burstable.slice/{self.UID_SLICE}/cri-containerd-abc.scope"
        mountinfo = self._write_mountinfo(
            f"1 0 0:29 {scope} /sys/fs/cgroup ro,nosuid - cgroup2 cgroup rw\n"
        )
        found = find_pod_cgroup_dir(self.UID, host_root=self.host_root,
                                    mountinfo_path=mountinfo)
        self.assertEqual(found, pod_slice)

    def test_private_cgroupns_falls_back_to_glob(self):
        # Real mountinfo captured from a private-cgroupns EKS node: the
        # container's own mount has root "/", and the /host/sys/fs/cgroup
        # hostPath mount (whose mountpoint merely CONTAINS "/sys/fs/cgroup")
        # has a namespace-relative "/.." chain as its root. Matching either
        # would bind the watchdog to a garbage path and starve the fallback.
        pod_slice = self._make_pod_slice()
        mountinfo = self._write_mountinfo(
            "13488 13487 0:29 / /sys/fs/cgroup ro,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw,seclabel\n"
            "13496 13482 0:29 /../../../.. /host/sys/fs/cgroup ro,relatime - cgroup2 cgroup2 rw,seclabel\n"
        )
        found = find_pod_cgroup_dir(self.UID, host_root=self.host_root,
                                    mountinfo_path=mountinfo)
        self.assertEqual(found, pod_slice)

    def test_private_cgroupns_without_uid_returns_none(self):
        self._make_pod_slice()
        mountinfo = self._write_mountinfo(
            "13488 13487 0:29 / /sys/fs/cgroup ro - cgroup2 cgroup rw\n"
            "13496 13482 0:29 /../../../.. /host/sys/fs/cgroup ro - cgroup2 cgroup2 rw\n"
        )
        self.assertIsNone(find_pod_cgroup_dir(None, host_root=self.host_root,
                                              mountinfo_path=mountinfo))


# ---------------------------------------------------------------------------
# Main-loop state machine (pending supervision, rollback, circuit breaker)
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeApi:
    """In-memory stand-in for PodApi; records every side effect."""

    def __init__(self, spec_limit: str = "2Gi", spec_requests: str = None):
        self.spec_limit = spec_limit
        self.spec_requests = spec_requests
        self.container_id = "containerd://app123"
        self.resize_pending = (None, "")
        self.patches = []
        self.request_patches = []
        self.events = []
        self.patch_error = None
        self.get_pod_error = None

    def get_pod(self):
        if self.get_pod_error:
            raise self.get_pod_error
        return object()

    def get_container_limit(self, pod):
        return self.spec_limit

    def get_container_requests(self, pod):
        return self.spec_requests

    def get_container_id(self, pod):
        return self.container_id

    def get_resize_pending(self, pod):
        return self.resize_pending

    def patch_limits(self, mem_str, requests_str=None):
        if self.patch_error:
            raise self.patch_error
        self.patches.append(mem_str)
        self.request_patches.append(requests_str or mem_str)
        self.spec_limit = mem_str
        self.spec_requests = requests_str or mem_str

    def annotate(self, key, value):
        pass

    def emit_event(self, reason, message, event_type="Normal"):
        self.events.append((reason, event_type))


class FakeNotifier:
    def __init__(self):
        self.cards = []

    def notify(self, event_key, title, color, markdown):
        self.cards.append(event_key)


def make_watchdog(read_fn, api=None, clock=None, host=(32 * GIB, 20 * GIB),
                  find_dir=None, baseline=2 * GIB, baseline_requests=None,
                  current_requests=None, **cfg_overrides):
    api = api or FakeApi()
    clock = clock or FakeClock()
    wd = watchdog.Watchdog(
        make_cfg(**cfg_overrides), api, FakeNotifier(), watchdog.Metrics(),
        watchdog.Heartbeat(),
        target_dir="/fake/pod/app.scope", pod_slice="/fake/pod",
        baseline=baseline, baseline_requests=baseline_requests,
        current_requests=current_requests,
        fatal_fn=lambda msg: (_ for _ in ()).throw(SystemExit(msg)),
        now_fn=clock.now, sleep_fn=clock.sleep,
        read_cgroup=read_fn, host_stats=lambda: host,
        find_container_dir=find_dir or (lambda pod_slice, cid=None: None),
        jitter_fn=lambda: 0.0,
    )
    return wd, api, clock


class TestWatchdogStateMachine(unittest.TestCase):
    def test_scale_up_patches_then_pending_resolves(self):
        state = {"ws": int(1.7 * GIB), "max": 2 * GIB}
        wd, api, clock = make_watchdog(lambda p: (state["ws"], state["max"]))
        wd.tick()
        self.assertEqual(api.patches, ["6Gi"])
        self.assertIsNotNone(wd.pending)
        self.assertFalse(wd.pending["is_down"])
        # kubelet applies the resize -> supervision resolves the pending state
        state["max"] = 6 * GIB
        clock.advance(0.1)
        wd.tick()
        self.assertIsNone(wd.pending)
        self.assertIn(("ResizeApplied", "Normal"), api.events)

    def test_pending_infeasible_rolls_back_and_opens_circuit(self):
        wd, api, clock = make_watchdog(lambda p: (int(1.9 * GIB), 2 * GIB))
        wd.pending = {"target": 6 * GIB, "is_down": False,
                      "started": clock.now(), "last_check": 0.0}
        wd.spec_limit = 6 * GIB
        api.resize_pending = ("Infeasible", "no allocatable")
        wd.tick()
        self.assertIsNone(wd.pending)
        self.assertEqual(api.patches, ["2Gi"])  # spec rolled back to enforced
        self.assertEqual(wd.spec_limit, 2 * GIB)
        self.assertGreater(wd.circuit_open_until, clock.now())
        self.assertIn(("ResizeFailed", "Warning"), api.events)

    def test_pending_timeout_rolls_back(self):
        wd, api, clock = make_watchdog(lambda p: (int(1.9 * GIB), 2 * GIB))
        wd.pending = {"target": 6 * GIB, "is_down": False,
                      "started": clock.now(), "last_check": 0.0}
        wd.spec_limit = 6 * GIB
        clock.advance(61)  # > pending_timeout (60s)
        wd.tick()
        self.assertIsNone(wd.pending)
        self.assertEqual(api.patches, ["2Gi"])
        self.assertGreater(wd.circuit_open_until, clock.now())

    def test_burst_withdraws_inflight_scale_down(self):
        # A scale-down PATCH is pending, then memory shoots past the scale-up
        # watermark: the shrink must be withdrawn immediately, not ride out
        # the 60s pending timeout while blocking OOM rescue.
        wd, api, clock = make_watchdog(lambda p: (int(5.5 * GIB), 6 * GIB))
        wd.pending = {"target": 2 * GIB, "is_down": True,
                      "started": clock.now(), "last_check": clock.now()}
        wd.spec_limit = 2 * GIB
        wd.tick()
        self.assertIsNone(wd.pending)
        self.assertEqual(api.patches, ["6Gi"])  # re-declared enforced limit
        self.assertEqual(wd.spec_limit, 6 * GIB)
        self.assertIn(("ScaleDownWithdrawn", "Normal"), api.events)
        # circuit stays closed: the very next tick may scale up
        self.assertLessEqual(wd.circuit_open_until, clock.now())

    def test_circuit_open_blocks_scale_up(self):
        wd, api, clock = make_watchdog(lambda p: (int(1.9 * GIB), 2 * GIB))
        wd.circuit_open_until = clock.now() + 300
        wd.tick()
        self.assertEqual(api.patches, [])
        self.assertIsNone(wd.pending)

    def test_patch_failure_opens_short_circuit(self):
        wd, api, clock = make_watchdog(lambda p: (int(1.9 * GIB), 2 * GIB))
        api.patch_error = RuntimeError("connection reset")
        wd.tick()
        self.assertEqual(api.patches, [])
        self.assertIsNone(wd.pending)
        self.assertGreater(wd.circuit_open_until, clock.now())

    def test_enoent_relocates_to_new_cgroup(self):
        # The target container restarted: its old scope is gone and a new one
        # exists; the watchdog must rebind within one tick instead of erroring
        # until the consecutive-failure limit restarts the sidecar.
        def read(path):
            if path == "/fake/pod/new.scope":
                return 1 * GIB, 4 * GIB
            raise FileNotFoundError(path)

        wd, api, clock = make_watchdog(
            read, find_dir=lambda pod_slice, cid=None: "/fake/pod/new.scope")
        wd.tick()  # relocates
        self.assertEqual(wd.target_dir, "/fake/pod/new.scope")
        self.assertIn(("CgroupRelocated", "Normal"), api.events)
        clock.advance(0.1)
        wd.tick()  # now reads the new scope without raising

    def test_enoent_without_replacement_raises(self):
        def read(path):
            raise FileNotFoundError(path)

        wd, api, clock = make_watchdog(read)  # find_dir returns None
        with self.assertRaises(FileNotFoundError):
            wd.tick()

    def test_scale_up_adopts_inflight_scale_down_instead_of_shrinking(self):
        # Spec was shrunk to 2Gi (own pre-restart scale-down or an external
        # actor) while the cgroup still enforces 8Gi, and a burst pushes the
        # working set past the watermark. Computing a "scale-up" from the
        # 2Gi spec would patch 6Gi -- BELOW the enforced 8Gi and below the
        # 7Gi working set, shrinking the container under memory pressure and
        # causing the very OOM the watchdog exists to prevent. It must adopt
        # the in-flight shrink instead; the withdraw path then rescues.
        api = FakeApi(spec_limit="2Gi")
        wd, api, clock = make_watchdog(lambda p: (7 * GIB, 8 * GIB), api=api)
        wd.tick()
        self.assertEqual(api.patches, [])
        self.assertIsNotNone(wd.pending)
        self.assertEqual(wd.pending["target"], 2 * GIB)
        self.assertTrue(wd.pending["is_down"])
        clock.advance(0.1)
        wd.tick()  # pressure is critical: the adopted shrink is withdrawn
        self.assertEqual(api.patches, ["8Gi"])
        clock.advance(0.1)
        wd.tick()  # and the normal rescue path takes over
        self.assertEqual(api.patches[-1], "12Gi")

    def test_pending_resolves_for_non_mi_aligned_quantities(self):
        # A decimal baseline such as "1500M" is not Mi-aligned. The patched
        # quantity must be canonicalized so pending["target"] matches what
        # the kernel ends up enforcing; otherwise supervision can never see
        # equality and every scale-down "fails" with a spurious rollback,
        # high-severity alert and 10-minute circuit break.
        baseline = 1500 * 1000 * 1000  # "1500M"
        state = {"ws": 512 * MIB, "max": 6 * GIB}
        api = FakeApi(spec_limit="6Gi")
        wd, api, clock = make_watchdog(lambda p: (state["ws"], state["max"]),
                                       api=api, baseline=baseline)
        wd.spec_limit = 6 * GIB
        wd.tick()  # starts the low-watermark cooldown
        clock.advance(181)
        wd.tick()
        self.assertEqual(api.patches, ["1430Mi"])  # 1.5e9 floored to 1Mi
        state["max"] = 1430 * MIB  # kubelet applies exactly that
        clock.advance(0.1)
        wd.tick()
        self.assertIsNone(wd.pending)
        self.assertIn(("ResizeApplied", "Normal"), api.events)

    def test_spec_read_failure_alerts_and_counts(self):
        # Losing the API server while memory is critical means OOM rescue is
        # inoperative; that must surface as a metric and a notification, not
        # just a log line.
        api = FakeApi()
        api.get_pod_error = RuntimeError("apiserver down")
        wd, api, clock = make_watchdog(lambda p: (int(1.9 * GIB), 2 * GIB), api=api)
        wd.tick()
        self.assertEqual(api.patches, [])
        self.assertIn("spec-read-failed", wd.notifier.cards)
        self.assertIn("watchdog_spec_read_errors_total", wd.metrics.render())

    def test_withdraw_patch_failure_keeps_supervising(self):
        # Regression guard for existing behavior: when withdrawing an
        # in-flight scale-down fails, the pending state must survive (still
        # bounded by pending_timeout) so a later tick can retry.
        wd, api, clock = make_watchdog(lambda p: (int(5.5 * GIB), 6 * GIB))
        wd.pending = {"target": 2 * GIB, "is_down": True,
                      "started": clock.now(), "last_check": clock.now()}
        wd.spec_limit = 2 * GIB
        api.patch_error = RuntimeError("connection reset")
        wd.tick()
        self.assertIsNotNone(wd.pending)
        api.patch_error = None
        clock.advance(0.1)
        wd.tick()  # retry succeeds
        self.assertIsNone(wd.pending)
        self.assertEqual(api.patches, ["6Gi"])
        self.assertIn(("ScaleDownWithdrawn", "Normal"), api.events)

    def test_adopts_external_inflight_scale_up(self):
        # Spec says 8Gi (external actor / pre-restart patch) while the cgroup
        # still enforces 2Gi: supervise the in-flight resize, don't re-patch.
        api = FakeApi(spec_limit="8Gi")
        wd, api, clock = make_watchdog(lambda p: (int(1.9 * GIB), 2 * GIB), api=api)
        wd.tick()
        self.assertEqual(api.patches, [])
        self.assertIsNotNone(wd.pending)
        self.assertEqual(wd.pending["target"], 8 * GIB)

    def test_scale_down_after_cooldown(self):
        api = FakeApi(spec_limit="6Gi")
        wd, api, clock = make_watchdog(lambda p: (512 * MIB, 6 * GIB), api=api)
        wd.spec_limit = 6 * GIB
        wd.tick()  # starts the low-watermark cooldown
        self.assertIsNotNone(wd.low_since)
        self.assertEqual(api.patches, [])
        clock.advance(181)  # > scale_down_cooldown (180s), also since start
        wd.tick()
        self.assertEqual(api.patches, ["2Gi"])  # max(baseline, 2x ws aligned)
        self.assertTrue(wd.pending["is_down"])

    def test_scale_down_adopts_external_resize_instead_of_overwriting(self):
        # Cooldown elapsed, but the spec was externally raised to 8Gi while
        # the cgroup still enforces 6Gi: the shrink must yield to supervision.
        api = FakeApi(spec_limit="8Gi")
        wd, api, clock = make_watchdog(lambda p: (512 * MIB, 6 * GIB), api=api)
        wd.spec_limit = 6 * GIB
        wd.tick()
        clock.advance(181)
        wd.tick()
        self.assertEqual(api.patches, [])
        self.assertIsNotNone(wd.pending)
        self.assertEqual(wd.pending["target"], 8 * GIB)
        self.assertFalse(wd.pending["is_down"])


class TestResolveMaxLimit(unittest.TestCase):
    """Cap derivation: the cap is ALWAYS factor x baseline (no absolute cap
    exists), so one chart-wide parameter adapts to every tenant's limits
    tier. Runtime borrowing is still bounded by the host ceiling and kubelet
    allocatable admission."""

    def test_factor_scales_baseline(self):
        cfg = make_cfg(max_factor=2.0)
        self.assertEqual(watchdog.resolve_max_limit(cfg, 4 * GIB), 8 * GIB)
        self.assertEqual(watchdog.resolve_max_limit(cfg, 16 * GIB), 32 * GIB)

    def test_factor_below_one_clamps_to_baseline(self):
        # factor < 1 (including an explicit 0) would put the cap under the
        # starting limit; never shrink, stay inert-but-loud instead.
        self.assertEqual(watchdog.resolve_max_limit(make_cfg(max_factor=0.5), 4 * GIB), 4 * GIB)
        self.assertEqual(watchdog.resolve_max_limit(make_cfg(max_factor=0.0), 4 * GIB), 4 * GIB)

    def test_result_is_mi_aligned_and_never_below_baseline(self):
        baseline = 1500 * 1000 * 1000  # non-Mi-aligned decimal quantity
        cap = watchdog.resolve_max_limit(make_cfg(max_factor=1.5), baseline)
        self.assertEqual(cap % MIB, 0)
        self.assertGreaterEqual(cap, baseline)

    def test_load_config_reads_factor_env_with_default(self):
        os.environ["WATCHDOG_MAX_FACTOR"] = "2.5"
        try:
            self.assertEqual(watchdog.load_config().max_factor, 2.5)
        finally:
            del os.environ["WATCHDOG_MAX_FACTOR"]
        self.assertEqual(watchdog.load_config().max_factor, 2.0)  # code default


class TestBaselineRequestsRestore(unittest.TestCase):
    """Prod tenants oversubscribe (requests 500Mi vs limits 8Gi). Because a
    resize always moves requests together with limits (scheduler accounting
    while borrowing), returning to baseline must also restore the ORIGINAL
    requests -- otherwise one burst permanently locks baseline-sized
    allocatable per pod and the oversubscription economics are destroyed."""

    def test_scale_down_to_baseline_restores_original_requests(self):
        api = FakeApi(spec_limit="8Gi")
        wd, api, clock = make_watchdog(lambda p: (512 * MIB, 8 * GIB), api=api,
                                       baseline=4 * GIB, baseline_requests="500Mi")
        wd.spec_limit = 8 * GIB
        wd.tick()  # starts the low-watermark cooldown
        clock.advance(181)
        wd.tick()
        self.assertEqual(api.patches, ["4Gi"])
        self.assertEqual(api.request_patches, ["500Mi"])

    def test_intermediate_scale_down_keeps_requests_equal_limits(self):
        # Still above baseline afterwards: the pod is still borrowing, so the
        # borrowed memory must stay visible to the scheduler.
        api = FakeApi(spec_limit="8Gi")
        wd, api, clock = make_watchdog(lambda p: (int(1.5 * GIB), 8 * GIB), api=api,
                                       baseline=2 * GIB, baseline_requests="500Mi")
        wd.spec_limit = 8 * GIB
        wd.tick()
        clock.advance(181)
        wd.tick()
        self.assertEqual(api.patches, ["3Gi"])      # 2x working set > baseline
        self.assertEqual(api.request_patches, ["3Gi"])

    def test_scale_up_keeps_requests_equal_limits(self):
        wd, api, clock = make_watchdog(lambda p: (int(1.7 * GIB), 2 * GIB),
                                       baseline_requests="500Mi")
        wd.tick()
        self.assertEqual(api.patches, ["6Gi"])
        self.assertEqual(api.request_patches, ["6Gi"])

    def test_invalid_baseline_requests_above_baseline_is_ignored(self):
        # requests must never exceed limits; a corrupt annotation above the
        # baseline limit falls back to requests == limits behaviour.
        api = FakeApi(spec_limit="8Gi")
        wd, api, clock = make_watchdog(lambda p: (512 * MIB, 8 * GIB), api=api,
                                       baseline=4 * GIB, baseline_requests="6Gi")
        wd.spec_limit = 8 * GIB
        wd.tick()
        clock.advance(181)
        wd.tick()
        self.assertEqual(api.patches, ["4Gi"])
        self.assertEqual(api.request_patches, ["4Gi"])


class TestRequestsRestoreAfterFailedScaleUp(unittest.TestCase):
    """A scale-up from baseline writes requests = limits (10Gi/10Gi); when it
    fails, the rollback lands back on the baseline limit but deliberately
    KEEPS requests = limits (the pod genuinely holds that much memory, and
    honest requests protect it in eviction ranking). The scale-down
    low-watermark machinery must then restore the original requests once the
    pressure is gone -- even though the limit never left baseline."""

    def _high_pressure_then_rollback(self):
        # baseline 8Gi / requests 500Mi; working set at ~86% triggers the borrow
        state = {"ws": int(6.9 * GIB), "max": 8 * GIB}
        wd, api, clock = make_watchdog(lambda p: (state["ws"], state["max"]),
                                       api=FakeApi(spec_limit="8Gi"),
                                       baseline=8 * GIB, baseline_requests="500Mi")
        wd.spec_limit = 8 * GIB
        wd.tick()  # scale-up 8Gi -> 12Gi accepted, requests moved to 12Gi
        self.assertEqual(api.patches, ["12Gi"])
        self.assertEqual(api.request_patches, ["12Gi"])
        api.resize_pending = ("Infeasible", "no allocatable")
        clock.advance(0.1)
        wd.tick()  # rollback: limit back to 8Gi, requests STAY at 8Gi
        self.assertEqual(api.patches, ["12Gi", "8Gi"])
        self.assertEqual(api.request_patches, ["12Gi", "8Gi"])
        return state, wd, api, clock

    def test_low_watermark_restores_requests_after_rollback(self):
        state, wd, api, clock = self._high_pressure_then_rollback()
        state["ws"] = 1 * GIB  # pressure gone: 12.5% of the 8Gi limit
        clock.advance(200)     # past the since-last-scale-up guard
        wd.tick()              # starts the low-watermark cooldown
        clock.advance(181)
        wd.tick()              # cooldown elapsed -> requests-only restore
        self.assertEqual(api.patches[-1], "8Gi")          # limit unchanged
        self.assertEqual(api.request_patches[-1], "500Mi")
        self.assertIn(("RequestsRestored", "Normal"), api.events)
        # the pending resolves immediately (kernel limit already equals target)
        clock.advance(0.1)
        wd.tick()
        self.assertIsNone(wd.pending)

    def test_no_restore_while_usage_stays_high(self):
        # The user-visible semantics: while the pod genuinely uses the memory,
        # the inflated requests are honest and must stay.
        state, wd, api, clock = self._high_pressure_then_rollback()
        state["ws"] = 5 * GIB  # 62.5% of limit: above the 40% watermark
        clock.advance(200)
        wd.tick()
        clock.advance(400)
        wd.tick()
        self.assertEqual(api.patches, ["12Gi", "8Gi"])  # nothing new
        self.assertIsNone(wd.low_since)

    def test_restart_recovers_inflated_requests_from_spec(self):
        # Sidecar restarted after the rollback: in-memory tracking is gone,
        # but the pod spec still says requests == 8Gi while the annotation
        # says 500Mi. Startup wiring passes the spec value in.
        api = FakeApi(spec_limit="8Gi")
        wd, api, clock = make_watchdog(lambda p: (1 * GIB, 8 * GIB), api=api,
                                       baseline=8 * GIB, baseline_requests="500Mi",
                                       current_requests="8Gi")
        wd.spec_limit = 8 * GIB
        wd.tick()
        clock.advance(181)
        wd.tick()
        self.assertEqual(api.patches, ["8Gi"])
        self.assertEqual(api.request_patches, ["500Mi"])

    def test_decide_scale_down_requests_inflated_branch(self):
        cfg = make_cfg()
        base = dict(cgroup_max=8 * GIB, spec_limit=8 * GIB, baseline=8 * GIB,
                    host_total=None, host_available=None)
        low = watchdog.Sample(working_set=1 * GIB, requests_inflated=True, **base)
        d = watchdog.decide_scale_down(low, cfg)
        self.assertEqual(d.action, watchdog.Action.SCALE_DOWN)
        self.assertEqual(d.target_bytes, 8 * GIB)
        high = watchdog.Sample(working_set=5 * GIB, requests_inflated=True, **base)
        self.assertEqual(watchdog.decide_scale_down(high, cfg).action,
                         watchdog.Action.NOOP)
        clean = watchdog.Sample(working_set=1 * GIB, requests_inflated=False, **base)
        self.assertEqual(watchdog.decide_scale_down(clean, cfg).action,
                         watchdog.Action.NOOP)


class TestPodApiPatchBody(unittest.TestCase):
    def test_patch_limits_wires_separate_requests(self):
        class FakeV1:
            def __init__(self):
                self.bodies = []

            def patch_namespaced_pod_resize(self, name, namespace, body,
                                            _request_timeout=None):
                self.bodies.append(body)

        v1 = FakeV1()
        api = watchdog.PodApi(v1, "pod-1", "ns-1")
        api.patch_limits("4Gi", "500Mi")
        res = v1.bodies[0]["spec"]["containers"][0]["resources"]
        self.assertEqual(res["limits"]["memory"], "4Gi")
        self.assertEqual(res["requests"]["memory"], "500Mi")
        api.patch_limits("6Gi")
        res = v1.bodies[1]["spec"]["containers"][0]["resources"]
        self.assertEqual(res["requests"]["memory"], "6Gi")


class TestMetricsRender(unittest.TestCase):
    def test_render_includes_help_and_type(self):
        metrics = watchdog.Metrics()
        metrics.inc("watchdog_blocked_total", {"reason": "host_full"})
        metrics.set_gauge("watchdog_working_set_bytes", 123.0)
        out = metrics.render()
        self.assertIn("# HELP watchdog_blocked_total", out)
        self.assertIn("# TYPE watchdog_blocked_total counter", out)
        self.assertIn("# TYPE watchdog_working_set_bytes gauge", out)
        self.assertIn('watchdog_blocked_total{reason="host_full"} 1', out)


class TestMetricsServer(unittest.TestCase):
    def test_bind_failure_raises(self):
        # The liveness probe depends on this endpoint; a failed bind must
        # propagate (main() turns it into fatal()) instead of leaving the
        # sidecar alive-but-unprobeable in a kubelet restart loop.
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("", 0))
        port = blocker.getsockname()[1]
        try:
            with self.assertRaises(OSError):
                watchdog.start_metrics_server(watchdog.Metrics(), port)
        finally:
            blocker.close()


class TestFeishuCooldown(unittest.TestCase):
    def test_queue_full_drop_does_not_consume_cooldown(self):
        n = watchdog.FeishuNotifier(cooldown_seconds=300.0)
        n.enabled = True
        n._queue = queue.Queue(maxsize=1)
        n._queue.put_nowait(("t", "c", "m"))  # fill the queue
        n.notify("k", "t", "red", "m")        # dropped: queue full
        n._queue.get_nowait()                 # queue drains
        n.notify("k", "t", "red", "m")        # must not be cooldown-suppressed
        self.assertEqual(n._queue.qsize(), 1)


class TestPodApiEvents(unittest.TestCase):
    class FakeEventsV1:
        def __init__(self):
            self.bodies = []

        def create_namespaced_event(self, namespace, body, _request_timeout=None):
            self.bodies.append(body)

    def _emit(self, pod_name="pod-1"):
        events = self.FakeEventsV1()
        api = watchdog.PodApi(None, pod_name, "ns-1", events_v1=events,
                              pod_uid="uid-42")
        api.emit_event("TestReason", "msg")
        return events.bodies[0]

    def test_emit_event_uses_injected_pod_uid(self):
        self.assertEqual(self._emit()["regarding"]["uid"], "uid-42")

    def test_generate_name_is_valid_rfc1123_prefix(self):
        # The API server validates metadata.generateName as a DNS-1123
        # subdomain, masking only a TRAILING DASH (deployment-style "foo-").
        # A trailing dot fails validation with 422 and every watchdog event
        # silently disappears -- exactly what happened on the first live
        # cluster. Guard the exact prefix rule here.
        import re
        gen = self._emit("heavy-worker-7c58d9445-ddm29")["metadata"]["generateName"]
        candidate = gen[:-1] + "a" if gen.endswith("-") else gen
        pattern = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
        self.assertRegex(candidate, pattern,
                         f"generateName {gen!r} is not a valid RFC1123 subdomain prefix")


class TestHostMemoryStats(unittest.TestCase):
    def test_parses_meminfo(self):
        with tempfile.NamedTemporaryFile("w", suffix="meminfo", delete=False) as f:
            f.write("MemTotal:       32000000 kB\n"
                    "MemFree:         1000000 kB\n"
                    "MemAvailable:   20000000 kB\n")
            path = f.name
        try:
            total, available = watchdog.get_host_memory_stats(path)
            self.assertEqual(total, 32000000 * 1024)
            self.assertEqual(available, 20000000 * 1024)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
