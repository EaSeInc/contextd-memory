#!/usr/bin/env bash
# contextd helper for the srv-ai home server.
# Usage: ctx.sh health | recall <q> [limit] | remember <text> [topic] [kind] | reindex
set -u

ENVF="${CTXD_ENV:-$HOME/.hermes_srv_ai.env}"
if [ -f "$ENVF" ]; then . "$ENVF"; fi
CTXD="${CTXD:-http://127.0.0.1:8090}"
: "${CTXD_TOKEN:?CTXD_TOKEN not set: create $ENVF}"

cmd="${1:-health}"
shift || true

jstr() { printf '%s' "$1" | python -c 'import json,sys;print(json.dumps(sys.stdin.read()))'; }

case "$cmd" in
  health)
    curl -s -m 10 "$CTXD/health"; echo ;;
  recall)
    q="${1:?query required}"; limit="${2:-5}"
    # curl --data-urlencode mangles UTF-8 in this MSYS shell, encode in python instead
    enc=$(python -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$q")
    curl -s -m 15 "$CTXD/recall?q=$enc&limit=$limit&budget=400" \
      -H "X-Context-Token: $CTXD_TOKEN"; echo ;;
  remember)
    text="${1:?text required}"; topic="${2:-infra}"; kind="${3:-fact}"
    curl -s -m 15 -X POST "$CTXD/remember" \
      -H "X-Context-Token: $CTXD_TOKEN" -H "Content-Type: application/json" \
      --data "{\"text\":$(jstr "$text"),\"topic\":$(jstr "$topic"),\"kind\":$(jstr "$kind")}"; echo ;;
  reindex)
    curl -s -m 60 -X POST "$CTXD/reindex" -H "X-Context-Token: $CTXD_TOKEN"; echo ;;
  *)
    echo "usage: ctx.sh health|recall|remember|reindex" >&2; exit 2 ;;
esac
