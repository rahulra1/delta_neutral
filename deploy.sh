#!/bin/bash
set -e

CURRENT=$(grep -oP '127\.0\.0\.1:\K\d+' /etc/nginx/sites-available/algox | head -1)

if [ "$CURRENT" = "5000" ]; then
  DEPLOY_DIR="/root/delta_neutral-green"
  DEPLOY_PORT="5001"
  DEPLOY_SVC="algox-green"
  OLD_SVC="algox-blue"
  OLD_PORT="5000"
  echo "🟢 Deploying to GREEN (port 5001)..."
else
  DEPLOY_DIR="/root/delta_neutral-blue"
  DEPLOY_PORT="5000"
  DEPLOY_SVC="algox-blue"
  OLD_SVC="algox-green"
  OLD_PORT="5001"
  echo "🔵 Deploying to BLUE (port 5000)..."
fi

# Pull latest code into both directories
cd /root/delta_neutral && git pull
cd /root/delta_neutral-green && git pull

# Install deps for the deploy target
cd "$DEPLOY_DIR"
source venv/bin/activate
pip install -r requirements.txt -q
deactivate

# Start new instance with peer port pointing to old instance
systemctl stop "$DEPLOY_SVC" 2>/dev/null || true
# Set peer port in the service env
mkdir -p /etc/systemd/system/${DEPLOY_SVC}.service.d
cat > /etc/systemd/system/${DEPLOY_SVC}.service.d/peer.conf << EEOF
[Service]
Environment=ALGOX_PEER_PORT=$OLD_PORT
EEOF
systemctl daemon-reload
systemctl restart "$DEPLOY_SVC"
sleep 3

# Health check
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$DEPLOY_PORT")
if [ "$STATUS" != "200" ] && [ "$STATUS" != "302" ]; then
  echo "❌ Health check failed (HTTP $STATUS). Aborting."
  systemctl stop "$DEPLOY_SVC"
  exit 1
fi
echo "✅ Health check passed"

# Switch new traffic to new instance
sed -i "s/127.0.0.1:[0-9]*/127.0.0.1:$DEPLOY_PORT/" /etc/nginx/sites-available/algox
nginx -t && systemctl reload nginx
echo "✅ New traffic → port $DEPLOY_PORT"

# Check if old instance has running strategies
check_running() {
  # Generate token from the deploy dir (has latest code)
  TOKEN=$(cd "$DEPLOY_DIR" && source venv/bin/activate && python3 -c 'from app import _make_token; print(_make_token(1))' 2>/dev/null)
  if [ -z "$TOKEN" ]; then
    echo "0"
    return
  fi
  RESP=$(curl -s "http://127.0.0.1:$OLD_PORT/api/strategies" -H "Authorization: Bearer $TOKEN" 2>/dev/null)
  if [ -z "$RESP" ]; then
    echo "0"
    return
  fi
  echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for s in d.get('strategies',[]) if s.get('status') in ('running','open (no monitor)')))" 2>/dev/null || echo "0"
}

RUNNING=$(check_running)
if [ "$RUNNING" = "0" ] || [ "$RUNNING" = "" ]; then
  systemctl stop "$OLD_SVC" 2>/dev/null || true
  systemctl stop algox 2>/dev/null || true
  echo "✅ Old instance stopped (no running strategies)"
else
  echo "⏳ Old instance has $RUNNING running strategy(s) — keeping alive"
  echo "   It will keep running on port $OLD_PORT until strategies finish."
  echo "   Users can still access running strategies via the old instance."
  echo ""
  echo "   To check status:  curl http://127.0.0.1:$OLD_PORT/api/strategies -H 'Authorization: Bearer ...'"
  echo "   To force stop:    systemctl stop $OLD_SVC"
  echo ""
  # Start background watcher that stops old instance when strategies finish
  nohup bash -c "
    TOKEN=\$(cd $DEPLOY_DIR && source venv/bin/activate && python3 -c 'from app import _make_token; print(_make_token(1))' 2>/dev/null)
    while true; do
      sleep 30
      RESP=\$(curl -s 'http://127.0.0.1:$OLD_PORT/api/strategies' -H \"Authorization: Bearer \$TOKEN\" 2>/dev/null)
      R=\$(echo \"\$RESP\" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(sum(1 for s in d.get(\"strategies\",[]) if s.get(\"status\") in (\"running\",\"open (no monitor)\")))' 2>/dev/null || echo '0')
      if [ \"\$R\" = '0' ] || [ \"\$R\" = '' ]; then
        systemctl stop $OLD_SVC 2>/dev/null || true
        systemctl stop algox 2>/dev/null || true
        rm -f /etc/systemd/system/${DEPLOY_SVC}.service.d/peer.conf
        systemctl daemon-reload
        systemctl restart $DEPLOY_SVC
        echo '[deploy-watcher] Old instance ($OLD_SVC) stopped, peer cleared' >> /var/log/algox-deploy.log
        exit 0
      fi
    done
  " >> /var/log/algox-deploy.log 2>&1 &
  echo "   Background watcher started — will auto-stop old instance when strategies finish."
fi

echo "🎉 Deploy complete! Active: $DEPLOY_SVC (port $DEPLOY_PORT)"
