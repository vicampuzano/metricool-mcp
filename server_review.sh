#!/usr/bin/env bash
# server_review.sh — Post-deploy health review for the Metricool MCP server.
#
# Answers "how has the server been?" with interpreted verdicts rather than raw
# counts, because the raw counts are easy to misread:
#   - "0 media timeouts" means nothing if no post with media was scheduled.
#   - A handful of token-probe timeouts per hour is the normal background rate;
#     hundreds in one hour is an upstream incident.
#   - /health "returned 000000" is curl hitting its own --max-time, i.e. the
#     server was saturated — NOT an HTTP error from the app.
#
# Usage:
#   ./server_review.sh                  # since the last service start
#   ./server_review.sh '2026-08-05 05:47:00'
#   ./server_review.sh '1 day ago'
#
# Runs locally on the server, or from a workstation via ssh (auto-detected).
set -uo pipefail

SINCE="${1:-}"
SSH_HOST="${MCP_SSH_HOST:-growthp1}"

# Re-run on the server when journalctl isn't here. The window is quoted with %q
# because it contains a space ("2026-08-05 05:47:00") and ssh flattens its command
# into one string — unquoted, the remote shell would see two arguments and silently
# review a wider window than asked for.
if ! command -v journalctl >/dev/null 2>&1; then
    exec ssh "$SSH_HOST" "bash -s -- $(printf '%q' "$SINCE")" < "$0"
fi

SERVICE="metricool-mcp"
HEALTH_LOG="/var/log/metricool-mcp-health.log"
HEALTH_URL="https://mcp.metricool.ai/health"

if [ -z "$SINCE" ]; then
    SINCE=$(systemctl show "$SERVICE" -p ActiveEnterTimestamp --value \
            | sed 's/^[A-Za-z]* //' | xargs -I{} date -d "{}" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
    [ -z "$SINCE" ] && SINCE="1 day ago"
    echo "No window given — using the current service start: $SINCE"
fi

LOG=$(mktemp); trap 'rm -f "$LOG"' EXIT
journalctl -u "$SERVICE" --since "$SINCE" --no-pager > "$LOG"

count() { grep -c "$1" "$LOG" || true; }

REQUESTS=$(count 'POST /mcp')
MEDIA_TIMEOUTS=$(count 'normalize_url transient failure')
MEDIA_FAILURES=$(count "Couldn't upload the media")
POSTS=$(grep -cE 'create_scheduled_post called|update_scheduled_post called' "$LOG" || true)
PROBE_TIMEOUTS=$(count 'Token probe network error')
DISCONNECTS=$(count 'ClientDisconnect')
RESTARTS=$(grep -c 'Started Metricool MCP Server' "$LOG" || true)

# Tracebacks that are NOT the SDK's client-disconnect noise.
OUR_TRACEBACKS=$(grep -A4 'Traceback (most recent call last)' "$LOG" \
                 | grep -cE '/opt/metricool-mcp/(server|client|validators|media_normalizer|pinterest_boards|oauth|middleware|fields_loader)\.py' || true)

# Window length in hours, floored at 1 so the per-hour rates below never divide by zero.
SPAN_SECS=$(( $(date +%s) - $(date -d "$SINCE" +%s 2>/dev/null || date +%s) ))
[ "$SPAN_SECS" -lt 3600 ] && SPAN_SECS=3600
HOURS=$(( SPAN_SECS / 3600 ))

echo
echo "==================== MCP server review ===================="
echo "Window : since $SINCE  (~${HOURS}h)"
echo "Commit : $(cd /opt/metricool-mcp 2>/dev/null && git log --oneline -1 || echo '?')"
echo "Service: $(systemctl is-active "$SERVICE")   restarts in window: $RESTARTS"
echo "Health : $(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$HEALTH_URL")   $(curl -s --max-time 10 "$HEALTH_URL")"
echo
echo "Traffic"
echo "  POST /mcp .................. $REQUESTS"
echo "  posts created/updated ...... $POSTS"
echo
echo "Media normalization  (the Aug 2026 regression — see the memory note)"
echo "  read timeouts .............. $MEDIA_TIMEOUTS"
echo "  hard failures .............. $MEDIA_FAILURES"
MIN_SAMPLE=20   # below this, "zero timeouts" is luck, not evidence
if [ "$POSTS" -eq 0 ]; then
    echo "  VERDICT: INCONCLUSIVE — no post was scheduled, so the fix was never exercised."
elif [ "$MEDIA_TIMEOUTS" -eq 0 ] && [ "$POSTS" -lt "$MIN_SAMPLE" ]; then
    echo "  VERDICT: PRELIMINARY — only $POSTS posts in this window; too few to conclude."
    echo "           Re-run over a busier window (media traffic peaks during the day)."
elif [ "$MEDIA_TIMEOUTS" -eq 0 ]; then
    echo "  VERDICT: GOOD — $POSTS posts went through with zero read timeouts."
else
    RATE=$(( MEDIA_TIMEOUTS * 100 / POSTS ))
    echo "  VERDICT: REGRESSION — ${MEDIA_TIMEOUTS} timeouts over ${POSTS} posts (${RATE}%)."
    echo "           Each costs a retry (~2x the read timeout) and starves the thread pool."
    echo "           Raise METRICOOL_MEDIA_READ_TIMEOUT (client.py) and check which hosts are slow:"
    grep 'normalize_url transient failure' "$LOG" | grep -oE 'https?://[^/]+' | sort | uniq -c | sort -rn | head -5 | sed 's/^/           /'
fi
echo
echo "Upstream (app.metricool.com)"
echo "  token probe timeouts ....... $PROBE_TIMEOUTS  (~$(( PROBE_TIMEOUTS / HOURS ))/h)"
if [ "$(( PROBE_TIMEOUTS / HOURS ))" -gt 20 ]; then
    echo "  VERDICT: UPSTREAM INCIDENT — background rate is ~1-6/h. Check by hour:"
    grep 'Token probe network error' "$LOG" | awk '{print $1, $2, substr($3,1,2)":00"}' | uniq -c | sed 's/^/           /'
else
    echo "  VERDICT: NORMAL — background rate is ~1-6/h. Not our code (token_check.py"
    echo "           calls requests directly and fails open)."
fi
echo
echo "Errors"
echo "  ClientDisconnect (SDK, benign) $DISCONNECTS"
echo "  tracebacks in OUR modules .... $OUR_TRACEBACKS"
[ "$OUR_TRACEBACKS" -gt 0 ] && echo "  VERDICT: INVESTIGATE — see: journalctl -u $SERVICE --since '$SINCE' | grep -B2 -A15 Traceback"
echo
echo "Healthcheck restarts  ('000000' = curl --max-time 10 expired = saturation)"
grep 'ALERT' "$HEALTH_LOG" 2>/dev/null | tail -5 | sed 's/^/  /' || echo "  (none logged)"
echo
PID=$(systemctl show "$SERVICE" -p MainPID --value)
if [ "$PID" != "0" ]; then
    echo "Process: threads=$(ls /proc/"$PID"/task 2>/dev/null | wc -l)" \
         "fds=$(ls /proc/"$PID"/fd 2>/dev/null | wc -l)/$(grep 'Max open files' /proc/"$PID"/limits | awk '{print $4}')" \
         "rss=$(awk '/VmRSS/{print $2/1024 "MB"}' /proc/"$PID"/status)" \
         "uptime=$(ps -o etime= -p "$PID" | xargs)"
fi
echo "==========================================================="
