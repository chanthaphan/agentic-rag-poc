#!/usr/bin/env bash
# Give the Foundry PROJECT a system-assigned managed identity and grant it
# read access to the search service (needed for the ProjectManagedIdentity
# MCP connection to Foundry IQ knowledge bases).
set -euo pipefail
SUB="${AZURE_SUBSCRIPTION_ID:-961d5838-fa9f-4a0a-837a-268292eabc92}"
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
ACCOUNT="${FOUNDRY_ACCOUNT:-my-model-hub}"
PROJECT="${FOUNDRY_PROJECT:-firstProject}"
SEARCH="${SEARCH_SERVICE_NAME:?set SEARCH_SERVICE_NAME}"

PROJECT_ID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT/projects/$PROJECT"
SEARCH_ID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Search/searchServices/$SEARCH"

PRINCIPAL=$(az rest --method get --url "https://management.azure.com$PROJECT_ID?api-version=2025-06-01" --query identity.principalId -o tsv || true)
if [ -z "$PRINCIPAL" ] || [ "$PRINCIPAL" = "None" ]; then
  echo "Enabling system-assigned identity on project $PROJECT ..."
  az rest --method patch --url "https://management.azure.com$PROJECT_ID?api-version=2025-06-01" \
    --body '{"identity":{"type":"SystemAssigned"}}' -o none
  sleep 10
  PRINCIPAL=$(az rest --method get --url "https://management.azure.com$PROJECT_ID?api-version=2025-06-01" --query identity.principalId -o tsv)
fi
echo "Project principal id: $PRINCIPAL"

for role in "Search Index Data Reader" "Search Service Contributor"; do
  az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
    --role "$role" --scope "$SEARCH_ID" -o none 2>/dev/null && echo "Assigned: $role" || echo "Role assignment exists or failed: $role"
done

# Also let the signed-in user use RBAC on the search service (data plane) for local testing.
ME=$(az ad signed-in-user show --query id -o tsv)
for role in "Search Service Contributor" "Search Index Data Contributor"; do
  az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
    --role "$role" --scope "$SEARCH_ID" -o none 2>/dev/null && echo "Assigned to you: $role" || echo "Role assignment exists or failed (you): $role"
done
