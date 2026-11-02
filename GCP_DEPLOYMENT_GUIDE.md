# 🚀 Complete GCP Deployment Guide for Tectonic

This guide walks you through deploying Tectonic to Google Cloud Platform step by step.

---

## 📋 Table of Contents
1. [Prerequisites](#prerequisites)
2. [Phase 1: GCP Account Setup](#phase-1-gcp-account-setup)
3. [Phase 2: Install Required Tools](#phase-2-install-required-tools)
4. [Phase 3: Configure GCP Project](#phase-3-configure-gcp-project)
5. [Phase 4: Set Up Secrets](#phase-4-set-up-secrets)
6. [Phase 5: Deploy to Cloud Run](#phase-5-deploy-to-cloud-run)
7. [Phase 6: Verify Deployment](#phase-6-verify-deployment)
8. [Troubleshooting](#troubleshooting)

---

## Prerequisites

Before starting, ensure you have:
- A Google account (Gmail)
- A credit/debit card for GCP billing (you get $300 free credit)
- Your Gemini API key (from https://aistudio.google.com/app/apikey)

---

## Phase 1: GCP Account Setup

### Step 1.1: Create Google Cloud Account

1. Open browser → https://cloud.google.com
2. Click **"Get started for free"**
3. Sign in with Google account
4. Enter billing info (you get **$300 free credit** for 90 days)
5. Complete verification

### Step 1.2: Create a New Project

1. Go to: https://console.cloud.google.com
2. Click **project dropdown** (top of page)
3. Click **"New Project"**
4. Enter: `tectonic-app`
5. Click **"Create"**
6. Select the new project from dropdown

---

## Phase 2: Install Required Tools

### Step 2.1: Install Google Cloud CLI

1. Download from: https://cloud.google.com/sdk/docs/install
2. Run installer (GoogleCloudSDKInstaller.exe)
3. Complete installation wizard
4. Open **NEW PowerShell window**
5. Verify: `gcloud --version`

### Step 2.2: Initialize gcloud

```powershell
gcloud init
```

- Press Y to login (browser opens)
- Sign in and grant permissions
- Select your project: `tectonic-app`
- Choose region: `us-central1` (recommended)

---

## Phase 3: Configure GCP Project

### Step 3.1: Enable Required APIs

Run these commands in PowerShell:

```powershell
gcloud services enable cloudbuild.googleapis.com
gcloud services enable run.googleapis.com
gcloud services enable secretmanager.googleapis.com
gcloud services enable containerregistry.googleapis.com
```

### Step 3.2: Set Default Region

```powershell
gcloud config set run/region us-central1
```

---

## Phase 4: Set Up Secrets

### Step 4.1: Create Secret for API Key

1. Get your Gemini API key from: https://aistudio.google.com/app/apikey

2. Create the secret (replace YOUR_API_KEY):
```powershell
echo "YOUR_API_KEY" | gcloud secrets create google-api-key --data-file=-
```

### Step 4.2: Grant Cloud Run Access

```powershell
$PROJECT_NUMBER = (gcloud projects describe (gcloud config get-value project) --format="value(projectNumber)")

gcloud secrets add-iam-policy-binding google-api-key `
    --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" `
    --role="roles/secretmanager.secretAccessor"
```

---

## Phase 5: Deploy to Cloud Run

### Step 5.1: Navigate to Project

```powershell
cd "c:\Users\DELL\Downloads\tectonic\Tectonic"
```

### Step 5.2: Deploy Using Cloud Build (Recommended)

```powershell
gcloud builds submit --config=cloudbuild.yaml
```

**Wait time:** 5-15 minutes for first deployment

### Step 5.3: Get Your Application URL

After deployment, you'll see:
```
Service URL: https://tectonic-xxxxxxxxxx-uc.a.run.app
```

**This is your live application URL!**

---

## Phase 6: Verify Deployment

1. Open the Service URL in your browser
2. You should see the Tectonic frontend
3. Test API: `curl https://YOUR_URL/api/env`

### View Logs

```powershell
gcloud run services logs read tectonic --region us-central1 --limit 50
```

---

## Troubleshooting

### Permission denied
```powershell
gcloud auth login
gcloud config set project tectonic-app
```

### Build fails
```powershell
gcloud builds submit --config=cloudbuild.yaml --machine-type=e2-highcpu-8
```

### View logs
```powershell
gcloud run services logs read tectonic --region us-central1
```

---

## Quick Commands

```powershell
# View services
gcloud run services list

# Get URL
gcloud run services describe tectonic --region us-central1 --format="value(status.url)"

# Real-time logs
gcloud run services logs tail tectonic --region us-central1

# Delete service (stop charges)
gcloud run services delete tectonic --region us-central1
```

---

## Cost Management

Cloud Run free tier includes:
- 2 million requests/month
- 360,000 GB-seconds of memory

Set budget alerts at: https://console.cloud.google.com/billing/budgets
