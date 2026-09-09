#!/usr/bin/env bash
# Put Entra ID sign-in (Container Apps built-in auth) in front of the whole app. Only users of this tenant can open it.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
APP="${CA_APP:-bankrag}"
FQDN=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)
APPREG="${APPREG_NAME:-bankrag-easyauth}"
CLIENT_ID=$(az ad app list --display-name "$APPREG" --query "[0].appId" -o tsv)
if [ -z "$CLIENT_ID" ]; then
  CLIENT_ID=$(az ad app create --display-name "$APPREG" --sign-in-audience AzureADMyOrg --web-redirect-uris "https://$FQDN/.auth/login/aad/callback" --enable-id-token-issuance true --query appId -o tsv)
else
  az ad app update --id "$CLIENT_ID" --web-redirect-uris "https://$FQDN/.auth/login/aad/callback" --enable-id-token-issuance true -o none
fi
SECRET=$(az ad app credential reset --id "$CLIENT_ID" --display-name easyauth --years 1 --query password -o tsv)
az containerapp secret set -g "$RG" -n "$APP" --secrets easyauth-secret="$SECRET" -o none
# Easy Auth is told to accept api://<client-id>, so that URI has to actually exist on the app registration: without it
# Entra cannot issue a token for that resource, and a service calling us with its managed identity (the Foundry agents
# reaching /mcp/services) fails before any request leaves Azure.
az ad app update --id "$CLIENT_ID" --identifier-uris "api://$CLIENT_ID" -o none

az containerapp auth microsoft update -g "$RG" -n "$APP" --client-id "$CLIENT_ID" --client-secret-name easyauth-secret --tenant-id "$AZURE_TENANT_ID" \
  --allowed-token-audiences "api://$CLIENT_ID" --yes -o none
az containerapp auth update -g "$RG" -n "$APP" --enabled true --unauthenticated-client-action RedirectToLoginPage --redirect-provider azureactivedirectory -o none
# the CLI cannot set the provider's boolean `enabled` flag; patch the auth config through ARM
APPID=$(az containerapp show -g "$RG" -n "$APP" --query id -o tsv)
az rest --method get --url "https://management.azure.com$APPID/authConfigs/current?api-version=2024-03-01" -o json > /tmp/auth.json
python3 - <<'PY'
import json
d = json.load(open("/tmp/auth.json")); p = d["properties"]
p.setdefault("identityProviders", {}).setdefault("azureActiveDirectory", {})["enabled"] = True
p.setdefault("globalValidation", {}).update({"unauthenticatedClientAction": "RedirectToLoginPage", "redirectToProvider": "azureactivedirectory", "excludedPaths": ["/health"]})
p.setdefault("platform", {})["enabled"] = True
json.dump({"properties": p}, open("/tmp/auth-put.json", "w"))
PY
az rest --method put --url "https://management.azure.com$APPID/authConfigs/current?api-version=2024-03-01" --body @/tmp/auth-put.json -o none
echo "Entra sign-in enabled for https://$FQDN (app registration $APPREG, client $CLIENT_ID)"
