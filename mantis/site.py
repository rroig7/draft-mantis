"""Render the draft board HTML from web/template.html + data/players.json."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def snapshot() -> dict | None:
    """Last season report + moves report, embedded in the hosted (static) copy of the board."""
    from . import espn_league
    sel = espn_league.load_store().get("league")
    if not sel:
        return None
    ldir = espn_league.league_dir(ROOT, sel)
    season, moves = ldir / "season.json", ldir / "moves.json"
    if not season.exists():
        return None
    snap = {"season": json.loads(season.read_text(encoding="utf-8")), "moves": json.loads(moves.read_text(encoding="utf-8")) if moves.exists() else None}
    snap["at"] = (snap["moves"] or snap["season"]).get("generated")
    return snap


def render(payload: dict | None = None, live: bool = False, snap: dict | None = None) -> str:
    if payload is None:
        payload = json.loads((ROOT / "data" / "players.json").read_text(encoding="utf-8"))
    tpl = (ROOT / "web" / "template.html").read_text(encoding="utf-8")
    out = (tpl.replace("/*__MANTIS_DATA__*/null", json.dumps(payload).replace("</", "<\\/"))
              .replace("/*__MANTIS_LIVE__*/false", "true" if live else "false"))
    if snap and not live:
        out = out.replace("/*__MANTIS_SNAPSHOT__*/null", json.dumps(snap).replace("</", "<\\/"))
    return out
