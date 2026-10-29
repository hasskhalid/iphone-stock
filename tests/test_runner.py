"""Offline tests for local_runner.py and the PC/GitHub handoff."""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "checker"))
import check_stock as cs  # noqa: E402
import local_runner as lr  # noqa: E402

BASE = {
    "radius_miles": 25, "max_stores": 12,
    "models": [
        {"label": "Duo Night Sky", "color": "Night Sky", "part": "A1/A", "url": "https://x/night"},
        {"label": "Duo Star White", "color": "Star White", "part": "B2/A", "url": "https://x/white"},
    ],
}


def status_with(avail_parts=(), error=None):
    return {
        "error": error,
        "models": [
            {"part": m["part"], "label": m["label"], "color": m["color"], "url": m["url"],
             "stores": [{"id": "R1", "name": "Menlo Park", "distance": "4 mi",
                         "available": m["part"] in avail_parts}]}
            for m in BASE["models"]
        ],
    }


class ConfigTests(unittest.TestCase):
    def test_local_overrides_and_defaults(self):
        cfg = lr.merge_config(BASE, {"zip": "12345", "ntfy_topic": "t", "poll_seconds": 20})
        self.assertEqual(cfg["zip"], "12345")
        self.assertEqual(cfg["poll_seconds"], 20)   # raw value; main() applies MIN_POLL
        self.assertEqual(cfg["heartbeat_seconds"], 60)
        self.assertEqual(len(cfg["models"]), 2)

    def test_local_models_keep_repo_urls(self):
        cfg = lr.merge_config(BASE, {"models": [{"label": "Mine", "part": "A1/A"}]})
        self.assertEqual(cfg["models"][0], {"label": "Mine", "color": "Night Sky", "part": "A1/A", "url": "https://x/night"})
        self.assertEqual(len(cfg["models"]), len(BASE["models"]))   # repo models are never dropped

    def test_local_only_models_are_added(self):
        cfg = lr.merge_config(BASE, {"models": [{"label": "Extra", "part": "Z9/A"}]})
        self.assertEqual([m["part"] for m in cfg["models"]][-1], "Z9/A")
        self.assertEqual(len(cfg["models"]), len(BASE["models"]) + 1)

    def test_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, ".env.local")
            with open(p, "w") as f:
                f.write("# comment\nGITHUB_TOKEN=abc123\nGITHUB_REPO='me/repo'\n")
            self.assertEqual(lr.load_env(p), {"GITHUB_TOKEN": "abc123", "GITHUB_REPO": "me/repo"})

    def test_mask_hides_secret(self):
        self.assertNotIn("secret-topic-name", lr.mask("secret-topic-name"))


class NagTests(unittest.TestCase):
    def test_nags_on_interval_until_sold_out(self):
        n = lr.Nagger(minutes=2, max_count=3)
        hot = status_with({"A1/A"})
        self.assertIsNone(n.update(0, hot, alerted=True))      # first alert already sent
        self.assertIsNone(n.update(60, hot, alerted=False))    # too soon
        self.assertTrue(n.update(121, hot, alerted=False))     # nag 1
        self.assertIsNone(n.update(150, hot, alerted=False))
        self.assertTrue(n.update(242, hot, alerted=False))     # nag 2
        self.assertIsNone(n.update(300, status_with(), alerted=False))  # sold out resets
        self.assertIsNone(n.update(500, hot, alerted=True))

    def test_respects_max(self):
        n = lr.Nagger(minutes=1, max_count=1)
        hot = status_with({"B2/A"})
        n.update(0, hot, alerted=True)
        self.assertTrue(n.update(61, hot, False))
        self.assertIsNone(n.update(200, hot, False))

    def test_disabled(self):
        n = lr.Nagger(minutes=0, max_count=5)
        hot = status_with({"A1/A"})
        n.update(0, hot, True)
        self.assertIsNone(n.update(999, hot, False))

    def test_spoken_text_reads_zip_digits(self):
        t = lr.spoken_text([("NIGHT SKY @ Grand Central", "Grand Central · ZIP 10017\nBag → …", "u")])
        self.assertEqual(t, "iPhone Duo in stock. Night Sky at Grand Central, zip 1 0 0 1 7.")

    def test_nag_message_links_to_color(self):
        title, msg, click = lr.nag_message(lr.in_stock_rows(status_with({"B2/A"})))
        self.assertIn("STAR WHITE", title)
        self.assertEqual(click, "https://x/white")
        self.assertIn("Menlo Park", msg)


class PublisherTests(unittest.TestCase):
    def test_publish_sequence(self):
        calls = []

        def fake(method, path, body=None):
            calls.append((method, path, body))
            if path.endswith("/git/trees"):
                return 201, {"sha": "tree1"}
            if path.endswith("/git/commits"):
                return 201, {"sha": "commit1"}
            return 200, {"ref": "refs/heads/status"}

        sha = lr.GitHubPublisher("tok", "me/repo", request=fake).publish({"status.json": "{}"}, "msg")
        self.assertEqual(sha, "commit1")
        self.assertEqual([c[0] for c in calls], ["POST", "POST", "PATCH"])
        self.assertEqual(calls[1][2]["parents"], [])
        self.assertTrue(calls[2][2]["force"])

    def test_creates_branch_if_missing(self):
        def fake(method, path, body=None):
            if path.endswith("/git/trees"):
                return 201, {"sha": "t"}
            if path.endswith("/git/commits"):
                return 201, {"sha": "c"}
            if method == "PATCH":
                return 422, {"message": "Reference does not exist"}
            return 201, {}
        self.assertEqual(lr.GitHubPublisher("tok", "me/repo", request=fake).publish({"a": "b"}, "m"), "c")

    def test_raises_on_auth_failure(self):
        pub = lr.GitHubPublisher("bad", "me/repo", request=lambda *a, **k: (401, {"message": "Bad credentials"}))
        with self.assertRaises(RuntimeError):
            pub.publish({"a": "b"}, "m")


class HandoffTests(unittest.TestCase):
    def run_checker(self, prev):
        with tempfile.TemporaryDirectory() as tmp:
            prev_p, out = os.path.join(tmp, "prev.json"), os.path.join(tmp, "status.json")
            cfg = os.path.join(tmp, "config.json")
            with open(prev_p, "w") as f:
                json.dump(prev, f)
            with open(cfg, "w") as f:
                json.dump(BASE, f)
            fx = os.path.join(ROOT, "tests", "fixtures", "live_sample_nyc.json")
            cs.main(["--config", cfg, "--prev", prev_p, "--out", out, "--fixture", fx,
                     "--defer-to-pc", "180", "--source", "github"])
            return os.path.exists(out)

    def test_github_skips_while_pc_is_live(self):
        fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.assertFalse(self.run_checker({"source": "pc", "checked_at": fresh}))

    def test_github_runs_when_pc_is_stale(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        self.assertTrue(self.run_checker({"source": "pc", "checked_at": old}))

    def test_github_takes_over_when_pc_is_erroring(self):
        fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.assertTrue(self.run_checker({"source": "pc", "checked_at": fresh, "error": "HTTP Error 541"}))

    def test_poll_floor_protects_against_rate_limits(self):
        self.assertGreaterEqual(lr.MIN_POLL, 45)
        self.assertGreaterEqual(lr.merge_config(BASE, {})["poll_seconds"], lr.MIN_POLL)

    def test_pace_starts_safe_and_walks_down_to_45(self):
        p = lr.Pace(45)
        self.assertEqual(p.rungs, [45, 60, 75, 90, 120])
        self.assertEqual(p.poll, lr.START_PACE)
        self.assertEqual(p.poll, 60)
        self.assertFalse(p.ok(lr.COOLDOWN_START - 100))
        self.assertTrue(p.ok(100))                       # a clean cooldown -> faster
        self.assertEqual(p.poll, 45)
        self.assertFalse(p.ok(10_000))                   # can't go below the setting

    def test_pace_backs_off_and_waits_longer_each_block(self):
        p = lr.Pace(45)
        p.idx = 0
        p.blocked()
        self.assertEqual((p.poll, p.cooldown), (60, 2 * lr.COOLDOWN_START))
        self.assertFalse(p.ok(lr.COOLDOWN_START))        # cooldown doubled
        self.assertTrue(p.ok(lr.COOLDOWN_START))
        self.assertEqual(p.poll, 45)
        p.blocked(); p.blocked(); p.blocked(); p.blocked(); p.blocked(); p.blocked()
        self.assertEqual(p.poll, 120)                    # top of the ladder
        self.assertEqual(p.cooldown, lr.COOLDOWN_MAX)

    def test_pace_resumes_or_resets_with_saved_state(self):
        p = lr.Pace(45); p.blocked()
        again = lr.Pace(45, {"pace": p.to_state()})
        self.assertEqual((again.poll, again.cooldown), (p.poll, p.cooldown))
        self.assertEqual(lr.Pace(75, {"pace": p.to_state()}).poll, 75)   # setting changed: start fresh
        self.assertEqual(lr.Pace(90, {"poll_seconds": 120}).poll, 90)    # old state file ignored
        self.assertEqual(lr.Pace(10).rungs[0], lr.MIN_POLL)

    def test_pick_poll_rush_and_steady(self):
        at = lambda h, m: time.struct_time((2026, 10, 27, h, m, 0, 1, 300, 1))
        # Option C (Oct 28): one 60s window 9:00-11:30, 120s overnight, 75s otherwise.
        self.assertEqual(lr.pick_poll(at(9, 0)), lr.PEAK_POLL)
        self.assertEqual(lr.pick_poll(at(11, 29)), lr.PEAK_POLL)
        self.assertEqual(lr.pick_poll(at(11, 30)), lr.STEADY_POLL)    # window ends at 11:30
        self.assertEqual(lr.pick_poll(at(8, 45)), lr.STEADY_POLL)     # 75s before 9:00
        self.assertEqual(lr.pick_poll(at(13, 0)), lr.STEADY_POLL)     # no afternoon window
        self.assertEqual(lr.pick_poll(at(2, 0)), 120)                 # overnight 120s
        self.assertEqual(lr.pick_poll(at(23, 30)), 120)
        self.assertEqual(lr.pick_poll(at(2, 0), night_slow=True), 120)  # a night block never speeds it up
        self.assertEqual(lr.pick_poll(at(7, 0)), lr.STEADY_POLL)      # 75s from 7 AM
        self.assertEqual(lr.pick_poll(at(22, 59)), lr.STEADY_POLL)

    def test_blocks_are_remembered_for_the_day_only(self):
        at = lambda d, h: time.struct_time((2026, 10, d, h, 0, 0, 1, 300, 1))
        self.assertEqual(lr.pick_poll(at(27, 9), no_peak_day="2026-10-27"), lr.STEADY_POLL)
        self.assertEqual(lr.pick_poll(at(27, 9), "2026-10-27", slow_day="2026-10-27"), lr.SLOW_POLL)
        self.assertEqual(lr.pick_poll(at(28, 9), "2026-10-27", "2026-10-27"), lr.PEAK_POLL)  # next day: fresh

    def test_close_block_records_range(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl"); tmp.close()
        orig, lr.BLOCK_LOG = lr.BLOCK_LOG, tmp.name
        self.addCleanup(lambda: (setattr(lr, "BLOCK_LOG", orig), os.remove(tmp.name)))
        b = lr.close_block({"start": "2026-10-27T22:54:25+00:00", "last_fail": "2026-10-27T23:19:31+00:00", "pace": 75},
                           "2026-10-27T23:24:22+00:00")
        self.assertEqual((b["at_least_minutes"], b["minutes"]), (25, 30))

    def test_flip_alternates_60_and_75(self):
        self.assertEqual([lr.flip_poll(n) for n in range(4)], [60, 75, 60, 75])

    def test_block_waits_long_once_then_probes_lightly(self):
        self.assertEqual([lr.block_wait(n) for n in (1, 2, 3, 4)], [900, 300, 300, 300])

    def test_github_runs_after_its_own_check(self):
        fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.assertTrue(self.run_checker({"source": "github", "checked_at": fresh}))

    def test_watchdog_warns_once_when_pc_goes_silent(self):
        sent = []
        orig = cs.send_ntfy
        cs.send_ntfy = lambda topic, title, *a, **k: sent.append(title)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds")
                prev_p, out, cfg = (os.path.join(tmp, n) for n in ("prev.json", "status.json", "config.json"))
                with open(prev_p, "w") as f:
                    json.dump({"source": "pc", "checked_at": old}, f)
                with open(cfg, "w") as f:
                    json.dump(dict(BASE, ntfy_topic="t"), f)
                fx = os.path.join(ROOT, "tests", "fixtures", "live_sample_nyc.json")
                args = ["--config", cfg, "--prev", prev_p, "--out", out, "--fixture", fx, "--source", "github", "--defer-to-pc", "300"]
                cs.main(args)
                self.assertTrue(any("stopped checking" in t for t in sent))
                # next run: last publisher is now github -> no repeat warning
                os.replace(out, prev_p)
                sent.clear()
                cs.main(args)
                self.assertFalse(any("stopped checking" in t for t in sent))
        finally:
            cs.send_ntfy = orig

    def test_watchdog_survives_backup_overwrite(self):
        """App-forced backup check overwrote the PC status; PC then dies -> still warned once."""
        sent = []
        orig = cs.send_ntfy
        cs.send_ntfy = lambda topic, title, *a, **k: sent.append(title)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds")
                prev = {"source": "github", "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "models": [], "pc": {"checked_at": old, "error": None, "warned": False}}
                prev_p, out, cfg = (os.path.join(tmp, n) for n in ("prev.json", "status.json", "config.json"))
                with open(prev_p, "w") as f:
                    json.dump(prev, f)
                with open(cfg, "w") as f:
                    json.dump(dict(BASE, ntfy_topic="t"), f)
                fx = os.path.join(ROOT, "tests", "fixtures", "live_sample_nyc.json")
                args = ["--config", cfg, "--prev", prev_p, "--out", out, "--fixture", fx, "--source", "github", "--defer-to-pc", "300"]
                cs.main(args)
                self.assertEqual(sum("stopped checking" in t for t in sent), 1)
                with open(out) as f:
                    self.assertTrue(json.load(f)["pc"]["warned"])
                os.replace(out, prev_p)
                sent.clear()
                cs.main(args)
                self.assertFalse(any("stopped checking" in t for t in sent))
        finally:
            cs.send_ntfy = orig

    def test_merge_pc_heartbeat_keeps_backup_data(self):
        remote = {"source": "github", "checked_at": "T2", "models": [{"part": "A"}]}
        merged = lr.merge_pc_heartbeat(remote, {"checked_at": "T3", "error": "541"})
        self.assertEqual(merged["source"], "github")
        self.assertEqual(merged["models"], [{"part": "A"}])
        self.assertEqual(merged["pc"]["error"], "541")
        self.assertIsNone(lr.merge_pc_heartbeat({"source": "pc", "models": [1]}, {}))
        self.assertIsNone(lr.merge_pc_heartbeat(None, {}))

    def test_seconds_since_handles_z_suffix(self):
        self.assertLess(cs.seconds_since(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")), 5)


class SignatureTests(unittest.TestCase):
    def test_signature_changes_with_stock(self):
        self.assertNotEqual(lr.availability_signature(status_with()), lr.availability_signature(status_with({"A1/A"})))
        self.assertEqual(lr.availability_signature(status_with()), lr.availability_signature(status_with()))


if __name__ == "__main__":
    unittest.main()


class BlockInfoTests(unittest.TestCase):
    def test_block_info(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(lr.block_info({"error": None}, None, 0, now), {})
        first = lr.block_info({"error": "HTTP 541", "checked_at": "2026-10-28T12:00:00+00:00"}, None, 0, now)
        self.assertEqual(first["block_since"], "2026-10-28T12:00:00+00:00")
        self.assertEqual(first["retry_at"], "2026-10-28T12:15:00+00:00")       # 15 min before the first retry
        later = lr.block_info({"error": "HTTP 541"}, {"start": "2026-10-28T11:40:00+00:00"}, 3, now)
        self.assertEqual(later["block_since"], "2026-10-28T11:40:00+00:00")
        self.assertEqual(later["retry_at"], "2026-10-28T12:05:00+00:00")       # then every 5 min


class ResumeBlockTests(unittest.TestCase):
    def test_restart_mid_block_continues_it(self):
        self.assertEqual(lr.resume_errors(None), 0)
        n = lr.resume_errors({"start": "2026-10-28T12:53:07+00:00"})
        self.assertEqual(n, 1)
        self.assertEqual(lr.block_wait(n + 1), lr.BLOCK_PROBE)     # next failure waits 5 min, not 15


class PushDayTests(unittest.TestCase):
    def test_push_day_is_60s_all_day_until_blocked(self):
        at = lambda h, m: time.strptime(f"2026-10-28 {h:02d}:{m:02d}", "%Y-%m-%d %H:%M")
        self.assertEqual(lr.pick_poll(at(15, 0), push_day="2026-10-28"), lr.PEAK_POLL)        # afternoon, normally 75
        self.assertEqual(lr.pick_poll(at(23, 30), push_day="2026-10-28"), lr.QUIET_POLL)      # night stays at the night pace
        self.assertEqual(lr.pick_poll(at(15, 0), "2026-10-28", None, "2026-10-28"), lr.STEADY_POLL)  # blocked -> 75
        self.assertEqual(lr.pick_poll(at(15, 0), push_day="2026-10-27"), lr.STEADY_POLL)      # only that day


class NightBlockTests(unittest.TestCase):
    def test_night_end_is_next_7am(self):
        start = time.mktime((2026, 10, 28, 23, 30, 0, 0, 0, -1))
        self.assertEqual(time.localtime(lr.night_end(start))[:5], (2026, 10, 29, 7, 0))
        early = time.mktime((2026, 10, 29, 2, 0, 0, 0, 0, -1))
        self.assertEqual(time.localtime(lr.night_end(early))[:5], (2026, 10, 29, 7, 0))

    def test_night_slow_does_not_touch_daytime(self):
        at = lambda d, h: time.strptime(f"2026-10-{d} {h:02d}:00", "%Y-%m-%d %H:%M")
        self.assertEqual(lr.pick_poll(at(29, 9), night_slow=True), lr.PEAK_POLL)   # 60s window still on


class LocalDayTests(unittest.TestCase):
    def test_local_day_uses_pc_time_zone(self):
        os.environ["TZ"] = "America/New_York"; time.tzset()
        try:
            self.assertEqual(lr.local_day("2026-10-29T01:30:00+00:00"), "2026-10-28")   # 9:30 PM Eastern
            self.assertEqual(lr.local_day("bad"), "")
        finally:
            os.environ.pop("TZ", None); time.tzset()


class RestartFlagTests(unittest.TestCase):
    def test_restart_flag_is_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(lr.restart_requested(tmp))
            open(os.path.join(tmp, lr.RESTART_FILE), "w").close()
            self.assertTrue(lr.restart_requested(tmp))
            self.assertFalse(lr.restart_requested(tmp))      # one restart per request
