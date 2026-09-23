import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkpoint import Checkpoint, listing_id
from parse import Listing


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ckpt.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_resume(self):
        ckpt = Checkpoint(category="plumber", location="wilmington-nc", path=self.path)
        ckpt.record(1, ["https://bbb.org/a", "https://bbb.org/b"], count_delta=2)
        ckpt.record(2, ["https://bbb.org/c"], count_delta=1)
        ckpt.save()

        loaded = Checkpoint.load(self.path, "plumber", "wilmington-nc")
        self.assertEqual(loaded.last_page, 2)
        self.assertEqual(loaded.count, 3)
        self.assertEqual(loaded.next_page(), 3)
        self.assertTrue(loaded.seen("https://bbb.org/a"))
        self.assertFalse(loaded.seen("https://bbb.org/zzz"))
        self.assertFalse(loaded.seen(None))

    def test_mismatched_run_is_not_resumed(self):
        ckpt = Checkpoint(category="plumber", location="wilmington-nc", path=self.path)
        ckpt.record(4, ["x"], count_delta=1)
        ckpt.save()
        other = Checkpoint.load(self.path, "roofing-contractors", "wilmington-nc")
        self.assertEqual(other.last_page, 0)
        self.assertEqual(other.next_page(), 1)

    def test_corrupt_file_starts_fresh(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        ckpt = Checkpoint.load(self.path, "plumber", "wilmington-nc")
        self.assertEqual(ckpt.last_page, 0)

    def test_missing_file_starts_fresh(self):
        ckpt = Checkpoint.load(os.path.join(self.tmp.name, "nope.json"), "plumber", "nc")
        self.assertEqual(ckpt.last_page, 0)

    def test_next_page_respects_skip(self):
        ckpt = Checkpoint(category="c", location="l", path=self.path)
        self.assertEqual(ckpt.next_page(start_page=5), 5)
        ckpt.record(7, [], 0)
        self.assertEqual(ckpt.next_page(start_page=5), 8)

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        ckpt = Checkpoint(category="c", location="l", path=self.path)
        ckpt.record(1, ["a"], 1)
        ckpt.save()
        ckpt.save()
        leftovers = [f for f in os.listdir(self.tmp.name) if f.startswith(".checkpoint-")]
        self.assertEqual(leftovers, [])
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["last_page"], 1)

    def test_clear(self):
        ckpt = Checkpoint(category="c", location="l", path=self.path)
        ckpt.save()
        self.assertTrue(os.path.exists(self.path))
        ckpt.clear()
        self.assertFalse(os.path.exists(self.path))

    def test_listing_id_prefers_profile_url(self):
        self.assertEqual(listing_id(Listing(company_name="A", profile_url="https://bbb.org/a",
                                            website="a.com")), "https://bbb.org/a")
        self.assertEqual(listing_id(Listing(company_name="A", website="a.com")), "web:a.com")
        self.assertEqual(listing_id(Listing(company_name="A", phone="+19105550134")), "tel:+19105550134")
        self.assertIsNone(listing_id(Listing(company_name="A")))


if __name__ == "__main__":
    unittest.main()
