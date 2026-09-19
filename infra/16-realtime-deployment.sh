#!/usr/bin/env bash
# Create the realtime (speech mode) deployment the voice page talks to.
#
# The browser opens a WebRTC call straight to this deployment with a short-lived key the app mints, so the deployment
# must live on the SAME Azure OpenAI account as the chat models (AOAI_ENDPOINT) and in a region that serves the
# realtime API: East US 2 or Sweden Central.
#
# Usage: ./infra/16-realtime-deployment.sh            (uses REALTIME_DEPLOYMENT from .env, default gpt-realtime-2.1)
#        REALTIME_MODEL=gpt-realtime-1.5 REALTIME_MODEL_VERSION=2026-02-23 ./infra/16-realtime-deployment.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
ACCOUNT="${FOUNDRY_ACCOUNT:-my-model-hub}"
NAME="${REALTIME_DEPLOYMENT:-gpt-realtime-2.1}"
MODEL="${REALTIME_MODEL:-$NAME}"
VERSION="${REALTIME_MODEL_VERSION:-2026-07-07}"
CAPACITY="${REALTIME_CAPACITY:-10}"

echo "== deploying $MODEL ($VERSION) as '$NAME' on $ACCOUNT"
az cognitiveservices account deployment create -g "$RG" -n "$ACCOUNT" \
  --deployment-name "$NAME" --model-name "$MODEL" --model-version "$VERSION" \
  --model-format OpenAI --sku-name GlobalStandard --sku-capacity "$CAPACITY" -o none

az cognitiveservices account deployment list -g "$RG" -n "$ACCOUNT" \
  --query "[?contains(name,'realtime')].{name:name,model:properties.model.name,version:properties.model.version,capacity:sku.capacity}" -o table

cat <<EOF

Next:
  - set REALTIME_DEPLOYMENT=$NAME in .env (and run infra/11-containerapp.sh to pass it to the app)
  - or pick it in Studio > Settings > Runtime > Speech deployment
If the quota is refused, ask for capacity on the account or deploy gpt-realtime-1.5 instead:
  REALTIME_MODEL=gpt-realtime-1.5 REALTIME_MODEL_VERSION=2026-02-23 REALTIME_DEPLOYMENT=gpt-realtime-1.5 $0
EOF
