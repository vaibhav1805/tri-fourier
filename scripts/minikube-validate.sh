#!/usr/bin/env bash
#
# minikube-validate.sh -- Deploy AutoTriage to Minikube and run smoke tests
#
# Usage:
#   ./scripts/minikube-validate.sh          # Full cycle: start -> build -> deploy -> test -> cleanup
#   ./scripts/minikube-validate.sh --no-cleanup   # Leave environment running after tests
#   ./scripts/minikube-validate.sh --skip-start   # Skip Minikube start (already running)
#   ./scripts/minikube-validate.sh --helm         # Deploy via Helm instead of kustomize
#
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NAMESPACE="autotriage"
IMAGE_NAME="autotriage"
IMAGE_TAG="test"
DEPLOY_METHOD="kustomize"  # kustomize or helm
CLEANUP=true
SKIP_START=false
TIMEOUT=120  # seconds to wait for pod readiness

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Counters
TESTS_PASSED=0
TESTS_FAILED=0
TESTS_TOTAL=0

# ── Argument Parsing ──────────────────────────────────────────────────────────
for arg in "$@"; do
  case $arg in
    --no-cleanup) CLEANUP=false ;;
    --skip-start) SKIP_START=true ;;
    --helm) DEPLOY_METHOD="helm" ;;
    --help|-h)
      echo "Usage: $0 [--no-cleanup] [--skip-start] [--helm]"
      echo ""
      echo "Options:"
      echo "  --no-cleanup   Leave Minikube and deployment running after tests"
      echo "  --skip-start   Skip Minikube start (assume already running)"
      echo "  --helm         Deploy via Helm chart instead of kustomize"
      exit 0
      ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

# ── Helper Functions ──────────────────────────────────────────────────────────
log_info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_fail()  { echo -e "${RED}[FAIL]${NC}  $*"; }
log_step()  { echo -e "\n${BLUE}━━━ $* ━━━${NC}"; }

assert() {
  local description="$1"
  shift
  TESTS_TOTAL=$((TESTS_TOTAL + 1))
  if "$@" >/dev/null 2>&1; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    log_ok "$description"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    log_fail "$description"
  fi
}

cleanup() {
  if [ "$CLEANUP" = true ]; then
    log_step "Cleanup"
    log_info "Deleting namespace $NAMESPACE..."
    kubectl delete namespace "$NAMESPACE" --ignore-not-found --timeout=60s 2>/dev/null || true
    log_info "Cleanup complete"
  else
    log_warn "Skipping cleanup (--no-cleanup). Resources remain in namespace: $NAMESPACE"
    log_info "To clean up manually: kubectl delete namespace $NAMESPACE"
  fi
}

check_prerequisites() {
  log_step "Checking Prerequisites"
  local missing=()

  command -v minikube >/dev/null 2>&1 || missing+=("minikube")
  command -v kubectl  >/dev/null 2>&1 || missing+=("kubectl")
  command -v docker   >/dev/null 2>&1 || missing+=("docker")

  if [ "$DEPLOY_METHOD" = "helm" ]; then
    command -v helm >/dev/null 2>&1 || missing+=("helm")
  fi

  if [ ${#missing[@]} -gt 0 ]; then
    log_fail "Missing required tools: ${missing[*]}"
    exit 1
  fi

  log_ok "All prerequisites found"
}

# ── Main Flow ─────────────────────────────────────────────────────────────────

echo -e "${BLUE}"
echo "  ╔═══════════════════════════════════════════════╗"
echo "  ║   AutoTriage Minikube Validation              ║"
echo "  ║   Deploy -> Smoke Test -> Report              ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo -e "${NC}"

check_prerequisites

# ── Step 1: Start Minikube ────────────────────────────────────────────────────
if [ "$SKIP_START" = false ]; then
  log_step "Starting Minikube"

  if minikube status --format='{{.Host}}' 2>/dev/null | grep -q "Running"; then
    log_info "Minikube is already running"
  else
    log_info "Starting Minikube with 4 CPUs and 8GB memory..."
    minikube start --cpus=4 --memory=8192 --driver=docker
  fi
else
  log_info "Skipping Minikube start (--skip-start)"
fi

assert "Minikube is running" minikube status

# ── Step 2: Build Docker Image in Minikube ────────────────────────────────────
log_step "Building Docker Image"

log_info "Configuring Docker to use Minikube's daemon..."
eval "$(minikube docker-env)"

log_info "Building $IMAGE_NAME:$IMAGE_TAG..."
docker build -t "$IMAGE_NAME:$IMAGE_TAG" "$PROJECT_DIR"

assert "Docker image built" docker image inspect "$IMAGE_NAME:$IMAGE_TAG"

# ── Step 3: Deploy to Minikube ────────────────────────────────────────────────
log_step "Deploying to Minikube ($DEPLOY_METHOD)"

# Clean up any previous deployment
kubectl delete namespace "$NAMESPACE" --ignore-not-found --timeout=60s 2>/dev/null || true
kubectl create namespace "$NAMESPACE" 2>/dev/null || true

if [ "$DEPLOY_METHOD" = "helm" ]; then
  helm upgrade --install autotriage "$PROJECT_DIR/deploy/helm/triagebot/" \
    --namespace "$NAMESPACE" \
    --set image.repository="$IMAGE_NAME" \
    --set image.tag="$IMAGE_TAG" \
    --set image.pullPolicy=Never \
    --set persistence.storageClass=standard \
    --set backup.bgsave.enabled=false \
    --set backup.s3Export.enabled=false \
    --wait --timeout="${TIMEOUT}s"
else
  # Patch deployment image for test and use standard storage class
  kubectl apply -k "$PROJECT_DIR/deploy/kubernetes/"

  # Patch for Minikube: use standard storage class and test image
  kubectl patch pvc autotriage-graph-pvc -n "$NAMESPACE" \
    --type='json' -p='[{"op":"replace","path":"/spec/storageClassName","value":"standard"}]' \
    2>/dev/null || true

  kubectl set image deployment/autotriage \
    autotriage="$IMAGE_NAME:$IMAGE_TAG" \
    -n "$NAMESPACE"

  kubectl patch deployment autotriage -n "$NAMESPACE" \
    --type='json' -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"Never"}]'
fi

# ── Step 4: Wait for Readiness ────────────────────────────────────────────────
log_step "Waiting for Pod Readiness (timeout: ${TIMEOUT}s)"

if kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=autotriage \
  -n "$NAMESPACE" \
  --timeout="${TIMEOUT}s" 2>/dev/null; then
  log_ok "Pod is ready"
else
  log_fail "Pod failed to become ready within ${TIMEOUT}s"
  log_info "Pod status:"
  kubectl get pods -n "$NAMESPACE" -o wide
  log_info "Pod events:"
  kubectl describe pod -l app.kubernetes.io/name=autotriage -n "$NAMESPACE" | tail -20
  log_info "Pod logs:"
  kubectl logs -l app.kubernetes.io/name=autotriage -n "$NAMESPACE" --tail=50 2>/dev/null || true
  cleanup
  exit 1
fi

# ── Step 5: Smoke Tests ──────────────────────────────────────────────────────
log_step "Running Smoke Tests"

# Get pod name
POD_NAME=$(kubectl get pod -n "$NAMESPACE" -l app.kubernetes.io/name=autotriage -o jsonpath='{.items[0].metadata.name}')
log_info "Testing pod: $POD_NAME"

# Test 1: Health endpoint returns 200
assert "Health endpoint returns 200" \
  kubectl exec -n "$NAMESPACE" "$POD_NAME" -- curl -sf http://localhost:8000/health

# Test 2: Health response contains "ok"
HEALTH_RESPONSE=$(kubectl exec -n "$NAMESPACE" "$POD_NAME" -- curl -sf http://localhost:8000/health 2>/dev/null || echo "")
assert "Health response contains status ok" \
  echo "$HEALTH_RESPONSE" | grep -q '"status":"ok"\|"status": "ok"'

# Test 3: Service is reachable via ClusterIP
assert "Service exists and has endpoints" \
  kubectl get endpoints autotriage -n "$NAMESPACE" -o jsonpath='{.subsets[0].addresses[0].ip}'

# Test 4: PVC is bound
assert "PVC is bound" \
  kubectl get pvc autotriage-graph-pvc -n "$NAMESPACE" -o jsonpath='{.status.phase}' | grep -q "Bound"

# Test 5: Graph data directory exists and is writable
assert "Graph data directory is writable" \
  kubectl exec -n "$NAMESPACE" "$POD_NAME" -- test -w /app/data/graph

# Test 6: Pod runs as non-root
RUNNING_USER=$(kubectl exec -n "$NAMESPACE" "$POD_NAME" -- id -u 2>/dev/null || echo "unknown")
assert "Pod runs as non-root (uid=1001)" \
  test "$RUNNING_USER" = "1001"

# Test 7: ServiceAccount is mounted
assert "ServiceAccount token is mounted" \
  kubectl exec -n "$NAMESPACE" "$POD_NAME" -- test -f /var/run/secrets/kubernetes.io/serviceaccount/token

# Test 8: RBAC allows pod listing
assert "RBAC: can list pods" \
  kubectl auth can-i list pods --as="system:serviceaccount:${NAMESPACE}:autotriage"

# Test 9: RBAC allows reading pod logs
assert "RBAC: can get pod logs" \
  kubectl auth can-i get pods/log --as="system:serviceaccount:${NAMESPACE}:autotriage"

# Test 10: Container resource limits are set
MEMORY_LIMIT=$(kubectl get pod "$POD_NAME" -n "$NAMESPACE" -o jsonpath='{.spec.containers[0].resources.limits.memory}' 2>/dev/null || echo "")
assert "Memory limit is configured" \
  test -n "$MEMORY_LIMIT"

# ── Step 6: Report ───────────────────────────────────────────────────────────
log_step "Test Results"

echo ""
echo -e "  Total:  $TESTS_TOTAL"
echo -e "  Passed: ${GREEN}$TESTS_PASSED${NC}"
echo -e "  Failed: ${RED}$TESTS_FAILED${NC}"
echo ""

if [ "$TESTS_FAILED" -eq 0 ]; then
  echo -e "${GREEN}  ALL TESTS PASSED${NC}"
else
  echo -e "${RED}  $TESTS_FAILED TEST(S) FAILED${NC}"
fi

echo ""
log_info "Deployment summary:"
kubectl get all -n "$NAMESPACE" 2>/dev/null
echo ""
kubectl get pvc -n "$NAMESPACE" 2>/dev/null

# ── Step 7: Cleanup ─────────────────────────────────────────────────────────
cleanup

# Exit with failure if any tests failed
if [ "$TESTS_FAILED" -gt 0 ]; then
  exit 1
fi
