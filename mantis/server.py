"""Local Draft Mantis server: serves the board and proxies your ESPN league so the page can poll it live.

Binds to 127.0.0.1 only. Your ESPN cookies stay on this machine and are never sent to the browser.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import config, draftfeed, espn_league, moves, season, site

LEAGUE_TTL = 1.0       # seconds; the page polls every 1.5-2.5s during a live draft
LEAGUE_TTL_FEED = 10.0  # while the Firefox add-on streams the draft room, ESPN's read API is only a backstop
MAX_FEED_BODY = 400_000


class State:
    lock = threading.Lock()
    season_lock = threading.Lock()
    moves_cache: tuple[float, dict] | None = None
    cache: tuple[tuple, float, dict] | None = None
    rebuild = {"running": False, "error": None, "finished": None}


def _status() -> dict:
    s = espn_league.load_store()
    swid = s.get("swid") or ""
    return {
        "hasCredentials": bool(swid and s.get("espn_s2")),
        "swid": (swid[:5] + "…" + swid[-4:]) if len(swid) > 10 else "",
        "league": s.get("league"),
        "defaultSeason": config.ESPN_SEASON,
        "rebuild": State.rebuild,
        "feeds": {str(k): v.status() for k, v in draftfeed.HUB.feeds.items()},
        "dataGenerated": _data_generated(),
    }


_gen_cache: dict = {}


def _data_generated() -> str | None:
    """meta.generated of data/players.json (re-read only when the file changes); the board reloads when it moves."""
    path = site.ROOT / "data" / "players.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _gen_cache.get("mtime") != mtime:
        _gen_cache.update(mtime=mtime, value=json.loads(path.read_text())["meta"]["generated"])
    return _gen_cache["value"]


def _roster_sig() -> str | None:
    """Changes whenever any roster in the league changes (a waiver clears, a trade goes through)."""
    try:
        snap = _league_snapshot()
    except espn_league.EspnError:
        return None
    return str(hash(tuple(sorted((t["id"], tuple(sorted(t["roster"]))) for t in snap["teams"]))))


def _season(force: bool = False, sims: int = season.DEFAULT_SIMS) -> dict:
    """Season report, cached in data/season.json until the player data, the league or 30 minutes change."""
    path = site.ROOT / "data" / "season.json"
    sel = espn_league.load_store().get("league")
    with State.season_lock:
        if not force and path.exists():
            rep = json.loads(path.read_text())
            fresh = time.time() - path.stat().st_mtime < 30 * 60
            same = rep.get("dataGenerated") == _data_generated() and rep.get("leagueId") == (sel or {}).get("id")
            if fresh and same and rep.get("rosterSig") == _roster_sig():
                return rep
        rep = season.run(site.ROOT, sims=sims)
        rep["leagueId"] = (sel or {}).get("id")
        rep["rosterSig"] = _roster_sig()
        path.write_text(json.dumps(rep))
        return rep


def _league_snapshot(force: bool = False) -> dict:
    s = espn_league.load_store()
    sel = s.get("league")
    if not sel:
        raise espn_league.EspnError("No league connected yet.", 409)
    key = (sel["id"], sel["season"], s.get("swid"), s.get("espn_s2"))
    feed = draftfeed.HUB.get(sel["id"])
    ttl = LEAGUE_TTL_FEED if feed and feed.active else LEAGUE_TTL
    with State.lock:
        cached = State.cache if State.cache and State.cache[0] == key else None
    if force or not cached or time.time() - cached[1] >= ttl:
        raw = espn_league.fetch_league(sel["id"], sel["season"], s)
        cached = (key, time.time(), espn_league.normalize(raw, s.get("swid")))
        with State.lock:
            State.cache = cached
    return draftfeed.merge(copy.deepcopy(cached[2]), feed)


def _run_rebuild():
    from .__main__ import build
    State.rebuild.update(running=True, error=None)
    try:
        build(refresh=True)
    except Exception as e:  # surfaced in the UI
        State.rebuild["error"] = str(e)
        traceback.print_exc()
    finally:
        State.rebuild.update(running=False, finished=time.strftime("%H:%M:%S"))


class Handler(BaseHTTPRequestHandler):
    server_version = "DraftMantis/1"

    def log_message(self, fmt, *args):
        if not any(x in (args[0] if args else "") for x in ("/api/league", "/api/draftfeed", "/api/status")):  # keep polls quiet
            super().log_message(fmt, *args)

    # -- helpers
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self, limit: int = 20000) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise espn_league.EspnError("Request too large.", 413)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise espn_league.EspnError("Request body must be JSON.", 400) from None

    def _allowed(self) -> bool:
        # Block DNS-rebinding (foreign Host) and cross-site writes (foreign Origin).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if self.command != "GET" and origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            # the Firefox add-on's background script may post draft-room lines, nothing else
            return origin.startswith("moz-extension://") and urlparse(self.path).path == "/api/draftfeed"
        return True

    def _route(self):
        if not self._allowed():
            return self._json(403, {"error": "Forbidden"})
        path = urlparse(self.path).path
        try:
            handler = ROUTES.get((self.command, path))
            if not handler:
                return self._json(404, {"error": "Not found"})
            handler(self)
        except espn_league.EspnError as e:
            self._json(e.status if 400 <= e.status < 600 else 502, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": f"Server error: {e}"})

    do_GET = do_POST = do_DELETE = _route

    # -- routes
    def index(self):
        self._send(200, site.render(live=True).encode(), "text/html; charset=utf-8")

    def status(self):
        self._json(200, _status())

    def save_credentials(self):
        b = self._body()
        swid, s2 = espn_league.normalize_swid(str(b.get("swid", ""))), str(b.get("espn_s2", "")).strip()
        if len(swid) < 30 or len(s2) < 50:
            raise espn_league.EspnError("SWID should look like {XXXXXXXX-XXXX-...} and espn_s2 is a long string "
                                        "(usually 200+ characters). Copy both from your browser's espn.com cookies.", 400)
        s = espn_league.load_store()
        s.update(swid=swid, espn_s2=s2)
        espn_league.save_store(s)
        State.cache = None
        self._json(200, _status())

    def clear_credentials(self):
        s = espn_league.load_store()
        s.pop("swid", None)
        s.pop("espn_s2", None)
        espn_league.save_store(s)
        State.cache = None
        self._json(200, _status())

    def leagues(self):
        self._json(200, {"leagues": espn_league.find_leagues(espn_league.load_store())})

    def connect(self):
        b = self._body()
        try:
            league_id, season = int(b.get("id")), int(b.get("season") or config.ESPN_SEASON)
        except (TypeError, ValueError):
            raise espn_league.EspnError("League ID must be a number, like the leagueId=… value in your league URL.", 400) from None
        s = espn_league.load_store()
        raw = espn_league.fetch_league(league_id, season, s)  # validates access before saving
        s["league"] = {"id": league_id, "season": season}
        espn_league.save_store(s)
        snap = espn_league.normalize(raw, s.get("swid"))
        State.cache = ((league_id, season, s.get("swid"), s.get("espn_s2")), time.time(), snap)
        self._json(200, snap)

    def disconnect(self):
        s = espn_league.load_store()
        s.pop("league", None)
        espn_league.save_store(s)
        State.cache = None
        self._json(200, _status())

    def league(self):
        self._json(200, _league_snapshot())

    def draftfeed(self):
        b = self._body(MAX_FEED_BODY)
        lines = b.get("lines")
        try:
            league_id = int(b.get("leagueId"))
            team_id = int(b["teamId"]) if b.get("teamId") not in (None, "") else None
        except (TypeError, ValueError):
            raise espn_league.EspnError("leagueId must be a number.", 400) from None
        if not isinstance(lines, list) or len(lines) > 20000 or not all(isinstance(x, str) and len(x) <= 2000 for x in lines):
            raise espn_league.EspnError("lines must be a list of strings.", 400)
        self._json(200, draftfeed.HUB.ingest(league_id, lines, team_id))

    def events(self):
        """Server-sent events: one message per draft-room change, so the board refreshes instantly."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        version = draftfeed.HUB.version
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                new = draftfeed.HUB.wait(version, timeout=15)
                self.wfile.write(f"data: {new}\n\n".encode() if new != version else b": ping\n\n")
                self.wfile.flush()
                version = new
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def season_get(self):
        self._json(200, _season())

    def season_run(self):
        b = self._body()
        sims = min(max(int(b.get("sims") or season.DEFAULT_SIMS), 100), 5000)
        self._json(200, _season(force=True, sims=sims))

    def moves_get(self):
        fresh = "fresh=1" in (urlparse(self.path).query or "")
        if not fresh and State.moves_cache and time.time() - State.moves_cache[0] < 45:
            return self._json(200, State.moves_cache[1])
        rep = moves.build(site.ROOT)
        State.moves_cache = (time.time(), rep)
        (site.ROOT / "data" / "moves.json").write_text(json.dumps(rep))  # feeds the hosted snapshot
        self._json(200, rep)

    def whatif(self):
        b = self._body()
        ms = b.get("moves")
        if not isinstance(ms, list) or len(ms) > 12:
            raise espn_league.EspnError("Send up to 12 moves.", 400)
        clean = []
        for m in ms:
            try:
                if "partner" in m:
                    clean.append({"partner": int(m["partner"]), "give": [int(x) for x in m["give"]], "get": [int(x) for x in m["get"]]})
                else:
                    clean.append({"add": [int(x) for x in m.get("add") or []], "drop": [int(x) for x in m.get("drop") or []]})
            except (TypeError, ValueError, KeyError):
                raise espn_league.EspnError("Each move needs player ids: add/drop, or partner/give/get.", 400) from None
        self._json(200, moves.build(site.ROOT, whatif=clean))

    def rebuild(self):
        if not State.rebuild["running"]:
            threading.Thread(target=_run_rebuild, daemon=True).start()
            State.rebuild["running"] = True
        self._json(202, State.rebuild)


ROUTES = {
    ("GET", "/"): Handler.index,
    ("GET", "/api/status"): Handler.status,
    ("POST", "/api/credentials"): Handler.save_credentials,
    ("DELETE", "/api/credentials"): Handler.clear_credentials,
    ("GET", "/api/leagues"): Handler.leagues,
    ("POST", "/api/league"): Handler.connect,
    ("DELETE", "/api/league"): Handler.disconnect,
    ("GET", "/api/league"): Handler.league,
    ("POST", "/api/rebuild"): Handler.rebuild,
    ("POST", "/api/draftfeed"): Handler.draftfeed,
    ("GET", "/api/events"): Handler.events,
    ("GET", "/api/season"): Handler.season_get,
    ("POST", "/api/season"): Handler.season_run,
    ("GET", "/api/moves"): Handler.moves_get,
    ("POST", "/api/whatif"): Handler.whatif,
}


def serve(port: int = 8765, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True  # open event streams must not block Ctrl+C
    url = f"http://localhost:{port}/"
    print(f"Draft Mantis live board: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
