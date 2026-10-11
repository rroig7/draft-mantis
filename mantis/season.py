"""Season projections for a connected ESPN league: standings simulation, power rankings, trades, waivers, streamers.

Method
  * Each player's per-game line comes from data/players.json (preseason projection blended with this season's
    stats as games accumulate; refreshed daily).
  * Weekly team totals: for every day of a matchup period, the players whose NBA team plays that day, best first,
    up to the number of starting spots (ESPN counts active lineup only). Each game counts with the player's
    availability (expected games / 82; 0 this week and next if OUT).
  * Week-to-week randomness per category is calibrated from the G-score paper (Rosenof, arXiv 2307.02188,
    Table 8: player-week variances), plus a per-season projection-error term that shrinks as the season goes on.
  * Monte Carlo over the league's real remaining schedule, then a seeded playoff bracket.
  * Trades and pickups are scored by the change in a team's expected matchup win rate against every other team
    (all-play), computed analytically from the same weekly means and variances.
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
import random
from pathlib import Path

from . import config, espn_league, sources

SEASON_DAY1 = dt.date(2026, 10, 20)  # ESPN scoring period 1
COUNT = ["pts", "reb", "ast", "stl", "blk", "tpm", "to"]
# per-game variance at league-average volume = player-week variance / ~3.5 games (G-score paper, Table 8)
GAME_VAR = {"pts": 128.0, "reb": 16.4, "ast": 9.0, "stl": 1.2, "blk": 0.77, "tpm": 2.58, "to": 2.53}
PCT_SHOT_VAR = {"fgp": 0.294, "ftp": 0.252}  # var(team pct) ~= k / attempts; matches the paper's player-week values
FP_GAME_CV = 0.33                            # per-game fantasy-point standard deviation / mean
DEFAULT_SIMS = 1500  # same count and seed as the Your moves scenarios, so "Now" matches the standings


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ---------------------------------------------------------------- league context

def league_context(raw: dict) -> dict:
    st = raw.get("settings") or {}
    ss = st.get("scheduleSettings") or {}
    # ESPN maps each matchup period to calendar *weeks* (e.g. {"17": [17, 18]} around the All-Star break);
    # weeks are opening night through the first Sunday, then Monday-Sunday. Convert to scoring-period days.
    weeks = fallback_periods(30)
    mp = ss.get("matchupPeriods") or {}
    periods = ({int(k): sorted(d for w in v for d in weeks.get(int(w), [])) for k, v in mp.items()} if mp else
               {k: v for k, v in weeks.items() if k <= int(ss.get("matchupPeriodCount") or 22)})
    sched = []
    for m in raw.get("schedule") or []:
        h, a = m.get("home") or {}, m.get("away") or {}
        if "teamId" not in h or "teamId" not in a:
            continue  # bye
        sched.append({
            "period": m.get("matchupPeriodId"), "home": h["teamId"], "away": a["teamId"],
            "winner": m.get("winner") or "UNDECIDED",
            "playoff": (m.get("playoffTierType") or "NONE") != "NONE",
        })
    today = (dt.date.today() - SEASON_DAY1).days + 1
    status = raw.get("status") or {}
    slots = ((st.get("rosterSettings") or {}).get("lineupSlotCounts")) or {}
    return {
        "periods": periods,
        "schedule": sched,
        "playoffTeams": int(ss.get("playoffTeamCount") or 4),
        "regularPeriods": int(ss.get("matchupPeriodCount") or 0),
        "scoringPeriod": int(raw.get("scoringPeriodId") or max(1, today)),
        "currentPeriod": int(status.get("currentMatchupPeriod") or 1),
        "irSlots": int(slots.get(espn_league.IR_SLOT, 0) or 0),
        "records": {t["id"]: ((t.get("record") or {}).get("overall") or {}) for t in raw.get("teams") or []},
    }


def fallback_periods(n_weeks: int = 22) -> dict[int, list[int]]:
    """Calendar weeks as ESPN scoring-period days: opening night through the first Sunday, then Monday-Sunday."""
    first_sunday = 1 + (6 - SEASON_DAY1.weekday()) % 7
    periods, start = {1: list(range(1, first_sunday + 1))}, first_sunday + 1
    for k in range(2, n_weeks + 1):
        periods[k] = list(range(start, start + 7))
        start += 7
    return periods


def fallback_schedule(team_ids: list[int], periods: list[int]) -> list[dict]:
    """Round-robin (circle method) when ESPN has not published matchups."""
    ids = list(team_ids) + ([None] if len(team_ids) % 2 else [])
    n, out = len(ids), []
    for i, period in enumerate(periods):
        rot = ids[:1] + ids[1:][-(i % (n - 1)):] + ids[1:][:-(i % (n - 1))] if i % (n - 1) else ids
        for j in range(n // 2):
            a, b = rot[j], rot[n - 1 - j]
            if a is not None and b is not None:
                out.append({"period": period, "home": a, "away": b, "winner": "UNDECIDED", "playoff": False})
    return out


# ---------------------------------------------------------------- weekly team model

class Model:
    def __init__(self, players: list[dict], meta: dict, league: dict, snap: dict, ctx: dict, pro_days: dict[str, list[int]]):
        self.by_id = {p["id"]: p for p in players}
        self.meta, self.league, self.snap, self.ctx = meta, league, snap, ctx
        self.cats = league["cats"] if league["format"] != "points" else []
        self.points = league["points"] or config.POINTS_SCORING
        self.is_points = league["format"] == "points"
        self.most_cats = "MOST" in (league.get("scoringType") or "")
        lineup = league.get("lineup") or {}
        self.starters = sum(int(v) for k, v in lineup.items() if str(k) not in ("12", espn_league.IR_SLOT)) or 10
        self.pro_days = {abbr: set(days) for abbr, days in pro_days.items()}
        self.means = meta["league"]["means"]
        self.fg_lg, self.ft_lg = meta["league"]["fgPct"], meta["league"]["ftPct"]
        self.value = {pid: self._value(p) for pid, p in self.by_id.items()}

    def fp(self, p: dict) -> float:
        ln = p["line"]
        get = lambda k: (ln["fga"] - ln["fgm"] if k == "fgmi" else ln["fta"] - ln["ftm"] if k == "ftmi"
                         else ln["tpa"] - ln["tpm"] if k == "tpmi" else ln.get(k, 0.0))
        return sum(w * get(k) for k, w in self.points.items())

    def _value(self, p: dict) -> float:
        """Per-game value under this league's scoring (z-sum or fantasy points); orders daily lineups."""
        if self.is_points:
            return self.fp(p)
        return sum(p["z"].get(c, 0.0) for c in self.cats)

    def play_prob(self, p: dict, period: int) -> float:
        if p["injury"] == "OUT" and period <= self.ctx["currentPeriod"] + 1:
            return 0.0
        base = min(1.0, max(0.2, p["expGp"] / 82))
        return base * (0.85 if p["injury"] == "DAY_TO_DAY" and period == self.ctx["currentPeriod"] else 1.0)

    def games(self, roster: list[int], days: list[int], period: int) -> dict[int, float]:
        """Expected games that count for each player over these days (lineup cap applied day by day)."""
        ps = sorted((self.by_id[i] for i in roster if i in self.by_id), key=lambda p: -self.value[p["id"]])
        out = {p["id"]: 0.0 for p in ps}
        for d in days:
            room = float(self.starters)
            for p in ps:
                if room <= 0:
                    break
                if d in self.pro_days.get(p["team"], ()):
                    g = min(self.play_prob(p, period), room)
                    out[p["id"]] += g
                    room -= g
        return out

    def setup_rates(self, rosters: dict[int, list[int]]) -> None:
        """Games per week left for each NBA team, and each fantasy team's share of games that fit its lineup."""
        periods = self.ctx["periods"] or fallback_periods()
        last_day = max((max(v) for v in periods.values() if v), default=170)
        self.days_left = list(range(self.ctx["scoringPeriod"], last_day + 1)) or [last_day]
        weeks_left = max(len(self.days_left) / 7, 1.0)
        first = self.days_left[0]
        self.gpw = {abbr: sum(1 for d in days if first <= d <= last_day) / weeks_left for abbr, days in self.pro_days.items()}
        # projection error: the same size the simulation draws per season (shrinks as games are played)
        progress = min(1.0, max(0.0, (self.ctx["scoringPeriod"] - 1) / 170))
        self.err_count, self.err_pct = 0.06 * (1 - 0.7 * progress), 0.006 * (1 - 0.7 * progress)
        self.cap = {}
        for t, roster in rosters.items():
            capped = sum(self.games(roster, self.days_left, 999).values())
            raw = sum(self.play_prob(self.by_id[i], 999) * self.gpw.get(self.by_id[i]["team"], 0) * weeks_left
                      for i in roster if i in self.by_id)
            self.cap[t] = capped / raw if raw else 1.0

    def avg_games(self, roster: list[int], team: int) -> dict[int, float]:
        """Counted games per week for the rest of the season (fast; used for trades and power rankings)."""
        r = self.cap.get(team, 1.0)
        default = 0.0 if self.gpw else 3.4  # unsigned players ("FA") have no NBA games
        return {i: self.play_prob(self.by_id[i], 999) * self.gpw.get(self.by_id[i]["team"], default) * r
                for i in roster if i in self.by_id}

    def week(self, games: dict[int, float]) -> dict:
        """Mean and variance of each scoring stat for one matchup period."""
        m = {c: 0.0 for c in COUNT}
        v = {c: 0.0 for c in COUNT}
        fga = fgm = fta = ftm = fp = fpv = 0.0
        for pid, g in games.items():
            if g <= 0:
                continue
            p, ln = self.by_id[pid], self.by_id[pid]["line"]
            for c in COUNT:
                m[c] += g * ln[c]
                v[c] += g * GAME_VAR[c] * max(ln[c], 0.05) / max(self.means[c], 0.05)
            fga += g * ln["fga"]; fgm += g * ln["fgm"]; fta += g * ln["fta"]; ftm += g * ln["ftm"]
            f = self.fp(p)
            fp += g * f
            fpv += g * (FP_GAME_CV * max(f, 1.0)) ** 2
        m["fgp"] = fgm / fga if fga else self.fg_lg
        m["ftp"] = ftm / fta if fta else self.ft_lg
        v["fgp"] = PCT_SHOT_VAR["fgp"] / max(fga, 1.0)
        v["ftp"] = PCT_SHOT_VAR["ftp"] / max(fta, 1.0)
        m["fp"], v["fp"] = fp, fpv
        m["fga"], m["fta"] = fga, fta
        return {"m": m, "v": v}


# ---------------------------------------------------------------- analytic matchup odds

def cat_win_probs(model: Model, a: dict, b: dict) -> dict[str, float]:
    """Weekly win chance per category: week-to-week noise plus projection error (matches the simulation)."""
    out = {}
    for c in (model.cats or ["fp"]):
        d = a["m"][c] - b["m"][c]
        if c == "to":
            d = -d
        pe = (lambda x: (model.err_pct * (2 if c == "ftp" else 1)) ** 2) if c in ("fgp", "ftp") else (lambda x: (model.err_count * x) ** 2)
        sd = math.sqrt(a["v"][c] + b["v"][c] + pe(a["m"][c]) + pe(b["m"][c])) or 1e-9
        out[c] = phi(d / sd)
    return out


def matchup_win(model: Model, probs: dict[str, float]) -> float:
    """Chance to win the matchup: points / most-categories = majority; each-category = expected share of cats."""
    if model.is_points:
        return probs["fp"]
    ps = list(probs.values())
    if not model.most_cats:
        return sum(ps) / len(ps)
    dist = [1.0]  # Poisson-binomial distribution of categories won
    for p in ps:
        dist = [(dist[k] if k < len(dist) else 0) * (1 - p) + (dist[k - 1] * p if k else 0) for k in range(len(dist) + 1)]
    n = len(ps)
    return sum(dist[k] for k in range(n + 1) if k * 2 > n) + 0.5 * sum(dist[k] for k in range(n + 1) if k * 2 == n)


def strength(model: Model, weeks: dict[int, dict], team: int) -> float:
    """All-play: average chance this team beats each other team in a typical week."""
    others = [t for t in weeks if t != team]
    return sum(matchup_win(model, cat_win_probs(model, weeks[team], weeks[o])) for o in others) / max(len(others), 1)


# ---------------------------------------------------------------- simulation

def bracket_order(size: int) -> list[int]:
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        order = [x for s in order for x in (s, n + 1 - s)]
    return order


def simulate(model: Model, rosters: dict[int, list[int]], sims: int = DEFAULT_SIMS, seed: int | None = None) -> dict:
    rng = random.Random(seed)
    ctx, teams = model.ctx, list(rosters)
    periods = ctx["periods"] or fallback_periods()
    sched = [m for m in ctx["schedule"] if not m["playoff"]]
    reg_periods = sorted({m["period"] for m in sched}) or list(periods)[: ctx["regularPeriods"] or 19]
    if not sched:
        sched = fallback_schedule(teams, reg_periods)
    cur = ctx["currentPeriod"]
    todo = [m for m in sched if m["winner"] == "UNDECIDED" and m["period"] >= cur]

    # current standings (ESPN record: category W-L-T for each-category leagues, matchup W-L-T otherwise)
    base = {t: [float(ctx["records"].get(t, {}).get(k, 0) or 0) for k in ("wins", "losses", "ties")] for t in teams}
    # weekly projections per team and period (remaining days only for the current period)
    week_of = {}
    for per in sorted({m["period"] for m in todo}):
        days = [d for d in periods.get(per, []) if d >= ctx["scoringPeriod"]] or periods.get(per, [])
        for t in teams:
            week_of[(t, per)] = model.week(model.games(rosters[t], days, per))
    avg_week = {t: model.week(model.avg_games(rosters[t], t)) for t in teams}
    err_count, err_pct = model.err_count, model.err_pct
    cats = model.cats or ["fp"]
    n_play = min(ctx["playoffTeams"], len(teams))

    tally = {t: {"w": 0.0, "l": 0.0, "t": 0.0, "playoffs": 0, "title": 0, "seed": 0.0} for t in teams}
    for _ in range(sims):
        # projection error: one draw per team and category for the whole season
        err = {t: {c: (rng.gauss(0, err_pct * (2 if c == "ftp" else 1)) if c in ("fgp", "ftp") else rng.gauss(1, err_count))
                   for c in cats} for t in teams}

        def draw(t, wk):
            out = {}
            for c in cats:
                mu = wk["m"][c] + err[t][c] if c in ("fgp", "ftp") else wk["m"][c] * err[t][c]
                out[c] = rng.gauss(mu, math.sqrt(wk["v"][c]))
            return out

        def play(a, b, wa, wb):
            xa, xb = draw(a, wa), draw(b, wb)
            won = sum((xa[c] < xb[c]) if c == "to" else (xa[c] > xb[c]) for c in cats)
            return won, len(cats) - won

        rec = {t: list(base[t]) for t in teams}
        for m in todo:
            a, b = m["home"], m["away"]
            if a not in rosters or b not in rosters:
                continue
            wa, wb = play(a, b, week_of[(a, m["period"])], week_of[(b, m["period"])])
            if model.is_points or model.most_cats:
                if wa > wb: rec[a][0] += 1; rec[b][1] += 1
                elif wb > wa: rec[b][0] += 1; rec[a][1] += 1
                else: rec[a][2] += 1; rec[b][2] += 1
            else:
                rec[a][0] += wa; rec[a][1] += wb; rec[b][0] += wb; rec[b][1] += wa
        pct = {t: (r[0] + r[2] / 2) / max(sum(r), 1) for t, r in rec.items()}
        order = sorted(teams, key=lambda t: (pct[t], rng.random()), reverse=True)
        for i, t in enumerate(order):
            tally[t]["w"] += rec[t][0]; tally[t]["l"] += rec[t][1]; tally[t]["t"] += rec[t][2]
            tally[t]["seed"] += i + 1
        seeds = order[:n_play]
        for t in seeds:
            tally[t]["playoffs"] += 1
        # seeded bracket; byes go to the top seeds
        size = 1 << max(1, (n_play - 1).bit_length())
        alive = [seeds[s - 1] if s <= n_play else None for s in bracket_order(size)]
        while len(alive) > 1:
            nxt = []
            for a, b in zip(alive[::2], alive[1::2]):
                if a is None or b is None:
                    nxt.append(a if b is None else b)
                    continue
                wa, wb = play(a, b, avg_week[a], avg_week[b])
                nxt.append(a if wa > wb or (wa == wb and rng.random() < 0.5) else b)
            alive = nxt
        if alive and alive[0] is not None:
            tally[alive[0]]["title"] += 1

    return {"tally": tally, "sims": sims, "avgWeek": avg_week, "remaining": len({m["period"] for m in todo}),
            "regularPeriods": len(reg_periods), "usedFallbackSchedule": not ctx["schedule"]}


# ---------------------------------------------------------------- roster rules (mirrors the board's engine)

SLOT_ORDER = [0, 1, 2, 3, 4, 7, 9, 5, 6, 8, 10, 11]


def lineup_ok(model: Model, roster: list[int]) -> bool:
    lim = model.league.get("limits") or {}
    counts: dict[str, int] = {}
    for pid in roster:
        pp = model.by_id.get(pid, {}).get("ppos")
        if pp:
            counts[pp] = counts.get(pp, 0) + 1
    if any(counts.get(pp, 0) > n for pp, n in lim.items()):
        return False
    slots = [int(s) for s in SLOT_ORDER for _ in range(int((model.league.get("lineup") or {}).get(str(s), 0)))]
    owner = [-1] * len(slots)
    ps = [pid for pid in roster if pid in model.by_id]

    def aug(i, seen):
        el = model.by_id[ps[i]].get("slots") or [11]
        for j, s in enumerate(slots):
            if j not in seen and s in el:
                seen.add(j)
                if owner[j] < 0 or aug(owner[j], seen):
                    owner[j] = i
                    return True
        return False

    filled = sum(aug(i, set()) for i in range(len(ps)))
    return filled >= min(len(slots), len(ps))


# ---------------------------------------------------------------- improvement suggestions

def suggest(model: Model, rosters: dict[int, list[int]], me: int, avail: list[int], names: dict) -> dict:
    weeks = {t: model.week(model.avg_games(r, t)) for t, r in rosters.items()}
    base = {t: strength(model, weeks, t) for t in rosters}
    val = model.value
    score = lambda ids: sum(max(val.get(i, -9), -3) for i in ids)

    def eval_swap(t_ids: dict[int, list[int]]):
        w = dict(weeks)
        for t, ids in t_ids.items():
            w[t] = model.week(model.avg_games(ids, t))
        return {t: strength(model, w, t) for t in t_ids}, w

    def cat_delta(w_new):
        before = {c: sum(cat_win_probs(model, weeks[me], weeks[o])[c] for o in rosters if o != me) for c in (model.cats or ["fp"])}
        after = {c: sum(cat_win_probs(model, w_new[me], w_new[o])[c] for o in rosters if o != me) for c in (model.cats or ["fp"])}
        n = max(len(rosters) - 1, 1)
        return {c: round((after[c] - before[c]) / n, 3) for c in before}

    mine = [i for i in rosters[me] if i in model.by_id]
    trades = []
    for other, theirs_all in rosters.items():
        if other == me:
            continue
        theirs = [i for i in theirs_all if i in model.by_id]
        top_mine = sorted(mine, key=lambda i: -val[i])[:9]
        top_theirs = sorted(theirs, key=lambda i: -val[i])[:9]
        combos = [((a,), (b,)) for a in mine for b in theirs]
        combos += [(ga, gb) for ga in itertools.combinations(top_mine, 2) for gb in itertools.combinations(top_theirs, 2)]
        for give, get in combos:
            if abs(score(give) - score(get)) > 2.5 + 0.15 * abs(score(give)):  # skip lopsided offers early
                continue
            new_me = [i for i in rosters[me] if i not in give] + list(get)
            new_ot = [i for i in theirs_all if i not in get] + list(give)
            s, w = eval_swap({me: new_me, other: new_ot})
            d_me, d_ot = s[me] - base[me], s[other] - base[other]
            if d_me < 0.004 or d_ot < -0.004:
                continue
            if not (lineup_ok(model, new_me) and lineup_ok(model, new_ot)):  # position limits + full lineup
                continue
            trades.append({"partner": other, "give": list(give), "get": list(get), "dMe": d_me, "dThem": d_ot,
                           "winWin": d_ot > 0.004, "cats": cat_delta(w)})
    trades.sort(key=lambda t: -(t["dMe"] + 0.5 * max(t["dThem"], 0)))
    picked, used = [], {}
    for t in trades:
        key = t["partner"]
        if used.get(key, 0) >= 2 or any(sum(x in p["give"] for p in picked) >= 2 for x in t["give"]):
            continue
        used[key] = used.get(key, 0) + 1
        picked.append(t)
        if len(picked) >= 10:
            break

    # waivers: best add/drop pairs from players nobody rosters
    adds = sorted((i for i in avail if i in model.by_id), key=lambda i: -val[i])[:120]
    waivers = []
    for a in adds:
        best = None
        for d in mine:
            new_me = [i for i in rosters[me] if i != d] + [a]
            s, w = eval_swap({me: new_me})
            delta = s[me] - base[me]
            if delta > 0.003 and (best is None or delta > best["dMe"]) and lineup_ok(model, new_me):
                best = {"add": a, "drop": d, "dMe": delta, "cats": cat_delta(w)}
        if best:
            waivers.append(best)
    waivers.sort(key=lambda x: -x["dMe"])
    waivers = waivers[:8]

    # streamers: available players with the most games left in the current matchup period
    per = model.ctx["currentPeriod"]
    days = [d for d in (model.ctx["periods"] or fallback_periods()).get(per, []) if d >= model.ctx["scoringPeriod"]]
    streamers = []
    for a in adds[:80]:
        p = model.by_id[a]
        g = sum(1 for d in days if d in model.pro_days.get(p["team"], ()))
        if g and p["injury"] != "OUT":
            streamers.append({"id": a, "games": g, "perGame": round(val[a], 2), "total": round(g * max(val[a], 0), 2)})
    streamers.sort(key=lambda x: -x["total"])

    # strategy notes
    notes = []
    n_other = max(len(rosters) - 1, 1)
    cat_p = {c: sum(cat_win_probs(model, weeks[me], weeks[o])[c] for o in rosters if o != me) / n_other for c in (model.cats or ["fp"])}
    if model.cats:
        weak = sorted(model.cats, key=lambda c: cat_p[c])[:2]
        swing = [c for c in model.cats if 0.4 <= cat_p[c] <= 0.6]
        for c in weak:
            if cat_p[c] < 0.3:
                notes.append({"kind": "punt", "cat": c, "text": f"You win {config.CAT_LABELS[c]} only {cat_p[c]:.0%} of weeks. "
                              + ("In a most-categories league, consider punting it: trade its specialists for help where you are close." if model.most_cats
                                 else "Each category counts, so target it in trades rather than punting outright.")})
        if swing:
            notes.append({"kind": "swing", "text": "Closest categories: " + ", ".join(config.CAT_LABELS[c] for c in swing)
                          + ". Small upgrades here flip the most matchups, so aim streams and trades at them."})
    out_players = [i for i in mine if model.by_id[i]["injury"] == "OUT"]
    if out_players:
        tip = (f"Move them to IR ({model.ctx['irSlots']} slot{'s' if model.ctx['irSlots'] != 1 else ''}) and use the open spot to stream."
               if model.ctx["irSlots"] else "Your league has no IR slots; weigh dropping one for a healthy streamer.")
        notes.append({"kind": "injury", "text": "Out right now: " + ", ".join(names.get(i, str(i)) for i in out_players) + ". " + tip})
    if mine:
        cur_games = sum(model.games(rosters[me], days, per).values()) if days else 0
        league_games = sum(sum(model.games(r, days, per).values()) for r in rosters.values()) / len(rosters) if days else 0
        if days and cur_games < league_games - 1:
            notes.append({"kind": "volume", "text": f"Your roster has about {cur_games:.0f} counted games left this week against a league average of {league_games:.0f}. Stream players with more games to close the gap."})
    hot = [i for i in mine if model.by_id[i].get("curGp", 0) >= 8 and model.by_id[i].get("fpCur") and model.by_id[i]["fpCur"] > 1.2 * model.by_id[i]["fpPrior"]]
    if hot:
        notes.append({"kind": "sell", "text": "Playing well above projection: " + ", ".join(names.get(i, str(i)) for i in hot)
                      + ". Sell-high candidates if a trade fills a need."})
    cold = [i for r in rosters for i in rosters[r] if r != me and i in model.by_id and model.by_id[i].get("curGp", 0) >= 8
            and model.by_id[i]["injury"] == "ACTIVE" and model.by_id[i].get("fpCur") and model.by_id[i]["fpCur"] < 0.8 * model.by_id[i]["fpPrior"]]
    if cold:
        notes.append({"kind": "buy", "text": "Healthy but below projection on other teams: " + ", ".join(names.get(i, str(i)) for i in cold[:6])
                      + ". Buy-low targets: their managers may sell at a discount."})
    return {"trades": picked, "waivers": waivers, "streamers": streamers[:8], "notes": notes, "catWin": cat_p, "base": base}


# ---------------------------------------------------------------- entry point

def report(players_payload: dict, raw: dict, snap: dict, pro_days: dict, sims: int = DEFAULT_SIMS, seed: int | None = None,
           rosters: dict[int, list[int]] | None = None, suggestions: bool = True,
           suggest_rosters: dict[int, list[int]] | None = None) -> dict:
    """Season outlook. `rosters` overrides the league's (what-if scenarios); `suggestions=False` skips trades/waivers."""
    players, meta = players_payload["players"], players_payload["meta"]
    ctx = league_context(raw)
    model = Model(players, meta, snap["league"], snap, ctx, pro_days)
    rosters = rosters or {t["id"]: list(t["roster"]) for t in snap["teams"]}
    model.setup_rates(rosters)
    me = snap.get("myTeamId")
    owned = {i for r in rosters.values() for i in r}
    avail = [p["id"] for p in players if p["id"] not in owned]
    names = {p["id"]: p["name"] for p in players} | {int(k): v for k, v in snap.get("names", {}).items()}
    sim = simulate(model, rosters, sims, seed)
    weeks = sim["avgWeek"]
    cats = model.cats or ["fp"]
    team_rows = []
    for t in snap["teams"]:
        tid, tal = t["id"], sim["tally"][t["id"]]
        cp = {c: sum(cat_win_probs(model, weeks[tid], weeks[o])[c] for o in rosters if o != tid) / max(len(rosters) - 1, 1) for c in cats}
        team_rows.append({
            "id": tid, "name": t["name"], "abbrev": t["abbrev"], "isMine": tid == me, "record": t.get("record"),
            "proj": {k: round(tal[k] / sims, 1) for k in ("w", "l", "t")},
            "playoffPct": round(100 * tal["playoffs"] / sims, 1), "titlePct": round(100 * tal["title"] / sims, 1),
            "avgSeed": round(tal["seed"] / sims, 1), "strength": round(100 * strength(model, weeks, tid), 1),
            "catWin": {c: round(cp[c], 3) for c in cp},
            "unknownPlayers": [i for i in rosters[tid] if i not in model.by_id],
        })
    for c in cats:
        for rank, row in enumerate(sorted(team_rows, key=lambda r: -r["catWin"][c]), 1):
            row.setdefault("catRank", {})[c] = rank
    team_rows.sort(key=lambda r: (-r["titlePct"], -r["playoffPct"], -r["strength"]))
    if suggest_rosters:  # suggestions start from the roster you will have once your pending moves clear
        owned_after = {i for r in suggest_rosters.values() for i in r}
        avail = [p["id"] for p in players if p["id"] not in owned_after]
    sug = suggest(model, suggest_rosters or rosters, me, avail, names) if me is not None and suggestions else None
    return {
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "dataGenerated": meta.get("generated"),
        "sims": sims, "scoringType": snap["league"].get("scoringType"), "cats": cats,
        "currentPeriod": ctx["currentPeriod"], "remainingPeriods": sim["remaining"], "regularPeriods": sim["regularPeriods"],
        "playoffTeams": ctx["playoffTeams"], "usedFallbackSchedule": sim["usedFallbackSchedule"],
        "seasonStarted": ctx["scoringPeriod"] > 1 and dt.date.today() >= SEASON_DAY1,
        "teams": team_rows, "myTeamId": me,
        "names": {str(k): v for k, v in names.items() if k in {i for r in rosters.values() for i in r} | set(avail[:200])},
        "suggestions": sug and {**{k: sug[k] for k in ("trades", "waivers", "streamers", "notes")},
                                "catWin": {c: round(v, 3) for c, v in sug["catWin"].items()}},
    }


def fetch_raw(league_id: int, season: int, creds: dict) -> dict:
    """League views needed for the season model: settings, rosters, records, schedule, status."""
    import urllib.parse
    url = espn_league.LEAGUE_URL.format(season=season, league_id=league_id) + "?" + urllib.parse.urlencode(
        [("view", v) for v in ("mSettings", "mTeam", "mRoster", "mMatchupScore", "mStatus")])
    try:
        return espn_league._get_json(url, creds, timeout=20, fresh=True)
    except espn_league.EspnError as e:
        if e.status in (401, 404):
            return espn_league.fetch_league(league_id, season, creds)  # same friendly errors
        raise


def run(root: Path, sims: int = DEFAULT_SIMS, refresh_schedule: bool = False) -> dict:
    import json
    from . import draftfeed
    store = espn_league.load_store()
    sel = store.get("league")
    if not sel:
        raise espn_league.EspnError("Connect your ESPN league first (python3 -m mantis league --id <leagueId>).", 409)
    raw = fetch_raw(sel["id"], sel["season"], store)
    snap = draftfeed.merge(espn_league.normalize(raw, store.get("swid"), espn_league.my_team(store)), draftfeed.HUB.get(sel["id"]))
    espn_league.require_rosters(snap)  # no outlook (or history/suggestion records) for an undrafted league
    ldir = espn_league.league_dir(root, sel)
    payload = json.loads((root / "data" / "players.json").read_text(encoding="utf-8"))
    pro_days = sources.espn_pro_schedule(root / "data" / "raw", refresh_schedule)
    from . import moves
    known = moves.sync(ldir, moves.fetch_transactions(sel["id"], sel["season"], store))
    pending = [x for m in reversed(known) if m["status"] == "PENDING" for x in moves.move_transfers(m)]
    now = {t["id"]: list(t["roster"]) for t in snap["teams"]}
    rep = report(payload, raw, snap, pro_days, sims, seed=moves.SEED if sims == moves.SCENARIO_SIMS else None,
                 suggest_rosters=moves.apply(now, pending) if pending else None)
    rep["pendingMoves"] = sum(m["status"] == "PENDING" for m in known)
    rep.update(leagueId=int(sel["id"]), leagueSeason=int(sel["season"]), leagueKey=espn_league.league_key(sel),
               leagueName=snap["league"]["name"])
    (ldir / "season.json").write_text(json.dumps(rep), encoding="utf-8")
    moves.record_plan(ldir, rep)      # remember what was suggested, to match against later moves
    moves.append_history(ldir, rep)   # odds over time
    return rep
