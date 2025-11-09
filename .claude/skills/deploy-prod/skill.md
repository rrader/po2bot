---
name: Deploy PO2 Bot to Production
description: Promote staging commit to production and deploy using SSH and Docker Compose.
---

# Deploy PO2 Bot to Production

Promote the current staging commit to production and deploy the PO2 Bot application.

## Server Configuration

### Staging Server
- **SSH Key**: `~/.ssh/personal`
- **SSH Host**: `root@rmn.pp.ua`
- **Project Directory**: `services/po2bot-staging`
- **Service Name**: `po2bot-staging`
- **Docker Compose File**: `/root/services/docker-compose.yml`

### Production Server
- **SSH Key**: `~/.ssh/personal`
- **SSH Host**: `root@rmn.pp.ua`
- **Project Directory**: `services/po2bot`
- **Service Name**: `po2bot`
- **Docker Compose File**: `/root/services/docker-compose.yml`

## Task

Execute the deployment process to promote staging to production:

### Step 1: Get Staging Commit Hash
Fetch the current git commit hash from the staging server:
```bash
ssh -i ~/.ssh/personal root@rmn.pp.ua "cd services/po2bot-staging && git rev-parse HEAD"
```

### Step 2: Fast-Forward Local Prod Branch
1. Ensure local repo is clean (warn if there are uncommitted changes)
2. Fetch latest changes
3. Fast-forward local `prod` branch to the staging commit:
   ```bash
   git fetch origin
   git checkout prod
   git merge --ff-only <STAGING_COMMIT_HASH>
   ```
   If fast-forward fails, stop and ask the user for guidance.

### Step 3: Push Prod Branch
Push the updated prod branch to remote (regular push, no force):
```bash
git push origin prod
```

### Step 4: Check and Sync Environment Variables
Before deployment, check if there are new environment variables in `.env.example` (from prod branch) that need to be added to production `.env`:

1. Fetch both files:
   - `.env.example` from local prod branch (just updated)
   - `.env` from the production server
2. Compare them to find any new variables in `.env.example` that are missing in `.env`
3. If new variables are found:
   - Add them to `.env` with the default values from `.env.example`
   - **Important**: Do NOT modify existing values in `.env` - they may be intentionally different
   - Ask the user if unsure about any changes
4. If changes were made, upload the updated `.env` back to the production server

### Step 5: Run Deployment Script
Once prod branch is pushed and .env is synced, run the deployment script:
```bash
./.claude/skills/deploy-prod/deploy.sh ~/.ssh/personal root@rmn.pp.ua services/po2bot po2bot
```

The script will:
1. SSH into the production server
2. Navigate to project directory
3. Checkout and pull prod branch
4. Rebuild the Docker image
5. Restart the service with docker-compose

### Step 6: Return to Main Branch
After deployment, switch back to main branch:
```bash
git checkout main
```
