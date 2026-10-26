"""Offline tests for checkout_module.py (no browser, no Playwright needed)."""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "checker"))
import checkout_module as co  # noqa: E402
import local_runner as lr  # noqa: E402


class FakeLocator:
    def __init__(self, text):
        self.text, self.clicked = text, False
    def inner_text(self, timeout=None):
        return self.text
    def get_attribute(self, name):
        return None
    def click(self, timeout=None):
        self.clicked = True


class GuardTests(unittest.TestCase):
    def test_never_clicks_place_order(self):
        for text in ("Place Your Order", "Place order", "Pay Now", "Complete Purchase", "Buy Now"):
            loc = FakeLocator(text)
            with self.assertRaises(co.StepFailed):
                co.safe_click(loc, "x")
            self.assertFalse(loc.clicked, text)

    def test_allows_normal_steps(self):
        for text in ("Add to Bag", "Check Out", "Continue to Payment", "Continue"):
            loc = FakeLocator(text)
            co.safe_click(loc, "x")
            self.assertTrue(loc.clicked, text)

    def test_payment_page_detection(self):
        self.assertTrue(co.PAYMENT_PAGE.search("https://secure.store.apple.com/shop/checkout?_s=Billing-init"))
        self.assertFalse(co.PAYMENT_PAGE.search("https://secure.store.apple.com/shop/checkout?_s=Fulfillment-init"))


class RunnerHookTests(unittest.TestCase):
    def test_checkout_target_from_alert(self):
        alert = ("512GB NIGHT SKY @ Grand Central +1", "Grand Central · ZIP 10017\nSoHo · ZIP 10012\nBag → …", "https://u")
        self.assertEqual(lr.checkout_target(alert), ("https://u", "10017", "Grand Central"))

    def test_runner_does_not_need_playwright(self):
        self.assertNotIn("playwright", open(os.path.join(ROOT, "local_runner.py")).read())


if __name__ == "__main__":
    unittest.main()


class PhoneHandoffTests(unittest.TestCase):
    def test_signed_in_url(self):
        self.assertTrue(co.signed_in_url("https://www.apple.com/shop/account/home"))
        self.assertFalse(co.signed_in_url("https://www.apple.com/shop/signIn?ssi=1AAABxyz&r=x"))
        self.assertFalse(co.signed_in_url("https://www.apple.com/shop/sorry/illegal_arguments"))
        self.assertFalse(co.signed_in_url("https://secure.store.apple.com/shop/signIn/account"))

    def test_product_from_url(self):
        self.assertEqual(co.product_from_url("https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-star-white-unlocked"),
                         ("256GB", "Star White"))

    def test_default_mode_is_phone_and_never_closes(self):
        self.assertEqual(co.DEFAULT_CONFIG["handoff"], "phone")
        self.assertFalse(co.DEFAULT_CONFIG["close_on_failure"])

    def test_guard_still_present(self):
        self.assertTrue(co.FORBIDDEN_CLICK.search("Place Your Order"))
