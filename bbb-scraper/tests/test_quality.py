"""Lead quality: social/directory domains, exclusions, column maps, run reports."""

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import parse
import scraper
from parse import Listing


def read_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def har_with(records, path):
    entry = {
        "request": {"url": "https://www.bbb.org/api/businesssearch?page=1", "method": "GET",
                    "headers": [], "cookies": []},
        "response": {"content": {"mimeType": "application/json",
                                 "text": json.dumps({"searchResults": records})}},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"log": {"entries": [entry]}}, fh)


class SocialDomainTest(unittest.TestCase):
    def test_social_hosts_are_not_treated_as_company_domains(self):
        for url in ["https://www.facebook.com/acme-plumbing",
                    "https://yelp.com/biz/acme", "https://www.instagram.com/acme",
                    "https://linktr.ee/acme", "https://sites.google.com/view/acme"]:
            domain, social = parse.classify_website(url)
            self.assertEqual(domain, "", url)
            self.assertTrue(social.startswith("http"), url)

    def test_real_domains_are_unaffected(self):
        domain, social = parse.classify_website("https://www.acmeplumbing.com/contact")
        self.assertEqual(domain, "acmeplumbing.com")
        self.assertEqual(social, "")

    def test_lookalike_subdomains_are_still_company_domains(self):
        # a business hosting on a subdomain of its own is a real domain
        domain, _ = parse.classify_website("https://acme.wixsite.com/plumbing")
        self.assertEqual(domain, "acme.wixsite.com")

    def test_two_facebook_only_businesses_both_survive(self):
        """The bug this exists to prevent: one dedupe key for every Facebook page."""
        a = parse.listing_from_record({"businessName": "Acme Plumbing",
                                       "websiteUrl": "https://www.facebook.com/acme",
                                       "phone": "(910) 555-0134"})
        b = parse.listing_from_record({"businessName": "Riverside Plumbing",
                                       "websiteUrl": "https://www.facebook.com/riverside",
                                       "phone": "(910) 555-0199"})
        self.assertNotEqual(a.dedupe_key(), b.dedupe_key())
        unique, dupes = parse.dedupe([a, b])
        self.assertEqual(len(unique), 2)
        self.assertEqual(dupes, [])

    def test_the_social_link_is_kept_not_discarded(self):
        listing = parse.listing_from_record({"businessName": "Acme",
                                             "websiteUrl": "https://www.facebook.com/acme"})
        self.assertEqual(listing.website, "")
        self.assertEqual(listing.social_url, "https://www.facebook.com/acme")
        self.assertEqual(listing.as_row()["social_url"], "https://www.facebook.com/acme")

    def test_a_real_domain_wins_over_a_social_link_in_the_dom(self):
        from browser_client import listing_from_card_html
        html = """<div class="card result-card">
          <h3><a href="/us/nc/wilmington/profile/plumber/acme-1">Acme Plumbing</a></h3>
          <a href="https://www.facebook.com/acmeplumbing">Facebook</a>
          <a href="https://acmeplumbing.com">Website</a>
        </div>"""
        listing = listing_from_card_html(html)
        self.assertEqual(listing.website, "acmeplumbing.com")
        self.assertEqual(listing.social_url, "https://www.facebook.com/acmeplumbing")

    def test_social_only_and_phoneless_is_low_confidence(self):
        listing = Listing(company_name="A", social_url="https://facebook.com/a")
        self.assertTrue(listing.is_low_confidence())


class ExclusionTest(unittest.TestCase):
    def args(self, name=None, domain=None, path=None):
        return type("A", (), {"exclude_name": name, "exclude_domain": domain,
                              "exclude_file": path})()

    def test_name_fragment_is_case_insensitive(self):
        names, domains = scraper.load_exclusions(self.args(name="Roto-Rooter, home depot"))
        self.assertTrue(scraper.excluded(
            Listing(company_name="ROTO-ROOTER Plumbing & Water Cleanup"), names, domains))
        self.assertFalse(scraper.excluded(
            Listing(company_name="Cape Fear Plumbing"), names, domains))

    def test_domain_match_includes_subdomains(self):
        names, domains = scraper.load_exclusions(self.args(domain="homedepot.com"))
        self.assertTrue(scraper.excluded(Listing(company_name="A", website="homedepot.com"),
                                         names, domains))
        self.assertTrue(scraper.excluded(Listing(company_name="A", website="pro.homedepot.com"),
                                         names, domains))
        self.assertFalse(scraper.excluded(Listing(company_name="A", website="nothomedepot.com"),
                                          names, domains))

    def test_exclude_file_splits_names_from_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ex.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# comment\nroto-rooter\n\nhomedepot.com\nace hardware\n")
            names, domains = scraper.load_exclusions(self.args(path=path))
        self.assertIn("roto-rooter", names)
        self.assertIn("ace hardware", names)
        self.assertIn("homedepot.com", domains)

    def test_shipped_example_file_parses(self):
        path = os.path.join(os.path.dirname(HERE), "exclusions.example.txt")
        names, domains = scraper.load_exclusions(self.args(path=path))
        self.assertTrue(names)
        self.assertTrue(domains)


class OutputShapeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out.csv")
        self.har = os.path.join(self.tmp.name, "c.har")
        har_with([
            {"businessName": "Cape Fear Plumbing", "websiteUrl": "https://capefearplumbing.com",
             "phone": "(910) 555-0134", "address": {"city": "Wilmington", "state": "NC"},
             "yearsInBusiness": 34, "reportUrl": "/us/nc/w/profile/plumber/cape-1"},
            {"businessName": "Acme Plumbing", "websiteUrl": "https://www.facebook.com/acme",
             "phone": "(910) 555-0155", "address": {"city": "Wilmington", "state": "NC"},
             "reportUrl": "/us/nc/w/profile/plumber/acme-2"},
            {"businessName": "Roto-Rooter Plumbing", "websiteUrl": "https://rotorooter.com",
             "phone": "(910) 555-0200", "address": {"city": "Wilmington", "state": "NC"},
             "reportUrl": "/us/nc/w/profile/plumber/roto-3"},
        ], self.har)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra):
        argv = ["--category", "plumber", "--location", "wilmington-nc", "--replay", self.har,
                "--output", self.out, "--no-detail", "--no-resume", *extra]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(argv)
        return code, buf.getvalue()

    def test_exclusions_apply_end_to_end(self):
        code, output = self.run_cli("--exclude-name", "roto-rooter")
        self.assertEqual(code, 0, output)
        names = [r["company_name"] for r in read_csv(self.out)]
        self.assertNotIn("Roto-Rooter Plumbing", names)
        self.assertIn("Cape Fear Plumbing", names)
        self.assertIn("excluded        : 1", output)

    def test_excluded_rows_go_to_the_rejects_file(self):
        rejects = os.path.join(self.tmp.name, "rejects.csv")
        code, _ = self.run_cli("--exclude-name", "roto-rooter", "--rejects", rejects)
        self.assertEqual(code, 0)
        self.assertEqual([r["company_name"] for r in read_csv(rejects)], ["Roto-Rooter Plumbing"])

    def test_column_map_renames_and_selects(self):
        mapping = os.path.join(self.tmp.name, "map.json")
        with open(mapping, "w", encoding="utf-8") as fh:
            json.dump({"columns": {"company_name": "Company", "website": "Domain",
                                   "social_url": "Social"}}, fh)
        code, output = self.run_cli("--column-map", mapping)
        self.assertEqual(code, 0, output)
        with open(self.out, encoding="utf-8") as fh:
            header = fh.readline().strip()
        self.assertEqual(header, "Company,Domain,Social")
        rows = read_csv(self.out)
        self.assertTrue(any(r["Social"] for r in rows))

    def test_shipped_example_map_is_valid(self):
        path = os.path.join(os.path.dirname(HERE), "column-map.example.json")
        fieldnames, mapping = parse.load_column_map(path)
        self.assertIn("Company", fieldnames)
        self.assertEqual(len(fieldnames), len(mapping))

    def test_bad_column_map_is_reported_not_silently_ignored(self):
        mapping = os.path.join(self.tmp.name, "bad.json")
        with open(mapping, "w", encoding="utf-8") as fh:
            json.dump({"columns": {"nonexistent_field": "X"}}, fh)
        with self.assertRaises(ValueError) as ctx:
            parse.load_column_map(mapping)
        self.assertIn("nonexistent_field", str(ctx.exception))

    def test_append_dedupe_survives_renamed_headers(self):
        mapping = os.path.join(self.tmp.name, "map.json")
        with open(mapping, "w", encoding="utf-8") as fh:
            json.dump({"columns": {"company_name": "Company", "website": "Company Domain",
                                   "phone": "Phone Number"}}, fh)
        self.run_cli("--column-map", mapping)
        first = len(read_csv(self.out))
        self.run_cli("--column-map", mapping, "--append")
        self.assertEqual(len(read_csv(self.out)), first,
                         "appending re-added rows already in the mapped file")

    def test_run_report_is_written(self):
        report = os.path.join(self.tmp.name, "run.json")
        code, output = self.run_cli("--exclude-name", "roto-rooter", "--report", report)
        self.assertEqual(code, 0)
        with open(report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["category"], "plumber")
        self.assertEqual(data["counts"]["excluded"], 1)
        self.assertEqual(data["counts"]["written"], len(read_csv(self.out)))
        self.assertEqual(data["approach"], "api")
        self.assertTrue(data["replayed_from"].endswith(".har"))
        self.assertIn("website", data["field_coverage"])

    def test_report_is_split_per_category_on_a_batch(self):
        report = os.path.join(self.tmp.name, "run.json")
        argv = ["--category", "plumber,electrician", "--location", "wilmington-nc",
                "--replay", self.har, "--output", self.out, "--no-detail", "--no-resume",
                "--report", report]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(argv)
        self.assertEqual(code, 0, buf.getvalue())
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "run-plumber.json")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "run-electrician.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
