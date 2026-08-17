"""Build the Streamlit app's CSVs from game_*.json files.

Reads games_2026/game_2026NNN.json (same shape as the 2025 files) and writes
the five CSVs the app / 2025 pipeline used into data_2026/:

  plays_unique.csv     gameId, week, nflPlayId, nflPlayType, nflPlayDescription,
                       nflPlayUrl + the situation columns (see SITUATION_COLS)
  play_players.csv     ... + side (off/def), playerName, teamId, position
  players_index.csv    playerName, teamId, position, pass_rush_snaps
  coplayer_counts.csv  playerName, teammate, teamId, count
  plays_wide.csv       play row + offensePlayer1..N, defensePlayer1..N

Situation columns (down, distance, field position, personnel) stay on the
play-level tables only - the app joins them onto play_players by
gameId+nflPlayId, which keeps the biggest CSV from growing. They're empty for
games fetched before situation_fields existed; run backfill_situations.py.

Point the app's sidebar "Folder containing the CSVs" at data_2026 to use it.

Usage:
    python preprocess_2026.py                      # games_2026/ -> data_2026/
    python preprocess_2026.py --in-dir . --out-dir data_check   # e.g. 2025 files
"""

import argparse
import glob
import json
import os
import re

import pandas as pd


def is_pass_rush(play_type):
    t = str(play_type or "")
    return t == "RUSH" or t.startswith("PASS")


# e.g. "5-T.Lance", "T.Lance", "89-W.Dissly"
ACTOR_RE = re.compile(r"(?:(\d{1,2})-)?([A-Z])[a-zA-Z'.]*\.([A-Za-z'-]+)")


def true_offense_team(play):
    """TeamId of the actual offense on a PASS/RUSH play, from the ball-handler
    named first in the description. Returns None when it can't be resolved.

    Needed because the 2025 game JSONs labeled offensePlayers/defensePlayers
    by home/away instead of possession, so sides are wrong whenever the home
    team had the ball. 2026 files are correct; this is a no-op for them.
    """
    text = play.get("nflPlayFullDescription") or play.get("nflPlayDescription") or ""
    m = ACTOR_RE.search(text)
    if not m:
        return None
    number, initial, last = m.group(1), m.group(2), m.group(3).lower()

    players = []
    for side_key in ("offensePlayers", "defensePlayers"):
        for p in play.get(side_key) or []:
            if str(p.get("lastName", "")).lower() == last:
                players.append(p)

    def uni_digits(p):
        d = re.sub(r"\D", "", str(p.get("uniformNumber", "")))
        return int(d) if d else None

    def initial_ok(p):
        # firstName can be a legal name that differs from the known name
        # (e.g. firstName "Muhammad" for "Isaiah Oliver"), so accept either
        return (str(p.get("firstName", "")).upper().startswith(initial)
                or str(p.get("playerName", "")).upper().startswith(initial))

    # strongest evidence first: jersey number, then initial, then lastname alone
    tiers = []
    if number:
        tiers.append([p for p in players if uni_digits(p) == int(number)])
    tiers.append([p for p in players if initial_ok(p)])
    tiers.append(players)
    for tier in tiers:
        teams = {p.get("teamId") for p in tier}
        if len(teams) == 1:
            return teams.pop()
    return None


def corrected_sides(play):
    """Return (offense_list, defense_list, status), swapping the JSON's arrays
    when the described ball-handler sits on the 'defense' side.
    status: 'ok' | 'swapped' | 'unresolved' (unresolved keeps original labels).

    possessionTeamId settles it outright when it's there (backfilled files and
    anything fetched since); the description parsing is the fallback for files
    that predate it.
    """
    off = play.get("offensePlayers") or []
    de = play.get("defensePlayers") or []
    if not is_pass_rush(play.get("nflPlayType")) or not (off or de):
        return off, de, "ok"

    poss = play.get("possessionTeamId")
    if poss is not None:
        poss = str(poss)
        if any(str(p.get("teamId")) == poss for p in de):
            return de, off, "swapped"
        if any(str(p.get("teamId")) == poss for p in off):
            return off, de, "ok"
        # possession team on neither side (rare data glitch): fall through

    team = true_offense_team(play)
    if team is None:
        return off, de, "unresolved"
    if any(p.get("teamId") == team for p in de):
        return de, off, "swapped"
    return off, de, "ok"


# ---------- Situation ----------
# Raw-ish columns only: what the API said, plus field position normalized to
# "yards from the end zone the offense is attacking" and personnel counted off
# the lineups. Buckets (short/long, red zone, nickel, pass-down) are the app's
# job, so the thresholds stay adjustable there instead of baked into the CSVs.
SITUATION_COLS = [
    "quarter", "down", "yardsToGo", "gameClock", "yardsToGoal", "isGoalToGo",
    "isRedzone", "possessionTeamId", "offScore", "defScore",
    "expectedPointsAdded", "offN", "offRB", "offTE", "offWR", "offOL",
    "defN", "defDL", "defLB", "defDB",
    "rusher", "target", "receiver", "passer", "touchYards",
]

# NFL GSIS stat ids, confirmed against the play descriptions in this data
# rather than taken on faith: on 90 plays, id 10 landed on exactly the player
# the description had rushing, 115 on the intended receiver whether or not he
# caught it, and 21/22 only on completions.
STAT_RUSH = {10, 11}        # 11 is the same with a touchdown
STAT_RECEPTION = {21, 22}
STAT_TARGET = {115}         # thrown at him, caught or not
# every way a dropback ends, so `passer` covers attempts rather than only the
# ones that worked: 14 sat on the quarterback for 128 incompletions and 20 for
# 26 sacks in this data, which is how they earned their place here
STAT_PASS = {14, 15, 16, 19, 20}


def touches(play, players_by_gsis):
    """Who touched the ball: rusher / target / receiver / passer, by name.

    Snap counts say a player was out there; this says the ball came to him,
    which for a skill player is the difference between being on the field and
    having a role. Comes from the API's own stat lines, so it needs no name
    matching — gsisId is the same id the lineup rows carry.
    """
    out = {"rusher": "", "target": "", "receiver": "", "passer": "", "touchYards": ""}
    for stat in play.get("playStats") or []:
        name = players_by_gsis.get(stat.get("gsisId"))
        if not name:
            continue
        sid = stat.get("statId")
        if sid in STAT_RUSH:
            out["rusher"], out["touchYards"] = name, stat.get("yards")
        elif sid in STAT_RECEPTION:
            out["receiver"], out["touchYards"] = name, stat.get("yards")
        elif sid in STAT_TARGET:
            out["target"] = name
        elif sid in STAT_PASS:
            out["passer"] = name
    return out


def team_abbrs(path="teams.csv"):
    """teamId -> abbr ('3800' -> 'AZ'), matching the yardlineSide vocabulary."""
    if not os.path.exists(path):
        print(f"note: no {path}, field position will be blank")
        return {}
    t = pd.read_csv(path, dtype=str)
    return {str(r.teamId): str(r.abbr) for r in t.itertuples()
            if pd.notna(r.teamId) and pd.notna(r.abbr)}


def yards_to_goal(play, abbrs):
    """Yards from the line of scrimmage to the end zone the offense wants.

    The API gives field position as a side + number ('AZ 37'), which is only
    meaningful once you know who has the ball; this flattens it to 1-99 so
    "inside the 20" is one comparison instead of two.
    """
    number = play.get("yardlineNumber")
    if number is None:
        return None
    number = int(number)
    if number == 50:
        return 50
    side = play.get("yardlineSide")
    own = abbrs.get(str(play.get("possessionTeamId")))
    if not side or not own:
        return None
    return 100 - number if side == own else number


def personnel(players):
    """Counts by positionGroup for one side's lineup.

    These are the positions the NFL lists players at, not where they lined up,
    so a fullback counts as an RB and a tackle-eligible package reads as six
    linemen. Good enough to tell 11 from 12 personnel and base from nickel.
    """
    counts = {}
    for p in players:
        group = str(p.get("positionGroup") or "?")
        counts[group] = counts.get(group, 0) + 1
    return counts


def situation_row(play, off_list, def_list, abbrs):
    off = personnel(off_list)
    de = personnel(def_list)
    # offN/defN so the app can tell a real personnel grouping from a lineup
    # that came back short - "1 RB 0 TE" out of 10 players would otherwise read
    # as a formation choice rather than a data gap
    return {
        "quarter": play.get("quarter"),
        "down": play.get("down"),
        "yardsToGo": play.get("yardsToGo"),
        "gameClock": play.get("gameClock"),
        "yardsToGoal": yards_to_goal(play, abbrs),
        "isGoalToGo": play.get("isGoalToGo"),
        "isRedzone": play.get("isRedzonePlay"),
        "possessionTeamId": play.get("possessionTeamId"),
        "offScore": play.get("offScore"),
        "defScore": play.get("defScore"),
        "expectedPointsAdded": play.get("expectedPointsAdded"),
        "offN": len(off_list),
        "offRB": off.get("RB", 0), "offTE": off.get("TE", 0),
        "offWR": off.get("WR", 0), "offOL": off.get("OL", 0),
        "defN": len(def_list),
        "defDL": de.get("DL", 0), "defLB": de.get("LB", 0),
        "defDB": de.get("DB", 0),
    }


def load_games(in_dir):
    paths = sorted(glob.glob(os.path.join(in_dir, "game_*.json")))
    if not paths:
        raise SystemExit(f"No game_*.json files in {in_dir}")
    print(f"{len(paths)} game file(s) to process")
    for i, p in enumerate(paths, start=1):
        print(f"  reading {os.path.basename(p)} ({i}/{len(paths)})", flush=True)
        with open(p, encoding="utf-8") as f:
            yield json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-dir", default="games_2026")
    ap.add_argument("--out-dir", default="data_2026")
    args = ap.parse_args()

    plays_rows, pp_rows, wide_rows = [], [], []
    swapped = unresolved = no_situation = 0
    abbrs = team_abbrs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "teams.csv"))

    for g in load_games(args.in_dir):
        game_id, week = g["gameId"], g.get("week")
        for pl in g.get("plays", []):
            off_list, def_list, status = corrected_sides(pl)
            if status == "swapped":
                swapped += 1
            elif status == "unresolved":
                unresolved += 1
            if pl.get("down") is None:
                no_situation += 1
            base = {
                "gameId": game_id,
                "week": week,
                "nflPlayId": pl["nflPlayId"],
                "nflPlayType": pl.get("nflPlayType", ""),
                "nflPlayDescription": pl.get("nflPlayDescription", ""),
            }
            by_gsis = {p.get("gsisId"): p.get("playerName", "")
                       for p in list(off_list) + list(def_list) if p.get("gsisId")}
            sit = {**situation_row(pl, off_list, def_list, abbrs),
                   **touches(pl, by_gsis)}
            plays_rows.append({**base, "nflPlayUrl": pl.get("nflPlayUrl", ""), **sit})

            wide = {**base, "nflPlayUrl": pl.get("nflPlayUrl", ""), **sit}
            for players, side, prefix in (
                (off_list, "off", "offensePlayer"),
                (def_list, "def", "defensePlayer"),
            ):
                for i, player in enumerate(players, start=1):
                    pp_rows.append({
                        **base,
                        "side": side,
                        "playerName": player.get("playerName", ""),
                        "teamId": player.get("teamId", ""),
                        "position": player.get("position", ""),
                    })
                    wide[f"{prefix}{i}"] = player.get("playerName", "")
            wide_rows.append(wide)

    plays = pd.DataFrame(plays_rows)
    pp = pd.DataFrame(pp_rows)
    wide = pd.DataFrame(wide_rows)

    # players_index: PASS/RUSH snap counts per player+team+position
    # (legacy 2025 pipeline kept separate rows when a player's listed position varied)
    pr = pp[pp["nflPlayType"].map(is_pass_rush)]
    idx = (pr.drop_duplicates(["gameId", "nflPlayId", "playerName", "teamId", "position"])
             .groupby(["playerName", "teamId", "position"], as_index=False).size()
             .rename(columns={"size": "pass_rush_snaps"}))
    idx = idx[["playerName", "teamId", "position", "pass_rush_snaps"]]
    idx = idx.sort_values("playerName")

    # coplayer_counts: same-team pairs on PASS/RUSH plays
    pr_key = pr[["gameId", "nflPlayId", "playerName", "teamId"]].drop_duplicates()
    pairs = pr_key.merge(pr_key, on=["gameId", "nflPlayId"], suffixes=("", "_mate"))
    pairs = pairs[(pairs["teamId"] == pairs["teamId_mate"]) &
                  (pairs["playerName"] != pairs["playerName_mate"])]
    cop = (pairs.groupby(["playerName", "playerName_mate", "teamId"], as_index=False)
                .size()
                .rename(columns={"playerName_mate": "teammate", "size": "count"}))
    cop = cop[["playerName", "teammate", "teamId", "count"]].sort_values(
        ["playerName", "count"], ascending=[True, False])

    os.makedirs(args.out_dir, exist_ok=True)
    out = lambda name: os.path.join(args.out_dir, name)
    plays.to_csv(out("plays_unique.csv"), index=False, encoding="utf-8")
    pp.to_csv(out("play_players.csv"), index=False, encoding="utf-8")
    idx.to_csv(out("players_index.csv"), index=False, encoding="utf-8")
    cop.to_csv(out("coplayer_counts.csv"), index=False, encoding="utf-8")
    wide.to_csv(out("plays_wide.csv"), index=False, encoding="utf-8")

    if swapped or unresolved:
        print(f"side repair: swapped {swapped} plays, {unresolved} unresolved (kept original labels)")
    if no_situation:
        print(f"{no_situation} play(s) with no down/distance - "
              f"run backfill_situations.py to fill them in")
    print(f"{args.out_dir}/")
    print(f"  plays_unique.csv     {len(plays):>7} rows")
    print(f"  play_players.csv     {len(pp):>7} rows")
    print(f"  players_index.csv    {len(idx):>7} rows")
    print(f"  coplayer_counts.csv  {len(cop):>7} rows")
    print(f"  plays_wide.csv       {len(wide):>7} rows")


if __name__ == "__main__":
    main()
