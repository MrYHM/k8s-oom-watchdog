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
DEFAULT_IMAGE="ghcr.io/mryhm/k8s-oom-watchdog:edge"
IMAGE="${IMAGE:-$DEFAULT_IMAGE}"
PULL_POLICY="${PULL_POLICY:-Always}"

command -v kubectl >/dev/null || { echo "kubectl not found"; exit 1; }

minor="$(kubectl version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["minor"].rstrip("+"))')"
if [ "$minor" -lt 33 ]; then
  echo "This cluster is v1.${minor}; the pods/resize subresource needs >= 1.33." >&2
  exit 1
fi
echo "==> Cluster is v1.${minor}, in-place resize available."

echo "==> Applying the demo manifest (image: $IMAGE)"
sed -e "s|image: $DEFAULT_IMAGE|image: $IMAGE|" \
    -e "s|imagePullPolicy: Always|imagePullPolicy: $PULL_POLICY|" \
    "$HERE/watchdog-demo.yaml" | kubectl apply -f -

echo "==> Waiting for the pod to become ready (pulling two images)"
kubectl -n "$NS" wait --for=condition=Ready pod -l app=bursty-worker --timeout=300s

POD="$(kubectl -n "$NS" get pod -l app=bursty-worker -o jsonpath='{.items[0].metadata.name}')"
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
