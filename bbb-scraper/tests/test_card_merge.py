"""Card markup fills what the JSON-LD leaves out.

A browser run reported 100% bbb_rating coverage while the HTTP run reported 0%
on the same pages: BBB's schema.org block carries no rating, and the result
card does. The JSON-LD stays authoritative -- it is the cleaner source -- and
the card only fills blanks.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import parse
import search_client

#: Shaped like a real page: JSON-LD without a rating, cards with one.
PAGE = """<!DOCTYPE html><html><body>
<script type="application/ld+json">
{"@type":"SearchResultsPage","mainEntity":{"@type":"ItemList","itemListElement":[
 {"@type":"ListItem","item":{"@type":"LocalBusiness","name":"Horizon Roofing",
  "url":"https://www.bbb.org/us/tn/nashville/profile/roofing-contractors/horizon-roofing-1",
  "telephone":"(615) 555-0100",
  "address":{"streetAddress":"1 Main St","addressLocality":"Nashville",
             "addressRegion":"TN","postalCode":"37201"}}},
 {"@type":"ListItem","item":{"@type":"LocalBusiness","name":"Cumberland Exteriors",
  "url":"https://www.bbb.org/us/tn/nashville/profile/roofing-contractors/cumberland-2",
  "telephone":"(615) 555-0200",
  "address":{"streetAddress":"2 Main St","addressLocality":"Nashville",
             "addressRegion":"TN","postalCode":"37203"}}}
]}}
</script>
<div class="card result-card">
  <a href="/us/tn/nashville/profile/roofing-contractors/horizon-roofing-1">Horizon Roofing</a>
  <div class="rating">BBB Rating: A+</div>
  <address>1 Main St, Nashville, TN 37201</address>
</div>
<div class="card result-card">
  <a href="/us/tn/nashville/profile/roofing-contractors/cumberland-2">Cumberland Exteriors</a>
  <div class="rating">BBB Rating: B-</div>
  <address>2 Main St, Nashville, TN 37203</address>
</div>
</body></html>"""


def have_bs4():
    try:
        import bs4  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(have_bs4(), "bs4 is not installed")
class CardMergeTest(unittest.TestCase):
    def parsed(self):
        listings, _skipped = parse.listings_from_html(PAGE, default_category="roofing-contractors")
        return listings

    def test_json_ld_alone_has_no_rating(self):
        """The premise: this is why the HTTP path reported 0%."""
        self.assertTrue(all(not l.bbb_rating for l in self.parsed()))

    def test_the_card_supplies_the_rating(self):
        listings = self.parsed()
        search_client.merge_card_fields(PAGE, listings, "roofing-contractors")
        self.assertEqual([l.bbb_rating for l in listings], ["A+", "B-"])

    def test_cards_are_matched_by_url_not_position(self):
        """JSON-LD can skip an entry the DOM still renders, so index-matching
        would silently attach one company's rating to another."""
        listings = self.parsed()
        listings.reverse()
        search_client.merge_card_fields(PAGE, listings, "roofing-contractors")
        by_name = {l.company_name: l.bbb_rating for l in listings}
        self.assertEqual(by_name["Horizon Roofing"], "A+")
        self.assertEqual(by_name["Cumberland Exteriors"], "B-")

    def test_a_relative_href_still_matches_an_absolute_url(self):
        listings = self.parsed()
        self.assertTrue(listings[0].profile_url.startswith("http"))
        merged = search_client.merge_card_fields(PAGE, listings, "roofing-contractors")
        self.assertEqual(merged, 2)

    def test_json_ld_values_are_never_overwritten(self):
        listings = self.parsed()
        listings[0].bbb_rating = "C"
        search_client.merge_card_fields(PAGE, listings, "roofing-contractors")
        self.assertEqual(listings[0].bbb_rating, "C")

    def test_a_page_with_no_cards_is_not_an_error(self):
        listings = self.parsed()
        self.assertEqual(
            search_client.merge_card_fields("<html></html>", listings, ""), 0)

    def test_no_listings_is_not_an_error(self):
        self.assertEqual(search_client.merge_card_fields(PAGE, [], ""), 0)


class NoBs4Test(unittest.TestCase):
    def test_the_merge_is_a_no_op_without_bs4(self):
        """The JSON-LD half works on its own, so a missing optional dependency
        must not take the whole HTTP path down with it."""
        import builtins
        real_import = builtins.__import__

        def blocked(name, *a, **kw):
            if name.startswith("bs4") or name == "browser_client":
                raise ImportError("blocked for this test")
            return real_import(name, *a, **kw)

        listings, _ = parse.listings_from_html(PAGE)
        builtins.__import__ = blocked
        try:
            self.assertEqual(search_client.merge_card_fields(PAGE, listings, ""), 0)
        finally:
            builtins.__import__ = real_import



@unittest.skipUnless(have_bs4(), "bs4 is not installed")
class NoFabricatedAddressTest(unittest.TestCase):
    """A service-area business has no storefront, so a blank street is a fact.

    The card parser finds an address by scanning the card's text, so a
    wholesale merge attached a street to businesses that publish none. A wrong
    address on a lead sheet is worse than an empty one.
    """

    SERVICE_AREA = """<!DOCTYPE html><html><body>
    <script type="application/ld+json">
    {"@type":"SearchResultsPage","mainEntity":{"@type":"ItemList","itemListElement":[
     {"@type":"ListItem","item":{"@type":"LocalBusiness","name":"Mobile Roofers",
      "url":"https://www.bbb.org/us/tn/nashville/profile/roofing-contractors/mobile-1",
      "telephone":"(615) 555-0300",
      "address":{"addressLocality":"Nashville","addressRegion":"TN",
                 "postalCode":"37201"}}}
    ]}}
    </script>
    <div class="card result-card">
      <a href="/us/tn/nashville/profile/roofing-contractors/mobile-1">Mobile Roofers</a>
      <div class="rating">BBB Rating: A</div>
      <p>Serving the Nashville area from 99 Elsewhere Rd, Nashville, TN 37201</p>
    </div>
    </body></html>"""

    def test_the_rating_is_taken_but_the_street_is_not(self):
        listings, _ = parse.listings_from_html(self.SERVICE_AREA)
        self.assertEqual(listings[0].street, "")
        search_client.merge_card_fields(self.SERVICE_AREA, listings, "")
        self.assertEqual(listings[0].bbb_rating, "A", "the rating should merge")
        self.assertEqual(listings[0].street, "",
                         "a street was invented for a service-area business")

    def test_only_the_declared_fields_can_be_filled(self):
        self.assertEqual(set(search_client.CARD_ONLY_FIELDS),
                         {"bbb_rating", "accredited"})


if __name__ == "__main__":
    unittest.main()
