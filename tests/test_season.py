"""Season model tests on a fake 12-team league drafted from the real player pool.

Run: python3 -m unittest discover tests
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("MANTIS_HOME", tempfile.mkdtemp())

from mantis import espn_league, season  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = json.loads((ROOT / "data" / "players.json").read_text(encoding="utf-8"))
PRO_DAYS = json.loads((ROOT / "data" / "raw" / "espn_proschedule.json").read_text(encoding="utf-8"))


def pro_days():
    return {t["abbrev"]: sorted(int(k) for k in t.get("proGamesByScoringPeriod") or {})
            for t in PRO_DAYS["settings"]["proTeams"] if t.get("proGamesByScoringPeriod")}


def fake_raw(scoring="H2H_MOST_CATEGORIES", stacked_team=1, points=False):
    """12 teams x 13 players; team `stacked_team` gets the 13 best players, the rest snake-draft by ADP."""
    players = [p for p in PAYLOAD["players"] if p["adp"]]
    players.sort(key=lambda p: p["adp"])
    by_pos = lambda ps: ps  # noqa: E731
    rosters = {t: [] for t in range(1, 13)}
    limits = {"C": 4}
    pool = list(players)
    if stacked_team:
        best = sorted(pool, key=lambda p: -sum(p["z"][c] for c in ("pts", "reb", "ast", "stl", "blk", "tpm", "fgp", "ftp", "to")))
        for p in best:
            if len(rosters[stacked_team]) == 13:
                break
            if p["ppos"] == "C" and sum(q["ppos"] == "C" for q in rosters[stacked_team]) >= 4:
                continue
            rosters[stacked_team].append(p)
        pool = [p for p in pool if p not in rosters[stacked_team]]
    order = [t for t in range(1, 13) if t != stacked_team]
    rnd = 0
    while any(len(rosters[t]) < 13 for t in order):
        for t in (order if rnd % 2 == 0 else order[::-1]):
            if len(rosters[t]) >= 13:
                continue
            for p in pool:
                if p["ppos"] == "C" and sum(q["ppos"] == "C" for q in rosters[t]) >= limits["C"]:
                    continue
                rosters[t].append(p)
                pool.remove(p)
                break
        rnd += 1
    sched = season.fallback_schedule(list(range(1, 13)), list(range(1, 20)))
    items = ([{"statId": s, "points": w} for s, w in ((0, 1), (6, 1), (3, 2), (2, 4), (1, 4), (11, -2), (13, 2), (14, -1), (15, 1), (16, -1), (17, 1))]
             if points else [{"statId": s, "isReverseItem": s == 11} for s in (0, 6, 3, 2, 1, 17, 19, 20, 11)])
    return {
        "id": 555, "seasonId": 2027, "scoringPeriodId": 1, "status": {"currentMatchupPeriod": 1},
        "settings": {
            "name": "Sim League", "size": 12,
            "scoringSettings": {"scoringType": "H2H_POINTS" if points else scoring, "scoringItems": items},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "5": 1, "6": 1, "11": 3, "12": 3, "13": 1},
                               "positionLimits": {"5": 4}},
            "scheduleSettings": {"matchupPeriodCount": 19, "playoffTeamCount": 6,
                                 # ESPN format: matchup period -> calendar weeks (17 spans the All-Star break)
                                 "matchupPeriods": {**{str(k): [k] for k in range(1, 17)}, "17": [17, 18],
                                                    **{str(k): [k + 1] for k in range(18, 22)}}},
            "draftSettings": {"type": "SNAKE", "pickOrder": list(range(1, 13))},
        },
        "teams": [{"id": t, "abbrev": f"T{t}", "name": f"Team {t}", "owners": ["{ME}"] if t == 7 else [],
                   "record": {"overall": {"wins": 0, "losses": 0, "ties": 0}},
                   "roster": {"entries": [{"playerId": p["id"]} for p in r]}} for t, r in rosters.items()],
        "schedule": [{"matchupPeriodId": m["period"], "home": {"teamId": m["home"]}, "away": {"teamId": m["away"]},
                      "winner": "UNDECIDED", "playoffTierType": "NONE"} for m in sched],
        "draftDetail": {"drafted": True, "inProgress": False, "picks": []},
    }


class SeasonTests(unittest.TestCase):
    def run_report(self, raw, sims=300):
        snap = espn_league.normalize(raw, "{ME}")
        t0 = time.time()
        rep = season.report(PAYLOAD, raw, snap, pro_days(), sims=sims, seed=7)
        return rep, time.time() - t0

    def test_matchup_periods_are_weeks(self):
        ctx = season.league_context(fake_raw())
        self.assertEqual(ctx["periods"][1], list(range(1, 7)))        # Tue Oct 20 - Sun Oct 25
        self.assertEqual(ctx["periods"][2], list(range(7, 14)))       # Mon - Sun
        self.assertEqual(len(ctx["periods"][17]), 14)                 # two-week matchup

    def test_round_robin_schedule(self):
        sched = season.fallback_schedule(list(range(1, 13)), list(range(1, 12)))
        for per in range(1, 12):
            week = [m for m in sched if m["period"] == per]
            teams = [t for m in week for t in (m["home"], m["away"])]
            self.assertEqual(sorted(teams), list(range(1, 13)))  # everyone plays once a week
        pairs = {frozenset((m["home"], m["away"])) for m in sched}
        self.assertEqual(len(pairs), 66)  # 11 weeks = every pair exactly once

    def test_most_categories_league(self):
        rep, secs = self.run_report(fake_raw())
        teams = {t["id"]: t for t in rep["teams"]}
        self.assertAlmostEqual(sum(t["titlePct"] for t in rep["teams"]), 100, delta=0.5)
        self.assertAlmostEqual(sum(t["playoffPct"] for t in rep["teams"]), 600, delta=1)
        self.assertEqual(rep["teams"][0]["id"], 1, "stacked team should be the title favorite")
        self.assertGreater(teams[1]["strength"], 75)
        for t in rep["teams"]:
            self.assertEqual(sum(t["proj"].values()), 19)  # 19 weekly matchups each
        sug = rep["suggestions"]
        self.assertIsNotNone(sug)
        mine = next(t for t in raw_rosters(fake_raw()) if t[0] == 7)[1]
        for tr in sug["trades"]:
            self.assertGreater(tr["dMe"], 0)
            self.assertTrue(set(tr["give"]) <= set(mine))
        for w in sug["waivers"]:
            self.assertIn(w["drop"], mine)
        print(f"\n  most-cats: {secs:.1f}s, favorite {rep['teams'][0]['name']} {rep['teams'][0]['titlePct']}%, "
              f"{len(sug['trades'])} trades, {len(sug['waivers'])} waivers, {len(sug['notes'])} notes")
        self.assertLess(secs, 60)

    def test_each_category_and_points(self):
        rep, _ = self.run_report(fake_raw("H2H_CATEGORY"), sims=200)
        self.assertEqual(sum(rep["teams"][0]["proj"].values()), 19 * 9)  # every category is a game
        rep, _ = self.run_report(fake_raw(points=True), sims=200)
        self.assertEqual(rep["cats"], ["fp"])
        self.assertEqual(rep["teams"][0]["id"], 1)


def raw_rosters(raw):
    return [(t["id"], [e["playerId"] for e in t["roster"]["entries"]]) for t in raw["teams"]]


if __name__ == "__main__":
    unittest.main()
