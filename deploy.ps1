# Tectonic GCP Deployment Script
# Run this after completing GCP account setup and gcloud init

param(
    [string]$ProjectId = "",
    [string]$Region = "us-central1",
    [string]$ServiceName = "tectonic"
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "   Tectonic GCP Deployment Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if gcloud is installed
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: gcloud CLI not found. Please install it first." -ForegroundColor Red
    Write-Host "Download from: https://cloud.google.com/sdk/docs/install" -ForegroundColor Yellow
    exit 1
}

# Get project ID
if ([string]::IsNullOrEmpty($ProjectId)) {
    $ProjectId = gcloud config get-value project 2>$null
    if ([string]::IsNullOrEmpty($ProjectId)) {
        Write-Host "ERROR: No project configured. Run 'gcloud init' first." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Project ID: $ProjectId" -ForegroundColor Green
Write-Host "Region: $Region" -ForegroundColor Green
Write-Host "Service: $ServiceName" -ForegroundColor Green
Write-Host ""

# Step 1: Enable APIs
Write-Host "[1/5] Enabling required APIs..." -ForegroundColor Yellow
$apis = @(
    "cloudbuild.googleapis.com",
    "run.googleapis.com", 
    "secretmanager.googleapis.com",
    "containerregistry.googleapis.com"
)

foreach ($api in $apis) {
    Write-Host "  Enabling $api..."
    gcloud services enable $api --quiet 2>$null
}
Write-Host "  Done!" -ForegroundColor Green
Write-Host ""

# Step 2: Check for secret
Write-Host "[2/5] Checking secrets..." -ForegroundColor Yellow
$secretExists = gcloud secrets describe google-api-key 2>$null
if (-not $secretExists) {
    Write-Host "  Secret 'google-api-key' not found." -ForegroundColor Yellow
    Write-Host ""
    $apiKey = Read-Host "  Enter your Gemini API Key"
    if ([string]::IsNullOrEmpty($apiKey)) {
        Write-Host "ERROR: API key is required." -ForegroundColor Red
        exit 1
    }
    Write-Host "  Creating secret..."
    $apiKey | gcloud secrets create google-api-key --data-file=- 2>$null
    Write-Host "  Secret created!" -ForegroundColor Green
} else {
    Write-Host "  Secret exists." -ForegroundColor Green
}
Write-Host ""

# Step 3: Grant permissions
Write-Host "[3/5] Setting up permissions..." -ForegroundColor Yellow
$projectNumber = (gcloud projects describe $ProjectId --format="value(projectNumber)")
$serviceAccount = "$projectNumber-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding google-api-key `
    --member="serviceAccount:$serviceAccount" `
    --role="roles/secretmanager.secretAccessor" `
    --quiet 2>$null

Write-Host "  Done!" -ForegroundColor Green
Write-Host ""

# Step 4: Build and deploy
Write-Host "[4/5] Building and deploying (this takes 5-15 minutes)..." -ForegroundColor Yellow
Write-Host "  Submitting build to Cloud Build..."
gcloud builds submit --config=cloudbuild.yaml --substitutions=_REGION=$Region

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Build failed. Check logs above." -ForegroundColor Red
    exit 1
}
Write-Host "  Done!" -ForegroundColor Green
Write-Host ""

# Step 5: Get URL
Write-Host "[5/5] Getting service URL..." -ForegroundColor Yellow
$serviceUrl = gcloud run services describe $ServiceName --region $Region --format="value(status.url)" 2>$null

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "   DEPLOYMENT SUCCESSFUL!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Your application is live at:" -ForegroundColor White
Write-Host $serviceUrl -ForegroundColor Cyan
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Yellow
Write-Host "  View logs:    gcloud run services logs tail $ServiceName --region $Region"
Write-Host "  Delete:       gcloud run services delete $ServiceName --region $Region"
Write-Host ""
