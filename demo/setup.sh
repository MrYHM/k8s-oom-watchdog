#!/usr/bin/env bash
# Deploys the demo and waits until the watchdog is supervising the target
# container. Run once; then run ./demo.sh (that is the part worth recording).
set -euo pipefail

NS=watchdog-demo
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Override when the cluster cannot reach ghcr.io -- push the image to a
# registry your nodes can pull from and point IMAGE at it, e.g.
#   IMAGE=<account>.dkr.ecr.<region>.amazonaws.com/oom-watchdog:demo ./setup.sh
# PULL_POLICY=IfNotPresent is what you want for a locally loaded image
# (kind load / minikube image load).
# ARCH pins the demo to nodes of one architecture. Needed when your image
# is single-arch but the cluster has mixed nodes -- otherwise the pod can
# land on a node the image was never built for.
DEFAULT_IMAGE="ghcr.io/mryhm/k8s-oom-watchdog:edge"
IMAGE="${IMAGE:-$DEFAULT_IMAGE}"
PULL_POLICY="${PULL_POLICY:-Always}"
ARCH="${ARCH:-}"
# The target container only needs a python3 interpreter. Override when the
# cluster cannot reach Docker Hub -- the watchdog image itself is built on
# python:3.12-slim, so it doubles as the workload image.
DEFAULT_WORKLOAD_IMAGE="python:3.12-slim"
WORKLOAD_IMAGE="${WORKLOAD_IMAGE:-$DEFAULT_WORKLOAD_IMAGE}"

command -v kubectl >/dev/null || { echo "kubectl not found"; exit 1; }

minor="$(kubectl version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["minor"].rstrip("+"))')"
if [ "$minor" -lt 33 ]; then
  echo "This cluster is v1.${minor}; the pods/resize subresource needs >= 1.33." >&2
  exit 1
fi
echo "==> Cluster is v1.${minor}, in-place resize available."

echo "==> Applying the demo manifest (image: $IMAGE)"
sed -e "s|image: $DEFAULT_IMAGE|image: $IMAGE|" \
    -e "s|image: $DEFAULT_WORKLOAD_IMAGE|image: $WORKLOAD_IMAGE|" \
    -e "s|imagePullPolicy: Always|imagePullPolicy: $PULL_POLICY|" \
    "$HERE/watchdog-demo.yaml" | kubectl apply -f -

if [ -n "$ARCH" ]; then
  echo "==> Pinning the demo to $ARCH nodes"
  kubectl -n "$NS" patch deployment bursty-worker --type=merge \
    -p "{\"spec\":{\"template\":{\"spec\":{\"nodeSelector\":{\"kubernetes.io/arch\":\"$ARCH\"}}}}}"
fi

# rollout status, not `wait pod`: patching the deployment (ARCH) rolls out a
# new ReplicaSet, and a label selector during the handover can return the pod
# that is on its way out.
echo "==> Waiting for the rollout to settle (pulling images)"
kubectl -n "$NS" rollout status deployment/bursty-worker --timeout=300s

# Pick a pod that is Running and not being torn down: during a rollout (or
# right after deleting a pod) the selector also returns the one on its way
# out, and every later step would then fail with NotFound.
POD=""
for _ in $(seq 1 60); do
  POD="$(kubectl -n "$NS" get pod -l app=bursty-worker -o json | python3 -c '
import json, sys
items = json.load(sys.stdin).get("items", [])
live = [p for p in items
        if not p["metadata"].get("deletionTimestamp")
        and p.get("status", {}).get("phase") == "Running"
        and all(c.get("ready") for c in p.get("status", {}).get("containerStatuses", []) or [])]
print(live[0]["metadata"]["name"] if live else "")
')"
  [ -n "$POD" ] && break
  sleep 2
done
[ -n "$POD" ] || { echo "No running bursty-worker pod found" >&2; exit 1; }
echo "==> Pod: $POD"

echo "==> Copying the stress tool into the target container"
kubectl -n "$NS" cp "$HERE/../trigger_oom_test.py" "$POD:/tmp/stress.py" -c bursty-worker

echo "==> Waiting for the watchdog to locate the target cgroup"
for _ in $(seq 1 30); do
  if kubectl -n "$NS" logs "$POD" -c watchdog 2>/dev/null | grep -q "Watchdog started"; then
    break
  fi
  sleep 2
done
kubectl -n "$NS" logs "$POD" -c watchdog | tail -5

cat <<'EOF'

Ready. Now run the demo (this is the part to record):

    ./demo.sh

To tear everything down afterwards:

    kubectl delete -f watchdog-demo.yaml
EOF
