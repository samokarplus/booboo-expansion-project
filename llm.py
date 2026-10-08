"""The one place that talks to an AI model.

Codex CLI (signed in with the ChatGPT subscription) is the default. Claude Code CLI is kept as an optional
fallback. Nothing here uses a billed API key: API-key environment variables are removed from the child process
and a Codex login that is not a ChatGPT login is refused.

Settings (all optional environment variables):
  PURSUIT_LLM=codex|claude          which one goes first (default: codex)
  PURSUIT_LLM_FALLBACK=claude|none  who to try if the first one is unavailable. Default: none, so Claude is NEVER
                                    used unless you set this to claude (or PURSUIT_LLM=claude).
  PURSUIT_CODEX_MODEL=<name>        override the Codex model (default: whatever `codex` is configured to use)
  PURSUIT_MODEL=<name>              override the Claude model (existing setting)
  PURSUIT_ALLOW_CODEX_API_KEY=1     allow a Codex login that bills an API key (off: refused)
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

HOME = Path.home()

# Words the CLIs print when a plan's allowance is used up or the service is temporarily unusable.
LIMIT_MARKERS = ("usage limit", "rate limit", "rate_limit", "limit reached", "hit your limit", "try again at",
                 "quota", "too many requests", "429", "upgrade to pro", "credits")
AUTH_MARKERS = ("not logged in", "please log in", "login required", "/login", "unauthorized", "401",
                "sign in", "token expired", "refresh token")


class LLMError(Exception):
    """The model answered, but the answer was unusable (or the CLI failed in a way retrying elsewhere won't fix)."""


class LLMUnavailable(LLMError):
    """Nothing was produced because the model can't be used right now (not installed, logged out, limit hit, offline)."""


def _find(name, extras):
    for c in [shutil.which(name)] + [str(p) for p in extras]:
        if c and os.path.exists(c):
            return c
    return None


def find_codex():
    return _find("codex", [Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
                           Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex"), HOME / ".local/bin/codex",
                           HOME / ".npm-global/bin/codex", HOME / ".bun/bin/codex"])


def find_claude():
    return _find("claude", [HOME / ".local/bin/claude", HOME / ".claude/local/claude"])


def _says(text, markers):
    t = (text or "").lower()
    return any(m in t for m in markers)


def _codex_env():
    """Child environment with API keys removed, so Codex can only use the ChatGPT subscription login."""
    env = dict(os.environ)
    if not os.environ.get("PURSUIT_ALLOW_CODEX_API_KEY"):
        for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT"):
            env.pop(k, None)
    return env


# ------------------------------------------------------------------------------------------------ providers

class Codex:
    name = "codex"
    _blocked = None     # reason string once a usage limit / login problem was seen in this process
    _auth = None

    @classmethod
    def available(cls):
        """True if Codex is installed and signed in with ChatGPT. Costs no model usage."""
        if cls._blocked:
            return False
        if cls._auth is None:
            cls._auth = cls._check_auth()
        return cls._auth

    @classmethod
    def _check_auth(cls):
        exe = find_codex()
        if not exe:
            cls._blocked = "Codex CLI not installed"
            return False
        try:
            r = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=30, env=_codex_env())
        except (subprocess.TimeoutExpired, OSError) as e:
            cls._blocked = f"could not check Codex login ({e})"
            return False
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 or "logged in" not in out.lower():
            cls._blocked = "Codex is not logged in (run: codex login)"
            return False
        if "chatgpt" not in out.lower() and not os.environ.get("PURSUIT_ALLOW_CODEX_API_KEY"):
            cls._blocked = ("Codex is logged in with an API key, which is billed per use. "
                            "Run: codex logout && codex login   and choose 'Sign in with ChatGPT'.")
            return False
        return True

    @classmethod
    def reason(cls):
        return cls._blocked or "unavailable"

    @classmethod
    def ask(cls, prompt, timeout, image=None):
        if not cls.available():
            raise LLMUnavailable(cls.reason())
        exe = find_codex()
        with tempfile.TemporaryDirectory(prefix="pursuit_codex_") as tmp:   # neutral folder: no project files picked up
            out = Path(tmp) / "reply.txt"
            cmd = [exe, "exec"]
            if image:
                cmd += [f"--image={image}"]
            cmd += ["--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "--color", "never",
                    "--output-last-message", str(out), "-C", tmp]
            if os.environ.get("PURSUIT_CODEX_MODEL"):
                cmd += ["--model", os.environ["PURSUIT_CODEX_MODEL"]]
            cmd += ["-"]    # prompt comes from stdin
            try:
                r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmp, timeout=timeout,
                                   env=_codex_env())
            except subprocess.TimeoutExpired:
                raise LLMUnavailable(f"Codex did not answer within {timeout}s")
            except OSError as e:
                raise LLMUnavailable(f"could not start Codex ({e})")
            text = out.read_text().strip() if out.exists() else ""
        combined = (r.stdout or "") + "\n" + (r.stderr or "")
        if r.returncode != 0 or not text:
            if _says(combined, LIMIT_MARKERS):
                cls._blocked = "Codex usage limit reached (resets later; see README)"
                raise LLMUnavailable(cls._blocked)
            if _says(combined, AUTH_MARKERS):
                cls._blocked = "Codex login expired (run: codex login)"
                raise LLMUnavailable(cls._blocked)
            raise LLMError(f"Codex failed (exit {r.returncode}): {combined.strip()[-500:]}")
        return text


class Claude:
    name = "claude"
    _blocked = None
    _auth = None

    @classmethod
    def available(cls):
        if cls._blocked:
            return False
        if cls._auth is None:
            cls._auth = cls._check_auth()
        return cls._auth

    @classmethod
    def _check_auth(cls):
        exe = find_claude()
        if not exe:
            cls._blocked = "Claude Code CLI not installed"
            return False
        try:
            r = subprocess.run([exe, "auth", "status", "--json"], capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0 and json.loads(r.stdout or "{}").get("loggedIn") is True
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
            cls._blocked = f"could not check Claude login ({e})"
            return False
        if not ok:
            cls._blocked = "Claude Code is not logged in"
        return ok

    @classmethod
    def reason(cls):
        return cls._blocked or "unavailable"

    @classmethod
    def ask(cls, prompt, timeout, image=None):
        if not cls.available():
            raise LLMUnavailable(cls.reason())
        exe = find_claude()
        with tempfile.TemporaryDirectory(prefix="pursuit_claude_") as tmp:
            cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence"]
            if image:
                shutil.copy(image, Path(tmp) / Path(image).name)
                cmd += ["--tools", "Read", "--allowedTools", "Read", "--add-dir", tmp]
            else:
                cmd += ["--tools", ""]
            if os.environ.get("PURSUIT_MODEL"):
                cmd += ["--model", os.environ["PURSUIT_MODEL"]]
            try:
                r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmp, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise LLMUnavailable(f"Claude did not answer within {timeout}s")
            except OSError as e:
                raise LLMUnavailable(f"could not start Claude ({e})")
        try:
            envelope = json.loads(r.stdout)
        except json.JSONDecodeError:
            raise LLMError(f"Claude CLI returned something unexpected:\n{(r.stdout or r.stderr)[-800:]}")
        result = envelope.get("result") or ""
        if envelope.get("is_error"):
            if _says(result, AUTH_MARKERS):
                cls._blocked = "Claude Code isn't logged in"
                raise LLMUnavailable(cls._blocked)
            if _says(result, LIMIT_MARKERS):
                cls._blocked = "Claude usage limit reached"
                raise LLMUnavailable(cls._blocked)
            raise LLMError(f"Claude CLI error: {result[:500]}")
        return result


PROVIDERS = {"codex": Codex, "claude": Claude}


# ------------------------------------------------------------------------------------------------ public API

def chain():
    """Providers in the order they will be tried."""
    first = os.environ.get("PURSUIT_LLM", "codex").strip().lower()
    if first not in PROVIDERS:
        raise LLMError(f"PURSUIT_LLM must be 'codex' or 'claude', not {first!r}")
    fallback = os.environ.get("PURSUIT_LLM_FALLBACK", "none").strip().lower()
    order = [PROVIDERS[first]]
    if fallback != "none" and fallback in PROVIDERS and fallback != first:
        order.append(PROVIDERS[fallback])
    return order


def available():
    """True if at least one configured provider can be used. Makes no model call."""
    return any(p.available() for p in chain())


def explain_unavailable():
    return "; ".join(f"{p.name}: {p.reason()}" for p in chain() if not p.available()) or "no AI provider configured"


def ask(prompt, timeout=900, image=None, log=None):
    """Send one prompt (optionally with one image file); return the model's text reply.

    Tries each provider in order. Moves on only when a provider is *unavailable* (logged out, limit hit, offline);
    an unusable answer raises LLMError instead. Raises LLMUnavailable if nobody could answer."""
    skipped = []
    for p in chain():
        try:
            reply = p.ask(prompt, timeout, image)
        except LLMUnavailable as e:
            skipped.append(f"{p.name}: {e}")
            if log:
                log(f"AI provider {p.name} unavailable ({e}); trying the next one." if p is not chain()[-1]
                    else f"AI provider {p.name} unavailable ({e}).")
            continue
        if skipped and log:
            log(f"Answered by {p.name} (fallback).")
        return reply
    raise LLMUnavailable("; ".join(skipped) or "no AI provider configured")


def reset_for_tests():
    for p in PROVIDERS.values():
        p._blocked = None
        p._auth = None
