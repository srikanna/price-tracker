# Complete Automated Google Cloud Provisioning & Deployment Script for Price Tracker
# Run this script to provision Firestore, deploy Cloud Run, setup Cloud Scheduler, and configure GitHub Actions CI/CD.

param (
    [string]$ProjectID = "cep-demo-x",
    [string]$Region = "us-central1",
    [string]$ServiceName = "price-tracker",
    [string]$CronSecret = "super-secret-price-tracker-cron-key-12345"
)

$ErrorActionPreference = "Stop"

# Ensure tools are on path
$env:Path = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin;C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI;" + $env:Path

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Price Tracker: Automated Google Cloud Deployment        " -ForegroundColor Cyan
Write-Host "  Project: $ProjectID | Region: $Region                   " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# Set active project
gcloud.cmd config set project $ProjectID

# 1. Enable Required Google Cloud APIs
Write-Host "`n[1/6] Enabling required Google Cloud APIs..." -ForegroundColor Yellow
gcloud.cmd services enable `
    run.googleapis.com `
    firestore.googleapis.com `
    cloudscheduler.googleapis.com `
    cloudbuild.googleapis.com `
    artifactregistry.googleapis.com `
    iam.googleapis.com

# 2. Provision Firestore Native Database
Write-Host "`n[2/6] Provisioning Google Cloud Firestore database..." -ForegroundColor Yellow
$dbList = gcloud.cmd firestore databases list --format="value(name)" 2>$null
if (-not $dbList) {
    Write-Host "Creating Firestore Native (default) database in $Region..." -ForegroundColor Green
    gcloud.cmd firestore databases create --location=$Region --type=firestore-native
} else {
    Write-Host "Firestore database already provisioned." -ForegroundColor Green
}

# 3. Create Service Accounts & Grant IAM Permissions
Write-Host "`n[3/6] Setting up Service Accounts and IAM permissions..." -ForegroundColor Yellow

$RunnerSA = "price-tracker-runner"
$RunnerEmail = "$RunnerSA@$ProjectID.iam.gserviceaccount.com"
$SchedulerSA = "price-tracker-scheduler"
$SchedulerEmail = "$SchedulerSA@$ProjectID.iam.gserviceaccount.com"

# Create Cloud Run runtime service account
$saCheck = gcloud.cmd iam service-accounts list --filter="email:$RunnerEmail" --format="value(email)" 2>$null
if (-not $saCheck) {
    gcloud.cmd iam service-accounts create $RunnerSA --display-name="Price Tracker Cloud Run Runner"
}
# Grant Firestore access (roles/datastore.user)
gcloud.cmd projects add-iam-policy-binding $ProjectID `
    --member="serviceAccount:$RunnerEmail" `
    --role="roles/datastore.user" --quiet

# Create Cloud Scheduler service account
$schedCheck = gcloud.cmd iam service-accounts list --filter="email:$SchedulerEmail" --format="value(email)" 2>$null
if (-not $schedCheck) {
    gcloud.cmd iam service-accounts create $SchedulerSA --display-name="Price Tracker Cloud Scheduler Invoker"
}

# 4. Build and Deploy Application to Google Cloud Run
Write-Host "`n[4/6] Building container and deploying to Google Cloud Run..." -ForegroundColor Yellow
gcloud.cmd run deploy $ServiceName `
    --source . `
    --platform managed `
    --region $Region `
    --allow-unauthenticated `
    --service-account $RunnerEmail `
    --set-env-vars GCP_PROJECT=$ProjectID,CRON_SECRET=$CronSecret `
    --timeout 120s `
    --memory 512Mi `
    --cpu 1 `
    --quiet

# Retrieve Cloud Run Service URL
$ServiceUrl = (gcloud.cmd run services describe $ServiceName --platform managed --region $Region --format="value(status.url)").Trim()
Write-Host "Cloud Run Service URL: $ServiceUrl" -ForegroundColor Green

# Grant Cloud Scheduler permission to invoke the Cloud Run service
gcloud.cmd run services add-iam-policy-binding $ServiceName `
    --platform managed `
    --region $Region `
    --member="serviceAccount:$SchedulerEmail" `
    --role="roles/run.invoker" --quiet

# 5. Set up Google Cloud Scheduler Job for Daily Scraping
Write-Host "`n[5/6] Configuring Cloud Scheduler daily scraping job..." -ForegroundColor Yellow
$JobName = "price-tracker-daily-scrape"
$JobUri = "$ServiceUrl/api/scrape-all"

$jobExists = gcloud.cmd scheduler jobs list --location=$Region --filter="name:$JobName" --format="value(name)" 2>$null

if ($jobExists) {
    Write-Host "Updating existing Cloud Scheduler job..." -ForegroundColor Green
    gcloud.cmd scheduler jobs update http $JobName `
        --location=$Region `
        --schedule="0 0 * * *" `
        --uri=$JobUri `
        --http-method=POST `
        --headers="X-Cron-Secret=$CronSecret,Content-Type=application/json" `
        --oidc-service-account-email=$SchedulerEmail `
        --oidc-token-audience=$ServiceUrl `
        --quiet
} else {
    Write-Host "Creating new Cloud Scheduler job (running daily at 00:00 UTC)..." -ForegroundColor Green
    gcloud.cmd scheduler jobs create http $JobName `
        --location=$Region `
        --schedule="0 0 * * *" `
        --uri=$JobUri `
        --http-method=POST `
        --headers="X-Cron-Secret=$CronSecret,Content-Type=application/json" `
        --oidc-service-account-email=$SchedulerEmail `
        --oidc-token-audience=$ServiceUrl `
        --quiet
}

# 6. Configure GitHub Actions CI/CD Secrets (Full Automation)
Write-Host "`n[6/6] Configuring GitHub Actions CI/CD automation..." -ForegroundColor Yellow
$ghAuth = gh auth status 2>&1
if ($LASTEXITCODE -eq 0) {
    $DeployerSA = "github-deployer"
    $DeployerEmail = "$DeployerSA@$ProjectID.iam.gserviceaccount.com"
    $deployerCheck = gcloud.cmd iam service-accounts list --filter="email:$DeployerEmail" --format="value(email)" 2>$null
    if (-not $deployerCheck) {
        gcloud.cmd iam service-accounts create $DeployerSA --display-name="GitHub Actions Deployer"
    }

    # Grant deployment roles to GitHub deployer
    gcloud.cmd projects add-iam-policy-binding $ProjectID --member="serviceAccount:$DeployerEmail" --role="roles/run.admin" --quiet
    gcloud.cmd projects add-iam-policy-binding $ProjectID --member="serviceAccount:$DeployerEmail" --role="roles/cloudbuild.builds.editor" --quiet
    gcloud.cmd projects add-iam-policy-binding $ProjectID --member="serviceAccount:$DeployerEmail" --role="roles/storage.admin" --quiet
    gcloud.cmd projects add-iam-policy-binding $ProjectID --member="serviceAccount:$DeployerEmail" --role="roles/iam.serviceAccountUser" --quiet

    # Generate key for GitHub Actions secret
    $KeyPath = "$env:TEMP\github-deployer-key.json"
    gcloud.cmd iam service-accounts keys create $KeyPath --iam-account=$DeployerEmail --quiet

    if (Test-Path $KeyPath) {
        $keyContent = Get-Content $KeyPath -Raw
        # Set GitHub repository secrets
        gh secret set GCP_SA_KEY --body "$keyContent" 2>$null
        gh secret set CRON_SECRET --body "$CronSecret" 2>$null
        Remove-Item $KeyPath -Force -ErrorAction SilentlyContinue
        Write-Host "GitHub Secrets 'GCP_SA_KEY' and 'CRON_SECRET' configured! Any git push to 'main' will automatically build and deploy." -ForegroundColor Green
    }
} else {
    Write-Host "GitHub CLI not authenticated or no remote configured. You can manually configure the GCP_SA_KEY secret in GitHub." -ForegroundColor Yellow
}

Write-Host "`n==========================================================" -ForegroundColor Green
Write-Host "  DEPLOYMENT COMPLETE!                                    " -ForegroundColor Green
Write-Host "  App URL: $ServiceUrl                                    " -ForegroundColor Green
Write-Host "  Secure Scrape Endpoint: $JobUri                        " -ForegroundColor Green
Write-Host "  Scheduler Job: $JobName (Daily at 00:00 UTC)           " -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
