"""Extraction written against markup captured from the live site.

The fixtures below are real BBB shapes, not invented ones: React's empty-comment
text splits, the <dt>/<dd> details list, the bpr-letter-grade span, and the
embedded analytics JSON that carries accreditation, rating and the website.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import parse

# Trimmed from a real profile page.
PROFILE = '''
<link rel="canonical" href="https://www.bbb.org/us/ks/wichita/profile/plumber/creekmore-plumbing-heating-0714-15740"/>
<h2 data-section-heading="true">About This Business</h2>
<div class="bpr-overview-dates stack">
  <p class="bds-body"><strong>BBB Accredited Since:</strong> <!-- -->10/21/2009</p>
  <p class="bds-body"><strong>Years in Business:</strong> <!-- -->78</p>
</div>
<div class="bpr-details-dl-data"><dt>BBB File Opened:</dt><dd>4/9/2009</dd></div>
<div class="bpr-details-dl-data"><dt>Business Started:</dt><dd>1/1/1948</dd></div>
<div class="bpr-details-dl-data"><dt>Business Incorporated:</dt><dd>8/29/1962</dd></div>
<div class="bpr-rating-card stack" id="rating">
  <h3 class="bds-h5">BBB Rating</h3>
  <span class="bpr-letter-grade" style="cursor:pointer">A+</span>
</div>
<script>window.__DATA__ = {"business_info":{"accredited_status":"AB","business_id":"15740",
"business_name":"Creekmore Plumbing &amp; Heating","business_phone":"(316) 264-1342",
"business_rating":"A+","tob_id":"10113-000"},"additionalEmailAddresses":[],
"additionalWebsiteAddresses":["https://creekmoreplumbing.com/"]};</script>
'''

# The search page's JSON-LD, exactly as BBB emits it.
SEARCH = '''
<script type="application/ld+json">{"@context":"https://schema.org","@type":"SearchResultsPage",
"mainEntity":{"@type":"ItemList","name":"Search results for Plumber near Cheney, KS",
"itemListOrder":"https://schema.org/ItemListOrderAscending","itemListElement":[
{"@type":"ListItem","position":16,"item":{"@type":"LocalBusiness","name":"Roto Rooter Plumbers",
"address":{"@type":"PostalAddress","addressLocality":"Wichita","addressRegion":"KS",
"postalCode":"67211-5035","addressCountry":"US","streetAddress":"801 E Mount Vernon St"},
"telephone":"(316) 858-2085",
"url":"https://www.bbb.org/us/ks/wichita/profile/plumber/roto-rooter-plumbers-0714-3657"}},
{"@type":"ListItem","position":17,"item":{"@type":"LocalBusiness","name":"P. Guthrie Plumbing Services",
"address":{"@type":"PostalAddress","addressLocality":"Hutchinson","addressRegion":"KS",
"postalCode":"67501-9240","addressCountry":"US","streetAddress":""},
"telephone":"(620) 899-6709",
"url":"https://www.bbb.org/us/ks/hutchinson/profile/plumber/p-guthrie-plumbing-services-0714-1000081062"}}
]}}</script>
'''


class RealSearchPageTest(unittest.TestCase):
    def test_search_results_parse(self):
        listings, skipped = parse.listings_from_html(SEARCH)
        self.assertEqual(len(listings), 2)
        self.assertEqual(skipped, 0)

    def test_fields_match_the_capture(self):
        first = parse.listings_from_html(SEARCH)[0][0]
        self.assertEqual(first.company_name, "Roto Rooter Plumbers")
        self.assertEqual(first.phone, "+13168582085")
        self.assertEqual(first.street, "801 E Mount Vernon St")
        self.assertEqual(first.city, "Wichita")
        self.assertEqual(first.state, "KS")
        self.assertEqual(first.zip, "67211")
        self.assertTrue(first.profile_url.endswith("roto-rooter-plumbers-0714-3657"))

    def test_category_comes_from_the_profile_url(self):
        """Search JSON-LD has no category; the URL path does."""
        for listing in parse.listings_from_html(SEARCH)[0]:
            self.assertEqual(listing.category, "plumber")

    def test_service_area_business_has_no_street(self):
        """streetAddress is empty for service-area businesses -- not a parse failure."""
        second = parse.listings_from_html(SEARCH)[0][1]
        self.assertEqual(second.street, "")
        self.assertEqual(second.city, "Hutchinson")

    def test_search_page_carries_no_commercial_fields(self):
        """Documents why the detail pass exists at all."""
        first = parse.listings_from_html(SEARCH)[0][0]
        self.assertEqual(first.website, "")
        self.assertEqual(first.bbb_rating, "")
        self.assertIsNone(first.accredited)
        self.assertIsNone(first.years_in_business)


class RealProfilePageTest(unittest.TestCase):
    def setUp(self):
        self.listing = parse.listing_from_profile_html(PROFILE)

    def test_website_from_the_embedded_json(self):
        self.assertEqual(self.listing.website, "creekmoreplumbing.com")

    def test_years_in_business_across_the_comment_split(self):
        """React renders <strong>Years in Business:</strong> <!-- -->78."""
        self.assertEqual(self.listing.years_in_business, 78)

    def test_rating_and_accreditation(self):
        self.assertEqual(self.listing.bbb_rating, "A+")
        self.assertTrue(self.listing.accredited)

    def test_category_from_canonical_url(self):
        self.assertEqual(self.listing.category, "plumber")

    def test_non_accredited_status_reads_false(self):
        html = PROFILE.replace('"accredited_status":"AB"', '"accredited_status":"NAB"')
        self.assertFalse(parse.listing_from_profile_html(html).accredited)

    def test_employees_absent_on_real_profiles(self):
        """BBB no longer publishes headcount -- blank, never guessed."""
        self.assertIsNone(self.listing.employees)

    def test_a_plain_text_variant_still_parses(self):
        """Patterns match the label, not one particular tag arrangement."""
        listing = parse.listing_from_profile_html(
            "<p>Years in Business: 27</p><p>Number of Employees: 32</p>")
        self.assertEqual(listing.years_in_business, 27)
        self.assertEqual(listing.employees, 32)

    def test_merging_a_profile_into_a_search_result(self):
        """The end-to-end shape: search gives identity, profile fills the rest."""
        listing = parse.listings_from_html(SEARCH)[0][0]
        self.assertTrue(listing.needs_detail())
        listing.merge(parse.listing_from_profile_html(PROFILE))

        self.assertEqual(listing.company_name, "Roto Rooter Plumbers")   # not overwritten
        self.assertEqual(listing.phone, "+13168582085")
        self.assertEqual(listing.website, "creekmoreplumbing.com")
        self.assertEqual(listing.bbb_rating, "A+")
        self.assertEqual(listing.years_in_business, 78)
        self.assertTrue(listing.accredited)
        self.assertFalse(listing.needs_detail())


if __name__ == "__main__":
    unittest.main(verbosity=2)
