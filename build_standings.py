#!/usr/bin/env python3
"""
Nightly standings builder for the World Cup 2026 pool.

Reads roster.json (who owns which teams — produced once by the browser tool),
fetches match results from football-data.org, computes each player's points
using the same scoring rules as the web tool, and writes standings.json.

Run by .github/workflows/standings.yml on a nightly cron.
API key comes from the FOOTBALL_DATA_TOKEN environment variable (a repo Secret).
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

API_URL = "https://api.football-data.org/v4/competitions/WC/matches"

# --- Scoring (must mirror the web tool's defaults) -------------------------
# These are overridden by whatever scoring block is saved in roster.json,
# so if you tweak point values in the tool before publishing the roster,
# the nightly job uses YOUR values automatically.
DEFAULT_SCORING = {
    "draw": 0.5, "groupWin": 1, "r32": 2, "r16": 3,
    "qf": 4, "sf": 6, "final": 8, "champ": 10,
}
STAGE_ORDER = ["r32", "r16", "qf", "sf", "final", "champ"]

# football-data.org stage strings -> our keys
STAGE_MAP = {
    "LAST_32": "r32",
    "LAST_16": "r16",
    "QUARTER_FINALS": "qf",
    "SEMI_FINALS": "sf",
    "FINAL": "final",
}

# Name reconciliation: football-data.org spellings -> the names used in the tool.
# Add any mismatches you spot here. Left = API name, right = tool name.
NAME_ALIASES = {
    "Turkey": "Türkiye",
    "Côte d'Ivoire": "Ivory Coast",
    "Cote d'Ivoire": "Ivory Coast",
    "Korea Republic": "South Korea",
    "Republic of Korea": "South Korea",
    "Czech Republic": "Czechia",
    "Cabo Verde": "Cape Verde",
    "DR Congo": "DR Congo",
    "Congo DR": "DR Congo",
    "United States": "USA",
}


def canon(name):
    if not name:
        return name
    return NAME_ALIASES.get(name, name)


def fetch_matches(token):
    req = urllib.request.Request(API_URL, headers={"X-Auth-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            return data.get("matches", [])
    except urllib.error.HTTPError as e:
        print(f"API HTTP error {e.code}: {e.reason}", file=sys.stderr)
        raise
    except Exception as e:
        print(f"API fetch failed: {e}", file=sys.stderr)
        raise


def build_results(matches):
    """Return {teamName: {groupWins, draws, stageReached}}."""
    results = {}

    def ensure(t):
        if t not in results:
            results[t] = {"groupWins": 0, "draws": 0, "stageReached": None}
        return results[t]

    def rank_ge(a, b):
        if not b:
            return True
        return STAGE_ORDER.index(a) >= STAGE_ORDER.index(b)

    for m in matches:
        home = canon((m.get("homeTeam") or {}).get("name"))
        away = canon((m.get("awayTeam") or {}).get("name"))
        if not home or not away:
            continue
        stage = m.get("stage")
        status = m.get("status")
        winner = (m.get("score") or {}).get("winner")  # HOME_TEAM / AWAY_TEAM / DRAW

        if stage == "GROUP_STAGE":
            if status == "FINISHED":
                if winner == "DRAW":
                    ensure(home)["draws"] += 1
                    ensure(away)["draws"] += 1
                elif winner == "HOME_TEAM":
                    ensure(home)["groupWins"] += 1
                elif winner == "AWAY_TEAM":
                    ensure(away)["groupWins"] += 1
        elif stage in STAGE_MAP:
            st = STAGE_MAP[stage]
            for t in (home, away):
                o = ensure(t)
                if rank_ge(st, o["stageReached"]):
                    o["stageReached"] = st
            if stage == "FINAL" and status == "FINISHED" and winner in ("HOME_TEAM", "AWAY_TEAM"):
                champ = home if winner == "HOME_TEAM" else away
                ensure(champ)["stageReached"] = "champ"

    return results


def team_points(team, results, scoring):
    r = results.get(team)
    if not r:
        return 0.0
    pts = r["groupWins"] * scoring["groupWin"] + r["draws"] * scoring["draw"]
    reached = r["stageReached"]
    if reached:
        idx = STAGE_ORDER.index(reached)
        for i in range(idx + 1):
            pts += scoring[STAGE_ORDER[i]]
    return pts


def main():
    token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if not token:
        print("FOOTBALL_DATA_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    try:
        with open("roster.json", encoding="utf-8") as f:
            roster = json.load(f)
    except FileNotFoundError:
        print("roster.json not found — publish a draft from the tool first.", file=sys.stderr)
        sys.exit(1)

    scoring = {**DEFAULT_SCORING, **roster.get("scoring", {})}
    players = roster["draft"]["players"]

    matches = fetch_matches(token)
    
    # Debug: print all unique team names and statuses the API returned
    api_teams = set()
    api_statuses = set()
    for m in matches:
        h = (m.get("homeTeam") or {}).get("name")
        a = (m.get("awayTeam") or {}).get("name")
        if h: api_teams.add(h)
        if a: api_teams.add(a)
        api_statuses.add(m.get("status"))
    print(f"API returned {len(matches)} matches, statuses: {api_statuses}")
    print(f"API team names: {sorted(api_teams)}")
    
    results = build_results(matches)

    standings = []
    for pl in players:
        teams_detail = []
        total = 0.0
        for t in pl["teams"]:
            name = t["team"]
            pts = team_points(name, results, scoring)
            total += pts
            r = results.get(name, {})
            teams_detail.append({
                "name": name,
                "group": t["group"],
                "pts": round(pts, 1),
                "stage": r.get("stageReached"),
                "groupWins": r.get("groupWins", 0),
                "draws": r.get("draws", 0),
            })
        teams_detail.sort(key=lambda x: x["pts"], reverse=True)
        standings.append({"name": pl["name"], "total": round(total, 1), "teams": teams_detail})

    standings.sort(key=lambda x: x["total"], reverse=True)

    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scoring": scoring,
        "standings": standings,
    }
    with open("standings.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"Wrote standings.json — {len(standings)} players, "
          f"{sum(1 for r in results.values())} teams with results.")


if __name__ == "__main__":
    main()
