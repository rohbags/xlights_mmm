"""Core regression tests: python -m unittest test_core.py"""
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from mutagen.id3 import ID3, TIT2, TALB, COMM, TPE1
from mutagen.mp4 import MP4
from core import (apply_one, desired_fields, differences, discover_sequences,
                  prepare_jobs, read_tags, scan_sequence, Record)


class MetadataTests(unittest.TestCase):
    def test_xsq_remains_byte_identical_and_animation_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mp3 = root / "audio.mp3"
            mp3.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 500)
            tags = ID3()
            tags.add(TIT2(encoding=3, text=["Old title"]))
            tags.add(TALB(encoding=3, text=["Keep album"]))
            tags.save(mp3)
            xsq = root / "song.xsq"
            xsq.write_text(f'<xsequence><head><song>New title</song><artist>Someone</artist>'
                           f'<album></album><comment></comment><sequenceType>Media</sequenceType>'
                           f'<mediaFile>{mp3}</mediaFile></head><bad-tail>', encoding="utf-8")
            animation = root / "animation.xsq"
            animation.write_text('<xsequence><head><sequenceType>Animation</sequenceType></head></xsequence>')
            original = hashlib.sha256(xsq.read_bytes()).hexdigest()
            self.assertEqual(len(discover_sequences(root, False)), 2)
            record = scan_sequence(xsq, False)
            self.assertEqual(record.status, "Different")
            self.assertIsNone(scan_sequence(animation, False))
            self.assertEqual(apply_one(mp3, desired_fields(record.expected, False))[0], "Written & verified")
            self.assertEqual(read_tags(mp3)["album"], "Keep album")
            self.assertEqual(hashlib.sha256(xsq.read_bytes()).hexdigest(), original)
            self.assertEqual(apply_one(mp3, desired_fields(record.expected, False))[0], "Match")
            self.assertEqual(apply_one(mp3, {"album": ""})[0], "Written & verified")
            self.assertEqual(read_tags(mp3)["album"], "")

    def test_existing_nonstandard_comment_and_blank_xsq(self):
        """Regression: named/foreign ID3 comments survive preserve mode,
        but are cleared if the user explicitly chooses clear blanks."""
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "named-comment.mp3"
            media.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 500)
            tags = ID3()
            tags.add(COMM(encoding=3, lang="eng", desc="source", text=["Ripped and posted by SantaDa"]))
            tags.add(COMM(encoding=3, lang="deu", desc="", text=["0"]))
            tags.save(media)
            self.assertNotEqual(read_tags(media)["comment"], "")
            xsq = Path(tmp) / "test.xsq"
            xsq.write_text(f'<xsequence><head><sequenceType>Media</sequenceType>'
                           f'<mediaFile>{media}</mediaFile><song>Test song</song>'
                           f'<comment></comment></head></xsequence>')
            before_xsq = xsq.read_bytes()
            record = scan_sequence(xsq, False)
            self.assertEqual(apply_one(media, desired_fields(record.expected, False))[0], "Written & verified")
            self.assertTrue(ID3(media).getall("COMM"), "Default mode must preserve existing comment frames")
            self.assertEqual(apply_one(media, desired_fields(record.expected, True))[0], "Written & verified")
            self.assertEqual(read_tags(media)["comment"], "")
            self.assertEqual(ID3(media).getall("COMM"), [])
            self.assertEqual(xsq.read_bytes(), before_xsq)

    def test_writing_nonblank_comment_replaces_nonstandard_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "comment.mp3"
            media.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 500)
            tags = ID3()
            tags.add(COMM(encoding=3, lang="eng", desc="source", text=["Amazon.com Song ID: 20353849"]))
            tags.save(media)
            status, actual = apply_one(media, {"comment": "New XSQ comment"})
            self.assertEqual(status, "Written & verified")
            self.assertEqual(actual["comment"], "New XSQ comment")
            self.assertEqual(len(ID3(media).getall("COMM")), 1)

    def test_double_escaped_entities_are_decoded_before_compare_and_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            mp3 = folder / "music.mp3"
            mp3.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 500)
            tags = ID3()
            tags.add(TPE1(encoding=3, text=["Snopp Dogg &amp; Anna Kendrick"]))
            tags.save(mp3)
            xsq = folder / "escaped.xsq"
            xsq.write_text(
                f'<xsequence><head><sequenceType>Media</sequenceType>'
                f'<mediaFile>{mp3}</mediaFile>'
                '<artist>Snopp Dogg &amp;amp; Anna Kendrick</artist>'
                '<song>Mike Candy&amp;apos;s Bootleg</song>'
                '</head></xsequence>', encoding="utf-8")
            original = xsq.read_bytes()
            record = scan_sequence(xsq, False)
            self.assertEqual(record.expected["artist"], "Snopp Dogg & Anna Kendrick")
            self.assertEqual(record.expected["song"], "Mike Candy's Bootleg")
            self.assertEqual(record.status, "Different")
            status, _ = apply_one(mp3, desired_fields(record.expected, False))
            self.assertEqual(status, "Written & verified")
            self.assertEqual(read_tags(mp3)["artist"], "Snopp Dogg & Anna Kendrick")
            self.assertEqual(read_tags(mp3)["song"], "Mike Candy's Bootleg")
            self.assertEqual(scan_sequence(xsq, False).status, "Match")
            self.assertEqual(xsq.read_bytes(), original)

    def test_clear_blank_comment_with_empty_preferred_and_nonempty_alternate(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "comment.mp3"
            media.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 500)
            tags = ID3()
            tags.add(COMM(encoding=3, lang="eng", desc="", text=[""]))
            tags.add(COMM(encoding=3, lang="eng", desc="source", text=["Old comment"]))
            tags.save(media)
            self.assertEqual(read_tags(media)["comment"], "Old comment")
            status, _ = apply_one(media, {"comment": ""})
            self.assertEqual(status, "Written & verified")
            self.assertEqual(ID3(media).getall("COMM"), [])
            self.assertEqual(apply_one(media, {"comment": ""})[0], "Match")

    def test_duplicate_conflict(self):
        one = Record("a.xsq", "/tmp/sound.mp3", expected={"song": "A"})
        two = Record("b.xsq", "/tmp/sound.mp3", expected={"song": "B"})
        jobs, failures = prepare_jobs([one, two], [0, 1], False)
        self.assertEqual(jobs, [])
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0][0], "Conflict")

    def test_m4a_when_ffmpeg_available(self):
        from shutil import which
        if which("ffmpeg") is None:
            self.skipTest("ffmpeg unavailable for test media creation")
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "test.m4a"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                            "anullsrc=r=44100:cl=mono", "-t", "0.1", "-c:a", "aac",
                            "-y", str(media)], check=True)
            obj = MP4(media)
            obj["\xa9gen"] = ["Jazz"]
            obj.save()
            self.assertEqual(apply_one(media, {"song": "Test", "comment": "Test comment"})[0], "Written & verified")
            self.assertEqual(read_tags(media)["comment"], "Test comment")
            self.assertEqual(MP4(media)["\xa9gen"], ["Jazz"])


if __name__ == "__main__":
    unittest.main()
