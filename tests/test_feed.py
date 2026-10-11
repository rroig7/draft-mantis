"""Draft-room feed (Firefox add-on) tests: parsing, merge into the league snapshot, server endpoint + event stream.

Run: python3 -m unittest discover tests
"""

import json
from pathlib import Path
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

os.environ.setdefault("MANTIS_HOME", tempfile.mkdtemp())

from mantis import draftfeed, espn_league, server  # noqa: E402
from test_league import SWID, fake_league  # noqa: E402


class FeedParseTests(unittest.TestCase):
    def test_parse_and_dedupe_replay(self):
        f = draftfeed.LeagueFeed(777)
        for line in ["SELECTING 3 30000", "SELECTED 3 3112335 1", "SELECTED 1 4278073 1", "CHATTER whatever",
                     "SELECTED 3 3112335 1", "SELECTED 2 -16014 1", "SELECTED x y"]:
            f.ingest(line)
        self.assertEqual([p["playerId"] for p in f.picks], [3112335, 4278073])  # replay and bad ids ignored
        self.assertIsNone(f.clock)  # a pick clears the clock
        f.ingest("SELECTING 4 45000")
        self.assertEqual(f.clock["teamId"], 4)
        self.assertEqual(f.commands["unparsed"], 1)
        self.assertTrue(f.active)

    def test_merge_overrides_empty_api_picks(self):
        snap = espn_league.normalize(fake_league(), SWID)
        snap["draft"]["picks"] = []  # what ESPN's read API reports mid-draft
        snap["owners"] = {}
        for t in snap["teams"]:
            t["roster"] = []
        f = draftfeed.LeagueFeed(777)
        for line in ["SELECTED 3 111 1", "SELECTED 1 222 1", "SELECTED 4 333 1", "SELECTING 2 30000"]:
            f.ingest(line)
        out = draftfeed.merge(snap, f)
        self.assertEqual([p["pick"] for p in out["draft"]["picks"]], [1, 2, 3])
        self.assertEqual(out["owners"]["333"], 4)
        self.assertIn(333, next(t for t in out["teams"] if t["id"] == 4)["roster"])
        self.assertEqual(out["draft"]["clock"]["teamId"], 2)
        self.assertTrue(out["draft"]["inProgress"])
        self.assertTrue(out["feed"]["active"])

    def test_merge_without_feed(self):
        snap = espn_league.normalize(fake_league(), SWID)
        self.assertFalse(draftfeed.merge(snap, None)["feed"]["active"])


class LeagueSwitchServerTests(unittest.TestCase):
    """Switching the connected league (or your team) must not serve the previous league's season or moves."""

    @classmethod
    def setUpClass(cls):
        from mantis import moves, season as season_mod
        raws = {777: fake_league(), 888: {**fake_league(), "id": 888, "settings": {**fake_league()["settings"], "name": "Other League"}}}
        cls.calls = []

        def fake_run(root, sims=1500, **k):
            sel = espn_league.load_store()["league"]
            cls.calls.append(("season", sel["id"]))
            snap = server._league_snapshot()
            return {"leagueKey": espn_league.league_key(sel), "leagueId": sel["id"], "myTeamId": snap["myTeamId"],
                    "dataGenerated": server._data_generated(), "teams": [], "generated": "x"}

        def fake_moves(root, whatif=None):
            sel = espn_league.load_store()["league"]
            cls.calls.append(("moves", sel["id"]))
            return {"leagueKey": espn_league.league_key(sel), "myTeamId": server._league_snapshot()["myTeamId"]}

        import shutil
        from mantis import site
        cls.tmp = Path(tempfile.mkdtemp())          # never write fake leagues into the real data/ folder
        (cls.tmp / "data").mkdir()
        shutil.copy(site.ROOT / "data" / "players.json", cls.tmp / "data")
        cls.patches = [mock.patch.object(site, "ROOT", cls.tmp),
                       mock.patch.object(espn_league, "fetch_league", side_effect=lambda i, s, c=None: raws[i]),
                       mock.patch.object(season_mod, "run", side_effect=fake_run),
                       mock.patch.object(moves, "build", side_effect=fake_moves)]
        for p in cls.patches:
            p.start()
        espn_league.save_store({"swid": SWID, "espn_s2": "x" * 100})
        server.State.cache = None
        server.State.moves_cache = None
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        for p in cls.patches:
            p.stop()

    def req(self, method, path, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read())

    def test_switch_league_and_team(self):
        self.req("POST", "/api/league", {"id": 777, "season": 2027})
        self.assertEqual(self.req("GET", "/api/season")["leagueKey"], "777-2027")
        self.assertEqual(self.req("GET", "/api/moves")["leagueKey"], "777-2027")
        self.assertEqual(self.req("GET", "/api/season")["leagueKey"], "777-2027")  # cached: no second run
        self.assertEqual(self.calls.count(("season", 777)), 1)

        snap = self.req("POST", "/api/league", {"id": 888, "season": 2027})          # switch leagues
        self.assertEqual(snap["leagueKey"], "888-2027")
        self.assertEqual(snap["league"]["name"], "Other League")
        self.assertEqual(self.req("GET", "/api/season")["leagueKey"], "888-2027")
        self.assertEqual(self.req("GET", "/api/moves")["leagueKey"], "888-2027")      # moves cache did not leak

        snap = self.req("POST", "/api/myteam", {"teamId": 2})                        # pick a different team
        self.assertEqual(snap["myTeamId"], 2)
        self.assertEqual(self.req("GET", "/api/season")["myTeamId"], 2)               # season rebuilt for that team
        self.assertEqual(self.req("GET", "/api/moves")["myTeamId"], 2)

        self.req("POST", "/api/league", {"id": 777, "season": 2027})                  # back to the first league
        self.assertEqual(self.req("GET", "/api/league")["myTeamId"], 4)               # its own team, not 888's pick
        self.assertEqual(self.req("GET", "/api/season")["leagueKey"], "777-2027")


class FeedServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        draftfeed.HUB.feeds.clear()
        cls.patch = mock.patch.object(espn_league, "fetch_league", side_effect=lambda i, s, c=None: fake_league())
        cls.patch.start()
        espn_league.save_store({"league": {"id": 777, "season": 2027}, "swid": SWID, "espn_s2": "x" * 100})
        server.State.cache = None
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.patch.stop()

    def post(self, path, body, origin=None):
        h = {"Content-Type": "application/json"}
        if origin:
            h["Origin"] = origin
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_feed_endpoint_and_merge(self):
        code, body = self.post("/api/draftfeed", {"leagueId": 777, "teamId": 4,
                                                   "lines": ["SELECTED 3 3112335 1", "SELECTING 1 30000"]},
                               origin="moz-extension://1234-abcd")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(body["picks"], 1)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/league") as r:
            snap = json.loads(r.read())
        self.assertTrue(snap["feed"]["active"])
        self.assertEqual(snap["draft"]["clock"]["teamId"], 1)
        self.assertEqual(snap["owners"]["3112335"], 3)

    def test_extension_origin_only_for_feed(self):
        code, _ = self.post("/api/credentials", {"swid": SWID, "espn_s2": "C" * 120}, origin="moz-extension://1234-abcd")
        self.assertEqual(code, 403)
        code, _ = self.post("/api/draftfeed", {"leagueId": 777, "lines": ["SELECTED 1 5 1"]}, origin="https://evil.example")
        self.assertEqual(code, 403)
        code, _ = self.post("/api/draftfeed", {"leagueId": 777, "lines": "SELECTED 1 5 1"})
        self.assertEqual(code, 400)

    def test_event_stream_pushes_on_pick(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: localhost\r\n\r\n")
        buf = b""
        while b"retry:" not in buf:
            buf += sock.recv(4096)
        self.post("/api/draftfeed", {"leagueId": 777, "lines": ["SELECTED 2 4278073 1"]})
        while b"data:" not in buf:
            buf += sock.recv(4096)
        sock.close()
        self.assertIn(b"text/event-stream", buf)


if __name__ == "__main__":
    unittest.main()
