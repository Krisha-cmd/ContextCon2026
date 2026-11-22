#!/usr/bin/env bash
# Deploy revagent to GCP in one command. Safe to re-run: every step is "create if missing",
# then update. Secret values are read from .env or generated; none are ever printed.
#
#   scripts/deploy.sh            # asks before creating billable resources
#   YES=1 scripts/deploy.sh      # no prompt
#
# Overridable: PROJECT REGION SQL_INSTANCE SQL_TIER DB_NAME DB_USER SA_NAME SERVICE JOB
#              ALLOW_PUBLIC (1 = API reachable without a Google identity; it still needs
#              the API_TOKEN bearer)
set -euo pipefail

cd "$(dirname "$0")/.."

env_val() {  # value of KEY in .env, or empty. Never echoed.
  [ -f .env ] && grep -E "^$1=" .env | head -1 | cut -d= -f2- || true
}

PROJECT="${PROJECT:-${GCP_PROJECT:-$(env_val GCP_PROJECT)}}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-asia-south1}"
SQL_INSTANCE="${SQL_INSTANCE:-revagent-pg}"
SQL_TIER="${SQL_TIER:-db-f1-micro}"
DB_NAME="${DB_NAME:-revagent}"
DB_USER="${DB_USER:-revagent}"
SA_NAME="${SA_NAME:-revagent-run}"
SERVICE="${SERVICE:-api}"
JOB="${JOB:-worker}"
ALLOW_PUBLIC="${ALLOW_PUBLIC:-1}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
CONN="${PROJECT}:${REGION}:${SQL_INSTANCE}"
SAVED_QUERY_ID="revagent-job-logs"

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
g() { gcloud --project "$PROJECT" --quiet "$@"; }

[ -n "$PROJECT" ] || die "no GCP project: set GCP_PROJECT in .env or run gcloud config set project"
command -v gcloud >/dev/null || die "gcloud not installed"

cat <<EOF
Deploying revagent
  project       $PROJECT
  region        $REGION
  Cloud SQL     $SQL_INSTANCE (Postgres 16, $SQL_TIER, 10 GB HDD, zonal, no backups)
  Cloud Run     service "$SERVICE" + job "$JOB", service account $SA

Billable: the Cloud SQL instance runs 24/7 (smallest tier, roughly USD 9-10 a month) and
is charged to the project's billing account, including any free-trial credits. Cloud Run,
Cloud Build and Secret Manager stay small at this scale. Delete with: scripts/teardown.sh
EOF
if [ "${YES:-0}" != "1" ]; then
  read -r -p "Continue? [y/N] " ok
  [ "$ok" = "y" ] || [ "$ok" = "Y" ] || die "aborted"
fi

step "Enabling APIs"
g services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com logging.googleapis.com \
  aiplatform.googleapis.com iam.googleapis.com

step "Service account $SA"
if ! g iam service-accounts describe "$SA" >/dev/null 2>&1; then
  g iam service-accounts create "$SA_NAME" --display-name="revagent api + worker"
fi
for role in roles/cloudsql.client roles/secretmanager.secretAccessor roles/aiplatform.user \
            roles/run.invoker roles/logging.logWriter; do
  g projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="$role" \
    --condition=None >/dev/null
done

step "Secrets"
put_secret() {  # name value: create, or add a version only when the value changed
  local name="$1" value="$2"
  if ! g secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | g secrets create "$name" --replication-policy=automatic --data-file=- >/dev/null
    echo "  created $name"
  elif [ "$(g secrets versions access latest --secret="$name" 2>/dev/null)" != "$value" ]; then
    printf '%s' "$value" | g secrets versions add "$name" --data-file=- >/dev/null
    echo "  updated $name"
  else
    echo "  unchanged $name"
  fi
}
secret_or_new() {  # existing secret value, or a fresh random one
  g secrets versions access latest --secret="$1" 2>/dev/null || openssl rand -hex 24
}
DB_PASSWORD="$(secret_or_new DB_PASSWORD)"
put_secret DB_PASSWORD "$DB_PASSWORD"
put_secret API_TOKEN "$(secret_or_new API_TOKEN)"
SECRETS="DB_PASSWORD=DB_PASSWORD:latest,API_TOKEN=API_TOKEN:latest"
for key in CRUSTDATA_API_KEY GSTIN_API_KEY REGISTRY_API_KEY; do
  val="$(env_val "$key")"
  if [ -n "$val" ]; then
    put_secret "$key" "$val"
    SECRETS="$SECRETS,$key=$key:latest"
  else
    echo "  skipped $key (not in .env)"
  fi
done

step "Cloud SQL $SQL_INSTANCE (this takes ~10 minutes the first time)"
if ! g sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  g sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_16 --edition=ENTERPRISE \
    --tier="$SQL_TIER" --region="$REGION" --availability-type=zonal --storage-type=HDD \
    --storage-size=10 --no-storage-auto-increase --no-backup
fi
g sql databases describe "$DB_NAME" --instance="$SQL_INSTANCE" >/dev/null 2>&1 \
  || g sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"
if g sql users list --instance="$SQL_INSTANCE" --format="value(name)" | grep -qx "$DB_USER"; then
  g sql users set-password "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASSWORD" >/dev/null
else
  g sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASSWORD"
fi
# Users made by gcloud get cloudsqlsuperuser, which may CREATE EXTENSION vector (schema.sql).

ENV_VARS="GCP_PROJECT=$PROJECT,GCP_REGION=$REGION,DB_USER=$DB_USER,DB_NAME=$DB_NAME,DB_HOST=/cloudsql/$CONN"

step "Cloud Run service $SERVICE (build from source)"
AUTH_FLAG="--no-allow-unauthenticated"
[ "$ALLOW_PUBLIC" = "1" ] && AUTH_FLAG="--allow-unauthenticated"
g run deploy "$SERVICE" --source . --region="$REGION" --service-account="$SA" --port=8000 \
  --add-cloudsql-instances="$CONN" --set-env-vars="$ENV_VARS,START_MODE=cloud_run" \
  --set-secrets="$SECRETS" --min-instances=0 --max-instances=2 --memory=512Mi \
  "$AUTH_FLAG" \
  || die "deploy failed. If the org blocks public services, re-run with ALLOW_PUBLIC=0"
IMAGE="$(g run services describe "$SERVICE" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].image)')"
URL="$(g run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"

step "Cloud Run job $JOB (same image)"
g run jobs deploy "$JOB" --image="$IMAGE" --region="$REGION" --service-account="$SA" \
  --set-cloudsql-instances="$CONN" --set-env-vars="$ENV_VARS" --set-secrets="$SECRETS" \
  --command=python --args=-m,app.runner.worker,--until-idle \
  --tasks=1 --max-retries=1 --task-timeout=3600 --memory=1Gi
# The API passes RUN_JOB_ID as an env override, which run.invoker alone doesn't allow.
g run jobs add-iam-policy-binding "$JOB" --region="$REGION" --member="serviceAccount:$SA" \
  --role=roles/run.jobsExecutorWithOverrides >/dev/null

step "Applying schema.sql (one worker execution: init_db, then idle exit)"
g run jobs execute "$JOB" --region="$REGION" --wait

step "Health check"
if [ "$ALLOW_PUBLIC" = "1" ]; then
  curl -fsS "$URL/health"; echo
else
  curl -fsS -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/health"; echo
fi

step "Structured logs in Cloud Logging"
sleep 10
FILTER="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SERVICE\" AND jsonPayload.logger:*"
g logging read "$FILTER" --freshness=15m --limit=3 \
  --format='table(timestamp,severity,jsonPayload.logger,jsonPayload.msg)' \
  || echo "  (no structured lines yet; check the Logs Explorer)"

step "Saved query \"$SAVED_QUERY_ID\" (logs for one job_id)"
QUERY='(resource.type="cloud_run_revision" OR resource.type="cloud_run_job")\njsonPayload.job_id="PASTE-JOB-ID"'
curl -fsS -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: application/json" \
  "https://logging.googleapis.com/v2/projects/$PROJECT/locations/global/savedQueries?savedQueryId=$SAVED_QUERY_ID" \
  -d "{\"displayName\":\"revagent: one job\",\"description\":\"Replace PASTE-JOB-ID with a job id\",\"visibility\":\"SHARED\",\"loggingQuery\":{\"filter\":\"$QUERY\"}}" \
  >/dev/null 2>&1 && echo "  saved" || echo "  exists already or not permitted; filter: $QUERY"

cat <<EOF

Done.
  API        $URL
  token      gcloud secrets versions access latest --secret=API_TOKEN --project=$PROJECT
  web UI     open $URL/?token=\$TOKEN once (sets a cookie; plain $URL after that)
  API        add the header "x-api-token: \$TOKEN" (JSON with Accept: application/json)
  job logs   Logs Explorer > Saved queries > "revagent: one job"
EOF
