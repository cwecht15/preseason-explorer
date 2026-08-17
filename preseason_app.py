import glob
import json
import os
import re
import subprocess
import sys
import time

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

# ---------- Config ----------
st.set_page_config(page_title="Preseason Player Co-Players Explorer", layout="wide")
APP_DIR = os.path.dirname(os.path.abspath(__file__)) or "."
TEAMS_CSV = os.path.join(APP_DIR, "teams.csv")

# width="stretch" only exists in newer Streamlit; Anaconda ships 1.46
_ver = tuple(int(x) for x in st.__version__.split(".")[:2])
WIDE = {"width": "stretch"} if _ver >= (1, 49) else {"use_container_width": True}

# ---------- Cache-busting helper ----------
def file_fingerprint(path):
    try:
        stat = os.stat(path)
        return f"{stat.st_mtime_ns}-{stat.st_size}"
    except FileNotFoundError:
        return "missing"

# ---------- Data discovery ----------
def discover_data_folders():
    """Find subfolders containing the app CSVs, labeled by season."""
    found = {}
    for d in sorted(os.listdir(APP_DIR)):
        full = os.path.join(APP_DIR, d)
        path = os.path.join(full, "plays_unique.csv")
        if os.path.isdir(full) and os.path.exists(path):
            try:
                head = pd.read_csv(path, nrows=1)
                season = str(int(head["gameId"].iloc[0]))[:4]
            except Exception:
                season = d
            found[f"{season}  ({d})"] = full
    return dict(sorted(found.items(), reverse=True))

# ---------- Caching loaders ----------
@st.cache_data(show_spinner=False)
def load_csv(path, fingerprint=None):
    # 'fingerprint' busts the cache when the file changes
    return pd.read_csv(path)

@st.cache_data(show_spinner=True)
def load_all(data_dir):
    plays_path = os.path.join(data_dir, "plays_unique.csv")
    pp_path = os.path.join(data_dir, "play_players.csv")
    plays = load_csv(plays_path, fingerprint=file_fingerprint(plays_path))
    pp = load_csv(pp_path, fingerprint=file_fingerprint(pp_path))
    for df in (plays, pp):
        df.columns = [c.strip() for c in df.columns]
    return plays, pp

@st.cache_data(show_spinner=False)
def load_team_map(fingerprint=None):
    """teamId (int) -> abbr, from teams.csv (fetched from pro.nfl.com/api/teams/all)."""
    if not os.path.exists(TEAMS_CSV):
        return {}
    t = pd.read_csv(TEAMS_CSV)
    return {int(r.teamId): str(r.abbr) for r in t.itertuples() if pd.notna(r.teamId)}

TEAM_MAP = load_team_map(fingerprint=file_fingerprint(TEAMS_CSV))

def team_label(tid):
    try:
        return TEAM_MAP.get(int(tid), str(tid))
    except (TypeError, ValueError):
        return str(tid)

# ---------- Core frames ----------
def pr_filter(df):
    # SACK is its own play type but it is a snap the same eleven played, and
    # leaving it out undercounted everyone by ~4% while hiding the down a
    # quarterback most needs charting on
    typ = df["nflPlayType"].fillna("").astype(str)
    return df[(typ == "RUSH") | (typ == "SACK") | (typ.str.startswith("PASS"))]

def scope_weeks(df, weeks_selected):
    if not weeks_selected:
        return df.iloc[0:0]
    return df[df["week"].isin(weeks_selected)]

@st.cache_data(show_spinner=False)
def build_scoped(data_dir, weeks_key, fingerprint=None):
    """Scrimmage plays (pass, rush, sack) and player-rows for the selected weeks,
    plus each unit's snap-order index within its game."""
    plays, pp = load_all(data_dir)
    weeks = list(weeks_key)
    plays_pr = scope_weeks(pr_filter(plays), weeks).copy()
    pp_pr = scope_weeks(pr_filter(pp), weeks).copy()
    pp_pr = pp_pr.drop_duplicates(["gameId", "nflPlayId", "playerName", "teamId", "side"])

    # down/distance/personnel live on the play table only (play_players.csv is
    # 22x bigger); join them on so every view can filter by situation
    sit_cols = [c for c in SITUATION_COLS if c in plays_pr.columns]
    if sit_cols:
        pp_pr = pp_pr.merge(plays_pr[["gameId", "nflPlayId"] + sit_cols],
                            on=["gameId", "nflPlayId"], how="left")
    plays_pr = add_situation_labels(plays_pr, DEFAULT_SIT)
    pp_pr = add_situation_labels(pp_pr, DEFAULT_SIT)

    # snap index: order of a unit's (team+side) scrimmage snaps within a game
    unit = (pp_pr[["gameId", "teamId", "side", "nflPlayId"]]
            .drop_duplicates()
            .sort_values(["gameId", "teamId", "side", "nflPlayId"]))
    unit["snapIndex"] = unit.groupby(["gameId", "teamId", "side"]).cumcount() + 1
    unit_sizes = (unit.groupby(["gameId", "teamId", "side"], as_index=False)["snapIndex"]
                  .max().rename(columns={"snapIndex": "unitSnaps"}))
    pp_pr = pp_pr.merge(unit, on=["gameId", "teamId", "side", "nflPlayId"], how="left")

    # drives: walk the FULL play list per game; a new drive starts when the
    # offense team changes or a possession-ending play type intervenes
    boundary_types = {"KICK_OFF", "PUNT", "FIELD_GOAL", "XP_KICK", "PAT2",
                      "INTERCEPTION", "GAME_START", "END_GAME"}
    off_team = (pp_pr[pp_pr["side"] == "off"]
                .groupby(["gameId", "nflPlayId"])["teamId"].first())
    drive_recs = []
    for gid, g in plays.sort_values(["gameId", "nflPlayId"]).groupby("gameId"):
        drive, snap_in, boundary, prev_team = 0, 0, True, None
        for r in g.itertuples():
            t = str(r.nflPlayType)
            if t in ("RUSH", "SACK") or t.startswith("PASS"):
                team = off_team.get((gid, r.nflPlayId))
                if team is None:
                    continue
                if boundary or team != prev_team:
                    drive += 1
                    snap_in = 0
                    boundary = False
                snap_in += 1
                prev_team = team
                drive_recs.append({"gameId": gid, "nflPlayId": r.nflPlayId,
                                   "driveNum": drive, "driveSnap": snap_in})
            elif t in boundary_types:
                boundary = True
    drives = pd.DataFrame(drive_recs)
    if not drives.empty:
        pp_pr = pp_pr.merge(drives, on=["gameId", "nflPlayId"], how="left")
        plays_pr = plays_pr.merge(drives, on=["gameId", "nflPlayId"], how="left")
        # ...and again as each unit's own possessions: driveNum counts both
        # teams, so a team's opening drive can be D2 of the game, which reads
        # like second-string work when it is exactly the opposite
        own = (pp_pr[["gameId", "teamId", "side", "driveNum"]].dropna()
               .drop_duplicates().sort_values(["gameId", "teamId", "side", "driveNum"]))
        own["teamDrive"] = own.groupby(["gameId", "teamId", "side"]).cumcount() + 1
        pp_pr = pp_pr.merge(own, on=["gameId", "teamId", "side", "driveNum"], how="left")
    else:
        pp_pr["driveNum"] = pp_pr["driveSnap"] = pp_pr["teamDrive"] = None
        plays_pr["driveNum"] = plays_pr["driveSnap"] = None
    return plays_pr, pp_pr, unit_sizes

def game_labels(pp_pr):
    """gameId -> 'Wk N vs OPP' label per team, derived from who shared each game."""
    g = pp_pr[["gameId", "week", "teamId"]].drop_duplicates()
    teams_by_game = g.groupby("gameId")["teamId"].apply(lambda s: sorted(set(s)))
    weeks_by_game = g.drop_duplicates("gameId").set_index("gameId")["week"]

    def label(game_id, my_team):
        others = [t for t in teams_by_game.get(game_id, []) if t != my_team]
        opp = team_label(others[0]) if others else "?"
        return f"Wk {weeks_by_game.get(game_id, '?')} vs {opp}"
    return label

# ---------- Situations ----------
# The CSVs carry the raw situation (down, distance, yards to the goal line,
# personnel counts); the buckets live here so the thresholds stay adjustable
# in the sidebar instead of being frozen into the data.
SITUATION_COLS = ["down", "yardsToGo", "yardsToGoal", "isGoalToGo", "isRedzone",
                  "quarter", "offN", "offRB", "offTE", "offOL", "defN", "defDB",
                  # who the ball went to on the play, so a player row can tell
                  # whether he was merely out there or actually got it
                  "rusher", "target", "receiver"]
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
# 8-10 and 11+ are split so a 1st & 10 doesn't sit in the same bucket as a
# 3rd & 15 - on the downs where it matters, that's the whole distinction
DIST_BUCKETS = ["1-3", "4-7", "8-10", "11+"]
DOWN_DIST = [f"{ORDINALS[d]} & {b}" for d in (1, 2, 3, 4) for b in DIST_BUCKETS]
CALLS = ["Short yardage", "Standard down", "Passing down"]
ZONES = ["Goal line (≤5 / GTG)", "Red zone (6-20)", "Open field", "Backed up (own ≤10)"]
DEF_PERSONNEL = ["Heavy (≤3 DB)", "Base (4 DB)", "Nickel (5 DB)", "Dime (6 DB)",
                 "Quarter (7+ DB)"]
# Football Outsiders' passing-down line, exposed so it can be argued with
DEFAULT_SIT = {"pass_late": 5, "pass_second": 8, "short": 2,
               "downs": [], "dists": [], "calls": [], "zones": [],
               "off_pers": [], "def_pers": []}
# ---------- Situation Board ----------
# The columns of the league-wide table. Each is a predicate over a frame that
# already carries the raw situation columns, so the same mask counts a player's
# snaps and the chances he had at them.
# RZ deliberately includes the goal line, unlike the mutually-exclusive "Red
# zone (6-20)" bucket in the Situations tabs — "red-zone snaps" in normal speech
# means everything inside the 20. The board says so on screen.
def _personnel(d, rb, te):
    return (num(d, "offN") == 11) & (num(d, "offRB") == rb) & (num(d, "offTE") == te)


BOARD_MASKS = {
    "RZ": lambda d, s: (num(d, "yardsToGoal") <= 20) | _bools(d, "isRedzone"),
    "GL": lambda d, s: (num(d, "yardsToGoal") <= 5) | _bools(d, "isGoalToGo"),
    "ShortYd": lambda d, s: num(d, "yardsToGo") <= s["short"],
    # what the offense actually did, not what the down chart suggested it would:
    # pass attempts, sacks and scrambles are all snaps the quarterback dropped
    # back on, and a 3rd & 8 handoff is not a passing snap by any useful reading
    "Dropback": lambda d, s: _bools(d, "isDropback"),
    "3rdDn": lambda d, s: num(d, "down") == 3,
    "Q4": lambda d, s: num(d, "quarter") == 4,
    "11": lambda d, s: _personnel(d, 1, 1),
    "12": lambda d, s: _personnel(d, 1, 2),
    "21": lambda d, s: _personnel(d, 2, 1),
    "13": lambda d, s: _personnel(d, 1, 3),
}
BOARD_HELP = {
    "Snaps": "His scrimmage snaps (pass, rush, sack) in the selected weeks.",
    "% of room": "Share of the snaps his team played at his position.",
    "Δ share": "Change in his share of team snaps, latest week vs the one before "
               "— the preseason signal is movement, not the total.",
    "Car · Tgt · Rec": "Carries, targets and catches, from the NFL's own stat "
                       "lines. Snaps are opportunity; these are a role.",
    "RZ": "Inside the 20 — goal-line snaps included.",
    "GL": "Inside the 5, or goal-to-go.",
    "ShortYd": "2 or fewer to go (the sidebar slider sets it).",
    "Dropback": "Snaps the offense actually dropped back on — pass attempts, "
                "sacks and scrambles. Not a down-and-distance guess.",
    "3rdDn": "Third down.",
    "Q4": "Fourth quarter — late duty is a depth signal.",
    "11 · 12 · 21 · 13": "Personnel groupings, RB then TE count: 11 is 1 RB 1 TE "
                         "3 WR, 12 is two tight ends, 21 adds a fullback, 13 is "
                         "three tight ends. Roster-listed positions, not "
                         "alignment. On defense these are what he faced.",
}
SKILL_POSITIONS = ["QB", "RB", "FB", "WR", "TE"]
DEF_POSITIONS = ["CB", "S", "FS", "SS", "DB", "OLB", "ILB", "MLB", "LB"]
BOARD_MODES = {"Played / chances": "chances", "Counts": "counts",
               "Share of chances %": "share", "Touches (car + tgt)": "touches"}
# what "his position room" means, for the share-of-room column
POSITION_ROOM = {"RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE", "QB": "QB",
                 "T": "OL", "G": "OL", "C": "OL",
                 "CB": "DB", "S": "DB", "FS": "DB", "SS": "DB", "DB": "DB",
                 "OLB": "LB", "ILB": "LB", "MLB": "LB", "LB": "LB",
                 "DE": "DL", "DT": "DL", "NT": "DL"}

SPLITS = {"Down & distance": ("Down & distance", DOWN_DIST),
          "Pass vs run down": ("Situation", CALLS),
          "Field zone": ("Field zone", ZONES),
          "Offense personnel": ("Off personnel", None),
          "Defense personnel": ("Def personnel", DEF_PERSONNEL)}
LABEL_COLS = ["Down & distance", "Situation", "Field zone",
              "Off personnel", "Def personnel"]


def has_situation(df):
    """Do these CSVs carry down & distance at all? (pre-backfill data doesn't)"""
    return "down" in df.columns and df["down"].notna().any()


def _bools(df, col):
    """CSV booleans, whatever dtype they survived the round trip as."""
    if col not in df.columns:
        return pd.Series(False, index=df.index)
    return df[col].astype(str).str.lower().isin(["true", "1", "1.0", "yes"])


def num(df, col):
    """A numeric column, or all-NaN when the CSVs predate it. Comparisons
    against NaN are False, which is what every caller here wants."""
    if col not in df.columns:
        return pd.Series(float("nan"), index=df.index)
    return pd.to_numeric(df[col], errors="coerce")


def call_bucket(df, sit):
    """Passing down / standard down / short yardage, at the given thresholds.

    Split out from the rest because it's the only bucket the sidebar sliders
    can move, so it's the only one worth recomputing when they do.
    """
    down = pd.to_numeric(df["down"], errors="coerce")
    ytg = pd.to_numeric(df["yardsToGo"], errors="coerce")
    # short yardage first: 3rd & 1 is a run down no matter what the down says
    passing = ((down >= 3) & (ytg >= sit["pass_late"])) | \
              ((down == 2) & (ytg >= sit["pass_second"]))
    return np.select([ytg <= sit["short"], passing, down.notna()],
                     [CALLS[0], CALLS[2], CALLS[1]], "?")


def add_situation_labels(df, sit):
    """Add the bucket columns every situational view splits on."""
    out = df.copy()
    if not has_situation(df):
        for c in LABEL_COLS:
            out[c] = "?"
        return out

    down = num(out, "down")
    ytg, to_goal = num(out, "yardsToGo"), num(out, "yardsToGoal")

    dist = pd.Series(np.select([ytg <= 3, ytg <= 7, ytg <= 10, ytg.notna()],
                               DIST_BUCKETS, "?"), index=out.index)
    down_lab = down.map(ORDINALS)
    out["Down & distance"] = (down_lab + " & " + dist).where(
        down_lab.notna() & dist.ne("?"), "?")

    out["Situation"] = call_bucket(out, sit)

    goal = _bools(out, "isGoalToGo") | (to_goal <= 5)
    red = _bools(out, "isRedzone") | (to_goal <= 20)
    out["Field zone"] = np.select([goal, red, to_goal >= 90, to_goal.notna()],
                                  [ZONES[0], ZONES[1], ZONES[3], ZONES[2]], "?")

    rb, te = num(out, "offRB"), num(out, "offTE")
    ol, off_n = num(out, "offOL"), num(out, "offN")
    code = (rb.fillna(0).astype(int).astype(str) + te.fillna(0).astype(int).astype(str))
    code = code + np.where(ol.eq(6), " (6 OL)", "")
    out["Off personnel"] = np.where(off_n.eq(11) & rb.notna() & te.notna(), code, "?")

    db, def_n = num(out, "defDB"), num(out, "defN")
    ok = def_n.eq(11) & db.notna()
    out["Def personnel"] = np.select(
        [ok & (db <= 3), ok & db.eq(4), ok & db.eq(5), ok & db.eq(6), ok & (db >= 7)],
        DEF_PERSONNEL, "?")
    return out


def situation_mask(df, sit):
    """Boolean mask for the sidebar's situation picks (empty pick = no filter)."""
    keep = pd.Series(True, index=df.index)
    if sit is None:
        return keep
    if sit["downs"] and "down" in df:
        keep &= pd.to_numeric(df["down"], errors="coerce").isin(sit["downs"])
    if sit["dists"] and "Down & distance" in df:
        keep &= df["Down & distance"].str.split(" & ").str[-1].isin(sit["dists"])
    for picks, col in ((sit["calls"], "Situation"), (sit["zones"], "Field zone"),
                       (sit["off_pers"], "Off personnel"),
                       (sit["def_pers"], "Def personnel")):
        if picks and col in df:
            keep &= df[col].isin(picks)
    return keep


def situation_active(sit):
    return bool(sit) and any(sit[k] for k in
                             ("downs", "dists", "calls", "zones", "off_pers", "def_pers"))


def unit_totals(pp):
    """Snaps per team-unit in whatever slice of plays is passed in."""
    return (pp[["gameId", "teamId", "side", "nflPlayId"]].drop_duplicates()
            .groupby(["gameId", "teamId", "side"], as_index=False).size()
            .rename(columns={"size": "unitSnaps"}))


def scoped(data_dir, weeks_selected, sit):
    """build_scoped + situation labels + the sidebar's situation filter.

    Snap order and drive numbers stay on the full play list — a filtered view
    should still say a player entered on his unit's 12th snap, not its 3rd.
    """
    plays_pr, pp_pr, unit_sizes = build_scoped(
        data_dir, tuple(weeks_selected),
        fingerprint=file_fingerprint(os.path.join(data_dir, "play_players.csv")))
    # build_scoped labels at the default thresholds (it's cached); only the
    # pass/run call moves when the sliders do, so that's all we redo here
    if sit and any(sit[k] != DEFAULT_SIT[k] for k in ("pass_late", "pass_second", "short")):
        plays_pr = plays_pr.assign(Situation=call_bucket(plays_pr, sit))
        pp_pr = pp_pr.assign(Situation=call_bucket(pp_pr, sit))
    pp_all = pp_pr  # pre-filter: the Situations tabs need the whole breakdown
    if situation_active(sit):
        plays_pr = plays_pr[situation_mask(plays_pr, sit)]
        pp_pr = pp_pr[situation_mask(pp_pr, sit)]
        unit_sizes = unit_totals(pp_pr)
    return plays_pr, pp_pr, unit_sizes, pp_all


# ---------- Chances: was he out there when the situation came up? ----------
# A raw bucket count can't tell "he came off for it" from "it never happened
# while he was in". These build the denominator that can: the snaps his unit
# ran between his first and last snap of a game, optionally narrowed to the
# stretch one QB was on the field for — the closest thing preseason data has
# to "the first-team offense was out there".

def qb_by_play(pp):
    """(gameId, nflPlayId) -> the QB on the field for the offense."""
    qb = pp[(pp["side"] == "off") & (pp["position"] == "QB")]
    return (qb[["gameId", "nflPlayId", "teamId", "playerName"]]
            .rename(columns={"teamId": "qbTeamId", "playerName": "QB"})
            .drop_duplicates(["gameId", "nflPlayId"]))


def qb_options(pp, units):
    """QBs whose time on the field overlaps these team-units, most snaps first.

    For an offense that's its own quarterbacks; for a defense it's the ones it
    lined up against, which is the same "were the starters in?" question.
    """
    plays = units.merge(qb_by_play(pp), on=["gameId", "nflPlayId"], how="inner")
    own = plays["qbTeamId"] == plays["teamId"]
    plays = plays[((plays["side"] == "off") & own) | ((plays["side"] == "def") & ~own)]
    return plays, plays.groupby("QB").size().sort_values(ascending=False)


def unit_snaps_frame(pp, team, side, cols):
    """One row per snap the unit played, carrying its situation labels."""
    sd = pp[(pp["teamId"] == team) & (pp["side"] == side)]
    keep = ["gameId", "teamId", "side", "nflPlayId", "snapIndex"] + list(cols)
    return sd[keep].drop_duplicates(["gameId", "nflPlayId"])


def stint_windows(pp, team, side):
    """Per player and game: the unit's first and last snap he was on for."""
    sd = pp[(pp["teamId"] == team) & (pp["side"] == side)]
    return (sd.groupby(["playerName", "gameId", "teamId", "side"])["snapIndex"]
            .agg(first="min", last="max").reset_index())


# How much of the game counts as "his to play". The drive is the default
# because that's the unit preseason rotations actually swap in: a player who
# takes a snap on a drive was in the game for that drive, and a snap on it he
# didn't take is a real substitution. First-to-last is the looser one — it
# spans drives he sat out entirely, which reads as passing up chances he was
# never offered — and is kept for players who rotate within a drive.
WINDOWS = {"Drives he played in": "drives",
           "First to last snap": "stint",
           "Every snap the unit played": "unit"}


def chances_matrix(pp, team, side, col, window, qb_plays=None):
    """Players x buckets: the snaps that were his to play, per `window`."""
    unit = unit_snaps_frame(pp, team, side, [col, "driveNum"])
    if qb_plays is not None:
        unit = unit.merge(qb_plays, on=["gameId", "nflPlayId"])
    mine = pp[(pp["teamId"] == team) & (pp["side"] == side)]

    if window == "unit":
        totals = unit.groupby(col)["nflPlayId"].count()
        names = pd.Index(sorted(mine["playerName"].unique()), name="playerName")
        return pd.DataFrame([totals] * len(names), index=names)

    if window == "stint":
        m = stint_windows(pp, team, side).merge(unit, on=["gameId", "teamId", "side"])
        m = m[(m["snapIndex"] >= m["first"]) & (m["snapIndex"] <= m["last"])]
    else:
        played = (mine[["playerName", "gameId", "driveNum"]].dropna().drop_duplicates())
        m = played.merge(unit.dropna(subset=["driveNum"]), on=["gameId", "driveNum"])

    if m.empty:
        return pd.DataFrame(index=pd.Index([], name="playerName"))
    return m.pivot_table(index="playerName", columns=col, values="nflPlayId",
                         aggfunc="count", fill_value=0)


# ---------- Situation Board: the whole league in one table ----------

def starting_qb_snaps(pp):
    """Snaps with that game's STARTING quarterback on the field.

    Per team per game, not per season: preseason starters rotate, and the
    starter is the QB who took the unit's first snap — emphatically not the one
    with the most snaps, who is usually the third-stringer mopping up.
    """
    qb = qb_by_play(pp)  # ordered by nflPlayId, which is play order within a game
    first = (qb.sort_values("nflPlayId").groupby(["gameId", "qbTeamId"]).head(1)
             [["gameId", "qbTeamId", "QB"]].rename(columns={"QB": "QB1"}))
    started = qb.merge(first, on=["gameId", "qbTeamId"])
    started = started[started["QB"] == started["QB1"]]
    return started[["gameId", "nflPlayId", "qbTeamId", "QB1"]]


NAME_SUFFIXES = {"Jr.", "Sr.", "II", "III", "IV", "V"}


def qb1_label(names):
    """Name the starter(s) behind a w/ QB1 ratio.

    A player's denominator is only meaningful once you know whose first team it
    was, and that changes game to game — AZ opened the HOF game with Beck and
    week 1 with Brissett, so two Cardinals RBs can show different denominators
    and be measured against different quarterbacks.
    """
    names = [n for n in dict.fromkeys(names) if isinstance(n, str) and n]
    if not names:
        return "—"
    if len(names) == 1:
        return names[0]
    last = []
    for n in names:
        parts = n.split()
        last.append(parts[-2] if len(parts) > 2 and parts[-1] in NAME_SUFFIXES
                    else parts[-1])
    return " / ".join(last[:2]) + (f" +{len(last) - 2}" if len(last) > 2 else "")


def drive_label(rows, cap=8):
    """"HOF D1,D2 · Wk 1 D3" — which of his unit's own drives he played.

    Numbered per team, not per game: the game's drive numbering alternates
    between the two sidelines, so a team's opening possession can be D2, which
    reads like second-string work when it is the exact opposite.
    """
    parts, shown = [], 0
    for week, grp in rows.sort_values(["week", "teamDrive"]).groupby("week"):
        nums = [int(d) for d in grp["teamDrive"]]
        room = max(cap - shown, 0)
        if not room:
            break
        parts.append(f"{WEEK_NAMES.get(week, week)} "
                     + ",".join(f"D{n}" for n in nums[:room]))
        shown += min(len(nums), room)
    total = len(rows)
    return " · ".join(parts) + (f" +{total - shown}" if total > shown else "")


def dropback_flag(df):
    """Did the offense drop back on this snap? Pass, sack or scramble.

    Sacks and scrambles are pass plays that never became throws; counting them
    as runs would credit a quarterback's escape to the ground game and leave a
    lineman's worst snaps out of his pass-protection total.
    """
    typ = df["nflPlayType"].fillna("").astype(str)
    desc = df.get("nflPlayDescription", pd.Series("", index=df.index)).fillna("")
    return typ.str.startswith("PASS") | typ.eq("SACK") | desc.str.contains("scramble")


def league_chances(pp):
    """One row per (player, snap that was his to play), for every team at once.

    Same rule as chances_matrix's drive window — every snap of a drive he took
    at least one snap on — but as a single merge rather than a loop per team,
    because the board needs all 32 at once.
    """
    carry = [c for c in list(SITUATION_COLS) + ["isDropback"] if c in pp.columns]
    unit = pp[["gameId", "teamId", "side", "nflPlayId", "driveNum"] + carry
              ].drop_duplicates(["gameId", "teamId", "side", "nflPlayId"])
    mine = (pp[["playerName", "gameId", "teamId", "side", "driveNum"]]
            .dropna(subset=["driveNum"]).drop_duplicates())
    return mine.merge(unit.dropna(subset=["driveNum"]),
                      on=["gameId", "teamId", "side", "driveNum"])


@st.cache_data(show_spinner=False)
def board_frame(data_dir, weeks_key, pass_late, pass_second, short, side,
                first_team_only, fingerprint=None):
    """Player x situation counts for every team. Scalars only, so it caches."""
    sit = dict(DEFAULT_SIT, pass_late=pass_late, pass_second=pass_second, short=short)
    _, pp_all, _ = build_scoped(data_dir, weeks_key, fingerprint=fingerprint)

    # before the side filter: on a defensive snap the quarterback is one of the
    # eleven on the *other* side of the ball, so he isn't in `pp` at all. Each
    # play has one offense, so this resolves to his own starter on offensive
    # rows and the one he lined up against on defensive rows.
    qb1 = starting_qb_snaps(pp_all)
    qb1_keys = qb1[["gameId", "nflPlayId"]].drop_duplicates()
    starters = qb1[["gameId", "qbTeamId", "QB1"]].drop_duplicates()

    pp = pp_all[pp_all["side"] == side].copy()
    if pp.empty:
        return pd.DataFrame()
    pp["isDropback"] = dropback_flag(pp)
    if first_team_only:  # narrows both halves of every ratio
        pp = pp.merge(qb1_keys, on=["gameId", "nflPlayId"])
        if pp.empty:
            return pd.DataFrame()

    chances = league_chances(pp)
    key = ["playerName", "teamId"]
    # the ball came to him on this snap — carries and targets are opportunity,
    # which is what separates a role from a jersey standing in formation
    for col, flag in (("rusher", "isCarry"), ("target", "isTarget"),
                      ("receiver", "isRec")):
        pp[flag] = (pp[col] == pp["playerName"]) if col in pp.columns else False
    pp["isTouch"] = pp["isCarry"] | pp["isTarget"]

    board = (pp.groupby(key)
             .agg(Pos=("position", lambda s: s.mode().iat[0] if not s.mode().empty else "?"),
                  Snaps=("nflPlayId", "size"),
                  Car=("isCarry", "sum"), Tgt=("isTarget", "sum"),
                  Rec=("isRec", "sum"))
             .reset_index())

    # his share of the snaps his team played at his position — 47 snaps means
    # nothing until you know whether the room ran 60 or 200
    room = pp.assign(room=pp["position"].map(POSITION_ROOM).fillna(pp["position"]))
    room_total = (room.groupby(["teamId", "room"])["nflPlayId"].size()
                  .rename("roomSnaps").reset_index())
    his_room = room.groupby(key)["room"].agg(
        lambda s: s.mode().iat[0] if not s.mode().empty else "?").rename("room").reset_index()
    board = (board.merge(his_room, on=key, how="left")
             .merge(room_total, on=["teamId", "room"], how="left"))

    # week over week: the preseason signal is movement, not the total
    weeks = sorted(pp["week"].dropna().unique())
    if len(weeks) >= 2:
        last, prev = weeks[-1], weeks[-2]
        per_week = pp.groupby(key + ["week"])["nflPlayId"].size().rename("n").reset_index()
        team_week = (pp.drop_duplicates(["gameId", "nflPlayId", "teamId"])
                     .groupby(["teamId", "week"]).size().rename("teamN").reset_index())
        per_week = per_week.merge(team_week, on=["teamId", "week"], how="left")
        per_week["share"] = 100 * per_week["n"] / per_week["teamN"].where(per_week["teamN"] > 0)
        wide = per_week.pivot_table(index=key, columns="week", values="share")
        delta = (wide.get(last, 0) - wide.get(prev, 0)).rename("dShare").reset_index()
        board = board.merge(delta, on=key, how="left")
    else:
        board["dShare"] = float("nan")

    # w/ QB1: of the snaps the starter was on the field for, how many was he?
    with_q1 = pp.merge(qb1_keys, on=["gameId", "nflPlayId"])
    played_q1 = with_q1.groupby(key).size().rename("q1Played")
    # denominator counts only the games he dressed for - a player who appeared
    # in week 3 alone shouldn't be marked down for the starter snaps of the two
    # weeks he wasn't there
    per_game = (with_q1.drop_duplicates(["gameId", "nflPlayId", "teamId"])
                .groupby(["gameId", "teamId"]).size().rename("gameQ1").reset_index())
    his_games = pp[["playerName", "teamId", "gameId"]].drop_duplicates()
    team_q1 = (his_games.merge(per_game, on=["gameId", "teamId"], how="left")
               .groupby(key)["gameQ1"].sum().rename("q1Team"))
    board = board.merge(played_q1, on=key, how="left").merge(team_q1, on=key, how="left")

    # who that starter actually was, per player, across the games he was there
    if side == "off":
        named = his_games.merge(starters, left_on=["gameId", "teamId"],
                                right_on=["gameId", "qbTeamId"], how="left")
    else:  # the starter on the other sideline
        named = his_games.merge(starters, on="gameId", how="left")
        named = named[named["qbTeamId"] != named["teamId"]]
    board = board.merge(named.groupby(key)["QB1"].apply(qb1_label).rename("QB1name"),
                        on=key, how="left")

    # which of his team's drives he was actually out there for: the aggregate
    # counts say how much, this says when — early drives are the starters'
    drives = pp[["playerName", "teamId", "week", "teamDrive"]].dropna().drop_duplicates()
    board = board.merge(drives.groupby(key).size().rename("Drives"), on=key, how="left")
    # columns picked before apply, not include_groups= — that kwarg only exists
    # on pandas 2.2+, and this way needs no floor at all
    board = board.merge(drives.groupby(key)[["week", "teamDrive"]].apply(drive_label)
                        .rename("driveList"), on=key, how="left")

    for name, mask in BOARD_MASKS.items():
        in_it = pp[mask(pp, sit)]
        played = in_it.groupby(key).size().rename(f"{name}Played")
        had = chances[mask(chances, sit)].groupby(key).size().rename(f"{name}Chances")
        touched = in_it.groupby(key)["isTouch"].sum().rename(f"{name}Touch")
        board = (board.merge(played, on=key, how="left")
                 .merge(had, on=key, how="left").merge(touched, on=key, how="left"))

    counts = [c for c in board.columns
              if c.endswith(("Played", "Chances", "Team", "Touch"))]
    board[counts] = board[counts].fillna(0).astype(int)
    board["PosShare"] = (100 * board["Snaps"] /
                         board["roomSnaps"].where(board["roomSnaps"] > 0)).round(1)
    board["Team"] = board["teamId"].map(team_label)
    return board


def board_display(board, mode, side="off"):
    """The frame as shown: ratios, plain counts, or share of chances."""
    out = pd.DataFrame({"Player": board["playerName"], "Team": board["Team"],
                        "Pos": board["Pos"], "Snaps": board["Snaps"]})
    out["% of room"] = board["PosShare"]
    out["Δ share"] = board["dShare"].round(1)
    if side == "off":  # nobody carries the ball on defense
        # catches are left out on purpose: targets are the usage signal, and
        # whether the pass arrived is on the quarterback as much as the receiver
        out["Car"], out["Tgt"] = board["Car"], board["Tgt"]
    # on defense the starter in question is the one across the line
    out["QB1" if side == "off" else "QB1 faced"] = board["QB1name"].fillna("—")
    pairs = [("w/ QB1" if side == "off" else "vs QB1", "q1Played", "q1Team", None)] + \
            [(n, f"{n}Played", f"{n}Chances", f"{n}Touch") for n in BOARD_MASKS]
    for label, played, total, touch in pairs:
        if mode == "touches" and touch:
            out[label] = board[touch]
        elif mode == "counts" or (mode == "touches" and not touch):
            out[label] = board[played]
        elif mode == "share":
            out[label] = (100 * board[played] /
                          board[total].where(board[total] > 0)).round(1)
        else:
            # right-justified so the browser's own column sort, which compares
            # these as text, still puts 9/9 below 10/12 where it belongs
            width = board[played].astype(str).str.len().max()
            out[label] = (board[played].astype(str).str.rjust(int(width or 1))
                          + "/" + board[total].astype(str))
    out["Drives"] = board["Drives"].fillna(0).astype(int)
    out["Drives played"] = board["driveList"].fillna("—")
    return out


WATCHLIST_PATH = os.path.join(APP_DIR, "watchlist.json")


def load_watchlist():
    """Starred players, kept on disk so they survive a restart."""
    try:
        with open(WATCHLIST_PATH, encoding="utf-8") as f:
            names = json.load(f)
        return [n for n in names if isinstance(n, str)]
    except (OSError, ValueError):
        return []


def save_watchlist(names):
    try:
        with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(set(names)), f, indent=1)
    except OSError as e:  # read-only filesystem on the hosted app
        st.caption(f"(Watchlist not saved: {e.strerror})")


def render_movers(board, weeks_selected):
    """Who moved since last week — the question a second week makes askable."""
    if board["dShare"].notna().sum() < 2 or len(weeks_selected) < 2:
        return
    ranked = board.dropna(subset=["dShare"]).sort_values("dShare", ascending=False)
    up = ranked[ranked["dShare"] > 0].head(5)
    down = ranked[ranked["dShare"] < 0].tail(5).iloc[::-1]
    line = lambda r: f"{r.playerName} ({r.Team} {r.Pos}) {r.dShare:+.0f}pts"
    c1, c2 = st.columns(2)
    c1.caption("**Climbing** — " + (" · ".join(line(r) for r in up.itertuples())
                                    or "nobody gained ground"))
    c2.caption("**Slipping** — " + (" · ".join(line(r) for r in down.itertuples())
                                    or "nobody lost ground"))


def board_styler(table, mode):
    """Colour the numeric columns so 600 rows can be scanned, not read."""
    numeric = [c for c in ("% of room", "Δ share") if c in table.columns]
    if mode in ("share", "counts", "touches"):
        numeric += [c for c in BOARD_MASKS if c in table.columns]
    numeric = [c for c in numeric if pd.api.types.is_numeric_dtype(table[c])]
    styler = table.style
    for col in numeric:
        # Δ is signed, so it gets a diverging scale centred on "no change"
        if col == "Δ share":
            limit = max(abs(table[col].min() or 0), abs(table[col].max() or 0), 1)
            styler = styler.background_gradient("RdYlGn", subset=[col],
                                                vmin=-limit, vmax=limit)
        else:
            styler = styler.background_gradient("Blues", subset=[col])
    # em dash, not "None": a player with one week of snaps has no delta to show,
    # which is an absence of comparison rather than a value of nothing
    return styler.format(precision=1, na_rep="—")


def render_board(data_dir, weeks_selected, season, sit):
    st.title("Situation Board")
    st.caption("Every skill player in the league, and what he was on the field for. "
               "**played / chances** — a chance is a snap on a drive he took part in, "
               "so **2/9** means he was in the game for nine of them and out there "
               "for two, while **0/0** means it never came up while he was in. "
               "Sort from any column header; the Show toggle swaps every situation "
               "column between snaps, share and touches.")
    sit = sit or DEFAULT_SIT
    if situation_active(sit):
        st.caption("This view ignores the sidebar situation filter — the columns "
                   "*are* the situations.")

    # filters first: they're what gets touched most, so they lead the page
    unit = st.radio("Unit", ["Offense", "Defense"], horizontal=True, key="bd_unit")
    side = "off" if unit == "Offense" else "def"

    board = board_frame(
        data_dir, tuple(weeks_selected), sit["pass_late"], sit["pass_second"],
        sit["short"], side, st.session_state.get("bd_first", False),
        fingerprint=file_fingerprint(os.path.join(data_dir, "play_players.csv")))
    if board.empty:
        st.info("No snaps with the current week filter.")
        return

    default_pos = SKILL_POSITIONS if side == "off" else DEF_POSITIONS
    pos_opts = sorted(board["Pos"].unique())
    c1, c2 = st.columns(2)
    with c1:
        teams = st.multiselect("Team", sorted(board["Team"].unique()), key="bd_teams",
                               placeholder="All 32 teams", help="Leave empty for all.")
    with c2:
        positions = st.multiselect(
            "Position", pos_opts, default=[p for p in default_pos if p in pos_opts],
            key=f"bd_pos_{side}", placeholder="All positions",
            help="Skill positions by default — clear it to see linemen too.")

    c3, c4, c5 = st.columns([2, 1, 1])
    with c3:
        mode_name = st.radio("Show", list(BOARD_MODES), horizontal=True, key="bd_mode")
    with c4:
        st.checkbox("First team only", key="bd_first",
                    help="Count only the snaps that game's starting QB was on the "
                         "field for — both halves of every ratio.")
    with c5:
        min_snaps = st.number_input("Min snaps", 0, 200, 1, key="bd_min")
    mode = BOARD_MODES[mode_name]

    here = set(board["playerName"])
    stored = load_watchlist()
    watch = st.multiselect("⭐ Watchlist", sorted(here),
                           default=[n for n in stored if n in here],
                           key=f"bd_watch_{side}",
                           placeholder="Star the players you keep coming back to",
                           help="Saved to watchlist.json, so it survives a restart.")
    # keep starred players this unit can't offer as options — otherwise a look
    # at the defense would quietly drop every offensive player from the file
    merged = sorted((set(stored) - here) | set(watch))
    if merged != sorted(stored):
        save_watchlist(merged)
    only_watch = st.checkbox("Only my watchlist", key="bd_watch_only",
                             disabled=not watch)

    render_movers(board, weeks_selected)

    shown = board
    if only_watch and watch:
        shown = shown[shown["playerName"].isin(watch)]
    if positions:
        shown = shown[shown["Pos"].isin(positions)]
    if teams:
        shown = shown[shown["Team"].isin(teams)]
    hidden = int((shown["Snaps"] < min_snaps).sum())
    shown = shown[shown["Snaps"] >= min_snaps]
    if shown.empty:
        st.info("Nothing matches those filters.")
        return

    # no sort control: every column sorts from its own header, including the
    # ratio ones now that the numerators line up
    shown = shown.sort_values("Snaps", ascending=False)

    table = board_display(shown, mode, side)
    st.caption(f"{len(table)} player(s)"
               + (f" · {hidden} below {min_snaps} snaps hidden" if hidden else ""))
    with st.expander("What the columns mean"):
        st.markdown("\n".join(f"- **{k}** — {v}" for k, v in BOARD_HELP.items())
                    + "\n- **w/ QB1** — of the snaps that game's *starting* QB was "
                      "on the field for, how many he was out there for. The starter "
                      "is whoever took the unit's first snap, not whoever took the "
                      "most — in preseason those are opposite people. On defense it "
                      "counts snaps against the opposing starter.")
    st.caption("Click a row to open that player's detail page.")
    # The key carries a nonce so a jump can drop the selection behind it —
    # otherwise coming back to the board would bounce straight out again on the
    # row still highlighted from last time.
    nonce = st.session_state.get("bd_sel_nonce", 0)
    event = st.dataframe(board_styler(table, mode), **WIDE, hide_index=True,
                         height=620, key=f"bd_table_{nonce}", on_select="rerun",
                         selection_mode="single-row")
    picked = getattr(getattr(event, "selection", None), "rows", None) or []
    if picked:
        st.session_state["bd_sel_nonce"] = nonce + 1
        st.session_state["player_pick"] = table.iloc[picked[0]]["Player"]
        st.session_state["view"] = "Player Explorer"
        st.rerun()

    st.download_button("Download board (CSV)",
                       table.to_csv(index=False).encode("utf-8"),
                       file_name=f"situation_board_{season}_{side}.csv",
                       mime="text/csv")


def situation_controls(plays_df):
    """Sidebar situation picker. -> sit dict, or None when the data predates it."""
    st.sidebar.header("Situation")
    pr = pr_filter(plays_df)
    if not has_situation(pr):
        st.sidebar.caption("These CSVs have no down & distance yet — run "
                           "`python backfill_situations.py`, then the fetch/preprocess "
                           "step, to add it.")
        return None

    sit = dict(DEFAULT_SIT)
    with st.sidebar.expander("Definitions"):
        st.caption("Where the passing-down line sits. Defaults follow the usual "
                   "convention: 3rd/4th & 5+, 2nd & 8+.")
        sit["pass_late"] = st.slider("3rd/4th down is a passing down at", 3, 10, 5)
        sit["pass_second"] = st.slider("2nd down is a passing down at", 5, 15, 8)
        sit["short"] = st.slider("Short yardage is this or less", 1, 4, 2)

    labeled = add_situation_labels(pr, sit)
    off_opts = sorted(c for c in labeled["Off personnel"].unique() if c != "?")
    with st.sidebar.expander("Filter snaps"):
        st.caption("Leave a box empty to include everything. Filters apply to every "
                   "view — co-players, depth chart, timelines and all.")
        sit["downs"] = st.multiselect("Down", [1, 2, 3, 4],
                                      format_func=lambda d: ORDINALS[d])
        sit["dists"] = st.multiselect("Distance", DIST_BUCKETS)
        sit["calls"] = st.multiselect("Down type", CALLS)
        sit["zones"] = st.multiselect("Field zone", ZONES)
        sit["off_pers"] = st.multiselect("Offense personnel", off_opts,
                                         help="RB + TE count off the listed positions, "
                                              "e.g. 11 = 1 RB, 1 TE, 3 WR.")
        sit["def_pers"] = st.multiselect("Defense personnel", DEF_PERSONNEL)

    if situation_active(sit):
        kept = int(situation_mask(labeled, sit).sum())
        st.sidebar.caption(f"⚑ Situation filter on — {kept} of {len(labeled)} "
                           f"scrimmage plays ({100 * kept / max(len(labeled), 1):.0f}%).")
    return sit


def buckets_faced(unit_snaps, col, order):
    """(order kept to what this unit actually saw, the rest) — the rest is
    reported rather than dropped silently."""
    if not order:
        return None, []
    seen = set(unit_snaps[col])
    return [b for b in order if b in seen], [b for b in order if b not in seen]


def split_table(pp, split_col, order, unit_snaps_by_bucket, mode, chances=None):
    """Players (rows) x situation buckets (columns).

    mode: 'snaps' raw counts · 'share' % of every unit snap in that bucket ·
    'chances' played/chances inside his own stint, which is the only one that
    separates "he came off for it" from "it never came up while he was in".
    """
    mat = pp.pivot_table(index="playerName", columns=split_col, values="nflPlayId",
                         aggfunc="count", fill_value=0)
    # `order` is the buckets the unit actually faced, so they survive here even
    # when this slice has none of them: a goal-line column that disappears
    # because one QB never saw one reads as "that never happens", which is the
    # same lie 0/0 exists to stop telling. Callers name what they dropped.
    known = [c for c in (order or sorted(mat.columns)) if c != "?"]
    extra = [c for c in mat.columns if c not in known and c != "?"]
    mat = mat.reindex(columns=known + extra + (["?"] if "?" in mat.columns else []),
                      fill_value=0)
    totals = mat.sum(axis=1)
    if mode == "share":
        denom = unit_snaps_by_bucket.reindex(mat.columns).fillna(0)
        mat = (100 * mat.div(denom.where(denom > 0), axis=1)).round(1)
    elif mode == "chances" and chances is not None:
        had = (chances.reindex(index=mat.index, columns=mat.columns)
               .fillna(0).astype(int).astype(str))
        mat = mat.astype(int).astype(str) + "/" + had
    pos = (pp.groupby("playerName")["position"]
           .agg(lambda s: s.mode().iat[0] if not s.mode().empty else "?"))
    mat.insert(0, "Snaps", totals)
    mat.insert(0, "Pos", mat.index.map(pos))
    return mat.loc[totals.sort_values(ascending=False).index]

# ---------- 2026 update pipeline (local only) ----------
# Fresh data reaches the hosted app only by being committed and pushed: Cloud
# serves the CSVs out of this repo, and its own filesystem is wiped on every
# restart. So the whole loop — paste auth, fetch, publish — runs from a local
# `streamlit run`, and step 3 is what the hosted app actually sees.
# "data" (the 2025 CSVs) is in here too — it barely ever changes, but when it
# does, a publish that skipped it would leave the hosted app serving 2025 data
# the local app has already moved past.
PUBLISH_PATHS = ["data", "data_2026", "games_2026", "teams.csv", "hc_by_season.csv",
                 "starter_summary.csv", "starter_trends.csv",
                 "starter_players.csv", "starter_weekly.csv"]


def running_locally():
    """Streamlit Community Cloud serves the repo out of /mount/src. The hosted
    app is public, so the auth box and the fetch buttons stay local-only."""
    here = os.path.abspath(APP_DIR).replace("\\", "/")
    return not here.startswith(("/mount/src", "/app"))


def elapsed_since(t0):
    s = int(time.time() - t0)
    return f"{s // 60}m {s % 60:02d}s"


def run_script(script, progress=False, keep=10):
    """Run a pipeline script, showing its output as it arrives.

    capture_output=True held every line until the process exited, which made a
    ten-minute fetch look like a hung spinner. -u stops the child buffering.
    """
    t0 = time.time()
    lines, log = [], st.empty()
    bar = st.progress(0.0, text="starting…") if progress else None
    total = current = 0
    within = 0.0
    detail = "starting…"

    proc = subprocess.Popen([sys.executable, "-u", script], cwd=APP_DIR,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    for raw in proc.stdout:
        line = raw.rstrip()
        if not line:
            continue
        lines.append(line)
        log.code("\n".join(lines[-keep:]))
        if not bar:
            continue
        # fetch_2026.py's own prints, read back as progress
        m = re.search(r"(\d+) game\(s\) to fetch", line)
        if m:
            total = int(m.group(1))
        if line.startswith("Fetching game"):
            current, within, detail = current + 1, 0.0, "reading play list"
        m = re.search(r"(\d+)/(\d+) plays", line)
        if m:
            within = int(m.group(1)) / max(int(m.group(2)), 1)
            detail = f"{m.group(1)}/{m.group(2)} plays"
        if line.lstrip().startswith("wrote "):
            within, detail = 1.0, "saved"
        if total:
            head = (f"game {current} of {total} · {detail}" if current
                    else f"{total} game(s) to fetch")
            bar.progress(min((max(current - 1, 0) + within) / total, 1.0),
                         text=f"{head} · {elapsed_since(t0)}")
    proc.wait()

    if not lines:
        log.code("(no output)")
    if bar:
        bar.progress(1.0, text=f"done in {elapsed_since(t0)}")
    return proc.returncode == 0


def git(*args):
    """Run git in APP_DIR -> (ok, combined output)."""
    try:
        r = subprocess.run(["git", *args], cwd=APP_DIR, capture_output=True, text=True)
    except OSError as e:
        return False, f"git isn't available here ({e})"
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def push_target():
    """(remote, branch, owner/repo) a publish would push to, or None.

    Named in the UI on purpose: this repo has more than one remote, and a
    push landing somewhere the hosted app doesn't read looks exactly like a
    deploy that silently did nothing.
    """
    ok, ref = git("rev-parse", "--abbrev-ref", "@{upstream}")
    if not ok or "/" not in ref:
        return None
    remote, branch = ref.split("/", 1)
    ok, url = git("remote", "get-url", remote)
    slug = "/".join(url.rstrip("/").removesuffix(".git").replace("\\", "/").split("/")[-2:]) if ok else "?"
    return remote, branch, slug


def publish_status():
    """Is any pipeline output still sitting on this machine only?"""
    # -uall lists new game files one by one instead of collapsing them into
    # "games_2026/", so the count below means something.
    ok, out = git("status", "--porcelain", "-uall", "--", *PUBLISH_PATHS)
    if not ok:
        return {"state": "nogit", "short": "git unavailable", "files": [],
                "message": f"Can't run git here, so publishing has to be manual. ({out[:120]})"}
    # "XY path" -- split rather than slice, since git() has stripped the leading
    # status column off the first line.
    files = [line.split(maxsplit=1)[-1] for line in out.splitlines() if line.strip()]
    ok_ahead, ahead_out = git("rev-list", "--count", "@{upstream}..HEAD")
    ahead = int(ahead_out) if ok_ahead and ahead_out.isdigit() else 0
    if files:
        return {"state": "dirty", "short": f"{len(files)} file(s) to publish", "files": files,
                "message": f"{len(files)} data file(s) changed on this machine. The hosted "
                           "app keeps showing the old numbers until you publish."}
    if ahead:
        return {"state": "ahead", "short": f"{ahead} commit(s) to push", "files": [],
                "message": f"{ahead} local commit(s) haven't reached origin/main yet."}
    return {"state": "clean", "short": "published", "files": [],
            "message": "The hosted app already has everything on this machine."}


def publish_data(files):
    """Commit the pipeline outputs and push to origin/main. -> (ok, message)"""
    if files:
        present = [p for p in PUBLISH_PATHS if os.path.exists(os.path.join(APP_DIR, p))]
        ok, out = git("add", "--", *present)
        if not ok:
            return False, f"git add failed:\n{out}"
        ok, out = git("commit", "-m", f"data: update 2026 preseason ({len(files)} file(s))")
        if not ok:
            return False, f"git commit failed:\n{out}"
    target = push_target()
    ok, out = git("push", target[0], f"HEAD:{target[1]}") if target else git("push")
    if not ok:
        low = out.lower()
        if "rejected" in low or "non-fast-forward" in low:
            out += ("\n\nThe remote has commits you don't. Run `git pull --rebase` "
                    "in this folder, then publish again.")
        elif "authentication" in low or "could not read" in low or "denied" in low:
            out += ("\n\nGit couldn't authenticate to GitHub. Push once from a "
                    "terminal to refresh your saved credentials, then try again.")
        return False, f"git push failed:\n{out}"
    where = f"{target[0]}/{target[1]} ({target[2]})" if target else "the default remote"
    return True, (f"Pushed to {where}. Streamlit Cloud redeploys on its own, "
                  "usually within a minute or two.")


# ---------- Coverage: is every game for a week actually in? ----------
# The fetch prints what it did and then the console scrolls away, which leaves
# no way to answer "did all 16 week-2 games land?" after the fact. This asks
# the schedule and compares it against the data the app is actually serving.

@st.cache_data(ttl=600, show_spinner=False)
def schedule_for(season, nonce=0):
    """The API's preseason schedule. `nonce` is how the Re-check button busts it."""
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)
    from fetch_2026 import scheduled_games
    return scheduled_games(season)


@st.cache_data(show_spinner=False)
def games_in_data(data_dir, fingerprint=None):
    """gameId -> the API's game uuid, read back out of each play's NFL Pro link.

    Taken from the CSVs rather than the game JSONs so this works for any season
    and on the hosted app, which serves data whose raw JSONs aren't committed.
    """
    plays, _ = load_all(data_dir)
    first = plays.dropna(subset=["nflPlayUrl"]).drop_duplicates("gameId")
    uuid = first["nflPlayUrl"].str.extract(r"gameId=([0-9a-f-]+)", expand=False)
    return pd.DataFrame({"gameId": first["gameId"].values, "week": first["week"].values,
                         "uuid": uuid.values}).dropna(subset=["uuid"])


@st.cache_data(show_spinner=False)
def smart_id_abbr(fingerprint=None):
    """smartId -> abbr, for naming games the schedule lists but the data lacks."""
    if not os.path.exists(TEAMS_CSV):
        return {}
    t = pd.read_csv(TEAMS_CSV, dtype=str)
    if "smartId" not in t.columns:
        return {}
    return {r.smartId: r.abbr for r in t.itertuples()
            if pd.notna(r.smartId) and pd.notna(r.abbr)}


def coverage(season, data_dir, nonce=0):
    """Per week: scheduled / final / in the data, plus the finals that are missing."""
    sched = schedule_for(season, nonce)
    if not sched or not sched.get("weeks"):
        return None
    have = set(games_in_data(
        data_dir, fingerprint=file_fingerprint(os.path.join(data_dir, "plays_unique.csv")))
        ["uuid"])
    abbr = smart_id_abbr(fingerprint=file_fingerprint(TEAMS_CSV))
    name = lambda g: f"{abbr.get(g['away'], '?')} @ {abbr.get(g['home'], '?')}"

    rows = []
    for w in sched["weeks"]:  # every week, including any the API hasn't filled in
        wk = [g for g in sched["games"] if g["week"] == w["week"]]
        final = [g for g in wk if g["final"]]
        missing = [g for g in final if g["gameId"] not in have]
        rows.append({
            "Week": WEEK_NAMES.get(w["week"], w["slug"] or str(w["week"])),
            "Scheduled": len(wk),
            "Final": len(final),
            "In the data": len(final) - len(missing),
            "Not played yet": len(wk) - len(final),
            "Missing": ", ".join(name(g) for g in missing)
                       or ("— not scheduled yet" if not wk else ""),
        })
    return pd.DataFrame(rows)


def render_coverage(season, data_dir):
    st.subheader("Coverage")
    st.caption("Every preseason game the NFL schedule lists for this season, against "
               "what this app is actually serving. A week is only complete when "
               "**In the data** matches **Final** — games that haven't kicked off yet "
               "aren't missing, they just haven't happened.")
    nonce = st.session_state.get("cov_nonce", 0)
    if st.button("Re-check the schedule",
                 help="Results are cached for ten minutes; this asks again now."):
        st.session_state["cov_nonce"] = nonce + 1
        st.rerun()
    try:
        with st.spinner("Asking pro.nfl.com for the schedule…"):
            table = coverage(season, data_dir, nonce)
    except Exception as e:  # offline, DNS, API shape change - never blocks the page
        st.warning(f"Couldn't reach the schedule endpoint, so coverage is unknown "
                   f"this time ({type(e).__name__}). The data itself is unaffected.")
        return
    if table is None or table.empty:
        st.info(f"The API lists no preseason games for {season} yet.")
        return

    gaps = table[table["In the data"] < table["Final"]]
    total_final, total_have = int(table["Final"].sum()), int(table["In the data"].sum())
    if gaps.empty:
        unplayed = int(table["Not played yet"].sum())
        st.success(f"All {total_final} completed game(s) are in"
                   + (f" — {unplayed} still to be played." if unplayed else "."))
    else:
        st.error(f"{total_final - total_have} completed game(s) missing: "
                 + " · ".join(f"{r.Week} {r.Missing}" for r in gaps.itertuples())
                 + ". Run step 2 below.")
    st.dataframe(table, **WIDE, hide_index=True)


# Where to send someone for a fresh token. Any /api/secured/… request carries
# the same bearer, and this page fires one (secured/stats/team-offense/overview
# /season) as it loads — so there is always a row to copy, instead of clicking
# around pro.nfl.com looking for a page that happens to make a secured call.
# The token is not in any cookie, so DevTools genuinely can't be skipped.
TOKEN_URL = "https://pro.nfl.com/stats/team-offense/season"


def update_headline():
    """(headline, token status, stale?, publish status) — which step is waiting.

    Unpublished data outranks a dead token: it means the hosted app is actively
    showing something older than what's on this machine.
    """
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)
    from fetch_2026 import auth_status

    status = auth_status()
    stale = status["state"] in ("expired", "missing", "unparsed")
    pub = publish_status()
    if pub["state"] in ("dirty", "ahead"):
        head = f"📤 step 3: {pub['short']}"
    elif stale:
        head = "⛔ step 1: new token needed"
    else:
        head = f"✅ ready — token {status['short']}"
    return head, status, stale, pub


def render_update_view(season, data_dir):
    st.title("Update data")
    render_coverage(season, data_dir)
    st.divider()

    if not running_locally():
        st.info("The fetch and publish steps are local-only. This deployment is "
                "public, so a token box on it would be a token box for anyone who "
                "finds the URL — and Streamlit Cloud wipes its filesystem on "
                "restart, so nothing fetched here would survive anyway. Run "
                "`start_app.bat` on your machine to update the data.")
        return

    # imported lazily: fetch_2026 needs `requests`, which the hosted app skips
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)
    from fetch_2026 import check_auth_live, parse_auth_headers, save_auth_text

    head, status, stale, pub = update_headline()
    icon = {"ok": "✅", "unknown": "❔", "expired": "⛔",
            "missing": "⚠️", "unparsed": "⚠️"}[status["state"]]
    st.subheader(f"Update 2026 data — {head}")
    st.caption("Run all three steps here. Nothing you fetch shows up on the "
               "hosted app until step 3 pushes it — Streamlit Cloud reads the "
               "data out of the repo, not off this machine.")

    with st.container(border=True):
        st.markdown(f"**1 · NFL Pro token** — {icon} {status['short']}")
        st.caption(status["message"])

        st.link_button("Open pro.nfl.com stats ↗", TOKEN_URL, **WIDE)
        st.markdown(
            "1. Open that page and log in if it asks. It calls `/api/secured/…` "
            "while it loads, so there's always a live request sitting there to "
            "copy — no hunting for a page that happens to make one.\n"
            "2. **F12** → **Network** tab → type `secured` in the filter box.\n"
            "3. Right-click the top row → **Copy** → **Copy as cURL**.\n"
            "4. Paste it below and hit **Save auth**.")

        # Paste box. The key carries a nonce so saving can blank the widget —
        # Streamlit forbids writing to a widget's state after it is drawn.
        nonce = st.session_state.get("auth_nonce", 0)
        pasted = st.text_area(
            "NFL Pro auth", key=f"auth_paste_{nonce}", height=110,
            placeholder="curl 'https://pro.nfl.com/api/secured/...' -H 'Cookie: ...' …",
            help="Any /api/secured/… request will do — they all carry the same "
                 "bearer token, which is good for about an hour.")

        unsaved = bool(pasted.strip())
        if unsaved:
            st.warning("This paste isn't saved yet — **Save auth** writes it to auth.txt.")

        c1, c2 = st.columns(2)
        if c1.button("Save auth", disabled=not unsaved, **WIDE,
                     type="primary" if unsaved else "secondary"):
            ok, msg = save_auth_text(pasted)
            if ok:
                st.session_state["auth_nonce"] = nonce + 1  # clears the paste box
                st.session_state.pop(f"auth_paste_{nonce}", None)  # drop the token
                st.session_state["auth_saved"] = msg
                st.rerun()
            else:
                st.error(msg)
        # Tests the paste when there is one -- testing the saved file while an
        # unsaved paste sits in the box just reports on the token being replaced.
        if c2.button("Test auth", **WIDE,
                     help="Checks the paste above, or the saved auth.txt if the box is empty"):
            probe = parse_auth_headers(pasted) if unsaved else None
            if unsaved and not probe:
                st.error("No Cookie or Authorization header in that paste — recopy it "
                         "with right-click > Copy > Copy as cURL.")
            else:
                with st.spinner("Asking pro.nfl.com…"):
                    ok, msg = check_auth_live(probe)
                if probe:
                    msg = ("That paste works — hit Save auth to keep it. " if ok
                           else "That paste was rejected: ") + msg
                (st.success if ok else st.error)(msg)
        saved = st.session_state.pop("auth_saved", None)
        if saved:
            st.success(f"Auth saved. {saved}")

    with st.container(border=True):
        st.markdown("**2 · Fetch new games**")
        st.caption("Downloads any completed preseason game you don't have yet into "
                   "games_2026/, then rebuilds data_2026/*.csv. Games already on "
                   "disk are skipped, so re-running is cheap — and the coverage "
                   "table above is how you check it caught everything.")
        if st.button("Fetch + preprocess", disabled=stale, **WIDE,
                     help="Refresh the token first (step 1)" if stale else
                          "Runs fetch_2026.py, then preprocess_2026.py"):
            with st.status("Fetching from pro.nfl.com…", expanded=True) as box:
                if not run_script("fetch_2026.py", progress=True):
                    box.update(label="Fetch failed — see output above", state="error")
                elif not run_script("preprocess_2026.py"):
                    box.update(label="Preprocess failed — see output above", state="error")
                else:
                    box.update(label="Data updated — now publish it (step 3)", state="complete")
                    st.cache_data.clear()

    with st.container(border=True):
        # recomputed: a fetch in this same run may have just changed the answer
        pub = publish_status()
        target = push_target()
        st.markdown(f"**3 · Publish to the hosted app** — {pub['short']}")
        st.caption(pub["message"] + (
            f" Goes to **{target[0]}/{target[1]}** ({target[2]}) — the repo Streamlit "
            "Cloud deploys from." if target else
            " No upstream branch is set, so a push has nowhere to go."))
        if pub["files"]:
            st.code("\n".join(pub["files"][:8]) +
                    (f"\n… and {len(pub['files']) - 8} more" if len(pub["files"]) > 8 else ""))
        done = st.session_state.pop("publish_done", None)
        if done:
            st.success(done)
        can_publish = pub["state"] in ("dirty", "ahead")
        if st.button("Commit + push", disabled=not can_publish, **WIDE,
                     help="Nothing new to publish" if not can_publish else
                          "git add + commit + push origin/main"):
            with st.spinner("Publishing…"):
                ok, msg = publish_data(pub["files"])
            if ok:
                st.session_state["publish_done"] = msg
                st.rerun()
            else:
                st.error(msg)


# ---------- Starter Trends view ----------
TREND_FILES = ["starter_summary.csv", "starter_trends.csv",
               "starter_players.csv", "hc_by_season.csv"]
COACH_ABBR_FIX = {"BLT": "BAL", "CLV": "CLE", "HST": "HOU", "ARZ": "AZ", "LA": "LAR"}


def rebuild_trends():
    return all(run_script(s) for s in ("starter_trends.py", "starter_summary.py"))


def render_starter_trends():
    st.title("Preseason Starter Trends")
    st.caption("How much each team plays its real week-1 starters in preseason — "
               "starters defined as the 22 on the field for the first offensive and "
               "defensive PASS/RUSH snaps of regular-season week 1.")

    missing = [f for f in TREND_FILES if not os.path.exists(os.path.join(APP_DIR, f))]
    if missing:
        st.info(f"Trend data not built yet (missing: {', '.join(missing)}).")
        if st.button("Build starter-trend data"):
            with st.spinner("Running analysis…"):
                if rebuild_trends():
                    st.cache_data.clear()
                    st.rerun()
        return

    fp = "-".join(file_fingerprint(os.path.join(APP_DIR, f)) for f in TREND_FILES)
    summary = load_csv(os.path.join(APP_DIR, "starter_summary.csv"), fingerprint=fp)
    trends = load_csv(os.path.join(APP_DIR, "starter_trends.csv"), fingerprint=fp)
    players = load_csv(os.path.join(APP_DIR, "starter_players.csv"), fingerprint=fp)
    hc = load_csv(os.path.join(APP_DIR, "hc_by_season.csv"), fingerprint=fp)
    hc = hc.assign(team=hc["team"].replace(COACH_ABBR_FIX))
    seasons = sorted(summary["season"].unique())

    with st.sidebar.expander("🔁 Rebuild trend data"):
        st.caption("Re-runs starter_trends.py + starter_summary.py over all "
                   "seasons on disk (2026 joins once its REG wk1 lineups are fetched).")
        if st.button("Rebuild now"):
            with st.spinner("Rebuilding…"):
                if rebuild_trends():
                    st.cache_data.clear()
                    st.rerun()

    tab_lg, tab_yoy, tab_wk, tab_out, tab_team = st.tabs(
        ["League Table", "Year over Year", "Weekly Pattern", "2026 Outlook", "Team Detail"])

    with tab_lg:
        p = summary.pivot_table(index="team", columns="season", values="starter_share")
        latest = summary[summary.season == seasons[-1]].set_index("team")
        tbl = p.round(1).rename(columns=lambda s: f"{s} share %")
        if len(seasons) >= 2:
            tbl["Δ"] = (p[seasons[-1]] - p[seasons[-2]]).round(1)
        tbl[f"{seasons[-1]} coach"] = latest["head_coach"]
        tbl[f"{seasons[-1]} QB1"] = latest["qb1"]
        tbl["QB1 snaps"] = latest["qb1_snaps"].astype("Int64")
        tbl = tbl.sort_values(f"{seasons[-1]} share %", ascending=False)
        st.dataframe(tbl, **WIDE, height=600)
        st.download_button("Download (CSV)", tbl.to_csv().encode("utf-8"),
                           file_name="starter_trends_league.csv", mime="text/csv")

    with tab_yoy:
        if len(seasons) < 2:
            st.info("Need at least two seasons.")
        else:
            s0, s1 = seasons[-2], seasons[-1]
            p = summary.pivot_table(index="team", columns="season", values="starter_share")
            hcp = summary.pivot_table(index="team", columns="season",
                                      values="head_coach", aggfunc="first")
            d = pd.DataFrame({
                "team": p.index,
                "prev": p[s0], "curr": p[s1],
                "coach_prev": hcp[s0], "coach_curr": hcp[s1],
            }).dropna(subset=["prev", "curr"])
            d["Coach"] = (d.coach_prev == d.coach_curr).map(
                {True: "Same coach", False: "New coach"})
            r = d["prev"].corr(d["curr"])
            st.caption(f"Year-over-year correlation: r = {r:.2f}. "
                       "Dashed line = identical behavior both years.")
            base = alt.Chart(d)
            diag = alt.Chart(pd.DataFrame({"v": [0, max(d.prev.max(), d.curr.max())]})
                             ).mark_line(strokeDash=[5, 5], color="gray").encode(
                                 x="v:Q", y="v:Q")
            pts = base.mark_circle(size=110, stroke="white", strokeWidth=1).encode(
                x=alt.X("prev:Q", title=f"{s0} starter share (%)"),
                y=alt.Y("curr:Q", title=f"{s1} starter share (%)"),
                color=alt.Color("Coach:N", scale=alt.Scale(
                    domain=["Same coach", "New coach"], range=["#2a78d6", "#eb6834"])),
                tooltip=["team", alt.Tooltip("prev:Q", title=str(s0)),
                         alt.Tooltip("curr:Q", title=str(s1)),
                         "coach_prev", "coach_curr"])
            labels = base.mark_text(dx=10, align="left", fontWeight="bold").encode(
                x="prev:Q", y="curr:Q", text="team")
            st.altair_chart((diag + pts + labels).properties(height=480), **WIDE)

    with tab_wk:
        wk = (trends.groupby(["season", "week"], as_index=False)
              .agg(share=("starter_snap_share", "mean")))
        wk["Week"] = wk["week"].map({0: "HOF", 1: "Wk 1", 2: "Wk 2", 3: "Wk 3"})
        st.caption("League-average starter snap share by preseason week. The HOF game "
                   "is a rest game; weeks 1–2 carry the starter work; week 3 is the "
                   "roster-battle finale.")
        st.altair_chart(
            alt.Chart(wk).mark_line(point=True).encode(
                x=alt.X("week:O", title="Preseason week",
                        axis=alt.Axis(labelAngle=0,
                                      labelExpr="{'0':'HOF','1':'Wk 1','2':'Wk 2','3':'Wk 3'}[datum.value]")),
                y=alt.Y("share:Q", title="Avg starter share (%)"),
                color=alt.Color("season:N", scale=alt.Scale(range=["#2a78d6", "#eb6834", "#1baf7a"])),
                tooltip=["season", "Week", alt.Tooltip("share:Q", format=".1f")],
            ).properties(height=320), **WIDE)

    with tab_out:
        hist = (summary.groupby("head_coach")[["season", "starter_share"]]
                .apply(lambda d: dict(zip(d["season"], d["starter_share"]))))
        cur = hc[hc.season == hc.season.max()].copy()
        cur["history"] = cur["coach"].map(hist)

        def avg_hist(h):
            return sum(h.values()) / len(h) if isinstance(h, dict) and h else None

        def expectation(h):
            a = avg_hist(h)
            if a is None:
                return "First-year HC"
            if a >= 12:
                return "Plays starters"
            if a <= 4.5:
                return "Sits starters"
            return "Middle of the pack"

        cur["avg"] = cur["history"].map(avg_hist)
        cur["Expectation"] = cur["history"].map(expectation)
        for s in seasons:
            cur[f"{s} %"] = cur["history"].map(
                lambda h, s=s: h.get(s) if isinstance(h, dict) else None)
        cur = cur.sort_values("avg", ascending=False, na_position="last")
        show = cur.rename(columns={"team": "Team", "coach": f"{int(hc.season.max())} coach"})
        cols = ["Team", f"{int(hc.season.max())} coach"] + [f"{s} %" for s in seasons] + ["Expectation"]
        st.caption("Tendency follows the coach, not the franchise — a coach's record "
                   "travels with him to his new team. First-year HCs have no record on "
                   "this measure.")
        st.dataframe(show[cols], **WIDE, height=600, hide_index=True)

    with tab_team:
        teams_avail = sorted(summary["team"].unique())
        team = st.selectbox("Team", teams_avail, index=None, placeholder="Pick a team…")
        if team:
            trends_t = trends.copy()
            trends_t["team"] = trends_t["teamId"].map(team_label)
            players_t = players.copy()
            players_t["team"] = players_t["teamId"].map(team_label)

            tt = trends_t[trends_t.team == team].sort_values(["season", "week"])
            tt["Week"] = tt["week"].map({0: "HOF", 1: "Wk 1", 2: "Wk 2", 3: "Wk 3"})
            show = tt.rename(columns={"unit": "Unit", "unit_snaps": "Unit snaps",
                                      "starters_played": "Starters played (of 11)",
                                      "starter_snap_share": "Starter share %",
                                      "qb1_snaps": "QB1 snaps"})
            st.markdown("**Per-game starter usage**")
            st.dataframe(show[["season", "Week", "Unit", "Unit snaps",
                               "Starters played (of 11)", "Starter share %", "QB1 snaps"]],
                         **WIDE, hide_index=True)

            season_pick = st.radio("Starter detail season", seasons[::-1], horizontal=True)
            det = players_t[(players_t.team == team) & (players_t.season == season_pick)]
            if not det.empty:
                piv = det.pivot_table(index=["unit", "playerName"], columns="week",
                                      values="snaps", aggfunc="sum", fill_value=0)
                piv.columns = [{0: "HOF", 1: "Wk 1", 2: "Wk 2", 3: "Wk 3"}.get(c, c)
                               for c in piv.columns]
                piv["Total"] = piv.sum(axis=1)
                piv = piv.sort_values(["unit", "Total"], ascending=[True, False])
                st.markdown(f"**{season_pick} week-1 starters — preseason snaps by week** "
                            "(0 = never played)")
                st.dataframe(piv, **WIDE, height=500)


# ---------- Team Explorer view ----------
WEEK_NAMES = {0: "HOF", 1: "Wk 1", 2: "Wk 2", 3: "Wk 3"}


def render_team_explorer(data_dir, weeks_selected, season, sit):
    st.title("Team Explorer")
    st.caption("Snap-count depth chart from preseason scrimmage plays. "
               "Lower avg entry snap = on the field earlier = higher on the depth chart.")

    plays_pr, pp_pr, unit_sizes, pp_all = scoped(data_dir, weeks_selected, sit)
    if situation_active(sit):
        st.info("⚑ Situation filter is on — every count below is snaps **in that "
                "situation only**, including the depth chart and drive matrix.")
    if pp_pr.empty:
        st.info("No data with the current week and situation filter.")
        return

    team_ids = sorted(pp_pr["teamId"].unique(), key=team_label)
    team = st.selectbox("Team", team_ids, index=None, format_func=team_label,
                        placeholder="Pick a team…")
    if team is None:
        st.caption("Pick a team to view its snap-count depth chart.")
        return

    # week-1 starters (if the trends pipeline has run for this season)
    starters = set()
    sp_path = os.path.join(APP_DIR, "starter_players.csv")
    if os.path.exists(sp_path):
        sp = load_csv(sp_path, fingerprint=file_fingerprint(sp_path))
        sp = sp[(sp["season"] == season) & (sp["teamId"] == team)]
        starters = set(zip(sp["unit"], sp["playerName"]))

    tp = pp_pr[pp_pr["teamId"] == team]
    tab_off, tab_def, tab_sit = st.tabs(["Offense", "Defense", "Situations"])
    for side, unit_name, tab in (("off", "offense", tab_off), ("def", "defense", tab_def)):
        with tab:
            sd = tp[tp["side"] == side]
            if sd.empty:
                st.info("No snaps.")
                continue
            unit_total = int(unit_sizes[(unit_sizes["teamId"] == team) &
                                        (unit_sizes["side"] == side)]["unitSnaps"].sum())
            st.caption(f"{unit_total} unit scrimmage snaps in the selected weeks.")

            per = (sd.groupby("playerName")
                   .agg(Pos=("position", lambda s: s.mode().iat[0] if not s.mode().empty else "?"),
                        Total=("nflPlayId", "size"))
                   .reset_index())
            entry = (sd.groupby(["playerName", "gameId"])["snapIndex"].min()
                     .groupby("playerName").mean().round(1).rename("Avg entry snap"))
            per = per.merge(entry, on="playerName")
            wk = (sd.groupby(["playerName", "week"])["nflPlayId"].size()
                  .unstack(fill_value=0))
            wk.columns = [WEEK_NAMES.get(c, str(c)) for c in wk.columns]
            per = per.merge(wk, on="playerName", how="left")
            per["% of unit"] = (100 * per["Total"] / max(unit_total, 1)).round(1)
            if starters:
                per["Wk1 starter"] = per["playerName"].map(
                    lambda n: "⭐" if (unit_name, n) in starters else "")
            per = per.sort_values("Total", ascending=False).rename(
                columns={"playerName": "Player"})
            week_cols = [c for c in WEEK_NAMES.values() if c in per.columns]
            cols = (["Player", "Pos"] + (["Wk1 starter"] if starters else [])
                    + week_cols + ["Total", "% of unit", "Avg entry snap"])
            st.dataframe(per[cols], **WIDE, height=560, hide_index=True)
            st.download_button(f"Download {unit_name} (CSV)",
                               per[cols].to_csv(index=False).encode("utf-8"),
                               file_name=f"{team_label(team)}_{unit_name}_{season}.csv",
                               mime="text/csv", key=f"dl_{side}")

            chart = per.head(20).copy()
            chart["Player"] = chart["Player"].astype(str)
            st.altair_chart(
                alt.Chart(chart).mark_bar().encode(
                    x=alt.X("Total:Q", title="Scrimmage snaps"),
                    y=alt.Y("Player:N", sort="-x", title=None),
                    tooltip=["Player", "Pos", "Total", "% of unit", "Avg entry snap"],
                ).properties(height=max(220, 22 * len(chart))), **WIDE)

            # ---- drive-by-drive matrix ----
            st.markdown("**Drive-by-drive**")
            st.caption("Pick a game to see who was on the field for each drive — the "
                       "cleanest rotation view: drive 1 is the first unit, later drives "
                       "are the twos and threes. Numbers are snaps within that drive, "
                       "and drives are this team's own possessions — D1 is its opening "
                       "series, whether or not it had the ball first.")
            label_for = game_labels(pp_pr)
            games_avail = (sd[["gameId", "week"]].drop_duplicates()
                           .sort_values(["week", "gameId"]))
            game_opts = games_avail["gameId"].tolist()
            game_pick = st.selectbox(
                "Game", game_opts, format_func=lambda g: label_for(g, team),
                key=f"drivegame_{side}")
            gd = sd[(sd["gameId"] == game_pick) & sd["teamDrive"].notna()]
            if gd.empty:
                st.info("No drive data for this game.")
            else:
                mat = gd.pivot_table(index="playerName", columns="teamDrive",
                                     values="nflPlayId", aggfunc="count", fill_value=0)
                mat.columns = [f"D{int(c)}" for c in mat.columns]
                drive_len = (gd.drop_duplicates(["nflPlayId"])
                             .groupby("teamDrive")["nflPlayId"].count())
                order = gd.groupby("playerName")["nflPlayId"].count()
                mat = mat.loc[order.sort_values(ascending=False).index]
                pos_map = (gd.groupby("playerName")["position"]
                           .agg(lambda s: s.mode().iat[0] if not s.mode().empty else "?"))
                mat.insert(0, "Pos", mat.index.map(pos_map))
                header = " · ".join(f"D{int(k)}: {v} snaps" for k, v in drive_len.items())
                st.caption(f"Drive lengths — {header}")
                st.dataframe(mat, **WIDE, height=min(560, 40 + 35 * len(mat)))

    # ---- who's on the field by situation ----
    with tab_sit:
        tp_all = pp_all[pp_all["teamId"] == team]
        if not has_situation(tp_all):
            st.info("This season's CSVs have no down & distance yet. Run "
                    "`python backfill_situations.py`, then the preprocess step.")
            return
        st.caption("Did he stay on the field when the situation changed, or did "
                   "somebody else come in for it? The passing-down back, the "
                   "base-defense-only linebacker and the goal-line tight end all show "
                   "up here.")
        if situation_active(sit):
            st.caption("This tab ignores the sidebar situation filter — the whole "
                       "breakdown *is* the point of it.")

        c1, c2 = st.columns([1, 2])
        with c1:
            unit_name = st.radio("Unit", ["Offense", "Defense"], horizontal=True,
                                 key="sit_unit")
        with c2:
            split_name = st.radio("Split by", list(SPLITS), horizontal=True,
                                  key="sit_split")
        side = "off" if unit_name == "Offense" else "def"
        col, order = SPLITS[split_name]
        sd = tp_all[tp_all["side"] == side]
        if sd.empty:
            st.info("No snaps for that unit.")
            return

        unit_snaps = sd.drop_duplicates(["gameId", "nflPlayId"])
        order, never = buckets_faced(unit_snaps, col, order)
        units = sd[["gameId", "nflPlayId", "teamId", "side"]].drop_duplicates()
        qb_plays, qb_counts = qb_options(pp_all, units)
        c3, c4, c5 = st.columns(3)
        with c3:
            qb_pick = st.selectbox(
                "QB on the field", ["Any QB"] + list(qb_counts.index), key="sit_qb",
                # .get, not [] - Streamlit keeps a widget's value across reruns, so
                # the stored QB can outlive the player or team that offered him
                format_func=lambda q: f"{q} ({qb_counts[q]})" if q in qb_counts else q,
                help="The closest thing to 'were the ones out there?' — pick the "
                     "starter and everything below counts only the snaps he was on "
                     "for. On a defense it's the quarterback they faced.")
        with c4:
            window_name = st.radio("Chances counted over", list(WINDOWS),
                                   key="sit_window",
                                   help="What counts as a snap he could have played.")
        with c5:
            mode_name = st.radio("Show", ["Played / chances", "Snaps",
                                          "% of every unit snap"], key="sit_mode")
        window = WINDOWS[window_name]
        mode = {"Played / chances": "chances", "Snaps": "snaps",
                "% of every unit snap": "share"}[mode_name]

        picked = qb_plays[qb_plays["QB"] == qb_pick][["gameId", "nflPlayId"]] \
            if qb_pick != "Any QB" else None
        if picked is not None:
            sd = sd.merge(picked, on=["gameId", "nflPlayId"])
            if sd.empty:
                st.info(f"No {unit_name.lower()} snaps with {qb_pick} on the field.")
                return

        if split_name == "Offense personnel" and side == "def":
            st.caption("Offense personnel on a defensive unit = the grouping they were "
                       "sent out against.")
        elif split_name == "Defense personnel" and side == "off":
            st.caption("Defense personnel on an offensive unit = what the defense "
                       "answered with.")

        unit_by_bucket = (sd.drop_duplicates(["gameId", "nflPlayId"])
                          .groupby(col)["nflPlayId"].size())
        if order:  # keep the zeros visible here too, in the split's own order
            unit_by_bucket = unit_by_bucket.reindex(
                list(order) + [b for b in unit_by_bucket.index if b not in order],
                fill_value=0)
        st.caption(("Unit snaps" if qb_pick == "Any QB" else f"Snaps with {qb_pick} in")
                   + " — " + " · ".join(f"{k}: {v}" for k, v in unit_by_bucket.items()))
        if never:
            st.caption("No column for " + ", ".join(never) + " — this unit never "
                       "faced one in the selected weeks.")
        if mode == "chances":
            st.caption(f"**played / chances**, where a chance is a snap on a "
                       f"{'drive he played in' if window == 'drives' else 'play inside his window'}"
                       ". **0/3** means he was in the game for three of them and off "
                       "the field for all three; **0/0** means it never came up while "
                       "he was in — no evidence either way, which is not the same thing.")

        chances = chances_matrix(pp_all, team, side, col, window, picked) \
            if mode == "chances" else None
        mat = split_table(sd, col, order, unit_by_bucket, mode, chances)
        st.dataframe(mat, **WIDE, height=min(620, 60 + 35 * len(mat)))
        st.download_button("Download split (CSV)",
                           mat.to_csv().encode("utf-8"),
                           file_name=f"{team_label(team)}_{side}_{split_name}_{season}.csv",
                           mime="text/csv", key="dl_sit")


def render_player_situations(pp_all, player_name, sit):
    """One player: what he was on the field for, out of what he could have been."""
    me_all = pp_all[pp_all["playerName"].str.lower() == player_name.lower()]
    if not has_situation(me_all):
        st.info("This season's CSVs have no down & distance yet. Run "
                "`python backfill_situations.py`, then the preprocess step.")
        return
    st.caption("Did he stay on the field when the situation changed? **Chances** "
               "counts the snaps that were his to play — by default every snap of a "
               "drive he took part in. Played **0 of 3** means he was in the game and "
               "somebody else went out there for it; **0 of 0** means it never came "
               "up while he was in, which is no evidence either way.")
    if situation_active(sit):
        st.caption("This tab ignores the sidebar situation filter — the whole "
                   "breakdown *is* the point of it.")

    my_units = me_all[["gameId", "nflPlayId", "teamId", "side"]].drop_duplicates()
    qb_plays, qb_counts = qb_options(pp_all, my_units)
    c1, c2, c3 = st.columns(3)
    with c1:
        split_name = st.selectbox("Split by", list(SPLITS), key="psit_split")
    with c2:
        window_name = st.radio("Chances counted over", list(WINDOWS), key="psit_window")
    with c3:
        qb_pick = st.selectbox(
            "QB on the field", ["Any QB"] + list(qb_counts.index), key="psit_qb",
            # `in` first, not a bare lookup - Streamlit keeps a widget's value across
            # reruns, so the stored QB can outlive the player who offered him
            format_func=lambda q: f"{q} ({qb_counts[q]})" if q in qb_counts else q,
            help="Pick the starter to ask about first-team snaps only. On defensive "
                 "snaps it's the QB he was facing.")
    col, order = SPLITS[split_name]
    window = WINDOWS[window_name]
    picked = qb_plays[qb_plays["QB"] == qb_pick][["gameId", "nflPlayId"]] \
        if qb_pick != "Any QB" else None

    # rows for everything his unit faced, not just what reached his drives -
    # "his unit had three goal-line snaps and he was on for none" is the answer
    unit_snaps = (pp_all.merge(me_all[["gameId", "teamId", "side"]].drop_duplicates(),
                               on=["gameId", "teamId", "side"])
                  .drop_duplicates(["gameId", "nflPlayId", "teamId", "side"]))
    order, never = buckets_faced(unit_snaps, col, order)

    mine = me_all.merge(picked, on=["gameId", "nflPlayId"]) if picked is not None \
        else me_all
    if mine.empty:
        st.info(f"He never took a scrimmage snap with {qb_pick} on the field — which "
                "is its own answer about whose group he was running with.")
        return

    # his chances, summed over every team-unit he appeared on (rarely more than one)
    rows = []
    for (team_id, side), _ in me_all.groupby(["teamId", "side"]):
        mat = chances_matrix(pp_all, team_id, side, col, window, picked)
        if player_name in mat.index:
            rows.append(mat.loc[player_name])
    chances = pd.concat(rows, axis=1).sum(axis=1) if rows else pd.Series(dtype="int64")

    split = pd.concat([mine.groupby(col)["nflPlayId"].size().rename("Played"),
                       chances.rename("Chances")], axis=1).fillna(0).astype(int)
    # same as the team table: buckets he never saw stay as visible 0-of-0 rows
    ordered = list(order) if order else sorted(split.index)
    split = split.reindex(ordered + [b for b in split.index if b not in ordered],
                          fill_value=0)
    split["Share %"] = (100 * split["Played"] /
                        split["Chances"].where(split["Chances"] > 0)).round(1)
    total = int(split["Chances"].sum())
    overall = round(100 * len(mine) / max(total, 1), 1)
    st.caption(f"Overall he played {len(mine)} of the {total} snaps that were his to "
               f"play ({overall}%) — the dashed line below. A blank share means no "
               f"chances came up, which is not a zero."
               + (" No row for " + ", ".join(never) + " — his unit never faced one."
                  if never else ""))

    plot = split.reset_index().rename(columns={col: "Bucket"})
    plot["Bucket"] = plot["Bucket"].astype(str)
    bars = alt.Chart(plot[plot["Chances"] > 0]).mark_bar().encode(
        x=alt.X("Bucket:N", sort=list(plot["Bucket"]), title=None,
                axis=alt.Axis(labelAngle=-30)),
        y=alt.Y("Share %:Q", title="% of his chances he played",
                scale=alt.Scale(domain=[0, 100])),
        tooltip=["Bucket", "Played", "Chances", "Share %"])
    rule = alt.Chart(pd.DataFrame({"y": [overall]})).mark_rule(
        strokeDash=[5, 5], color="#888").encode(y="y:Q")
    st.altair_chart((bars + rule).properties(height=300), **WIDE)
    st.dataframe(split.reset_index().rename(columns={col: split_name}),
                 **WIDE, hide_index=True)


# ---------- Sidebar ----------
view = st.sidebar.radio(
    "View", ["Situation Board", "Player Explorer", "Team Explorer",
             "Starter Trends", "Update data"],
    key="view")  # keyed so a click on the board can switch pages

# The update view is a page of its own, so the sidebar only carries the one line
# that used to be the panel's header: which step is waiting on you, from wherever
# you happen to be standing.
if os.path.exists(os.path.join(APP_DIR, "fetch_2026.py")) and running_locally():
    try:
        st.sidebar.caption(f"Data pipeline — {update_headline()[0]} "
                           "· see **Update data**")
    except Exception:
        pass

if view == "Starter Trends":
    render_starter_trends()
    st.stop()

st.sidebar.header("Data")
folders = discover_data_folders()
if not folders:
    st.error("No data folders found (need a subfolder with plays_unique.csv / play_players.csv).")
    st.stop()
season_label = st.sidebar.selectbox("Season", options=list(folders.keys()))
data_dir = folders[season_label]

plays_df, pp_df = load_all(data_dir)

all_weeks = sorted(w for w in plays_df["week"].dropna().unique().tolist())
weeks_selected = st.sidebar.multiselect("Weeks", options=all_weeks, default=all_weeks)

if st.sidebar.button("🔄 Refresh data (clear cache)"):
    st.cache_data.clear()
    st.rerun()

sit = situation_controls(plays_df)

try:
    season_num = int(season_label[:4])
except ValueError:
    season_num = 0

if view == "Update data":
    render_update_view(season_num, data_dir)
    st.stop()

if view == "Situation Board":
    render_board(data_dir, weeks_selected, season_num, sit)
    st.stop()

if view == "Team Explorer":
    render_team_explorer(data_dir, weeks_selected, season_num, sit)
    st.stop()

# ---------- Main ----------
st.title("Preseason Player Co-Players Explorer")

all_names = sorted(pp_df["playerName"].dropna().unique().tolist())
# a player carried in from the board (or a previous season) may not exist here
if st.session_state.get("player_pick") not in all_names:
    st.session_state.pop("player_pick", None)
player_name = st.selectbox("Player", options=all_names, index=None, key="player_pick",
                           placeholder="Type to search a player…")
if not player_name:
    st.caption("Pick a player to view results.")
    st.stop()

plays_pr, pp_pr, unit_sizes, pp_all = scoped(data_dir, weeks_selected, sit)

me = pp_pr[pp_pr["playerName"].str.lower() == player_name.lower()]
if me.empty:
    st.info("No scrimmage snaps for this player with the current week "
            "and situation filter.")
    st.stop()

# ---------- Header metrics ----------
st.subheader(player_name)
st.caption("Everything on this page counts **scrimmage snaps only** — pass, rush and "
           "sack plays; kickoffs, punts, kneels and nullified penalty plays are "
           "excluded — limited to the weeks selected in the sidebar.")
off_snaps = int((me["side"] == "off").sum())
def_snaps = int((me["side"] == "def").sum())
teams = ", ".join(sorted({team_label(t) for t in me["teamId"].unique()}))
poss = ", ".join(sorted({str(p) for p in me["position"].dropna().unique()}))
m1, m2, m3, m4 = st.columns(4)
m1.metric("Offensive snaps", off_snaps,
          help="Pass/rush plays where he was one of the 11 on offense.")
m2.metric("Defensive snaps", def_snaps,
          help="Pass/rush plays where he was one of the 11 on defense.")
m3.metric("Team", teams or "—")
m4.metric("Position(s)", poss or "—",
          help="Positions the NFL listed him at on those snaps.")

if situation_active(sit):
    st.info("⚑ Situation filter is on — every number on this page counts only his "
            "snaps in that situation.")

tab_cop, tab_start, tab_sit, tab_week, tab_plays = st.tabs(
    ["Co-Players", "Starter Analysis", "Situations", "Weekly Trend", "Plays"])

my_keys = me[["gameId", "nflPlayId", "teamId"]].drop_duplicates()
my_snap_total = len(me[["gameId", "nflPlayId"]].drop_duplicates())

# ---------- Co-Players ----------
with tab_cop:
    st.caption("Teammates who were **on the field at the same time** as him (same team, "
               "same snap). Use it to see which unit he runs with: sharing snaps with "
               "starters means first-team work, sharing with backups means depth work. "
               "**Snaps together** = plays both were on the field · "
               "**% of his snaps** = how often that teammate was out there during HIS "
               "snaps · **% of teammate's snaps** = how often HE was out there during "
               "the teammate's snaps.")
    mates = pp_pr.merge(my_keys, on=["gameId", "nflPlayId", "teamId"])
    mates = mates[mates["playerName"].str.lower() != player_name.lower()]

    cop = (mates.groupby(["playerName", "teamId"], as_index=False)
           .agg(count=("nflPlayId", "size"),
                position=("position", lambda s: s.mode().iat[0] if not s.mode().empty else "Unknown")))
    mate_totals = (pp_pr.groupby(["playerName", "teamId"], as_index=False)
                   .size().rename(columns={"size": "mateSnaps"}))
    cop = cop.merge(mate_totals, on=["playerName", "teamId"], how="left")
    cop["% of his snaps"] = (100 * cop["count"] / max(my_snap_total, 1)).round(1)
    cop["% of teammate's snaps"] = (100 * cop["count"] / cop["mateSnaps"]).round(1)
    cop = cop.sort_values(["count", "playerName"], ascending=[False, True])

    c1, c2 = st.columns([1, 2])
    with c1:
        top_n = st.slider("Top N", 5, 50, 20,
                          help="How many teammates to show, most snaps together first.")
    with c2:
        pos_options = sorted(cop["position"].fillna("Unknown").unique())
        selected_pos = st.multiselect("Filter positions", options=pos_options, default=pos_options,
                                      help="Limit the teammate list to these positions.")
    if selected_pos:
        cop = cop[cop["position"].isin(selected_pos)]

    disp = cop.rename(columns={"playerName": "Teammate", "position": "Position",
                               "count": "Snaps together"}).copy()
    disp["Team"] = disp["teamId"].map(team_label)
    disp = disp[["Teammate", "Position", "Team", "Snaps together",
                 "% of his snaps", "% of teammate's snaps"]]
    st.dataframe(disp.head(top_n), **WIDE, height=420, hide_index=True)
    st.download_button("Download co-player counts (CSV)",
                       disp.to_csv(index=False).encode("utf-8"),
                       file_name=f"{player_name}_coplayers.csv", mime="text/csv")

    chart_df = disp.head(top_n)
    if not chart_df.empty:
        st.altair_chart(
            alt.Chart(chart_df).mark_bar().encode(
                x=alt.X("Snaps together:Q"),
                y=alt.Y("Teammate:N", sort="-x"),
                color=alt.Color("Position:N", legend=alt.Legend(title="Pos")),
                tooltip=["Teammate", "Position", "Snaps together",
                         "% of his snaps", "% of teammate's snaps"],
            ).properties(height=max(220, 22 * len(chart_df))),
            **WIDE)

# ---------- Starter Analysis ----------
with tab_start:
    label_for = game_labels(pp_pr)

    per_game = (me.groupby(["gameId", "teamId", "side"], as_index=False)
                .agg(snaps=("nflPlayId", "nunique"),
                     first=("snapIndex", "min"),
                     last=("snapIndex", "max")))
    per_game = per_game.merge(unit_sizes, on=["gameId", "teamId", "side"], how="left")
    per_game["share %"] = (100 * per_game["snaps"] / per_game["unitSnaps"]).round(1)
    per_game["Game"] = [label_for(g, t) for g, t in zip(per_game["gameId"], per_game["teamId"])]
    per_game["Unit"] = per_game["side"].map({"off": "Offense", "def": "Defense"})

    st.markdown("**Per-game usage**")
    st.caption("One row per game and unit. **Unit snaps** = how many scrimmage snaps his "
               "team's offense (or defense) ran that game · **Snaps / share %** = how many "
               "of those he was on the field for · **Entered / exited on snap #** = when he "
               "came on and left, counted in his unit's snap order — entering on snap 1 "
               "means he was out there with the first team; a high entry number means he "
               "came in after the starters sat.")
    show = per_game.rename(columns={"snaps": "Snaps", "unitSnaps": "Unit snaps",
                                    "first": "Entered on snap #", "last": "Exited on snap #"})
    st.dataframe(show[["Game", "Unit", "Snaps", "Unit snaps", "share %",
                       "Entered on snap #", "Exited on snap #"]],
                 **WIDE, hide_index=True)

    # participation timeline: which of the unit's snaps was he on the field for
    tl = me[["gameId", "teamId", "side", "snapIndex", "week"]].copy()
    tl["Game"] = [label_for(g, t) for g, t in zip(tl["gameId"], tl["teamId"])]
    tl["Unit"] = tl["side"].map({"off": "Offense", "def": "Defense"})
    tl["Row"] = tl["Game"] + "  ·  " + tl["Unit"]
    if not tl.empty:
        st.markdown("**Snap timeline**")
        st.caption("Each tick is one snap he played, laid out left-to-right in the order "
                   "his unit's snaps happened. Ticks bunched at the left = played early "
                   "with the starters, then sat. Ticks on the right = mop-up duty late.")
        row_order = (tl[["Row", "week"]].drop_duplicates()
                     .sort_values(["week", "Row"])["Row"].tolist())
        st.altair_chart(
            alt.Chart(tl).mark_tick(thickness=2.5, size=16).encode(
                x=alt.X("snapIndex:Q", title="Unit snap # in game",
                        axis=alt.Axis(labelAngle=0)),
                y=alt.Y("Row:N", sort=row_order, title=None,
                        axis=alt.Axis(labelLimit=220)),
                color=alt.Color("Unit:N", legend=None),
                tooltip=["Game", "Unit", "snapIndex"],
            ).properties(height={"step": 36}),
            **WIDE)

    # drives he took part in
    if me["teamDrive"].notna().any():
        st.markdown("**Drives played**")
        st.caption("Which possessions he was on the field for, numbered as his own "
                   "unit's series — D1 is the team's opening drive, whether or not it "
                   "had the ball first; drive length = that drive's snaps.")
        drive_lens = (pp_pr.drop_duplicates(["gameId", "nflPlayId", "teamId", "side"])
                      .groupby(["gameId", "teamId", "side", "teamDrive"])["nflPlayId"]
                      .count().rename("Drive length").reset_index())
        dr = (me[me["teamDrive"].notna()]
              .groupby(["gameId", "teamId", "side", "teamDrive"], as_index=False)
              .agg(snaps=("nflPlayId", "nunique")))
        dr = dr.merge(drive_lens, on=["gameId", "teamId", "side", "teamDrive"], how="left")
        dr = dr.sort_values(["gameId", "teamDrive"])
        dr["Game"] = [label_for(g, t) for g, t in zip(dr["gameId"], dr["teamId"])]
        dr["Unit"] = dr["side"].map({"off": "Offense", "def": "Defense"})
        dr["Drive"] = dr["teamDrive"].astype(int).map("D{}".format)
        show_dr = dr.rename(columns={"snaps": "His snaps"})
        st.dataframe(show_dr[["Game", "Unit", "Drive", "His snaps", "Drive length"]],
                     **WIDE, hide_index=True,
                     height=min(420, 40 + 35 * len(show_dr)))

    # QB-anchored usage
    qbs = pp_pr[(pp_pr["side"] == "off") & (pp_pr["position"] == "QB")]
    qbs = qbs[["gameId", "nflPlayId", "teamId", "playerName"]].rename(
        columns={"teamId": "qbTeamId", "playerName": "QB"})
    onfield = me[["gameId", "nflPlayId", "teamId", "side"]].merge(
        qbs, on=["gameId", "nflPlayId"])
    onfield["which"] = onfield.apply(
        lambda r: "Own team QB" if r["teamId"] == r["qbTeamId"] else "Opposing QB", axis=1)
    if not onfield.empty:
        qb_tab = (onfield.groupby(["which", "QB"], as_index=False)
                  .agg(snaps=("nflPlayId", "size"))
                  .sort_values(["which", "snaps"], ascending=[True, False]))
        qb_tab["% of snaps"] = (100 * qb_tab["snaps"] / max(my_snap_total, 1)).round(1)
        st.markdown("**QB on the field during his snaps** — sharing snaps with the "
                    "starting QB (or facing the opponent's starter) marks first-team work")
        st.dataframe(qb_tab.rename(columns={"which": "", "snaps": "Snaps"}),
                     **WIDE, hide_index=True)

# ---------- Situations ----------
with tab_sit:
    render_player_situations(pp_all, player_name, sit)

# ---------- Weekly Trend ----------
with tab_week:
    wk = (me.groupby(["week", "teamId", "side"], as_index=False)
          .agg(snaps=("nflPlayId", "nunique")))
    wk_units = (pp_pr[["gameId", "week", "teamId", "side", "nflPlayId"]]
                .drop_duplicates()
                .groupby(["week", "teamId", "side"], as_index=False)
                .agg(unitSnaps=("nflPlayId", "size")))
    wk = wk.merge(wk_units, on=["week", "teamId", "side"], how="left")
    wk["share %"] = (100 * wk["snaps"] / wk["unitSnaps"]).round(1)
    wk["Unit"] = wk["side"].map({"off": "Offense", "def": "Defense"})

    if wk.empty:
        st.info("No weekly data with current filters.")
    else:
        st.markdown("**Snap share by week**")
        st.caption("Of all the pass/rush snaps his unit played each week, the percentage "
                   "he was on the field for. Rising line across the preseason = climbing "
                   "the depth chart; falling = losing work to others.")
        st.altair_chart(
            alt.Chart(wk).mark_line(point=True).encode(
                x=alt.X("week:O", title="Week",
                        axis=alt.Axis(labelAngle=0,
                                      labelExpr="{'0':'HOF','1':'Wk 1','2':'Wk 2','3':'Wk 3'}[datum.value]")),
                y=alt.Y("share %:Q", title="% of unit snaps"),
                color=alt.Color("Unit:N", legend=alt.Legend(title=None, orient="top")),
                tooltip=["week", "Unit", "snaps", "unitSnaps", "share %"],
            ).properties(height=280),
            **WIDE)
        st.dataframe(wk.rename(columns={"week": "Week", "snaps": "Snaps",
                                        "unitSnaps": "Unit snaps"})
                     [["Week", "Unit", "Snaps", "Unit snaps", "share %"]],
                     **WIDE, hide_index=True)

# ---------- Plays ----------
with tab_plays:
    plays_involving = my_keys.merge(plays_pr, on=["gameId", "nflPlayId"], how="inner")
    plays_involving = plays_involving.sort_values(["gameId", "week", "nflPlayId"],
                                                  na_position="last")
    if plays_involving.empty:
        st.info("No matching PASS/RUSH plays with current filters.")
    else:
        st.caption("Every pass/rush play he was on the field for, with the official play "
                   "description. The link opens the play on NFL Pro.")
        if st.checkbox("Add 'teammates on field' column",
                       help="Adds a column listing the same-team players on the field with him on each play."):
            mates = pp_pr.merge(my_keys, on=["gameId", "nflPlayId", "teamId"])
            mates = mates[mates["playerName"].str.lower() != player_name.lower()].copy()
            mates["who"] = mates["playerName"] + " (" + mates["position"].fillna("?") + ")"
            joined = (mates.sort_values("who")
                      .groupby(["gameId", "nflPlayId"])["who"]
                      .agg(", ".join).rename("Teammates on field"))
            plays_involving = plays_involving.merge(joined, on=["gameId", "nflPlayId"], how="left")

        # his unit's own drive number, matching every other view
        own_drives = (me[["gameId", "nflPlayId", "teamDrive"]].drop_duplicates()
                      if "teamDrive" in me.columns else None)
        if own_drives is not None:
            plays_involving = plays_involving.merge(own_drives,
                                                    on=["gameId", "nflPlayId"], how="left")
            plays_involving["Drive"] = plays_involving["teamDrive"].astype("Int64")
        else:
            plays_involving["Drive"] = plays_involving["driveNum"].astype("Int64")
        show_cols = [c for c in ["gameId", "week", "Drive", "nflPlayId", "nflPlayType",
                                 "Down & distance", "Field zone", "Off personnel",
                                 "Def personnel", "nflPlayDescription",
                                 "Teammates on field", "nflPlayUrl"]
                     if c in plays_involving.columns]
        st.dataframe(plays_involving[show_cols], **WIDE, height=500,
                     hide_index=True,
                     column_config={"nflPlayUrl": st.column_config.LinkColumn(
                         "Play link", display_text="open")})
        st.download_button("Download plays (CSV)",
                           plays_involving[show_cols].to_csv(index=False).encode("utf-8"),
                           file_name=f"{player_name}_plays.csv", mime="text/csv")

st.caption("PASS/RUSH plays only ('PASS' includes any type starting with PASS). "
           "Snap order within a game is the unit's play sequence — in preseason, "
           "earlier snaps ≈ higher on the depth chart.")
