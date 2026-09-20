# Push code to GitHub repository
# Usage:
#   .\push_to_github.ps1
#   .\push_to_github.ps1 -Repo "https://github.com/<username>/price-tracker.git"

param (
    [string]$Repo = "",
    [string]$CommitMessage = "Initial commit: Price tracker with Flask, Firestore, and Cloud Run automation"
)

$ErrorActionPreference = "Stop"

# Ensure tools are on path
$env:Path = "C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI;" + $env:Path

Write-Host ">>> Checking Git status..." -ForegroundColor Cyan

# Ensure branch is main
git branch -M main 2>$null

# Check remote
$existingRemote = git remote get-url origin 2>$null

if (-not $existingRemote) {
    if ($Repo -ne "") {
        Write-Host ">>> Setting remote origin to: $Repo" -ForegroundColor Green
        git remote add origin $Repo
    } else {
        # Check if gh CLI is authenticated
        $ghAuth = gh auth status 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host ">>> GitHub CLI detected! Creating repository on GitHub..." -ForegroundColor Cyan
            gh repo create price-tracker --public --source=. --remote=origin --push
            Write-Host ">>> Repository created and pushed successfully via GitHub CLI!" -ForegroundColor Green
            exit 0
        } else {
            Write-Host ">>> No remote 'origin' configured." -ForegroundColor Yellow
            $Repo = Read-Host "Enter your GitHub repository URL (e.g. https://github.com/username/price-tracker.git)"
            if ($Repo) {
                git remote add origin $Repo
            } else {
                Write-Error "Repository URL is required to push to GitHub."
            }
        }
    }
}

Write-Host ">>> Staging files and committing..." -ForegroundColor Cyan
git add .
git commit -m "$CommitMessage" 2>$null

Write-Host ">>> Pushing to GitHub (branch: main)..." -ForegroundColor Cyan
git push -u origin main

Write-Host ">>> Pushed to GitHub successfully!" -ForegroundColor Green
