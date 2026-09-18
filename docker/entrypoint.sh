#!/usr/bin/env sh
# Seed the persistent volume on first boot, point the app at it, start uvicorn.
set -e
DATA="${DATA_DIR:-/data}"
mkdir -p "$DATA/.state"
for d in skills rules knowledge evals; do
  if [ ! -d "$DATA/$d" ] || [ -z "$(ls -A "$DATA/$d" 2>/dev/null)" ]; then
    echo "seeding $DATA/$d from image"
    mkdir -p "$DATA/$d" && cp -R "/app/$d/." "$DATA/$d/"
  fi
done
[ -f "$DATA/pricing.yaml" ] || cp /app/pricing.yaml "$DATA/pricing.yaml"
mkdir -p /app/.state-local
# SQLite stays on local disk (SMB shares cannot lock it reliably); it is backed up to the share every minute and restored on boot.
export SQLITE_DB_PATH=/app/.state-local/bankrag.db SQLITE_DB_BACKUP="$DATA/.state/bankrag.db.bak"
export DEEPEVAL_TELEMETRY_OPT_OUT=YES DEEPEVAL_UPDATE_WARNING_OPT_IN=0
# LangGraph checkpoints hold only LangChain messages: refuse anything else when deserialising them
export LANGGRAPH_STRICT_MSGPACK=true
export SKILLS_DIR="$DATA/skills" RULES_DIR="$DATA/rules" KNOWLEDGE_DIR="$DATA/knowledge" STATE_DIR="$DATA/.state" EVALS_DIR="$DATA/evals" PRICING_FILE="$DATA/pricing.yaml"
cd /app
exec uvicorn bankrag.api:app --host 0.0.0.0 --port "${API_PORT:-8010}" --proxy-headers --forwarded-allow-ips="*"
