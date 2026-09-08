#!/usr/bin/env bash
# Grant the Foundry PROJECT's managed identity the "Foundry Agent Consumer" role on the project, so the RemoteA2A
# connections (auth ProjectManagedIdentity) may call the skill agents' A2A endpoints from the bank-concierge agent.
# Run once per project (needs Owner / User Access Administrator on the subscription). Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
PROJECT_ID="/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/$AZURE_RESOURCE_GROUP/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY_ACCOUNT/projects/$FOUNDRY_PROJECT"
PRINCIPAL=$(az rest --method get --url "https://management.azure.com$PROJECT_ID?api-version=2025-06-01" --query identity.principalId -o tsv)
echo "Project principal id: $PRINCIPAL"
az role assignment create --assignee-object-id "$PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role "eed3b665-ab3a-47b6-8f48-c9382fb1dad6" --scope "$PROJECT_ID" -o none && echo "Foundry Agent Consumer granted on $PROJECT_ID"
