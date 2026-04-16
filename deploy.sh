#!/usr/bin/env bash
set -euo pipefail

EC2_HOST="ec2-user@63.178.175.200"
SSH_KEY="$HOME/.ssh/geo-redirect-checker.pem"
GIT_DIR="/opt/discord-bot"
RUN_DIR="/opt/geo-monitor"
SERVICE="discord-bot"

echo "==> Pulling latest code on EC2..."
ssh -i "$SSH_KEY" "$EC2_HOST" "cd $GIT_DIR && sudo git pull origin main"

echo "==> Copying deployable files to $RUN_DIR..."
ssh -i "$SSH_KEY" "$EC2_HOST" "sudo cp $GIT_DIR/discord_bot.py $GIT_DIR/monitor.py $GIT_DIR/config.yaml $GIT_DIR/livemode.py $GIT_DIR/testmode.py $RUN_DIR/ 2>/dev/null; sudo rm -rf $RUN_DIR/__pycache__"

echo "==> Restarting $SERVICE..."
ssh -i "$SSH_KEY" "$EC2_HOST" "sudo systemctl restart $SERVICE"

echo "==> Checking status..."
ssh -i "$SSH_KEY" "$EC2_HOST" "sudo systemctl status $SERVICE --no-pager; echo '---'; sudo journalctl -u $SERVICE -n 15 --no-pager"

echo "==> Done."
