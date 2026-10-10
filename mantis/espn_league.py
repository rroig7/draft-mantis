"""ESPN fantasy league connection (unofficial v3 API).

* League data:  lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{season}/segments/0/leagues/{id}
                views mSettings, mTeam, mRoster, mDraftDetail. Public leagues need no auth.
* Private leagues and "find my leagues" need the ESPN login cookies SWID and espn_s2.
* League discovery: fan.api.espn.com/apis/v2/fans/{SWID} lists the fantasy entries tied to the account.

Credentials are stored only on this machine, in ~/.draft-mantis/espn.json (mode 600).
Env vars ESPN_SWID / ESPN_S2 override the file.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

LEAGUE_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{season}/segments/0/leagues/{league_id}"
FAN_URL = "https://fan.api.espn.com/apis/v2/fans/{swid}"
STORE = Path(os.environ.get("MANTIS_HOME", Path.home() / ".draft-mantis")) / "espn.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

CAT_STATS = {"0": "pts", "6": "reb", "3": "ast", "2": "stl", "1": "blk", "17": "tpm", "19": "fgp", "20": "ftp", "11": "to"}
POINT_STATS = {
    "0": "pts", "1": "blk", "2": "stl", "3": "ast", "6": "reb", "11": "to", "13": "fgm", "14": "fga",
    "15": "ftm", "16": "fta", "17": "tpm", "18": "tpa", "23": "fgmi", "24": "ftmi", "25": "tpmi",
}
STAT_NAMES = {
    "4": "OREB", "5": "DREB", "7": "EJ", "8": "FF", "9": "PF", "10": "TF", "12": "DQ", "13": "FGM", "14": "FGA",
    "15": "FTM", "16": "FTA", "18": "3PA", "21": "3P%", "22": "eFG%", "35": "A/TO", "37": "DD", "38": "TD",
    "39": "QD", "40": "MIN", "41": "GS", "42": "GP",
}
IR_SLOT = "13"
POSITION_IDS = {"1": "PG", "2": "SG", "3": "SF", "4": "PF", "5": "C"}


class EspnError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- credentials / selection store

def load_store() -> dict:
    data = {}
    if STORE.exists():
        try:
            data = json.loads(STORE.read_text())
        except json.JSONDecodeError:
            data = {}
    if os.environ.get("ESPN_SWID"):
        data["swid"] = os.environ["ESPN_SWID"]
    if os.environ.get("ESPN_S2"):
        data["espn_s2"] = os.environ["ESPN_S2"]
    return data


def save_store(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(STORE)


def normalize_swid(swid: str) -> str:
    s = swid.strip().strip("{}").upper()
    return "{" + s + "}" if s else ""


# ---------------------------------------------------------------- HTTP

def _get_json(url: str, cookies: dict | None = None, timeout: int = 20, fresh: bool = False):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if fresh:  # ask any cache in front of ESPN's read API for the newest copy
        headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})
    if cookies and cookies.get("swid") and cookies.get("espn_s2"):
        headers["Cookie"] = f"SWID={cookies['swid']}; espn_s2={cookies['espn_s2']}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise EspnError(f"HTTP {e.code}", e.code) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise EspnError(f"Could not reach ESPN ({getattr(e, 'reason', e)}). Check your connection.", 504) from None


def fetch_league(league_id: int, season: int, creds: dict | None = None) -> dict:
    url = LEAGUE_URL.format(season=season, league_id=league_id) + "?" + urllib.parse.urlencode(
        [("view", v) for v in ("mSettings", "mTeam", "mRoster", "mDraftDetail", "mStatus")])
    try:
        return _get_json(url, creds, timeout=10, fresh=True)
    except EspnError as e:
        if e.status == 401:
            msg = ("ESPN says league {id} is private. Add your SWID and espn_s2 cookies, then try again."
                   if not (creds and creds.get("espn_s2")) else
                   "ESPN rejected your cookies for league {id}. They may have expired: copy fresh SWID and espn_s2 values.")
            raise EspnError(msg.format(id=league_id), 401) from None
        if e.status == 404:
            raise EspnError(f"No NBA fantasy league {league_id} found for the {season - 1}-{str(season)[2:]} season. "
                            "Check the leagueId in your league's URL.", 404) from None
        raise


def find_leagues(creds: dict) -> list[dict]:
    """List the account's fantasy basketball leagues via the ESPN fan API, verified against the league API."""
    swid = creds.get("swid")
    if not swid or not creds.get("espn_s2"):
        raise EspnError("Add your SWID and espn_s2 cookies first.", 400)
    url = FAN_URL.format(swid=urllib.parse.quote(swid)) + "?" + urllib.parse.urlencode({
        "displayHiddenPrefs": "true", "context": "fantasy", "useCookieAuth": "true",
        "source": "fantasyapp-ios", "featureFlags": "challengeEntries",
    })
    try:
        fan = _get_json(url, creds)
    except EspnError as e:
        if e.status in (401, 403, 404):
            raise EspnError("ESPN did not recognize that SWID. Copy it again, including the curly braces.", 401) from None
        raise

    candidates: dict[tuple[int, int], dict] = {}

    def walk(node):
        if isinstance(node, dict):
            groups = node.get("groups")
            if isinstance(groups, list) and groups and isinstance(groups[0], dict) and "groupId" in groups[0]:
                tag = str(node.get("abbrev") or node.get("gameAbbrev") or node.get("entryURL") or "").lower()
                if not tag or "fba" in tag or "basketball" in tag:
                    season = int(node.get("seasonId") or config.ESPN_SEASON)
                    for g in groups:
                        key = (int(g["groupId"]), season)
                        candidates.setdefault(key, {
                            "id": key[0], "season": season, "name": g.get("groupName") or f"League {key[0]}",
                            "teamId": node.get("entryId"),
                            "teamName": " ".join(x for x in (node.get("entryLocation"), node.get("entryNickname")) if x)
                                        or node.get("entryName") or "",
                        })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(fan)
    found = []
    # newest seasons first; verify each is a real basketball league this account can open
    for c in sorted(candidates.values(), key=lambda c: (-c["season"], c["name"]))[:20]:
        try:
            lg = _get_json(LEAGUE_URL.format(season=c["season"], league_id=c["id"]) + "?view=mSettings", creds)
        except EspnError:
            continue
        st = lg.get("settings", {})
        c["name"] = st.get("name") or c["name"]
        c["size"] = st.get("size")
        c["scoring"] = (st.get("scoringSettings") or {}).get("scoringType")
        found.append(c)
    return found


# ---------------------------------------------------------------- normalize

def _team_name(t: dict) -> str:
    return t.get("name") or " ".join(x for x in (t.get("location"), t.get("nickname")) if x) or f"Team {t['id']}"


def normalize(raw: dict, swid: str | None = None) -> dict:
    """Reduce ESPN's league payload to what the draft board needs."""
    st = raw.get("settings") or {}
    sc = st.get("scoringSettings") or {}
    scoring_type = sc.get("scoringType") or "H2H_CATEGORY"
    items = sc.get("scoringItems") or []
    is_points = "POINTS" in scoring_type

    cats, points, unsupported = [], {}, []
    for it in items:
        sid = str(it.get("statId"))
        if is_points:
            if sid in POINT_STATS:
                points[POINT_STATS[sid]] = points.get(POINT_STATS[sid], 0) + float(it.get("points") or 0)
            elif it.get("points"):
                unsupported.append(f"{STAT_NAMES.get(sid, 'stat ' + sid)} ({it['points']:+g})")
        elif sid in CAT_STATS:
            cats.append(CAT_STATS[sid])
        else:
            unsupported.append(STAT_NAMES.get(sid, "stat " + sid))

    rs = st.get("rosterSettings") or {}
    slots = rs.get("lineupSlotCounts") or {}
    roster_size = sum(int(v) for k, v in slots.items() if str(k) != IR_SLOT) or config.ROSTER_SIZE
    lineup = {str(k): int(v) for k, v in slots.items() if int(v) > 0 and str(k) != IR_SLOT}
    # positionLimits is keyed by a player's primary (default) position id; 0 means no limit
    limits = {POSITION_IDS[str(k)]: int(v) for k, v in (rs.get("positionLimits") or {}).items()
              if str(k) in POSITION_IDS and int(v) > 0}
    ds = st.get("draftSettings") or {}
    dd = raw.get("draftDetail") or {}

    me = normalize_swid(swid) if swid else ""
    names: dict[int, str] = {}
    positions: dict[str, list] = {}  # rostered players the board may not know: [primary position, eligible slots]
    owner_of: dict[int, int] = {}
    teams = []
    for t in raw.get("teams") or []:
        owners = [normalize_swid(o) for o in (t.get("owners") or []) if isinstance(o, str)]
        if t.get("primaryOwner"):
            owners.append(normalize_swid(t["primaryOwner"]))
        roster = []
        for e in (t.get("roster") or {}).get("entries") or []:
            pid = e.get("playerId")
            if pid is None:
                continue
            roster.append(pid)
            owner_of[pid] = t["id"]
            player = ((e.get("playerPoolEntry") or {}).get("player")) or {}
            if player.get("fullName"):
                names[pid] = player["fullName"]
            if player.get("defaultPositionId"):
                positions[str(pid)] = [POSITION_IDS.get(str(player["defaultPositionId"]), "UT"),
                                       [x for x in player.get("eligibleSlots") or [] if x <= 11]]
        rec = ((t.get("record") or {}).get("overall")) or {}
        teams.append({
            "id": t["id"], "name": _team_name(t), "abbrev": t.get("abbrev") or str(t["id"]),
            "roster": roster, "isMine": bool(me and me in owners),
            "record": f"{rec.get('wins', 0)}-{rec.get('losses', 0)}" + (f"-{rec['ties']}" if rec.get("ties") else ""),
        })

    picks = []
    for p in dd.get("picks") or []:
        pid = p.get("playerId")
        if pid is None or pid <= 0:
            continue
        picks.append({"pick": p.get("overallPickNumber"), "round": p.get("roundId"), "teamId": p.get("teamId"),
                      "playerId": pid, "keeper": bool(p.get("keeper")), "bid": p.get("bidAmount") or None})
        owner_of.setdefault(pid, p.get("teamId"))
    picks.sort(key=lambda p: p["pick"] or 0)

    my = next((t["id"] for t in teams if t["isMine"]), None)
    return {
        "league": {
            "id": raw.get("id"), "season": raw.get("seasonId"), "name": st.get("name") or f"League {raw.get('id')}",
            "size": st.get("size") or len(teams), "scoringType": scoring_type,
            "format": "points" if is_points else "cats", "cats": cats, "points": points, "unsupported": unsupported,
            "rosterSize": roster_size, "lineup": lineup, "limits": limits,
        },
        "draft": {
            "type": ds.get("type"), "date": ds.get("date"), "pickOrder": ds.get("pickOrder") or [],
            "secondsPerPick": ds.get("timePerSelection"),
            "inProgress": bool(dd.get("inProgress")), "drafted": bool(dd.get("drafted")), "picks": picks,
        },
        "myTeamId": my,
        "teams": teams,
        "owners": {str(k): v for k, v in owner_of.items()},
        "names": {str(k): v for k, v in names.items()},
        "positions": positions,
        "fetched": time.strftime("%H:%M:%S"),
    }
