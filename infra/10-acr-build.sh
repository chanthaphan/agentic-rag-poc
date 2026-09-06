#!/usr/bin/env bash
# Build the container image in Azure Container Registry (no local Docker needed). ACR Basic ~ $5/month.
set -euo pipefail
cd "$(dirname "$0")/.."
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
LOCATION="${LOCATION:-eastus2}"
ACR="${ACR_NAME:-bankragacr}"
TAG="${IMAGE_TAG:-$(date +%Y%m%d%H%M)}"
az acr show -g "$RG" -n "$ACR" >/dev/null 2>&1 || az acr create -g "$RG" -n "$ACR" --sku Basic --location "$LOCATION" --admin-enabled false -o none
az acr build -r "$ACR" -t "bankrag:$TAG" -t "bankrag:latest" --platform linux/amd64 . 
echo "IMAGE=$ACR.azurecr.io/bankrag:$TAG"
