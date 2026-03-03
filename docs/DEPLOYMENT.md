# AutoTriage Deployment Guide

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Local Development (Docker Compose)](#local-development-docker-compose)
3. [Minikube Testing](#minikube-testing)
4. [Production Kubernetes Deployment](#production-kubernetes-deployment)
5. [Helm Chart Deployment](#helm-chart-deployment)
6. [Configuration Management](#configuration-management)
7. [Backup and Restore](#backup-and-restore)
8. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- Docker 24+ and Docker Compose v2
- Python 3.12+
- kubectl 1.28+
- Helm 3.14+ (for Helm deployments)
- Minikube 1.32+ (for local K8s testing)
- AWS CLI v2 (for S3 backup/restore)

---

## Local Development (Docker Compose)

### Quick Start

```bash
cd projects/triagebot

# Create .env from template
cp .env.example .env
# Edit .env with your credentials

# Build and start
docker compose up --build

# Verify health
curl http://localhost:8000/health
```

### Environment Variables

Create a `.env` file in the project root:

```bash
# LLM Provider
MODEL_PROVIDER=bedrock
MODEL_ID=us.anthropic.claude-sonnet-4-6-v1:0

# AWS (for Bedrock + CloudWatch)
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret

# Slack (optional for local dev)
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...

# Observability (optional)
LOG_LEVEL=debug
```

### Data Persistence

Graph data is stored in the `autotriage-graph` Docker volume. Data survives container restarts but not `docker compose down -v`.

```bash
# Inspect volume
docker volume inspect triagebot_autotriage-graph

# Backup volume to local directory
docker run --rm -v triagebot_autotriage-graph:/data -v $(pwd)/backup:/backup \
  alpine tar czf /backup/graph-backup.tar.gz -C /data .

# Restore from backup
docker run --rm -v triagebot_autotriage-graph:/data -v $(pwd)/backup:/backup \
  alpine sh -c "cd /data && tar xzf /backup/graph-backup.tar.gz"
```

---

## Minikube Testing

### Setup

```bash
# Start Minikube with sufficient resources
minikube start --cpus=4 --memory=8192 --driver=docker

# Build image inside Minikube's Docker
eval $(minikube docker-env)
docker build -t autotriage:latest .

# Apply manifests
kubectl apply -k deploy/kubernetes/

# Check deployment status
kubectl get all -n autotriage

# Wait for pod to be ready
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=autotriage \
  -n autotriage --timeout=120s
```

### Accessing the Service

```bash
# Port forward
kubectl port-forward svc/autotriage 8000:80 -n autotriage

# Test health endpoint
curl http://localhost:8000/health
```

### View Logs

```bash
kubectl logs -f deployment/autotriage -n autotriage
```

### Clean Up

```bash
kubectl delete -k deploy/kubernetes/
minikube stop
```

---

## Production Kubernetes Deployment

### Using Raw Manifests

1. **Update the ConfigMap** with production values:
   ```bash
   # Edit deploy/kubernetes/configmap.yaml
   # Set PROMETHEUS_URL, OTEL endpoint, LOG_LEVEL, etc.
   ```

2. **Create secrets** (use External Secrets or CSI driver in production):
   ```bash
   kubectl create secret generic autotriage-secrets \
     --from-literal=SLACK_BOT_TOKEN=xoxb-... \
     --from-literal=SLACK_APP_TOKEN=xapp-... \
     --from-literal=SLACK_SIGNING_SECRET=... \
     -n autotriage
   ```

3. **Update the image** in deployment.yaml to your registry:
   ```yaml
   image: ghcr.io/your-org/autotriage:v0.1.0
   ```

4. **Apply**:
   ```bash
   kubectl apply -k deploy/kubernetes/
   ```

### Using Helm

See [Helm Chart Deployment](#helm-chart-deployment) below.

### Verifying Deployment

```bash
# Check pod status
kubectl get pods -n autotriage

# Check logs for startup errors
kubectl logs -f deployment/autotriage -n autotriage

# Verify health endpoint
kubectl exec -n autotriage deployment/autotriage -- \
  curl -sf http://localhost:8000/health

# Check RBAC
kubectl auth can-i list pods --as=system:serviceaccount:autotriage:autotriage
```

---

## Helm Chart Deployment

### Install

```bash
# From the project root
helm install autotriage deploy/helm/triagebot/ \
  --namespace autotriage \
  --create-namespace \
  -f deploy/helm/triagebot/values.yaml
```

### Environment Overrides

Create environment-specific values files:

```bash
# deploy/helm/values-staging.yaml
helm install autotriage deploy/helm/triagebot/ \
  --namespace autotriage-staging \
  --create-namespace \
  -f deploy/helm/triagebot/values.yaml \
  -f deploy/helm/values-staging.yaml
```

### Upgrade

```bash
helm upgrade autotriage deploy/helm/triagebot/ \
  --namespace autotriage \
  --set image.tag=v0.2.0
```

### Uninstall

```bash
helm uninstall autotriage --namespace autotriage
# PVC is retained by default; delete manually if needed
kubectl delete pvc autotriage-graph-pvc -n autotriage
```

---

## Configuration Management

### Environment-Specific Configs

| Setting | Dev | Staging | Production |
|---------|-----|---------|------------|
| LOG_LEVEL | debug | info | warning |
| replicas | 1 | 1 | 1 |
| resources.requests.cpu | 250m | 500m | 500m |
| resources.requests.memory | 1Gi | 2Gi | 2Gi |
| resources.limits.cpu | 1 | 2 | 2 |
| resources.limits.memory | 2Gi | 4Gi | 4Gi |
| persistence.size | 5Gi | 10Gi | 10Gi |
| backup.s3Export.enabled | false | false | true |
| ingress.enabled | false | true | true |

### Secret Management

**Local/Dev:** `.env` file or K8s Secret with `stringData`

**Staging/Production:** Use one of:
- AWS Secrets Manager + External Secrets Operator
- HashiCorp Vault + CSI driver
- Sealed Secrets

Never commit secrets to version control.

---

## Backup and Restore

### Automated Backups

Two CronJobs handle backup:

1. **BGSAVE (every 15 min):** Triggers FalkorDBLite to write an RDB snapshot to disk. This is fast and non-blocking.

2. **S3 Export (nightly at 2 AM):** Copies the graph data directory to S3 for disaster recovery.

### Manual Backup

```bash
# Trigger BGSAVE
curl -X POST http://autotriage.autotriage.svc.cluster.local/api/admin/bgsave

# Copy snapshot from pod
kubectl cp autotriage/$(kubectl get pod -n autotriage -l app.kubernetes.io/name=autotriage -o jsonpath='{.items[0].metadata.name}'):/app/data/graph ./graph-backup/
```

### Restore from Backup

```bash
# Scale down to prevent writes during restore
kubectl scale deployment/autotriage --replicas=0 -n autotriage

# Copy backup to PVC (using a temporary pod)
kubectl run restore --rm -it --image=alpine \
  --overrides='{"spec":{"containers":[{"name":"restore","image":"alpine","command":["sh"],"stdin":true,"tty":true,"volumeMounts":[{"name":"graph","mountPath":"/data"}]}],"volumes":[{"name":"graph","persistentVolumeClaim":{"claimName":"autotriage-graph-pvc"}}]}}' \
  -n autotriage

# Inside the pod: copy your backup files to /data/

# Scale back up
kubectl scale deployment/autotriage --replicas=1 -n autotriage
```

### Restore from S3

```bash
# Download from S3
aws s3 sync s3://autotriage-backups/graph-snapshots/LATEST/ ./graph-restore/

# Follow manual restore steps above with the downloaded files
```

### Full Rebuild

If all backups are lost, the graph can be rebuilt from live infrastructure:

```bash
# The ingestion pipeline re-discovers all services from K8s API + AWS APIs
curl -X POST http://localhost:8000/api/admin/rebuild-graph
```

---

## Troubleshooting

### Pod Won't Start

```bash
# Check events
kubectl describe pod -l app.kubernetes.io/name=autotriage -n autotriage

# Check logs
kubectl logs -l app.kubernetes.io/name=autotriage -n autotriage --previous
```

Common issues:
- **OOMKilled:** Increase memory limits
- **CrashLoopBackOff:** Check logs for Python import errors or missing dependencies
- **Pending:** PVC not bound (check StorageClass availability)

### FalkorDBLite Won't Start

FalkorDBLite runs as a subprocess. If it fails:

```bash
# Check for disk space issues
kubectl exec -n autotriage deployment/autotriage -- df -h /app/data/graph

# Check for corrupted data
kubectl exec -n autotriage deployment/autotriage -- ls -la /app/data/graph/
```

### Health Check Fails

```bash
# Test from inside the pod
kubectl exec -n autotriage deployment/autotriage -- curl -v http://localhost:8000/health

# Check if port 8000 is listening
kubectl exec -n autotriage deployment/autotriage -- ss -tlnp
```

### RBAC Issues

```bash
# Verify service account permissions
kubectl auth can-i list pods \
  --as=system:serviceaccount:autotriage:autotriage \
  --all-namespaces

kubectl auth can-i get pods/log \
  --as=system:serviceaccount:autotriage:autotriage \
  --all-namespaces
```

### Graph Data Lost After Restart

- Verify the PVC is bound: `kubectl get pvc -n autotriage`
- Verify the volume mount in the pod: `kubectl describe pod ... | grep -A5 Mounts`
- Ensure the Deployment uses `Recreate` strategy (not `RollingUpdate`) to avoid PVC access conflicts
