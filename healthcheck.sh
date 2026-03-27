#!/usr/bin/env bash
# healthcheck.sh — Simple uptime monitor for the Metricool MCP server.
# Checks /health endpoint and restarts the service if it fails.
# Install: crontab -e → */5 * * * * /opt/metricool-mcp/healthcheck.sh
set -euo pipefail

LOGFILE="/var/log/metricool-mcp-health.log"
URL="https://mcp.metricool.ai/health"
SERVICE="metricool-mcp"

status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$URL" 2>/dev/null || echo "000")

if [ "$status" != "200" ]; then
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') ALERT: /health returned $status — restarting $SERVICE" >> "$LOGFILE"
    systemctl restart "$SERVICE"
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') INFO: $SERVICE restarted" >> "$LOGFILE"
else
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') OK: $status" >> "$LOGFILE"
fi
