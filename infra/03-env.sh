#!/usr/bin/env bash
# Collect endpoints and keys into .env (creates it from .env.example if missing).
set -euo pipefail
cd "$(dirname "$0")/.."
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
ACCOUNT="${FOUNDRY_ACCOUNT:-my-model-hub}"
SEARCH="${SEARCH_SERVICE_NAME:?set SEARCH_SERVICE_NAME}"
[ -f .env ] || cp .env.example .env

set_var() { # key value
  if grep -q "^$1=" .env; then
    sed -i '' "s|^$1=.*|$1=$2|" .env
  else
    echo "$1=$2" >> .env
  fi
}
set_var SEARCH_SERVICE_NAME "$SEARCH"
set_var SEARCH_ENDPOINT "https://$SEARCH.search.windows.net"
set_var SEARCH_ADMIN_KEY "$(az search admin-key show -g "$RG" --service-name "$SEARCH" --query primaryKey -o tsv)"
set_var SEARCH_QUERY_KEY "$(az search query-key list -g "$RG" --service-name "$SEARCH" --query '[0].key' -o tsv)"
set_var AOAI_API_KEY "$(az cognitiveservices account keys list -g "$RG" -n "$ACCOUNT" --query key1 -o tsv)"
echo ".env updated (keys redacted):"
sed -E 's/(KEY=).+/\1<set>/' .env
