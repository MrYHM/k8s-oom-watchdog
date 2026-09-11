#!/usr/bin/env bash
# Drives one full rescue and prints a single-terminal status table: memory
# climbs past the 80% watermark, the watchdog raises the limit in place, and
# the container survives. This is the script the README GIF records.
set -euo pipefail

NS=watchdog-demo
DURATION="${DURATION:-30}"   # seconds of observation

POD="$(kubectl -n "$NS" get pod -l app=bursty-worker -o json | python3 -c '
import json, sys
items = json.load(sys.stdin).get("items", [])
live = [p for p in items
        if not p["metadata"].get("deletionTimestamp")
        and p.get("status", {}).get("phase") == "Running"]
print(live[0]["metadata"]["name"] if live else "")
')"
[ -n "$POD" ] || { echo "No running bursty-worker pod; run ./setup.sh first" >&2; exit 1; }

# The watchdog's own /metrics is the authoritative view: it reports the
# working set with page cache already subtracted, exactly the number its
# decisions use.
TMPD="$(mktemp -d)"
kubectl -n "$NS" port-forward "pod/$POD" 18090:8090 >/dev/null 2>&1 &
PF=$!
STRESS=""
cleanup() {
  rm -rf "$TMPD"
  kill "$PF" 2>/dev/null || true
  [ -n "$STRESS" ] && kill "$STRESS" 2>/dev/null || true
}
trap cleanup EXIT
sleep 2

echo "Pod: $POD   baseline: 256Mi   step: 192Mi   cap: 1Gi (4 x baseline)"
echo "Growing the working set to ~660Mi at ~46MiB/s -- watch REQUESTS and LIMIT."
echo

# 0.65GiB target, 16MiB steps every 0.35s (~46MiB/s), hold 6s, then release.
# The rate is deliberately below the rescue path's throughput: at 80MiB/s a
# 192Mi step buys under 2.5s, which is inside the detect + PATCH + kubelet
# apply latency, and the container loses the race often enough to matter.
kubectl -n "$NS" exec "$POD" -c bursty-worker -- \
  python3 /tmp/stress.py 0.65 16 0.35 6 >/dev/null 2>&1 &
STRESS=$!

printf '%-9s %11s %10s %12s %6s %9s  %s\n' \
  TIME "WORKING SET" "REQUESTS" "LIMIT" "USED%" "RESTARTS" "LAST EVENT"
end=$(( $(date +%s) + DURATION ))
last_event=""
prev_spec=""
while [ "$(date +%s)" -lt "$end" ]; do
  # Two API reads per tick, fired together so sampling stays ~1s: the pod spec
  # carries requests/limits (they move in lockstep -- borrowed memory has to be
  # visible to the scheduler) plus the restart count, while the working set
  # comes from the watchdog's own metrics, page cache already subtracted.
  kubectl -n "$NS" get pod "$POD" -o json >"$TMPD/pod.json" 2>/dev/null &
  p_spec=$!
  kubectl -n "$NS" get events --field-selector "involvedObject.name=$POD" \
    --sort-by=.lastTimestamp -o jsonpath='{.items[-1:].reason}' \
    >"$TMPD/evt.txt" 2>/dev/null &
  p_evt=$!
  metrics="$(curl -sf --max-time 2 localhost:18090/metrics || true)"
  # Wait on these two only -- a bare `wait` would also block on the
  # port-forward and the stress job, neither of which ever exits.
  wait "$p_spec" "$p_evt" 2>/dev/null || true

  event="$(cat "$TMPD/evt.txt" 2>/dev/null || true)"
  [ -n "$event" ] && last_event="$event"

  spec="$(python3 -c '
import json, sys
try:
    pod = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
c = next(c for c in pod["spec"]["containers"] if c["name"] == "bursty-worker")
res = c.get("resources", {})
st = next((s for s in pod.get("status", {}).get("containerStatuses", [])
           if s["name"] == "bursty-worker"), {})
print(res.get("requests", {}).get("memory", "?"),
      res.get("limits", {}).get("memory", "?"),
      st.get("restartCount", 0))
' "$TMPD/pod.json" 2>/dev/null || true)"
  [ -z "$spec" ] && { sleep 1; continue; }
  set -- $spec
  req="$1"; lim_spec="$2"; restarts="$3"

  if [ -n "$metrics" ]; then
    out="$(echo "$metrics" | awk -v t="$(date +%H:%M:%S)" -v ev="${last_event:--}" \
                                 -v prev="$prev_spec" -v req="$req" \
                                 -v lim_spec="$lim_spec" -v rs="$restarts" '
      function human(b) {
        if (b >= 1073741824) return sprintf("%.1fGi", b / 1073741824)
        return sprintf("%dMi", b / 1048576)
      }
      /^watchdog_working_set_bytes /  { ws = $2 }
      /^watchdog_memory_limit_bytes / { lim = $2 }
      END {
        if (lim > 0) {
          # Flag against the spec value shown in the columns, not the kernel
          # value: the spec moves first and the mark would trail a row.
          mark = (prev != "" && lim_spec != prev) ? " <<" : "   "
          printf "%s\t%-9s %11s %10s %9s%s %5.0f%% %9s  %s\n", \
                 lim_spec, t, human(ws), req, lim_spec, mark, ws / lim * 100, rs, ev
        }
      }')"
    if [ -n "$out" ]; then
      printf '%s\n' "${out#*	}"
      prev_spec="${out%%	*}"
    fi
  fi
  sleep 1
done

echo
echo "Rescue done: the limit was raised in place. Verify the container never"
echo "restarted -- straight from the pod status, nothing the watchdog reports:"
echo
# Echo the command before running it: the number on screen should be
# reproducible by anyone watching, not taken on trust.
echo "  \$ kubectl -n $NS get pod $POD \\"
echo "      -o jsonpath='{.status.containerStatuses[?(@.name==\"bursty-worker\")].restartCount}'"
printf '  '
kubectl -n "$NS" get pod "$POD" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="bursty-worker")].restartCount}'
echo "   <- restarts"
echo
echo "Scale-down follows automatically once the working set stays below 40% for 180s."
