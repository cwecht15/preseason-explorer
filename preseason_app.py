import glob
import os
import subprocess
import sys

import altair as alt
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
    typ = df["nflPlayType"].fillna("").astype(str)
    return df[(typ == "RUSH") | (typ.str.startswith("PASS"))]

def scope_weeks(df, weeks_selected):
    if not weeks_selected:
        return df.iloc[0:0]
    return df[df["week"].isin(weeks_selected)]

@st.cache_data(show_spinner=False)
def build_scoped(data_dir, weeks_key, fingerprint=None):
    """PASS/RUSH-only plays and player-rows for the selected weeks,
    plus each unit's snap-order index within its game."""
    plays, pp = load_all(data_dir)
    weeks = list(weeks_key)
    plays_pr = scope_weeks(pr_filter(plays), weeks).copy()
    pp_pr = scope_weeks(pr_filter(pp), weeks).copy()
    pp_pr = pp_pr.drop_duplicates(["gameId", "nflPlayId", "playerName", "teamId", "side"])

    # snap index: order of a unit's (team+side) PASS/RUSH snaps within a game
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
            if t == "RUSH" or t.startswith("PASS"):
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
    else:
        pp_pr["driveNum"] = pp_pr["driveSnap"] = None
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

# ---------- 2026 update pipeline (local only) ----------
# Fresh data reaches the hosted app only by being committed and pushed: Cloud
# serves the CSVs out of this repo, and its own filesystem is wiped on every
# restart. So the whole loop — paste auth, fetch, publish — runs from a local
# `streamlit run`, and step 3 is what the hosted app actually sees.
PUBLISH_PATHS = ["data_2026", "games_2026", "teams.csv", "hc_by_season.csv",
                 "starter_summary.csv", "starter_trends.csv",
                 "starter_players.csv", "starter_weekly.csv"]


def running_locally():
    """Streamlit Community Cloud serves the repo out of /mount/src. The hosted
    app is public, so the auth box and the fetch buttons stay local-only."""
    here = os.path.abspath(APP_DIR).replace("\\", "/")
    return not here.startswith(("/mount/src", "/app"))


def run_script(script, tail=1500):
    r = subprocess.run([sys.executable, script], cwd=APP_DIR,
                       capture_output=True, text=True)
    st.code((r.stdout + r.stderr)[-tail:] or "(no output)")
    return r.returncode == 0


def git(*args):
    """Run git in APP_DIR -> (ok, combined output)."""
    try:
        r = subprocess.run(["git", *args], cwd=APP_DIR, capture_output=True, text=True)
    except OSError as e:
        return False, f"git isn't available here ({e})"
    return r.returncode == 0, (r.stdout + r.stderr).strip()


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
    ok, out = git("push")
    if not ok:
        low = out.lower()
        if "rejected" in low or "non-fast-forward" in low:
            out += ("\n\nThe remote has commits you don't. Run `git pull --rebase` "
                    "in this folder, then publish again.")
        elif "authentication" in low or "could not read" in low or "denied" in low:
            out += ("\n\nGit couldn't authenticate to GitHub. Push once from a "
                    "terminal to refresh your saved credentials, then try again.")
        return False, f"git push failed:\n{out}"
    return True, "Pushed to origin/main. Streamlit Cloud redeploys on its own, usually within a minute or two."


def render_update_panel():
    # imported lazily: fetch_2026 needs `requests`, which the hosted app skips
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)
    from fetch_2026 import auth_status, check_auth_live, save_auth_text

    status = auth_status()
    icon = {"ok": "✅", "unknown": "❔", "expired": "⛔",
            "missing": "⚠️", "unparsed": "⚠️"}[status["state"]]
    stale = status["state"] in ("expired", "missing", "unparsed")
    pub = publish_status()

    # The header says which step is waiting on you, so the sidebar answers
    # "what do I have to do?" without being opened.
    # Unpublished data outranks a dead token: it means the hosted app is
    # actively showing something older than what's on this machine.
    if pub["state"] in ("dirty", "ahead"):
        head = f"📤 step 3: {pub['short']}"
    elif stale:
        head = f"{icon} step 1: new token needed"
    else:
        head = f"✅ ready — token {status['short']}"

    with st.sidebar.expander(f"⬇️ Update 2026 data — {head}",
                             expanded=stale or pub["state"] == "dirty"):
        st.caption("Run all three steps here. Nothing you fetch shows up on the "
                   "hosted app until step 3 pushes it — Streamlit Cloud reads the "
                   "data out of the repo, not off this machine.")

        st.markdown(f"**1 · NFL Pro token** — {icon} {status['short']}")
        st.caption(status["message"])

        # Paste box. The key carries a nonce so saving can blank the widget —
        # Streamlit forbids writing to a widget's state after it is drawn.
        nonce = st.session_state.get("auth_nonce", 0)
        pasted = st.text_area(
            "NFL Pro auth", key=f"auth_paste_{nonce}", height=110,
            placeholder="curl 'https://pro.nfl.com/api/secured/...' -H 'Cookie: ...' …",
            help="pro.nfl.com logged in → DevTools → Network → click any "
                 "/api/secured/… request → right-click → Copy → Copy as cURL. "
                 "The token is good for about an hour.")

        c1, c2 = st.columns(2)
        if c1.button("Save auth", disabled=not pasted.strip(), **WIDE):
            ok, msg = save_auth_text(pasted)
            if ok:
                st.session_state["auth_nonce"] = nonce + 1  # clears the paste box
                st.session_state.pop(f"auth_paste_{nonce}", None)  # drop the token
                st.session_state["auth_saved"] = msg
                st.rerun()
            else:
                st.error(msg)
        if c2.button("Test auth", **WIDE):
            with st.spinner("Asking pro.nfl.com…"):
                ok, msg = check_auth_live()
            (st.success if ok else st.error)(msg)
        saved = st.session_state.pop("auth_saved", None)
        if saved:
            st.success(f"Auth saved. {saved}")

        st.divider()
        st.markdown("**2 · Fetch new games**")
        st.caption("Downloads any completed preseason game you don't have yet into "
                   "games_2026/, then rebuilds data_2026/*.csv. Games already on "
                   "disk are skipped, so re-running is cheap.")
        if st.button("Fetch + preprocess", disabled=stale, **WIDE,
                     help="Refresh the token first (step 1)" if stale else
                          "Runs fetch_2026.py, then preprocess_2026.py"):
            with st.status("Fetching from pro.nfl.com…", expanded=True) as box:
                if not run_script("fetch_2026.py"):
                    box.update(label="Fetch failed — see output above", state="error")
                elif not run_script("preprocess_2026.py", tail=800):
                    box.update(label="Preprocess failed — see output above", state="error")
                else:
                    box.update(label="Data updated — now publish it (step 3)", state="complete")
                    st.cache_data.clear()

        st.divider()
        # recomputed: a fetch in this same run may have just changed the answer
        pub = publish_status()
        st.markdown(f"**3 · Publish to the hosted app** — {pub['short']}")
        st.caption(pub["message"])
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
    for script in ("starter_trends.py", "starter_summary.py"):
        r = subprocess.run([sys.executable, script], cwd=APP_DIR,
                           capture_output=True, text=True)
        st.code((r.stdout + r.stderr)[-800:] or "(no output)")
        if r.returncode != 0:
            return False
    return True


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


def render_team_explorer(data_dir, weeks_selected, season):
    st.title("Team Explorer")
    st.caption("Snap-count depth chart from preseason PASS/RUSH plays. "
               "Lower avg entry snap = on the field earlier = higher on the depth chart.")

    plays_pr, pp_pr, unit_sizes = build_scoped(
        data_dir, tuple(weeks_selected),
        fingerprint=file_fingerprint(os.path.join(data_dir, "play_players.csv")))
    if pp_pr.empty:
        st.info("No data with the current week filter.")
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
    tab_off, tab_def = st.tabs(["Offense", "Defense"])
    for side, unit_name, tab in (("off", "offense", tab_off), ("def", "defense", tab_def)):
        with tab:
            sd = tp[tp["side"] == side]
            if sd.empty:
                st.info("No snaps.")
                continue
            unit_total = int(unit_sizes[(unit_sizes["teamId"] == team) &
                                        (unit_sizes["side"] == side)]["unitSnaps"].sum())
            st.caption(f"{unit_total} unit PASS/RUSH snaps in the selected weeks.")

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
                    x=alt.X("Total:Q", title="PASS/RUSH snaps"),
                    y=alt.Y("Player:N", sort="-x", title=None),
                    tooltip=["Player", "Pos", "Total", "% of unit", "Avg entry snap"],
                ).properties(height=max(220, 22 * len(chart))), **WIDE)

            # ---- drive-by-drive matrix ----
            st.markdown("**Drive-by-drive**")
            st.caption("Pick a game to see who was on the field for each drive — the "
                       "cleanest rotation view: drive 1 is the first unit, later drives "
                       "are the twos and threes. Numbers are snaps within that drive; "
                       "drive numbers count both teams' possessions in game order.")
            label_for = game_labels(pp_pr)
            games_avail = (sd[["gameId", "week"]].drop_duplicates()
                           .sort_values(["week", "gameId"]))
            game_opts = games_avail["gameId"].tolist()
            game_pick = st.selectbox(
                "Game", game_opts, format_func=lambda g: label_for(g, team),
                key=f"drivegame_{side}")
            gd = sd[(sd["gameId"] == game_pick) & sd["driveNum"].notna()]
            if gd.empty:
                st.info("No drive data for this game.")
            else:
                mat = gd.pivot_table(index="playerName", columns="driveNum",
                                     values="nflPlayId", aggfunc="count", fill_value=0)
                mat.columns = [f"D{int(c)}" for c in mat.columns]
                drive_len = (gd.drop_duplicates(["nflPlayId"])
                             .groupby("driveNum")["nflPlayId"].count())
                order = gd.groupby("playerName")["nflPlayId"].count()
                mat = mat.loc[order.sort_values(ascending=False).index]
                pos_map = (gd.groupby("playerName")["position"]
                           .agg(lambda s: s.mode().iat[0] if not s.mode().empty else "?"))
                mat.insert(0, "Pos", mat.index.map(pos_map))
                header = " · ".join(f"D{int(k)}: {v} snaps" for k, v in drive_len.items())
                st.caption(f"Drive lengths — {header}")
                st.dataframe(mat, **WIDE, height=min(560, 40 + 35 * len(mat)))


# ---------- Sidebar ----------
view = st.sidebar.radio("View", ["Player Explorer", "Team Explorer", "Starter Trends"])
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

# ---------- Sidebar: update pipeline ----------
if running_locally() and os.path.exists(os.path.join(APP_DIR, "fetch_2026.py")):
    render_update_panel()

if view == "Team Explorer":
    try:
        season_num = int(season_label[:4])
    except ValueError:
        season_num = 0
    render_team_explorer(data_dir, weeks_selected, season_num)
    st.stop()

# ---------- Main ----------
st.title("Preseason Player Co-Players Explorer")

all_names = sorted(pp_df["playerName"].dropna().unique().tolist())
player_name = st.selectbox("Player", options=all_names, index=None,
                           placeholder="Type to search a player…")
if not player_name:
    st.caption("Pick a player to view results.")
    st.stop()

plays_pr, pp_pr, unit_sizes = build_scoped(
    data_dir, tuple(weeks_selected),
    fingerprint=file_fingerprint(os.path.join(data_dir, "play_players.csv")))

me = pp_pr[pp_pr["playerName"].str.lower() == player_name.lower()]
if me.empty:
    st.info("No PASS/RUSH snaps for this player with the current week filter.")
    st.stop()

# ---------- Header metrics ----------
st.subheader(player_name)
st.caption("Everything on this page counts **real pass/rush snaps only** (kickoffs, "
           "punts, kneels and nullified penalty plays are excluded), limited to the "
           "weeks selected in the sidebar.")
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

tab_cop, tab_start, tab_week, tab_plays = st.tabs(
    ["Co-Players", "Starter Analysis", "Weekly Trend", "Plays"])

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
    st.caption("One row per game and unit. **Unit snaps** = how many pass/rush snaps his "
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
    if me["driveNum"].notna().any():
        st.markdown("**Drives played**")
        st.caption("Which possessions he was on the field for. Drive numbers count both "
                   "teams' possessions in game order (D1 = the game's first drive); "
                   "drive length = that drive's pass/rush snaps.")
        drive_lens = (pp_pr.drop_duplicates(["gameId", "nflPlayId"])
                      .groupby(["gameId", "driveNum"])["nflPlayId"].count()
                      .rename("Drive length").reset_index())
        dr = (me[me["driveNum"].notna()]
              .groupby(["gameId", "teamId", "side", "driveNum"], as_index=False)
              .agg(snaps=("nflPlayId", "nunique")))
        dr = dr.merge(drive_lens, on=["gameId", "driveNum"], how="left")
        dr = dr.sort_values(["gameId", "driveNum"])
        dr["Game"] = [label_for(g, t) for g, t in zip(dr["gameId"], dr["teamId"])]
        dr["Unit"] = dr["side"].map({"off": "Offense", "def": "Defense"})
        dr["Drive"] = dr["driveNum"].astype(int).map("D{}".format)
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

        plays_involving["Drive"] = plays_involving["driveNum"].astype("Int64")
        show_cols = [c for c in ["gameId", "week", "Drive", "nflPlayId", "nflPlayType",
                                 "nflPlayDescription", "Teammates on field", "nflPlayUrl"]
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
