"""Data fetchers for Draft Mantis.

Sources (all public, no API key):
  * ESPN Fantasy API   - 2026-27 projections, 2025-26 actuals, ADP, auction $, injury status, outlook
  * Basketball-Reference - per-game history (games played -> durability), age, usage rate
  * Sleeper API        - birth dates / years of experience (age for rookies & movers)

Raw responses are cached under data/raw/ so re-scoring never re-hits the network
unless --refresh is passed.
"""

from __future__ import annotations

import json
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from . import config, net

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _get(url: str, headers: dict | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with net.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _cached(path: Path, fetch, refresh: bool) -> bytes:
    if path.exists() and not refresh:
        return path.read_bytes()
    body = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return body


# ---------------------------------------------------------------- ESPN

def espn_players(raw_dir: Path, refresh: bool = False) -> list[dict]:
    """Two pulls merged by player id: ESPN's draft-rank order (preseason value) and % rostered order
    (catches in-season breakouts that were never in the draft top 400)."""
    season = config.ESPN_SEASON
    url = (
        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{season}"
        "/segments/0/leaguedefaults/1?view=kona_player_info"
    )
    # 10<season> = ESPN projection, 00<season> = this season so far, 02<season> = last 15 days, 00<season-1> = last season
    stats = {"value": 2, "additionalValue": [f"10{season}", f"00{season}", f"02{season}", f"00{season - 1}"]}
    pulls = [
        ("espn_players.json", {"sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"}}),
        ("espn_players_owned.json", {"sortPercOwned": {"sortPriority": 1, "sortAsc": False}}),
    ]
    merged: dict[int, dict] = {}
    for fname, sort in pulls:
        flt = {"players": {"limit": config.ESPN_POOL_LIMIT, **sort, "filterStatsForTopScoringPeriodIds": stats}}
        body = _cached(raw_dir / fname,
                       lambda: _get(url, {"x-fantasy-filter": json.dumps(flt), "Accept": "application/json"}), refresh)
        for p in json.loads(body)["players"]:
            merged.setdefault(p["id"], p)
    return list(merged.values())


def espn_pro_schedule(raw_dir: Path, refresh: bool = False) -> dict[str, list[int]]:
    """NBA team abbrev -> ESPN scoring periods (days; 1 = opening night) with a game."""
    url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{config.ESPN_SEASON}?view=proTeamSchedules_wl"
    body = _cached(raw_dir / "espn_proschedule.json", lambda: _get(url), refresh)
    out = {}
    for t in json.loads(body)["settings"]["proTeams"]:
        days = sorted(int(k) for k in (t.get("proGamesByScoringPeriod") or {}))
        if days:
            out[t["abbrev"]] = days
    return out


def espn_teams(raw_dir: Path, refresh: bool = False) -> dict[int, str]:
    url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{config.ESPN_SEASON}?view=proTeamSchedules_wl"
    body = _cached(raw_dir / "espn_teams.json", lambda: _get(url), refresh)
    return {t["id"]: t["abbrev"] for t in json.loads(body)["settings"]["proTeams"]}


# ---------------------------------------------------------------- Basketball-Reference

class _BrefTable(HTMLParser):
    """Collects rows of one <table id=...> as {data-stat: text} dicts."""

    def __init__(self, table_id: str):
        super().__init__()
        self.table_id = table_id
        self.in_table = False
        self.depth = 0
        self.row: dict | None = None
        self.cell: str | None = None
        self.rows: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("id") == self.table_id:
            self.in_table, self.depth = True, 1
            return
        if not self.in_table:
            return
        if tag == "table":
            self.depth += 1
        elif tag == "tr":
            self.row = {}
        elif tag in ("td", "th") and self.row is not None and "data-stat" in a:
            self.cell = a["data-stat"]
            self.row[self.cell] = ""
            if a.get("data-append-csv"):
                self.row["bref_id"] = a["data-append-csv"]

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag == "table":
            self.depth -= 1
            self.in_table = self.depth > 0
        elif tag in ("td", "th"):
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row.get("name_display") and self.row.get("bref_id"):
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_table and self.row is not None and self.cell:
            self.row[self.cell] += data


def bref_table(raw_dir: Path, end_year: int, kind: str, refresh: bool = False) -> list[dict]:
    """kind: 'per_game' or 'advanced'. Traded players keep only their combined (first) row."""
    url = f"https://www.basketball-reference.com/leagues/NBA_{end_year}_{kind}.html"
    path = raw_dir / f"bref_{end_year}_{kind}.html"
    fetched = refresh or not path.exists()
    body = _cached(path, lambda: _get(url), refresh)
    if fetched:
        time.sleep(4)  # bref allows ~20 req/min
    parser = _BrefTable(f"{kind}_stats" if kind == "per_game" else kind)
    parser.feed(body.decode("utf-8", "replace"))
    seen, out = set(), []
    for r in parser.rows:
        if r["bref_id"] in seen:
            continue
        seen.add(r["bref_id"])
        out.append(r)
    return out


# ---------------------------------------------------------------- Sleeper

def sleeper_players(raw_dir: Path, refresh: bool = False) -> list[dict]:
    body = _cached(raw_dir / "sleeper_players.json", lambda: _get("https://api.sleeper.app/v1/players/nba"), refresh)
    return [p for p in json.loads(body).values() if p.get("full_name")]
