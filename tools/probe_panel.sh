#!/usr/bin/env bash
# Reconnaissance against a SPAN panel. Read-only and safe to re-run.
#
# Usage: ./tools/probe_panel.sh [host]
#
# The key question this answers: are the REST endpoints returning 502 (backend
# down) or 200 (proximity window is open)? Press the panel door switch 3 times
# and re-run within 15 minutes to find out.

set -uo pipefail

HOST="${1:-${SPAN_HOST:-}}"
if [[ -z "$HOST" ]]; then
  echo "usage: $0 <panel-ip-or-hostname>" >&2
  exit 2
fi

echo "=== SPAN panel probe: $HOST ==="
echo

echo "--- mDNS identity ---"
if command -v dns-sd >/dev/null 2>&1; then
  # dns-sd never exits on its own; -t bounds it on macOS builds that support it.
  dns-sd -t 4 -B _span._tcp local 2>/dev/null | grep -i span || echo "  (no _span._tcp advertisement seen)"
elif command -v avahi-browse >/dev/null 2>&1; then
  avahi-browse -tpr _span._tcp 2>/dev/null | grep -i txt || echo "  (no _span._tcp advertisement seen)"
else
  echo "  (no mDNS tool available; install avahi-utils or use macOS dns-sd)"
fi
echo

echo "--- ports ---"
for spec in "80:REST (nginx)" "443:REST over TLS" "8883:MQTTS - Electrification Bus" \
            "9001:MQTT over WebSocket" "50058:gRPC (services removed in r202627)" \
            "50065:legacy gRPC (removed)"; do
  port="${spec%%:*}"; label="${spec#*:}"
  if nc -z -w 3 "$HOST" "$port" 2>/dev/null; then
    printf '  + %-6s open    %s\n' "$port" "$label"
  else
    printf '  - %-6s closed  %s\n' "$port" "$label"
  fi
done
echo

echo "--- broker TLS identity (8883) ---"
if nc -z -w 3 "$HOST" 8883 2>/dev/null; then
  openssl s_client -connect "$HOST:8883" </dev/null 2>/dev/null \
    | grep -E "^(subject|issuer)=" | sed 's/^/  /' \
    || echo "  (handshake produced no certificate detail)"
else
  echo "  (8883 closed)"
fi
echo

echo "--- REST endpoints ---"
REST_UP=0
for path in /api/v2/certificate/ca /api/v2/auth/register /api/v2/status \
            /api/v2/homie/schema /api/v1/panel /api/v1/circuits; do
  code=$(curl -sk -m 5 -o /dev/null -w '%{http_code}' "http://$HOST$path" 2>/dev/null)
  printf '  %-4s %s\n' "$code" "$path"
  case "$code" in
    500|502|503|000) ;;
    *) REST_UP=1 ;;
  esac
done
echo

if [[ "$REST_UP" == "1" ]]; then
  echo "REST API is responding."
  echo "Next:  span-bridge auth --host $HOST"
else
  echo "REST API is not running (502 = nginx upstream refused)."
  echo "Press the panel door switch 3 times, then re-run this within 15 minutes."
fi
