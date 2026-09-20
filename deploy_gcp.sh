#!/usr/bin/env bash
set -e

# Complete Automated Google Cloud Provisioning & Deployment Script for Price Tracker
# Run this script to provision Firestore, deploy Cloud Run, setup Cloud Scheduler, and configure GitHub Actions CI/CD.

PROJECT_ID=${1:-"cep-demo-x"}
REGION=${2:-"us-central1"}
SERVICE_NAME=${3:-"price-tracker"}
CRON_SECRET=${4:-"super-secret-price-tracker-cron-key-12345"}

echo "=========================================================="
echo "  Price Tracker: Automated Google Cloud Deployment        "
echo "  Project: $PROJECT_ID | Region: $REGION                  "
echo "=========================================================="

# Set active project
gcloud config set project "$PROJECT_ID"

# 1. Enable Required Google Cloud APIs
echo -e "\n[1/6] Enabling required Google Cloud APIs..."
gcloud services enable \
    run.googleapis.com \
    firestore.googleapis.com \
    cloudscheduler.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    iam.googleapis.com

# 2. Provision Firestore Native Database
echo -e "\n[2/6] Provisioning Google Cloud Firestore database..."
DB_EXISTS=$(gcloud firestore databases list --format="value(name)" 2>/dev/null || true)
if [ -z "$DB_EXISTS" ]; then
    echo "Creating Firestore Native (default) database in $REGION..."
    gcloud firestore databases create --location="$REGION" --type=firestore-native
else
    echo "Firestore database already provisioned."
fi

# 3. Create Service Accounts & Grant IAM Permissions
echo -e "\n[3/6] Setting up Service Accounts and IAM permissions..."
RUNNER_SA="price-tracker-runner"
RUNNER_EMAIL="$RUNNER_SA@$PROJECT_ID.iam.gserviceaccount.com"
SCHEDULER_SA="price-tracker-scheduler"
SCHEDULER_EMAIL="$SCHEDULER_SA@$PROJECT_ID.iam.gserviceaccount.com"

# Create Cloud Run runtime service account
if ! gcloud iam service-accounts list --filter="email:$RUNNER_EMAIL" --format="value(email)" 2>/dev/null | grep -q "$RUNNER_EMAIL"; then
    gcloud iam service-accounts create "$RUNNER_SA" --display-name="Price Tracker Cloud Run Runner"
fi

# Grant Firestore access (roles/datastore.user)
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$RUNNER_EMAIL" \
    --role="roles/datastore.user" --quiet

# Create Cloud Scheduler service account
if ! gcloud iam service-accounts list --filter="email:$SCHEDULER_EMAIL" --format="value(email)" 2>/dev/null | grep -q "$SCHEDULER_EMAIL"; then
    gcloud iam service-accounts create "$SCHEDULER_SA" --display-name="Price Tracker Cloud Scheduler Invoker"
fi

# 4. Build and Deploy Application to Google Cloud Run
echo -e "\n[4/6] Building container and deploying to Google Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --platform managed \
    --region "$REGION" \
    --allow-unauthenticated \
    --service-account "$RUNNER_EMAIL" \
    --set-env-vars GCP_PROJECT="$PROJECT_ID",CRON_SECRET="$CRON_SECRET" \
    --timeout 120s \
    --memory 512Mi \
    --cpu 1 \
    --quiet

# Retrieve Cloud Run Service URL
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --platform managed --region "$REGION" --format="value(status.url)")
echo "Cloud Run Service URL: $SERVICE_URL"

# Grant Cloud Scheduler permission to invoke the Cloud Run service
gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
    --platform managed \
    --region "$REGION" \
    --member="serviceAccount:$SCHEDULER_EMAIL" \
    --role="roles/run.invoker" --quiet

# 5. Set up Google Cloud Scheduler Job for Daily Scraping
echo -e "\n[5/6] Configuring Cloud Scheduler daily scraping job..."
JOB_NAME="price-tracker-daily-scrape"
JOB_URI="$SERVICE_URL/api/scrape-all"

if gcloud scheduler jobs list --location="$REGION" --filter="name:$JOB_NAME" --format="value(name)" 2>/dev/null | grep -q "$JOB_NAME"; then
    echo "Updating existing Cloud Scheduler job..."
    gcloud scheduler jobs update http "$JOB_NAME" \
        --location="$REGION" \
        --schedule="0 0 * * *" \
        --uri="$JOB_URI" \
        --http-method=POST \
        --headers="X-Cron-Secret=$CRON_SECRET,Content-Type=application/json" \
        --oidc-service-account-email="$SCHEDULER_EMAIL" \
        --oidc-token-audience="$SERVICE_URL" \
        --quiet
else
    echo "Creating new Cloud Scheduler job (running daily at 00:00 UTC)..."
    gcloud scheduler jobs create http "$JOB_NAME" \
        --location="$REGION" \
        --schedule="0 0 * * *" \
        --uri="$JOB_URI" \
        --http-method=POST \
        --headers="X-Cron-Secret=$CRON_SECRET,Content-Type=application/json" \
        --oidc-service-account-email="$SCHEDULER_EMAIL" \
        --oidc-token-audience="$SERVICE_URL" \
        --quiet
fi

# 6. Configure GitHub Actions CI/CD Secrets (Full Automation)
echo -e "\n[6/6] Configuring GitHub Actions CI/CD automation..."
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
    DEPLOYER_SA="github-deployer"
    DEPLOYER_EMAIL="$DEPLOYER_SA@$PROJECT_ID.iam.gserviceaccount.com"

    if ! gcloud iam service-accounts list --filter="email:$DEPLOYER_EMAIL" --format="value(email)" 2>/dev/null | grep -q "$DEPLOYER_EMAIL"; then
        gcloud iam service-accounts create "$DEPLOYER_SA" --display-name="GitHub Actions Deployer"
    fi

    # Grant deployment roles to GitHub deployer
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$DEPLOYER_EMAIL" --role="roles/run.admin" --quiet
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$DEPLOYER_EMAIL" --role="roles/cloudbuild.builds.editor" --quiet
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$DEPLOYER_EMAIL" --role="roles/storage.admin" --quiet
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$DEPLOYER_EMAIL" --role="roles/iam.serviceAccountUser" --quiet

    # Configure Workload Identity Federation (Keyless GitHub Authentication)
    PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

    if ! gcloud iam workload-identity-pools list --location="global" --filter="name:github-pool" --format="value(name)" 2>/dev/null | grep -q "github-pool"; then
        gcloud iam workload-identity-pools create github-pool --location="global" --display-name="GitHub Actions Pool" --quiet
    fi

    if ! gcloud iam workload-identity-pools providers list --workload-identity-pool="github-pool" --location="global" --filter="name:github-provider" --format="value(name)" 2>/dev/null | grep -q "github-provider"; then
        gcloud iam workload-identity-pools providers create-oidc github-provider \
            --location="global" \
            --workload-identity-pool="github-pool" \
            --display-name="GitHub Provider" \
            --issuer-uri="https://token.actions.githubusercontent.com" \
            --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
            --attribute-condition="assertion.repository == 'srikanna/price-tracker'" \
            --quiet
    fi

    gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_EMAIL" \
        --role="roles/iam.workloadIdentityUser" \
        --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/srikanna/price-tracker" \
        --quiet

    gh secret set CRON_SECRET --body "$CRON_SECRET" 2>/dev/null || true
    echo "Workload Identity Federation configured! Any git push to 'main' will automatically build and deploy."
else
    echo "GitHub CLI not authenticated or not installed."
fi

echo "=========================================================="
echo "  DEPLOYMENT COMPLETE!                                    "
echo "  App URL: $SERVICE_URL                                   "
echo "  Secure Scrape Endpoint: $JOB_URI                        "
echo "  Scheduler Job: $JOB_NAME (Daily at 00:00 UTC)           "
echo "=========================================================="
