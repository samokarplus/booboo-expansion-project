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

say "Checking Codex CLI (used to pick the best moments; runs on your ChatGPT Plus subscription, not the API)"
if ! command -v codex >/dev/null; then
  echo "Installing Codex CLI..."
  brew install codex || npm install -g @openai/codex
fi
if codex login status 2>&1 | grep -qi "chatgpt"; then
  echo "Codex is logged in with ChatGPT. ✓"
  CODEX_OK=1
else
  CODEX_OK=0
fi
# Claude Code is now only an OPTIONAL fallback. Nothing is installed for it here.
command -v claude >/dev/null && echo "(Claude Code found: it is NOT used unless you set PURSUIT_LLM_FALLBACK=claude.)"

chmod +x pursuit-clips setup.sh
say "Installing the 'pursuit-clips' command"
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/pursuit-clips" "$HOME/.local/bin/pursuit-clips"
if ! grep -q '.local/bin' "$HOME/.zshrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
  echo "Added ~/.local/bin to your PATH (in ~/.zshrc). Open a NEW Terminal window before using the commands."
fi
say "Setup complete."
if [ "$CODEX_OK" = "0" ]; then
  echo
  echo "ONE MORE STEP: log in to Codex. In Terminal run:"
  echo "    codex login"
  echo "choose 'Sign in with ChatGPT' (NOT an API key), finish the login in your browser."
fi
echo
echo "Then make clips with:"
echo "    pursuit-clips \"https://www.youtube.com/watch?v=...\""
