import subprocess
import tempfile
import unittest
from pathlib import Path

import pursuit_clips as pc


class PursuitClipsTests(unittest.TestCase):
    def test_extract_json_ignores_prose_and_braces(self):
        reply = 'note {not json}\n```json\n{"clips":[{"start":1,"end":12,"start_words":"hello",' \
                '"end_words":"done","overall":80}]}\n```'
        self.assertEqual(pc.extract_json(reply)["clips"][0]["start"], 1.0)

    def test_extract_json_rejects_bad_clip(self):
        with self.assertRaisesRegex(ValueError, "start_words"):
            pc.extract_json('{"clips":[{"start":1,"end":12,"end_words":"done","overall":80}]}')

    def test_snap_does_not_cut_into_first_or_next_word(self):
        words = [
            {"w": "before", "s": 0.0, "e": 0.95},
            {"w": "Start", "s": 1.0, "e": 1.4},
            {"w": "here", "s": 1.45, "e": 1.8},
            {"w": "Next", "s": 1.84, "e": 2.2},
        ]
        start, end, _ = pc.snap_clip(
            {"start": 1, "end": 1.8, "start_words": "Start here", "end_words": "Start here"}, words
        )
        self.assertLessEqual(start, 1.0)
        self.assertGreaterEqual(end, 1.82)
        self.assertLess(end, 1.84)

    def test_ass_escapes_override_syntax_and_has_highlights(self):
        ass = pc.build_ass(
            [{"w": r"we{ird}", "s": 2.0, "e": 2.4}, {"w": "title", "s": 2.5, "e": 3.0}],
            2.0, 2.0, r"A {safe} hook", False,
        )
        self.assertIn("We(ird)", ass)
        self.assertIn("A (safe) hook", ass)
        self.assertIn(pc.HIGHLIGHT, ass)
        self.assertNotIn(r"we{ird}", ass)

    def test_caption_groups_never_overlap(self):
        words = [{"w": w, "s": 0.5 * i, "e": 0.5 * i + 0.45} for i, w in
                 enumerate("one two three four. And then the next part of it".split())]
        ass = pc.build_ass(words, 0, 6, None, False)

        def secs(t):
            h, m, s = t.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        spans = sorted((secs(l.split(",")[1]), secs(l.split(",")[2]))
                       for l in ass.splitlines() if l.startswith("Dialogue: 0,"))
        for (s1, e1), (s2, _) in zip(spans, spans[1:]):
            self.assertLessEqual(e1, s2 + 1e-9, "two caption lines on screen at once")

    def test_names_are_safe_but_keep_unicode(self):
        self.assertEqual(pc.safe_folder_name('  Anya’s: "episode" / test  '), "Anya’s episode test")
        self.assertNotIn("/", pc.slugify("A/B: weird clip"))

    def test_ffmpeg_render_with_unicode_and_apostrophe_path(self):
        ffmpeg, ffprobe = pc.find_ffmpeg()
        with tempfile.TemporaryDirectory(prefix="pursuit_test_") as tmp:
            root = Path(tmp)
            source = root / "Anya’s test: it's fine.mp4"
            subprocess.run([
                ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
            ], check=True)
            ass = root / "captions.ass"
            ass.write_text(pc.build_ass([
                {"w": "Hello", "s": 0.1, "e": 0.7}, {"w": "Anya!", "s": 0.8, "e": 1.4},
            ], 0, 2, "A safe hook", False), encoding="utf-8")
            info = pc.probe(ffprobe, source)
            output = root / "finished.mp4"
            pc.render_clip(ffmpeg, source, info, 0, 2, ass, "video", 140, output)
            pc.verify_output(ffprobe, output, 2)

    def test_vertical_source_honors_caption_crop_band(self):
        ffmpeg, ffprobe = pc.find_ffmpeg()
        with tempfile.TemporaryDirectory(prefix="pursuit_band_test_") as tmp:
            root = Path(tmp)
            source = root / "vertical.mp4"
            subprocess.run([
                ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
                "color=red:s=360x512:r=30:d=2[top];color=blue:s=360x128:r=30:d=2[bottom];[top][bottom]vstack",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
            ], check=True)
            ass = root / "empty.ass"
            ass.write_text(pc.build_ass([], 0, 2, None, False), encoding="utf-8")
            output = root / "cropped.mp4"
            pc.render_clip(ffmpeg, source, pc.probe(ffprobe, source), 0, 2, ass, "video", None, output,
                           band=(0.0, 0.8))
            pixel = subprocess.run([
                ffmpeg, "-v", "error", "-ss", "1", "-i", str(output), "-vf",
                "crop=2:2:540:1600,format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-",
            ], capture_output=True, check=True).stdout
            self.assertGreater(pixel[0], pixel[2] * 2, "bottom caption band was not removed")


if __name__ == "__main__":
    unittest.main()
