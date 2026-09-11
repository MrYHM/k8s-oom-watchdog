#!/usr/bin/env python3
# Copyright 2026 HY
# SPDX-License-Identifier: Apache-2.0
"""Memory stress tool for watchdog scale-up drills.

Grows a real (page-touched) allocation inside the heavy-worker container
until the target size, holds it, then releases -- driving the working set
through the watchdog's 80% watermark so the in-place resize path can be
observed end to end.

Usage (inside the target container, see the README's "Stress drill"):

    # Some app images manage Python with uv; the interpreter is not on the
    # exec PATH, so use the full venv path:
    python3 trigger_oom_test.py <target_gb> <step_mb> <interval_s> <hold_s>
    python3 trigger_oom_test.py 24 100 1 30
"""

import logging
import sys
import time
from typing import List

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("oom-drill")

MIB = 1024 ** 2


def grow(target_gb: float, step_mb: int, interval_s: float, hold_s: float) -> None:
    target_bytes = int(target_gb * 1024 * MIB)
    step_bytes = step_mb * MIB
    blocks: List[bytearray] = []
    allocated = 0

    logger.info("Growing to %.1fGiB in %dMiB steps every %.1fs, then holding %.1fs.",
                target_gb, step_mb, interval_s, hold_s)
    while allocated < target_bytes:
        # bytearray zero-fills, so every page is actually touched and the
        # allocation lands in the cgroup working set (not lazy mappings).
        blocks.append(bytearray(step_bytes))
        allocated += step_bytes
        logger.info("Allocated %.1fGiB / %.1fGiB", allocated / 1024 / MIB, target_gb)
        time.sleep(interval_s)

    logger.info("Target reached; holding for %.1fs...", hold_s)
    time.sleep(hold_s)

    blocks.clear()
    logger.info("Released. Watch the watchdog scale back down after its cooldown.")


def main() -> None:
    if len(sys.argv) != 5:
        logger.error("Usage: %s <target_gb> <step_mb> <interval_s> <hold_s>", sys.argv[0])
        sys.exit(2)
    grow(float(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]))


if __name__ == "__main__":
    main()
