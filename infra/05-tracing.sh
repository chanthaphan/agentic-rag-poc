#!/usr/bin/env bash
# Server-side agent tracing: create Application Insights (workspace-based), connect it to the Foundry project, and let the
# container app read it (Monitoring Reader) so handoff answers can pull the specialist's token usage. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
RG="$AZURE_RESOURCE_GROUP"; NAME="${APPINSIGHTS_NAME:-bankrag-insights}"; LOC="${APPINSIGHTS_LOCATION:-eastus2}"
WS=$(az resource list -g "$RG" --resource-type Microsoft.OperationalInsights/workspaces --query "[0].id" -o tsv)
[ -n "$WS" ] || WS=$(az monitor log-analytics workspace create -g "$RG" -n "${NAME}-logs" -l "$LOC" --query id -o tsv)
az monitor app-insights component show -g "$RG" -a "$NAME" >/dev/null 2>&1 || az monitor app-insights component create -g "$RG" -a "$NAME" -l "$LOC" --workspace "$WS" --kind web --application-type web -o none
AI_ID=$(az monitor app-insights component show -g "$RG" -a "$NAME" --query id -o tsv)
APP_ID=$(az monitor app-insights component show -g "$RG" -a "$NAME" --query appId -o tsv)
CS=$(az monitor app-insights component show -g "$RG" -a "$NAME" --query connectionString -o tsv)
PROJECT_ID="/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY_ACCOUNT/projects/$FOUNDRY_PROJECT"
TOKEN=$(az account get-access-token --scope https://management.azure.com/.default --query accessToken -o tsv)
curl -s -o /dev/null -w "project connection: HTTP %{http_code}\n" -X PUT "https://management.azure.com$PROJECT_ID/connections/$NAME?api-version=2025-10-01-preview" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"$NAME\",\"type\":\"Microsoft.MachineLearningServices/workspaces/connections\",\"properties\":{\"category\":\"AppInsights\",\"authType\":\"ApiKey\",\"target\":\"$AI_ID\",\"isSharedToAll\":true,\"credentials\":{\"key\":\"$CS\"},\"metadata\":{\"ApiType\":\"Azure\",\"ResourceId\":\"$AI_ID\"}}}"
MI=$(az containerapp show -g "$RG" -n "${APP_NAME:-bankrag}" --query identity.principalId -o tsv 2>/dev/null || true)
if [ -n "$MI" ]; then
  for scope in "$AI_ID" "$WS"; do az role assignment create --assignee-object-id "$MI" --assignee-principal-type ServicePrincipal --role "Monitoring Reader" --scope "$scope" -o none 2>/dev/null || true; done
  az containerapp update -g "$RG" -n "${APP_NAME:-bankrag}" --set-env-vars "APPINSIGHTS_APP_ID=$APP_ID" -o none
fi
echo "APPINSIGHTS_APP_ID=$APP_ID   (put this in .env for local runs)"
