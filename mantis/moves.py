"""Track league moves (waiver claims, free-agent adds, drops, trades), match them to Mantis suggestions,
and project the season under scenarios: draft-day roster, now, pending moves cleared, remaining suggestions made.

ESPN views (read-only, verified on a live league):
  mTransactions2        recent transactions: type DRAFT/WAIVER/FREEAGENT/ROSTER/TRADE_*, status PENDING/EXECUTED/...
                        items: [{type ADD/DROP/TRADE, playerId, fromTeamId, toTeamId}]  (team 0 = free agency)
  mPendingTransactions  the pending ones, with processDate
mTransactions2 only covers a recent window, so every transaction seen is kept in data/leagues/<id>-<season>/transactions.json.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
from pathlib import Path

from . import espn_league, season, sources

SKIP_TYPES = {"DRAFT"}
MOVE_ITEMS = {"ADD", "DROP", "TRADE"}
SCENARIO_SIMS = 1500
SEED = 20261020  # same random draws for every scenario, so differences come from the rosters


# ---------------------------------------------------------------- fetch + store

def fetch_transactions(league_id: int, season_id: int, creds: dict) -> list[dict]:
    base = espn_league.LEAGUE_URL.format(season=season_id, league_id=league_id)
    out: dict[str, dict] = {}
    for view, key in (("mTransactions2", "transactions"), ("mPendingTransactions", "pendingTransactions")):
        url = base + "?" + urllib.parse.urlencode({"view": view})
        try:
            data = espn_league._get_json(url, creds, timeout=20, fresh=True)
        except espn_league.EspnError:
            continue
        for t in data.get(key) or []:  # the key is absent when there is nothing pending
            out[t["id"]] = {**out.get(t["id"], {}), **t}
    return list(out.values())


def normalize(tx: dict) -> dict | None:
    if tx.get("type") in SKIP_TYPES:
        return None
    items = [{"kind": it.get("type"), "playerId": it.get("playerId"), "from": it.get("fromTeamId") or 0, "to": it.get("toTeamId") or 0}
             for it in tx.get("items") or [] if it.get("type") in MOVE_ITEMS and it.get("playerId")]
    if not items:
        return None
    return {
        "id": tx["id"], "type": tx.get("type"), "status": tx.get("status") or ("PENDING" if tx.get("isPending") else "EXECUTED"),
        "teamId": tx.get("teamId"), "proposed": tx.get("proposedDate"), "process": tx.get("processDate"),
        "items": items,
    }


def sync(ldir: Path, raw_tx: list[dict]) -> list[dict]:
    """Merge newly seen transactions into the league's transactions.json and return all known moves, newest first."""
    path = ldir / "transactions.json"
    known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for tx in raw_tx:
        n = normalize(tx)
        if n:
            known[n["id"]] = {**known.get(n["id"], {}), **{k: v for k, v in n.items() if v is not None}}
    # a pending move ESPN no longer lists as pending, and that never executed, was cancelled or failed
    live_ids = {t["id"] for t in raw_tx}
    for m in known.values():
        if m["status"] == "PENDING" and m["id"] not in live_ids and m.get("process") and m["process"] < time.time() * 1000 - 3600e3:
            m["status"] = "UNKNOWN"
    path.write_text(json.dumps(known), encoding="utf-8")
    return sorted(known.values(), key=lambda m: -(m.get("proposed") or 0))


# ---------------------------------------------------------------- suggestions plan

def plan_key(s: dict) -> str:
    if s["kind"] == "waiver":
        return f"waiver:{s['add']}:{s['drop']}"
    return f"trade:{s['partner']}:{'-'.join(map(str, sorted(s['give'])))}:{'-'.join(map(str, sorted(s['get'])))}"


def record_plan(ldir: Path, rep: dict) -> None:
    """Remember every suggestion the user was shown, so later moves can be matched to it."""
    sug = rep.get("suggestions")
    if not sug:
        return
    path = ldir / "plan.json"
    plan = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    now = rep["generated"]
    new = [{"kind": "waiver", "add": w["add"], "drop": w["drop"], "dMe": w["dMe"]} for w in sug["waivers"][:5]]
    new += [{"kind": "trade", "partner": t["partner"], "give": t["give"], "get": t["get"], "dMe": t["dMe"], "dThem": t["dThem"]}
            for t in sug["trades"][:5]]
    for s in new:
        k = plan_key(s)
        plan[k] = {**s, "key": k, "firstSeen": plan.get(k, {}).get("firstSeen", now), "lastSeen": now,
                   "dMeFirst": plan.get(k, {}).get("dMeFirst", s["dMe"])}
    cutoff = (dt.datetime.now() - dt.timedelta(days=21)).strftime("%Y-%m-%d")
    plan = {k: v for k, v in plan.items() if v["lastSeen"] >= cutoff}
    path.write_text(json.dumps(plan), encoding="utf-8")


def transfers_of(s: dict, me: int) -> list[tuple[int, int, int]]:
    """A suggestion or custom move as (player, from team, to team); team 0 is free agency."""
    if s.get("kind") == "waiver" or "add" in s or "drop" in s:
        adds = s["add"] if isinstance(s.get("add"), list) else [s["add"]] if s.get("add") else []
        drops = s["drop"] if isinstance(s.get("drop"), list) else [s["drop"]] if s.get("drop") else []
        return [(a, 0, me) for a in adds] + [(d, me, 0) for d in drops]
    return [(g, me, s["partner"]) for g in s["give"]] + [(h, s["partner"], me) for h in s["get"]]


def plan_status(s: dict, moves: list[dict], rosters: dict[int, list[int]], me: int) -> dict:
    """open / pending / partial / done, by matching each player movement to league transactions."""
    parts = []
    for pid, frm, to in transfers_of(s, me):
        hit = next((m for m in moves for it in m["items"]
                    if it["playerId"] == pid and it["to"] == to and (frm == 0 or it["from"] == frm)
                    and m["status"] in ("PENDING", "EXECUTED")), None)
        if hit:
            parts.append("done" if hit["status"] == "EXECUTED" else "pending")
        elif to and pid in rosters.get(to, []) or (not to and pid not in rosters.get(frm, [])):
            parts.append("done")  # already where the suggestion wanted him (move older than ESPN's window)
        else:
            parts.append("open")
    if all(p == "done" for p in parts):
        state = "done"
    elif all(p in ("done", "pending") for p in parts):
        state = "pending"
    elif any(p != "open" for p in parts):
        state = "partial"
    else:
        state = "open"
    return {"state": state, "parts": parts}


# ---------------------------------------------------------------- applying moves

def apply(rosters: dict[int, list[int]], transfers: list[tuple[int, int, int]]) -> dict[int, list[int]]:
    out = {t: list(r) for t, r in rosters.items()}
    for pid, frm, to in transfers:
        if frm and pid in out.get(frm, []):
            out[frm].remove(pid)
        elif frm:
            continue  # player already gone from that team: move is stale, skip it
        if to and to in out and pid not in out[to]:
            out[to].append(pid)
    return out


def move_transfers(m: dict) -> list[tuple[int, int, int]]:
    return [(it["playerId"], it["from"], it["to"]) for it in m["items"]]


def reverse(m: dict) -> list[tuple[int, int, int]]:
    return [(pid, to, frm) for pid, frm, to in reversed(move_transfers(m))]


# ---------------------------------------------------------------- report

class Context:
    """Everything needed to run the season model repeatedly on different rosters."""

    def __init__(self, root: Path):
        store = espn_league.load_store()
        sel = store.get("league")
        if not sel:
            raise espn_league.EspnError("Connect your ESPN league first.", 409)
        from . import draftfeed
        self.root, self.store, self.sel = root, store, sel
        self.raw = season.fetch_raw(sel["id"], sel["season"], store)
        self.snap = draftfeed.merge(espn_league.normalize(self.raw, store.get("swid"), espn_league.my_team(store)),
                                    draftfeed.HUB.get(sel["id"]))
        espn_league.require_rosters(self.snap)
        self.ldir = espn_league.league_dir(root, sel)
        self.key = espn_league.league_key(sel)
        self.payload = json.loads((root / "data" / "players.json").read_text(encoding="utf-8"))
        self.pro = sources.espn_pro_schedule(root / "data" / "raw")
        self.me = self.snap.get("myTeamId")
        self.rosters = {t["id"]: list(t["roster"]) for t in self.snap["teams"]}
        self.moves = sync(self.ldir, fetch_transactions(sel["id"], sel["season"], store))
        self.names = {p["id"]: p["name"] for p in self.payload["players"]} | {int(k): v for k, v in self.snap.get("names", {}).items()}
        ctx = season.league_context(self.raw)
        self.model = season.Model(self.payload["players"], self.payload["meta"], self.snap["league"], self.snap, ctx, self.pro)
        self.model.setup_rates(self.rosters)

    def scenario(self, rosters: dict[int, list[int]], sims: int = SCENARIO_SIMS) -> dict:
        rep = season.report(self.payload, self.raw, self.snap, self.pro, sims=sims, seed=SEED, rosters=rosters, suggestions=False)
        mine = next((t for t in rep["teams"] if t["id"] == self.me), None)
        rank = 1 + [t["id"] for t in rep["teams"]].index(self.me) if mine else None
        warn = []
        if self.me in rosters:
            size, lim = len(rosters[self.me]), self.snap["league"]["rosterSize"]
            if size > lim:
                warn.append(f"Your roster would have {size} players; the limit is {lim}. Add a drop.")
            elif not season.lineup_ok(self.model, rosters[self.me]):
                warn.append("This breaks a position limit or leaves a starting spot you cannot fill.")
        return {"me": mine and {k: mine[k] for k in ("strength", "playoffPct", "titlePct", "proj", "avgSeed")},
                "rank": rank, "teams": rep["teams"], "warnings": warn}

    def impact(self, before: dict[int, list[int]], after: dict[int, list[int]]) -> float:
        """Change in your weekly win rate (analytic, fast) between two league states."""
        def s(r):
            weeks = {t: self.model.week(self.model.avg_games(ids, t)) for t, ids in r.items()}
            return season.strength(self.model, weeks, self.me)
        return round(s(after) - s(before), 4)


def build(root: Path, whatif: list[dict] | None = None) -> dict:
    c = Context(root)
    me, now = c.me, c.rosters
    if me is None:
        raise espn_league.EspnError("Which team is yours? Pick it in the League panel on the Draft board tab.", 409)
    mine_exec = [m for m in c.moves if m["status"] == "EXECUTED" and any(me in (it["from"], it["to"]) for it in m["items"])]
    pending = [m for m in c.moves if m["status"] == "PENDING"]

    draft_day = now
    for m in mine_exec:  # newest first, so undo in that order
        draft_day = apply(draft_day, reverse(m))
    with_pending = apply(now, [x for m in reversed(pending) for x in move_transfers(m)])

    plan_path = c.ldir / "plan.json"
    plan = list(json.loads(plan_path.read_text(encoding="utf-8")).values()) if plan_path.exists() else []
    tracked = []
    for s in sorted(plan, key=lambda s: (s["kind"] != "waiver", -s["dMe"])):
        st = plan_status(s, c.moves, now, me)
        tracked.append({**s, **st})
    open_sug = [s for s in tracked if s["state"] in ("open", "partial")]
    # remaining suggestions, best first: only the parts not already done/pending, and only if they still help
    # the roster as it will be (suggestions made against an older roster can go stale)
    remaining, used, applied_keys = with_pending, set(), []
    for s in sorted(open_sug, key=lambda s: -s["dMe"]):
        tr = [t for t, part in zip(transfers_of(s, me), s["parts"]) if part == "open"]
        if not tr or any(pid in used for pid, _, _ in tr):
            continue
        trial = apply(remaining, tr)
        ok = season.lineup_ok(c.model, trial[me]) and len(trial[me]) <= c.snap["league"]["rosterSize"]
        if ok and c.impact(remaining, trial) > 0.002:
            remaining = trial
            used |= {pid for pid, _, _ in tr}
            applied_keys.append(s["key"])

    open_spots = c.snap["league"]["rosterSize"] - len(now[me])
    scenarios = [
        {"key": "draft", "label": "Draft-day roster", "detail": f"{len(mine_exec)} of your completed moves undone", **c.scenario(draft_day)},
        {"key": "now", "label": "Now", "detail": "Rosters as ESPN shows them"
         + (f"; {open_spots} empty roster spot{'s' if open_spots > 1 else ''} until your claims process" if open_spots > 0 else ""),
         **c.scenario(now)},
        {"key": "pending", "label": "If pending moves clear", "detail": f"{len(pending)} pending", **c.scenario(with_pending)},
        {"key": "plan", "label": "Plus remaining suggestions", "detail": f"{len(applied_keys)} more moves", **c.scenario(remaining)},
    ]
    custom = None
    if whatif:
        tr = [x for w in whatif for x in (move_transfers(w) if "items" in w else transfers_of(w, me))]
        custom = {"key": "custom", "label": "Your what-if", "detail": f"{len(whatif)} move{'s' if len(whatif) != 1 else ''} on top of pending", **c.scenario(apply(with_pending, tr))}

    for m in c.moves:
        involved = any(me in (it["from"], it["to"]) for it in m["items"])
        m["mine"] = involved
        if not involved or m["status"] not in ("PENDING", "EXECUTED"):
            continue
        if m["status"] == "PENDING":
            m["impact"] = c.impact(now, apply(now, move_transfers(m)))
        else:
            m["impact"] = c.impact(apply(now, reverse(m)), now)
        made = dt.datetime.fromtimestamp((m.get("proposed") or 0) / 1000).strftime("%Y-%m-%d %H:%M")
        m["suggested"] = [s["key"] for s in tracked if s["state"] != "open" and s["firstSeen"] <= made
                          and any(t[0] == it["playerId"] and t[2] == it["to"] for t in transfers_of(s, me) for it in m["items"])]

    hist_path = c.ldir / "history.jsonl"
    history = [json.loads(x) for x in hist_path.read_text(encoding="utf-8").splitlines() if x.strip()] if hist_path.exists() else []
    avail = sorted((p for p in c.payload["players"] if str(p["id"]) not in c.snap["owners"] and p["team"] != "FA"),
                   key=lambda p: -c.model.value[p["id"]])[:80]
    return {
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "leagueKey": c.key, "leagueName": c.snap["league"]["name"],
        "myTeamId": me, "rosterSize": c.snap["league"]["rosterSize"],
        "scenarios": scenarios, "custom": custom,
        "moves": [m for m in c.moves if m.get("mine") or m["type"].startswith("TRADE")][:40],
        "tracked": tracked, "planApplied": applied_keys,
        "history": history[-120:],
        "teams": [{"id": t["id"], "name": t["name"], "abbrev": t["abbrev"], "roster": now[t["id"]]} for t in c.snap["teams"]],
        "available": [p["id"] for p in avail],
        "names": {str(k): v for k, v in c.names.items()
                  if k in {i for r in now.values() for i in r} | {p["id"] for p in avail}
                  | {it["playerId"] for m in c.moves for it in m["items"]} | {i for s in plan for i in transfers_ids(s)}},
    }


def transfers_ids(s: dict) -> list[int]:
    if s["kind"] == "waiver":
        return [s["add"], s["drop"]]
    return list(s["give"]) + list(s["get"])


def append_history(ldir: Path, rep: dict) -> None:
    """One line per season report: how your odds move over the season."""
    mine = next((t for t in rep["teams"] if t["isMine"]), None)
    if not mine:
        return
    path = ldir / "history.jsonl"
    line = {"at": rep["generated"], "strength": mine["strength"], "playoffPct": mine["playoffPct"],
            "titlePct": mine["titlePct"], "projW": mine["proj"]["w"], "projL": mine["proj"]["l"]}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
