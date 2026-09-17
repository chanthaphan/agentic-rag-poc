#!/usr/bin/env bash
# Expose the live-service MCP tools (/mcp/services) to external MCP clients. The app's own agents call the same
# tools in-process and need none of this.
#
# The endpoint stays BEHIND the app's Easy Auth: a caller authenticates with its Entra identity and the app then
# checks the caller's object id against MCP_CALLER_PRINCIPALS. Nothing is exposed publicly.
#
# Sets on the container app:
#   PUBLIC_BASE_URL         https://<fqdn>          the app's own name (Host allow-list of the MCP transport)
#   MCP_CALLER_PRINCIPALS   <object ids>            comma-separated identities allowed to call the tools
# Usage: MCP_CALLERS=<oid>[,<oid>] ./infra/13-services-tool.sh   (run infra/12-easyauth.sh first)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
APP="${CA_APP:-talkwithgrace}"
CALLERS="${MCP_CALLERS:?set MCP_CALLERS to the object id(s) allowed to call /mcp/services}"

FQDN=$(az containerapp show -g "$RG" -n "$APP" --query "properties.configuration.ingress.fqdn" -o tsv)
[ -n "$FQDN" ] || { echo "no ingress on $APP; run infra/11-containerapp.sh first"; exit 1; }

echo "app            : https://$FQDN"
echo "allowed callers: $CALLERS"

az containerapp update -g "$RG" -n "$APP" --set-env-vars \
  "PUBLIC_BASE_URL=https://$FQDN" \
  "MCP_CALLER_PRINCIPALS=$CALLERS" -o none

echo "done: /mcp/services answers the listed identities through Easy Auth"
