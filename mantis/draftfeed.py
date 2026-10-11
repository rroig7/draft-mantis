"""Live draft-room feed from the Draft Mantis Bridge Firefox add-on.

The add-on listens (read-only) to the ESPN draft room's connection to fantasydraft.espn.com in your own
browser and forwards its command lines here. Observed formats (community-documented, unofficial):

    SELECTING <teamId> <msOnClock>        team is on the clock
    SELECTED  <teamId> <playerId> <n>     team drafted player

ESPN replays the whole draft to a client that (re)joins, so picks are de-duplicated by player.
Commissioner undos are not handled: their message format has not been observed. A rescheduled or
re-ordered draft is: FeedHub.reconcile() checks every ESPN snapshot and drops picks from the old draft.
"""

from __future__ import annotations

import threading
import time
from collections import Counter

ACTIVE_FOR = 15 * 60  # seconds after the last event that the feed counts as live


class LeagueFeed:
    def __init__(self, league_id: int):
        self.league_id = league_id
        self.picks: list[dict] = []
        self.seen: set[int] = set()
        self.clock: dict | None = None
        self.lines = 0
        self.commands: Counter = Counter()
        self.last_event = 0.0
        self.team_id: int | None = None

    def ingest(self, line: str) -> bool:
        parts = line.strip().split()
        if not parts:
            return False
        cmd = parts[0].upper()
        self.lines += 1
        self.commands[cmd] += 1
        self.last_event = time.time()
        try:
            if cmd == "SELECTED" and len(parts) >= 3:
                team, player = int(parts[1]), int(parts[2])
                if player > 0 and player not in self.seen:
                    self.seen.add(player)
                    self.picks.append({"teamId": team, "playerId": player})
                    self.clock = None
                    return True
            elif cmd == "SELECTING" and len(parts) >= 2:
                ms = int(parts[2]) if len(parts) >= 3 else None
                self.clock = {"teamId": int(parts[1]), "ms": ms, "at": time.time()}
                return True
        except ValueError:
            self.commands["unparsed"] += 1
        return False

    def reset(self) -> None:
        """Forget picks and clock from a draft the commissioner has since reset or rescheduled."""
        self.picks, self.seen, self.clock = [], set(), None

    @property
    def active(self) -> bool:
        return bool(self.last_event) and time.time() - self.last_event < ACTIVE_FOR

    def status(self) -> dict:
        return {
            "active": self.active, "lines": self.lines, "picks": len(self.picks),
            "secondsSinceEvent": round(time.time() - self.last_event, 1) if self.last_event else None,
            "commands": dict(self.commands.most_common(12)),
        }


class FeedHub:
    """Thread-safe store of feeds by league, with a version counter the event stream waits on."""

    def __init__(self):
        self.cond = threading.Condition()
        self.version = 0
        self.feeds: dict[int, LeagueFeed] = {}
        self.drafts: dict[int, dict] = {}  # league -> schedule last confirmed from ESPN, see reconcile()

    def ingest(self, league_id: int, lines: list[str], team_id: int | None = None) -> dict:
        with self.cond:
            feed = self.feeds.setdefault(league_id, LeagueFeed(league_id))
            if team_id is not None:
                feed.team_id = team_id
            changed = sum(feed.ingest(ln) for ln in lines)
            if changed:
                self.version += 1
                self.cond.notify_all()
            return {"accepted": len(lines), "changed": changed, **feed.status()}

    def reconcile(self, league_id: int, draft: dict, fetched_at: float) -> dict | None:
        """Validate the draft against ESPN on every league fetch; returns the latest reschedule, if any.

        A draft is identified by its start time, type and pick order. When the commissioner changes any of
        them (a reschedule, teams added or removed), the draft room's picks for the old draft no longer apply
        and are dropped. A new schedule counts only once two separate fetches agree, because ESPN's read API
        briefly serves half-edited settings (no start time) while the commissioner is still making changes."""
        sess = draft_session(draft)
        with self.cond:
            st = self.drafts.get(league_id)
            if st is None:
                self.drafts[league_id] = {"session": sess, "date": draft.get("date"), "change": None}
                return None
            if sess == st["session"]:
                st.pop("cand", None)
                return st["change"]
            cand = st.get("cand")
            if not cand or cand[0] != sess:
                st["cand"] = (sess, fetched_at)
                return st["change"]
            if cand[1] == fetched_at:  # same fetch served twice from cache: not a second opinion
                return st["change"]
            prev = st["date"] or (st["change"] or {}).get("from")
            st.update(session=sess, date=draft.get("date"),
                      change={"from": prev, "to": draft.get("date"), "at": int(time.time() * 1000)})
            st.pop("cand")
            feed = self.feeds.get(league_id)
            if feed:
                feed.reset()
            self.version += 1
            self.cond.notify_all()
            return st["change"]

    def get(self, league_id) -> LeagueFeed | None:
        with self.cond:
            return self.feeds.get(league_id)

    def wait(self, seen_version: int, timeout: float) -> int:
        with self.cond:
            self.cond.wait_for(lambda: self.version != seen_version, timeout)
            return self.version


def draft_session(draft: dict) -> tuple:
    return draft.get("date"), draft.get("type"), tuple(draft.get("pickOrder") or ())


def merge(snap: dict, feed: LeagueFeed | None) -> dict:
    """Overlay draft-room picks and the on-clock team onto a normalized ESPN league snapshot."""
    draft = snap["draft"]
    if not feed or not feed.last_event:
        snap["feed"] = {"active": False}
        return snap
    if not feed.active and not draft["inProgress"] and not draft["drafted"]:
        # ESPN says no draft is running and the room has been quiet: whatever it streamed earlier is stale
        snap["feed"] = {**feed.status(), "active": False}
        return snap
    api_picks = draft["picks"]
    if len(feed.picks) >= len(api_picks):
        # The draft room is ahead (ESPN's read API reports little or nothing mid-draft): its order is the pick order.
        draft["picks"] = [{"pick": i + 1, "round": None, "teamId": p["teamId"], "playerId": p["playerId"],
                           "keeper": False, "bid": None} for i, p in enumerate(feed.picks)]
    teams = {t["id"]: t for t in snap["teams"]}
    for p in draft["picks"]:
        pid, tid = p["playerId"], p["teamId"]
        if str(pid) not in snap["owners"]:
            snap["owners"][str(pid)] = tid
            if tid in teams and pid not in teams[tid]["roster"]:
                teams[tid]["roster"].append(pid)
    if feed.active and not draft["drafted"]:
        draft["inProgress"] = True
    if feed.clock and feed.active:
        left = None
        if feed.clock["ms"] is not None:
            left = max(0, int(feed.clock["ms"] - (time.time() - feed.clock["at"]) * 1000))
        draft["clock"] = {"teamId": feed.clock["teamId"], "msLeft": left}
    snap["feed"] = feed.status()
    return snap


HUB = FeedHub()
