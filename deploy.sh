#!/bin/bash
set -e

REPO_DIR="/root/delta_neutral"
BLUE_DIR="/root/delta_neutral-blue"
GREEN_DIR="/root/delta_neutral-green"

# --- First-time setup ---
init_bluegreen() {
  echo "🔧 First-time blue-green setup..."

  # Create blue and green directories
  if [ ! -d "$BLUE_DIR" ]; then
    cp -a "$REPO_DIR" "$BLUE_DIR"
    cd "$BLUE_DIR"
    python3 -m venv venv
    source venv/bin/activate && pip install -r requirements.txt -q && deactivate
  fi
  if [ ! -d "$GREEN_DIR" ]; then
    cp -a "$REPO_DIR" "$GREEN_DIR"
    cd "$GREEN_DIR"
    python3 -m venv venv
    source venv/bin/activate && pip install -r requirements.txt -q && deactivate
  fi

  # Create service files
  for COLOR in blue green; do
    PORT=5000; [ "$COLOR" = "green" ] && PORT=5001
    DIR="/root/delta_neutral-${COLOR}"
    cat > /etc/systemd/system/algox-${COLOR}.service << EOF
[Unit]
Description=AlgoX ${COLOR^}
After=network.target

[Service]
User=root
WorkingDirectory=${DIR}
EnvironmentFile=${DIR}/.env
ExecStart=${DIR}/venv/bin/gunicorn --bind 127.0.0.1:${PORT} --workers 1 --threads 4 --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  done

  systemctl daemon-reload

  # Point nginx to blue and start blue as the first active instance
  sed -i "s/127.0.0.1:[0-9]*/127.0.0.1:5000/" /etc/nginx/sites-available/algox
  nginx -t && systemctl reload nginx
  systemctl restart algox-blue
  sleep 2

  STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000)
  if [ "$STATUS" != "200" ] && [ "$STATUS" != "302" ]; then
    echo "❌ Blue health check failed (HTTP $STATUS)"
    exit 1
  fi

  # Stop old single-instance service
  systemctl stop algox 2>/dev/null || true
  systemctl disable algox 2>/dev/null || true

  echo "✅ First-time setup complete — Blue active on port 5000"
  echo "   Run deploy.sh again to deploy to Green."
  exit 0
}

# Check if blue-green is set up
if [ ! -d "$BLUE_DIR" ] || [ ! -d "$GREEN_DIR" ] || \
   [ ! -f /etc/systemd/system/algox-blue.service ] || \
   [ ! -f /etc/systemd/system/algox-green.service ]; then
  init_bluegreen
fi

# --- Determine which side to deploy to ---
CURRENT=$(grep -oP '127\.0\.0\.1:\K\d+' /etc/nginx/sites-available/algox | head -1)

if [ "$CURRENT" = "5000" ]; then
  DEPLOY_DIR="$GREEN_DIR"; DEPLOY_PORT="5001"; DEPLOY_SVC="algox-green"
  OLD_DIR="$BLUE_DIR"; OLD_PORT="5000"; OLD_SVC="algox-blue"
  echo "🟢 Deploying to GREEN (port 5001)..."
else
  DEPLOY_DIR="$BLUE_DIR"; DEPLOY_PORT="5000"; DEPLOY_SVC="algox-blue"
  OLD_DIR="$GREEN_DIR"; OLD_PORT="5001"; OLD_SVC="algox-green"
  echo "🔵 Deploying to BLUE (port 5000)..."
fi

# --- Pull latest code ---
cd "$REPO_DIR" && git pull
cd "$DEPLOY_DIR" && git pull

# --- Install deps ---
cd "$DEPLOY_DIR"
source venv/bin/activate
pip install -r requirements.txt -q
deactivate

# --- Start new instance with peer pointing to old ---
systemctl stop "$DEPLOY_SVC" 2>/dev/null || true
mkdir -p /etc/systemd/system/${DEPLOY_SVC}.service.d
cat > /etc/systemd/system/${DEPLOY_SVC}.service.d/peer.conf << EEOF
[Service]
Environment=ALGOX_PEER_PORT=$OLD_PORT
EEOF
systemctl daemon-reload
systemctl restart "$DEPLOY_SVC"
sleep 3

# --- Health check ---
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$DEPLOY_PORT")
if [ "$STATUS" != "200" ] && [ "$STATUS" != "302" ]; then
  echo "❌ Health check failed (HTTP $STATUS). Aborting."
  systemctl stop "$DEPLOY_SVC"
  exit 1
fi
echo "✅ Health check passed"

# --- Switch nginx ---
sed -i "s/127.0.0.1:[0-9]*/127.0.0.1:$DEPLOY_PORT/" /etc/nginx/sites-available/algox
nginx -t && systemctl reload nginx
echo "✅ New traffic → port $DEPLOY_PORT"

# --- Check old instance for running strategies ---
check_running() {
  TOKEN=$(cd "$DEPLOY_DIR" && source venv/bin/activate && python3 -c 'from app import _make_token; print(_make_token(1))' 2>/dev/null)
  [ -z "$TOKEN" ] && echo "0" && return
  RESP=$(curl -s "http://127.0.0.1:$OLD_PORT/api/strategies" -H "Authorization: Bearer $TOKEN" 2>/dev/null)
  [ -z "$RESP" ] && echo "0" && return
  echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for s in d.get('strategies',[]) if s.get('status') in ('running','open (no monitor)')))" 2>/dev/null || echo "0"
}

RUNNING=$(check_running)
if [ "$RUNNING" = "0" ] || [ "$RUNNING" = "" ]; then
  systemctl stop "$OLD_SVC" 2>/dev/null || true
  rm -f /etc/systemd/system/${DEPLOY_SVC}.service.d/peer.conf
  systemctl daemon-reload
  echo "✅ Old instance stopped (no running strategies)"
else
  echo "⏳ Old instance has $RUNNING running strategy(s) — keeping alive"
  echo "   To force stop: systemctl stop $OLD_SVC"

  # Background watcher
  nohup bash -c "
    TOKEN=\$(cd $DEPLOY_DIR && source venv/bin/activate && python3 -c 'from app import _make_token; print(_make_token(1))' 2>/dev/null)
    while true; do
      sleep 30
      RESP=\$(curl -s 'http://127.0.0.1:$OLD_PORT/api/strategies' -H \"Authorization: Bearer \$TOKEN\" 2>/dev/null)
      R=\$(echo \"\$RESP\" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(sum(1 for s in d.get(\"strategies\",[]) if s.get(\"status\") in (\"running\",\"open (no monitor)\")))' 2>/dev/null || echo '0')
      if [ \"\$R\" = '0' ] || [ \"\$R\" = '' ]; then
        systemctl stop $OLD_SVC 2>/dev/null || true
        rm -f /etc/systemd/system/${DEPLOY_SVC}.service.d/peer.conf
        systemctl daemon-reload
        systemctl restart $DEPLOY_SVC
        echo \"[deploy-watcher] Old instance ($OLD_SVC) stopped, peer cleared\" >> /var/log/algox-deploy.log
        exit 0
      fi
    done
  " >> /var/log/algox-deploy.log 2>&1 &
  echo "   Background watcher started — will auto-stop old instance when strategies finish."
fi

echo "🎉 Deploy complete! Active: $DEPLOY_SVC (port $DEPLOY_PORT)"
