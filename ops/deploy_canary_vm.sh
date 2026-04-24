#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dexter_pro}"
BRANCH="${BRANCH:-deploy-xau-family-canary}"
SERVICE="${SERVICE:-dexter-monitor}"

cd "$APP_DIR"

echo "[1/5] fetch + checkout $BRANCH from dexter"
git fetch dexter "$BRANCH" --prune
git checkout "$BRANCH"
git reset --hard "dexter/$BRANCH"

echo "[2/5] install deps"
. .venv/bin/activate
pip install -r requirements.txt

echo "[3/5] compile touched production files"
python3 -m py_compile config.py execution/ctrader_executor.py scheduler.py scanners/fibo_advance.py analysis/fibonacci.py

echo "[4/5] restart service"
sudo systemctl restart "$SERVICE"

echo "[5/5] service status"
sudo systemctl status "$SERVICE" --no-pager
