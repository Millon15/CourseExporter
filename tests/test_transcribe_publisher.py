import json
import unittest
from dataclasses import replace

from course_exporter.transcribe import publisher
from course_exporter.transcribe.source import SourceChanged
from tests.test_transcribe_journal import JournalFixture, document


class FakeWriter:
    def __init__(self, job):
        parent = job.source.parents[0]
        self.objects = {
            "root": self.folder("root", "Courses", []),
            "course": self.folder("course", job.asset.course, ["root"]),
            parent: self.folder(parent, job.asset.lesson_dir, ["course"]),
            job.source.id: self.entry(job.source.id, job.source.name, parent, job.source.size),
        }
        self.bodies = {}
        self.allocated = 0
        self.created = []
        self.fail = lambda _: None
        self.after = lambda _: None

    @staticmethod
    def folder(identifier, name, parents):
        return {"id": identifier, "name": name, "parents": parents, "mimeType": publisher.FOLDER}

    @staticmethod
    def entry(identifier, name, parent, size):
        return {"id": identifier, "name": name, "parents": [parent], "size": str(size)}

    def metadata(self, identifier):
        return self.objects.get(identifier)

    def content(self, identifier, limit):
        return self.bodies.get(identifier)

    def children(self, parent):
        return [obj for obj in self.objects.values() if obj["parents"] == [parent]]

    def reserve(self, count):
        ids = [f"reserved{i}" for i in range(self.allocated, self.allocated + count)]
        self.allocated += count
        return ids

    def create(self, file):
        self.created.append(file)
        self.fail(file)
        self.objects[file.id] = self.entry(file.id, file.name, file.parent, len(file.data))
        self.bodies[file.id] = file.data
        self.after(file)


class ReadSource:
    def __init__(self, source):
        self.source = source

    def metadata(self, _):
        return self.source


class PublicationTest(JournalFixture, unittest.TestCase):
    def publishing(self):
        self.pilot()
        self.ready()
        lease = self.db.lease()["lease_id"]
        data = json.dumps(document(self.db.leased_job(lease))).encode()
        self.db.accept(lease, data, lambda *_: None)
        job, data = self.db.next_publish()
        writer = FakeWriter(job)
        reader = ReadSource(job.source)
        return job, data, writer, reader

    def test_timeout_missing_and_conflict_present_reuse_reserved_ids(self):
        job, data, writer, reader = self.publishing()
        prefix = publisher._prefix(job)
        writer.objects["invalid"] = writer.entry("invalid", prefix + "selected.g0007.json", "P", 2)
        writer.bodies["invalid"] = b"{}"

        def fail(file):
            self.assertEqual(len(self.db.plan(job.id)[1]), 5)
            if len(writer.created) == 1:
                raise publisher.Retryable("timeout before the object existed")

        def after(file):
            if file.role == "text":
                raise publisher.Conflict("object already created")

        writer.fail, writer.after = fail, after
        publish = publisher.Publisher(self.db, writer, reader, "root", lambda _: True)
        publish.publish(job, data)
        self.assertEqual(writer.allocated, 5)
        self.assertEqual(writer.created[0].id, writer.created[1].id)
        self.assertEqual(writer.created[-1].name, prefix + "selected.g0008.json")
        self.assertTrue(publish.already_compliant(job))
        publish.publish(job, data)
        self.assertEqual(len(writer.created), 6)

    def test_resume_refuses_wrong_parent_without_another_id(self):
        job, data, writer, reader = self.publishing()

        def fail(file):
            writer.objects[file.id] = writer.entry(file.id, file.name, "elsewhere", len(file.data))
            raise publisher.Conflict("reserved ID exists")

        writer.fail = fail
        publish = publisher.Publisher(self.db, writer, reader, "root", lambda _: True)
        with self.assertRaisesRegex(publisher.PublishError, "different parent"):
            publish.publish(job, data)
        with self.assertRaisesRegex(publisher.PublishError, "different parent"):
            publish.publish(job, data)
        self.assertEqual(writer.allocated, 5)
        self.assertEqual(len(writer.created), 1)

    def test_change_after_outputs_creates_no_selection(self):
        job, data, writer, reader = self.publishing()

        def after(file):
            if file.role == "manifest":
                reader.source = replace(reader.source, revision="new")

        writer.after = after
        publish = publisher.Publisher(self.db, writer, reader, "root", lambda _: True)
        with self.assertRaises(SourceChanged):
            publish.publish(job, data)
        self.assertEqual([file.role for file in writer.created], ["vtt", "text", "segments", "manifest"])

    def test_refresh_to_partial_creates_no_selection_even_with_unchanged_drive_file(self):
        job, data, writer, reader = self.publishing()
        publish = publisher.Publisher(self.db, writer, reader, "root", lambda _: len(writer.created) < 4)
        with self.assertRaisesRegex(SourceChanged, "no longer lists"):
            publish.publish(job, data)
        self.assertEqual(len(writer.created), 4)

    def test_courses_ancestry_is_checked_before_creating(self):
        job, data, writer, reader = self.publishing()
        writer.objects["course"]["parents"] = ["another-root"]
        publish = publisher.Publisher(self.db, writer, reader, "root", lambda _: True)
        with self.assertRaises(SourceChanged):
            publish.publish(job, data)
        self.assertEqual(writer.allocated, 0)
        self.assertEqual(writer.created, [])
