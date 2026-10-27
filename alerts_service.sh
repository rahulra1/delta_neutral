#!/bin/zsh
# Keep the Telegram alert bots running on this Mac via launchd (user LaunchAgents: start at login, auto-restart on crash).
#   ./alerts_service.sh install     # (re)install + start all 3 bots and a "stay awake" helper
#   ./alerts_service.sh status      # show whether each is running
#   ./alerts_service.sh logs        # tail the logs
#   ./alerts_service.sh uninstall   # stop and remove everything
# Bots: run_event_strategy.py (calendar + paper trades), run_catalyst_watch.py (breaking news), run_listing_monitor.py (Upbit).
# The Mac must stay ON and awake: the helper runs `caffeinate -i` (blocks idle sleep); closing the lid still sleeps it.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-$DIR/venv/bin/python3}"
LA="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
LABELS=(com.algox.event-strategy com.algox.catalyst-watch com.algox.listing-monitor com.algox.caffeinate)

args_for() {
  case "$1" in
    com.algox.event-strategy)  echo "$PY -u $DIR/run_event_strategy.py --notify" ;;
    com.algox.catalyst-watch)  echo "$PY -u $DIR/run_catalyst_watch.py --notify --digest-hours 100000" ;;
    com.algox.listing-monitor) echo "$PY -u $DIR/run_listing_monitor.py --notify" ;;
    com.algox.caffeinate)      echo "/usr/bin/caffeinate -i" ;;
  esac
}

write_plist() {
  local label=$1 name=${1#com.algox.}
  local xml_args=""
  for a in ${(z)$(args_for $label)}; do xml_args+="    <string>$a</string>"$'\n'; done
  cat > "$LA/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
$xml_args  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DIR/logs/$name.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/$name.log</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict>
</plist>
EOF
}

case "${1:-status}" in
  install)
    mkdir -p "$LA" "$DIR/logs"
    for l in $LABELS; do
      launchctl bootout "$DOMAIN/$l" 2>/dev/null
      write_plist $l
      launchctl bootstrap "$DOMAIN" "$LA/$l.plist" && echo "started $l"
    done ;;
  uninstall)
    for l in $LABELS; do
      launchctl bootout "$DOMAIN/$l" 2>/dev/null && echo "stopped $l"
      rm -f "$LA/$l.plist"
    done ;;
  status)
    for l in $LABELS; do
      line=$(launchctl print "$DOMAIN/$l" 2>/dev/null | grep -E "^\s+(state|pid) =" | tr -s ' ' | tr '\n' ' ')
      echo "$l: ${line:-NOT INSTALLED}"
    done ;;
  logs)
    tail -n 20 "$DIR"/logs/{event-strategy,catalyst-watch,listing-monitor}.log ;;
  *) echo "usage: $0 install|status|logs|uninstall"; exit 1 ;;
esac
