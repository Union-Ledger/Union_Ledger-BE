#!/bin/bash
set -euxo pipefail

APP_DIR="${app_install_dir}"

# Docker + Git (Amazon Linux 2023)
dnf update -y
dnf install -y docker git rsync
systemctl enable --now docker

# Docker Compose v2 plugin
mkdir -p /usr/local/lib/docker/cli-plugins
COMPOSE_VERSION="v2.32.4"
curl -fsSL "https://github.com/docker/compose/releases/download/$${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

usermod -aG docker ec2-user

mkdir -p "$APP_DIR"
chown ec2-user:ec2-user "$APP_DIR"

# First deploy is done from your laptop via: ./scripts/deploy.sh
touch "$APP_DIR/.bootstrap_complete"
