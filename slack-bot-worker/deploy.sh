#!/bin/bash

# 1. Load the local secrets if the file exists
if [ -f .env ]; then
    echo "Loading secrets from .env file..."
    source .env
else
    echo "No .env file found. Expecting variables from the CI/CD environment..."
fi

# 2. Deploy the Cloud Run service using the injected variables
gcloud run deploy slack-bot-worker \
  --source . \
  --region=northamerica-northeast2 \
  --no-allow-unauthenticated \
  --set-build-env-vars GOOGLE_RUNTIME_VERSION="3.13" \
  --set-env-vars GCP_PROJECT_ID=valor-sales,SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}
  