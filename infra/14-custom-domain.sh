#!/usr/bin/env bash
# Put the app on a custom domain (chat.example.com) instead of the *.azurecontainerapps.io name.
#
# You need to already own the domain: this script does not buy one. The certificate is a Container Apps MANAGED
# certificate, which is free and renews itself - no purchase, no upload, no expiry to diary.
#
# Run it twice. The first run prints the two DNS records to create and stops; the second, once they resolve, binds the
# hostname and moves the app onto it. Nothing is destructive: the old *.azurecontainerapps.io name keeps working, and
# stays in PUBLIC_BASE_ALIASES so a Foundry agent calling the old name is still answered rather than 421'd.
#
# Usage: infra/14-custom-domain.sh chat.example.com
set -euo pipefail
cd "$(dirname "$0")/.."
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
APP="${CA_APP:-talkwithgrace}"
ENVNAME="${CA_ENV:-bankrag-env}"
HOST="${1:?usage: infra/14-custom-domain.sh <hostname>, e.g. chat.example.com}"

FQDN=$(az containerapp show -g "$RG" -n "$APP" --query "properties.configuration.ingress.fqdn" -o tsv)
VERIFY=$(az containerapp show -g "$RG" -n "$APP" --query "properties.customDomainVerificationId" -o tsv)
SUBDOMAIN="${HOST%%.*}"
APEX="${HOST#*.}"
[ "$SUBDOMAIN" = "$HOST" ] && APEX="$HOST"  # an apex domain has no label to strip

echo "== DNS records for $HOST"
if [ "$SUBDOMAIN" = "$HOST" ]; then
  IP=$(az containerapp env show -g "$RG" -n "$ENVNAME" --query "properties.staticIp" -o tsv)
  echo "  A    @                  $IP"
  echo "  TXT  asuid              $VERIFY"
else
  echo "  CNAME  $SUBDOMAIN            $FQDN"
  echo "  TXT    asuid.$SUBDOMAIN      $VERIFY"
fi
echo

echo "== checking DNS"
RESOLVED=$(dig +short TXT "asuid.$SUBDOMAIN.$APEX" 2>/dev/null | tr -d '"' | head -1)
if [ "$SUBDOMAIN" = "$HOST" ]; then RESOLVED=$(dig +short TXT "asuid.$HOST" 2>/dev/null | tr -d '"' | head -1); fi
if [ "$RESOLVED" != "$VERIFY" ]; then
  echo "  asuid TXT is '${RESOLVED:-not set}', expected '$VERIFY'"
  echo "  Create the records above, wait for them to propagate, then run this again."
  exit 0
fi
echo "  verified"

echo "== hostname + managed certificate (free, auto-renewing)"
az containerapp hostname add -g "$RG" -n "$APP" --hostname "$HOST" -o none 2>/dev/null || true
az containerapp hostname bind -g "$RG" -n "$APP" --hostname "$HOST" --environment "$ENVNAME" --validation-method CNAME -o none

echo "== app settings"
# The custom name becomes canonical (it is what the Foundry connection will target); the old one stays allowed so the
# agent keeps working until `bankrag skills sync` has repointed the connection and you have tested a tool call.
az containerapp update -g "$RG" -n "$APP" --set-env-vars \
  "PUBLIC_BASE_URL=https://$HOST" \
  "PUBLIC_BASE_ALIASES=https://$FQDN" -o none

cat <<NEXT

Done: https://$HOST

Still to do, in this order:
  1. bash infra/12-easyauth.sh     # adds the new callback, keeps the old one
  2. uv run bankrag skills sync    # repoints the bank-services-mcp connection at the new name
  3. Ask the assistant for a branch near you and check the answer really came from the tool.
  4. Only then, drop the old name:
       az containerapp update -g $RG -n $APP --set-env-vars PUBLIC_BASE_ALIASES=""
     and remove https://$FQDN/.auth/login/aad/callback from the app registration.
NEXT
