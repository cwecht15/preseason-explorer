"""Fetch the opening portion of each REG week-1 game from pro.nfl.com (--season N).

Purpose: identify each team's actual week-1 starters (first offensive /
defensive PASS-RUSH lineups) to measure preseason starter usage. Stops per
game once both teams have >=6 offensive PASS/RUSH snaps observed (or 90
plays), so it fetches only a fraction of each game.

Writes reg2025_wk1/game_<uuid>.json using the same play shape as fetch_2026.
Requires auth.txt (see fetch_2026.py docstring).
"""

import json
import os
import sys
import time

import requests

from fetch_2026 import (BASE, HEADERS, load_auth_headers, get_json,
                        normalize_play_type, player_dicts)

import argparse
_ap = argparse.ArgumentParser()
_ap.add_argument("--season", type=int, default=2025)
SEASON = _ap.parse_args().season
OUT_DIR = f"reg{SEASON}_wk1"
DELAY = 0.12
MIN_OFF_SNAPS = 6
MAX_PLAYS = 90


def main():
    auth = load_auth_headers()
    if not auth:
        sys.exit("auth.txt missing/unparseable")
    os.makedirs(OUT_DIR, exist_ok=True)
    s = requests.Session()

    data = get_json(s, f"{BASE}/api/scores/live/games",
                    params={"season": SEASON, "seasonType": "REG", "week": 1})
    games = (data or {}).get("games", [])
    print(f"{len(games)} REG wk1 games")

    for game in games:
        uuid = game["gameId"]
        out_path = os.path.join(OUT_DIR, f"game_{uuid}.json")
        if os.path.exists(out_path):
            print(f"{uuid} exists, skipping")
            continue
        plays_raw = get_json(s, f"{BASE}/api/secured/plays/playlist/game",
                             params={"gameId": uuid}, extra_headers=auth)
        if not plays_raw or not plays_raw.get("plays"):
            print(f"{uuid}: no playlist!")
            continue
        plays_out = []
        off_counts = {}
        for pl in plays_raw["plays"]:
            play_id = pl.get("playId")
            if play_id is None:
                continue
            summary = get_json(s, f"{BASE}/api/plays/summaryPlay",
                               params={"gameId": uuid, "playId": play_id}) or {}
            time.sleep(DELAY)
            sp = summary.get("play") or {}
            offense, defense = player_dicts(summary)
            desc = pl.get("playDescription") or sp.get("playDescription") or ""
            ptype = normalize_play_type(pl.get("playType") or sp.get("playType") or "", desc)
            plays_out.append({
                "nflPlayId": play_id,
                "nflPlayType": ptype,
                "nflPlayDescription": desc,
                "nflPlayFullDescription": sp.get("playDescriptionWithJerseyNumbers") or desc,
                "offensePlayers": offense,
                "defensePlayers": defense,
            })
            if (ptype == "RUSH" or ptype.startswith("PASS")) and offense:
                tid = offense[0].get("teamId")
                off_counts[tid] = off_counts.get(tid, 0) + 1
            if len(plays_out) >= MAX_PLAYS or (
                    len(off_counts) >= 2 and min(off_counts.values()) >= MIN_OFF_SNAPS):
                break
        meta = {
            "gameId": uuid,
            "season": SEASON, "seasonType": "REG", "week": 1,
            "homeTeamId": (game.get("homeTeam") or {}).get("teamId"),
            "awayTeamId": (game.get("awayTeam") or {}).get("teamId"),
            "startTime": game.get("startTime", ""),
            "plays": plays_out,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        print(f"{uuid}: {len(plays_out)} plays, off snaps {off_counts}")

    print("done")


if __name__ == "__main__":
    main()
