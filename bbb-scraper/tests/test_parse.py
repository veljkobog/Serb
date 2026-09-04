import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parse
from parse import Listing


class TestNormalizers(unittest.TestCase):
    def test_domain_normalization(self):
        cases = {
            "https://www.AcmePlumbing.com/contact?utm=1": "acmeplumbing.com",
            "http://acme-plumbing.net": "acme-plumbing.net",
            "www.acme.co.uk": "acme.co.uk",
            "acme.com:8080/x": "acme.com",
            "  HTTPS://ACME.COM/  ": "acme.com",
        }
        for raw, want in cases.items():
            self.assertEqual(parse.normalize_domain(raw), want, raw)

    def test_domain_rejects_junk_and_bbb_links(self):
        for raw in ["", None, "n/a", "mailto:a@b.com", "/us/nc/wilmington/profile/plumber/acme",
                    "https://www.bbb.org/us/nc/profile/x", "notadomain", "some text here", 42]:
            self.assertEqual(parse.normalize_domain(raw), "", repr(raw))

    def test_phone_to_e164(self):
        for raw in ["(910) 555-0134", "910-555-0134", "9105550134", "+1 910 555 0134", "1-910-555-0134"]:
            self.assertEqual(parse.normalize_phone(raw), "+19105550134", raw)

    def test_phone_rejects_invalid(self):
        for raw in ["", None, "555-0134", "0105550134", "abc", "12345678901234"]:
            self.assertEqual(parse.normalize_phone(raw), "", repr(raw))

    def test_rating_blank_when_not_rated(self):
        self.assertEqual(parse.normalize_rating("A+"), "A+")
        self.assertEqual(parse.normalize_rating("b-"), "B-")
        self.assertEqual(parse.normalize_rating("NR"), "")
        self.assertEqual(parse.normalize_rating("Z"), "")
        self.assertEqual(parse.normalize_rating(None), "")

    def test_zip_and_state(self):
        self.assertEqual(parse.normalize_zip("28401-1234"), "28401")
        self.assertEqual(parse.normalize_zip("Wilmington NC 28401"), "28401")
        self.assertEqual(parse.normalize_zip("none"), "")
        self.assertEqual(parse.normalize_state("nc"), "NC")

    def test_years_never_guesses(self):
        today = date(2026, 8, 18)
        self.assertEqual(parse.parse_years_in_business("25 years", today), 25)
        self.assertEqual(parse.parse_years_in_business("In business: 12 yrs", today), 12)
        self.assertEqual(parse.parse_years_in_business(30, today), 30)
        self.assertEqual(parse.parse_years_in_business("3/1/1998", today), 28)
        self.assertEqual(parse.parse_years_in_business("1998-03-01", today), 28)
        self.assertEqual(parse.parse_years_in_business("1998", today), 28)
        # unknown stays unknown
        for raw in [None, "", "unknown", "many years of service", "n/a"]:
            self.assertIsNone(parse.parse_years_in_business(raw, today), repr(raw))
        # nonsense years rejected
        self.assertIsNone(parse.parse_years_in_business("2999", today))

    def test_parse_bool(self):
        self.assertTrue(parse.parse_bool("Yes"))
        self.assertFalse(parse.parse_bool("no"))
        self.assertIsNone(parse.parse_bool("maybe"))
        self.assertIsNone(parse.parse_bool(None))


class TestRecordExtraction(unittest.TestCase):
    def test_camel_case_schema(self):
        record = {
            "businessName": "Acme  Plumbing   LLC",
            "websiteUrl": "https://www.acmeplumbing.com/",
            "phone": "(910) 555-0134",
            "address": {"street": "12 Market St", "city": "Wilmington", "state": "nc",
                        "postalCode": "28401-1234"},
            "primaryCategory": "Plumber",
            "yearsInBusiness": 22,
            "isAccredited": True,
            "bbbRating": "A+",
            "reportUrl": "/us/nc/wilmington/profile/plumber/acme-0593-123",
        }
        listing = parse.listing_from_record(record)
        self.assertEqual(listing.company_name, "Acme Plumbing LLC")
        self.assertEqual(listing.website, "acmeplumbing.com")
        self.assertEqual(listing.phone, "+19105550134")
        self.assertEqual(listing.street, "12 Market St")
        self.assertEqual(listing.state, "NC")
        self.assertEqual(listing.zip, "28401")
        self.assertEqual(listing.years_in_business, 22)
        self.assertTrue(listing.accredited)
        self.assertEqual(listing.bbb_rating, "A+")
        self.assertEqual(listing.profile_url,
                         "https://www.bbb.org/us/nc/wilmington/profile/plumber/acme-0593-123")

    def test_snake_case_and_nested_variants(self):
        record = {
            "company_name": "Bee Electric",
            "web_address": "beeelectric.com",
            "phones": [{"number": "9105550111"}],
            "address_line1": "9 Oak Ave",
            "city": "Raleigh",
            "state_province": "NC",
            "zip_code": "27601",
            "categories": [{"name": "Electrician"}],
            "business_started_date": "1998-03-01",
            "accredited": "false",
            "rating": {"letter": "B"},
            "profile_url": "https://www.bbb.org/us/nc/raleigh/profile/electrician/bee",
        }
        listing = parse.listing_from_record(record)
        self.assertEqual(listing.company_name, "Bee Electric")
        self.assertEqual(listing.website, "beeelectric.com")
        self.assertEqual(listing.phone, "+19105550111")
        self.assertEqual(listing.category, "Electrician")
        self.assertEqual(listing.bbb_rating, "B")
        self.assertFalse(listing.accredited)
        self.assertIsNotNone(listing.years_in_business)

    def test_ambiguous_url_key_routed_by_host(self):
        # `url` is the BBB profile here...
        listing = parse.listing_from_record(
            {"name": "Cee Roofing", "url": "/us/nc/profile/roofing/cee", "website": "ceeroofing.com"}
        )
        self.assertEqual(listing.website, "ceeroofing.com")
        self.assertTrue(listing.profile_url.endswith("/us/nc/profile/roofing/cee"))

        # ...and the company site here, with no other website key present.
        listing = parse.listing_from_record({"name": "Dee HVAC", "url": "https://deehvac.com"})
        self.assertEqual(listing.website, "deehvac.com")
        self.assertEqual(listing.profile_url, "")

    def test_missing_fields_stay_blank(self):
        listing = parse.listing_from_record({"businessName": "Sparse Co"})
        row = listing.as_row()
        self.assertEqual(row["years_in_business"], "")
        self.assertEqual(row["accredited"], "")
        self.assertEqual(row["bbb_rating"], "")

    def test_default_category_applied(self):
        listing = parse.listing_from_record({"name": "X Co"}, default_category="plumber")
        self.assertEqual(listing.category, "plumber")


class TestFindRecords(unittest.TestCase):
    def test_finds_records_under_unknown_envelope(self):
        payload = {
            "meta": {"totalResults": 2, "facets": [{"id": 1}, {"id": 2}]},
            "data": {"searchResults": [
                {"businessName": "A", "phone": "9105550134", "reportUrl": "/a", "city": "W"},
                {"businessName": "B", "phone": "9105550135", "reportUrl": "/b", "city": "W"},
            ]},
        }
        records = parse.find_records(payload)
        self.assertEqual(len(records), 2)
        self.assertEqual(parse.find_total_count(payload), 2)

    def test_ignores_unrelated_lists(self):
        payload = {"filters": [{"id": 1, "label": "x"}, {"id": 2, "label": "y"}], "results": []}
        self.assertEqual(parse.find_records(payload), [])


class TestDedupeAndOutput(unittest.TestCase):
    def test_dedupe_on_domain_then_phone(self):
        listings = [
            Listing(company_name="A", website="acme.com"),
            Listing(company_name="A dup", website="acme.com", phone="+19105550134"),
            Listing(company_name="B", phone="+19105550199"),
            Listing(company_name="B dup", phone="+19105550199"),
            Listing(company_name="C", website="cee.com"),
        ]
        unique, dupes = parse.dedupe(listings)
        self.assertEqual(len(unique), 3)
        self.assertEqual(len(dupes), 2)
        domains = [l.website for l in unique if l.website]
        self.assertEqual(len(domains), len(set(domains)))

    def test_dedupe_merges_richer_duplicate(self):
        listings = [
            Listing(company_name="A", website="acme.com"),
            Listing(company_name="A", website="acme.com", phone="+19105550134", bbb_rating="A+"),
        ]
        unique, _ = parse.dedupe(listings)
        self.assertEqual(unique[0].phone, "+19105550134")
        self.assertEqual(unique[0].bbb_rating, "A+")

    def test_records_without_keys_are_not_collapsed(self):
        listings = [Listing(company_name="A"), Listing(company_name="B")]
        unique, dupes = parse.dedupe(listings)
        self.assertEqual(len(unique), 2)
        self.assertEqual(dupes, [])

    def test_low_confidence_detection(self):
        self.assertTrue(Listing(company_name="A").is_low_confidence())
        self.assertFalse(Listing(company_name="A", phone="+19105550134").is_low_confidence())
        self.assertFalse(Listing(company_name="A", website="a.com").is_low_confidence())

    def test_merge_never_overwrites(self):
        base = Listing(company_name="A", website="acme.com")
        base.merge(Listing(company_name="Z", website="other.com", years_in_business=15, accredited=True))
        self.assertEqual(base.company_name, "A")
        self.assertEqual(base.website, "acme.com")
        self.assertEqual(base.years_in_business, 15)
        self.assertTrue(base.accredited)

    def test_needs_detail(self):
        self.assertTrue(Listing(company_name="A").needs_detail())
        self.assertFalse(Listing(company_name="A", years_in_business=3, accredited=False).needs_detail())

    def test_csv_header_and_order(self):
        import csv, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            parse.write_csv(path, [Listing(company_name="A", website="a.com", accredited=True)])
            with open(path, encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
        self.assertEqual(rows[0], parse.FIELD_ORDER)
        self.assertEqual(rows[1][parse.FIELD_ORDER.index("accredited")], "true")



class RowCoverageTest(unittest.TestCase):
    """as_row() and FIELD_ORDER must agree, exactly.

    Two fields were added to FIELD_ORDER and to the dataclass but not to
    as_row(). csv.DictWriter fills a missing key with the restval, so every
    row shipped with those columns blank and nothing failed -- the Apollo
    match label, which is the entire sleeper signal, was never written. With a
    column map the same gap is a hard KeyError instead, which is how it was
    finally noticed.
    """

    def test_as_row_covers_every_declared_column(self):
        row = parse.Listing(company_name="X").as_row()
        self.assertEqual(set(row), set(parse.FIELD_ORDER),
                         "as_row() and FIELD_ORDER have drifted")

    def test_as_row_emits_the_columns_in_declared_order(self):
        self.assertEqual(list(parse.Listing().as_row()), parse.FIELD_ORDER)

    def test_every_dataclass_field_reaches_a_column(self):
        from dataclasses import fields
        declared = {f.name for f in fields(parse.Listing)}
        self.assertEqual(declared, set(parse.FIELD_ORDER),
                         "a Listing field exists that no column carries")

    def test_a_value_actually_reaches_the_csv(self):
        """Asserting the column exists is not enough -- it existed, and was
        empty on every row."""
        import csv
        import tempfile
        listing = parse.Listing(company_name="Sleeper Roofing",
                                phone="+17135551000",
                                apollo_org_id="a" * 24,
                                apollo_match="not-in-apollo")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            parse.write_csv(path, [listing])
            with open(path, encoding="utf-8") as fh:
                written = list(csv.DictReader(fh))
        self.assertEqual(written[0]["apollo_match"], "not-in-apollo")
        self.assertEqual(written[0]["apollo_org_id"], "a" * 24)

    def test_the_shipped_column_map_applies_without_a_keyerror(self):
        """The lead format names apollo columns; a gap in as_row crashes here."""
        import csv
        import tempfile
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        column_map = parse.load_column_map(os.path.join(here, "lead-format.json"))
        listing = parse.Listing(company_name="X", apollo_match="high")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mapped.csv")
            parse.write_csv(path, [listing], column_map=column_map)
            with open(path, encoding="utf-8") as fh:
                written = list(csv.DictReader(fh))
        self.assertEqual(written[0]["apollo_match"], "high")


if __name__ == "__main__":
    unittest.main()
