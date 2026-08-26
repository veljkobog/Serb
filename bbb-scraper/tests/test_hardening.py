"""Silent-failure guards: identity, schema drift, spreadsheet safety, caps."""

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import checkpoint as checkpoint_mod
import parse
import scraper
from parse import Listing


def read_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class TollFreeIdentityTest(unittest.TestCase):
    def test_toll_free_detection(self):
        for number in ["+18005551234", "(833) 555-1234", "1-844-555-1234", "+18885551234"]:
            self.assertTrue(parse.is_toll_free(number), number)
        for number in ["+19105550134", "(212) 555-1234", ""]:
            self.assertFalse(parse.is_toll_free(number), number)

    def test_shared_toll_free_does_not_merge_two_businesses(self):
        """Franchisees and answering services share 800 numbers."""
        a = parse.listing_from_record({"businessName": "Acme Plumbing", "phone": "(800) 555-1234"})
        b = parse.listing_from_record({"businessName": "Riverside Plumbing", "phone": "800-555-1234"})
        self.assertIsNone(a.dedupe_key())
        unique, dupes = parse.dedupe([a, b])
        self.assertEqual(len(unique), 2)
        self.assertEqual(dupes, [])

    def test_local_numbers_still_dedupe(self):
        a = parse.listing_from_record({"businessName": "Local Co", "phone": "(910) 555-0134"})
        b = parse.listing_from_record({"businessName": "Local Co", "phone": "910-555-0134"})
        unique, dupes = parse.dedupe([a, b])
        self.assertEqual(len(unique), 1)
        self.assertEqual(len(dupes), 1)

    def test_toll_free_number_is_still_written(self):
        listing = parse.listing_from_record({"businessName": "Acme", "phone": "(800) 555-1234"})
        self.assertEqual(listing.phone, "+18005551234")
        self.assertFalse(listing.is_low_confidence())   # it's a contact, just not identity

    def test_website_still_wins_over_a_toll_free_phone(self):
        a = parse.listing_from_record({"businessName": "A", "websiteUrl": "https://acme.com",
                                       "phone": "(800) 555-1234"})
        self.assertEqual(a.dedupe_key(), "web:acme.com")


class SchemaDriftTest(unittest.TestCase):
    def test_more_name_keys_are_understood(self):
        for key in ("organizationName", "tradeName", "dbaName", "businessTitle"):
            listing = parse.listing_from_record({key: "Acme Plumbing", "phone": "9105550134"})
            self.assertEqual(listing.company_name, "Acme Plumbing", key)

    def test_nameless_records_are_counted_not_silently_dropped(self):
        payload = {"searchResults": [
            {"businessName": "Acme", "phone": "9105550134", "city": "W"},
            {"phone": "9105550135", "city": "W", "reportUrl": "/a"},     # no name at all
            {"phone": "9105550136", "city": "W", "reportUrl": "/b"},
        ]}
        listings, skipped = parse.listings_from_payload(payload)
        self.assertEqual(len(listings), 1)
        self.assertEqual(skipped, 2)

    def test_warning_names_the_drift(self):
        err = io.StringIO()
        with redirect_stderr(err):
            scraper.warn_unnamed(9, 1)
        message = err.getvalue()
        self.assertIn("9 record(s)", message)
        self.assertIn("--inspect-har", message)

    def test_no_warning_when_nothing_was_skipped(self):
        err = io.StringIO()
        with redirect_stderr(err):
            scraper.warn_unnamed(0, 50)
        self.assertEqual(err.getvalue(), "")


class SpreadsheetSafetyTest(unittest.TestCase):
    def test_formula_prefixes_are_neutralized(self):
        for payload in ['=HYPERLINK("http://evil.test","x")', '+1+1', '-2+3',
                        '@SUM(A1)', '=cmd|" /C calc"!A0']:
            self.assertTrue(parse.sanitize_cell(payload).startswith("'"), payload)

    def test_ordinary_names_are_untouched(self):
        for payload in ["Acme Plumbing", "A-1 Plumbing", "", "O'Brien & Sons"]:
            self.assertEqual(parse.sanitize_cell(payload), payload)

    def test_written_csv_escapes_names_but_not_phones(self):
        listing = parse.listing_from_record({
            "businessName": '=HYPERLINK("http://evil.test","Acme")',
            "websiteUrl": "https://acme.com", "phone": "(910) 555-0134",
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "o.csv")
            parse.write_csv(path, [listing])
            row = read_csv(path)[0]
        self.assertTrue(row["company_name"].startswith("'="))
        self.assertEqual(row["phone"], "+19105550134", "a normalized phone must not be escaped")
        self.assertEqual(row["website"], "acme.com")

    def test_escaping_survives_a_column_map(self):
        listing = parse.listing_from_record({"businessName": "=EVIL()", "websiteUrl": "https://a.com"})
        with tempfile.TemporaryDirectory() as tmp:
            mapping = os.path.join(tmp, "m.json")
            with open(mapping, "w", encoding="utf-8") as fh:
                json.dump({"columns": {"company_name": "Company"}}, fh)
            path = os.path.join(tmp, "o.csv")
            parse.write_csv(path, [listing], column_map=parse.load_column_map(mapping))
            row = read_csv(path)[0]
        self.assertTrue(row["Company"].startswith("'="))


class ResumeSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "progress.json")

    def tearDown(self):
        self.tmp.cleanup()

    def base_args(self, **over):
        values = dict(min_years=0, min_employees=0, min_bbb_reviews=0, max_bbb_complaints=None,
                      min_google_reviews=0, min_google_rating=0.0, allow_low_match=False,
                      drop_unknown=False, exclude_name=None, exclude_domain=None,
                      exclude_file=None, require_website=False)
        values.update(over)
        return type("A", (), values)()

    def test_fingerprint_is_stable_and_sensitive(self):
        first = checkpoint_mod.settings_fingerprint(scraper.filter_settings(self.base_args()))
        same = checkpoint_mod.settings_fingerprint(scraper.filter_settings(self.base_args()))
        changed = checkpoint_mod.settings_fingerprint(
            scraper.filter_settings(self.base_args(min_years=10)))
        self.assertEqual(first, same)
        self.assertNotEqual(first, changed)

    def test_fingerprint_survives_a_save_and_load(self):
        progress = checkpoint_mod.RunProgress(category="plumber", scope="nc", path=self.path)
        progress.settings = "abc123"
        progress.mark_done("charlotte-nc", 5)
        progress.save()
        loaded = checkpoint_mod.RunProgress.load(self.path, "plumber", "nc")
        self.assertEqual(loaded.settings, "abc123")


class DetailCapTest(unittest.TestCase):
    def test_cap_is_reported_not_silent(self):
        listings = [Listing(company_name=f"Co {i}", profile_url=f"https://www.bbb.org/p/{i}")
                    for i in range(10)]
        args = type("A", (), {"max_detail": 3, "verbose": False})()
        buf = io.StringIO()
        with redirect_stdout(buf):
            scraper.enrich_details(lambda url: None, listings, args)
        output = buf.getvalue()
        self.assertIn("visiting 3 detail page(s)", output)
        self.assertIn("7 skipped at --max-detail 3", output)

    def test_no_cap_note_when_nothing_is_skipped(self):
        listings = [Listing(company_name="Co", profile_url="https://www.bbb.org/p/1")]
        args = type("A", (), {"max_detail": 100, "verbose": False})()
        buf = io.StringIO()
        with redirect_stdout(buf):
            scraper.enrich_details(lambda url: None, listings, args)
        self.assertNotIn("skipped at", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
