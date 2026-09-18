#!/usr/bin/env bash
# Create the Free-tier Azure AI Search service, trying regions in order
# (East US 2 is often capacity constrained). Enables RBAC + API keys (aadOrApiKey)
# so the app identity can read the knowledge bases.
set -euo pipefail
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
NAME="${SEARCH_SERVICE_NAME:-bankrag-search-$(printf '%04x' $RANDOM)}"
SKU="${SEARCH_SKU:-free}"
REGIONS=(${SEARCH_REGIONS:-eastus2 southcentralus northcentralus})

if az search service show -g "$RG" -n "$NAME" >/dev/null 2>&1; then
  echo "Search service $NAME already exists in $RG"
else
  for region in "${REGIONS[@]}"; do
    echo "Trying to create $NAME ($SKU) in $region ..."
    if az search service create -g "$RG" -n "$NAME" --sku "$SKU" --location "$region" \
         --auth-options aadOrApiKey --aad-auth-failure-mode http401WithBearerChallenge \
         --partition-count 1 --replica-count 1 -o none; then
      echo "Created $NAME in $region"
      break
    else
      echo "Failed in $region, trying next region"
    fi
  done
fi
az search service show -g "$RG" -n "$NAME" --query "{name:name, location:location, sku:sku.name, status:status, hosting:hostingMode}" -o table
echo "SEARCH_SERVICE_NAME=$NAME"
