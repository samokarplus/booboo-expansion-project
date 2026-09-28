#!/bin/bash
# One-time setup for PURSUIT clips. Safe to re-run.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
say() { printf "\n\033[1m==> %s\033[0m\n" "$1"; }

if [ "$(uname -m)" != "arm64" ]; then
  echo "This tool needs an Apple Silicon Mac (M1/M2/M3/M4)." >&2; exit 1
fi

export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
if ! command -v brew >/dev/null; then
  echo "Homebrew is not installed. Install it first (paste this into Terminal, then re-run ./setup.sh):" >&2
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' >&2
  exit 1
fi

say "Installing FFmpeg (full, with subtitle support), yt-dlp, deno, Python 3.12"
brew install ffmpeg-full yt-dlp deno python@3.12
brew upgrade yt-dlp deno 2>/dev/null || true

say "Creating Python environment"
PY=/opt/homebrew/opt/python@3.12/bin/python3.12
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "Downloading the Whisper speech model (~1.6 GB, one time)"
.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/whisper-large-v3-turbo')"

say "Checking Claude Code (used to pick the best moments; runs on your Claude subscription)"
if ! command -v claude >/dev/null; then
  echo "Installing Claude Code CLI..."
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
if claude -p "Reply with just OK" --output-format json 2>/dev/null | grep -q '"is_error":false'; then
  echo "Claude Code is logged in. ✓"
  CLAUDE_OK=1
else
  CLAUDE_OK=0
fi

chmod +x pursuit-clips setup.sh
say "Installing the 'pursuit-clips' command"
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/pursuit-clips" "$HOME/.local/bin/pursuit-clips"
if ! grep -q '.local/bin' "$HOME/.zshrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
  echo "Added ~/.local/bin to your PATH (in ~/.zshrc). Open a NEW Terminal window before using the commands."
fi
say "Setup complete."
if [ "$CLAUDE_OK" = "0" ]; then
  echo
  echo "ONE MORE STEP: log in to Claude Code. In Terminal run:"
  echo "    claude"
  echo "choose 'Claude account with subscription', finish the login in your browser, then type /exit."
fi
echo
echo "Then make clips with:"
echo "    pursuit-clips \"https://www.youtube.com/watch?v=...\""
