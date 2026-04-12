#!/bin/bash

# 1. Resolve the script's directory so the .env path works regardless of cwd
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

if [ -f "$ENV_FILE" ]; then
    echo "Loading secrets from $ENV_FILE ..."
    set -a
    source "$ENV_FILE"
    set +a
else
    echo "No .env file found. Expecting variables from the CI/CD environment..."
fi

# Verify that required variables are set
if [ -z "$SLACK_SIGNING_SECRET" ] || [ -z "$SLACK_BOT_TOKEN" ]; then
    echo "ERROR: SLACK_SIGNING_SECRET or SLACK_BOT_TOKEN is empty. Aborting deploy."
    exit 1
fi


# 2. Deploy the Cloud Run service using the injected variables
gcloud run deploy slack-receiver \
  --source . \
  --region=northamerica-northeast2 \
  --allow-unauthenticated \
  --set-build-env-vars GOOGLE_RUNTIME_VERSION="3.13" \
  --set-env-vars GCP_PROJECT_ID=valor-sales,PUBSUB_TOPIC_ID=slack-events-topic,SLACK_SIGNING_SECRET=${SLACK_SIGNING_SECRET},SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}