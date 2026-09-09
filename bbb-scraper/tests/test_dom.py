"""DOM parsing tests for Approach B -- no browser required."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_client import listing_from_card_html, listing_from_detail_html

CARD = """
<div class="card result-card">
  <h3><a href="/us/nc/wilmington/profile/plumber/acme-plumbing-0593-90012345">Acme Plumbing LLC</a></h3>
  <p class="bds-body">BBB Accredited Business</p>
  <div class="address">12 Market St, Wilmington, NC 28401</div>
  <a href="tel:+19105550134">(910) 555-0134</a>
  <a href="https://www.acmeplumbing.com" target="_blank">Visit Website</a>
  <span>34 years in business</span>
  <span class="rating">A+ rating</span>
</div>
"""

CARD_SPARSE = """
<div class="card result-card">
  <h3><a href="/us/nc/raleigh/profile/electrician/sparse-co-0593-1">Sparse Co</a></h3>
</div>
"""

DETAIL = """
<html><body>
  <h1>Acme Plumbing LLC</h1>
  <div class="accreditation">BBB Accredited Business since 1/1/2005</div>
  <p>BBB Rating: A+</p>
  <p>Business Started: 3/1/1992</p>
  <a href="https://www.acmeplumbing.com">acmeplumbing.com</a>
  <p>(910) 555-0134</p>
</body></html>
"""

DETAIL_NOT_ACCREDITED = """
<html><body>
  <p>This business is not BBB Accredited</p>
  <p>BBB Rating: NR</p>
  <p>18 years in business</p>
</body></html>
"""


class TestCardParsing(unittest.TestCase):
    def test_full_card(self):
        listing = listing_from_card_html(CARD, default_category="plumber")
        self.assertEqual(listing.company_name, "Acme Plumbing LLC")
        self.assertEqual(listing.website, "acmeplumbing.com")
        self.assertEqual(listing.phone, "+19105550134")
        self.assertEqual(listing.street, "12 Market St")
        self.assertEqual(listing.city, "Wilmington")
        self.assertEqual(listing.state, "NC")
        self.assertEqual(listing.zip, "28401")
        self.assertEqual(listing.category, "plumber")
        self.assertEqual(listing.years_in_business, 34)
        self.assertTrue(listing.accredited)
        self.assertEqual(listing.bbb_rating, "A+")
        self.assertTrue(listing.profile_url.startswith("https://www.bbb.org/us/nc/wilmington/profile/"))

    def test_sparse_card_flags_detail_visit(self):
        listing = listing_from_card_html(CARD_SPARSE, default_category="electrician")
        self.assertEqual(listing.company_name, "Sparse Co")
        self.assertIsNone(listing.years_in_business)
        self.assertIsNone(listing.accredited)
        self.assertTrue(listing.needs_detail())
        self.assertTrue(listing.is_low_confidence())


class TestDetailParsing(unittest.TestCase):
    def test_detail_fills_gaps(self):
        detail = listing_from_detail_html(DETAIL)
        self.assertTrue(detail.accredited)
        self.assertEqual(detail.bbb_rating, "A+")
        self.assertEqual(detail.website, "acmeplumbing.com")
        self.assertEqual(detail.phone, "+19105550134")
        self.assertIsNotNone(detail.years_in_business)
        self.assertGreaterEqual(detail.years_in_business, 33)

    def test_not_accredited_and_nr_rating(self):
        detail = listing_from_detail_html(DETAIL_NOT_ACCREDITED)
        self.assertFalse(detail.accredited)
        self.assertEqual(detail.bbb_rating, "")   # NR -> blank
        self.assertEqual(detail.years_in_business, 18)

    def test_merge_into_card(self):
        card = listing_from_card_html(CARD_SPARSE, default_category="electrician")
        card.merge(listing_from_detail_html(DETAIL_NOT_ACCREDITED))
        self.assertEqual(card.company_name, "Sparse Co")
        self.assertEqual(card.years_in_business, 18)
        self.assertFalse(card.accredited)
        self.assertFalse(card.needs_detail())


if __name__ == "__main__":
    unittest.main()
