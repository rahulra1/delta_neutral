#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════
# Beta (Pre-Staging) Deployment
#
# Flow:  Code → Beta (test) → Promote to Gamma → Prod (blue/green)
#
# Usage:
#   ./deploy-beta.sh deploy    — Deploy latest code to beta
#   ./deploy-beta.sh status    — Check beta health
#   ./deploy-beta.sh promote   — Promote beta to gamma
#   ./deploy-beta.sh rollback  — Rollback beta to previous version
# ═══════════════════════════════════════════════════════════════

REPO_DIR="/root/delta_neutral"
BETA_DIR="/root/delta_neutral-beta"
GAMMA_DIR="/root/delta_neutral-gamma"
DATA_DIR="/root/delta_neutral-data"

BETA_PORT=5003
GAMMA_PORT=5002

BETA_NGINX="/etc/nginx/sites-available/algox-beta"

# ── Helpers ──

health_check() {
  local port=$1
  local status=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$port" 2>/dev/null)
  [ "$status" = "200" ] || [ "$status" = "302" ]
}

# ── Init Beta ──

init_beta() {
  echo "🔧 Setting up beta environment..."

  mkdir -p "$DATA_DIR"

  if [ ! -d "$BETA_DIR" ]; then
    cp -a "$REPO_DIR" "$BETA_DIR"
    cd "$BETA_DIR"
    python3 -m venv venv
    source venv/bin/activate && pip install -r requirements.txt -q && deactivate
  fi

  # Beta systemd service
  cat > /etc/systemd/system/algox-beta.service << EOF
[Unit]
Description=AlgoX Beta (Pre-Staging)
After=network.target

[Service]
User=root
WorkingDirectory=${BETA_DIR}
EnvironmentFile=${BETA_DIR}/.env
Environment=ALGOX_DB_PATH=${DATA_DIR}/beta_users.db
Environment=ALGOX_HISTORY_FILE=${DATA_DIR}/beta_trade_history.json
Environment=ALGOX_ENV=beta
ExecStart=${BETA_DIR}/venv/bin/gunicorn --bind 127.0.0.1:${BETA_PORT} --workers 1 --threads 4 --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  # Beta nginx (separate subdomain)
  if [ -f /etc/letsencrypt/live/beta.algox.co.in/fullchain.pem ]; then
    cat > "$BETA_NGINX" << EOF
server {
    listen 80;
    server_name beta.algox.co.in;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name beta.algox.co.in;

    ssl_certificate /etc/letsencrypt/live/beta.algox.co.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/beta.algox.co.in/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        proxy_pass http://127.0.0.1:${BETA_PORT};
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
  else
    cat > "$BETA_NGINX" << EOF
server {
    listen 80;
    server_name beta.algox.co.in;

    location / {
        proxy_pass http://127.0.0.1:${BETA_PORT};
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
  fi

  ln -sf "$BETA_NGINX" /etc/nginx/sites-enabled/algox-beta 2>/dev/null || true
  systemctl daemon-reload
  nginx -t && systemctl reload nginx 2>/dev/null || true

  echo "✅ Beta environment ready on port $BETA_PORT"
  echo "   Database: ${DATA_DIR}/beta_users.db"
  echo "   History:  ${DATA_DIR}/beta_trade_history.json"
}

# ── Deploy to Beta ──

deploy_beta() {
  if [ ! -d "$BETA_DIR" ]; then
    init_beta
  fi

  echo "🔵 Deploying to BETA (port $BETA_PORT)..."

  cd "$REPO_DIR" && git pull
  cd "$BETA_DIR" && git pull

  # Always rewrite service file to pick up env changes
  cat > /etc/systemd/system/algox-beta.service << EOF
[Unit]
Description=AlgoX Beta (Pre-Staging)
After=network.target

[Service]
User=root
WorkingDirectory=${BETA_DIR}
EnvironmentFile=${BETA_DIR}/.env
Environment=ALGOX_DB_PATH=${DATA_DIR}/beta_users.db
Environment=ALGOX_HISTORY_FILE=${DATA_DIR}/beta_trade_history.json
Environment=ALGOX_ENV=beta
ExecStart=${BETA_DIR}/venv/bin/gunicorn --bind 127.0.0.1:${BETA_PORT} --workers 1 --threads 4 --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload

  # Install deps + build frontend
  source venv/bin/activate
  pip install -r requirements.txt -q
  cd frontend && npm install --silent && npm run build 2>/dev/null
  cd ..
  deactivate

  # Restart beta
  systemctl restart algox-beta
  sleep 3

  # Ensure nginx config is current and enabled
  # Check if SSL cert exists for beta
  if [ -f /etc/letsencrypt/live/beta.algox.co.in/fullchain.pem ]; then
    cat > "$BETA_NGINX" << EOF
server {
    listen 80;
    server_name beta.algox.co.in;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name beta.algox.co.in;

    ssl_certificate /etc/letsencrypt/live/beta.algox.co.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/beta.algox.co.in/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        proxy_pass http://127.0.0.1:${BETA_PORT};
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
  else
    cat > "$BETA_NGINX" << EOF
server {
    listen 80;
    server_name beta.algox.co.in;

    location / {
        proxy_pass http://127.0.0.1:${BETA_PORT};
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
  fi
  ln -sf "$BETA_NGINX" /etc/nginx/sites-enabled/algox-beta 2>/dev/null || true
  nginx -t && systemctl reload nginx

  if health_check $BETA_PORT; then
    echo "✅ Beta deployed and healthy"
    echo ""
    echo "   Test at: http://127.0.0.1:$BETA_PORT"
    echo "   Domain:  http://beta.algox.co.in"
    echo "   When stable, run: ./deploy-beta.sh promote"
  else
    echo "❌ Beta health check failed!"
    journalctl -u algox-beta --no-pager -n 20
    exit 1
  fi
}

# ── Status ──

status() {
  echo "═══ Beta Status ═══"
  echo ""

  # Beta
  if systemctl is-active --quiet algox-beta 2>/dev/null; then
    if health_check $BETA_PORT; then
      echo "🔵 Beta (port $BETA_PORT): ✅ Healthy"
    else
      echo "🔵 Beta (port $BETA_PORT): ❌ Unhealthy"
    fi
  else
    echo "🔵 Beta: ⏹ Not running"
  fi

  # Gamma (downstream)
  if systemctl is-active --quiet algox-gamma 2>/dev/null; then
    if health_check $GAMMA_PORT; then
      echo "🟡 Gamma (port $GAMMA_PORT): ✅ Healthy"
    else
      echo "🟡 Gamma (port $GAMMA_PORT): ❌ Unhealthy"
    fi
  else
    echo "🟡 Gamma: ⏹ Not running"
  fi

  echo ""
  echo "Pipeline: Code → Beta (5003) → Gamma (5002) → Prod (blue/green)"
}

# ── Promote Beta to Gamma ──

promote() {
  echo "🚀 Promoting beta to gamma..."

  # Verify beta is healthy
  if ! health_check $BETA_PORT; then
    echo "❌ Beta is not healthy. Fix beta first."
    exit 1
  fi

  # Ensure gamma dir exists
  if [ ! -d "$GAMMA_DIR" ]; then
    echo "❌ Gamma environment not set up. Run ./deploy-gamma.sh init first."
    exit 1
  fi

  # Sync code from beta to gamma (preserving gamma's venv, .env, .git, node_modules)
  rsync -a --exclude='venv' --exclude='node_modules' --exclude='.git' \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' \
        "$BETA_DIR/" "$GAMMA_DIR/"

  # Install deps in gamma
  cd "$GAMMA_DIR"
  source venv/bin/activate
  pip install -r requirements.txt -q
  if [ -d frontend ]; then
    cd frontend && npm install --silent && npm run build 2>/dev/null
    cd ..
  fi
  deactivate

  # Rewrite gamma service to ensure correct env
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
  systemctl daemon-reload

  # Restart gamma
  systemctl restart algox-gamma
  sleep 3

  if health_check $GAMMA_PORT; then
    echo "✅ Gamma deployed from beta and healthy"
    echo ""
    echo "   Test at: http://127.0.0.1:$GAMMA_PORT"
    echo "   Domain:  http://gamma.algox.co.in"
    echo "   When stable, run: ./deploy-gamma.sh promote  (to push to prod)"
  else
    echo "❌ Gamma health check failed after promote!"
    journalctl -u algox-gamma --no-pager -n 20
    exit 1
  fi
}

# ── Rollback Beta ──

rollback() {
  echo "⏪ Rolling back beta to previous version..."

  if [ ! -d "$BETA_DIR" ]; then
    echo "❌ Beta directory does not exist. Nothing to rollback."
    exit 1
  fi

  cd "$BETA_DIR"

  # Get current and previous commit
  CURRENT=$(git rev-parse --short HEAD)
  PREV=$(git rev-parse --short HEAD~1 2>/dev/null)

  if [ -z "$PREV" ]; then
    echo "❌ No previous commit to rollback to."
    exit 1
  fi

  echo "   Current: $CURRENT"
  echo "   Rolling back to: $PREV"

  git checkout HEAD~1 -- .
  git checkout HEAD -- .env 2>/dev/null || true

  # Reinstall deps
  source venv/bin/activate
  pip install -r requirements.txt -q
  if [ -d frontend ]; then
    cd frontend && npm install --silent && npm run build 2>/dev/null
    cd ..
  fi
  deactivate

  # Restart
  systemctl restart algox-beta
  sleep 3

  if health_check $BETA_PORT; then
    echo "✅ Beta rolled back and healthy"
  else
    echo "❌ Beta unhealthy after rollback!"
    journalctl -u algox-beta --no-pager -n 20
    exit 1
  fi
}

# ── Main ──

case "${1:-deploy}" in
  deploy)   deploy_beta ;;
  status)   status ;;
  promote)  promote ;;
  rollback) rollback ;;
  init)     init_beta ;;
  *)        echo "Usage: $0 {deploy|status|promote|rollback}"; exit 1 ;;
esac
