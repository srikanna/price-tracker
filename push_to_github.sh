#!/usr/bin/env bash
set -e

# Usage:
#   ./push_to_github.sh
#   ./push_to_github.sh https://github.com/<username>/price-tracker.git

REPO_URL=$1
COMMIT_MSG=${2:-"Initial commit: Price tracker with Flask, Firestore, and Cloud Run automation"}

echo ">>> Checking Git status..."
git branch -M main || true

EXISTING_REMOTE=$(git remote get-url origin 2>/dev/null || true)

if [ -z "$EXISTING_REMOTE" ]; then
    if [ -n "$REPO_URL" ]; then
        echo ">>> Setting remote origin to: $REPO_URL"
        git remote add origin "$REPO_URL"
    elif command -v gh &> /dev/null && gh auth status &> /dev/null; then
        echo ">>> GitHub CLI detected! Creating repository on GitHub..."
        gh repo create price-tracker --public --source=. --remote=origin --push
        echo ">>> Repository created and pushed successfully via GitHub CLI!"
        exit 0
    else
        read -rp "Enter your GitHub repository URL (e.g. https://github.com/username/price-tracker.git): " REPO_URL
        if [ -n "$REPO_URL" ]; then
            git remote add origin "$REPO_URL"
        else
            echo "Error: Repository URL is required to push to GitHub."
            exit 1
        fi
    fi
fi

echo ">>> Staging files and committing..."
git add .
git commit -m "$COMMIT_MSG" || true

echo ">>> Pushing to GitHub (branch: main)..."
git push -u origin main

echo ">>> Pushed to GitHub successfully!"
