#!/usr/bin/env bash
# Wire the live-service MCP tools (/mcp/services) for the Foundry agents.
#
# The endpoint stays BEHIND the app's Easy Auth: the agent authenticates as the Foundry project's managed identity and
# gets a token for the Easy Auth app registration, and the app then checks the caller's object id. Nothing is exposed
# publicly, and no shared key travels in the agent definition.
#
# Sets on the container app:
#   PUBLIC_BASE_URL         https://<fqdn>            so the agent knows where to call us
#   MCP_AUDIENCE            api://<easyauth-client>   the token audience the connection asks for
#   MCP_CALLER_PRINCIPALS   <project principal id>    the only identity allowed to call the tools
# Run infra/12-easyauth.sh first (it creates the app registration this reuses), then `bankrag skills sync`.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a   # the other infra scripts read .env; this one needs AZURE_SUBSCRIPTION_ID from it too
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
APP="${CA_APP:-talkwithgrace}"
APPREG="${APPREG:-bankrag-easyauth}"
ACCOUNT="${FOUNDRY_ACCOUNT:-my-model-hub}"
PROJECT="${FOUNDRY_PROJECT:-firstProject}"
SUB="${AZURE_SUBSCRIPTION_ID:?set AZURE_SUBSCRIPTION_ID}"

FQDN=$(az containerapp show -g "$RG" -n "$APP" --query "properties.configuration.ingress.fqdn" -o tsv)
[ -n "$FQDN" ] || { echo "no ingress on $APP; run infra/11-containerapp.sh first"; exit 1; }

CLIENT_ID=$(az ad app list --display-name "$APPREG" --query "[0].appId" -o tsv)
[ -n "$CLIENT_ID" ] || { echo "app registration '$APPREG' not found; run infra/12-easyauth.sh first"; exit 1; }

PROJECT_ID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT/projects/$PROJECT"
PRINCIPAL=$(az rest --method get --url "https://management.azure.com$PROJECT_ID?api-version=2025-06-01" --query identity.principalId -o tsv)
[ -n "$PRINCIPAL" ] && [ "$PRINCIPAL" != "None" ] || { echo "project has no managed identity; run infra/02-project-identity.sh first"; exit 1; }

echo "app            : https://$FQDN"
echo "audience       : api://$CLIENT_ID"
echo "allowed caller : $PRINCIPAL (Foundry project managed identity)"

az containerapp update -g "$RG" -n "$APP" --set-env-vars \
  "PUBLIC_BASE_URL=https://$FQDN" \
  "MCP_AUDIENCE=api://$CLIENT_ID" \
  "MCP_CALLER_PRINCIPALS=$PRINCIPAL" -o none

echo "done. Now run: uv run bankrag skills sync   # creates the bank-services-mcp connection and attaches the tool"
