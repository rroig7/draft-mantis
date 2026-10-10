"""Merge sources into one player table and compute the Draft Mantis Score.

Pipeline
  1. Per-game line   = 70% ESPN 2026-27 projection + 30% 2025-26 actuals (projection only for rookies)
  2. Category value  = z-score of each 9-cat stat vs. the draftable pool (teams x roster);
                       FG%/FT% are volume-weighted impact (FGA x (FG% - pool FG%)); TO counts negative
  3. Replacement     = value of the last draftable player; VORP = value - replacement
  4. Season value    = VORP x availability^0.7 x age curve x current-injury factor  (only positive VORP is scaled)
  5. Mantis Score    = 50 + 50 x value / best value   ->  100 = best player, 50 = replacement level
  6. Edge            = ESPN ADP - Mantis rank (positive = the market lets him fall past his value)
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
import unicodedata

from . import config

SEASON_START = dt.date(2026, 10, 20)

ALIASES = [  # groups of normalized names that refer to the same player across sources
    {"alex sarr", "alexandre sarr"},
    {"nic claxton", "nicolas claxton"},
    {"herb jones", "herbert jones"},
    {"carlton carrington", "bub carrington"},
    {"ron holland", "ronald holland"},
    {"gg jackson", "gregory jackson"},
]


def _lookup(table: dict, key: str):
    if key in table:
        return table[key]
    for group in ALIASES:
        if key in group:
            return next((table[k] for k in group if k in table), None)
    return None


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’]", "", s)
    s = re.sub(r"[^a-z ]", " ", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _line(avg: dict) -> dict:
    return {key: float(avg.get(sid, 0.0)) for sid, key in config.ESPN_STAT_IDS.items()}


def fantasy_points(line: dict) -> float:
    return sum(line.get(k, 0.0) * w for k, w in config.POINTS_SCORING.items())


def _age_mult(age):
    if age is None:
        return 1.02
    for max_age, m in config.AGE_CURVE:
        if age <= max_age:
            return m
    return 1.0


# ---------------------------------------------------------------- merge

def merge(espn: list[dict], teams: dict[int, str], bref_last: list[dict], bref_prior: list[dict],
          bref_adv: list[dict], sleeper: list[dict]) -> tuple[list[dict], list[str]]:
    by_name = lambda rows: {norm(r["name_display"]): r for r in rows}
    b_last, b_prior, b_adv = by_name(bref_last), by_name(bref_prior), by_name(bref_adv)
    sl = {}
    for p in sleeper:  # prefer active, rostered entries on name collisions
        k = norm(p["full_name"])
        if k not in sl or (p.get("active") and p.get("team")):
            sl[k] = p

    proj_id, last_id = f"10{config.ESPN_SEASON}", f"00{config.ESPN_SEASON - 1}"
    players, unmatched = [], []
    for e in espn:
        p = e["player"]
        stats = {s["id"]: s for s in p.get("stats", [])}
        proj = stats.get(proj_id, {}).get("averageStats")
        last = stats.get(last_id, {}).get("averageStats")
        cur = stats.get(f"00{config.ESPN_SEASON}", {}).get("averageStats")
        l15 = stats.get(f"02{config.ESPN_SEASON}", {}).get("averageStats")
        cur = _line(cur) if cur and _line(cur)["gp"] >= 1 else None
        l15 = _line(l15) if l15 and _line(l15)["gp"] >= 1 else None
        proj = _line(proj) if proj else None
        last = _line(last) if last else None
        if last and (last["gp"] < 1 or last["min"] <= 0):  # ESPN keeps empty placeholder lines
            last = None
        if not proj and not last:
            continue

        key = norm(p["fullName"])
        bl, bp, ba, sp = (_lookup(t, key) for t in (b_last, b_prior, b_adv, sl))
        if last and not bl:
            unmatched.append(p["fullName"])

        age = None
        if sp and sp.get("birth_date"):
            b = dt.date.fromisoformat(sp["birth_date"])
            age = SEASON_START.year - b.year - ((SEASON_START.month, SEASON_START.day) < (b.month, b.day))
        elif bl and _num(bl.get("age")) is not None:
            age = int(_num(bl["age"])) + 1

        pos = [config.ESPN_SLOT_POS[s] for s in p.get("eligibleSlots", []) if s in config.ESPN_SLOT_POS]
        own = p.get("ownership") or {}
        ranks = p.get("draftRanksByRankType") or {}
        players.append({
            "id": p["id"],
            "name": p["fullName"],
            "team": teams.get(p.get("proTeamId"), "FA"),
            "pos": pos or [config.ESPN_POSITIONS.get(p.get("defaultPositionId"), "UT")],
            "ppos": config.ESPN_POSITIONS.get(p.get("defaultPositionId"), (pos or ["UT"])[0]),
            "slots": [sl for sl in p.get("eligibleSlots", []) if sl <= 11],
            "age": age,
            "exp": sp.get("years_exp") if sp else None,
            "injury": p.get("injuryStatus") or "ACTIVE",
            "adp": round(own["averageDraftPosition"], 1) if own.get("averageDraftPosition") else None,
            "auction": round(own.get("auctionValueAverage") or 0, 1),
            "espnRank": (ranks.get("ROTO") or {}).get("rank"),
            "espnPtsRank": (ranks.get("STANDARD") or {}).get("rank"),
            "outlook": p.get("seasonOutlook") or "",
            "proj": proj,
            "last": last,
            "cur": cur,
            "l15": l15,
            "gpHist": [
                int(_num(bl["games"], 0)) if bl else None,
                int(_num(bp["games"], 0)) if bp else None,
            ],
            "usg": _num(ba.get("usg_pct")) if ba else None,
            "bpm": _num(ba.get("bpm")) if ba else None,
        })
    return players, unmatched


# ---------------------------------------------------------------- model

def build_lines(players: list[dict]) -> None:
    """Blend projection + actuals into the per-game line we score, and estimate games."""
    keys = ["pts", "reb", "ast", "stl", "blk", "tpm", "tpa", "to", "fgm", "fga", "ftm", "fta", "min"]
    for p in players:
        proj, last = p["proj"], p["last"]
        use_last = last and last["gp"] >= config.MIN_GP_FOR_ACTUALS
        if proj and use_last:
            w = config.PROJ_WEIGHT
            prior = {k: w * proj[k] + (1 - w) * last[k] for k in keys}
        else:
            prior = dict(proj or last or p["cur"])
        # In season: lean on this season's actual stats as games pile up (50/50 at CUR_HALF_GAMES games)
        cur = p.get("cur")
        w_cur = cur["gp"] / (cur["gp"] + config.CUR_HALF_GAMES) if cur else 0.0
        line = {k: w_cur * cur[k] + (1 - w_cur) * prior[k] for k in keys} if cur else prior
        p["line"] = {k: round(line[k], 3) for k in keys}
        p["curGp"] = int(cur["gp"]) if cur else 0
        p["curWeight"] = round(w_cur, 3)
        p["fpPrior"] = round(fantasy_points(prior), 2)
        p["fpCur"] = round(fantasy_points(cur), 2) if cur else None
        p["fpL15"] = round(fantasy_points(p["l15"]), 2) if p.get("l15") else None
        p["fgPct"] = line["fgm"] / line["fga"] if line["fga"] else 0.0
        p["ftPct"] = line["ftm"] / line["fta"] if line["fta"] else 0.0
        p["fp"] = round(fantasy_points(line), 2)
        p["fpProj"] = round(fantasy_points(proj), 2) if proj else None
        p["fpLast"] = round(fantasy_points(last), 2) if last else None

        proj_gp = proj["gp"] if proj and proj["gp"] else None
        hist = [(g, w) for g, w in zip(p["gpHist"], config.GP_HISTORY_WEIGHTS) if g is not None]
        hist_gp = sum(g * w for g, w in hist) / sum(w for _, w in hist) if hist else None
        if proj_gp and hist_gp is not None:
            exp_gp = config.GP_PROJ_WEIGHT * proj_gp + (1 - config.GP_PROJ_WEIGHT) * hist_gp
        else:
            exp_gp = proj_gp or hist_gp or 60
        p["expGp"] = round(min(exp_gp, 80), 1)

        avail = min(max(p["expGp"] / config.GP_FULL_SEASON, 0.25), 1.08) ** config.AVAIL_DAMPING
        p["avail"] = round(avail, 4)
        comeback = config.COMEBACK_MULT if last is None and (p.get("exp") or 0) > 0 else 1.0
        p["mult"] = round(_age_mult(p["age"]) * config.INJURY_MULT.get(p["injury"], 1.0) * comeback, 4)


def _cat_raw(p: dict, fg_lg: float, ft_lg: float) -> dict:
    ln = p["line"]
    return {
        "pts": ln["pts"], "reb": ln["reb"], "ast": ln["ast"], "stl": ln["stl"], "blk": ln["blk"],
        "tpm": ln["tpm"], "to": ln["to"],
        "fgp": ln["fga"] * (p["fgPct"] - fg_lg),
        "ftp": ln["fta"] * (p["ftPct"] - ft_lg),
        "fp": p["fp"],
    }


def compute_z(players: list[dict], pool_size: int) -> dict:
    """Iteratively z-score every category against the top `pool_size` players."""
    pool = sorted(players, key=lambda p: -p["fp"])[:pool_size]
    for _ in range(4):
        fg_lg = sum(p["line"]["fgm"] for p in pool) / sum(p["line"]["fga"] for p in pool)
        ft_lg = sum(p["line"]["ftm"] for p in pool) / sum(p["line"]["fta"] for p in pool)
        raws = {id(p): _cat_raw(p, fg_lg, ft_lg) for p in players}
        stats = {}
        for c in config.CATS + ["fp"]:
            vals = [raws[id(p)][c] for p in pool]
            stats[c] = (statistics.fmean(vals), statistics.pstdev(vals) or 1.0)
        for p in players:
            r = raws[id(p)]
            z = {c: (r[c] - stats[c][0]) / stats[c][1] for c in config.CATS + ["fp"]}
            z["to"] = -z["to"]
            p["z"] = {c: round(v, 3) for c, v in z.items()}
        pool = sorted(players, key=lambda p: -sum(p["z"][c] for c in config.CATS))[:pool_size]
    return {"fgPct": round(fg_lg, 4), "ftPct": round(ft_lg, 4),
            "means": {c: round(m, 3) for c, (m, _) in stats.items()},
            "sds": {c: round(s, 3) for c, (_, s) in stats.items()}}


def score(players: list[dict], weights: dict[str, float], teams: int = config.TEAMS,
          roster: int = config.ROSTER_SIZE) -> list[dict]:
    """Return players sorted by Mantis Score for a given category weighting.

    weights: {cat: weight}. Use {"fp": 1} for points leagues; drop a cat (weight 0) to punt it.
    Mirrors `scoreAll()` in web/template.html - keep them in sync.
    """
    n = teams * roster
    base = {p["id"]: sum(p["z"][c] * w for c, w in weights.items()) for p in players}
    ordered = sorted(base.values(), reverse=True)
    repl = ordered[min(n, len(ordered)) - 1]
    vals = {}
    for p in players:
        vorp = base[p["id"]] - repl
        vals[p["id"]] = vorp * p["avail"] * p["mult"] if vorp > 0 else vorp
    top = max(vals.values()) or 1.0
    ranked = sorted(players, key=lambda p: -vals[p["id"]])
    out = []
    for i, p in enumerate(ranked, 1):
        s = 50 + 50 * vals[p["id"]] / top
        out.append({**p, "score": round(max(s, 0), 1), "rank": i,
                    "edge": round(p["adp"] - i, 1) if p["adp"] else None})
    return out


def flags(p: dict) -> list[str]:
    f = []
    if p["last"] is None:
        f.append("Rookie" if (p.get("exp") or 0) == 0 else "Comeback")
    if p["injury"] == "OUT":
        f.append("Out")
    if p["expGp"] < 60:
        f.append("Durability")
    if p["age"] is not None and p["age"] <= 25 and p["fpProj"] and p["fpLast"] and p["last"]["gp"] >= 20 \
            and p["fpProj"] >= p["fpLast"] * 1.08:
        f.append("Breakout")
    if p["age"] is not None and p["age"] >= 32:
        f.append("Aging")
    if p.get("curGp", 0) >= 5 and p.get("fpL15") and p.get("fpPrior"):
        if p["fpL15"] >= p["fpPrior"] * 1.2:
            f.append("Hot")
        elif p["fpL15"] <= p["fpPrior"] * 0.8:
            f.append("Cold")
    return f
