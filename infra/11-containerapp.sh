#!/usr/bin/env bash
# Deploy/update the app on Azure Container Apps (consumption) with an Azure Files volume for data.
# Requires: .env with the runtime values (keys are read from it and stored as Container Apps secrets).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
LOCATION="${LOCATION:-eastus2}"
ACR="${ACR_NAME:-bankragacr}"
IMAGE="${IMAGE:-$ACR.azurecr.io/bankrag:latest}"
ENVNAME="${CA_ENV:-bankrag-env}"
APP="${CA_APP:-talkwithgrace}"
STG="${STORAGE_ACCOUNT:-bankragdata$(echo "$AZURE_SUBSCRIPTION_ID" | cut -c1-6)}"
# Each app needs its own share and its own environment storage binding: two apps sharing one share would write over
# each other's SQLite backup, skills, rules and settings. Defaults keep the original app on the original share.
SHARE="${DATA_SHARE:-bankrag-data}"
BINDING="${DATA_BINDING:-data}"
MIN="${MIN_REPLICAS:-1}"

echo "== storage account + file share"
az storage account show -g "$RG" -n "$STG" >/dev/null 2>&1 || az storage account create -g "$RG" -n "$STG" --location "$LOCATION" --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 -o none
STG_KEY=$(az storage account keys list -g "$RG" -n "$STG" --query "[0].value" -o tsv)
az storage share-rm show --storage-account "$STG" -g "$RG" -n "$SHARE" >/dev/null 2>&1 || az storage share-rm create --storage-account "$STG" -g "$RG" -n "$SHARE" --quota 5 -o none

echo "== container apps environment"
az containerapp env show -g "$RG" -n "$ENVNAME" >/dev/null 2>&1 || az containerapp env create -g "$RG" -n "$ENVNAME" --location "$LOCATION" -o none
az containerapp env storage set -g "$RG" -n "$ENVNAME" --storage-name "$BINDING" --azure-file-account-name "$STG" --azure-file-account-key "$STG_KEY" --azure-file-share-name "$SHARE" --access-mode ReadWrite -o none

echo "== container app"
ACR_ID=$(az acr show -g "$RG" -n "$ACR" --query id -o tsv)
if ! az containerapp show -g "$RG" -n "$APP" >/dev/null 2>&1; then
  az containerapp create -g "$RG" -n "$APP" --environment "$ENVNAME" --image "$IMAGE" \
    --registry-server "$ACR.azurecr.io" --registry-identity system \
    --system-assigned --ingress external --target-port 8010 --transport auto \
    --cpu 0.5 --memory 1.0Gi --min-replicas "$MIN" --max-replicas 1 -o none
fi
PRINCIPAL=$(az containerapp identity show -g "$RG" -n "$APP" --query principalId -o tsv)
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID" -o none 2>/dev/null || true

echo "== secrets + env"
SECRETS=(aoai-key="$AOAI_API_KEY" search-admin-key="$SEARCH_ADMIN_KEY" search-query-key="$SEARCH_QUERY_KEY" studio-password="$STUDIO_PASSWORD")
if [ -n "${BBL_API_KEY:-}" ]; then SECRETS+=(bbl-api-key="$BBL_API_KEY"); fi
az containerapp secret set -g "$RG" -n "$APP" --secrets "${SECRETS[@]}" -o none
# Optional values are appended only when .env has them: an empty secret value is rejected by az, and an empty
# BBL_API_KEY / GOOGLE_MAPS_KEY would turn the live tools and the map card off with no way to tell why.
ENVVARS=(
  AZURE_TENANT_ID="$AZURE_TENANT_ID" AZURE_SUBSCRIPTION_ID="$AZURE_SUBSCRIPTION_ID" AZURE_RESOURCE_GROUP="$RG"
  FOUNDRY_ACCOUNT="$FOUNDRY_ACCOUNT" AOAI_API_VERSION="${AOAI_API_VERSION:-2025-04-01-preview}"
  AOAI_ENDPOINT="$AOAI_ENDPOINT" AOAI_API_KEY=secretref:aoai-key EMBED_DEPLOYMENT="$EMBED_DEPLOYMENT" EMBED_DIMS="$EMBED_DIMS"
  DEFAULT_CHAT_MODEL="$DEFAULT_CHAT_MODEL" ROUTER_MODEL="$ROUTER_MODEL"
  ORCHESTRATION_MODE="${ORCHESTRATION_MODE:-router}" HISTORY_TURNS="${HISTORY_TURNS:-6}" SUGGESTIONS_MODE="${SUGGESTIONS_MODE:-dynamic}"
  SEARCH_SERVICE_NAME="$SEARCH_SERVICE_NAME" SEARCH_ENDPOINT="$SEARCH_ENDPOINT" SEARCH_ADMIN_KEY=secretref:search-admin-key SEARCH_QUERY_KEY=secretref:search-query-key
  SEARCH_INDEX="$SEARCH_INDEX" SEARCH_API_VERSION="$SEARCH_API_VERSION" KB_REASONING_EFFORT="$KB_REASONING_EFFORT" KB_LLM_DEPLOYMENT="$KB_LLM_DEPLOYMENT" KB_MCP_AUTH="$KB_MCP_AUTH"
  STUDIO_PASSWORD=secretref:studio-password STUDIO_ADMINS="${STUDIO_ADMINS:-}" STUDIO_TESTERS="${STUDIO_TESTERS:-}" STUDIO_EXTERNALS="${STUDIO_EXTERNALS:-}"
  APP_USER_NAME="${APP_USER_NAME:-Pim}" APP_USER_INITIALS="${APP_USER_INITIALS:-PW}" ASSISTANT_NAME="${ASSISTANT_NAME:-Assistant}" DATA_DIR=/data
)
if [ -n "${BBL_API_KEY:-}" ]; then ENVVARS+=(BBL_API_KEY=secretref:bbl-api-key); fi
if [ -n "${GOOGLE_MAPS_KEY:-}" ]; then ENVVARS+=(GOOGLE_MAPS_KEY="$GOOGLE_MAPS_KEY"); fi
az containerapp update -g "$RG" -n "$APP" --image "$IMAGE" --min-replicas "$MIN" --max-replicas 1 \
  --set-env-vars "${ENVVARS[@]}" -o none

echo "== volume mount (via yaml)"
az containerapp show -g "$RG" -n "$APP" -o yaml > /tmp/ca.yaml
python3 - "$BINDING" <<'PY'
import sys, yaml
binding = sys.argv[1]
p = "/tmp/ca.yaml"; d = yaml.safe_load(open(p))
t = d["properties"]["template"]
t["volumes"] = [{"name": "data", "storageName": binding, "storageType": "AzureFile"}]
for c in t["containers"]:
    c["volumeMounts"] = [{"volumeName": "data", "mountPath": "/data"}]
yaml.safe_dump(d, open(p, "w"))
PY
az containerapp update -g "$RG" -n "$APP" --yaml /tmp/ca.yaml -o none

echo "== roles for the app identity"
ACCOUNT_ID="/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY_ACCOUNT"
# Cognitive Services OpenAI User: chat completions without a key (the AOAI_API_KEY fallback); Reader: the deployments list in Studio
for role in "Cognitive Services OpenAI User" "Reader"; do
  az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal --role "$role" --scope "$ACCOUNT_ID" -o none 2>/dev/null && echo "assigned $role" || echo "exists: $role"
done
# the app calls the knowledge-base MCP endpoint itself (KB_MCP_AUTH=identity)
SEARCH_ID="/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/$RG/providers/Microsoft.Search/searchServices/$SEARCH_SERVICE_NAME"
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal --role "Search Index Data Reader" --scope "$SEARCH_ID" -o none 2>/dev/null && echo "assigned Search Index Data Reader" || echo "exists: Search Index Data Reader"
FQDN=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)
echo "APP URL: https://$FQDN   (share $SHARE, binding $BINDING)"
