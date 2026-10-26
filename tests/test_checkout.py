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


class GuestHandoffTests(unittest.TestCase):
    def test_product_from_url(self):
        self.assertEqual(co.product_from_url("https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-star-white-unlocked"),
                         ("256GB", "Star White"))
        self.assertEqual(co.product_from_url("https://www.apple.com/shop/product/ABC12AM/A/usb-c-cable"), ("", ""))

    def test_cursor_goes_to_first_empty_card_field(self):
        self.assertEqual(co.first_empty_field(["", "", ""]), 0)              # nothing filled: card number
        self.assertEqual(co.first_empty_field(["4111 ****", "12/29", ""]), 2)  # autofilled: CVV
        self.assertEqual(co.first_empty_field([None, None, ""]), 2)          # only CVV box found
        self.assertIsNone(co.first_empty_field([None, None, None]))
        self.assertIsNone(co.first_empty_field(["x", "y", "z"]))

    def test_defaults_never_close_and_never_store_payment(self):
        self.assertFalse(co.DEFAULT_CONFIG["close_on_failure"])
        for key in co.DEFAULT_CONFIG:
            self.assertNotRegex(key, r"card|cvv|cvc|payment")

    def test_payment_keys_in_local_file_are_ignored(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w") as f:
                json.dump({"cvv": "123", "contact": {"first_name": "H"}, "beep_seconds": 30}, f)
            orig, co.CONFIG_FILE = co.CONFIG_FILE, path
            try:
                cfg = co.load_config()
            finally:
                co.CONFIG_FILE = orig
        self.assertNotIn("cvv", cfg)
        self.assertEqual(cfg["beep_seconds"], 30)
        self.assertEqual(cfg["contact"]["first_name"], "H")

    def test_wanted_options_match_apple_page(self):
        opts = co.wanted_options("512GB", "Star White")
        self.assertIn(("dimensionColor", "starwhite", None), opts)
        self.assertIn(("dimensionCapacity", "512gb", None), opts)
        self.assertIn(("carrierModel", "UNLOCKED/US", None), opts)
        self.assertIn(("purchase_option_group", "fullprice", None), opts)
        self.assertEqual(opts[-1][0], "applecare-options")
        self.assertRegex("No AppleCare coverage", opts[-1][2])
        self.assertNotRegex("AppleCare+ with Theft and Loss", opts[-1][2])

    def test_guard_still_present(self):
        self.assertTrue(co.FORBIDDEN_CLICK.search("Place Your Order"))
        self.assertFalse(co.FORBIDDEN_CLICK.search("Credit or Debit Card"))

    def test_no_signin_automation(self):
        src = open(os.path.join(ROOT, "checkout_module.py")).read()
        self.assertNotIn("ACCOUNT_URL", src)
        self.assertNotIn("webdriver", src)


if __name__ == "__main__":
    unittest.main()
