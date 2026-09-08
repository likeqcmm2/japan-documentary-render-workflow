import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_streaming_batch.py"
SPEC = importlib.util.spec_from_file_location("run_streaming_batch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StreamingBatchTests(unittest.TestCase):
    def test_dynamic_ltx_wave_dispatches_every_runtime_id_once(self):
        calls = []
        lock = threading.Lock()

        def fake_run(command, **_kwargs):
            runtime_id = int(command[command.index("--runtime-ids") + 1])
            with lock:
                calls.append(runtime_id)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(MODULE, "run", side_effect=fake_run):
            production = {
                "project": Path(tmp),
                "input_json": Path(tmp) / "shots.json",
            }
            args = SimpleNamespace(ltx_workers=2)
            completed = MODULE.run_ltx_wave(
                production, [(1, 9), (2, 3), (3, 8), (4, 2), (5, 7)], 1, args,
                ["http://gpu0", "http://gpu1"], ["/comfy0", "/comfy1"],
            )
        self.assertEqual(completed, 5)
        self.assertEqual(sorted(calls), [1, 2, 3, 4, 5])

    def test_state_upserts_phase_and_records_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = MODULE.State(Path(tmp) / "state.sqlite3")
            state.set("k0", "images", "running")
            state.set("k0", "images", "complete", {"count": 3})
            with state.connect() as db:
                row = db.execute("SELECT status, detail FROM phases").fetchone()
                events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            self.assertEqual(row[0], "complete")
            self.assertEqual(json.loads(row[1]), {"count": 3})
            self.assertEqual(events, 2)

    def test_manifest_defaults_are_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shot = root / "shot.json"
            voice = root / "voice.wav"
            shot.write_text("[]")
            voice.write_bytes(b"wav")
            manifest = root / "batch.json"
            manifest.write_text(json.dumps({"productions": [{"shot_json": "shot.json", "voice": "voice.wav"}]}))
            productions = MODULE.normalize_productions(manifest, root / "projects")
            self.assertEqual(productions[0]["name"], "shot")
            self.assertEqual(productions[0]["drive_name"], "shot_final_video.mp4")


if __name__ == "__main__":
    unittest.main()
