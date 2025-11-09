#!/bin/bash

# Deploy PO2 Bot to Production
# This script deploys the PO2 Bot application to the production server
# Usage: ./deploy.sh [SSH_KEY] [SSH_HOST] [PROJECT_DIR] [SERVICE_NAME]

set -e  # Exit on error

# Configuration - can be overridden by command line arguments
SSH_KEY="${1:-~/.ssh/personal}"
SSH_HOST="${2:-root@rmn.pp.ua}"
PROJECT_DIR="${3:-services/po2bot}"
SERVICE_NAME="${4:-po2bot}"

echo "🚀 Starting deployment to PRODUCTION..."
echo "Server: $SSH_HOST"
echo "Project: $PROJECT_DIR"
echo "Service: $SERVICE_NAME"
echo ""

# Deploy command - checkout prod branch and pull
echo "📦 Checking out prod branch, pulling latest code, building image, and restarting service..."
ssh -i "$SSH_KEY" "$SSH_HOST" "cd $PROJECT_DIR && git checkout prod && GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' git pull && cd /root/services && docker-compose build $SERVICE_NAME && docker-compose up -d $SERVICE_NAME"

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Deployment completed successfully!"
    echo ""
    echo "📊 Checking service status..."
    ssh -i "$SSH_KEY" "$SSH_HOST" "docker-compose ps $SERVICE_NAME"

    echo ""
    echo "📝 Recent logs:"
    ssh -i "$SSH_KEY" "$SSH_HOST" "docker-compose logs --tail=20 $SERVICE_NAME"

    echo ""
    echo "💡 To view live logs, run:"
    echo "   ssh -i $SSH_KEY $SSH_HOST \"docker-compose logs -f $SERVICE_NAME\""
else
    echo ""
    echo "❌ Deployment failed!"
    exit 1
fi
