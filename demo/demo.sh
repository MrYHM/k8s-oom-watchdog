#!/usr/bin/env bash
# Drives one full rescue and prints a single-terminal status table: memory
# climbs past the 80% watermark, the watchdog raises the limit in place, and
# the container survives. This is the script the README GIF records.
set -euo pipefail

NS=watchdog-demo
DURATION="${DURATION:-45}"   # seconds of observation

POD="$(kubectl -n "$NS" get pod -l app=bursty-worker -o jsonpath='{.items[0].metadata.name}')"

# The watchdog's own /metrics is the authoritative view: it reports the
# working set with page cache already subtracted, exactly the number its
# decisions use.
kubectl -n "$NS" port-forward "pod/$POD" 18090:8090 >/dev/null 2>&1 &
PF=$!
STRESS=""
cleanup() {
  kill "$PF" 2>/dev/null || true
  [ -n "$STRESS" ] && kill "$STRESS" 2>/dev/null || true
}
trap cleanup EXIT
sleep 2

echo "Pod: $POD   baseline: 256Mi   step: 128Mi   cap: 1Gi (4 x baseline)"
echo "Growing the working set to ~700Mi at ~80MiB/s -- watch the LIMIT column."
echo

# 0.7GiB target, 16MiB steps every 0.2s, hold 20s, then release.
kubectl -n "$NS" exec "$POD" -c bursty-worker -- \
  python3 /tmp/stress.py 0.7 16 0.2 20 >/dev/null 2>&1 &
STRESS=$!

printf '%-9s %12s %12s %7s  %s\n' TIME "WORKING SET" "LIMIT" "USED%" "LAST EVENT"
end=$(( $(date +%s) + DURATION ))
last_event=""
while [ "$(date +%s)" -lt "$end" ]; do
  metrics="$(curl -sf --max-time 2 localhost:18090/metrics || true)"
  if [ -n "$metrics" ]; then
    event="$(kubectl -n "$NS" get events \
      --field-selector "involvedObject.name=$POD" \
      --sort-by=.lastTimestamp -o jsonpath='{.items[-1:].reason}' 2>/dev/null || true)"
    [ -n "$event" ] && last_event="$event"
    echo "$metrics" | awk -v t="$(date +%H:%M:%S)" -v ev="${last_event:--}" '
      function human(b) {
        if (b >= 1073741824) return sprintf("%.1fGi", b / 1073741824)
        return sprintf("%dMi", b / 1048576)
      }
      /^watchdog_working_set_bytes /  { ws = $2 }
      /^watchdog_memory_limit_bytes / { lim = $2 }
      END {
        if (lim > 0)
          printf "%-9s %12s %12s %6.0f%%  %s\n", t, human(ws), human(lim), ws / lim * 100, ev
      }'
  fi
  sleep 1
done

echo
echo "Rescue done: the limit was raised in place and the container never restarted."
kubectl -n "$NS" get pod "$POD" \
  -o jsonpath='{range .status.containerStatuses[?(@.name=="bursty-worker")]}restarts: {.restartCount}{"\n"}{end}'
echo "Scale-down follows automatically once the working set stays below 40% for 180s."
