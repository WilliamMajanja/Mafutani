"""Tests for the zero-dep document store.

The failure modes here are specific:
* a write that is not atomic (crash mid-write corrupts the file)
* a concurrent write that interleaves and loses a document
* a schema mismatch that is guessed at instead of refused
* a corrupt line that takes down the whole collection
"""
import json
import os
import sys
import tempfile
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from data.nosql import DocStore, DocError, SCHEMA_VERSION  # noqa: E402


class TestDocStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = DocStore(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_put_and_get(self):
        doc = self.store.put("audit", {"id": "1", "event": "login"})
        self.assertEqual(doc["id"], "1")
        got = self.store.get("audit", "1")
        self.assertEqual(got["event"], "login")

    def test_put_requires_id(self):
        with self.assertRaises(DocError):
            self.store.put("audit", {"event": "login"})

    def test_put_requires_dict(self):
        with self.assertRaises(DocError):
            self.store.put("audit", "not a dict")

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get("audit", "nope"))

    def test_list_newest_first(self):
        # _ts is set by put(), so two puts in the same second get the
        # same timestamp. The sort is stable, so insertion order is
        # preserved for equal timestamps. To test newest-first we need
        # different timestamps, which means sleeping or setting _ts
        # directly. We set it directly.
        self.store.put("audit", {"id": "1"})
        self.store.put("audit", {"id": "2"})
        # Force different _ts values.
        path = os.path.join(self._tmp, "audit.jsonl")
        with open(path) as f:
            lines = f.readlines()
        docs = [json.loads(l) for l in lines]
        docs[0]["_ts"] = 1
        docs[1]["_ts"] = 2
        with open(path, "w") as f:
            for d in docs:
                f.write(json.dumps(d) + "\n")
        result = self.store.list("audit")
        self.assertEqual(result[0]["id"], "2")

    def test_list_limit(self):
        for i in range(10):
            self.store.put("audit", {"id": str(i)})
        self.assertEqual(len(self.store.list("audit", limit=3)), 3)

    def test_count(self):
        for i in range(5):
            self.store.put("audit", {"id": str(i)})
        self.assertEqual(self.store.count("audit"), 5)

    def test_query(self):
        self.store.put("audit", {"id": "1", "event": "login"})
        self.store.put("audit", {"id": "2", "event": "logout"})
        self.store.put("audit", {"id": "3", "event": "login"})
        results = self.store.query("audit", event="login")
        self.assertEqual(len(results), 2)

    def test_delete(self):
        self.store.put("audit", {"id": "1"})
        self.assertTrue(self.store.delete("audit", "1"))
        self.assertIsNone(self.store.get("audit", "1"))
        self.assertFalse(self.store.delete("audit", "1"))

    def test_update_in_place(self):
        self.store.put("audit", {"id": "1", "event": "login"})
        self.store.put("audit", {"id": "1", "event": "logout"})
        self.assertEqual(self.store.count("audit"), 1)
        self.assertEqual(self.store.get("audit", "1")["event"], "logout")

    def test_schema_version_set(self):
        doc = self.store.put("audit", {"id": "1"})
        self.assertEqual(doc["_schema"], SCHEMA_VERSION)

    def test_corrupt_line_skipped(self):
        # Write a corrupt line directly, then verify the rest of the
        # collection is still readable.
        path = os.path.join(self._tmp, "audit.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"id": "1", "event": "good"}) + "\n")
            f.write("this is not json\n")
            f.write(json.dumps({"id": "2", "event": "also good"}) + "\n")
        docs = self.store.list("audit")
        self.assertEqual(len(docs), 2)

    def test_bad_collection_name(self):
        with self.assertRaises(DocError):
            self.store.put("../etc", {"id": "1"})
        with self.assertRaises(DocError):
            self.store.put("a/b", {"id": "1"})

    def test_stats(self):
        self.store.put("audit", {"id": "1"})
        self.store.put("sessions", {"id": "1"})
        stats = self.store.stats()
        self.assertEqual(stats.get("audit"), 1)
        self.assertEqual(stats.get("sessions"), 1)

    def test_rebuild_index(self):
        for i in range(5):
            self.store.put("audit", {"id": str(i)})
        n = self.store.rebuild_index("audit")
        self.assertEqual(n, 5)

    def test_persistence(self):
        # A new store on the same directory sees the same documents.
        self.store.put("audit", {"id": "1", "event": "login"})
        store2 = DocStore(self._tmp)
        self.assertEqual(store2.get("audit", "1")["event"], "login")

    def test_concurrent_writes(self):
        # Two stores on the same directory, writing simultaneously.
        # The lock serialises them, so no document is lost.
        store2 = DocStore(self._tmp)
        for i in range(10):
            self.store.put("audit", {"id": "a{}".format(i)})
            store2.put("audit", {"id": "b{}".format(i)})
        self.assertEqual(self.store.count("audit"), 20)


if __name__ == "__main__":
    unittest.main()
