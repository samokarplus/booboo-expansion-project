"""Tests for the AI layer. No real Codex/Claude call is ever made: fake CLI scripts stand in for them."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import llm  # noqa: E402

FAKE_CODEX = r'''#!/bin/bash
echo "$@" >> "$FAKE_DIR/codex_args.log"
env | grep -E '^(OPENAI|CODEX)_API_KEY' >> "$FAKE_DIR/codex_env.log"
if [ "$1" = "login" ]; then echo "${FAKE_LOGIN:-Logged in using ChatGPT}"; exit ${FAKE_LOGIN_RC:-0}; fi
out=""; while [ $# -gt 0 ]; do [ "$1" = "--output-last-message" ] && out="$2"; shift; done
cat > "$FAKE_DIR/codex_stdin.txt"
if [ -n "$FAKE_CODEX_ERR" ]; then echo "$FAKE_CODEX_ERR" >&2; exit 1; fi
printf '%s' "${FAKE_CODEX_REPLY:-{\"ok\": true}}" > "$out"
'''
FAKE_CLAUDE = r'''#!/bin/bash
echo "$@" >> "$FAKE_DIR/claude_args.log"
if [ "$1" = "auth" ]; then echo '{"loggedIn": true}'; exit 0; fi
cat > /dev/null
echo '{"is_error": false, "result": "from claude"}'
'''


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE)):
            f = self.dir / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        env = {"PATH": f"{self.dir}:/usr/bin:/bin", "FAKE_DIR": str(self.dir), "HOME": str(self.dir)}
        self.patches = [patch.dict(os.environ, env, clear=True),
                        patch.object(llm, "find_codex", lambda: str(self.dir / "codex")),
                        patch.object(llm, "find_claude", lambda: str(self.dir / "claude"))]
        for p in self.patches:
            p.start()
        llm.reset_for_tests()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        llm.reset_for_tests()
        self.tmp.cleanup()

    def read(self, name):
        f = self.dir / name
        return f.read_text() if f.exists() else ""

    def test_codex_is_used_first_with_safe_flags_and_prompt_on_stdin(self):
        self.assertEqual(llm.ask("pick clips", timeout=20), '{"ok": true}')
        args = self.read("codex_args.log")
        for flag in ("exec", "--sandbox read-only", "--ephemeral", "--skip-git-repo-check", "--output-last-message"):
            self.assertIn(flag, args)
        self.assertEqual(self.read("codex_stdin.txt"), "pick clips")
        self.assertEqual(self.read("claude_args.log"), "")          # Claude untouched

    def test_api_keys_are_never_passed_to_codex(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-secret", "CODEX_API_KEY": "sk-secret2"}):
            llm.ask("x", timeout=20)
        self.assertEqual(self.read("codex_env.log"), "")

    def test_image_is_attached(self):
        img = self.dir / "sheet.jpg"
        img.write_bytes(b"jpg")
        llm.ask("look", timeout=20, image=str(img))
        self.assertIn(f"--image={img}", self.read("codex_args.log"))

    def test_default_never_switches_to_claude(self):
        with patch.dict(os.environ, {"FAKE_CODEX_ERR": "You've hit your usage limit."}):
            with self.assertRaises(llm.LLMUnavailable):
                llm.ask("x", timeout=20)
        self.assertEqual(self.read("claude_args.log"), "")

    def test_usage_limit_falls_back_to_claude_only_when_opted_in(self):
        with patch.dict(os.environ, {"PURSUIT_LLM_FALLBACK": "claude", "FAKE_CODEX_ERR": "You've hit your usage limit. Try again at 3:00 PM."}):
            self.assertEqual(llm.ask("x", timeout=20), "from claude")
            n = self.read("codex_args.log").count("exec")
            self.assertEqual(llm.ask("y", timeout=20), "from claude")
            self.assertEqual(self.read("codex_args.log").count("exec"), n)   # blocked: no more Codex calls this run

    def test_usage_limit_with_fallback_off_is_unavailable_not_a_crash(self):
        with patch.dict(os.environ, {"FAKE_CODEX_ERR": "usage limit reached", "PURSUIT_LLM_FALLBACK": "none"}):
            with self.assertRaises(llm.LLMUnavailable):
                llm.ask("x", timeout=20)
        self.assertEqual(self.read("claude_args.log"), "")

    def test_api_key_login_is_refused(self):
        with patch.dict(os.environ, {"FAKE_LOGIN": "Logged in using an API key", "PURSUIT_LLM_FALLBACK": "none"}):
            self.assertFalse(llm.available())
            self.assertIn("API key", llm.explain_unavailable())
            self.assertEqual(self.read("codex_stdin.txt"), "")

    def test_logged_out_codex_uses_claude_fallback_when_opted_in_and_never_calls_codex_exec(self):
        with patch.dict(os.environ, {"PURSUIT_LLM_FALLBACK": "claude", "FAKE_LOGIN": "Not logged in", "FAKE_LOGIN_RC": "1"}):
            self.assertEqual(llm.ask("x", timeout=20), "from claude")
        self.assertNotIn("exec", self.read("codex_args.log"))

    def test_other_codex_failure_is_an_error_not_a_silent_switch(self):
        with patch.dict(os.environ, {"FAKE_CODEX_ERR": "internal exploded"}):
            with self.assertRaises(llm.LLMError) as cm:
                llm.ask("x", timeout=20)
            self.assertNotIsInstance(cm.exception, llm.LLMUnavailable)
        self.assertEqual(self.read("claude_args.log"), "")

    def test_claude_can_be_chosen_first(self):
        with patch.dict(os.environ, {"PURSUIT_LLM": "claude"}):
            self.assertEqual(llm.ask("x", timeout=20), "from claude")
        self.assertEqual(self.read("codex_stdin.txt"), "")

    def test_bad_provider_name_is_rejected(self):
        with patch.dict(os.environ, {"PURSUIT_LLM": "gpt"}):
            with self.assertRaises(llm.LLMError):
                llm.chain()


class AutopilotIntegration(unittest.TestCase):
    def test_visual_qc_outage_is_transient_and_refunds_the_attempt(self):
        import autopilot as ap
        with patch("autopilot.contact_sheet"), patch("autopilot.llm.ask", side_effect=llm.LLMUnavailable("usage limit")):
            with self.assertRaises(ap.Transient):
                ap.visual_qc("ffmpeg", {"file": "a.mp4", "duration_sec": 10}, "/tmp", {"onscreen_hook": "h"}, True, "text")
        state = {"episodes": {}}
        ep = {"id": "E1", "title": "t", "url": "u"}
        with patch("autopilot.llm_available", return_value=True), patch("autopilot.save"), \
                patch("autopilot.render_and_check", side_effect=ap.Transient("limit")):
            with self.assertRaises(ap.Transient):
                ap.process_episode(ep, state, dry_run=False)
        self.assertEqual(state["episodes"]["E1"]["attempts"], 0)

    def test_visual_qc_parses_a_verdict(self):
        import autopilot as ap
        reply = 'Sure.\n{"publish": true, "double_captions": false, "issues": []}'
        with patch("autopilot.contact_sheet"), patch("autopilot.llm.ask", return_value=reply):
            v = ap.visual_qc("ffmpeg", {"file": "a.mp4", "duration_sec": 10}, "/tmp", {"onscreen_hook": "h"}, True, "text")
        self.assertTrue(v["publish"])


if __name__ == "__main__":
    unittest.main()
