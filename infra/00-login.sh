#!/usr/bin/env bash
# Sign in to the PERSONAL tenant used by this POC and select its subscription.
# Interactive (MFA) - run this yourself in a terminal.
set -euo pipefail
TENANT="${AZURE_TENANT_ID:-379bc25f-3328-4548-a99f-41ef31905732}"
SUB="${AZURE_SUBSCRIPTION_ID:-961d5838-fa9f-4a0a-837a-268292eabc92}"
az login --tenant "$TENANT"
az account set --subscription "$SUB"
az account show --query "{name:name, id:id, tenant:tenantId, user:user.name}" -o table
