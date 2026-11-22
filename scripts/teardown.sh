#!/usr/bin/env bash
# Delete what scripts/deploy.sh created, so nothing keeps billing. Asks first.
# Keeps the secrets (free at this size) unless DELETE_SECRETS=1.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}}"
REGION="${REGION:-asia-south1}"
SQL_INSTANCE="${SQL_INSTANCE:-revagent-pg}"
SERVICE="${SERVICE:-api}"
JOB="${JOB:-worker}"
g() { gcloud --project "$PROJECT" --quiet "$@"; }

echo "This DELETES in $PROJECT: Cloud SQL $SQL_INSTANCE (and all its data), service $SERVICE, job $JOB."
read -r -p "Type the project id to confirm: " ok
[ "$ok" = "$PROJECT" ] || { echo "aborted"; exit 1; }

g run services delete "$SERVICE" --region="$REGION" || true
g run jobs delete "$JOB" --region="$REGION" || true
g sql instances delete "$SQL_INSTANCE" || true
if [ "${DELETE_SECRETS:-0}" = "1" ]; then
  for s in DB_PASSWORD API_TOKEN CRUSTDATA_API_KEY GSTIN_API_KEY REGISTRY_API_KEY; do
    g secrets delete "$s" 2>/dev/null || true
  done
fi
echo "Done."
