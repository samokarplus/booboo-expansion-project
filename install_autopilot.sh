#!/bin/bash
# AI: Claude Code (your Claude login) picks and checks the moments; Codex is only the backup if Claude is unavailable.
# Turns the autopilot on (launchd, runs every 3 hours while the Mac is awake; a missed run happens on wake).
#   ./install_autopilot.sh            install / update
#   ./install_autopilot.sh --remove   turn it off completely
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_PYTHON="$HOME/Library/Application Support/PURSUIT_AUTOPILOT/venv-local/bin/python"
LABEL=com.pursuit.autopilot
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "$1" = "--remove" ]; then
  rm -f "$PLIST"
  echo "Autopilot removed. (Posts already scheduled on Post for Me will still go out.)"
  exit 0
fi
# Run from local Application Support, outside Desktop/Documents file-provider storage.
# Keep the repository as the editable source; rerun this installer after source changes.
RUNTIME="$HOME/Library/Application Support/PURSUIT_AUTOPILOT/runtime"
export PURSUIT_INSTALL_SOURCE="$DIR" PURSUIT_INSTALL_RUNTIME="$RUNTIME"
BOOTSTRAP_PYTHON="$DIR/.venv/bin/python"
if [ -x "$LOCAL_PYTHON" ]; then BOOTSTRAP_PYTHON="$LOCAL_PYTHON"; fi
"$BOOTSTRAP_PYTHON" - <<'PYTHON'
import os, shutil
from pathlib import Path
source = Path(os.environ['PURSUIT_INSTALL_SOURCE'])
runtime = Path(os.environ['PURSUIT_INSTALL_RUNTIME'])
runtime.mkdir(parents=True, exist_ok=True)
def local_copy(src, dst):
    # Buffered reads avoid macOS fcopyfile stalls observed on this source folder.
    with open(src, 'rb') as incoming, open(dst, 'wb') as outgoing:
        shutil.copyfileobj(incoming, outgoing)
    shutil.copymode(src, dst)
    return dst
for path in source.glob('*.py'):
    local_copy(path, runtime / path.name)
shutil.copytree(source / 'fonts', runtime / 'fonts', dirs_exist_ok=True, copy_function=local_copy)
local_copy(source / 'requirements.txt', runtime / 'requirements.txt')
local_copy(source / 'clip_prompt.md', runtime / 'clip_prompt.md')
PYTHON
if [ ! -x "$LOCAL_PYTHON" ]; then
  "$BOOTSTRAP_PYTHON" -m venv "$(dirname "$(dirname "$LOCAL_PYTHON")")"
fi
"$LOCAL_PYTHON" -m pip install -r "$RUNTIME/requirements.txt"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
{
cat <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$LOCAL_PYTHON</string><string>$RUNTIME/autopilot.py</string><string>run</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PURSUIT_OUT</key><string>$HOME/Library/Application Support/PURSUIT_AUTOPILOT/clips</string><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.npm-global/bin:/usr/bin:/bin:/usr/sbin:/sbin</string><key>PURSUIT_LLM</key><string>claude</string><key>PURSUIT_MODEL</key><string>claude-sonnet-5-5</string><key>PURSUIT_LLM_FALLBACK</key><string>codex</string></dict>
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
echo "Status file: $HOME/Library/Application Support/PURSUIT_AUTOPILOT/clips/AUTOPILOT_STATUS.txt"
