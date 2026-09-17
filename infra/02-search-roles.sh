#!/usr/bin/env bash
# Grant the search-service roles the app needs: the signed-in user (local runs) and, when given, the container app's
# managed identity (CA_PRINCIPAL_ID). The app calls the knowledge-base MCP endpoint with its own identity
# (KB_MCP_AUTH=identity), so the caller needs Search Index Data Reader; the admin roles cover index/KB management.
set -euo pipefail
SUB="${AZURE_SUBSCRIPTION_ID:-961d5838-fa9f-4a0a-837a-268292eabc92}"
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
SEARCH="${SEARCH_SERVICE_NAME:?set SEARCH_SERVICE_NAME}"
SEARCH_ID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Search/searchServices/$SEARCH"

ME=$(az ad signed-in-user show --query id -o tsv)
for role in "Search Service Contributor" "Search Index Data Contributor" "Search Index Data Reader"; do
  az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
    --role "$role" --scope "$SEARCH_ID" -o none 2>/dev/null && echo "Assigned to you: $role" || echo "Role assignment exists or failed (you): $role"
done

if [ -n "${CA_PRINCIPAL_ID:-}" ]; then
  for role in "Search Index Data Reader"; do
    az role assignment create --assignee-object-id "$CA_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
      --role "$role" --scope "$SEARCH_ID" -o none 2>/dev/null && echo "Assigned to the app: $role" || echo "Role assignment exists or failed (app): $role"
  done
fi
