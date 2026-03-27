#!/usr/bin/env bash
# deploy.sh — Ubuntu deployment script for the Metricool MCP server
# Run as root or with sudo on the target Ubuntu machine.
set -euo pipefail

DEPLOY_DIR="/opt/metricool-mcp"
SERVICE_USER="metricool"
JAVA_PROJECT_DIR="$(dirname "$0")/../mcp-server"  # adjust if needed

echo "==> Creating system user '$SERVICE_USER'"
id "$SERVICE_USER" &>/dev/null || useradd --system --shell /sbin/nologin "$SERVICE_USER"

echo "==> Creating deployment directory"
mkdir -p "$DEPLOY_DIR"
cp -r ./* "$DEPLOY_DIR/"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$DEPLOY_DIR"

echo "==> Copying data-studio-fields.yaml from Java project"
cp "$JAVA_PROJECT_DIR/tools/src/main/resources/data-studio-fields.yaml" \
   "$DEPLOY_DIR/data/data-studio-fields.yaml"

echo "==> Creating Python 3.10 virtual environment"
python3.10 -m venv "$DEPLOY_DIR/venv"
"$DEPLOY_DIR/venv/bin/pip" install --upgrade pip
"$DEPLOY_DIR/venv/bin/pip" install -r "$DEPLOY_DIR/requirements.txt"

echo "==> Installing systemd service"
cp "$DEPLOY_DIR/metricool-mcp.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable metricool-mcp
systemctl restart metricool-mcp

echo "==> Installing nginx config"
cp "$DEPLOY_DIR/nginx.conf" /etc/nginx/sites-available/metricool-mcp
ln -sf /etc/nginx/sites-available/metricool-mcp /etc/nginx/sites-enabled/metricool-mcp
nginx -t && systemctl reload nginx

echo ""
echo "==> Done! Next steps:"
echo "    1. Create /opt/metricool-mcp/.env with MCP_API_KEY=<key>  (single-user mode)"
echo "       Or leave it empty for multi-user mode (clients send Authorization: Bearer)"
echo "    2. Obtain TLS cert:  certbot --nginx -d mcp.metricool.ai"
echo "    3. Restart service:  systemctl restart metricool-mcp"
