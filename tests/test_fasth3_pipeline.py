import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run_fasth3_videos.py"
PAYLOAD = REPO / "comfy_workflows" / "fasth3-8step-i2v.payload.json"


class FastH3PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.comfy = self.root / "ComfyUI"
        self.project = self.root / "project"
        self.comfy.mkdir()
        images = self.project / "generated_images"
        images.mkdir(parents=True)
        (images / "shot_001.png").write_bytes(b"test png content")
        shot = [{"id": 42, "start": 3.2, "end": 5.21, "media_type": "video",
                 "shot": "still", "edit": {"motion_prompt": "camera glide"}}]
        input_json = self.root / "shots.json"
        input_json.write_text(json.dumps(shot))
        argv = [str(SCRIPT), "--project", str(self.project), "--comfy", str(self.comfy),
                "--input-json", str(input_json), "--payload", str(PAYLOAD), "--runtime-ids", "999"]
        with mock.patch.object(sys, "argv", argv):
            self.module = runpy.run_path(str(SCRIPT))
        self.item = {**shot[0], "_runtime_id": 1}

    def test_payload_uses_video_only_fasth3_graph_and_exact_source_size(self):
        graph, duration, frames, prompt, _ = self.module["make_prompt"](self.item)
        self.assertEqual((duration, frames, prompt), (3, 50, "camera glide"))
        self.assertEqual(graph["105:104"]["inputs"]["width"], 1280)
        self.assertEqual(graph["105:104"]["inputs"]["height"], 704)
        self.assertEqual(graph["105:104"]["inputs"]["prompt"], "camera glide")
        self.assertEqual(graph["105:111"]["inputs"]["value"], 3.0)
        self.assertEqual(graph["105:15"]["inputs"]["noise_seed"], 1000001)
        self.assertEqual(graph["137"]["inputs"]["crop"], "center")
        self.assertEqual(graph["105:91"]["inputs"]["fps"], 24)
        self.assertNotIn("audio", graph["105:91"]["inputs"])
        self.assertFalse(any("AudioVAE" in node["class_type"] for node in graph.values()))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_normalizes_24fps_source_to_exact_25fps_silent_clip(self):
        output = self.comfy / "output" / "video"
        output.mkdir(parents=True)
        source = output / "shot.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "testsrc2=s=1280x704:r=24:d=2.1",
                        "-c:v", "libx264", "-preset", "ultrafast", str(source)], check=True)
        normalized = self.module["normalize_comfy_output"]("shot.mp4", "video", 1, 50)
        valid, info = self.module["valid_video"](normalized, 50)
        self.assertTrue(valid, info)
        self.assertEqual(info["fps"], "25/1")
        self.assertEqual(info["frames"], 50)
        self.assertFalse(info["audio"])


if __name__ == "__main__":
    unittest.main()
