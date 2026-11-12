#!/usr/bin/env bash
# usage: run.sh <python> <mode> <port> <runs>
PY=$1; MODE=$2; PORT=$3; RUNS=$4
cd "$(dirname "$0")"
MODE=$MODE $PY -m uvicorn app:app --port $PORT --loop uvloop --http httptools --log-level warning 2> server_$PORT.log &
SRV=$!
for i in $(seq 50); do curl -s -o /dev/null --max-time 0.2 http://127.0.0.1:$PORT/openapi.json && break; sleep 0.1; done
for i in $(seq $RUNS); do
  # read for 0.3 s, then drop the socket
  curl -s -N --max-time 0.3 http://127.0.0.1:$PORT/stream > /dev/null
  sleep 0.4
done
sleep 0.5; kill $SRV; wait $SRV 2>/dev/null
late=$(grep -c "gen cleanup dep_open=False" server_$PORT.log); ok=$(grep -c "gen cleanup dep_open=True" server_$PORT.log)
never=$((RUNS - late - ok)); errs=$(grep -c "Exception in ASGI" server_$PORT.log); rb=$(grep -c "outcome=rollback" server_$PORT.log); cm=$(grep -c "outcome=commit" server_$PORT.log)
echo "mode=$MODE runs=$RUNS: cleanup before dep exit $ok, after dep exit $late, not before shutdown $never; 'Exception in ASGI application' $errs; dependency saw exception $rb, exited normally $cm"
