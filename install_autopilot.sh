#!/bin/bash
# Turns the autopilot on (launchd, runs every 3 hours while the Mac is awake; a missed run happens on wake).
#   ./install_autopilot.sh            install / update
#   ./install_autopilot.sh --remove   turn it off completely
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL=com.pursuit.autopilot
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "$1" = "--remove" ]; then
  rm -f "$PLIST"
  echo "Autopilot removed. (Posts already scheduled on Post for Me will still go out.)"
  exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
{
cat <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$DIR/.venv/bin/python</string><string>$DIR/autopilot.py</string><string>run</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.npm-global/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StartCalendarInterval</key>
  <array>
XML
for h in 7 10 13 16 19 22; do
  echo "    <dict><key>Hour</key><integer>$h</integer><key>Minute</key><integer>15</integer></dict>"
done
cat <<XML
  </array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/pursuit-autopilot.launchd.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/pursuit-autopilot.launchd.log</string>
</dict>
</plist>
XML
} > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Autopilot installed: checks for new PURSUIT episodes at 7:15, 10:15, 13:15, 16:15, 19:15, 22:15."
echo "Status file: ~/Desktop/PURSUIT_CLIPS/AUTOPILOT_STATUS.txt"
