#!/usr/bin/env bash
# Sync project to EC2 and run docker compose (production).
#
# Usage:
#   export DEPLOY_KEY=~/.ssh/my-keypair.pem
#   ./scripts/deploy.sh
#
# Optional:
#   DEPLOY_HOST=1.2.3.4 DEPLOY_USER=ec2-user ./scripts/deploy.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TERRAFORM_DIR="${ROOT_DIR}/terraform"
REMOTE_DIR="${REMOTE_DIR:-/opt/union-ledger}"
COMPOSE_FILE="docker-compose.prod.yml"
SSH_USER="${DEPLOY_USER:-ec2-user}"

if [[ -z "${DEPLOY_KEY:-}" ]]; then
  echo "DEPLOY_KEY is required (path to .pem), e.g. export DEPLOY_KEY=~/.ssh/my-keypair.pem" >&2
  exit 1
fi

if [[ ! -f "${DEPLOY_KEY}" ]]; then
  echo "DEPLOY_KEY file not found: ${DEPLOY_KEY}" >&2
  exit 1
fi

if [[ -z "${DEPLOY_HOST:-}" ]]; then
  if command -v terraform >/dev/null 2>&1 && [[ -d "${TERRAFORM_DIR}" ]]; then
    DEPLOY_HOST="$(terraform -chdir="${TERRAFORM_DIR}" output -raw public_ip 2>/dev/null || true)"
  fi
fi

if [[ -z "${DEPLOY_HOST:-}" ]]; then
  echo "Set DEPLOY_HOST or run 'terraform apply' in ./terraform first." >&2
  exit 1
fi

if [[ ! -f "${ROOT_DIR}/.env" ]]; then
  echo "Missing ${ROOT_DIR}/.env — copy from .env.production.example and edit." >&2
  exit 1
fi

SSH_OPTS=(-i "${DEPLOY_KEY}" -o StrictHostKeyChecking=accept-new)

echo "==> Waiting for SSH (${SSH_USER}@${DEPLOY_HOST})..."
for _ in $(seq 1 30); do
  if ssh "${SSH_OPTS[@]}" "${SSH_USER}@${DEPLOY_HOST}" "echo ok" >/dev/null 2>&1; then
    break
  fi
  sleep 10
done

echo "==> Ensure rsync on EC2 (Amazon Linux does not include it by default)"
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${DEPLOY_HOST}" \
  "command -v rsync >/dev/null || sudo dnf install -y rsync"

echo "==> Rsync sources to ${REMOTE_DIR}"
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${DEPLOY_HOST}" "sudo mkdir -p '${REMOTE_DIR}' && sudo chown ${SSH_USER}:${SSH_USER} '${REMOTE_DIR}'"

rsync -az --delete \
  -e "ssh ${SSH_OPTS[*]}" \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.ruff_cache' \
  --exclude 'storage' \
  --exclude 'terraform/.terraform' \
  --exclude 'terraform/*.tfstate*' \
  --exclude 'terraform/terraform.tfvars' \
  --exclude '.env.example' \
  "${ROOT_DIR}/" "${SSH_USER}@${DEPLOY_HOST}:${REMOTE_DIR}/"

echo "==> Docker build & up"
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${DEPLOY_HOST}" bash -s <<EOF
set -euo pipefail
cd '${REMOTE_DIR}'
docker compose -f '${COMPOSE_FILE}' up -d --build
docker compose -f '${COMPOSE_FILE}' exec -T api alembic upgrade head
docker compose -f '${COMPOSE_FILE}' ps
# Remove legacy cloudflared container if it still exists
docker rm -f union-ledger-cloudflared-1 2>/dev/null || true
EOF

echo ""
echo "Deploy finished."
echo "  API docs: http://${DEPLOY_HOST}:8000/docs"
echo "  Health:   http://${DEPLOY_HOST}:8000/api/v1/health"
echo "  HTTPS: configure nginx on EC2 (443 → 127.0.0.1:8000) — see terraform/README.md"
