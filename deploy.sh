#!/bin/bash
# -------------------------------------------------------
# deploy.sh — push local changes to the EC2 bot
# -------------------------------------------------------
# Runs: git push → SSH into EC2 → git pull → restart bot service.
# Assumes GitHub repo origin is set and SSH key is at ~/.ssh/geo-redirect-checker.pem
# -------------------------------------------------------
set -euo pipefail

EC2_HOST="ec2-user@63.178.175.200"
SSH_KEY="$HOME/.ssh/geo-redirect-checker.pem"
REMOTE_DIR="/opt/discord-bot"

echo "=== Pushing local changes to GitHub ==="
git push

echo "=== Pulling on EC2 ==="
ssh -i "$SSH_KEY" "$EC2_HOST" \
    "cd $REMOTE_DIR && sudo git pull && sudo systemctl restart discord-bot && sudo systemctl status discord-bot --no-pager | head -10"

echo "=== Done ==="
