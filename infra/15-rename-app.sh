#!/usr/bin/env bash
# Give the app a new name - the free half of the domain question.
#
# A Container Apps URL is <app name>.<environment domain>, so an app called talkwithgrace answers on
# https://talkwithgrace.<env>.azurecontainerapps.io, with a valid certificate, at no extra cost. The environment's
# middle label is assigned by Azure and cannot be chosen; only a real custom domain removes it (infra/14-custom-domain.sh).
#
# Container Apps cannot be renamed in place, so this clones the running app under the new name: same image, same
# secrets, same environment variables, same /data volume, same role assignments. The OLD APP KEEPS RUNNING - nothing
# is deleted, and the old URL stays allowed in PUBLIC_BASE_ALIASES - so you can test the new one and walk away from it
# if you do not like it.
#
# Usage: infra/15-rename-app.sh talkwithgrace
set -euo pipefail
cd "$(dirname "$0")/.."
RG="${AZURE_RESOURCE_GROUP:-my-aiverse}"
OLD="${CA_APP:-bankrag}"
ENVNAME="${CA_ENV:-bankrag-env}"
NEW="${1:?usage: infra/15-rename-app.sh <new-app-name>, e.g. talkwithgrace}"
[[ "$NEW" =~ ^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$ ]] || { echo "'$NEW' is not a valid Container App name (lower-case letters, digits and hyphens, 2-32 chars)"; exit 1; }

az containerapp show -g "$RG" -n "$NEW" >/dev/null 2>&1 && { echo "'$NEW' already exists in $RG - pick another name or delete it first"; exit 1; }

OLD_FQDN=$(az containerapp show -g "$RG" -n "$OLD" --query "properties.configuration.ingress.fqdn" -o tsv)
IMAGE=$(az containerapp show -g "$RG" -n "$OLD" --query "properties.template.containers[0].image" -o tsv)
ACR="${ACR_NAME:-bankragacr}"
echo "cloning $OLD -> $NEW"
echo "  image : $IMAGE"
echo "  from  : https://$OLD_FQDN"

echo "== create"
az containerapp create -g "$RG" -n "$NEW" --environment "$ENVNAME" --image "$IMAGE" \
  --registry-server "$ACR.azurecr.io" --registry-identity system \
  --system-assigned --ingress external --target-port 8010 --transport auto \
  --cpu 0.5 --memory 1.0Gi --min-replicas 1 --max-replicas 1 -o none

NEW_PRINCIPAL=$(az containerapp identity show -g "$RG" -n "$NEW" --query principalId -o tsv)
OLD_PRINCIPAL=$(az containerapp identity show -g "$RG" -n "$OLD" --query principalId -o tsv)

echo "== roles (a new app means a new identity, so every grant is made again)"
az role assignment list --assignee "$OLD_PRINCIPAL" --all --query "[].{role:roleDefinitionId,scope:scope}" -o tsv |
  while IFS=$'\t' read -r role scope; do
    az role assignment create --assignee-object-id "$NEW_PRINCIPAL" --assignee-principal-type ServicePrincipal \
      --role "${role##*/}" --scope "$scope" -o none 2>/dev/null && echo "  granted ${role##*/} on ${scope##*/}" || echo "  exists  ${role##*/} on ${scope##*/}"
  done

echo "== secrets"
# name=value pairs are read straight into the update, NUL-separated so a value may contain anything: they never reach
# the terminal, a log or a file. bash 3.2 has no mapfile, hence the read loop.
SECRET_ARGS=()
while IFS= read -r -d '' pair; do
  SECRET_ARGS+=("$pair")
done < <(az containerapp secret list -g "$RG" -n "$OLD" --show-values -o json | python3 -c '
import json, sys
for s in json.load(sys.stdin):
    sys.stdout.write(s["name"] + "=" + (s.get("value") or "") + "\0")
')
if [ "${#SECRET_ARGS[@]}" -gt 0 ]; then
  az containerapp secret set -g "$RG" -n "$NEW" --secrets "${SECRET_ARGS[@]}" -o none
fi
echo "  copied ${#SECRET_ARGS[@]} secret(s)"

echo "== template (env vars, volume mount, scale) "
az containerapp show -g "$RG" -n "$OLD" -o yaml > /tmp/clone.yaml
python3 - "$NEW" <<'PY'
import sys, yaml
new = sys.argv[1]; path = "/tmp/clone.yaml"
d = yaml.safe_load(open(path))
d["name"] = new
for key in ("id", "systemData", "identity"):
    d.pop(key, None)
p = d.get("properties", {})
for key in ("provisioningState", "latestRevisionName", "latestRevisionFqdn", "latestReadyRevisionName",
            "eventStreamEndpoint", "outboundIpAddresses", "customDomainVerificationId", "runningStatus"):
    p.pop(key, None)
ing = p.get("configuration", {}).get("ingress", {})
ing.pop("fqdn", None)
ing.pop("customDomains", None)  # a custom domain belongs to one app; bind it again once you are happy with this one
# secrets came across in the step above; leaving the (redacted) list here would blank them
p.get("configuration", {}).pop("secrets", None)
yaml.safe_dump(d, open(path, "w"))
PY
az containerapp update -g "$RG" -n "$NEW" --yaml /tmp/clone.yaml -o none
rm -f /tmp/clone.yaml

NEW_FQDN=$(az containerapp show -g "$RG" -n "$NEW" --query "properties.configuration.ingress.fqdn" -o tsv)
echo "== app settings"
az containerapp update -g "$RG" -n "$NEW" --set-env-vars \
  "PUBLIC_BASE_URL=https://$NEW_FQDN" "PUBLIC_BASE_ALIASES=https://$OLD_FQDN" -o none

cat <<NEXT

Done: https://$NEW_FQDN
$OLD is still running on https://$OLD_FQDN and has not been touched.

Still to do, in this order:
  1. CA_APP=$NEW bash infra/12-easyauth.sh    # sign-in for the new name (the old app keeps its own)
  2. CA_APP=$NEW bash infra/13-services-tool.sh
  3. uv run bankrag skills sync               # repoints the bank-services-mcp connection at the new name
  4. Open https://$NEW_FQDN, sign in, and ask for a branch near you - check the answer really came from the tool.
  5. Happy? Then retire the old one:
       az containerapp delete -g $RG -n $OLD --yes
     Not happy? Point the connection back with:
       CA_APP=$OLD bash infra/13-services-tool.sh && uv run bankrag skills sync
NEXT
