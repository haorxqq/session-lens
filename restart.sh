#!/usr/bin/env bash
# Restart session-lens: if something is listening on port 5678, stop it, then start fresh.
set -euo pipefail

PORT=5678
cd "$(dirname "$0")"

# Stop any process currently listening on the port.
PIDS=$(lsof -ti :"$PORT" || true)
if [ -n "$PIDS" ]; then
  echo "Stopping process(es) on port $PORT: $PIDS"
  kill $PIDS 2>/dev/null || true
  # Wait up to ~5s for the port to free up, then force-kill if needed.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.5
    [ -z "$(lsof -ti :"$PORT" || true)" ] && break
  done
  REMAIN=$(lsof -ti :"$PORT" || true)
  if [ -n "$REMAIN" ]; then
    echo "Force killing: $REMAIN"
    kill -9 $REMAIN 2>/dev/null || true
    sleep 0.5
  fi
else
  echo "Nothing running on port $PORT."
fi

# Start fresh, detached from this terminal.
echo "Starting app.py ..."
nohup python app.py > nohup.out 2>&1 &

# Confirm it came up.
sleep 3
if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/" 2>/dev/null | grep -q "200"; then
  echo "Started: http://localhost:$PORT  (PID $(lsof -ti :"$PORT"))"
else
  echo "Started, but http://localhost:$PORT did not return 200 yet. Check nohup.out:"
  tail -n 15 nohup.out
fi
