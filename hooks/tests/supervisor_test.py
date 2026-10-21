#!/usr/bin/env python3
"""Python-level regression suite for the supervisor skill's scripts (fleet.py, ledger.py).

Run: python3 -I hooks/tests/supervisor_test.py   (also run by supervisor-event.test.sh)

It tests the scripts of the tree it sits in: <root>/skills/supervisor/scripts, where <root> is two
directories above this file (the export package, or ~/.claude once installed).

Uses a throwaway HOME so the real supervisor state is never touched. Nothing here calls
happier or claude: inventory(), history() and subprocess are stubbed per test. Literal
expectations on purpose: asserting against the module's own constants could not fail.
"""
import contextlib
import importlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REAL = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp()
os.makedirs(os.path.join(HOME, ".claude"))
os.symlink(os.path.join(REAL, "skills"), os.path.join(HOME, ".claude", "skills"))
os.environ["HOME"] = HOME
os.environ.pop("HAPPIER_SESSION_ID", None)
SCRIPTS = os.path.join(REAL, "skills", "supervisor", "scripts")
sys.path.insert(0, SCRIPTS)
import ledger  # noqa: E402
import fleet  # noqa: E402

REAL_OWN_STATUS = fleet.own_status  # CompactSelf stubs it per test
REAL_INVENTORY = fleet.inventory  # Base stubs it for every test
REAL_OWN_CONTEXT = fleet.own_context_tokens  # Base stubs it too
REAL_POPEN = subprocess.Popen  # Doorbell stubs it for the doorbell spawn


class Base(unittest.TestCase):
    def setUp(self):
        global ledger, fleet
        shutil.rmtree(os.path.join(HOME, ".claude", "state"), ignore_errors=True)
        ledger = importlib.reload(ledger)
        fleet = importlib.reload(fleet)  # undoes every monkeypatch from the previous test
        os.makedirs(ledger.STATE_DIR)
        open(fleet.EVENTS, "w").close()
        fleet.inventory = lambda detail_local=True: ({}, [])
        fleet.POLL_BUDGET = 0  # tests use sub-second arms; Budgets re-enables it
        fleet.own_context_tokens = lambda: None  # no real `claude agents` call per wake

    def watch(self, hb=0.05, poll=0.01, backstop=0, limits=None):
        """One arm. hb is the arm's length (max_wait): with no wake it ends as rearm."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fleet.watch(poll=poll, backstop=backstop, max_wait=hb, limits=limits)
        return json.loads(buf.getvalue())

    def event(self, **e):
        with open(fleet.EVENTS, "a") as f:
            f.write(json.dumps(e) + "\n")

    def own(self, sid="own1"):
        ledger.append({"op": "add", "id": "W1", "title": "t", "owner": sid})


class Classification(Base):
    def test_owned_stop_wakes(self):
        self.own(); self.event(event="Stop", happier="own1", last="x")
        o = self.watch()
        self.assertEqual((o["wake"], len(o["events"])), ("event", 1))

    def test_unowned_stop_folds(self):
        self.event(event="Stop", happier="other", last="x")
        o = self.watch()
        self.assertEqual((o["wake"], o["folded_event_count"]), ("rearm", 1))

    def test_unowned_permission_prompt_wakes(self):
        self.event(event="Notification", type="permission_prompt", session="z")
        self.assertEqual(self.watch()["wake"], "event")

    def test_idle_prompt_folds_owned_or_not(self):
        self.own()
        self.event(event="Notification", type="idle_prompt", session="z")
        self.event(event="Notification", type="idle_prompt", happier="own1")
        o = self.watch()
        self.assertEqual((o["wake"], o["folded_event_count"]), ("rearm", 2))

    def test_hook_wake_skips_the_poll(self):
        self.own(); self.event(event="Stop", happier="own1")
        calls = []
        fleet.inventory = lambda detail_local=True: calls.append(1) or ({}, [])
        self.assertEqual((self.watch()["wake"], len(calls)), ("event", 0))


class WatcherMechanics(Base):
    def test_second_watcher_reports_already_armed(self):
        import fcntl
        with open(fleet.WATCH_LOCK, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.watch()["wake"], "already-armed")

    def test_wake_is_printed_before_the_offset_is_committed(self):
        self.event(event="Notification", type="permission_prompt")
        def boom(st):
            raise SystemExit(9)
        fleet.save_state = boom
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            fleet.watch(poll=0.01, backstop=0, max_wait=0.05)
        self.assertEqual(json.loads(buf.getvalue())["wake"], "event")
        self.assertEqual(fleet.load_state()["offset"], 0)

    def test_rotation_drains_an_event_that_raced_the_rename(self):
        self.event(event="Stop", happier="x")
        fleet.ROTATE_BYTES = 10
        self.watch()
        self.assertEqual(fleet.load_state()["offset"], 0)
        self.assertFalse(os.path.exists(fleet.EVENTS))
        with open(fleet.EVENTS + ".1", "a") as f:  # a hook that opened the old file pre-rename
            f.write(json.dumps({"event": "Notification", "type": "permission_prompt"}) + "\n")
        self.assertEqual(self.watch()["wake"], "event")

    def test_every_wait_is_capped_by_max_wait_and_never_oversleeps(self):
        signal.signal(signal.SIGALRM, lambda *a: self.fail("uncapped wait"))
        signal.alarm(3)
        try:
            t0 = time.time()
            o = self.watch(hb=0.2, poll=5, backstop=1e9)
            elapsed = time.time() - t0
        finally:
            signal.alarm(0)
        self.assertEqual(o["wake"], "rearm")
        self.assertLess(elapsed, 1.0)

    def test_cli_refuses_max_wait_past_the_job_ceiling(self):
        try:  # an accepted value starts a real watcher; the timeout turns that hang into a failure
            r = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "fleet.py"), "watch", "--max-wait", "8000"],
                               capture_output=True, text=True, env={**os.environ, "HOME": HOME}, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("--max-wait 8000 was accepted")
        self.assertEqual(r.returncode, 2)

    def test_default_max_wait_is_accepted_and_under_7200(self):
        with open(os.path.join(SCRIPTS, "fleet.py")) as f:
            src = f.read()
        self.assertIn('"--max-wait", type=float, default=6600', src)
        self.assertTrue(0 < 6600 <= fleet.MAX_WAIT_CEILING < 7200)


class QuietFleet(Base):
    """The user, 2026-10-17: no wake unless something is actually new or stuck."""

    def test_nothing_in_flight_ends_in_rearm_and_never_earlier(self):
        t0 = time.time()
        self.event(event="Stop", happier="other")         # folded noise must not cut the wait short
        o = self.watch(hb=0.4)
        self.assertEqual(o["wake"], "rearm")
        self.assertGreaterEqual(time.time() - t0, 0.4)

    def test_work_in_flight_ends_in_rearm_at_max_wait_only(self):
        self.own()
        t0 = time.time()
        o = self.watch(hb=0.4)
        self.assertEqual(o["wake"], "rearm")
        self.assertGreaterEqual(time.time() - t0, 0.4)

    def remote(self, sup=False):
        return {"source": "happier", "id": "r1", "title": "r", "host": "other", "local": False,
                "path": "/home/alice/supervisor" if sup else "/x", "supervisor": sup}

    def test_remote_sessions_never_fold_or_wake(self):
        fleet.inventory = lambda detail_local=True: ({"h:r1": self.remote()}, [])
        a = self.watch(hb=0.05)                           # appears
        fleet.inventory = lambda detail_local=True: ({}, [])
        b = self.watch(hb=0.05)                           # goes
        self.assertEqual((a["wake"], a["folded_changes"], b["wake"], b["folded_changes"]), ("rearm", [], "rearm", []))

    def test_a_peer_supervisor_appearing_does_not_fold(self):
        fleet.inventory = lambda detail_local=True: ({"h:r1": self.remote(sup=True)}, [])
        o = self.watch(hb=0.05)
        self.assertEqual((o["wake"], o["folded_changes"]), ("rearm", []))

    def test_a_peer_supervisor_gone_still_wakes(self):
        fleet.inventory = lambda detail_local=True: ({"h:r1": self.remote(sup=True)}, [])
        self.watch(hb=0.05)
        fleet.inventory = lambda detail_local=True: ({}, [])
        o = self.watch(hb=2)  # several polls: an all-drop is doubted once, then needs two misses
        self.assertEqual((o["wake"], o["changes"][0]["change"]), ("event", "peer supervisor gone"))


class Stuck(Base):
    LIM = {"busy_max": 0.3, "idle_max": 0.3, "pending_max": 0.3}

    def setUp(self):
        super().setUp()
        self.own()

    def inv(self, status, pending=0):
        fleet.inventory = lambda detail_local=True: ({"h:own1": {"source": "happier", "id": "own1", "title": "w", "host": "h",
                                                                 "local": True, "status": status, "pending": pending}}, [])

    def changes(self, o):
        return [c["change"].split(" ")[0] for c in o["changes"]]

    def test_busy_past_the_limit_wakes_once_and_rearms_when_it_clears(self):
        self.inv("busy")
        a = self.watch(hb=2, limits=self.LIM)
        b = self.watch(hb=0.5, limits=self.LIM)            # still busy: no second wake
        self.inv("idle"); self.watch(hb=0.1, limits=self.LIM)
        self.inv("busy")
        c = self.watch(hb=2, limits=self.LIM)              # a new busy stretch can fire again
        self.assertEqual((self.changes(a), b["wake"], self.changes(c)), (["stuck?"], "rearm", ["stuck?"]))

    def test_idle_after_a_turn_without_status_wakes_once(self):
        self.inv("idle")
        self.event(event="Stop", happier="own1", last="Which colour do you want?")
        a = self.watch(hb=2, limits=self.LIM)               # the Stop itself
        b = self.watch(hb=2, limits=self.LIM)               # then silence past idle_max
        c = self.watch(hb=0.5, limits=self.LIM)             # fired once only
        self.assertEqual((a["wake"], self.changes(b), c["wake"]), ("event", ["idle"], "rearm"))

    def test_idle_after_a_turn_with_a_status_line_does_not_wake(self):
        self.inv("idle")
        self.event(event="Stop", happier="own1", last="All done.\nSTATUS: done, PR #4 open")
        self.watch(hb=2, limits=self.LIM)
        self.assertEqual(self.watch(hb=0.6, limits=self.LIM)["wake"], "rearm")

    def test_idle_after_an_interim_status_working_turn_does_not_wake(self):
        self.inv("idle")                                    # worker paused only to wait on its own background job
        self.event(event="Stop", happier="own1", last="Build running in the background.\nSTATUS: working (waiting on cargo test)")
        lim = {**self.LIM, "busy_max": 60}                  # isolate the idle check from the working-hang one
        self.watch(hb=2, limits=lim)
        self.assertEqual(self.watch(hb=0.6, limits=lim)["wake"], "rearm")

    def test_a_working_turn_that_never_resumes_wakes_once_after_busy_max(self):
        self.inv("idle")
        self.event(event="Stop", happier="own1", last="STATUS: working (waiting on cargo test)")
        a = self.watch(hb=2, limits=self.LIM)               # the Stop itself
        b = self.watch(hb=2, limits=self.LIM)               # its job never returns: past busy_max
        c = self.watch(hb=0.5, limits=self.LIM)             # fired once only
        self.assertEqual((a["wake"], self.changes(b), c["wake"]), ("event", ["working?"], "rearm"))

    def test_only_the_last_status_line_marks_a_turn_interim(self):
        self.inv("idle")                                    # quoted an earlier interim line, then finished
        self.event(event="Stop", happier="own1", last="Earlier: STATUS: working (waiting on x)\nSTATUS: done")
        self.watch(hb=2, limits=self.LIM)
        self.assertEqual(self.watch(hb=0.6, limits=self.LIM)["wake"], "rearm")

    def test_markdown_status_lines_count_and_parse(self):
        lim = {**self.LIM, "busy_max": 60}
        self.inv("idle")
        self.event(event="Stop", happier="own1", last="All done.\n**STATUS**: done")
        self.watch(hb=2, limits=lim)
        no_idle = self.watch(hb=0.6, limits=lim)["wake"]
        self.event(event="Stop", happier="own1", last="**STATUS:** working (waiting on x)")
        self.watch(hb=2, limits=self.LIM)
        self.assertEqual((no_idle, self.changes(self.watch(hb=2, limits=self.LIM))), ("rearm", ["working?"]))

    def test_a_pending_permission_past_the_limit_wakes_once(self):
        self.inv("waiting", pending=1)
        a = self.watch(hb=2, limits=self.LIM)               # the new request wakes at once
        b = self.watch(hb=2, limits=self.LIM)               # the reminder after pending_max
        c = self.watch(hb=0.5, limits=self.LIM)             # once only
        self.assertEqual((self.changes(a), self.changes(b), c["wake"]), (["pending", "waiting"], ["still"], "rearm"))

    def test_a_finished_items_worker_stops_being_watched_for_stuck(self):
        self.inv("busy")
        self.watch(hb=0.1, limits=self.LIM)                  # busy clock starts while owned
        ledger.append({"op": "set", "id": "W1", "state": "done", "evidence": "x"})
        self.assertEqual(self.watch(hb=0.6, limits=self.LIM)["wake"], "rearm")

    def test_unowned_sessions_are_never_stuck(self):
        fleet.inventory = lambda detail_local=True: ({"h:x": {"source": "happier", "id": "x", "title": "x", "host": "h",
                                                              "local": True, "status": "busy"}}, [])
        self.assertEqual(self.watch(hb=0.6, limits=self.LIM)["wake"], "rearm")


class Backstop(Base):
    PREV = {"h:a": {"id": "a", "title": "t", "host": "h", "status": "busy"},   # b and c already missed once
            "h:b": {"id": "b", "title": "u", "host": "h", "missing": 1},
            "c:c": {"id": "c", "title": "v", "host": "h", "missing": 1}}
    CUR = {"h:a": {"id": "a", "title": "t", "host": "h", "status": "idle"}}

    def changes(self, failed, prev=None):
        u, f, fin, carried = fleet.diff(prev or self.PREV, self.CUR, {"a", "b", "c"}, failed)
        return sorted([c["change"].split(" ")[0] + ":" + c["session"] for c in u + fin] +
                      [f"carried{r['missing']}:{r['id']}" for r in carried.values()])

    def test_owned_finish_and_gone(self):
        self.assertEqual(self.changes(set()), ["finished:a", "gone:b", "gone:c"])

    def test_a_session_that_changes_key_is_not_gone(self):
        # Owned under its Claude id; then Happier lists it and inventory folds it into h:H.
        prev = {"c:C": {"id": "C", "title": "w", "host": "h", "missing": 1}}
        moved = {"h:H": {"id": "H", "title": "w", "host": "h", "claude_session": "C"}}
        self.assertEqual(fleet.diff(prev, moved, {"C"}, set())[0], [])
        back = {"h:H": {"id": "H", "title": "w", "host": "h", "claude_session": "C", "missing": 1}}
        self.assertEqual(fleet.diff(back, {"c:C": {"id": "C", "title": "w", "host": "h"}}, {"H"}, set())[0], [])
        # Control: the same miss with no record carrying it under another key is gone.
        self.assertEqual([c["change"] for c in fleet.diff(prev, {}, {"C"}, set())[0]], ["gone"])

    def test_a_first_miss_is_carried_not_gone(self):
        prev = {k: {f: v for f, v in r.items() if f != "missing"} for k, r in self.PREV.items()}
        self.assertEqual(self.changes(set(), prev), ["carried1:b", "carried1:c", "finished:a"])

    def test_a_failed_listing_hides_only_its_own_source(self):
        self.assertEqual(self.changes({"h:"}), ["finished:a", "gone:c"])
        self.assertEqual(self.changes({"c:"}), ["finished:a", "gone:b"])

    def test_failing_listing_keeps_only_that_sources_last_known_sessions(self):
        c = lambda i: {"source": "claude", "id": i, "title": i, "host": "h"}
        fleet.inventory = lambda detail_local=True: ({"h:own1": {"source": "happier", "id": "own1", "title": "w", "host": "h"},
                                                      "c:keep": c("keep"), "c:old": c("old")}, [])
        one_poll = dict(backstop=1e9)
        self.watch(**one_poll)
        fleet.inventory = lambda detail_local=True: ({"c:keep": c("keep"), "c:z": c("z")}, ["happier session list: boom"])
        self.watch(**one_poll)
        after_one = sorted(fleet.load_state()["fleet"])
        self.watch(**one_poll)
        # h:own1 kept (its listing failed); c:old held for one confirming poll, then dropped
        self.assertEqual((after_one, sorted(fleet.load_state()["fleet"])),
                         (["c:keep", "c:old", "c:z", "h:own1"], ["c:keep", "c:z", "h:own1"]))

    def test_a_listing_that_keeps_failing_becomes_one_wake(self):
        fleet.BLIND_POLLS = 3
        fleet.inventory = lambda detail_local=True: ({}, ["happier session list: boom"])
        o = self.watch(hb=3)
        self.assertEqual(o["wake"], "event")
        self.assertTrue(o["changes"][0]["change"].startswith("backstop blind"))


class TransientListings(Base):
    """2026-10-18: one bad `happier session list` reported every owned worker and the peer
    supervisor gone on two machines; a second later all were listed active. A session is gone
    only after MISS_POLLS successful listings in a row omit it, and a listing that drops every
    known session is first treated as a failed fetch. Each scenario is re-run with its defence
    switched off (the deliberate break): a no-wake result that the break cannot flip proves nothing."""

    W1 = {"source": "happier", "id": "own1", "title": "w1", "host": "h", "local": True}
    W2 = {"source": "happier", "id": "own2", "title": "w2", "host": "h", "local": True}
    PEER = {"source": "happier", "id": "peer", "title": "sup", "host": "other", "local": False,
            "path": "/home/alice/supervisor", "supervisor": True}
    FULL = (W1, W2, PEER)

    def setUp(self):
        super().setUp()
        self.own("own1")
        ledger.append({"op": "add", "id": "W2", "title": "t", "owner": "own2"})
        fleet.BLIND_POLLS = 99  # keep the blind wake out of these sequences

    def polls(self, *listings, **knobs):
        """A fresh watcher state, then one arm with exactly one poll per listing. Returns each
        arm's wake changes as "change:session"."""
        for k, v in knobs.items():
            setattr(fleet, k, v)
        if os.path.exists(fleet.WATCH_STATE):
            os.remove(fleet.WATCH_STATE)
        out = []
        for lst in listings:
            fleet.inventory = lambda detail_local=True, lst=lst: ({"h:" + r["id"]: dict(r) for r in lst}, [])
            o = self.watch(hb=0.05, backstop=1e9)
            out.append(sorted(f"{c['change']}:{c['session']}" for c in o["changes"]))  # every change, not only gone
        return out

    ALL_GONE = ["gone:own1", "gone:own2", "peer supervisor gone:peer"]

    def test_an_empty_listing_between_two_full_ones_does_not_wake(self):
        f = self.FULL
        self.assertEqual(self.polls(f, (), f), [[], [], []])
        # Either defence alone holds; with both switched off the same blip wakes for everything.
        self.assertEqual(self.polls(f, (), f, MISS_POLLS=1, DROP_DOUBT=1), [[], [], []])
        self.assertEqual(self.polls(f, (), f, MISS_POLLS=2, DROP_DOUBT=0), [[], [], []])
        self.assertEqual(self.polls(f, (), f, MISS_POLLS=1, DROP_DOUBT=0), [[], self.ALL_GONE, []])

    def test_a_partial_listing_missing_one_session_for_one_poll_does_not_wake(self):
        f, partial = self.FULL, (self.W2, self.PEER)
        self.assertEqual(self.polls(f, partial, f), [[], [], []])
        self.assertEqual(self.polls(f, partial, f, MISS_POLLS=1), [[], ["gone:own1"], []])  # break

    def test_an_owned_session_missing_for_two_polls_wakes(self):
        f, partial = self.FULL, (self.W2, self.PEER)
        self.assertEqual(self.polls(f, partial, partial), [[], [], ["gone:own1"]])
        # Break: the misses must be consecutive; a listing that shows it again resets the count.
        self.assertEqual(self.polls(f, partial, f, partial), [[], [], [], []])

    def test_a_peer_supervisor_missing_for_one_poll_does_not_wake_and_for_two_does(self):
        f, partial = self.FULL, (self.W1, self.W2)
        self.assertEqual(self.polls(f, partial, f), [[], [], []])
        self.assertEqual(self.polls(f, partial, partial), [[], [], ["peer supervisor gone:peer"]])
        self.assertEqual(self.polls(f, partial, f, MISS_POLLS=1), [[], ["peer supervisor gone:peer"], []])  # break

    def test_a_real_mass_exit_is_still_reported_once_listings_agree(self):
        f = self.FULL
        self.assertEqual(self.polls(f, (), (), ()), [[], [], [], self.ALL_GONE])
        # Break: without the doubt the confirmation alone reports it one poll sooner.
        self.assertEqual(self.polls(f, (), (), DROP_DOUBT=0), [[], [], self.ALL_GONE])

    def test_a_failed_listing_never_reports_gone_whatever_the_binary_path(self):
        """The 2026-10-18 root cause, through the real inventory(): HAPPIER is an absolute path,
        and the error label built from argv stopped matching what watch() keys on."""
        importlib.reload(fleet)  # the real inventory() and run_json()
        fleet.POLL_BUDGET, fleet.BLIND_POLLS = 0, 99
        fleet.HAPPIER = "/usr/local/bin/happier"
        host = socket.gethostname()
        listing = json.dumps({"ok": True, "data": {"sessions": [{"id": "own1", "host": host, "path": "/w"}],
                                                   "hasNext": False}})
        fail = [False]
        def run(cmd, **kw):
            if cmd[0] == "claude":
                return subprocess.CompletedProcess(cmd, 0, "[]", "")
            if cmd[2] == "status":
                return subprocess.CompletedProcess(cmd, 0, '{"ok": true, "data": {"agentState": {}}}', "")
            if fail[0]:
                raise subprocess.TimeoutExpired(cmd, 30)
            return subprocess.CompletedProcess(cmd, 0, listing, "")
        def arms(**knobs):
            for k, v in knobs.items():
                setattr(fleet, k, v)
            if os.path.exists(fleet.WATCH_STATE):
                os.remove(fleet.WATCH_STATE)
            fail[0] = False
            self.watch(hb=0.05, backstop=1e9)
            fail[0] = True
            return [c["change"] for c in self.watch(hb=0.05, backstop=1e9)["changes"]]
        with mock.patch.object(fleet.subprocess, "run", run):
            # Other defences off, so only the source classification stands between a timeout and "gone".
            self.assertEqual(arms(MISS_POLLS=1, DROP_DOUBT=0), [])
            real = fleet.run_json  # break: the pre-fix label, derived from the absolute argv
            fleet.run_json = lambda cmd, timeout=30, label=None: real(cmd, timeout, " ".join(cmd[:3]))
            self.assertEqual(arms(), ["gone"])

    def test_untrustworthy_listings_are_failed_fetches(self):
        s = lambda *ids: [{"id": i} for i in ids]
        ok = lambda sessions, **extra: {"ok": True, "data": {"sessions": sessions, "hasNext": False, **extra}}
        bad = [None, [], {"ok": False, "error": {"code": "x"}}, {"ok": True}, {"ok": True, "data": {}},
               {"ok": True, "data": {"sessions": None}}, ok([{"title": "no id"}]),
               ok(s("me", "a"), hasNext=True), ok(s("me", *[str(i) for i in range(199)])), ok(s("a", "b")),
               ok([{"id": "me", "path": 42}]), ok([{"id": "me", "host": ["x"]}]), ok([{"id": ""}, {"id": "me"}])]
        self.assertEqual([fleet.trusted_listing(h, "me")[0] for h in bad], [None] * len(bad))
        # Controls: a whole listing passes, with or without a known self id.
        self.assertEqual(fleet.trusted_listing(ok(s("me", "a")), "me"), (s("me", "a"), None))
        self.assertEqual(fleet.trusted_listing(ok(s("a")), None), (s("a"), None))
        nulls = [{"id": "me", "host": None, "path": None, "title": None}]
        self.assertEqual(fleet.trusted_listing(ok(nulls), "me"), (nulls, None))
        self.assertEqual(fleet.trusted_listing(ok(s("me", *[str(i) for i in range(198)])), "me")[1], None)

    def test_a_happier_session_never_resurfaces_as_claude_while_its_listing_fails(self):
        importlib.reload(fleet)
        host = socket.gethostname()
        listing = {"ok": True, "data": {"sessions": [{"id": "own1", "host": host, "path": "/w"}]}}
        hap = {"out": json.dumps(listing)}
        def run(cmd, **kw):
            if cmd[0] == "claude":
                return subprocess.CompletedProcess(cmd, 0, json.dumps([{"sessionId": "cs1", "pid": 1, "cwd": "/w",
                                                                         "status": "busy"}]), "")
            if cmd[2] == "status":
                return subprocess.CompletedProcess(cmd, 0, '{"ok": true, "data": {"agentState": {}}}', "")
            return subprocess.CompletedProcess(cmd, 0, hap["out"], "")
        fleet.happier_id_of = lambda pid: "own1"
        with mock.patch.object(fleet.subprocess, "run", run):
            folded = sorted(fleet.inventory()[0])
            hap["out"] = '{"ok": true, "data": {}}'              # untrusted listing
            during = sorted(fleet.inventory()[0])
            listing["data"]["sessions"] = []                     # control: listing fine, session not active in Happier
            hap["out"] = json.dumps(listing)
            unlisted = sorted(fleet.inventory()[0])
        self.assertEqual((folded, during, unlisted), (["h:own1"], [], ["c:cs1"]))

    def test_an_unrecognised_claude_listing_is_a_failure_of_that_source(self):
        importlib.reload(fleet)
        outs = {"happier": '{"ok": true, "data": {"sessions": []}}'}
        for claude_out, want in (('{"error": "x"}', ["claude agents: unrecognised response shape"]),
                                 ('[{"name": "no sessionId"}]', ["claude agents: unrecognised response shape"]),
                                 ('[{"sessionId": 42}]', ["claude agents: unrecognised response shape"]),
                                 ('[{"sessionId": "s", "cwd": 7}]', ["claude agents: unrecognised response shape"]),
                                 ("[]", [])):  # control
            outs["claude"] = claude_out
            with mock.patch.object(fleet.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
                    cmd, 0, outs["claude" if cmd[0] == "claude" else "happier"], "")):
                self.assertEqual(fleet.inventory()[1], want)


class OneWakePerTurn(Base):
    """A worker turn wakes the supervisor exactly once and no turn is lost. Turns are
    identified by the head of their final message; history() returns the latest one."""

    def setUp(self):
        super().setUp()
        self.latest = {"own1": "A"}
        self.created = {}  # text -> createdAt ms; a turn's message is stamped when first served
        def hist(k):
            t = self.latest[k]
            return [{"role": "agent", "text": t, "createdAt": self.created.setdefault(t, time.time() * 1000)}]
        fleet.history = hist
        fleet.FINISH_GRACE = 0.2
        self.own()

    def inv(self, *statuses):
        seq = list(statuses)
        def inv(detail_local=True):
            st = seq.pop(0) if len(seq) > 1 else seq[0]
            return ({"h:own1": {"source": "happier", "id": "own1", "title": "w", "host": "h", "status": st}}, [])
        fleet.inventory = inv

    def stop(self, text):
        self.event(event="Stop", happier="own1", last=text)

    @staticmethod
    def brief(o):
        return f"{o['wake']}:{len(o['events'])}e{len(o['changes'])}c"

    def test_poll_first_then_stop_wakes_once(self):
        self.inv("busy", "idle")
        threading.Timer(0.05, self.stop, ["A"]).start()
        a, b = self.watch(hb=3), self.watch(hb=0.5)
        self.assertEqual((self.brief(a), self.brief(b)), ("event:1e0c", "rearm:0e0c"))

    def test_no_stop_at_all_poll_reports_with_text(self):
        self.inv("busy", "idle")
        o = self.watch(hb=3)
        self.assertEqual((self.brief(o), o["changes"][0].get("text")), ("event:0e1c", "A"))

    def test_late_stop_for_a_poll_reported_turn_is_folded(self):
        self.inv("busy", "idle"); self.watch(hb=3)
        self.stop("A"); self.inv("idle")
        o = self.watch(hb=0.3)
        self.assertEqual((self.brief(o), o["folded_event_count"]), ("rearm:0e0c", 1))

    def test_next_turns_stop_still_wakes_after_a_poll_report(self):
        self.inv("busy", "idle"); self.watch(hb=3)
        self.stop("B"); self.inv("idle")
        self.assertEqual(self.brief(self.watch(hb=0.3)), "event:1e0c")

    def test_stop_first_then_busy_idle_poll_next_arm_no_second_wake(self):
        self.inv("busy"); self.watch(hb=0.1)
        self.stop("A"); a = self.watch(hb=3)
        self.inv("idle"); b = self.watch(hb=0.5)
        self.assertEqual((self.brief(a), self.brief(b)), ("event:1e0c", "rearm:0e0c"))

    # A Stop-less turn, reported by the poll, must move the idle clock exactly as a Stop would.
    LIM = {"busy_max": 60, "idle_max": 0.6, "pending_max": 60}

    def test_a_stopless_turn_with_a_status_line_clears_an_earlier_turns_idle_clock(self):
        self.stop("Which colour?"); self.watch(hb=3, limits=self.LIM)       # A: no STATUS, answered
        self.latest["own1"] = "Building.\nSTATUS: working (waiting on build)"
        self.inv("busy", "idle")
        b = self.watch(hb=3, limits=self.LIM)                                # B arrives by poll only
        c = self.watch(hb=1.5, limits=self.LIM)
        self.assertEqual((b["changes"][0]["change"].split(" ")[0], c["wake"]), ("finished", "rearm"))

    def test_a_stopless_turn_without_a_status_line_still_gets_the_idle_wake(self):
        self.stop("All set.\nSTATUS: done"); self.watch(hb=3, limits=self.LIM)
        self.latest["own1"] = "Which colour?"
        self.inv("busy", "idle")
        self.watch(hb=3, limits=self.LIM)                                    # B arrives by poll only
        c = self.watch(hb=3, limits=self.LIM)
        self.assertEqual([x["change"].split(" ")[0] for x in c["changes"]], ["idle"])

    def test_a_late_stop_for_a_poll_recorded_turn_does_not_rewake_idle(self):
        self.inv("busy", "idle")
        self.latest["own1"] = "Which colour?"
        self.watch(hb=3, limits=self.LIM)                                    # turn by poll only
        a = self.watch(hb=3, limits=self.LIM)                                # its idle wake
        self.stop("Which colour?"); self.inv("idle")                        # its Stop, late
        b = self.watch(hb=1.5, limits=self.LIM)
        self.assertEqual(([x["change"].split(" ")[0] for x in a["changes"]], b["wake"]), (["idle"], "rearm"))

    def test_a_textless_poll_finish_keeps_the_stop_recorded_turn(self):
        fleet.history = lambda k: None                                       # history unreadable
        self.inv("busy")
        self.watch(hb=0.1, limits=self.LIM)                                  # seen busy
        self.stop("All set.\nSTATUS: done"); self.inv("idle")
        self.watch(hb=3, limits=self.LIM)                                    # the Stop wakes
        self.watch(hb=3, limits=self.LIM)                                    # the textless poll finish
        self.assertEqual(self.watch(hb=1.5, limits=self.LIM)["wake"], "rearm")

    def test_stopless_turn_not_hidden_by_an_earlier_turns_stop(self):
        self.inv("busy", "idle")
        threading.Timer(0.05, self.stop, ["A"]).start()
        self.watch(hb=3)                      # A pending, then A's Stop wakes
        self.latest["own1"] = "B"; self.inv("busy", "idle")
        o = self.watch(hb=3)                  # B ran and finished with no Stop
        self.assertEqual((self.brief(o), o["changes"][0].get("text")), ("event:0e1c", "B"))

    def test_turns_sharing_a_long_opening_are_still_distinct(self):
        head = "Summary of the work so far, " * 10  # identical first 200+ chars
        self.stop(head + "STATUS: blocked on A"); self.watch(hb=3)
        self.stop(head + "STATUS: done, B shipped")
        self.assertEqual(self.brief(self.watch(hb=0.3)), "event:1e0c")

    def test_stale_history_retries_then_reports_the_new_turn(self):
        self.stop("A"); self.watch(hb=3)                   # A delivered by its hook
        self.created["A"] = 1000.0                          # ...long ago
        served = []
        def hist(k):
            served.append(1)
            if len(served) < 3:                             # history lags: still shows A
                return [{"role": "agent", "text": "A", "createdAt": 1000.0}]
            return [{"role": "agent", "text": "B", "createdAt": time.time() * 1000}]
        fleet.history = hist
        self.inv("busy", "idle")                            # B ran with no Stop
        o = self.watch(hb=5)
        self.assertEqual((self.brief(o), o["changes"][0].get("text"), len(served)), ("event:0e1c", "B", 3))

    def test_history_that_never_moves_on_reports_without_text(self):
        self.stop("A"); self.watch(hb=3)
        fleet.history = lambda k: [{"role": "agent", "text": "A", "createdAt": 1000.0}]
        fleet.STALE_RETRIES = 1
        self.inv("busy", "idle")
        o = self.watch(hb=5)
        self.assertEqual((self.brief(o), o["changes"][0].get("text")), ("event:0e1c", None))

    def test_a_failed_claude_listing_keeps_the_busy_baseline(self):
        recs = [{"source": "happier", "id": "own1", "title": "w", "host": "h", "status": "busy"},
                {"source": "happier", "id": "own1", "title": "w", "host": "h"},   # claude agents failed
                {"source": "happier", "id": "own1", "title": "w", "host": "h", "status": "idle"}]
        errs = [[], ["claude agents: boom"], []]
        def inv(detail_local=True):  # one result per arm's first poll; repeats the last after
            r, e = (recs.pop(0), errs.pop(0)) if len(recs) > 1 else (recs[0], errs[0])
            return ({"h:own1": dict(r)}, list(e))
        fleet.inventory = inv
        self.latest["own1"] = "B"
        self.watch(hb=0.05, backstop=1e9); self.watch(hb=0.05, backstop=1e9)  # one poll per arm
        o = self.watch(hb=3, backstop=1e9)
        self.assertEqual((self.brief(o), o["changes"][0].get("text")), ("event:0e1c", "B"))

    def test_delivered_turns_are_pruned_after_absence_and_age(self):
        self.stop("A"); self.watch(hb=3)
        st = fleet.load_state()
        st["delivered_turns"]["old"] = {"turn": "x|x", "at": 1.0, "absent": None}
        st["delivered_turns"]["own1"]["absent"] = 1.0
        fleet.save_state(st)
        fleet.inventory = lambda detail_local=True: ({}, ["claude agents: boom"])  # even mid-outage
        self.watch(hb=0.05)
        self.assertEqual(fleet.load_state()["delivered_turns"], {})

    def test_unreadable_history_reports_rather_than_drops(self):
        fleet.history = lambda k: None
        self.inv("busy", "idle")
        self.assertEqual(self.brief(self.watch(hb=3)), "event:0e1c")


class Budgets(Base):
    def test_no_poll_starts_within_the_poll_budget_of_max_wait(self):
        calls = []
        fleet.inventory = lambda detail_local=True: calls.append(1) or ({}, [])
        fleet.POLL_BUDGET = 150
        self.watch(hb=0.05)                      # a 0.05 s arm < POLL_BUDGET: a poll could overrun
        self.assertEqual(len(calls), 0)

    def test_status_calls_stop_at_the_status_budget(self):
        importlib.reload(fleet)                  # the real inventory(), with run_json stubbed
        host = fleet.socket.gethostname()
        calls = []
        def rj(cmd, timeout=30, label=None):
            calls.append(cmd[2])
            if cmd[2] == "list":
                return {"ok": True, "data": {"sessions": [{"id": f"s{i}", "host": host, "path": "/p"} for i in range(5)]}}, None
            if cmd[2] == "status":
                time.sleep(0.05)
                return {"data": {"agentState": {"pendingRequestsCount": 0}}}, None
            return [], None
        fleet.run_json = rj
        fleet.STATUS_BUDGET = 0.08
        fleet.inventory()
        self.assertLess(calls.count("status"), 5)

    def test_record_keeps_both_key_ends_of_a_long_unicode_message(self):
        # Control chars JSON-escape to 6 bytes each, forcing record() down to its smallest clip.
        full = "\x01" * 3000 + " middle " + "\x02" * 3000
        r = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "fleet.py"), "record"],
                           input=json.dumps({"hook_event_name": "Stop", "session_id": "s", "cwd": "/tmp",
                                             "last_assistant_message": full}),
                           text=True, env={**os.environ, "HOME": HOME}, timeout=10)
        with open(fleet.EVENTS) as f:
            logged = [json.loads(l) for l in f if l.strip()][-1]["last"]
        self.assertEqual(fleet.turn_key(logged), fleet.turn_key(full))


class HappierBinary(Base):
    def test_falls_back_to_the_install_bin_when_not_on_path(self):
        b = os.path.join(HOME, ".happier", "bin"); os.makedirs(b, exist_ok=True)
        exe = os.path.join(b, "happier")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        try:
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
                self.assertEqual(fleet._happier_bin(), exe)
            os.chmod(exe, 0o644)  # not executable: keep the bare name so the error names it
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
                self.assertEqual(fleet._happier_bin(), "happier")
        finally:
            shutil.rmtree(os.path.join(HOME, ".happier"))

    def test_prefers_path(self):
        b = tempfile.mkdtemp(); exe = os.path.join(b, "happier")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        try:
            with mock.patch.dict(os.environ, {"PATH": b}):
                self.assertEqual(fleet._happier_bin(), exe)
        finally:
            shutil.rmtree(b)


class HypervisorFlag(Base):
    """A ~/hypervisor session is flagged HYPERVISOR, never SUPERVISOR: in ~/supervisor it would
    read as a duplicate supervisor and its ending as a peer supervisor gone."""

    def test_snapshot_flags_the_hypervisor_by_path(self):
        listing = {"v": 1, "ok": True, "kind": "session_list", "data": {"sessions": [
            {"id": "hv1", "host": "elsewhere", "path": "/home/alice/hypervisor", "title": "Hypervisor"},
            {"id": "sv1", "host": "elsewhere", "path": "/home/alice/supervisor", "title": "Supervisor"}],
            "hasNext": False}}
        def run_json(cmd, **k):
            return (listing, None) if "list" in cmd else ([], None)
        out = io.StringIO()
        with mock.patch.object(fleet, "run_json", run_json), mock.patch.object(fleet, "inventory", REAL_INVENTORY), \
             contextlib.redirect_stdout(out):
            fleet.snapshot(False)
        lines = {l.split()[1]: l for l in out.getvalue().splitlines() if l.startswith("  [")}
        self.assertTrue(lines["hv1"].endswith("HYPERVISOR"), out.getvalue())
        self.assertNotIn("SUPERVISOR", lines["hv1"].replace("HYPERVISOR", ""))
        self.assertTrue(lines["sv1"].endswith("SUPERVISOR"))


class LauncherPaths(Base):
    def test_supervisor_and_hypervisor_dirs_match_linux_and_macos_homes(self):
        for home in ("/home/alice", "/Users/alice", "/root"):
            self.assertTrue(fleet.PEER_PATH.match(home + "/supervisor"), home)
            self.assertTrue(fleet.HYPER_PATH.match(home + "/hypervisor/"), home)
        for path in ("/home/alice/supervisor-old", "/home/alice/work/supervisor", "/Users/supervisor", "/srv/supervisor"):
            self.assertFalse(fleet.PEER_PATH.match(path), path)


class CompactSelf(Base):
    """fleet.py compact-self. Measured 2026-10-19: /compact sent mid-turn is injected into the
    running turn as text and derails it; sent to an idle session it compacts."""

    def setUp(self):
        super().setUp()
        self.sent, self.statuses = [], []
        os.environ["HAPPIER_SESSION_ID"] = "me"
        self.addCleanup(os.environ.pop, "HAPPIER_SESSION_ID", None)
        p = mock.patch.object(fleet, "own_status", lambda: self.statuses.pop(0) if self.statuses else "idle")
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(fleet, "happier_call", lambda args, **k: (self.sent.append(args) or ({}, None)))
        p.start(); self.addCleanup(p.stop)

    def run_(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return fleet.compact_self("Keep: decisions", poll=0, sleep=lambda s: None)

    def test_waits_for_two_idle_polls_before_sending_then_for_compaction_to_finish(self):
        log = []
        seq = ["busy", "idle", "busy", "idle", "idle", "busy", "busy", "idle"]
        def status():
            log.append(len(self.sent))  # how many sends had happened when each poll ran
            return seq.pop(0)
        with mock.patch.object(fleet, "own_status", status):
            self.assertEqual(self.run_(), 0)
        self.assertEqual(self.sent, [["send", "me", "/compact Keep: decisions"]])
        # The send happens only after the 4th and 5th polls are idle in a row.
        self.assertEqual(log, [0, 0, 0, 0, 0, 1, 1, 1])

    def test_an_unknown_status_resets_the_idle_count_and_an_unseen_finish_exits_1(self):
        # None between idles resets the count, so the send waits for two clean idles in a row.
        # After the send the status never returns to idle before the deadline: exit 1.
        seq, log = [None, "idle", None, "idle", "idle"], []
        def status():
            log.append(len(self.sent))
            return seq.pop(0) if seq else "busy"
        with mock.patch.object(fleet, "own_status", status), \
             mock.patch.object(fleet.time, "time", side_effect=[0] + [1] * 8 + [10 ** 9] * 20):
            self.assertEqual(self.run_(), 1)
        self.assertEqual(log[:6], [0, 0, 0, 0, 0, 1])
        self.assertEqual(len(self.sent), 1)

    def test_sends_on_the_second_idle_read_without_sleeping_first(self):
        # Review 2026-10-19: a sleep between the idle read and the send let a new turn start.
        events, seq = [], ["idle", "idle", "busy", "idle"]
        def status():
            events.append("poll")
            return seq.pop(0)
        with mock.patch.object(fleet, "own_status", status), \
             mock.patch.object(fleet, "happier_call", lambda args, **k: (events.append("send") or ({}, None))):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = fleet.compact_self("", poll=0, sleep=lambda s: events.append("sleep"))
        self.assertEqual(rc, 0)
        self.assertEqual(events[:4], ["poll", "sleep", "poll", "send"])

    def test_own_status_counts_only_the_interactive_session(self):
        agents = [{"pid": 1, "kind": "interactive", "status": "idle", "sessionId": "a"},
                  {"pid": 2, "kind": "print", "status": "busy", "sessionId": "b"}]  # a claude -p child
        with mock.patch.object(fleet, "run_json", lambda *a, **k: (agents, None)), \
             mock.patch.object(fleet, "happier_id_of", lambda pid: "me"):
            self.assertEqual(REAL_OWN_STATUS(), "idle")
            agents[1]["kind"] = "interactive"  # two interactive matches: ambiguous, unknown
            self.assertIsNone(REAL_OWN_STATUS())

    def test_then_is_sent_after_compaction_and_even_when_the_finish_is_unseen(self):
        # /compact kills tracked jobs, so --then is the only post-compaction wake (2026-10-19).
        self.statuses = ["idle", "idle", "busy", "idle"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fleet.compact_self("", "run startup", poll=0, sleep=lambda s: None), 0)
        self.assertEqual(self.sent, [["send", "me", "/compact"], ["send", "me", "run startup"]])
        self.sent.clear()
        seq = ["idle", "idle"]
        with mock.patch.object(fleet, "own_status", lambda: seq.pop(0) if seq else "busy"), \
             mock.patch.object(fleet.time, "time", side_effect=[0] + [1] * 4 + [10 ** 9] * 20), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fleet.compact_self("", "run startup", poll=0, sleep=lambda s: None), 1)
        self.assertEqual(self.sent[-1], ["send", "me", "run startup"])

    def test_refuses_outside_happier(self):
        os.environ.pop("HAPPIER_SESSION_ID")
        self.assertEqual(self.run_(), 1)
        self.assertEqual(self.sent, [])


class CloseOut(Base):
    """fleet.py archive / reopen: the archive-finished-conversations rule, 2026-10-17."""

    # Happier's literal --json output on stdout, measured 2026-10-17 against scratch sessions.
    OK = '{"v":1,"ok":true,"kind":"session_archive","data":{"sessionId":"w1","archivedAt":1792254340645}}'
    NOT_FOUND = '{"v":1,"ok":false,"kind":"session_archive","error":{"code":"session_not_found","message":"Session not found"}}'
    NO_AUTH = '{"v":1,"ok":false,"kind":"session_archive","error":{"code":"not_authenticated"}}'

    def setUp(self):
        super().setUp()
        self.calls = []
        class R:
            returncode, stdout, stderr = 0, self.OK, ""
        self.R = R
        self.sessions = {"w1": {"path": "/work/repo"}, "w2": {"path": "/tmp/x"},
                         "peer": {"path": "/home/alice/supervisor"}}
        self.status_err = {}  # sid -> error code session_info reports instead of a record
        fleet.session_info = lambda sid: ((None, self.status_err[sid]) if sid in self.status_err
                                          else (self.sessions[sid], None) if sid in self.sessions
                                          else (None, "session_not_found"))
        self.patch = mock.patch.object(fleet.subprocess, "run", lambda args, **k: self.calls.append(args[1:4]) or self.R())
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # park/orphan list sessions: these are local Claude sessions claude agents reports idle.
        # None means the listing failed.
        self.idle, self.claude_of = {"w1", "w2"}, {}
        inv = mock.patch.object(fleet, "inventory", lambda detail_local=True: (
            ({}, ["listing failed"]) if self.idle is None else
            ({"h:" + s: {"source": "happier", "id": s, "status": "idle", "local": True,
                         "claude_session": self.claude_of.get(s)} for s in self.idle}, [])))
        inv.start()
        self.addCleanup(inv.stop)

    def item(self, state="done", evidence="PR #1 merged", owners=("w1",), note=None):
        ledger.append({"op": "add", "id": "W1", "title": "t", "owner": owners[0]})
        for o in owners[1:]:
            ledger.append({"op": "set", "id": "W1", "owner": o})
        ledger.append({"op": "set", "id": "W1", "state": state, "evidence": evidence, "note": note})

    def run_(self, fn, *a):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a)

    def test_refuses_unless_done_evidenced_and_decision_free(self):
        self.item(state="working")
        self.assertEqual(self.run_(fleet.archive, "W1"), 2)
        setUp = self.setUp; setUp(); self.item(evidence=None)
        self.assertEqual(self.run_(fleet.archive, "W1"), 2)
        setUp(); self.item()
        ledger.append({"op": "decide", "id": "W1", "question": "q", "recommendation": "r"})
        self.assertEqual(self.run_(fleet.archive, "W1"), 2)
        setUp(); self.item(state="stopped", evidence=None)  # stopped, but no reason noted
        self.assertEqual(self.run_(fleet.archive, "W1"), 2)
        self.assertEqual(self.calls, [])

    def test_a_stopped_or_failed_item_with_a_noted_reason_closes_out(self):
        # 2026-10-19 audit: a stopped item's worker otherwise stays open forever.
        for state in ("stopped", "failed"):
            self.setUp()
            self.item(state=state, evidence=None, note="the user stopped it")
            self.assertEqual(self.run_(fleet.archive, "W1"), 0)
            self.assertEqual(self.calls, [["session", "archive", "w1"]])

    def test_a_script_note_is_not_a_stop_reason(self):
        # Only the note written with the stop counts; a later or earlier script note does not.
        self.item(state="working", evidence=None, note="released w9 without archiving")
        ledger.append({"op": "set", "id": "W1", "state": "stopped"})
        ledger.append({"op": "set", "id": "W1", "note": "reopened w1"})
        self.assertEqual(self.run_(fleet.archive, "W1"), 2)
        self.assertEqual(self.calls, [])

    def test_park_then_reopen_round_trips(self):
        self.item(state="working", evidence=None)
        self.assertEqual(self.run_(fleet.archive, "W1", None, True), 2)  # no next yet
        ledger.append({"op": "set", "id": "W1", "next": "upstream PR #781 merges"})
        self.assertEqual(self.run_(fleet.archive, "W1", None, True), 0)
        self.assertEqual(self.calls, [["session", "archive", "w1"]])
        it = ledger.fold()["W1"]
        self.assertEqual((it["state"], it["owners"], it["parked"]), ("working", [], ["w1"]))
        self.assertTrue(it["notes"][-1].startswith("parked w1 (archived w1); reopen when: upstream PR #781"))
        self.assertIn("parked: w1", ledger.describe(it))  # visible after a compaction
        self.assertEqual(self.run_(fleet.reopen, "W1", "w1"), 0)
        self.assertEqual(self.calls[-1], ["session", "unarchive", "w1"])
        it = ledger.fold()["W1"]
        self.assertEqual((it["state"], it["owners"], it["parked"]), ("working", ["w1"], []))
        self.assertNotIn("parked:", ledger.describe(it))

    def test_park_only_touches_a_worker_known_to_be_idle(self):
        self.item(state="working", evidence=None)
        ledger.append({"op": "set", "id": "W1", "next": "CI"})
        self.idle = set()  # busy, a Codex worker, or remote: claude agents does not say idle
        self.assertEqual(self.run_(fleet.archive, "W1", None, True), 1)
        self.idle = None  # listing failed
        self.assertEqual(self.run_(fleet.archive, "W1", None, True), 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(ledger.fold()["W1"]["owners"], ["w1"])

    def test_park_keeps_a_worker_that_works_another_open_item(self):
        self.item(state="working", evidence=None)
        ledger.append({"op": "set", "id": "W1", "next": "review"})
        ledger.append({"op": "add", "id": "W2", "title": "other", "owner": "w1"})
        ledger.append({"op": "set", "id": "W2", "state": "working"})
        self.assertEqual(self.run_(fleet.archive, "W1", None, True), 1)
        self.assertEqual(self.calls, [])
        it = ledger.fold()["W1"]
        self.assertEqual((it["owners"], it["parked"]), (["w1"], []))

    def test_a_worker_whose_every_item_waits_parks_on_all_and_reopens_on_all(self):
        # 2026-10-19: one worker, two items, both waiting on the 03:00 run.
        self.item(state="working", evidence=None)
        ledger.append({"op": "set", "id": "W1", "next": "03:00 run"})
        ledger.append({"op": "add", "id": "W2", "title": "other", "owner": "w1"})
        ledger.append({"op": "set", "id": "W2", "state": "working", "next": "03:00 run"})
        ledger.append({"op": "add", "id": "W3", "title": "done one", "owner": "w1"})
        ledger.append({"op": "set", "id": "W3", "state": "done", "evidence": "x"})  # closed: ignored
        self.assertEqual(self.run_(fleet.archive, "W1", None, True), 0)
        self.assertEqual(self.calls, [["session", "archive", "w1"]])
        f = ledger.fold()
        self.assertEqual([(f[i]["owners"], f[i]["parked"]) for i in ("W1", "W2")], [([], ["w1"]), ([], ["w1"])])
        self.assertEqual(self.run_(fleet.reopen, "W2", "w1"), 0)
        f = ledger.fold()
        self.assertEqual([(f[i]["state"], f[i]["owners"], f[i]["parked"]) for i in ("W1", "W2")],
                         [("working", ["w1"], []), ("working", ["w1"], [])])

    def test_reopen_of_a_co_parked_worker_keeps_the_other_items_state(self):
        self.item(state="working", evidence=None)
        ledger.append({"op": "set", "id": "W1", "next": "review"})
        ledger.append({"op": "add", "id": "W2", "title": "other", "owner": "w1"})
        ledger.append({"op": "set", "id": "W2", "state": "needs-input", "next": "the user"})
        self.assertEqual(self.run_(fleet.archive, "W1", None, True), 0)
        self.assertEqual(self.run_(fleet.reopen, "W1", "w1"), 0)
        it = ledger.fold()["W2"]
        self.assertEqual((it["state"], it["owners"]), ("needs-input", ["w1"]))  # its own wait is not over

    def test_park_refuses_a_finished_item(self):
        self.item()
        self.assertEqual(self.run_(fleet.archive, "W1", None, True), 2)
        self.assertEqual(self.calls, [])

    def test_turn_listing_skips_status_calls_and_fails_closed(self):
        seen = {}
        def inv(detail_local=True):
            seen["detail"] = detail_local
            return {"h:a": {}}, []
        with mock.patch.object(fleet, "inventory", inv):
            self.assertEqual(fleet.turn_listing(), {"h:a": {}})
        self.assertIs(seen["detail"], False)  # no per-session status calls to fail on
        with mock.patch.object(fleet, "inventory", lambda detail_local=True: ({}, ["claude agents failed"])):
            self.assertIsNone(fleet.turn_listing())

    def test_between_turns_for_claude_sessions_uses_claude_agents(self):
        recs = {"h:a": {"source": "happier", "id": "a", "local": True, "status": "busy", "claude_session": "ca"},
                "h:b": {"source": "happier", "id": "b", "local": True, "status": "idle", "claude_session": "cb"},
                "h:r": {"source": "happier", "id": "r", "local": False}}  # another host's session
        with mock.patch.object(fleet, "history", lambda sid: self.fail("Claude/remote never read history")):
            self.assertEqual([fleet.between_turns(s, recs) for s in ("a", "ca", "b", "cb", "r", "zz")],
                             [False, False, True, True, False, False])

    def test_between_turns_for_a_codex_session_needs_a_quiet_status_ended_turn(self):
        # 2026-10-19: a finished Codex reviewer could never be archived.
        recs = {"h:x": {"source": "happier", "id": "x", "local": True}}  # no Claude turn state
        now = 10_000.0
        def at(role, text, age):
            return [{"role": "user", "text": "review it", "createdAt": 0},
                    {"role": role, "text": text, "createdAt": (now - age) * 1000}]
        for msgs, want in ((at("agent", "LGTM.\nSTATUS: done", 200), True),
                           (at("agent", "**STATUS:** blocked", 121), True),
                           (at("agent", "LGTM.\nSTATUS: done", 30), False),   # may still be running
                           (at("agent", "Looking at the diff now", 500), False),  # no STATUS: mid-turn
                           (at("user", "STATUS: done?", 500), False),        # a question waits on it
                           (None, False)):                                    # history unreadable
            with mock.patch.object(fleet, "history", lambda sid, m=msgs: m):
                self.assertIs(fleet.between_turns("x", recs, now=now), want, msgs)

    def test_orphan_archives_a_finished_codex_session(self):
        here = socket.gethostname()
        self.sessions["cx"] = {"id": "cx", "path": "/work/repo", "host": here}
        self.idle = set()
        last = [{"role": "agent", "text": "STATUS: done", "createdAt": (time.time() - 600) * 1000}]
        with mock.patch.object(fleet, "inventory", lambda detail_local=True: (
                {"h:cx": {"source": "happier", "id": "cx", "local": True}}, [])), \
             mock.patch.object(fleet, "history", lambda sid: last):
            self.assertEqual(self.run_(fleet.orphan, "cx", "finished Codex review, released from W4"), 0)
        self.assertEqual(self.calls, [["session", "archive", "cx"]])

    def test_never_archives_the_hypervisor(self):
        self.sessions["hv"] = {"path": "/home/alice/hypervisor"}
        self.item(owners=("w1", "hv"))
        self.assertEqual(self.run_(fleet.archive, "W1"), 1)
        self.assertEqual(self.calls, [["session", "archive", "w1"]])
        self.assertEqual(ledger.fold()["W1"]["owners"], ["hv"])

    def test_orphan_archives_a_finished_unowned_local_session_and_logs_why(self):
        here = socket.gethostname()
        self.sessions["o"] = {"id": "o", "path": "/home/alice", "host": here, "title": "hop"}
        self.idle = {"o"}
        self.assertEqual(self.run_(fleet.orphan, "o", "borrowed for a hop"), 0)
        self.assertEqual(self.calls, [["session", "archive", "o"]])
        with open(os.path.join(fleet.STATE_DIR, "orphans.jsonl")) as f:
            self.assertEqual(json.loads(f.readline())["reason"], "borrowed for a hop")

    def test_orphan_refuses_what_is_not_an_orphan(self):
        here = socket.gethostname()
        self.sessions.update({
            "sup": {"path": "/home/alice/supervisor", "host": here},
            "hv": {"path": "/home/alice/hypervisor", "host": here},
            "far": {"path": "/x", "host": "other-host"},  # its supervisor may own it
            "busy": {"path": "/x", "host": here}})
        self.item(state="done", owners=("w1",))
        self.sessions["w1"]["host"] = here
        self.idle = {"sup", "hv", "far", "w1"}
        for sid in ("sup", "hv", "far", "w1", "busy"):
            self.assertEqual(self.run_(fleet.orphan, sid, "r"), 2, sid)
        self.assertEqual(self.run_(fleet.orphan, "zzz", "r"), 2)  # unknown
        self.assertEqual(self.run_(fleet.orphan, "busy", "  "), 2)  # no reason
        self.assertEqual(self.calls, [])

    def test_orphan_refuses_a_worker_the_ledger_owns_by_its_claude_id(self):
        here = socket.gethostname()
        self.sessions["o"] = {"id": "o", "path": "/work/repo", "host": here}
        self.idle, self.claude_of = {"o"}, {"o": "claude-uuid"}
        ledger.append({"op": "add", "id": "W1", "title": "t", "owner": "claude-uuid"})
        ledger.append({"op": "set", "id": "W1", "state": "working"})
        self.assertEqual(self.run_(fleet.orphan, "o", "r"), 2)
        self.assertEqual(self.calls, [])

    def test_reopen_with_another_id_clears_every_stale_parked_id(self):
        self.item(state="working", evidence=None)
        ledger.append({"op": "set", "id": "W1", "next": "review"})
        self.assertEqual(self.run_(fleet.archive, "W1", None, True), 0)
        self.assertEqual(self.run_(fleet.reopen, "W1", "w2"), 0)
        it = ledger.fold()["W1"]
        self.assertEqual((it["owners"], it["parked"]), (["w2"], []))

    def test_archives_notes_and_releases_every_worker(self):
        self.item(owners=("w1", "w2"))
        self.assertEqual(self.run_(fleet.archive, "W1"), 0)
        self.assertEqual(self.calls, [["session", "archive", "w1"], ["session", "archive", "w2"]])
        it = ledger.fold()["W1"]
        self.assertEqual((it["owners"], it["notes"]), ([], ["archived w1", "archived w2"]))

    def test_never_archives_a_supervisor(self):
        self.item(owners=("w1", "peer"))
        self.assertEqual(self.run_(fleet.archive, "W1"), 1)
        self.assertEqual(self.calls, [["session", "archive", "w1"]])
        self.assertEqual(ledger.fold()["W1"]["owners"], ["peer"])

    def test_never_archives_itself(self):
        self.item(owners=("me",))
        with mock.patch.dict(os.environ, {"HAPPIER_SESSION_ID": "me"}):
            self.assertEqual(self.run_(fleet.archive, "W1"), 1)
        self.assertEqual(self.calls, [])

    def test_a_failed_archive_keeps_the_owner(self):
        self.R.returncode = 1
        self.item()
        self.assertEqual(self.run_(fleet.archive, "W1"), 1)
        self.assertEqual(ledger.fold()["W1"]["owners"], ["w1"])

    def test_two_done_items_sharing_a_worker_both_close(self):
        # Supervisor report, 2026-10-17: the second archive exited 1 and kept the owner.
        self.item()
        ledger.append({"op": "add", "id": "W2", "title": "t2", "owner": "w1"})
        ledger.append({"op": "set", "id": "W2", "state": "done", "evidence": "PR #2 merged"})
        self.assertEqual(self.run_(fleet.archive, "W1"), 0)
        self.sessions["w1"]["archivedAt"] = 1792254340645  # what status reports from now on
        self.R.returncode, self.R.stdout = 1, self.NOT_FOUND
        self.assertEqual(self.run_(fleet.archive, "W2"), 0)
        it = ledger.fold()["W2"]
        self.assertEqual((it["owners"], it["notes"]), ([], ["already archived w1"]))
        self.assertEqual(self.calls, [["session", "archive", "w1"]])  # not asked twice

    def test_archived_between_status_and_archive_counts_as_already_archived(self):
        self.item()
        self.R.returncode, self.R.stdout = 1, self.NOT_FOUND
        self.assertEqual(self.run_(fleet.archive, "W1"), 0)
        it = ledger.fold()["W1"]
        self.assertEqual((it["owners"], it["notes"]), ([], ["already archived w1"]))

    def test_a_session_happier_does_not_know_is_released(self):
        self.item(owners=("gone",))
        self.assertEqual(self.run_(fleet.archive, "W1"), 0)
        it = ledger.fold()["W1"]
        self.assertEqual((it["owners"], it["notes"]), ([], ["not found in Happier, released gone"]))
        self.assertEqual(self.calls, [])

    def test_a_genuine_failure_still_fails_and_keeps_the_owner(self):
        self.item()
        for out, err in ((self.NO_AUTH, ""), ("", "fetch failed: ECONNREFUSED"), ("not json", ""),
                         ('{"v":1,"ok":false,"kind":"session_archive"}', "")):
            self.R.returncode, self.R.stdout, self.R.stderr = 1, out, err
            self.assertEqual(self.run_(fleet.archive, "W1"), 1, out or err)
            it = ledger.fold()["W1"]
            self.assertEqual((it["owners"], it["notes"]), (["w1"], []))

    def test_archives_the_session_status_resolved(self):
        self.item(owners=("w1",))
        self.sessions["w1"]["id"] = "w1-full-id"
        self.assertEqual(self.run_(fleet.archive, "W1"), 0)
        self.assertEqual(self.calls, [["session", "archive", "w1-full-id"]])

    def test_an_unreadable_status_fails_closed(self):
        # Without the path the supervisor check cannot run, so nothing is archived.
        self.item(owners=("peer",))
        self.status_err["peer"] = "not_authenticated"
        self.assertEqual(self.run_(fleet.archive, "W1"), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(ledger.fold()["W1"]["owners"], ["peer"])

    def test_an_already_archived_supervisor_is_still_skipped(self):
        self.item(owners=("peer",))
        self.sessions["peer"]["archivedAt"] = 1792254340645
        self.assertEqual(self.run_(fleet.archive, "W1"), 1)
        self.assertEqual(ledger.fold()["W1"]["owners"], ["peer"])

    def test_session_flag_limits_to_one_owner(self):
        self.item(owners=("w1", "w2"))
        self.assertEqual(self.run_(fleet.archive, "W1", "w2"), 0)
        self.assertEqual(self.calls, [["session", "archive", "w2"]])
        self.assertEqual(self.run_(fleet.archive, "W1", "nobody"), 2)

    def test_a_worker_still_running_other_work_is_released_not_archived(self):
        self.item(owners=("w1", "w2"))
        ledger.append({"op": "add", "id": "W2", "title": "live run", "owner": "w2"})
        ledger.append({"op": "set", "id": "W2", "state": "working"})
        self.assertEqual(self.run_(fleet.archive, "W1"), 1)
        self.assertEqual(self.calls, [["session", "archive", "w1"]])  # w2 untouched in Happier
        items = ledger.fold()
        self.assertEqual(items["W1"]["owners"], [])                   # ...but released from W1
        self.assertEqual(items["W2"]["owners"], ["w2"])               # ...and still owns its run
        self.assertTrue(any("still owns W2" in n for n in items["W1"]["notes"]))

    def test_reopen_unarchives_and_puts_the_item_back_to_working(self):
        self.item(); self.run_(fleet.archive, "W1")
        self.assertEqual(self.run_(fleet.reopen, "W1", "w1"), 0)
        self.assertEqual(self.calls[-1], ["session", "unarchive", "w1"])
        it = ledger.fold()["W1"]
        self.assertEqual((it["state"], it["owners"]), ("working", ["w1"]))
        self.assertIn("w1", ledger.owned_sessions(ledger.fold()))


class HappierCall(Base):
    """happier_call: success needs exit 0 AND ok:true; anything else is an error code or description."""

    def call(self, rc, out, err=""):
        class R:
            returncode, stdout, stderr = rc, out, err
        with mock.patch.object(fleet.subprocess, "run", lambda args, **k: R()):
            return fleet.happier_call(["archive", "x"])

    def test_shapes(self):
        self.assertEqual(self.call(0, '{"ok":true,"data":{"a":1}}'), ({"a": 1}, None))
        self.assertEqual(self.call(1, '{"ok":false,"error":{"code":"session_not_found"}}'), (None, "session_not_found"))
        self.assertEqual(self.call(0, '{"ok":false,"error":{"code":"weird"}}'), (None, "weird"))
        self.assertEqual(self.call(1, '{"ok":true,"data":{}}'), (None, 'exit 1: {"ok":true,"data":{}}'))
        self.assertEqual(self.call(0, "not json"), (None, "exit 0: not json"))
        self.assertEqual(self.call(1, "[]", "boom"), (None, "exit 1: boom"))

    def test_a_timeout_is_an_error_not_a_crash(self):
        def boom(args, **k):
            raise subprocess.TimeoutExpired(args, k["timeout"])
        with mock.patch.object(fleet.subprocess, "run", boom):
            data, err = fleet.happier_call(["status", "x"], timeout=10)
        self.assertIsNone(data)
        self.assertIn("timed out", err)


class Messaging(Base):
    Q = {"role": "agent", "kind": "acp", "text": "red or blue?", "createdAt": 2}

    def answerable(self, msgs):
        fleet.run_json = lambda cmd, timeout=30: ({"data": {"messages": msgs}}, None)
        with contextlib.redirect_stdout(io.StringIO()):
            return fleet.answerable("w")

    def test_answerable(self):
        user = lambda t, at: {"role": "user", "kind": "text", "text": t, "createdAt": at}
        self.assertEqual(self.answerable([self.Q]), 0)
        self.assertEqual(self.answerable([self.Q, user("red", 3)]), 3)
        self.assertEqual(self.answerable([user("go", 1), self.Q]), 0)
        self.assertEqual(self.answerable([self.Q, {"role": "agent", "kind": "event", "text": "m", "createdAt": 4}]), 0)
        self.assertEqual(self.answerable([]), 2)
        self.assertEqual(self.answerable([self.Q, user("red", 2)]), 3)  # same ms keeps oldest-first

    def test_only_the_users_reply_counts_as_already_answered(self):
        """2026-10-21: the supervisor's own earlier message read as "already answered"."""
        q = {**self.Q, "id": "q"}
        reply = {"role": "user", "kind": "text", "text": "red", "createdAt": 3, "id": "r"}
        def run(sender):
            def fake(cmd, timeout=30):
                if "raw" in cmd:
                    if sender == "unreadable":
                        return None, "boom"
                    return {"data": {"messages": [{"id": "q", "role": "agent", "raw": {}},
                            {"id": "r", "role": "user", "raw": {"meta": {"sentFrom": sender} if sender else {}}}]}}, None
                return {"data": {"messages": [q, reply]}}, None
            fleet.run_json = fake
            with contextlib.redirect_stdout(io.StringIO()):
                return fleet.answerable("w")
        self.assertEqual([run(s) for s in ("cli", "happier", "android", "web", None, "unreadable")], [0, 0, 3, 3, 3, 3])

    def test_send_passes_worker_text_as_argv_and_never_executes_it(self):
        seen = []
        class R:
            returncode, stdout, stderr = 0, '{"ok":true,"data":{}}', ""
        pwned = os.path.join(HOME, "pwned")
        with mock.patch.object(fleet.subprocess, "run", lambda args, **k: seen.append(args) or R()), \
             mock.patch.object(sys, "stdin", io.StringIO(f"relay: $(touch {pwned}) and `touch {pwned}2`")), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = fleet.send("w", guard=False)
        self.assertEqual(rc, 0)
        self.assertTrue(seen[0][4].startswith("relay: $(touch"))
        self.assertFalse(os.path.exists(pwned) or os.path.exists(pwned + "2"))

    def test_send_reports_failure_unless_happier_confirms(self):
        """2026-10-21: exit 0 with ok:false read as delivered, a silent drop."""
        def runner(stdout, rc):
            class R:
                returncode, stderr = rc, ""
            R.stdout = stdout
            return lambda args, **k: R()
        for stdout, rc, want in [('{"ok":true,"data":{"sessionId":"w"}}', 0, 0),
                                 ('{"ok":false,"error":{"code":"session_not_found"}}', 0, 1),
                                 ('{"ok":false,"error":{"code":"session_not_found"}}', 1, 1),
                                 ("not json", 0, 1)]:
            out = io.StringIO()
            with mock.patch.object(fleet.subprocess, "run", runner(stdout, rc)), \
                 mock.patch.object(sys, "stdin", io.StringIO("hello")), contextlib.redirect_stdout(out):
                self.assertEqual(fleet.send("w", guard=False), want, stdout)
            self.assertEqual(out.getvalue().startswith("sent:"), want == 0)

    def test_send_guard_refuses_when_the_user_already_answered(self):
        fleet.answerable = lambda sid: 3
        sent = []
        with mock.patch.object(fleet.subprocess, "run", lambda args, **k: sent.append(args)), \
             mock.patch.object(sys, "stdin", io.StringIO("blue")):
            self.assertEqual((fleet.send("w", guard=True), len(sent)), (3, 0))


class LedgerCli(Base):
    def L(self, *args, check=True):
        r = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "ledger.py"), *args],
                           capture_output=True, text=True, env={**os.environ, "HOME": HOME})
        if check and r.returncode:
            self.fail(r.stderr)
        return r

    def test_a_decision_must_name_why_only_the_user_can_answer_and_what_was_tried(self):
        """2026-10-21: six relayed questions, four answerable by a probe."""
        iid = self.L("add", "--title", "t", "--objective", "o").stdout.strip()
        base = ["decide", iid, "--question", "Which config?", "--recommendation", "A"]
        for extra in ([], ["--kind", "taste"], ["--tried", "read the code and deployment"],
                      ["--kind", "curious", "--tried", "read the code and deployment"],
                      ["--kind", "taste", "--tried", "none"]):
            self.assertNotEqual(self.L(*base, *extra, check=False).returncode, 0, extra)
        self.assertNotIn("OPEN DECISION", self.L("show").stdout)
        self.L(*base, "--kind", "action", "--tried", "mouse buttons need a reboot to reload the driver")
        out = self.L("show").stdout
        self.assertIn("OPEN DECISION [action]", out)
        self.assertIn("tried: mouse buttons need a reboot", out)

    def test_round_trip(self):
        iid = self.L("add", "--title", "t1", "--objective", "o", "--criteria", "c1").stdout.strip()
        self.L("set", iid, "--state", "working", "--owner", "s9")
        self.assertEqual(self.L("owned").stdout.split(), ["s9"])
        self.L("set", iid, "--owner", "s10", "--drop-owner", "s9")
        self.assertEqual(self.L("owned").stdout.split(), ["s10"])
        self.L("decide", iid, "--question", "Q?", "--recommendation", "R", "--kind", "taste", "--tried", "checked the code and the deployment")
        self.assertIn("OPEN DECISION", self.L("show").stdout)
        with open(os.path.join(ledger.STATE_DIR, "ledger.md")) as f:
            self.assertIn("Open decisions", f.read())
        self.assertNotEqual(self.L("set", iid, "--state", "done", check=False).returncode, 0)
        self.L("resolve", iid, "--answer", "A")
        self.L("set", iid, "--state", "done", "--evidence", "proof")
        self.assertEqual(self.L("owned").stdout.split(), [])
        self.assertNotEqual(self.L("resolve", iid, "--answer", "B", check=False).returncode, 0)
        self.assertNotEqual(self.L("set", iid, "--state", "finished", check=False).returncode, 0)

    def test_concurrent_adds_get_distinct_ids(self):
        procs = [subprocess.Popen([sys.executable, "-I", os.path.join(SCRIPTS, "ledger.py"), "add",
                                   "--title", f"c{i}", "--objective", "o"], stdout=subprocess.PIPE,
                                  text=True, env={**os.environ, "HOME": HOME}) for i in range(6)]
        ids = [p.communicate()[0].strip() for p in procs]
        self.assertEqual(len(set(ids)), 6)
        self.assertEqual(len(ledger.fold()), 6)



class Published(Base):
    """~/.claude/supervisors/<profile>.md: the per-machine summary a synced ~/.claude carries to the
    other machines (2026-10-17). Literal limits on purpose."""

    L = LedgerCli.L

    def setUp(self):
        super().setUp()
        pubdir = os.path.join(HOME, ".claude", "supervisors")
        if os.path.isfile(pubdir):
            os.remove(pubdir)  # rmtree cannot remove a file, and ignore_errors would hide that
        shutil.rmtree(pubdir, ignore_errors=True)
        self.assertFalse(os.path.exists(pubdir))
        with open(os.path.join(ledger.STATE_DIR, "profile"), "w") as f:
            f.write("example\n")
        self.path = os.path.join(HOME, ".claude", "supervisors", "example.md")

    def pub(self):
        self.assertTrue(os.path.exists(self.path), "the summary was not published")
        with open(self.path) as f:
            return f.read()

    def test_written_on_add_set_decide_and_resolve(self):
        iid = self.L("add", "--title", "first item", "--objective", "o").stdout.strip()
        self.assertIn("| W1 | queued | first item |", self.pub())
        self.L("set", iid, "--state", "working", "--next", "open the PR", "--evidence", "tests OK")
        self.assertIn("| W1 | working | first item | open the PR | tests OK |", self.pub())
        self.L("decide", iid, "--question", "Ship it?", "--recommendation", "Yes", "--kind", "taste", "--tried", "checked the code and the deployment")
        self.assertIn("- **W1 first item**: Ship it? Recommendation: Yes", self.pub())
        self.L("resolve", iid, "--answer", "yes")
        self.assertNotIn("Open decisions", self.pub())
        self.L("set", iid, "--state", "done")
        self.assertIn("## Recently closed", self.pub())
        self.assertIn("| W1 | done | first item |  | tests OK |", self.pub())  # a closed item has no next
        self.assertIn("Nothing in flight.", self.pub())

    def test_header_names_the_machine_and_time(self):
        self.L("add", "--title", "t", "--objective", "o")
        head = self.pub().splitlines()[:2]
        self.assertEqual(head[0], f"# Supervisor (example) on {socket.gethostname()}")
        self.assertRegex(head[1], r"^_Generated \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ by ledger\.py ")

    def test_excludes_notes_worker_text_and_everything_local(self):
        iid = self.L("add", "--title", "t", "--objective", "OBJECTIVE-MARK", "--criteria", "CRITERIA-MARK",
                     "--owner", "SESSION-MARK", "--repo", "/REPO-MARK").stdout.strip()
        self.L("set", iid, "--note", "WORKER-OUTPUT-MARK pasted from a worker")
        self.L("decide", iid, "--question", "q", "--recommendation", "r", "--kind", "taste",
               "--tried", "TRIED-MARK, probed the deployment")
        self.L("resolve", iid, "--answer", "ANSWER-MARK")
        self.event(text="EVENT-MARK from events.jsonl")
        self.L("render")
        text = self.pub()
        for mark in ("OBJECTIVE", "CRITERIA", "SESSION", "REPO", "WORKER-OUTPUT", "ANSWER", "EVENT", "TRIED"):
            self.assertNotIn(f"{mark}-MARK", text)
        with open(os.path.join(ledger.STATE_DIR, "ledger.md")) as f:
            self.assertIn("SESSION-MARK", f.read())  # the local view keeps it

    def test_truncates_long_fields_and_keeps_tables_intact(self):
        iid = self.L("add", "--title", "T" * 500, "--objective", "o").stdout.strip()
        self.L("set", iid, "--next", "N" * 500, "--evidence", "line one\nline | two " + "E" * 500)
        self.L("decide", iid, "--question", "Q" * 900, "--recommendation", "R" * 900, "--kind", "taste", "--tried", "checked the code and the deployment")
        text = self.pub()
        for ch, limit in (("T", 120), ("N", 200), ("Q", 300), ("R", 300)):
            run = max(len(m) for m in re.findall(f"{ch}+", text))
            self.assertEqual(run, limit - 1, ch)
        self.assertIn("line one line / two " + "E" * (240 - 21) + "\u2026", text)
        row = [l for l in text.splitlines() if l.startswith("| W1 ")][0]
        self.assertEqual(row.count("|"), 6)

    def test_redacts_credential_shapes(self):
        iid = self.L("add", "--title", "t", "--objective", "o").stdout.strip()
        self.L("set", iid, "--evidence", "a ghp_abcdefghijklmnop1234 b sk-ant-api03-AAAAAAAAAAAAAAAA "
               "c ASIAIOSFODNN7EXAMPLE d xapp-1-A0123456789-abcdefghijk e sk-benchmark-v1")
        self.assertIn("a [redacted] b [redacted] c [redacted] d [redacted] e sk-benchmark-v1", self.pub())

    def test_markup_in_a_field_cannot_reshape_the_page(self):
        iid = self.L("add", "--title", "<details><summary>x", "--objective", "o").stdout.strip()
        self.L("decide", iid, "--question", "<!-- hide everything below", "--recommendation", "a & b", "--kind", "taste", "--tried", "checked the code and the deployment")
        text = self.pub()
        self.assertNotIn("<details>", text)
        self.assertNotIn("<!--", text)
        self.assertIn("&lt;!-- hide everything below Recommendation: a &amp; b", text)
        ledger.append({"op": "set", "id": iid, "state": "working | injected"})  # a hand-edited log
        self.L("render")
        row = [l for l in self.pub().splitlines() if l.startswith("| W1 ")][0]
        self.assertEqual(row.count("|"), 6)

    def test_only_recent_closed_items_are_published(self):
        for i in range(22):
            self.L("add", "--title", f"c{i}", "--objective", "o")
        for i in range(1, 23):
            ledger.append({"op": "set", "id": f"W{i}", "state": "done", "ts": 1000 + i})
        self.L("render")
        text = self.pub()
        self.assertIn("| W22 | done | c21 |", text)
        self.assertNotIn("| W2 | done |", text)
        self.assertIn("2 older closed item(s) are in the local ledger only.", text)

    def test_an_unchanged_ledger_does_not_rewrite_the_file(self):
        self.L("add", "--title", "t", "--objective", "o")
        lines = self.pub().split("\n")
        lines[1] = re.sub(r"\d{4}-\d\d-\d\dT[\d:]+Z", "2000-01-01T00:00:00Z", lines[1])
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.L("render")
        self.assertIn("2000-01-01T00:00:00Z", self.pub())  # only the time would change: left alone
        self.L("set", "W1", "--next", "changed")
        self.assertNotIn("2000-01-01T00:00:00Z", self.pub())

    def test_a_hand_edited_header_is_rewritten(self):
        self.L("add", "--title", "t", "--objective", "o")
        lines = self.pub().split("\n")
        lines[2] = "owner: SESSION-SECRET"
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.L("render")
        self.assertNotIn("SESSION-SECRET", self.pub())

    def test_a_bad_profile_is_reported_and_the_write_still_lands(self):
        with open(os.path.join(ledger.STATE_DIR, "profile"), "wb") as f:
            f.write(b"\xff\xfe example\n")
        r = self.L("add", "--title", "t", "--objective", "o", check=False)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "W1"), r.stderr)
        self.assertIn("could not publish the summary", r.stderr)
        with open(os.path.join(ledger.STATE_DIR, "profile"), "w") as f:
            f.write("example\nother\n")
        r = self.L("set", "W1", "--next", "n", check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("which has no profiles/<name>.md", r.stderr)
        self.assertFalse(os.path.exists(self.path))

    def test_add_reports_its_id_even_when_rendering_crashes(self):
        # A retry after a silent failure would create a duplicate item, so the id comes first.
        self.L("add", "--title", "t", "--objective", "o")
        ledger.append({"op": "set", "id": "W1", "ts": "not a number"})  # a hand-edited log
        r = self.L("add", "--title", "t2", "--objective", "o", check=False)
        self.assertEqual(r.stdout.strip(), "W2")
        self.assertIn("TypeError", r.stderr)

    def test_render_waits_for_the_ledger_lock(self):
        self.L("add", "--title", "t", "--objective", "o")
        with ledger.write_lock():
            p = subprocess.Popen([sys.executable, "-I", os.path.join(SCRIPTS, "ledger.py"), "render"],
                                 stdout=subprocess.DEVNULL, env={**os.environ, "HOME": HOME})
            time.sleep(0.5)
            self.assertIsNone(p.poll())  # blocked behind the writer
        self.assertEqual(p.wait(timeout=10), 0)

    def test_a_failed_publish_does_not_fail_the_ledger_write(self):
        os.makedirs(os.path.join(HOME, ".claude"), exist_ok=True)
        blocker = os.path.join(HOME, ".claude", "supervisors")
        with open(blocker, "w") as f:
            f.write("a file where the directory should be")
        self.addCleanup(os.remove, blocker)
        r = self.L("add", "--title", "t", "--objective", "o", check=False)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "W1"), r.stderr)
        self.assertIn("could not publish the summary", r.stderr)
        self.assertIn("W1", ledger.fold())

    def test_an_interrupted_write_leaves_no_temp_file(self):
        self.L("add", "--title", "t", "--objective", "o")
        with mock.patch.object(ledger.os, "replace", side_effect=OSError("disk full")):
            ledger.render({**ledger.fold(), "W9": {**ledger.fold()["W1"], "id": "W9"}})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["example.md"])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_temp_files_are_gitignored_and_the_summary_is_not(self):
        """config/gitignore.snippet, applied to a scratch repo the way it is applied to ~/.claude."""
        snippet = os.path.join(REAL, "config", "gitignore.snippet")
        if not os.path.exists(snippet):
            self.skipTest("run from the package to test config/gitignore.snippet (it is not installed)")
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, True)
        subprocess.run(["git", "init", "-q", repo], check=True)
        shutil.copy(snippet, os.path.join(repo, ".gitignore"))
        def ignored(rel):
            return subprocess.run(["git", "-C", repo, "check-ignore", "-q", rel]).returncode == 0
        self.assertTrue(ignored("state/supervisor/ledger.jsonl"))  # control: the instrument can say yes
        self.assertTrue(ignored("supervisors/example.md.123.tmp"))
        self.assertFalse(ignored("supervisors/example.md"))

    def test_init_refuses_the_unfilled_template(self):
        r = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "ledger.py"), "init", "--profile", "example"],
                           capture_output=True, text=True, env={**os.environ, "HOME": HOME})
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid choice", r.stderr)

    def test_a_profile_is_any_profiles_md_with_a_plain_name(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for name in ("alpha.md", "beta-2.md", "Bad Name.md", "../x.md", "notes.txt", ".hidden.md"):
            if "/" not in name:
                open(os.path.join(d, name), "w").close()
        with mock.patch.object(ledger, "PROFILE_DIR", d):
            self.assertEqual(ledger.profile_names(), ["alpha", "beta-2"])
        with mock.patch.object(ledger, "PROFILE_DIR", os.path.join(d, "missing")):
            self.assertEqual(ledger.profile_names(), [])
        self.assertIn("example", ledger.profile_names())  # the shipped template is a usable profile

    def test_init_refuses_a_profile_with_no_file(self):
        r = self.L("init", "--profile", "no-such-profile", check=False)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(HOME, "supervisor")))

    def test_nothing_is_published_before_init(self):
        os.remove(os.path.join(ledger.STATE_DIR, "profile"))
        self.L("add", "--title", "t", "--objective", "o")
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(os.listdir(os.path.join(HOME, ".claude")).count("supervisors"), 0)



class Catchup(Base):
    """fleet.py catchup: the cut-off is the user's last typed message (meta.sentFrom from an app),
    never a script's or a peer's (sentFrom cli), and the report groups ledger changes since."""

    @staticmethod
    def msg(at, sent, text="t", truncate=False):
        raw = {"role": "user", "content": {"type": "text", "text": text}, "meta": {"sentFrom": sent}}
        return {"createdAt": at * 1000, "text": text, "raw": json.dumps(raw)[:90] if truncate else raw}

    def pages(self, *pages):
        """Stub happier_call with transcript pages, newest first, as Happier serves them."""
        calls = []
        def call(args, timeout=30):
            calls.append(json.loads(args[args.index("--input-json") + 1]))
            items, more = pages[len(calls) - 1]
            return {"result": {"ok": True, "items": items, "hasMore": more,
                               "nextCursor": "c%d" % len(calls) if more else None}}, None
        fleet.happier_call = call
        return calls

    def test_sent_from_reads_meta_and_survives_a_truncated_raw(self):
        self.assertEqual(fleet.sent_from(self.msg(1, "android")), "android")
        self.assertEqual(fleet.sent_from(self.msg(1, "web", truncate=True)), "web")
        self.assertIsNone(fleet.sent_from({"raw": None}))

    def test_skips_scripts_and_peers_and_pages_back_to_the_user(self):
        calls = self.pages(([self.msg(50, "cli"), self.msg(60, "happier"), self.msg(70, "cli")], True),
                           ([self.msg(10, "web", "old"), self.msg(20, "android", "newest user"), self.msg(30, "cli")], True))
        item, err = fleet.last_user_message("me")
        self.assertEqual((item["text"], err), ("newest user", None))
        self.pages(([self.msg(50, "cli")], True), ([self.msg(10, "web", "old"), self.msg(20, "android", "newest user")], False))
        item, err = fleet.last_user_message("me", skip=1)  # catchup's own request is the newest
        self.assertEqual((item["text"], err), ("old", None))
        self.assertEqual(len(calls), 2)
        self.assertNotIn("cursor", calls[0])
        self.assertEqual(calls[1]["cursor"], "c1")
        self.assertEqual(calls[0]["roles"], ["user"])

    def test_no_user_message_and_unreadable_transcript(self):
        self.pages(([self.msg(1, "cli")], False))
        self.assertEqual(fleet.last_user_message("me"), (None, None))
        fleet.happier_call = lambda args, timeout=30: (None, "session_not_found")
        self.assertEqual(fleet.last_user_message("me"), (None, "session_not_found"))

    def write(self, *entries):
        with open(ledger.LOG, "a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_fold_until_is_the_ledger_as_it_stood(self):
        self.write({"ts": 100, "op": "add", "id": "W1", "title": "a"},
                   {"ts": 200, "op": "set", "id": "W1", "state": "done"})
        self.assertEqual(ledger.fold(until=150)["W1"]["state"], "queued")
        self.assertEqual(ledger.fold()["W1"]["state"], "done")

    @staticmethod
    def sections(report):
        return dict(re.findall(r"== ([A-Z ]+)\n((?:(?!==).*\n?)*)", report))

    def test_report_groups_changes_since_the_cutoff(self):
        self.write({"ts": 100, "op": "add", "id": "W1", "title": "finished one"},
                   {"ts": 100, "op": "set", "id": "W1", "state": "working"},
                   {"ts": 100, "op": "add", "id": "W2", "title": "old decision"},
                   {"ts": 110, "op": "decide", "id": "W2", "question": "red?", "recommendation": "yes"},
                   {"ts": 100, "op": "add", "id": "W3", "title": "quiet one"},
                   {"ts": 100, "op": "add", "id": "W5", "title": "waits on the user"},
                   {"ts": 300, "op": "set", "id": "W5", "state": "working", "next": "User to send the draft"},
                   {"ts": 300, "op": "set", "id": "W1", "state": "done", "evidence": "PR 7 merged"},
                   {"ts": 310, "op": "add", "id": "W4", "title": "new one"})
        report = fleet.format_rows(*fleet.ledger_rows(200))
        sections = self.sections(report)
        self.assertIn("W1 [working -> done] finished one", sections["FINISHED"])
        self.assertIn("evidence: PR 7 merged", sections["FINISHED"])
        self.assertIn("W2", sections["NEEDS YOU"])
        self.assertIn("open since before your last message", sections["NEEDS YOU"])
        self.assertIn("W5 [queued -> working] waits on the user", sections["NEEDS YOU"])  # next is the user's action
        self.assertIn("W4 [queued] new one", sections["STARTED"])
        self.assertNotIn("W3", report.split("== UNCHANGED")[0])
        self.assertIn("UNCHANGED: 1 open item(s)", report)  # W3 only: W2 is listed under NEEDS YOU

    def test_user_action_is_an_action_not_a_quote(self):
        for nxt in ("User to SEND the chat draft", "waiting on the user's approval", "user's decision: lockfile PR",
                    "Needs user login"):
            self.assertTrue(fleet.USER_ACTION.match(nxt), nxt)
        for nxt in ("User 2026-10-19: 'install the CLI'. Worker installs it", "on next promotion: verify",
                    "username field renamed", ""):
            self.assertFalse(fleet.USER_ACTION.match(nxt), nxt)

    SUMMARY = """# Supervisor (example) on host
_Generated 2026-10-19T20:00:00Z by ledger.py; do not edit._

{dec}## In flight

| ID | State | Title | Next | Latest evidence |
| --- | --- | --- | --- | --- |
{rows}
"""

    def summary(self, rows, dec=""):
        return self.SUMMARY.format(rows="\n".join(rows), dec=dec)

    def test_parse_published_round_trips_ledgers_own_rendering(self):
        items = {"W7": {"id": "W7", "title": "a / b <x>", "state": "working", "next": "n | m", "evidence": ["e > f"],
                        "decisions": [{"question": "q?", "recommendation": "r", "answer": None, "asked": 1}],
                        "updated": 1}}
        parsed, dec = fleet.parse_published(ledger.publish_body(items))
        self.assertEqual(parsed["W7"], {"state": "working", "title": "a / b <x>", "next": "n / m", "evidence": "e > f"})
        self.assertIn("q?", dec["W7"][0])

    def test_published_rows_diff_a_peer_summary(self):
        old = self.summary(["| W1 | working | one | n | e1 |", "| W2 | working | two | n | e |", "| W3 | working | three | n | e |"])
        new = self.summary(["| W1 | done | one |  | e2 |", "| W2 | working | two | n | e |", "| W4 | queued | four | n |  |",
                            "| W3 | working | three | n | e |"],
                           dec="## Open decisions\n\n- **W3 three**: blue? Recommendation: yes\n\n")
        report = fleet.format_rows(*fleet.published_rows(old, new))
        sections = self.sections(report)
        self.assertIn("W1 [working -> done] one", sections["FINISHED"])
        self.assertIn("evidence: e2", sections["FINISHED"])
        self.assertIn("W4 [queued] four", sections["STARTED"])
        self.assertIn("W3", sections["NEEDS YOU"])
        self.assertIn("blue?", sections["NEEDS YOU"])
        self.assertIn("UNCHANGED: 1 open item(s)", report)  # W2
        self.assertNotIn("W2", report.split("== UNCHANGED")[0])
        # A peer with no summary at the cut-off (new machine): everything is new, nothing crashes.
        self.assertIn("W1", fleet.format_rows(*fleet.published_rows("", new)))

    def test_cutoff_skips_its_own_request_and_refuses_without_an_earlier_one(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(fleet.catchup(), 1)
        self.assertIn("--since", out.getvalue())
        self.pages(([self.msg(1, "cli"), self.msg(2, "web", "/catchup")], False))  # only the request itself
        with mock.patch.dict(os.environ, {"HAPPIER_SESSION_ID": "me"}), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(fleet.catchup(), 1)
        self.assertIn("before this request", out.getvalue())
        self.pages(([self.msg(1, "web", "earlier ask"), self.msg(2, "web", "/catchup")], False))
        with mock.patch.dict(os.environ, {"HAPPIER_SESSION_ID": "me"}), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(fleet.catchup(), 0)
        self.assertIn("earlier ask", out.getvalue())
        self.assertNotIn("/catchup", out.getvalue())

    def test_stale_peer_summary_is_flagged(self):
        now = fleet.parse_since("2026-10-20T00:30:00Z")
        self.assertEqual(fleet.stale_note("2026-10-19T23:03:22Z", now), "")
        self.assertIn("8h old", fleet.stale_note("2026-10-19T16:25:54Z", now))
        self.assertIn("no generation time", fleet.stale_note(None, now))

    def test_parse_since(self):
        self.assertEqual(fleet.parse_since("2026-10-19T17:45:30Z"), 1792431930)
        self.assertEqual(fleet.parse_since("2026-10-19T17:45:30"), 1792431930)
        self.assertEqual(fleet.parse_since("1792431930"), 1792431930)
        with self.assertRaises(ValueError):
            fleet.parse_since("yesterday")


class PlanGuard(Base):
    """fleet.py plan-guard: undo a plan mode nobody asked for, in supervisors and owned workers only."""

    def guard(self, meta=None, mode="plan", cwd="/work", me="w1", set_err=None):
        calls = []
        def call(args, timeout=30):
            calls.append(args)
            if args[0] == "actions":
                return ({"result": {"items": [{"raw": {"meta": meta}}]}}, None) if meta is not None else (None, "boom")
            return ({}, set_err)
        fleet.happier_call = call
        out = fleet.plan_guard({"permission_mode": mode, "cwd": cwd}, me)
        return (json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else None), calls

    def test_owned_worker_resumed_in_plan_is_restored_and_told(self):
        self.own("w1")
        text, calls = self.guard({"sentFrom": "android", "permissionMode": "safe-yolo"})
        self.assertIn(["set-permission-mode", "w1", "safe-yolo"], calls)
        self.assertIn("store safe-yolo again: done", text)
        self.assertIn("next process start", text)
        self.assertIn("do not write a plan file", text)

    def test_supervisor_dir_counts_without_ownership(self):
        text, _ = self.guard({"permissionMode": "safe-yolo"}, cwd=os.path.join(HOME, "supervisor"), me="sup")
        self.assertIn("the standing supervisor", text)

    def test_left_alone(self):
        self.own("w1")
        self.assertEqual(self.guard({"permissionMode": "safe-yolo"}, mode="auto"), (None, []))  # not in plan
        self.assertEqual(self.guard({"permissionMode": "safe-yolo"}, me="stranger"), (None, []))  # not ours
        self.assertEqual(self.guard({"permissionMode": "safe-yolo"}, me=None), (None, []))
        for asked in ({"agentModeId": "plan", "permissionMode": "safe-yolo"}, {"permissionMode": "plan"}):
            text, calls = self.guard(asked)
            self.assertIsNone(text)  # the user asked for plan: respect it
            self.assertFalse([c for c in calls if c[0] == "set-permission-mode"])
        text, calls = self.guard(None)  # transcript unreadable: cannot tell, leave it
        self.assertIsNone(text)

    def test_a_failed_restore_is_reported_not_hidden(self):
        self.own("w1")
        text, _ = self.guard({"permissionMode": "safe-yolo"}, set_err="session_not_found")
        self.assertIn("failed (session_not_found)", text)


class SyncStuck(Base):
    """watch wakes once when ~/.claude keeps local commits off origin/main past SYNC_STUCK."""

    def check(self, st, now, n):
        return fleet.sync_check(st, now, ahead=lambda: n)

    def test_wakes_once_per_stuck_stretch(self):
        st = {}
        self.assertEqual(self.check(st, 1000, 0), [])
        self.assertEqual(self.check(st, 1100, 2), [])          # starts the stretch
        self.assertEqual(self.check(st, 1100 + 900, 2), [])    # not yet past 15 min
        out = self.check(st, 1100 + 961, 3)
        self.assertEqual(len(out), 1)
        self.assertIn("config sync stuck: 3 local", out[0]["change"])
        self.assertEqual(self.check(st, 1100 + 2000, 3), [])   # fired once
        self.assertEqual(self.check(st, 1100 + 2100, 0), [])   # pushed: clears
        self.assertEqual(self.check(st, 1100 + 2200, 1), [])   # a new stretch starts over
        self.assertEqual(len(self.check(st, 1100 + 3200, 1)), 1)

    def test_throttled_and_unreadable_is_quiet(self):
        st = {}
        self.check(st, 1000, 5)
        calls = []
        fleet.sync_check(st, 1030, ahead=lambda: calls.append(1) or 5)
        self.assertEqual(calls, [])  # within SYNC_EVERY: git not run
        self.assertEqual(self.check({}, 5000, None), [])

    def test_a_git_timeout_is_a_backstop_error_not_a_dead_watcher(self):
        """2026-10-21: rev-list timed out under I/O thrash and killed one machine's watcher."""
        def slow(*a, **k):
            raise subprocess.TimeoutExpired(a[0], 30)
        with mock.patch.object(fleet.subprocess, "run", slow):
            self.assertIsNone(fleet.git_out("log"))
            with self.assertRaises(RuntimeError):
                fleet.config_ahead()
            o = self.watch()
        self.assertEqual(o["wake"], "rearm")
        self.assertTrue(any("config sync check skipped" in e and "TimeoutExpired" in e for e in o["backstop_errors"]))
        st, errors = {"sync": {"since": 100, "checked": 0}}, []
        self.assertEqual(fleet.sync_check(st, 5000, ahead=slow, errors=errors), [])
        self.assertEqual((st["sync"]["since"], len(errors)), (100, 1))  # the stretch is kept

    def test_watch_reports_it(self):
        fleet.config_ahead = lambda: 4
        st_path = fleet.WATCH_STATE
        with open(st_path, "w") as f:
            json.dump({"sync": {"since": time.time() - 2000, "checked": 0}}, f)
        out = self.watch()
        self.assertIn("config sync stuck: 4", json.dumps(out["changes"]))


class HostAliases(Base):
    """A recreated agent container gets a new hostname; its Happier sessions keep the old one (2026-10-20)."""

    def test_learns_the_old_host_from_its_own_session_and_remembers_it(self):
        with open(fleet.SELF_ID, "w") as f:
            f.write("sup")
        hosts = fleet.local_hosts([{"id": "sup", "host": "old-container-id"}, {"id": "w", "host": "elsewhere"}])
        self.assertEqual(hosts, {socket.gethostname(), "old-container-id"})
        os.remove(fleet.SELF_ID)
        self.assertIn("old-container-id", fleet.local_hosts())  # persisted in host-aliases
        self.assertNotIn("elsewhere", fleet.local_hosts())

    def test_inventory_counts_old_host_sessions_as_local(self):
        listing = {"ok": True, "data": {"sessions": [
            {"id": "sup", "host": "old-container-id", "path": "/home/alice/supervisor", "title": "(alpha) Supervisor"},
            {"id": "w1", "host": "old-container-id", "path": "/work/x", "title": "W1"},
            {"id": "p1", "host": "other-host", "path": "/home/bob/supervisor", "title": "(beta) Supervisor"}]}}
        fleet.run_json = lambda cmd, timeout=30, label=None: (listing, None) if "session" in cmd else ([], None)
        fleet.inventory = REAL_INVENTORY
        with mock.patch.dict(os.environ, {"HAPPIER_SESSION_ID": "sup"}):
            sessions, errors = fleet.inventory(detail_local=False)
        self.assertEqual(errors, [])
        self.assertTrue(sessions["h:w1"]["local"])
        self.assertFalse(sessions["h:p1"]["local"])


class ContextHint(Base):
    def test_every_wake_reports_context_and_flags_compaction_past_the_threshold(self):
        fleet.own_context_tokens = lambda: 540_262
        o = self.watch()
        self.assertEqual((o["wake"], o["context_tokens"], o["compact"]), ("rearm", 540_262, True))
        fleet.own_context_tokens = lambda: 150_000
        self.assertFalse(self.watch()["compact"])
        fleet.own_context_tokens = lambda: None
        self.assertFalse(self.watch()["compact"])

    def test_reads_the_latest_usage_from_its_own_transcript(self):
        d = os.path.join(HOME, ".claude", "projects", "-x")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "abc.jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1}}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 300000,
                                                                           "cache_creation_input_tokens": 20}}}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
        fleet.run_json = lambda cmd, timeout=30, label=None: ([{"kind": "interactive", "pid": 1, "sessionId": "abc"}], None)
        fleet.happier_id_of = lambda pid: "me"
        self.addCleanup(shutil.rmtree, d)
        with mock.patch.dict(os.environ, {"HAPPIER_SESSION_ID": "me"}):
            self.assertEqual(REAL_OWN_CONTEXT(), 300025)
        fleet.happier_id_of = lambda pid: "someone else"
        with mock.patch.dict(os.environ, {"HAPPIER_SESSION_ID": "me"}):
            self.assertIsNone(REAL_OWN_CONTEXT())


class Doorbell(Base):
    """The user, 2026-10-21: a quiet supervisor must not cost tokens. A quiet arm parks into a
    detached doorbell that rings the supervisor (one Happier message) only on a real wake."""

    def setUp(self):
        super().setUp()
        self.sent, self.spawned = [], []
        fleet.happier_call = lambda args, timeout=30: self.sent.append(args) or ({}, None)
        fleet.session_info = lambda sid: ({"id": sid, "active": True, "archivedAt": None}, None)
        env = mock.patch.dict(os.environ, {"HAPPIER_SESSION_ID": "me"})
        env.start(); self.addCleanup(env.stop)
        real = subprocess.Popen  # the watcher's git calls (sync_check) still need the real one
        popen = mock.patch.object(fleet.subprocess, "Popen", lambda argv, **kw: self.spawned.append((argv, kw))
                                  if "--doorbell" in argv else real(argv, **kw))
        popen.start(); self.addCleanup(popen.stop)

    def doorbell(self, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fleet.watch(poll=0.01, backstop=kw.pop("backstop", 0), max_wait=0.05, doorbell=True, **kw)
        return buf.getvalue()

    def test_a_quiet_arm_parks_into_a_detached_doorbell(self):
        o = self.watch(limits={"busy_max": 11, "idle_max": 22, "pending_max": 33})
        self.assertEqual(o["wake"], "parked")
        argv, kw = self.spawned[0]
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(argv[3:5], ["watch", "--doorbell"])
        self.assertEqual(argv[argv.index("--busy-max") + 1:][:1] + argv[argv.index("--pending-max") + 1:][:1], ["11", "33"])

    def test_parks_argv_is_what_the_cli_routes_to_the_doorbell(self):
        self.watch()
        argv, _ = self.spawned[0]
        calls = []
        fleet.watch = lambda *a, **kw: calls.append((a, kw))
        with mock.patch.object(sys, "argv", argv[2:]), mock.patch.object(fleet.signal, "signal"):
            fleet.main()
        (a, kw), = calls
        self.assertEqual((kw, a[3]["idle_max"]), ({"doorbell": True}, 1200))

    def test_no_session_to_ring_stays_rearm(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.watch()["wake"], "rearm")
        self.assertEqual(self.spawned, [])

    def test_busy_or_waiting_work_stays_rearm(self):
        self.own()
        rec = {"source": "happier", "id": "own1", "title": "t", "host": "h", "local": True, "pending": 0, "supervisor": False}
        for status in ("busy", "waiting"):
            fleet.inventory = lambda detail_local=True, status=status: ({"h:own1": {**rec, "status": status}}, [])
            self.watch()  # newly waiting is itself a wake; the arm after it is the quiet one
            self.assertEqual(self.watch()["wake"], "rearm", status)
        self.assertEqual(self.spawned, [])

    def test_a_wake_rings_once_and_the_tracked_arm_delivers_it_once(self):
        self.event(event="Notification", type="permission_prompt", session="z")
        self.assertEqual(self.doorbell(), "")
        (args,), = [self.sent]
        self.assertEqual(args[:2], ["send", "me"])
        self.assertIn("(1 event(s), 0 change(s))", args[2])
        self.assertFalse(os.path.exists(fleet.DOORBELL_PID))
        calls = []
        fleet.inventory = lambda detail_local=True: calls.append(1) or ({}, [])
        o = self.watch()
        self.assertEqual((o["wake"], o["events"][0]["type"], calls), ("event", "permission_prompt", []))  # no poll first
        self.assertEqual(self.watch()["wake"], "parked")  # delivered once, not again

    def test_a_wake_only_its_clocks_knew_survives_the_handover(self):
        fleet.SYNC_STUCK, fleet.SYNC_EVERY = 0, 0  # the doorbell has watched the unpushed commits long enough
        fleet.config_ahead = lambda: 2
        self.doorbell()
        self.assertEqual(len(self.sent), 1)
        fleet.SYNC_STUCK = 900  # the tracked arm's own clock would only start now
        o = self.watch()
        self.assertEqual(o["wake"], "event")
        self.assertIn("config sync stuck", o["changes"][0]["change"])

    def test_it_outlasts_max_wait_and_rings_on_the_first_real_wake(self):
        t = threading.Thread(target=self.doorbell, kwargs={"backstop": 1e9})
        t.start()
        time.sleep(0.4)  # eight times max_wait: a tracked arm would have exited long ago
        self.assertEqual((t.is_alive(), self.sent), (True, []))
        self.event(event="Notification", type="permission_prompt", session="z")
        t.join(5)
        self.assertEqual((t.is_alive(), len(self.sent)), (False, 1))

    def test_it_exits_quietly_once_its_supervisor_is_gone(self):
        for gone in [(None, "session_not_found"), ({"active": False, "archivedAt": None}, None),
                     ({"active": True, "archivedAt": 123}, None)]:
            fleet.session_info = lambda sid, gone=gone: gone
            self.doorbell()
        self.assertEqual(self.sent, [])

    def test_ring_retries_through_an_outage_and_stops_for_a_dead_session(self):
        results = iter([(None, "timeout"), (None, "timeout"), ({}, None)])
        fleet.happier_call = lambda args, timeout=30: next(results)
        fleet.session_info = lambda sid: (None, "timeout")  # relay down: unknown, keep trying
        naps = []
        self.assertTrue(fleet.ring("me", 1, 0, sleep=naps.append))
        self.assertEqual(len(naps), 2)
        fleet.happier_call = lambda args, timeout=30: (None, "timeout")
        fleet.session_info = lambda sid: (None, "session_not_found")
        self.assertFalse(fleet.ring("me", 1, 0, sleep=naps.append))

    def test_a_tracked_arm_retires_the_doorbell_and_takes_the_lock(self):
        code = ("import fcntl,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f, fcntl.LOCK_EX); "
                "open(sys.argv[1]+'.ready','w').close(); time.sleep(60)")
        child = REAL_POPEN([sys.executable, "-c", code, fleet.WATCH_LOCK, "/x/fleet.py", "--doorbell"])
        self.addCleanup(child.kill)
        for _ in range(100):
            if os.path.exists(fleet.WATCH_LOCK + ".ready"):
                break
            time.sleep(0.05)
        with open(fleet.DOORBELL_PID, "w") as f:
            f.write(str(child.pid))
        self.assertEqual(self.watch()["wake"], "parked")  # took the lock, ran a whole quiet arm
        self.assertIsNotNone(child.wait(5))

    def test_a_held_lock_that_is_not_a_doorbell_still_reads_already_armed(self):
        import fcntl
        with open(fleet.DOORBELL_PID, "w") as f:
            f.write(str(os.getpid()))  # a stale pid file naming a live non-doorbell process
        with open(fleet.WATCH_LOCK, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.watch()["wake"], "already-armed")

    def test_a_doorbell_that_cannot_get_the_lock_leaves_it_to_the_tracked_arm(self):
        import fcntl
        fleet.DOORBELL_START = 0.2
        with open(fleet.WATCH_LOCK, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.event(event="Notification", type="permission_prompt", session="z")
            self.doorbell()
        self.assertEqual(self.sent, [])

if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
