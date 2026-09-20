# 🛒 PricePulse - E-Commerce Price Tracker

A lightweight, robust web application built with **Python & Flask** that tracks product prices across any e-commerce website (Amazon, Walmart, Target, eBay, Shopify stores, and generic retailers). 

Product information, current prices, and historical trends are persisted in **Google Cloud Firestore**, with an automated daily price refresh powered by **Google Cloud Scheduler** and deployed seamlessly to **Google Cloud Run**.

---

## 🌟 Features

- **Universal E-Commerce Scraper**: Extracts product name, price, currency, and product image using a multi-tiered hierarchy:
  1. Schema.org JSON-LD structured data (`application/ld+json`)
  2. OpenGraph & Twitter Card metadata (`og:title`, `og:price:amount`, etc.)
  3. Microdata (`itemprop="price"`, `itemprop="name"`)
  4. Platform-specific and generic CSS selectors
  5. Multi-currency regex fallback (`$`, `£`, `€`, `¥`, `₹`, etc.)
- **Persistent Storage with Google Cloud Firestore**:
  - Automatically provisions a Firestore Native database (`(default)` in `us-central1`).
  - Tracks individual product documents with latest price, lowest/highest price ever seen, and price change percentages.
  - Keeps historical price logs in a subcollection and inline recent history for fast rendering.
- **Modern Dashboard UI**:
  - Responsive design using Tailwind CSS and Lucide icons.
  - Interactive price trend timeline graphs powered by **Chart.js**.
  - Metric summary cards (Total products, Average price, Price drops, Price hikes).
  - One-click manual refresh and deletion of tracked items.
- **Automated & Secure Daily Scraper**:
  - Secure endpoint: `POST /api/scrape-all`.
  - Protected via `X-Cron-Secret`, Bearer token, or Google Cloud Scheduler OIDC Service Account authentication.
- **End-to-End Automated CI/CD**:
  - Push code to GitHub -> GitHub Actions builds container and deploys to Google Cloud Run automatically.

---

## 🏗️ Architecture

```
                       +-----------------------------------+
                       |      Google Cloud Scheduler       |
                       |    (Daily Trigger @ 00:00 UTC)    |
                       +-----------------+-----------------+
                                         |
                       POST /api/scrape-all (OIDC Auth)
                                         v
+-------------------+      +-------------------------------+
|    Web Browser    | ---> |    Google Cloud Run Service   |
|   (Dashboard UI)  |      |         (Flask App)           |
+-------------------+      +-------+---------------+-------+
                                   |               |
                         Queries / Updates   Scrapes Prices
                                   |               |
                                   v               v
                        +------------------+   +-------------------+
                        |   Google Cloud   |   |   E-Commerce      |
                        |    Firestore     |   |   Websites        |
                        +------------------+   +-------------------+
```

---

## 🚀 Quick Start (Local Development)

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Application
```bash
python app.py
```
Open your browser at `http://localhost:8080`.

> **Note:** If running locally without Google Cloud credentials, the application automatically falls back to an in-memory store so you can test all features and routes without setup!

---

## 🛠️ Exact Google Cloud Deployment Commands

### 1. Set Google Cloud Project
```bash
export PROJECT_ID="cep-demo-x"
export REGION="us-central1"
export CRON_SECRET="super-secret-price-tracker-cron-key-12345"

gcloud config set project $PROJECT_ID
```

### 2. Enable Required Google Cloud APIs
```bash
gcloud services enable \
    run.googleapis.com \
    firestore.googleapis.com \
    cloudscheduler.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    iam.googleapis.com
```

### 3. Provision Google Cloud Firestore Database (Native Mode)
```bash
gcloud firestore databases create \
    --location=$REGION \
    --type=firestore-native
```

### 4. Create Service Accounts and Grant Permissions
```bash
# Service account for Cloud Run runtime
gcloud iam service-accounts create price-tracker-runner \
    --display-name="Price Tracker Cloud Run Runner"

# Grant Firestore access to Cloud Run
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:price-tracker-runner@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/datastore.user"

# Service account for Cloud Scheduler invoker
gcloud iam service-accounts create price-tracker-scheduler \
    --display-name="Price Tracker Cloud Scheduler Invoker"
```

### 5. Build and Deploy to Google Cloud Run
```bash
gcloud run deploy price-tracker \
    --source . \
    --platform managed \
    --region $REGION \
    --allow-unauthenticated \
    --service-account "price-tracker-runner@$PROJECT_ID.iam.gserviceaccount.com" \
    --set-env-vars GCP_PROJECT=$PROJECT_ID,CRON_SECRET=$CRON_SECRET \
    --timeout 120s \
    --memory 512Mi \
    --cpu 1
```

### 6. Grant Cloud Scheduler Permission to Invoke Cloud Run
```bash
gcloud run services add-iam-policy-binding price-tracker \
    --platform managed \
    --region $REGION \
    --member="serviceAccount:price-tracker-scheduler@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/run.invoker"
```

### 7. Create Google Cloud Scheduler Job (Runs Once Daily at 00:00 UTC)
```bash
SERVICE_URL=$(gcloud run services describe price-tracker --platform managed --region $REGION --format="value(status.url)")

gcloud scheduler jobs create http price-tracker-daily-scrape \
    --location=$REGION \
    --schedule="0 0 * * *" \
    --uri="$SERVICE_URL/api/scrape-all" \
    --http-method=POST \
    --headers="X-Cron-Secret=$CRON_SECRET,Content-Type=application/json" \
    --oidc-service-account-email="price-tracker-scheduler@$PROJECT_ID.iam.gserviceaccount.com" \
    --oidc-token-audience="$SERVICE_URL"
```

---

## ⚡ 1-Click Automated Setup Scripts

You can also run the provided automation scripts which execute all of the above commands automatically:

### In PowerShell (Windows):
```powershell
.\deploy_gcp.ps1
```

### In Bash (Linux/macOS):
```bash
chmod +x deploy_gcp.sh
./deploy_gcp.sh
```

---

## 🔄 Automated CI/CD with GitHub Actions

Every commit pushed to the `main` branch will automatically trigger `.github/workflows/deploy.yml` to build the container and deploy the latest revision to Google Cloud Run.

### Setting Up GitHub Secrets:
1. **`GCP_SA_KEY`**: JSON key for a service account with Cloud Run Admin & Cloud Build Editor permissions.
2. **`CRON_SECRET`**: The secret token used to protect the batch scrape endpoint.

To generate and set these secrets automatically using the GitHub CLI:
```bash
# Create deployer service account
gcloud iam service-accounts create github-deployer --display-name="GitHub Actions Deployer"

# Grant deployment permissions
gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:github-deployer@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/run.admin"
gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:github-deployer@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/cloudbuild.builds.editor"
gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:github-deployer@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/storage.admin"
gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:github-deployer@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/iam.serviceAccountUser"

# Create key and set GitHub secret
gcloud iam service-accounts keys create key.json --iam-account="github-deployer@$PROJECT_ID.iam.gserviceaccount.com"
gh secret set GCP_SA_KEY < key.json
gh secret set CRON_SECRET --body "$CRON_SECRET"
rm key.json
```

---

## 📡 API Reference

### `POST /api/scrape-all`
Scrapes all tracked URLs in Firestore and logs updated price points.

**Authentication Headers (any of the following):**
- `X-Cron-Secret: <CRON_SECRET>`
- `Authorization: Bearer <CRON_SECRET>`
- Google Cloud OIDC Identity Token (passed by Cloud Scheduler)

**Sample Response:**
```json
{
  "status": "success",
  "timestamp": "2026-09-20T12:00:00.000000+00:00",
  "total": 5,
  "updated": 4,
  "failed": 1,
  "details": [
    {
      "id": "8496bf8dba16cd78cb4e",
      "name": "A Light in the Attic",
      "price": 51.77,
      "status": "success"
    }
  ]
}
```
