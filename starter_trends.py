"""Preseason starter-usage analysis.

For each team-season: identify the REAL week-1 regular-season starters (the
11 on the team's first offensive PASS/RUSH snap + 11 on first defensive
PASS/RUSH snap of REG week 1), then measure how much preseason work those
players (and specifically QB1) got, per preseason week.

Sources:
  2024: ../NFL_API_PBP/2024pbp  (full season incl. PRE + REG)
  2025: game_2025*.json here (PRE) + reg2025_wk1/ (REG wk1 openings)

Outputs starter_trends.csv (one row per team-season-preseason-week).
"""

import glob
import json
import os

import pandas as pd

from preprocess_2026 import corrected_sides, is_pass_rush

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_2024 = os.path.join(HERE, "..", "NFL_API_PBP", "2024pbp")


def iter_games(paths):
    for p in paths:
        try:
            yield json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print(f"skip {p}: {e}")


def game_rows(g, season, seasontype_field=True):
    """Yield per-play (side-corrected) lineup rows for PASS/RUSH plays."""
    for pl in g.get("plays", []):
        if not is_pass_rush(pl.get("nflPlayType")):
            continue
        off, de, _ = corrected_sides(pl)
        if not off or not de:
            continue
        yield {
            "week": g.get("week"),
            "seasonType": g.get("seasonType"),
            "gameId": g.get("gameId"),
            "nflPlayId": pl.get("nflPlayId"),
            "off": off, "def": de,
        }


def starters_from_reg_wk1(games):
    """team -> dict(offense=[names], defense=[names], qb1=name), from each
    team's first offensive / first defensive PASS-RUSH snap of REG week 1."""
    off_starters, def_starters = {}, {}
    for g in games:
        rows = sorted(game_rows(g, None), key=lambda r: r["nflPlayId"])
        for r in rows:
            off_team = r["off"][0].get("teamId")
            def_team = r["def"][0].get("teamId")
            if off_team not in off_starters:
                qbs = [p["playerName"] for p in r["off"] if p.get("position") == "QB"]
                off_starters[off_team] = {
                    "offense": [p["playerName"] for p in r["off"]],
                    "qb1": qbs[0] if qbs else None,
                }
            if def_team not in def_starters:
                def_starters[def_team] = [p["playerName"] for p in r["def"]]
    starters = {}
    for team in set(off_starters) | set(def_starters):
        d = dict(off_starters.get(team, {}))
        if team in def_starters:
            d["defense"] = def_starters[team]
        starters[team] = d
    return starters


def preseason_usage(games, starters):
    """Returns (team-week metrics df, per-starter detail df)."""
    recs, detail = [], []
    per_game = {}
    for g in games:
        for r in game_rows(g, None):
            key = (g.get("week"), g.get("gameId"))
            for side_key in ("off", "def"):
                team = r[side_key][0].get("teamId")
                d = per_game.setdefault((key, team, side_key), {"snaps": 0, "players": {}})
                d["snaps"] += 1
                for p in r[side_key]:
                    d["players"][p["playerName"]] = d["players"].get(p["playerName"], 0) + 1

    for ((week, game_id), team, side_key), d in per_game.items():
        st = starters.get(team)
        if not st:
            continue
        unit = "offense" if side_key == "off" else "defense"
        names = st.get(unit) or []
        if not names:
            continue
        played = [n for n in names if d["players"].get(n, 0) > 0]
        share = sum(d["players"].get(n, 0) for n in names) / (len(names) * d["snaps"]) if d["snaps"] else 0
        rec = {
            "week": week, "gameId": game_id, "teamId": team, "unit": unit,
            "unit_snaps": d["snaps"],
            "starters_played": len(played),
            "starter_snap_share": round(100 * share, 1),
        }
        if unit == "offense" and st.get("qb1"):
            rec["qb1"] = st["qb1"]
            rec["qb1_snaps"] = d["players"].get(st["qb1"], 0)
        recs.append(rec)
        for n in names:
            detail.append({
                "week": week, "gameId": game_id, "teamId": team, "unit": unit,
                "playerName": n, "snaps": d["players"].get(n, 0),
                "unit_snaps": d["snaps"],
                "is_qb1": unit == "offense" and n == st.get("qb1"),
            })
    return pd.DataFrame(recs), pd.DataFrame(detail)


def analyze(season, pre_paths, reg_paths):
    pre_games = list(iter_games(pre_paths))
    reg_games = list(iter_games(reg_paths))
    starters = starters_from_reg_wk1(reg_games)
    print(f"{season}: {len(pre_games)} preseason games, starters for {len(starters)} teams")
    df, detail = preseason_usage(pre_games, starters)
    df["season"] = season
    detail["season"] = season
    return df, detail


def season_sources():
    """(season, preseason game paths, REG-wk1 game paths) for every season
    with data on disk. 2026+ joins automatically once reg<season>_wk1/ exists."""
    p24 = sorted(glob.glob(os.path.join(SRC_2024, "game_*.json")))
    pre24, reg24 = [], []
    for p in p24:
        g = json.load(open(p, encoding="utf-8"))
        if g.get("seasonType") == "PRE":
            pre24.append(p)
        elif g.get("seasonType") == "REG" and g.get("week") == 1:
            reg24.append(p)
    yield 2024, pre24, reg24
    yield 2025, sorted(glob.glob(os.path.join(HERE, "game_2025*.json"))), \
        sorted(glob.glob(os.path.join(HERE, "reg2025_wk1", "game_*.json")))
    for season in (2026, 2027, 2028):
        pre = sorted(glob.glob(os.path.join(HERE, f"games_{season}", "game_*.json")))
        reg = sorted(glob.glob(os.path.join(HERE, f"reg{season}_wk1", "game_*.json")))
        if pre and reg:
            yield season, pre, reg


def main():
    frames, details = [], []
    for season, pre, reg in season_sources():
        if not reg:
            print(f"{season}: no REG wk1 lineups yet — skipped "
                  f"(run: python fetch_reg_wk1.py --season {season})")
            continue
        df, detail = analyze(season, pre, reg)
        frames.append(df)
        details.append(detail)

    out = pd.concat(frames, ignore_index=True)
    out.to_csv(os.path.join(HERE, "starter_trends.csv"), index=False)
    print(f"wrote starter_trends.csv ({len(out)} rows)")
    det = pd.concat(details, ignore_index=True)
    det.to_csv(os.path.join(HERE, "starter_players.csv"), index=False)
    print(f"wrote starter_players.csv ({len(det)} rows)")


if __name__ == "__main__":
    main()
