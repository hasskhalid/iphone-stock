"""Offline tests for the daily plan (tools/update_plan.py) and weighted zone schedule."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "checker"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import check_stock as cs  # noqa: E402
import update_plan as up  # noqa: E402

NOW = datetime(2026, 10, 27, 11, 47, tzinfo=timezone.utc)


def status(zones):
    return {"zones": {k: {"models": [{"stores": [{"id": i} for i in ids]}]} for k, ids in zones.items()}}


def restock(sid, hours_ago, store="S"):
    return {"kind": "in", "store_id": sid, "store": store, "t": (NOW - timedelta(hours=hours_ago)).isoformat()}


class WeightedScheduleTests(unittest.TestCase):
    def test_weights_interleave_in_proportion(self):
        o = cs.zone_schedule(3, [6, 4, 1])
        self.assertEqual(len(o), 11)
        self.assertEqual([o.count(z) for z in (0, 1, 2)], [6, 4, 1])
        self.assertNotIn((2, 2), list(zip(o, o[1:])))

    def test_bad_or_missing_weights_fall_back(self):
        self.assertEqual(cs.zone_schedule(3, None), cs.zone_schedule(3))
        self.assertEqual(cs.zone_schedule(3, [1, 2]), cs.zone_schedule(3))      # wrong length (zip added/removed)
        self.assertEqual(cs.zone_schedule(3, [0, 2, 1]), cs.zone_schedule(3))   # out of range


class PlanTests(unittest.TestCase):
    ZONES = {"z0": ["A", "B"], "z1": ["B", "G"], "z2": ["C"]}

    def test_not_enough_data_keeps_default(self):
        p = up.plan(status(self.ZONES), {"events": [restock("A", 2)]}, NOW)
        self.assertIsNone(p["zone_weights"])

    def test_busy_home_but_near_zone_never_starved(self):
        ev = [restock("A", h) for h in (2, 5, 9, 30)]
        p = up.plan(status(self.ZONES), {"events": ev}, NOW)
        w = p["zone_weights"]
        self.assertGreaterEqual(w[1], up.NEAR_MIN_WEIGHT)
        self.assertLessEqual(w[0], w[1] + 2)
        self.assertEqual(w[2], 1)

    def test_shared_store_split_and_old_events_ignored(self):
        ev = [restock("B", 1), restock("G", 2), restock("G", 3), restock("C", 24 * 30)]
        p = up.plan(status(self.ZONES), {"events": ev}, NOW)
        self.assertEqual(p["restocks"], 3)
        self.assertEqual(p["zones"], {"z0": 0.5, "z1": 2.5, "z2": 0.0})
        self.assertEqual(p["zone_weights"][1], up.MAX_WEIGHT)

    def test_no_zip_in_plan(self):
        p = up.plan(status(self.ZONES), {"events": [restock("A", 1)] * 3}, NOW)
        self.assertNotRegex(str(p), r"\b0\d{4}\b")


if __name__ == "__main__":
    unittest.main()
