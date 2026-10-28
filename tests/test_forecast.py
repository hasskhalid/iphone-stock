"""Offline tests for tools/forecast.py (next-restock forecast)."""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import forecast as fc  # noqa: E402

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)
PRIORS = {"hour_counts_et": [0] * 10 + [50] * 4 + [0] * 10, "colour_counts_256gb": {"Night Sky": 60, "Star White": 40}}
STATUS = {"models": [{"stores": [{"id": "A", "name": "Alpha"}, {"id": "B", "name": "Beta"}, {"id": "C", "name": "Gamma"}]}]}


def ev(sid, hours_ago, color="Night Sky"):
    return {"kind": "in", "store_id": sid, "store": sid, "color": color,
            "t": (NOW - timedelta(hours=hours_ago)).isoformat()}


class ForecastTests(unittest.TestCase):
    def test_probabilities_are_proper(self):
        f = fc.forecast(STATUS, {"events": [ev("A", 5), ev("B", 30)]}, PRIORS, NOW)
        self.assertAlmostEqual(sum(f["hours_et"]), 1, places=3)
        self.assertAlmostEqual(sum(f["colours"].values()), 1, places=3)
        self.assertTrue(all(0 <= h["p"] <= 1 for h in f["next_hours"]))
        self.assertLessEqual(sum(h["p"] for h in f["next_hours"]), 1.0001)

    def test_recent_busy_store_ranks_first(self):
        f = fc.forecast(STATUS, {"events": [ev("A", 2), ev("A", 26), ev("C", 200)]}, PRIORS, NOW)
        self.assertEqual(f["stores"][0]["name"], "Alpha")

    def test_window_follows_national_profile_with_little_data(self):
        f = fc.forecast(STATUS, {"events": [ev("A", 1)]}, PRIORS, NOW)
        self.assertIn(f["window"]["start_et"], range(8, 11))

    def test_flapping_counts_once(self):
        e = [ev("A", 1.0), ev("A", 0.95), ev("A", 0.9)]
        self.assertEqual(len(fc.restock_events({"events": e}, {}, {})), 1)

    def test_backtest_only_uses_the_past(self):
        e = [ev("A", h) for h in (90, 70, 50, 30, 10)]
        b = fc.forecast(STATUS, {"events": e}, PRIORS, NOW)["backtest"]
        self.assertEqual(b["n"], 2)
        self.assertEqual(b["hits"]["top3_store"], 2)   # A keeps restocking: should be predicted

    def test_ignores_unwatched_stores_and_models(self):
        hist = {"events": [ev("A", 5), ev("NY1", 4), dict(ev("B", 3), part="BIG512")]}
        status = {"models": [dict(STATUS["models"][0], part="P256")]}
        got = fc.restock_events(hist, {}, {"Alpha": "A", "Beta": "B"}, {"P256"})
        self.assertEqual([e["store_id"] for e in got], ["A"])
        self.assertEqual(fc.forecast(status, hist, PRIORS, NOW)["local_restocks"], 1)

    def test_no_zip_or_secrets(self):
        f = fc.forecast(STATUS, {"events": [ev("A", 3)]}, PRIORS, NOW)
        self.assertNotRegex(json.dumps(f), r"\b0\d{4}\b")


if __name__ == "__main__":
    unittest.main()
