"""Draft Mantis CLI.

  python3 -m mantis build [--refresh]            fetch (cached) data, score, write data/ + dist/draft-mantis.html
  python3 -m mantis top [-n 50] [--format 9cat|8cat|points] [--punt ft to] [--teams 12] [--pos C]
  python3 -m mantis serve [--port 8765]          live board on localhost, synced to your ESPN league
  python3 -m mantis login                        save ESPN SWID / espn_s2 cookies (private leagues)
  python3 -m mantis leagues                      list the NBA fantasy leagues on your ESPN account
  python3 -m mantis league [--id 123 --season 2027]   connect a league / show its live draft state
  python3 -m mantis addon                        package the Firefox add-on (firefox-addon/) as dist/*.xpi
  python3 -m mantis season [--sims 2000]         projected standings, playoff/title odds, trades, waivers
  python3 -m mantis daily                        refresh all data and the season report (what the scheduler runs)
  python3 -m mantis schedule install|remove|status [--at 07:30]   run `daily` every day via launchd (macOS)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path

from . import config, espn_league, model, site, sources

ROOT = Path(__file__).resolve().parent.parent
RAW, DATA, DIST = ROOT / "data" / "raw", ROOT / "data", ROOT / "dist"


def weights_for(fmt: str, punt: list[str]) -> dict[str, float]:
    if fmt == "points":
        return {"fp": 1.0}
    cats = [c for c in config.CATS if not (fmt == "8cat" and c == "to")]
    return {c: 1.0 for c in cats if c not in punt}


def build(refresh: bool) -> None:
    print("Fetching ESPN players/projections ...")
    espn = sources.espn_players(RAW, refresh)
    teams = sources.espn_teams(RAW, refresh)
    print("Fetching Basketball-Reference history ...")
    b_last = sources.bref_table(RAW, config.BREF_LAST, "per_game", refresh)
    b_prior = sources.bref_table(RAW, config.BREF_PRIOR, "per_game", refresh)
    b_adv = sources.bref_table(RAW, config.BREF_LAST, "advanced", refresh)
    print("Fetching Sleeper bios ...")
    sl = sources.sleeper_players(RAW, refresh)

    players, unmatched = model.merge(espn, teams, b_last, b_prior, b_adv, sl)
    model.build_lines(players)
    league = model.compute_z(players, config.TEAMS * config.ROSTER_SIZE)
    for p in players:
        p["flags"] = model.flags(p)

    ranked = model.score(players, weights_for("9cat", []))
    print(f"Merged {len(players)} players; {len(unmatched)} with 25-26 stats unmatched on bref: {unmatched[:12]}")

    DATA.mkdir(exist_ok=True)
    meta = {
        "season": config.SEASON_LABEL,
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "teams": config.TEAMS, "roster": config.ROSTER_SIZE,
        "league": league,
        "cats": config.CATS, "catLabels": config.CAT_LABELS,
        "pointsScoring": config.POINTS_SCORING,
        "model": {"projWeight": config.PROJ_WEIGHT, "gpFull": config.GP_FULL_SEASON,
                  "availDamping": config.AVAIL_DAMPING},
    }
    slim = []
    for p in ranked:
        slim.append({k: p[k] for k in (
            "id", "name", "team", "pos", "ppos", "slots", "age", "injury", "adp", "auction", "espnRank", "espnPtsRank",
            "outlook", "line", "fgPct", "ftPct", "fp", "fpProj", "fpLast", "fpPrior", "fpCur", "fpL15", "curGp", "curWeight", "expGp", "gpHist", "usg", "bpm",
            "avail", "mult", "z", "flags")})
        trim = lambda ln: {k: round(ln[k], 2) for k in ("pts", "reb", "ast", "stl", "blk", "tpm", "to", "min")} if ln else None
        slim[-1]["projLine"], slim[-1]["lastLine"] = trim(p["proj"]), trim(p["last"])
        slim[-1]["projGp"] = p["proj"]["gp"] if p["proj"] else None
        slim[-1]["lastGp"] = p["last"]["gp"] if p["last"] else None
    payload = {"meta": meta, "players": slim}
    (DATA / "players.json").write_text(json.dumps(payload))

    with open(DATA / "rankings_9cat.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "name", "team", "pos", "age", "score", "adp", "edge", "exp_gp", "fp_per_game",
                    *[config.CAT_LABELS[c] + "_z" for c in config.CATS], "flags"])
        for p in ranked:
            w.writerow([p["rank"], p["name"], p["team"], "/".join(p["pos"]), p["age"], p["score"], p["adp"],
                        p["edge"], p["expGp"], p["fp"], *[p["z"][c] for c in config.CATS], ";".join(p["flags"])])

    DIST.mkdir(exist_ok=True)
    write_dist(payload)
    print(f"Wrote {DATA/'players.json'}, {DATA/'rankings_9cat.csv'}, {DIST/'draft-mantis.html'}")


def write_dist(payload: dict | None = None) -> None:
    """Static copy for hosting: board + last season/moves snapshot (if a league is connected)."""
    DIST.mkdir(exist_ok=True)
    (DIST / "draft-mantis.html").write_text(site.render(payload, live=False, snap=site.snapshot()))


def save_moves() -> None:
    from . import moves
    rep = moves.build(ROOT)
    (DATA / "moves.json").write_text(json.dumps(rep))


def top(args) -> None:
    payload = json.loads((DATA / "players.json").read_text())
    players = payload["players"]
    ranked = model.score(players, weights_for(args.format, args.punt), teams=args.teams)
    if args.pos:
        ranked = [p for p in ranked if args.pos.upper() in p["pos"]]
    hdr = f"{'#':>3} {'Player':<26}{'Tm':<5}{'Pos':<9}{'Age':>4}{'Score':>7}{'ADP':>7}{'Edge':>7}{'xGP':>6}  Flags"
    print(hdr + "\n" + "-" * len(hdr))
    for p in ranked[: args.n]:
        edge = f"{p['edge']:+.0f}" if p["edge"] is not None else "-"
        print(f"{p['rank']:>3} {p['name'][:25]:<26}{p['team']:<5}{'/'.join(p['pos']):<9}{p['age'] or '-':>4}"
              f"{p['score']:>7.1f}{p['adp'] or '-':>7}{edge:>7}{p['expGp']:>6.0f}  {', '.join(p['flags'])}")


def login() -> None:
    import getpass
    print("Copy these from your browser while logged in to espn.com:")
    print("  Chrome: DevTools > Application > Cookies > https://www.espn.com  (Safari/Firefox: Storage tab)")
    swid = espn_league.normalize_swid(input("SWID (looks like {XXXXXXXX-XXXX-...}): "))
    s2 = getpass.getpass("espn_s2 (input hidden): ").strip()
    store = espn_league.load_store()
    store.update(swid=swid, espn_s2=s2)
    espn_league.save_store(store)
    print(f"Saved to {espn_league.STORE} (readable only by you).")


def leagues() -> None:
    found = espn_league.find_leagues(espn_league.load_store())
    if not found:
        print("No NBA fantasy leagues found on this account. Connect by ID instead: python3 -m mantis league --id <leagueId>")
    for lg in found:
        print(f"{lg['id']:>12}  {lg['season'] - 1}-{str(lg['season'])[2:]}  {lg['name']}  "
              f"({lg.get('size') or '?'} teams, {lg.get('scoring') or '?'})  your team: {lg['teamName'] or '?'}")


def league(args) -> None:
    store = espn_league.load_store()
    if args.id:
        store["league"] = {"id": args.id, "season": args.season}
    if not store.get("league"):
        raise SystemExit("No league connected. Run: python3 -m mantis league --id <leagueId>")
    sel = store["league"]
    snap = espn_league.normalize(espn_league.fetch_league(sel["id"], sel["season"], store), store.get("swid"))
    if args.id:
        espn_league.save_store(store)
    lg, dr = snap["league"], snap["draft"]
    print(f"{lg['name']}  ·  {lg['size']} teams  ·  {lg['scoringType']}  ·  {lg['rosterSize']} roster spots")
    print("Categories: " + (", ".join(config.CAT_LABELS[c] for c in lg["cats"]) if lg["cats"] else
                            ", ".join(f"{k} {v:+g}" for k, v in lg["points"].items())))
    if lg["unsupported"]:
        print("Not modeled: " + ", ".join(lg["unsupported"]))
    state = "in progress" if dr["inProgress"] else "complete" if dr["drafted"] else "not started"
    print(f"Draft: {dr['type'] or '?'}, {state}, {len(dr['picks'])} picks made")
    for t in snap["teams"]:
        mark = " <- you" if t["id"] == snap["myTeamId"] else ""
        print(f"  {t['abbrev']:<5} {t['name'][:30]:<31} {t['record']:<7} {len(t['roster'])} players{mark}")


def addon() -> None:
    import zipfile
    src = ROOT / "firefox-addon"
    version = json.loads((src / "manifest.json").read_text())["version"]
    DIST.mkdir(exist_ok=True)
    out = DIST / f"draft-mantis-bridge-{version}.xpi"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                z.write(f, f.relative_to(src).as_posix())
    print(f"Wrote {out}")
    print("Unsigned: load it in Firefox Developer Edition/Nightly, or sign it (see README > Firefox add-on).")


def season_cmd(sims: int) -> None:
    from . import season
    rep = season.run(ROOT, sims=sims)
    rep["leagueId"] = (espn_league.load_store().get("league") or {}).get("id")
    (DATA / "season.json").write_text(json.dumps(rep))
    save_moves()
    write_dist()
    name = lambda i: rep["names"].get(str(i), str(i))  # noqa: E731
    print(f"{rep['sims']} simulated seasons · week {rep['currentPeriod']} · {rep['remainingPeriods']} matchup weeks left"
          + (" · ESPN schedule not published yet, round-robin assumed" if rep["usedFallbackSchedule"] else ""))
    print(f"{'#':>2} {'Team':<28}{'Record':>8}{'Proj W-L-T':>14}{'Strength':>10}{'Playoffs':>10}{'Title':>8}")
    for i, t in enumerate(rep["teams"], 1):
        pr = t["proj"]
        print(f"{i:>2} {t['name'][:27]:<28}{t['record'] or '-':>8}{pr['w']:>6.1f}-{pr['l']:.1f}-{pr['t']:.1f}"
              f"{t['strength']:>9.1f}%{t['playoffPct']:>9.1f}%{t['titlePct']:>7.1f}%{'  <- you' if t['isMine'] else ''}")
    sug = rep.get("suggestions")
    if not sug:
        print("\nPick your team on the board (League panel) to get trade and waiver ideas.")
        return
    print("\nTrade ideas (change in your weekly win rate):")
    for t in sug["trades"][:8]:
        print(f"  {'WIN-WIN ' if t['winWin'] else ''}give {', '.join(map(name, t['give']))} -> get {', '.join(map(name, t['get']))}"
              f"   you {t['dMe'] * 100:+.1f} pts, them {t['dThem'] * 100:+.1f}")
    print("\nWaiver adds:")
    for w in sug["waivers"][:6]:
        print(f"  add {name(w['add'])}, drop {name(w['drop'])}   {w['dMe'] * 100:+.1f} pts")
    print("\nStream this week:", ", ".join(f"{name(x['id'])} ({x['games']} g)" for x in sug["streamers"][:6]))
    for n in sug["notes"]:
        print("  -", n["text"])


def daily() -> None:
    """Scheduled job: re-pull every source, rebuild scores, then refresh the season report if a league is connected."""
    import datetime as dt
    import traceback
    print(f"== Draft Mantis daily refresh {dt.datetime.now():%Y-%m-%d %H:%M}")
    build(refresh=True)
    if espn_league.load_store().get("league"):
        try:
            from . import season
            sources.espn_pro_schedule(RAW, refresh=True)
            rep = season.run(ROOT)
            rep["leagueId"] = espn_league.load_store()["league"]["id"]
            (DATA / "season.json").write_text(json.dumps(rep))
            print(f"Season report updated: {len(rep['teams'])} teams, {rep['sims']} sims")
            save_moves()
            write_dist()
        except Exception:  # the data refresh above still counts
            traceback.print_exc()


LAUNCH_LABEL = "com.draftmantis.daily"


def schedule_cmd(action: str, at: str) -> None:
    import os
    import plistlib
    import subprocess
    import sys
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if action == "status":
        if not plist.exists():
            print("Not scheduled. Run: python3 -m mantis schedule install")
            return
        cfg = plistlib.loads(plist.read_bytes())
        when = cfg["StartCalendarInterval"]
        out = subprocess.run(["launchctl", "print", f"{domain}/{LAUNCH_LABEL}"], capture_output=True, text=True).stdout
        last = next((ln.split("=", 1)[1].strip() for ln in out.splitlines() if "last exit code" in ln), "never run")
        print(f"Daily refresh at {when['Hour']:02d}:{when['Minute']:02d} · loaded: {'yes' if out else 'no'} · last exit: {last}")
        print(f"Log: {cfg['StandardOutPath']}")
        return
    subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
    if action == "remove":
        plist.unlink(missing_ok=True)
        print("Daily refresh removed.")
        return
    hour, minute = (int(x) for x in at.split(":"))
    log = DATA / "logs" / "daily.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [sys.executable, "-m", "mantis", "daily"],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},  # a missed run (Mac asleep) fires on wake
        "StandardOutPath": str(log), "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }))
    res = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
    if res.returncode:
        raise SystemExit(f"launchctl could not load the job: {res.stderr.strip()}")
    print(f"Daily refresh scheduled at {hour:02d}:{minute:02d} ({plist}). Log: {log}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="mantis", description="Draft Mantis - NBA fantasy draft scores")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--refresh", action="store_true", help="ignore cache and re-download all sources")
    t = sub.add_parser("top")
    t.add_argument("-n", type=int, default=50)
    t.add_argument("--format", choices=["9cat", "8cat", "points"], default="9cat")
    t.add_argument("--punt", nargs="*", default=[], choices=config.CATS)
    t.add_argument("--teams", type=int, default=config.TEAMS)
    t.add_argument("--pos")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    sub.add_parser("login")
    sub.add_parser("leagues")
    sub.add_parser("addon")
    se = sub.add_parser("season")
    se.add_argument("--sims", type=int, default=1500)
    sub.add_parser("daily")
    sc = sub.add_parser("schedule")
    sc.add_argument("action", choices=["install", "remove", "status"])
    sc.add_argument("--at", default="07:30", help="local time HH:MM (after ESPN posts overnight stats)")
    lg = sub.add_parser("league")
    lg.add_argument("--id", type=int)
    lg.add_argument("--season", type=int, default=config.ESPN_SEASON, help="season end year (2027 = 2026-27)")
    args = ap.parse_args()
    try:
        if args.cmd == "build":
            build(args.refresh)
        elif args.cmd == "top":
            top(args)
        elif args.cmd == "serve":
            from .server import serve
            serve(args.port, not args.no_open)
        elif args.cmd == "login":
            login()
        elif args.cmd == "leagues":
            leagues()
        elif args.cmd == "addon":
            addon()
        elif args.cmd == "season":
            season_cmd(args.sims)
        elif args.cmd == "daily":
            daily()
        elif args.cmd == "schedule":
            schedule_cmd(args.action, args.at)
        else:
            league(args)
    except espn_league.EspnError as e:
        raise SystemExit(f"ESPN: {e}")


if __name__ == "__main__":
    main()
