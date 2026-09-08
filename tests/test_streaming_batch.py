import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_streaming_batch.py"
SPEC = importlib.util.spec_from_file_location("run_streaming_batch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StreamingBatchTests(unittest.TestCase):
    def test_balancing_preserves_upstream_order_per_worker(self):
        assignments = MODULE.balanced_assignments([(1, 8), (2, 3), (3, 7), (4, 2), (5, 6)], 2)
        self.assertEqual(sorted(sum(assignments, [])), [1, 2, 3, 4, 5])
        for worker_ids in assignments:
            self.assertEqual(worker_ids, sorted(worker_ids))
        self.assertIn(1, assignments[0])
        self.assertIn(2, assignments[1])

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
