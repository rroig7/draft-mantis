"""Offline tests for the ESPN league connection: normalization + local server, using a fake ESPN league.

Run: python3 -m unittest discover tests
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("MANTIS_HOME", tempfile.mkdtemp())  # never touch the real ~/.draft-mantis

from mantis import espn_league, server  # noqa: E402

SWID = "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"


def fake_league(points: bool = False) -> dict:
    """Shape of lm-api-reads .../leagues/{id}?view=mSettings&view=mTeam&view=mRoster&view=mDraftDetail."""
    ids = [p["id"] for p in json.loads((ROOT / "data" / "players.json").read_text(encoding="utf-8"))["players"][:12]]
    if points:
        items = [{"statId": 0, "points": 1}, {"statId": 6, "points": 1.2}, {"statId": 3, "points": 1.5},
                 {"statId": 2, "points": 3}, {"statId": 1, "points": 3}, {"statId": 11, "points": -1},
                 {"statId": 37, "points": 5}]
        stype = "H2H_POINTS"
    else:
        items = [{"statId": s, "isReverseItem": s == 11} for s in (0, 6, 3, 2, 1, 17, 19, 20, 11, 21)]
        stype = "H2H_CATEGORY"
    picks = [{"overallPickNumber": i + 1, "roundId": 1 if i < 4 else 2, "teamId": t, "playerId": ids[i], "keeper": False}
             for i, t in enumerate([3, 1, 4, 2, 2])]
    teams = []
    for tid in (1, 2, 3, 4):
        owned = [p["playerId"] for p in picks if p["teamId"] == tid]
        teams.append({
            "id": tid, "abbrev": f"T{tid}", "name": f"Team {tid}",
            "owners": [SWID if tid == 4 else "{11111111-2222-3333-4444-55555555555%d}" % tid],
            "record": {"overall": {"wins": 0, "losses": 0, "ties": 0}},
            "roster": {"entries": [{"playerId": pid, "playerPoolEntry": {"player": {"fullName": f"P{pid}", "defaultPositionId": 5,
                                                                                   "eligibleSlots": [4, 9, 10, 11, 12, 13]}}} for pid in owned]},
        })
    return {
        "id": 777, "seasonId": 2027,
        "settings": {
            "name": "Mantis Test League", "size": 4,
            "scoringSettings": {"scoringType": stype, "scoringItems": items},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "11": 3, "12": 3, "13": 2},
                               "positionLimits": {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 4}},
            "draftSettings": {"type": "SNAKE", "pickOrder": [3, 1, 4, 2], "date": 1791900000000, "timePerSelection": 90},
        },
        "teams": teams,
        "draftDetail": {"inProgress": True, "drafted": False, "picks": picks},
    }


class NormalizeTests(unittest.TestCase):
    def test_categories_league(self):
        snap = espn_league.normalize(fake_league(), SWID.lower())
        lg = snap["league"]
        self.assertEqual(lg["format"], "cats")
        self.assertEqual(lg["cats"], ["pts", "reb", "ast", "stl", "blk", "tpm", "fgp", "ftp", "to"])
        self.assertEqual(lg["unsupported"], ["3P%"])
        self.assertEqual(lg["rosterSize"], 11)  # IR slot excluded
        self.assertEqual(lg["limits"], {"C": 4})  # positionLimits keyed by primary position id, 0 = none
        self.assertEqual(lg["lineup"], {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "11": 3, "12": 3})
        first = snap["draft"]["picks"][0]["playerId"]
        self.assertEqual(snap["positions"][str(first)], ["C", [4, 9, 10, 11]])  # bench/IR slots dropped
        self.assertEqual(snap["myTeamId"], 4)   # SWID match is case-insensitive
        self.assertEqual(len(snap["draft"]["picks"]), 5)
        self.assertEqual(snap["draft"]["pickOrder"], [3, 1, 4, 2])
        first = snap["draft"]["picks"][0]["playerId"]
        self.assertEqual(snap["owners"][str(first)], 3)

    def test_points_league(self):
        lg = espn_league.normalize(fake_league(points=True))["league"]
        self.assertEqual(lg["format"], "points")
        self.assertEqual(lg["points"]["stl"], 3)
        self.assertEqual(lg["points"]["to"], -1)
        self.assertEqual(lg["unsupported"], ["DD (+5)"])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patch = mock.patch.object(espn_league, "fetch_league", side_effect=lambda i, s, c=None: fake_league())
        cls.patch.start()
        espn_league.save_store({})  # start disconnected, whatever other test modules left behind
        server.State.cache = None
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.patch.stop()

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method, headers=headers or {},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_flow(self):
        code, html = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"const LIVE = true", html)

        code, body = self.call("GET", "/api/league")
        self.assertEqual(code, 409)

        code, body = self.call("POST", "/api/credentials", {"swid": "short", "espn_s2": "x"})
        self.assertEqual(code, 400)
        code, body = self.call("POST", "/api/credentials", {"swid": SWID, "espn_s2": "A" * 120})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["hasCredentials"])
        self.assertNotIn("A" * 120, body.decode())  # the cookie is never echoed back
        self.assertEqual(os.stat(espn_league.STORE).st_mode & 0o777, 0o600)

        code, body = self.call("POST", "/api/league", {"id": 777, "season": 2027})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["myTeamId"], 4)
        code, body = self.call("GET", "/api/league")
        self.assertEqual(json.loads(body)["league"]["name"], "Mantis Test League")

    def test_rejects_foreign_host_and_origin(self):
        code, _ = self.call("GET", "/api/status", headers={"Host": "evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.call("POST", "/api/credentials", {"swid": SWID, "espn_s2": "B" * 120},
                            headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main()
