"""schema.org JSON-LD parsing -- BBB's actual search results live here."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import parse

# An ItemList of LocalBusinesses, the shape a search results page emits.
SEARCH_HTML = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "ItemList",
  "itemListElement": [
    {
      "@type": "ListItem",
      "position": 1,
      "item": {
        "@type": ["LocalBusiness", "Plumber"],
        "name": "P. Guthrie Plumbing Services",
        "url": "https://www.bbb.org/us/ks/hutchinson/profile/plumber/p-guthrie-plumbing-services-0714-10",
        "telephone": "(620) 899-6709",
        "address": {
          "@type": "PostalAddress",
          "streetAddress": "1 Main St",
          "addressLocality": "Hutchinson",
          "addressRegion": "KS",
          "postalCode": "67501-9240"
        },
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.8", "reviewCount": "17"},
        "sameAs": ["https://www.guthrieplumbing.com", "https://www.facebook.com/guthrie"],
        "foundingDate": "1998-03-01"
      }
    },
    {
      "@type": "ListItem",
      "position": 2,
      "item": {
        "@type": "LocalBusiness",
        "name": "Creekmore Plumbing & Heating",
        "url": "https://www.bbb.org/us/ks/wichita/profile/plumber/creekmore-plumbing-heating-0714-15740",
        "telephone": "(316) 264-1342",
        "address": {
          "@type": "PostalAddress",
          "streetAddress": "440 Pattie St",
          "addressLocality": "Wichita",
          "addressRegion": "KS",
          "postalCode": "67211-1723"
        }
      }
    }
  ]
}
</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]}</script>
</head><body>results</body></html>
"""


class JsonLdExtractionTest(unittest.TestCase):
    def test_blocks_are_found(self):
        blocks = parse.find_jsonld_blocks(SEARCH_HTML)
        self.assertEqual(len(blocks), 2)

    def test_malformed_block_is_skipped_not_fatal(self):
        html = ('<script type="application/ld+json">{not json</script>'
                '<script type="application/ld+json">{"@type":"LocalBusiness",'
                '"name":"A","telephone":"9105550134"}</script>')
        blocks = parse.find_jsonld_blocks(html)
        self.assertEqual(len(blocks), 1)

    def test_itemlist_is_unwrapped(self):
        listings, skipped = parse.listings_from_html(SEARCH_HTML, default_category="plumber")
        self.assertEqual(len(listings), 2)
        self.assertEqual(skipped, 0)
        self.assertEqual([l.company_name for l in listings],
                         ["P. Guthrie Plumbing Services", "Creekmore Plumbing & Heating"])

    def test_fields_map_from_schema_org(self):
        listings, _ = parse.listings_from_html(SEARCH_HTML, default_category="plumber")
        first = listings[0]
        self.assertEqual(first.phone, "+16208996709")
        self.assertEqual(first.street, "1 Main St")
        self.assertEqual(first.city, "Hutchinson")
        self.assertEqual(first.state, "KS")
        self.assertEqual(first.zip, "67501")
        self.assertEqual(first.category, "plumber")
        self.assertTrue(first.profile_url.endswith("p-guthrie-plumbing-services-0714-10"))
        self.assertEqual(first.bbb_reviews, 17)

    def test_same_as_splits_website_from_social(self):
        first = parse.listings_from_html(SEARCH_HTML)[0][0]
        self.assertEqual(first.website, "guthrieplumbing.com")
        self.assertIn("facebook.com", first.social_url)

    def test_founding_date_becomes_years_in_business(self):
        first = parse.listings_from_html(SEARCH_HTML)[0][0]
        self.assertIsNotNone(first.years_in_business)
        self.assertGreater(first.years_in_business, 20)

    def test_bbb_url_lands_in_profile_not_website(self):
        second = parse.listings_from_html(SEARCH_HTML)[0][1]
        self.assertEqual(second.website, "")
        self.assertIn("bbb.org", second.profile_url)

    def test_breadcrumbs_and_other_lists_yield_nothing(self):
        html = ('<script type="application/ld+json">'
                '{"@type":"BreadcrumbList","itemListElement":'
                '[{"@type":"ListItem","name":"Home","item":"https://www.bbb.org/"}]}</script>')
        listings, _ = parse.listings_from_html(html)
        self.assertEqual(listings, [])

    def test_a_bare_business_object_works_too(self):
        html = ('<script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"HomeAndConstructionBusiness",'
                '"name":"Solo Plumbing","telephone":"910-555-0134",'
                '"address":{"addressLocality":"Wilmington","addressRegion":"NC"}}</script>')
        listings, _ = parse.listings_from_html(html)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].phone, "+19105550134")

    def test_dedupe_still_applies_to_jsonld_results(self):
        listings, _ = parse.listings_from_html(SEARCH_HTML)
        unique, dupes = parse.dedupe(listings + listings)
        self.assertEqual(len(unique), 2)
        self.assertEqual(len(dupes), 2)

    def test_no_jsonld_is_not_an_error(self):
        listings, skipped = parse.listings_from_html("<html><body>nothing</body></html>")
        self.assertEqual(listings, [])
        self.assertEqual(skipped, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
