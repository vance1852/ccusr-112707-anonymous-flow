"""核对本地记录在装载时完整保留了协议字段与输入顺序。"""
import json
import subprocess
import sys
import unittest

from device_replay import ROOT, load_profile, read_frames


class DeviceArchiveTest(unittest.TestCase):
    def test_envelopes_and_payload_widths(self):
        profile = load_profile()
        frames = list(read_frames())
        self.assertEqual(len(frames), 27)
        self.assertEqual(set(profile["wire_fields"]), {f["type"] for f in frames})
        self.assertEqual([f["arrival"] for f in frames], list(range(1, len(frames) + 1)))
        self.assertEqual([f["received"] for f in frames], sorted(f["received"] for f in frames))
        for frame in frames:
            with self.subTest(arrival=frame["arrival"]):
                self.assertIsInstance(frame["captured"], int)
                self.assertTrue(frame["source"])
                self.assertEqual(len(profile["wire_fields"][frame["type"]].split()), len(frame["args"]))

    def test_cli_matches_raw_archive(self):
        run = subprocess.run(
            [sys.executable, "device_replay.py"],
            cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual([json.loads(line) for line in run.stdout.splitlines()], list(read_frames()))

    def test_related_device_evidence_is_present_in_reception_order(self):
        frames = list(read_frames())
        indices = [8, 9, 10, 23]
        self.assertEqual([
            (frames[i]["source"], frames[i]["type"], frames[i]["args"][0]) for i in indices
        ], [('sA', 'fragment', 'sA'), ('sB', 'fragment', 'sB'), ('sA', 'fragment', 'sA'), ('ingress', 'apply', 'sA')])
        indices = [17, 18, 20, 21, 22, 24]
        self.assertEqual([
            (frames[i]["source"], frames[i]["type"], frames[i]["args"][0]) for i in indices
        ], [('ops', 'coverage', 'east'), ('ops', 'publish', 'tea'), ('edge', 'cache', 'tea'), ('sB', 'link', 'offline'), ('ops', 'share', 'east'), ('sB', 'fragment', 'sB')])
        indices = [14, 15, 16, 19]
        self.assertEqual([
            (frames[i]["source"], frames[i]["type"], frames[i]["args"][0]) for i in indices
        ], [('sE', 'fragment', 'sE'), ('sE', 'fragment', 'sE'), ('sE', 'fragment', 'sE'), ('ops', 'publish', 'cafe')])
