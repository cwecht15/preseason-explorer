"""Aggregate starter_trends.csv into team-season summaries with coaches.

Writes starter_summary.csv: one row per team-season with QB1 usage, offensive
and defensive starter snap-share (avg across preseason weeks), starters-rested
counts, and the head coach that season.
"""

import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

# coaching table -> teams.csv abbreviations
COACH_ABBR_FIX = {"BLT": "BAL", "CLV": "CLE", "HST": "HOU", "ARZ": "AZ", "LA": "LAR"}


def main():
    df = pd.read_csv(os.path.join(HERE, "starter_trends.csv"))
    teams = pd.read_csv(os.path.join(HERE, "teams.csv"))
    tmap = {int(r.teamId): r.abbr for r in teams.itertuples()}
    df["team"] = df["teamId"].map(lambda x: tmap.get(int(x), str(x)))

    hc = pd.read_csv(os.path.join(HERE, "hc_by_season.csv"))
    hc["team"] = hc["team"].replace(COACH_ABBR_FIX)

    off = df[df.unit == "offense"]
    de = df[df.unit == "defense"]

    o = (off.groupby(["season", "team"], as_index=False)
         .agg(games=("gameId", "nunique"),
              qb1=("qb1", "first"),
              qb1_snaps=("qb1_snaps", "sum"),
              off_share=("starter_snap_share", "mean"),
              off_starters_played=("starters_played", "mean")))
    d = (de.groupby(["season", "team"], as_index=False)
         .agg(def_share=("starter_snap_share", "mean"),
              def_starters_played=("starters_played", "mean")))
    s = o.merge(d, on=["season", "team"], how="outer")
    s["starter_share"] = ((s["off_share"] + s["def_share"]) / 2).round(1)
    for c in ("off_share", "def_share", "off_starters_played", "def_starters_played"):
        s[c] = s[c].round(1)

    s = s.merge(hc.rename(columns={"coach": "head_coach"}), on=["season", "team"], how="left")
    s = s.sort_values(["season", "starter_share"], ascending=[True, False])
    s.to_csv(os.path.join(HERE, "starter_summary.csv"), index=False)
    print(s.groupby("season").size().to_string())
    print("wrote starter_summary.csv")

    # per-week ramp: how usage builds across preseason weeks 0-3
    wk = (df.groupby(["season", "team", "unit", "week"], as_index=False)
          .agg(share=("starter_snap_share", "mean"),
               qb1_snaps=("qb1_snaps", "sum")))
    wk["team"] = wk["team"]
    wk.to_csv(os.path.join(HERE, "starter_weekly.csv"), index=False)
    print("wrote starter_weekly.csv")


if __name__ == "__main__":
    main()
