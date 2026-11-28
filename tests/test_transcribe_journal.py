import hashlib
import tempfile
import unittest
from pathlib import Path

from course_exporter.transcribe import d2_pins, journal
from course_exporter.transcribe.source import CachedAudio
from tests.test_transcribe_source import asset, drive_file


class JournalFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1_000.0
        self.db = journal.Journal(self.root / "journal.sqlite3", clock=lambda: self.now)
        self.audio_path = self.root / ("b" * 64 + ".opus")
        self.audio_path.write_bytes(b"synthetic opus")
        self.sha = hashlib.sha256(self.audio_path.read_bytes()).hexdigest()

    def observe(self, name, language="ru", data=b"video", **kwargs):
        return self.db.observe(asset(asset_id=name), drive_file(data, id=name, **kwargs), language,
                               d2_pins.PRODUCER_CONFIG, "backlog")

    def pilot(self):
        ru = self.observe("russian")
        uk = self.observe("ukrainian", "uk")
        self.db.set_pilot([ru, uk])
        return ru, uk

    def ready(self):
        job = self.db.next_prepare()
        self.db.ready(job.id, CachedAudio(self.audio_path, job.source, "stream", self.sha, 10.5))
        return job


class JournalTest(JournalFixture, unittest.TestCase):
    def test_exact_revision_scope_and_gated_bulk(self):
        ru, uk = self.pilot()
        third = self.observe("other")
        with self.assertRaisesRegex(ValueError, "two verified pilot"):
            self.db.open_bulk()
        listed = []
        for _ in range(2):
            job = self.ready()
            lease = self.db.lease()
            self.db.accept(lease["lease_id"], b"result", lambda *_: None)
            self.db.next_publish()
            self.db.mark(job.id, "publishing", "verified", None)
            listed.append(job.id)
        self.assertEqual(set(listed), {ru, uk})
        self.assertIsNone(self.db.next_prepare())
        self.db.open_bulk()
        self.assertEqual(self.db.next_prepare().id, third)

    def test_receipt_survives_restart_expiry_and_publish_state(self):
        self.pilot()
        job = self.ready()
        lease = self.db.lease()
        receipt = self.db.accept(lease["lease_id"], b"received", lambda *_: None)
        self.now += 1000
        reopened = journal.Journal(self.db.path, clock=lambda: self.now)
        self.assertEqual(reopened.next_publish()[0].id, job.id)
        reopened.mark(job.id, "publishing", "verified", None)
        self.assertEqual(reopened.accept(lease["lease_id"], b"received", self.fail), receipt)
        with self.assertRaises(journal.LeaseRefused):
            reopened.accept(lease["lease_id"], b"different", self.fail)

    def test_renew_fences_old_lease_and_poison_item_does_not_block(self):
        self.pilot()
        job = self.ready()
        first = self.db.lease()
        self.now += 599
        self.assertNotEqual(self.db.renew(first["lease_id"])["expires_at"], first["expires_at"])
        self.now += 600
        self.assertEqual(self.db.request()["state"], "ready")
        with self.assertRaises(journal.LeaseRefused):
            self.db.accept(first["lease_id"], b"stale", self.fail)
        for _ in range(2):
            new = self.db.lease()
            self.assertEqual(new["job_id"], job.id)
            self.now += 601
            self.db.request()
        self.assertEqual(self.db.status()["counts"]["failed"], 1)
        self.assertNotEqual(self.ready().id, job.id)

    def test_maintenance_deferral_does_not_consume_a_lease(self):
        self.pilot()
        for _ in range(4):
            job = self.db.next_prepare()
            self.db.mark(job.id, "preparing", "deferred", "floor")
        self.db.pause("floor")
        self.assertEqual(self.db.request()["state"], "deferred")
        self.assertEqual(self.db.lease(), None)
        self.db.pause(None)
        self.assertEqual(self.ready().expired_leases, 0)

    def test_revision_key_and_first_observation(self):
        first = self.observe("id")
        observed = self.db.report(scoped=False)[0].observed
        self.now += 100
        self.assertEqual(self.observe("id"), first)
        self.assertNotEqual(self.observe("id", revision="new"), first)
        rows = {job.id: job for job in self.db.report(scoped=False)}
        self.assertEqual(rows[first].observed, observed)


def document(job):
    return {
        "schema": "uch-segments/1", "pipeline": "D2", "result_version": 1,
        "producer_config": d2_pins.PRODUCER_CONFIG, "language": job.language, "speakers": ["S1"],
        "source": {"clip_id": job.id, "audio_sha256": job.audio_sha256, "offset_s": 0, "duration_s": job.duration_s},
        "segments": [{
            "start": 0.1, "end": 0.9, "speaker": "S1", "text": "приклад", "flags": [],
            "words": [{"start": 0.1, "end": 0.9, "text": "приклад", "speaker": "S1", "flag": None}],
        }],
        "run": {"run_id": "demo", "host": "mac", "warm": False, "peak_rss_mb": 1.0,
                "stages_s": {"total": 0.5}, "request_ids": [], "usage": None, "cost_usd": None},
    }
