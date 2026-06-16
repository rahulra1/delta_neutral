#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════
# Gamma (Staging) + Blue/Green (Prod) Deployment
#
# Flow:  Code → Gamma (test) → Promote to Prod (blue/green)
#
# Usage:
#   ./deploy-gamma.sh deploy    — Deploy latest code to gamma
#   ./deploy-gamma.sh status    — Check gamma health
#   ./deploy-gamma.sh promote   — Promote gamma to prod (blue/green swap)
#   ./deploy-gamma.sh rollback  — Rollback prod to previous color
# ═══════════════════════════════════════════════════════════════

REPO_DIR="/root/delta_neutral"
GAMMA_DIR="/root/delta_neutral-gamma"
BLUE_DIR="/root/delta_neutral-blue"
GREEN_DIR="/root/delta_neutral-green"
DATA_DIR="/root/delta_neutral-data"

GAMMA_PORT=5002
BLUE_PORT=5000
GREEN_PORT=5001

NGINX_CONF="/etc/nginx/sites-available/algox"
GAMMA_NGINX="/etc/nginx/sites-available/algox-gamma"

# ── Helpers ──

health_check() {
  local port=$1
  local status=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$port" 2>/dev/null)
  [ "$status" = "200" ] || [ "$status" = "302" ]
}

get_active_prod_port() {
  grep -oP '127\.0\.0\.1:\K\d+' "$NGINX_CONF" | head -1
}

# ── Init Gamma ──

init_gamma() {
  echo "🔧 Setting up gamma environment..."

  mkdir -p "$DATA_DIR"

  if [ ! -d "$GAMMA_DIR" ]; then
    cp -a "$REPO_DIR" "$GAMMA_DIR"
    cd "$GAMMA_DIR"
    python3 -m venv venv
    source venv/bin/activate && pip install -r requirements.txt -q && deactivate
  fi

  # Gamma systemd service
  cat > /etc/systemd/system/algox-gamma.service << EOF
[Unit]
Description=AlgoX Gamma (Staging)
After=network.target

[Service]
User=root
WorkingDirectory=${GAMMA_DIR}
EnvironmentFile=${GAMMA_DIR}/.env
Environment=ALGOX_DB_PATH=${DATA_DIR}/gamma_users.db
Environment=ALGOX_HISTORY_FILE=${DATA_DIR}/gamma_trade_history.json
Environment=ALGOX_ENV=gamma
ExecStart=${GAMMA_DIR}/venv/bin/gunicorn --bind 127.0.0.1:${GAMMA_PORT} --workers 1 --threads 4 --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  # Gamma nginx (separate subdomain or path)
  cat > "$GAMMA_NGINX" << EOF
server {
    listen 80;
    server_name gamma.algox.co.in;

    location / {
        proxy_pass http://127.0.0.1:${GAMMA_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }
}
EOF

  ln -sf "$GAMMA_NGINX" /etc/nginx/sites-enabled/algox-gamma 2>/dev/null || true
  systemctl daemon-reload
  nginx -t && systemctl reload nginx 2>/dev/null || true

  echo "✅ Gamma environment ready on port $GAMMA_PORT"
}

# ── Deploy to Gamma ──

deploy_gamma() {
  if [ ! -d "$GAMMA_DIR" ]; then
    init_gamma
  fi

  echo "🟡 Deploying to GAMMA (port $GAMMA_PORT)..."

  cd "$REPO_DIR" && git pull
  cd "$GAMMA_DIR" && git pull

  # Install deps + build frontend
  source venv/bin/activate
  pip install -r requirements.txt -q
  cd frontend && npm install --silent && npm run build 2>/dev/null
  cd ..
  deactivate

  # Restart gamma
  systemctl restart algox-gamma
  sleep 3

  if health_check $GAMMA_PORT; then
    echo "✅ Gamma deployed and healthy"
    echo ""
    echo "   Test at: http://127.0.0.1:$GAMMA_PORT"
    echo "   When stable, run: ./deploy-gamma.sh promote"
  else
    echo "❌ Gamma health check failed!"
    journalctl -u algox-gamma --no-pager -n 20
    exit 1
  fi
}

# ── Status ──

status() {
  echo "═══ Deployment Status ═══"
  echo ""

  # Gamma
  if systemctl is-active --quiet algox-gamma 2>/dev/null; then
    if health_check $GAMMA_PORT; then
      echo "🟡 Gamma (port $GAMMA_PORT): ✅ Healthy"
    else
      echo "🟡 Gamma (port $GAMMA_PORT): ❌ Unhealthy"
    fi
  else
    echo "🟡 Gamma: ⏹ Not running"
  fi

  # Prod
  ACTIVE_PORT=$(get_active_prod_port)
  if [ "$ACTIVE_PORT" = "$BLUE_PORT" ]; then
    ACTIVE_COLOR="BLUE"; STANDBY_COLOR="GREEN"; STANDBY_PORT=$GREEN_PORT
  else
    ACTIVE_COLOR="GREEN"; STANDBY_COLOR="BLUE"; STANDBY_PORT=$BLUE_PORT
  fi

  if health_check $ACTIVE_PORT; then
    echo "🟢 Prod [$ACTIVE_COLOR] (port $ACTIVE_PORT): ✅ Active — serving traffic"
  else
    echo "🔴 Prod [$ACTIVE_COLOR] (port $ACTIVE_PORT): ❌ DOWN"
  fi

  if systemctl is-active --quiet algox-$(echo $STANDBY_COLOR | tr A-Z a-z) 2>/dev/null; then
    echo "⚪ Standby [$STANDBY_COLOR] (port $STANDBY_PORT): Running (draining)"
  else
    echo "⚪ Standby [$STANDBY_COLOR] (port $STANDBY_PORT): Stopped"
  fi
  echo ""
}

# ── Promote Gamma to Prod (Blue/Green swap) ──

promote() {
  echo "🚀 Promoting gamma to production..."

  # Verify gamma is healthy
  if ! health_check $GAMMA_PORT; then
    echo "❌ Gamma is not healthy. Fix gamma first."
    exit 1
  fi

  # Determine target (inactive prod slot)
  ACTIVE_PORT=$(get_active_prod_port)
  if [ "$ACTIVE_PORT" = "$BLUE_PORT" ]; then
    DEPLOY_DIR="$GREEN_DIR"; DEPLOY_PORT=$GREEN_PORT; DEPLOY_SVC="algox-green"
    OLD_PORT=$BLUE_PORT; OLD_SVC="algox-blue"
    echo "   Promoting to GREEN (port $GREEN_PORT)..."
  else
    DEPLOY_DIR="$BLUE_DIR"; DEPLOY_PORT=$BLUE_PORT; DEPLOY_SVC="algox-blue"
    OLD_PORT=$GREEN_PORT; OLD_SVC="algox-green"
    echo "   Promoting to BLUE (port $BLUE_PORT)..."
  fi

  # Sync code from gamma to target prod slot
  rsync -a --exclude='venv' --exclude='node_modules' --exclude='.git' \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' \
        "$GAMMA_DIR/" "$DEPLOY_DIR/"

  # Install deps
  cd "$DEPLOY_DIR"
  source venv/bin/activate
  pip install -r requirements.txt -q
  deactivate

  # Start new prod instance
  systemctl stop "$DEPLOY_SVC" 2>/dev/null || true
  mkdir -p /etc/systemd/system/${DEPLOY_SVC}.service.d
  cat > /etc/systemd/system/${DEPLOY_SVC}.service.d/peer.conf << EEOF
[Service]
Environment=ALGOX_PEER_PORT=$OLD_PORT
EEOF
  systemctl daemon-reload
  systemctl restart "$DEPLOY_SVC"
  sleep 3

  # Health check
  if ! health_check $DEPLOY_PORT; then
    echo "❌ Prod health check failed! Rolling back."
    systemctl stop "$DEPLOY_SVC"
    exit 1
  fi
  echo "✅ New prod instance healthy"

  # Switch traffic
  sed -i "s/127.0.0.1:[0-9]*/127.0.0.1:$DEPLOY_PORT/" "$NGINX_CONF"
  nginx -t && systemctl reload nginx
  echo "✅ Traffic switched to port $DEPLOY_PORT"

  # Drain old instance
  sleep 5
  systemctl stop "$OLD_SVC" 2>/dev/null || true
  rm -f /etc/systemd/system/${DEPLOY_SVC}.service.d/peer.conf
  systemctl daemon-reload
  systemctl restart "$DEPLOY_SVC"

  echo ""
  echo "🎉 Promote complete!"
  echo "   Prod: $DEPLOY_SVC (port $DEPLOY_PORT)"
  echo "   Gamma still running for next iteration"
}

# ── Rollback ──

rollback() {
  ACTIVE_PORT=$(get_active_prod_port)
  if [ "$ACTIVE_PORT" = "$BLUE_PORT" ]; then
    TARGET_PORT=$GREEN_PORT; TARGET_SVC="algox-green"
  else
    TARGET_PORT=$BLUE_PORT; TARGET_SVC="algox-blue"
  fi

  echo "⏪ Rolling back to $TARGET_SVC (port $TARGET_PORT)..."

  systemctl restart "$TARGET_SVC"
  sleep 3

  if ! health_check $TARGET_PORT; then
    echo "❌ Rollback target not healthy!"
    exit 1
  fi

  sed -i "s/127.0.0.1:[0-9]*/127.0.0.1:$TARGET_PORT/" "$NGINX_CONF"
  nginx -t && systemctl reload nginx

  echo "✅ Rolled back. Traffic → port $TARGET_PORT"
}

# ── Main ──

case "${1:-deploy}" in
  deploy)  deploy_gamma ;;
  status)  status ;;
  promote) promote ;;
  rollback) rollback ;;
  init)    init_gamma ;;
  *)       echo "Usage: $0 {deploy|status|promote|rollback}"; exit 1 ;;
esac
