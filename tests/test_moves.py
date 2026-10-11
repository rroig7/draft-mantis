"""Move tracking: transactions -> moves, suggestion matching, scenarios and what-ifs on a fake league.

Run: python3 -m unittest discover tests
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("MANTIS_HOME", tempfile.mkdtemp())

from mantis import espn_league, moves, season  # noqa: E402
import test_season as TS  # noqa: E402

ME = 7


def tx(id_, typ, status, items, proposed="2026-10-10 12:00", process=None):
    import datetime as dt
    ms = int(dt.datetime.strptime(proposed, "%Y-%m-%d %H:%M").timestamp() * 1000)
    return {"id": id_, "type": typ, "status": status, "isPending": status == "PENDING", "teamId": ME, "proposedDate": ms,
            "processDate": process, "items": [{"type": k, "playerId": p, "fromTeamId": f, "toTeamId": t} for k, p, f, t in items]}


class MoveLogicTests(unittest.TestCase):
    def test_normalize_skips_draft_and_lineup(self):
        self.assertIsNone(moves.normalize(tx("d", "DRAFT", "EXECUTED", [("DRAFT", 1, 0, ME)])))
        self.assertIsNone(moves.normalize(tx("l", "ROSTER", "EXECUTED", [("LINEUP", 1, ME, ME)])))
        m = moves.normalize(tx("w", "WAIVER", "PENDING", [("ADD", 5, 0, ME), ("DROP", 6, ME, 0)]))
        self.assertEqual([i["kind"] for i in m["items"]], ["ADD", "DROP"])

    def test_apply_and_reverse(self):
        r = {ME: [1, 2, 3], 2: [4, 5]}
        trade = moves.normalize(tx("t", "TRADE_ACCEPT", "EXECUTED", [("TRADE", 1, ME, 2), ("TRADE", 4, 2, ME)]))
        after = moves.apply(r, moves.move_transfers(trade))
        self.assertEqual(sorted(after[ME]), [2, 3, 4])
        self.assertEqual(sorted(after[2]), [1, 5])
        self.assertEqual({k: sorted(v) for k, v in moves.apply(after, moves.reverse(trade)).items()}, {ME: [1, 2, 3], 2: [4, 5]})
        self.assertEqual(moves.apply(r, [(9, ME, 0)]), r)  # stale move (player not there) is skipped

    def test_plan_status(self):
        plan = {"kind": "waiver", "add": 50, "drop": 3, "dMe": 0.1}
        known = [moves.normalize(tx("w", "WAIVER", "PENDING", [("ADD", 50, 0, ME)])),
                 moves.normalize(tx("r", "ROSTER", "EXECUTED", [("DROP", 3, ME, 0)]))]
        self.assertEqual(moves.plan_status(plan, known, {ME: [1, 2]}, ME), {"state": "pending", "parts": ["pending", "done"]})
        self.assertEqual(moves.plan_status(plan, [], {ME: [1, 2, 3]}, ME)["state"], "open")
        self.assertEqual(moves.plan_status(plan, known[1:], {ME: [1, 2]}, ME)["state"], "partial")
        self.assertEqual(moves.plan_status(plan, [], {ME: [1, 2, 50]}, ME)["state"], "done")  # older than ESPN's window


class ScenarioTests(unittest.TestCase):
    """Executed drop + pending waiver add on a fake 12-team league, end to end through moves.build()."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "data" / "raw").mkdir(parents=True)
        shutil.copy(TS.ROOT / "data" / "players.json", self.root / "data")
        shutil.copy(TS.ROOT / "data" / "raw" / "espn_proschedule.json", self.root / "data" / "raw")
        raw = TS.fake_raw(stacked_team=0)
        snap = espn_league.normalize(raw, "{ME}")
        mine = next(t for t in snap["teams"] if t["id"] == ME)["roster"]
        owned = {i for t in snap["teams"] for i in t["roster"]}
        free = [p for p in TS.PAYLOAD["players"] if p["id"] not in owned and p["team"] != "FA"]
        self.best_fa = max(free, key=lambda p: p["fp"])["id"]
        self.worst = min(mine, key=lambda i: next(p["fp"] for p in TS.PAYLOAD["players"] if p["id"] == i))
        # the drop already went through on ESPN, so the league roster no longer has him
        for t in raw["teams"]:
            if t["id"] == ME:
                t["roster"]["entries"] = [e for e in t["roster"]["entries"] if e["playerId"] != self.worst]
        self.raw = raw
        self.txs = [tx("r1", "ROSTER", "EXECUTED", [("DROP", self.worst, ME, 0)], "2026-10-10 10:47"),
                    tx("w1", "WAIVER", "PENDING", [("ADD", self.best_fa, 0, ME)], "2026-10-10 10:48", process=1791702000000)]
        # written where pre-league-folder versions kept it: the first build must move it into data/leagues/555-2027/
        (self.root / "data" / "plan.json").write_text(json.dumps({"waiver:x": {
            "kind": "waiver", "add": self.best_fa, "drop": self.worst, "dMe": 0.1, "dMeFirst": 0.1, "key": "waiver:x",
            "firstSeen": "2026-10-10 10:33", "lastSeen": "2026-10-10 10:33"}}), encoding="utf-8")
        espn_league.save_store({"league": {"id": 555, "season": 2027}, "swid": "{ME}", "espn_s2": "x" * 100})
        self.patches = [mock.patch.object(season, "fetch_raw", return_value=raw),
                        mock.patch.object(moves, "fetch_transactions", return_value=self.txs)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_scenarios_and_tracking(self):
        r = moves.build(self.root)
        sc = {s["key"]: s for s in r["scenarios"]}
        self.assertIn("empty roster spot", sc["now"]["detail"])
        self.assertGreater(sc["pending"]["me"]["playoffPct"], sc["now"]["me"]["playoffPct"])  # the claim helps
        self.assertGreaterEqual(sc["plan"]["me"]["playoffPct"], sc["pending"]["me"]["playoffPct"] - 1)
        tracked = r["tracked"][0]
        self.assertEqual(tracked["state"], "pending")  # drop done, add pending
        pend = next(m for m in r["moves"] if m["id"] == "w1")
        self.assertGreater(pend["impact"], 0)
        self.assertTrue(pend["suggested"])  # made after the suggestion was shown
        # what-if: also drop the best player on the roster -> worse than pending alone
        star = max(next(t for t in r["teams"] if t["id"] == ME)["roster"],
                   key=lambda i: next((p["fp"] for p in TS.PAYLOAD["players"] if p["id"] == i), 0))
        w = moves.build(self.root, whatif=[{"add": [], "drop": [star]}])
        self.assertLess(w["custom"]["me"]["playoffPct"], sc["pending"]["me"]["playoffPct"])
        print(f"\n  scenarios: draft {sc['draft']['me']['playoffPct']}% · now {sc['now']['me']['playoffPct']}% · "
              f"pending {sc['pending']['me']['playoffPct']}% · plan {sc['plan']['me']['playoffPct']}% · what-if {w['custom']['me']['playoffPct']}%")

    def test_suggestion_shown_after_move_is_not_credited(self):
        ldir = espn_league.league_dir(self.root, {"id": 555, "season": 2027})
        plan = json.loads((ldir / "plan.json").read_text(encoding="utf-8"))
        plan["waiver:x"]["firstSeen"] = "2026-10-10 11:30"   # shown after the claim was made
        (ldir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        r = moves.build(self.root)
        self.assertFalse(next(m for m in r["moves"] if m["id"] == "w1")["suggested"])


class LeagueSwitchTests(ScenarioTests):
    """Two leagues on one install: each keeps its own moves, suggestions and history."""

    test_scenarios_and_tracking = None                      # inherited setUp only, not these
    test_suggestion_shown_after_move_is_not_credited = None

    def test_switch_keeps_leagues_apart(self):
        a = moves.build(self.root)
        dir_a = self.root / "data" / "leagues" / "555-2027"
        self.assertTrue((dir_a / "plan.json").exists())          # legacy file moved into league A's folder
        self.assertFalse((self.root / "data" / "plan.json").exists())
        self.assertEqual(a["leagueKey"], "555-2027")
        # switch to league 888: same fake rosters, no transactions, no plan
        raw_b = json.loads(json.dumps(self.raw))
        raw_b["id"] = 888
        espn_league.save_store({"league": {"id": 888, "season": 2027}, "swid": "{ME}", "espn_s2": "x" * 100})
        with mock.patch.object(season, "fetch_raw", return_value=raw_b), mock.patch.object(moves, "fetch_transactions", return_value=[]):
            b = moves.build(self.root)
        self.assertEqual(b["leagueKey"], "888-2027")
        self.assertEqual(b["moves"], [])
        self.assertEqual(b["tracked"], [])                         # league A's suggestions do not leak
        self.assertTrue((self.root / "data" / "leagues" / "888-2027" / "transactions.json").exists())
        self.assertEqual(len(json.loads((dir_a / "transactions.json").read_text(encoding="utf-8"))), 2)  # A untouched

    def test_undrafted_league_has_no_outlook(self):
        raw = TS.fake_raw(stacked_team=0)
        raw["id"] = 999
        raw["draftDetail"] = {"drafted": False, "inProgress": False, "picks": []}
        for t in raw["teams"]:
            t["roster"]["entries"] = []
        snap = espn_league.normalize(raw, "{ME}")
        self.assertFalse(espn_league.rosters_ready(snap))
        espn_league.save_store({"league": {"id": 999, "season": 2027}, "swid": "{ME}", "espn_s2": "x" * 100})
        with mock.patch.object(season, "fetch_raw", return_value=raw), mock.patch.object(moves, "fetch_transactions", return_value=[]):
            with self.assertRaises(espn_league.EspnError) as cm:
                moves.build(self.root)
            self.assertIn("has not drafted yet", str(cm.exception))
            with self.assertRaises(espn_league.EspnError):
                season.run(self.root)
        self.assertFalse((self.root / "data" / "leagues" / "999-2027" / "history.jsonl").exists())  # nothing recorded

    def test_my_team_override(self):
        raw = TS.fake_raw(stacked_team=0)
        self.assertEqual(espn_league.normalize(raw, "{ME}")["myTeamId"], ME)
        snap = espn_league.normalize(raw, None, my_team_id=3)
        self.assertEqual(snap["myTeamId"], 3)
        self.assertEqual([t["id"] for t in snap["teams"] if t["isMine"]], [3])
        self.assertEqual(espn_league.normalize(raw, "{ME}", my_team_id=99)["myTeamId"], ME)  # unknown team ignored
        store = {"league": {"id": 555, "season": 2027}, "myTeams": {"555-2027": 3, "888-2027": 5}}
        self.assertEqual(espn_league.my_team(store), 3)
        store["league"]["id"] = 888
        self.assertEqual(espn_league.my_team(store), 5)


if __name__ == "__main__":
    unittest.main()
