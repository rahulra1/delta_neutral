#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════
# Telegram Alert Bots — GAMMA (staging) ONLY
#   event calendar + paper trades, crypto breaking news, Upbit listings
#
# Normally you don't call this directly: ./deploy-gamma.sh deploy runs it
# as its last step (single command installs on first run, updates later).
#
# Usage:
#   ./deploy-alerts.sh deploy      — git pull + install/update + restart bots in gamma
#   ./deploy-alerts.sh status      — are the gamma bots running?
#   ./deploy-alerts.sh logs        — last log lines of each bot
#   ./deploy-alerts.sh test        — send a test Telegram message
#   ./deploy-alerts.sh stop        — stop the bots (keeps install)
#   ./deploy-alerts.sh uninstall   — stop + remove the bot services
#
# Telegram: bot token + chat id are built in (api/notifier.py) — no setup.
# To use a different bot/chat, put TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
# in $GAMMA_DIR/.env (overrides the defaults).
#
# PAPER / ALERT ONLY — never places orders; does not touch blue/green prod,
# the gamma web app (algox-gamma), users.db or trade history.
# ═══════════════════════════════════════════════════════════════

REPO_DIR="${REPO_DIR:-/root/delta_neutral}"
GAMMA_DIR="${GAMMA_DIR:-/root/delta_neutral-gamma}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SERVICES="algox-gamma-event-strategy algox-gamma-catalyst-watch algox-gamma-listing-monitor"

svc_cmd() {
  case "$1" in
    algox-gamma-event-strategy)  echo "run_event_strategy.py --notify" ;;
    algox-gamma-catalyst-watch)  echo "run_catalyst_watch.py --notify --digest-hours 100000" ;;
    algox-gamma-listing-monitor) echo "run_listing_monitor.py --notify" ;;
  esac
}
svc_desc() {
  case "$1" in
    algox-gamma-event-strategy)  echo "AlgoX Gamma Alerts - coin event calendar + paper trades" ;;
    algox-gamma-catalyst-watch)  echo "AlgoX Gamma Alerts - crypto breaking news" ;;
    algox-gamma-listing-monitor) echo "AlgoX Gamma Alerts - Upbit listings" ;;
  esac
}

show_status() {
  for S in $SERVICES; do
    if systemctl is-active --quiet "$S" 2>/dev/null; then
      echo "🟡 $S: ✅ Running"
    else
      echo "🟡 $S: ⏹ Not running"
    fi
  done
}

# ─── sub-commands ──────────────────────────────────────────────
ACTION="${1:-deploy}"
NO_PULL=0; [ "${2:-}" = "--no-pull" ] && NO_PULL=1

case "$ACTION" in
  status)    show_status; exit 0 ;;
  logs)      for S in $SERVICES; do echo "── $S"; journalctl -u "$S" -n 8 --no-pager -o cat; done; exit 0 ;;
  test)      cd "$GAMMA_DIR" && venv/bin/python run_catalyst_watch.py --notify-test; exit 0 ;;
  stop)      systemctl stop $SERVICES; show_status; exit 0 ;;
  uninstall)
    for S in $SERVICES; do
      systemctl disable --now "$S" 2>/dev/null || true
      rm -f "$SYSTEMD_DIR/$S.service"
    done
    systemctl daemon-reload
    echo "✅ Gamma alert services removed (code + state kept in $GAMMA_DIR)"
    exit 0 ;;
  deploy) ;;
  *) echo "Usage: $0 {deploy|status|logs|test|stop|uninstall}"; exit 1 ;;
esac

echo "🟡 Deploying alert bots to GAMMA..."

# ─── 1. Gamma checkout must exist (created by ./deploy-gamma.sh) ──
if [ ! -d "$GAMMA_DIR" ] || [ ! -x "$GAMMA_DIR/venv/bin/python" ]; then
  echo "❌ $GAMMA_DIR (with venv) not found — run ./deploy-gamma.sh deploy first"
  exit 1
fi
if [ "$NO_PULL" = "0" ]; then
  cd "$REPO_DIR" && git pull
  cd "$GAMMA_DIR" && git pull
fi
cd "$GAMMA_DIR"

for f in run_event_strategy.py run_catalyst_watch.py run_listing_monitor.py api/notifier.py api/coinmarketcal.py; do
  [ -f "$f" ] || { echo "❌ $f missing in gamma — were the alert bot files committed and pushed?"; exit 1; }
done

# ─── 2. Packages (gamma venv already has requirements.txt; make sure of the 2 the bots need) ──
PKGS=$(grep -iE '^(requests|python-dotenv)==' requirements.txt | tr '\n' ' ')
[ -n "$PKGS" ] || PKGS="requests==2.32.3 python-dotenv==1.1.0"
venv/bin/pip install -q $PKGS

# ─── 3. Telegram credentials ───────────────────────────────────
# Built-in defaults in api/notifier.py (token + chat id) — nothing to set up.
# Optional override: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in $GAMMA_DIR/.env
if grep -qE '^TELEGRAM_BOT_TOKEN=.' .env 2>/dev/null; then
  echo "✅ Telegram: using TELEGRAM_BOT_TOKEN from $GAMMA_DIR/.env (overrides built-in default)"
else
  echo "✅ Telegram: using built-in bot token + chat id (api/notifier.py)"
fi

# ─── 4. Smoke test before touching running bots ────────────────
venv/bin/python -c "import run_event_strategy, run_catalyst_watch, run_listing_monitor
from api import notifier
assert notifier.telegram_configured(), 'TELEGRAM_* missing in .env'" \
  || { echo "❌ Smoke test failed — running bots left untouched"; exit 1; }
echo "✅ Code + Telegram config OK"

# ─── 5. systemd services (always rewritten to pick up changes) ─
FIRST_RUN=0; [ -f "$SYSTEMD_DIR/algox-gamma-event-strategy.service" ] || FIRST_RUN=1
for S in $SERVICES; do
  cat > "$SYSTEMD_DIR/$S.service" << EOF
[Unit]
Description=$(svc_desc $S)
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=${GAMMA_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=ALGOX_ENV=gamma
ExecStart=${GAMMA_DIR}/venv/bin/python -u ${GAMMA_DIR}/$(svc_cmd $S)
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
done
systemctl daemon-reload
for S in $SERVICES; do systemctl enable --quiet "$S"; systemctl restart "$S"; done

# ─── 6. Health check ───────────────────────────────────────────
sleep 15
FAILED=0
for S in $SERVICES; do
  if systemctl is-active --quiet "$S"; then
    echo "✅ $S running"
  else
    echo "❌ $S not running:"; journalctl -u "$S" -n 15 --no-pager -o cat; FAILED=1
  fi
done
[ "$FAILED" = "0" ] || exit 1

if [ "$FIRST_RUN" = "1" ]; then
  venv/bin/python run_catalyst_watch.py --notify-test
  echo "📨 Test message attempted (see 'sent via' above) — check Telegram."
  echo "   First event calendar arrives in ~4-5 min (first CoinMarketCal scan), then daily 08:00 IST."
  echo "⚠️  Stop the copies on your Mac now, or every alert arrives twice:  ./alerts_service.sh uninstall"
fi
echo "✅ Gamma alert bots deployed ($(git log -1 --format='%h %s'))"
