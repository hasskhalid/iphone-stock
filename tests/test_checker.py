"""Offline tests: run with  python -m unittest discover tests"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "checker"))
import check_stock as cs  # noqa: E402

MODELS = [
    {"label": "Test Phone Black", "part": "TEST1LL/A"},
    {"label": "Test Phone White", "part": "TEST2LL/A"},
]


def store(num, name, city, dist, a1, a2):
    def avail(flag):
        return {
            "pickupDisplay": "available" if flag else "unavailable",
            "pickupSearchQuote": "Available Today" if flag else "Currently unavailable",
        }
    return {
        "storeNumber": num, "storeName": name, "city": city, "state": "NJ",
        "storedistance": float(dist.split()[0]), "storeDistanceWithUnit": dist,
        "partsAvailability": {"TEST1LL/A": avail(a1), "TEST2LL/A": avail(a2)},
    }


def response(*stores):
    # Current shape of /shop/retail/pickup-message (verified live Oct 2026)
    return {"head": {"status": "200"}, "body": {"stores": list(stores)}}


def legacy_response(*stores):
    # Older /shop/fulfillment-messages shape
    return {"body": {"content": {"pickupMessage": {"stores": list(stores)}}}}


SOLD_OUT = response(
    store("R001", "Market Street", "Springfield", "4.2 mi", False, False),
    store("R002", "Riverside Mall", "Shelbyville", "11.0 mi", False, False),
)
RESTOCK = response(
    store("R001", "Market Street", "Springfield", "4.2 mi", True, False),
    store("R002", "Riverside Mall", "Shelbyville", "11.0 mi", False, False),
)


class ParseTests(unittest.TestCase):
    def test_parse_marks_availability(self):
        models = cs.parse_response(RESTOCK, MODELS, max_stores=8)
        self.assertTrue(models[0]["any_available"])
        self.assertFalse(models[1]["any_available"])
        self.assertEqual(models[0]["stores"][0]["quote"], "Available Today")

    def test_max_stores_limits_rows(self):
        models = cs.parse_response(RESTOCK, MODELS, max_stores=1)
        self.assertEqual(len(models[0]["stores"]), 1)

    def test_radius_filters_far_stores(self):
        models = cs.parse_response(RESTOCK, MODELS, max_stores=12, radius_miles=10)
        self.assertEqual([st["id"] for st in models[0]["stores"]], ["R001"])

    def test_legacy_shape_still_parses(self):
        data = legacy_response(store("R001", "Market Street", "Springfield", "4.2 mi", True, False))
        self.assertTrue(cs.parse_response(data, MODELS, 8)[0]["any_available"])

    def test_city_includes_state(self):
        self.assertEqual(cs.parse_response(RESTOCK, MODELS, 8)[0]["stores"][0]["city"], "Springfield, NJ")

    def test_apple_error_raises(self):
        bad = {"body": {"content": {"pickupMessage": {"errorMessage": "Invalid part"}}}}
        with self.assertRaises(RuntimeError):
            cs.parse_response(bad, MODELS, 8)

    def test_url_contains_parts_and_zip(self):
        url = cs.build_url(["A1/A", "B2/A"], "12345")
        self.assertIn("parts.0=A1%2FA", url)
        self.assertIn("parts.1=B2%2FA", url)
        self.assertIn("location=12345", url)
        self.assertIn("/shop/retail/pickup-message?", url)


class AlertTests(unittest.TestCase):
    def test_alert_only_on_transition(self):
        prev = {"models": cs.parse_response(SOLD_OUT, MODELS, 8)}
        now = cs.parse_response(RESTOCK, MODELS, 8)
        fresh = cs.new_availability(prev, now)
        self.assertEqual(list(fresh), ["TEST1LL/A"])
        self.assertEqual(fresh["TEST1LL/A"][0]["id"], "R001")

    def test_no_repeat_alert_when_still_available(self):
        prev = {"models": cs.parse_response(RESTOCK, MODELS, 8)}
        now = cs.parse_response(RESTOCK, MODELS, 8)
        self.assertEqual(cs.new_availability(prev, now), {})

    def test_alert_text(self):
        prev = {"models": cs.parse_response(SOLD_OUT, MODELS, 8)}
        now = cs.parse_response(RESTOCK, MODELS, 8)
        alerts = cs.build_alerts(cs.new_availability(prev, now), now)
        self.assertEqual(alerts[0][0], "TEST PHONE BLACK @ Market Street")
        self.assertIn("Market Street", alerts[0][1])

    def test_alert_clicks_through_to_model_url(self):
        models = [dict(MODELS[0], url="https://example.com/black"), MODELS[1]]
        prev = {"models": cs.parse_response(SOLD_OUT, models, 8)}
        now = cs.parse_response(RESTOCK, models, 8)
        alerts = cs.build_alerts(cs.new_availability(prev, now), now)
        self.assertEqual(alerts[0][2], "https://example.com/black")


class ActionTests(unittest.TestCase):
    def test_actions_app_first_then_web_bag(self):
        a = cs.alert_actions("https://example.com/black")
        self.assertTrue(a.startswith("view, Apple Store app, https://example.com/black"))
        self.assertIn("Bag in Safari, https://www.apple.com/shop/bag", a)

    def test_default_click_opens_app(self):
        self.assertEqual(cs.load_settings({})["alert_click"], "product")
        self.assertEqual(cs.load_settings({"alert_click": "bag"})["alert_click"], "bag")


class AlertFormatTests(unittest.TestCase):
    def test_title_is_color_at_store_and_body_leads_with_zip(self):
        models = [{"label": "iPhone Duo 256GB Night Sky", "color": "Night Sky", "part": "P1", "url": "u"}]
        st = {"id": "R1", "name": "Grand Central", "zip": "10017", "city": "New York, NY", "available": True}
        title, body, url = cs.build_alerts({"P1": [st]}, models)[0]
        self.assertEqual(title, "256GB NIGHT SKY @ Grand Central")
        self.assertTrue(body.startswith("Grand Central · ZIP 10017"))
        self.assertIn("type ZIP 10017", body)
        self.assertNotIn(" mi", body)

    def test_zip_parsed_from_apple_address(self):
        st = store("R001", "Market Street", "Springfield", "4.2 mi", True, False)
        st["address"] = {"address2": "1 Main St", "postalCode": "07078-1234"}
        self.assertEqual(cs.parse_response(response(st), MODELS, 8)[0]["stores"][0]["zip"], "07078")


class ZoneTests(unittest.TestCase):
    def settings(self):
        return {"models": MODELS, "zip": "11111", "extra_zips": ["22222"], "max_stores": 12, "radius": 25,
                "topic": "", "server": "s"}

    def test_rotates_and_merges_without_false_alerts(self):
        far = response(store("R900", "Far Mall", "Paramus", "3.0 mi", False, False))
        near = SOLD_OUT
        with tempfile.TemporaryDirectory() as tmp:
            fn, ff = os.path.join(tmp, "n.json"), os.path.join(tmp, "f.json")
            json.dump(near, open(fn, "w")); json.dump(far, open(ff, "w"))
            st1, _, a1 = cs.check_once(self.settings(), {}, {"events": []}, fixture=fn, log=lambda m: None)
            self.assertEqual(st1["next_zone"], 1)
            st2, _, a2 = cs.check_once(self.settings(), st1, {"events": []}, fixture=ff, log=lambda m: None)
            ids = [s["id"] for s in st2["models"][0]["stores"]]
            self.assertEqual(set(ids), {"R001", "R002", "R900"})          # both zones merged
            self.assertEqual(st2["next_zone"], 0)
            self.assertEqual(a1 + a2, [])
            self.assertNotIn("11111", json.dumps(st2))                     # ZIPs never in public status
            self.assertNotIn("22222", json.dumps(st2))

    def test_restock_in_second_zone_alerts(self):
        far_sold = response(store("R900", "Far Mall", "Paramus", "3.0 mi", False, False))
        far_hot = response(store("R900", "Far Mall", "Paramus", "3.0 mi", True, False))
        with tempfile.TemporaryDirectory() as tmp:
            p = lambda n, d: (json.dump(d, open(os.path.join(tmp, n), "w")), os.path.join(tmp, n))[1]
            st, _, _ = cs.check_once(self.settings(), {}, {"events": []}, fixture=p("a", SOLD_OUT), log=lambda m: None)
            st, _, _ = cs.check_once(self.settings(), st, {"events": []}, fixture=p("b", far_sold), log=lambda m: None)
            st, _, _ = cs.check_once(self.settings(), st, {"events": []}, fixture=p("c", SOLD_OUT), log=lambda m: None)
            st, _, alerts = cs.check_once(self.settings(), st, {"events": []}, fixture=p("d", far_hot), log=lambda m: None)
            self.assertEqual(len(alerts), 1)
            self.assertIn("Far Mall", alerts[0][0])


class AreaTests(unittest.TestCase):
    def test_schedule_checks_home_every_other_time(self):
        self.assertEqual(cs.zone_schedule(1), [0])
        self.assertEqual(cs.zone_schedule(2), [0, 1])
        self.assertEqual(cs.zone_schedule(3), [0, 1, 0, 1, 0, 1, 0, 2])   # near zone as often as home

    def test_keeps_all_nj_and_nearby_others(self):
        rows = [{"id": "a", "state": "NJ", "miles": 60, "available": False},
                {"id": "b", "state": "NY", "miles": 18, "available": True},
                {"id": "c", "state": "NY", "miles": 34, "available": True},
                {"id": "d", "state": "CT", "miles": None, "available": False}]
        out = cs.filter_area([{"part": "P", "stores": rows}], 25, ["NJ"])[0]
        self.assertEqual([r["id"] for r in out["stores"]], ["a", "b", "d"])
        self.assertTrue(out["any_available"])

    def test_extra_zone_not_cut_by_radius_and_third_zone_waits_its_turn(self):
        settings = {"models": MODELS, "zip": "11111", "extra_zips": ["22222", "33333"], "max_stores": 12,
                    "radius": 25, "keep_states": ["NJ"], "topic": "", "server": "s"}
        with tempfile.TemporaryDirectory() as tmp:
            fn = os.path.join(tmp, "n.json"); json.dump(SOLD_OUT, open(fn, "w"))
            st, seen = {}, []
            for _ in range(8):
                st, _, _ = cs.check_once(settings, st, {"events": []}, fixture=fn, log=lambda m: None)
                seen.append(sorted(st["zones"]))
            self.assertEqual(seen[-1], ["z0", "z1", "z2"])
            self.assertNotIn("33333", json.dumps(st))


class RushTests(unittest.TestCase):
    """A restock in one zone (American Dream) sends the next check to the other zones
    (where Garden State Plaza also restocked) instead of waiting for their usual turn."""
    def test_restock_triggers_immediate_check_of_other_zones(self):
        settings = {"models": MODELS, "zip": "11111", "extra_zips": ["22222", "33333"], "max_stores": 12,
                    "radius": 25, "keep_states": [], "topic": "", "server": "s"}
        quiet = response(store("R001", "Home Mall", "Edison", "3.0 mi", False, False))
        hot_home = response(store("R001", "Home Mall", "Edison", "3.0 mi", True, False))
        north_hot = response(store("R117", "Garden State Plaza", "Paramus", "2.0 mi", True, False))
        calls = []
        orig = cs.fetch_apple
        cs.fetch_apple = lambda parts, z: (calls.append(z), {"11111": hot_home, "22222": north_hot, "33333": quiet}[z])[1]
        try:
            st = {}
            st, _, _ = cs.check_once(settings, st, {"events": []}, log=lambda m: None)   # home: restock found
            self.assertEqual(st["rush_zones"], [1, 2])
            st, _, a2 = cs.check_once(settings, st, {"events": []}, log=lambda m: None)  # straight to zone 1
            st, _, _ = cs.check_once(settings, st, {"events": []}, log=lambda m: None)   # then zone 2
            st, _, _ = cs.check_once(settings, st, {"events": []}, log=lambda m: None)   # back to the schedule (home)
        finally:
            cs.fetch_apple = orig
        self.assertEqual(calls, ["11111", "22222", "33333", "11111"])   # rush, then home again
        self.assertTrue(any("Garden State Plaza" in t for t, _, _ in a2))
        self.assertNotIn("rush_zones", st)
        self.assertNotIn("22222", json.dumps(st))


class DispatchTests(unittest.TestCase):
    def test_all_channels_fire(self):
        calls = []
        orig = (cs.send_ntfy, cs.send_telegram, cs.send_pushover)
        cs.send_ntfy = lambda *a, **k: calls.append("ntfy")
        cs.send_telegram = lambda *a, **k: calls.append("telegram")
        cs.send_pushover = lambda *a, **k: calls.append("pushover")
        try:
            settings = {"topic": "t", "server": "s", "telegram_token": "x", "telegram_chat": "1",
                        "pushover_token": "p", "pushover_user": "u"}
            errs = cs.dispatch_alert(settings, "T", "M", "https://x")
            self.assertEqual(errs, [])
            self.assertEqual(sorted(calls), ["ntfy", "pushover", "telegram"])
        finally:
            cs.send_ntfy, cs.send_telegram, cs.send_pushover = orig

    def test_one_channel_failing_doesnt_block_others(self):
        calls = []
        orig = (cs.send_ntfy, cs.send_telegram)
        def boom(*a, **k): raise RuntimeError("down")
        cs.send_ntfy = boom
        cs.send_telegram = lambda *a, **k: calls.append("telegram")
        try:
            errs = cs.dispatch_alert({"topic": "t", "telegram_token": "x", "telegram_chat": "1"}, "T", "M", "u")
            self.assertEqual(calls, ["telegram"])
            self.assertTrue(errs and errs[0].startswith("ntfy"))
        finally:
            cs.send_ntfy, cs.send_telegram = orig

    def test_unconfigured_channels_skipped(self):
        s = cs.load_settings({})
        self.assertEqual((s["telegram_token"], s["pushover_token"]), ("", ""))


class HistoryTests(unittest.TestCase):
    def test_restock_then_sellout_events(self):
        sold = {"models": cs.parse_response(SOLD_OUT, MODELS, 8)}
        stocked = cs.parse_response(RESTOCK, MODELS, 8)
        ins = cs.change_events(sold, stocked, "T1")
        self.assertEqual([(e["kind"], e["store_id"], e["part"]) for e in ins], [("in", "R001", "TEST1LL/A")])
        outs = cs.change_events({"models": stocked}, sold["models"], "T2")
        self.assertEqual([(e["kind"], e["store"]) for e in outs], [("out", "Market Street")])

    def test_no_events_when_unchanged(self):
        sold = cs.parse_response(SOLD_OUT, MODELS, 8)
        self.assertEqual(cs.change_events({"models": sold}, sold, "T"), [])

    def test_store_details(self):
        st = store("R001", "Market Street", "Springfield", "4.2 mi", True, False)
        st.update({"address": {"address2": "1 Main St", "postalCode": "00001"},
                   "phoneNumber": "555-0100", "reservationUrl": "http://www.apple.com/retail/x"})
        row = cs.parse_response(response(st), MODELS, 8)[0]["stores"][0]
        self.assertEqual(row["address"], "1 Main St, Springfield, NJ 00001")
        self.assertEqual(row["phone"], "555-0100")
        self.assertTrue(row["store_url"].startswith("https://"))
        self.assertIn("google.com/maps", row["maps_url"])


class EndToEndTests(unittest.TestCase):
    def run_main(self, tmp, fixture, prev=None):
        cfg = os.path.join(tmp, "config.json")
        fx = os.path.join(tmp, "fixture.json")
        out = os.path.join(tmp, "status.json")
        with open(cfg, "w") as f:
            json.dump({"zip": "00000", "max_stores": 8, "models": MODELS}, f)
        with open(fx, "w") as f:
            json.dump(fixture, f)
        hist = os.path.join(tmp, "history.json")
        argv = ["--config", cfg, "--fixture", fx, "--out", out, "--dry-run",
                "--history-in", hist, "--history-out", hist]
        if prev:
            argv += ["--prev", prev]
        self.assertEqual(cs.main(argv), 0)
        with open(out) as f:
            return json.load(f), out

    def test_two_checks_produce_one_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, out1 = self.run_main(tmp, SOLD_OUT)
            self.assertIsNone(first["error"])
            prev = os.path.join(tmp, "prev.json")
            os.replace(out1, prev)
            second, _ = self.run_main(tmp, RESTOCK, prev=prev)
            self.assertTrue(second["models"][0]["any_available"])
            with open(os.path.join(tmp, "history.json")) as f:
                events = json.load(f)["events"]
            self.assertEqual([(e["kind"], e["store_id"]) for e in events], [("in", "R001")])

    def test_drill_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.json")
            with open(cfg, "w") as f:
                json.dump({"models": [dict(MODELS[0], url="https://example.com/black")]}, f)
            self.assertEqual(cs.main(["--config", cfg, "--drill", "--dry-run"]), 0)

    def test_test_alert_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.json")
            with open(cfg, "w") as f:
                json.dump({"models": MODELS}, f)
            self.assertEqual(cs.main(["--config", cfg, "--test-alert", "--dry-run"]), 0)

    def test_failure_keeps_last_good_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, out1 = self.run_main(tmp, RESTOCK)
            prev = os.path.join(tmp, "prev.json")
            os.replace(out1, prev)
            bad = {"body": {"content": {"pickupMessage": {"errorMessage": "Blocked"}}}}
            second, _ = self.run_main(tmp, bad, prev=prev)
            self.assertIn("Blocked", second["error"])
            self.assertEqual(second["models"], first["models"])
            self.assertEqual(second["last_success_at"], first["checked_at"])



class HomeDistanceTests(unittest.TestCase):
    """Stores found via an extra zone must be ranked by distance from HOME, not from that zone."""
    HOME = (40.7527, -73.9772)   # made-up home point for the test

    def row(self, sid, lat, lng, miles):
        return {"id": sid, "name": sid, "lat": lat, "lng": lng, "miles": miles, "distance": f"{miles} mi", "available": False}

    def test_extra_zone_store_gets_real_distance_from_home(self):
        pts = {"A": (40.7412, -74.0054), "B": (40.7637, -73.9730), "C": (40.6856, -73.9782), "D": (40.7251, -73.9990), "E": (40.5491, -74.3358)}
        z0 = [self.row(k, la, ln, round(cs.haversine_miles(*self.HOME, la, ln), 2)) for k, (la, ln) in pts.items()]
        far = self.row("FAR", 40.9176, -74.0751, 0.5)          # 0.5 mi from ITS zone, ~12 mi from home
        zones = {"z0": {"checked_at": "T1", "models": [{"part": "P", "stores": z0}]},
                 "z1": {"checked_at": "T2", "models": [{"part": "P", "stores": [far, dict(z0[0], miles=9.9)]}]}}
        rows = cs.merge_zones(zones, [{"part": "P", "label": "x"}])[0]["stores"]
        got = {r["id"]: r["miles"] for r in rows}
        self.assertAlmostEqual(got["FAR"], cs.haversine_miles(*self.HOME, 40.9176, -74.0751), delta=0.3)
        self.assertEqual(got["A"], z0[0]["miles"])            # home-zone distance wins over z1's
        self.assertEqual([r["id"] for r in rows], sorted(got, key=got.get))
        self.assertNotEqual(rows[0]["id"], "FAR")

    def test_single_zone_unchanged(self):
        z0 = [self.row("A", 1, 1, 3.0), self.row("B", 2, 2, 1.0)]
        rows = cs.merge_zones({"z0": {"checked_at": "T", "models": [{"part": "P", "stores": z0}]}}, [{"part": "P"}])[0]["stores"]
        self.assertEqual([r["id"] for r in rows], ["B", "A"])

if __name__ == "__main__":
    unittest.main()
