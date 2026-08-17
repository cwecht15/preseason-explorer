"""Fetch 2026 preseason play-by-play + on-field lineups from pro.nfl.com.

Produces games_2026/game_2026NNN.json files in the same shape as the 2025
game_2025NNN.json files (metadata + plays[] with offensePlayers/defensePlayers,
plus the down/distance/field-position fields summaryPlay hands back anyway —
see situation_fields; backfill_situations.py adds those to older files).

Auth: only the play-list endpoint (/api/secured/plays/playlist/game) needs a
logged-in NFL Pro session. Everything else (schedule, summaryPlay lineups) is
public. To supply auth, open https://pro.nfl.com/stats/team-offense/season in
Chrome while logged in — that page calls /api/secured/... as it loads, so a
request to copy is always there — then DevTools > Network, filter on "secured",
right-click the top row > Copy > Copy as cURL, and paste the whole thing into a
file named auth.txt in this folder. This script pulls the Cookie and
Authorization headers out of it. Any secured request will do; they all carry
the same bearer, a JWT good for about an hour. (The app's "Update data" view
does the same paste behind a validity check, and links straight to that page.)

Usage:
    python fetch_2026.py            # fetch all completed 2026 preseason games
    python fetch_2026.py --week 1   # only preseason week 1 (0 = HOF)
"""

import argparse
import base64
import csv
import datetime
import glob
import json
import os
import re
import sys
import time

import requests

BASE = "https://pro.nfl.com"
SEASON = 2026
OUT_DIR = "games_2026"
AUTH_FILE = "auth.txt"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
DELAY = 0.15  # seconds between play requests, be polite

# summaryPlay/playlist "play_type_*" strings -> 2025-file style enums
PLAY_TYPE_MAP = {
    "play_type_rush": "RUSH",
    "play_type_pass": "PASS",
    "play_type_sack": "SACK",
    "play_type_interception": "INTERCEPTION",
    "play_type_kickoff": "KICK_OFF",
    "play_type_punt": "PUNT",
    "play_type_field_goal": "FIELD_GOAL",
    "play_type_xp_kick": "XP_KICK",
    "play_type_xp": "XP_KICK",
    "play_type_extra_point": "XP_KICK",
    "play_type_penalty": "PENALTY",
    "play_type_timeout": "TIMEOUT",
    "play_type_game_start": "GAME_START",
    "play_type_end_quarter": "END_QUARTER",
    "play_type_end_game": "END_GAME",
    "play_type_two_point_conversion": "PAT2",
}


def parse_auth_headers(text):
    """Parse Cookie / Authorization headers out of a pasted 'Copy as cURL'."""
    if '^"' in text:  # "Copy as cURL (cmd)" caret-escaping: ^X means literal X
        text = re.sub(r"\^(.)", r"\1", text.replace("^\n", "\n"))
    headers = {}
    # -H 'Name: value' or -H "Name: value"
    for m in re.finditer(r"-H\s+(['\"])(.+?):\s*(.*?)\1", text, re.DOTALL):
        name, value = m.group(2).strip(), m.group(3).strip()
        if name.lower() in ("cookie", "authorization"):
            headers[name.title()] = value
    # -b 'cookie string' (curl cookie flag)
    m = re.search(r"(?:-b|--cookie)\s+(['\"])(.+?)\1", text, re.DOTALL)
    if m and "Cookie" not in headers:
        headers["Cookie"] = m.group(2).strip()
    # also accept plain "Cookie: ..." / "Authorization: ..." lines
    for line in text.splitlines():
        m = re.match(r"\s*(Cookie|Authorization)\s*:\s*(.+)", line, re.IGNORECASE)
        if m and m.group(1).title() not in headers:
            headers[m.group(1).title()] = m.group(2).strip()
    return headers or None


def load_auth_headers():
    """Cookie / Authorization headers from auth.txt, or None."""
    if not os.path.exists(AUTH_FILE):
        return None
    return parse_auth_headers(open(AUTH_FILE, encoding="utf-8", errors="replace").read())


# ---------- Auth freshness ----------
# The Cookie jar outlives the session, but the Authorization bearer is a JWT
# that pro.nfl.com issues with roughly a one-hour life. Reading its `exp` claim
# tells us the token is dead without spending a request to find out.

def jwt_expiry(token):
    """Unix `exp` out of a JWT payload, or None if it isn't a readable JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def format_duration(seconds):
    seconds = int(abs(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def auth_status(headers=None):
    """State of the stored auth: dict(state, message, short, seconds_left).

    state is one of: missing, unparsed, expired, ok, unknown (no readable JWT,
    so freshness can only be settled by actually calling the API).
    """
    if headers is None:
        if not os.path.exists(AUTH_FILE):
            return {"state": "missing", "seconds_left": None,
                    "short": "no auth.txt",
                    "message": f"No {AUTH_FILE} yet - paste a Copy-as-cURL to create it."}
        headers = load_auth_headers()
    if not headers:
        return {"state": "unparsed", "seconds_left": None,
                "short": "unreadable",
                "message": f"{AUTH_FILE} has no Cookie or Authorization header in it."}

    bearer = (headers.get("Authorization") or "").split()
    exp = jwt_expiry(bearer[-1]) if bearer else None
    if exp is None:
        return {"state": "unknown", "seconds_left": None,
                "short": "age unknown",
                "message": "Auth loaded, but no readable token expiry - test it to be sure."}

    left = exp - datetime.datetime.now(datetime.timezone.utc).timestamp()
    when = datetime.datetime.fromtimestamp(exp).strftime("%a %H:%M")
    if left <= 0:
        return {"state": "expired", "seconds_left": left,
                "short": f"expired {format_duration(left)} ago",
                "message": f"Token expired {format_duration(left)} ago (at {when}). Paste a fresh Copy-as-cURL."}
    return {"state": "ok", "seconds_left": left,
            "short": f"{format_duration(left)} left",
            "message": f"Token valid for another {format_duration(left)} (until {when})."}


def save_auth_text(text):
    """Validate a pasted Copy-as-cURL and write it to auth.txt. -> (ok, message)"""
    headers = parse_auth_headers(text or "")
    if not headers:
        return False, ("Couldn't find a Cookie or Authorization header in that paste. "
                       "Use right-click > Copy > Copy as cURL on a pro.nfl.com/api/... request.")
    if "Authorization" not in headers:
        return False, ("Found a Cookie but no Authorization header - copy a request to "
                       "/api/secured/... , the public endpoints don't carry the token.")
    status = auth_status(headers)
    if status["state"] == "expired":  # refuse before writing, so a stale paste
        return False, ("That token is already " + status["short"]  # can't clobber
                       + " - copy a newer request.")               # a live one
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    return True, status["message"]


def check_auth_live(headers=None, session=None):
    """Actually call the secured endpoint once. -> (ok, message)

    Pass headers to test a paste that hasn't been saved yet; omit to test
    whatever is currently in auth.txt.
    """
    auth = headers or load_auth_headers()
    if not auth:
        return False, auth_status()["message"]
    session = session or requests.Session()
    try:
        data = get_json(session, f"{BASE}/api/scores/live/games",
                        params={"season": SEASON, "seasonType": "PRE", "week": 1})
        games = (data or {}).get("games", [])
        done = [g for g in games if g.get("gameState") == "POST" or g.get("phase") == "FINAL"]
        if not done:
            return False, "No completed game available to test against."
        h = dict(HEADERS)
        h.update(auth)
        r = session.get(f"{BASE}/api/secured/plays/playlist/game",
                        params={"gameId": done[0]["gameId"]}, headers=h, timeout=30)
    except requests.RequestException as e:
        return False, f"Couldn't reach pro.nfl.com: {e}"
    if r.status_code == 200:
        return True, "pro.nfl.com accepted the token - ready to fetch."
    if r.status_code in (401, 403):
        return False, f"pro.nfl.com rejected the token ({r.status_code}) - paste a fresh Copy-as-cURL."
    return False, f"Unexpected response from pro.nfl.com: {r.status_code}."


def get_json(session, url, params=None, extra_headers=None, retries=3):
    h = dict(HEADERS)
    if extra_headers:
        h.update(extra_headers)
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, headers=h, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403):
                raise PermissionError(f"{r.status_code} on {url}")
            if r.status_code >= 500:
                return None  # e.g. summaryPlay for a nonexistent playId
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
        time.sleep(1.0 * (attempt + 1))
    return None


def normalize_play_type(raw, description=""):
    if not raw:
        raw = ""
    raw = str(raw).strip()
    if raw in PLAY_TYPE_MAP:
        return PLAY_TYPE_MAP[raw]
    if raw.lower() in PLAY_TYPE_MAP:
        return PLAY_TYPE_MAP[raw.lower()]
    if raw.isupper():  # already 2025-style (RUSH, PASS, ...)
        return raw
    d = (description or "").strip().upper()
    if raw in ("play_type_unknown", ""):
        if d == "GAME":
            return "GAME_START"
        if "END QUARTER" in d or d.startswith("END OF QUARTER"):
            return "END_QUARTER"
        if "END GAME" in d or "END OF GAME" in d:
            return "END_GAME"
        if d.startswith("TIMEOUT") or "TWO-MINUTE WARNING" in d:
            return "TIMEOUT"
        if "NO PLAY" in d:  # nullified by penalty (matches 2025 vocabulary)
            return "PENALTY"
        return "UNSPECIFIED"
    return raw or "UNKNOWN"


def scheduled_games(season=SEASON, session=None):
    """What the API says the preseason is: {"weeks": [...], "games": [...]}.

    The week list comes back separately from the games because a week the
    schedule knows about but hasn't populated yet ("no week 2 games listed")
    is a different answer from a week with games missing, and a row that
    simply isn't there can't say either.

    Public endpoints only — no NFL Pro token — so the app can check its own
    coverage anywhere, including the hosted deployment where the fetch buttons
    are hidden. Team ids here are the long "smartId" form, which is what the
    game files store as away/homeTeamId.
    """
    session = session or requests.Session()
    weeks = get_json(session, f"{BASE}/api/schedules/weeks", params={"season": season})
    pre = [w for w in (weeks or {}).get("weeks", []) if w.get("seasonType") == "PRE"]
    games = []
    for w in pre:
        data = get_json(session, f"{BASE}/api/scores/live/games",
                        params={"season": season, "seasonType": "PRE", "week": w["week"]})
        for g in (data or {}).get("games", []):
            games.append({
                "week": w["week"],
                "weekSlug": w.get("weekSlug", ""),
                "gameId": g.get("gameId"),
                "away": (g.get("awayTeam") or {}).get("teamId"),
                "home": (g.get("homeTeam") or {}).get("teamId"),
                "final": g.get("gameState") == "POST" or g.get("phase") == "FINAL",
                "status": g.get("displayStatus") or "",
                "startTime": g.get("startTime") or "",
            })
    return {"weeks": [{"week": w["week"], "slug": w.get("weekSlug", "")} for w in pre],
            "games": games}


def games_on_disk(out_dir=OUT_DIR):
    """fapiGameId -> filename, for everything already fetched."""
    have = {}
    for path in sorted(glob.glob(os.path.join(out_dir, "game_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                have[json.load(f).get("fapiGameId")] = os.path.basename(path)
        except (OSError, ValueError):
            continue
    return have


def team_abbrs(path=None):
    """smartId -> abbr, out of teams.csv, for naming games in the report."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "teams.csv")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {r["smartId"]: r["abbr"] for r in rows if r.get("smartId")}


def coverage_report(season=SEASON, session=None, out_dir=OUT_DIR):
    """Print per-week 'N of M final games on disk'. -> count still missing.

    Printed at the end of a fetch so the run finishes with a straight answer
    about whether a week is complete, rather than leaving it to be inferred
    from a scroll of per-game lines.
    """
    sched = scheduled_games(season, session)
    have, abbr = games_on_disk(out_dir), team_abbrs()
    missing_total = 0
    print(f"coverage for {season} preseason ({out_dir}/):")
    for w in sched["weeks"]:
        wk = [g for g in sched["games"] if g["week"] == w["week"]]
        final = [g for g in wk if g["final"]]
        missing = [g for g in final if g["gameId"] not in have]
        missing_total += len(missing)
        if not wk:
            print(f"  {w['slug']:>4}: no games listed yet")
            continue
        note = f", {len(wk) - len(final)} not played yet" if len(wk) > len(final) else ""
        print(f"  {w['slug']:>4}: {len(final) - len(missing)} of {len(final)} "
              f"final game(s) on disk{note}"
              + ("" if not missing else "  MISSING " + ", ".join(
                  f"{abbr.get(g['away'], '?')}@{abbr.get(g['home'], '?')}"
                  for g in missing)))
    print("all completed games are on disk" if not missing_total
          else f"{missing_total} completed game(s) still missing")
    return missing_total


def fetch_playlist(session, auth, game_uuid):
    """Return the ordered play list for a game (requires NFL Pro auth)."""
    url = f"{BASE}/api/secured/plays/playlist/game"
    data = get_json(session, url, params={"gameId": game_uuid}, extra_headers=auth)
    if not data or not data.get("plays"):
        return None
    return data["plays"]


def fetch_summary_play(session, game_uuid, play_id):
    url = f"{BASE}/api/plays/summaryPlay"
    return get_json(session, url, params={"gameId": game_uuid, "playId": play_id})


def player_dicts(summary):
    """Split summaryPlay home/away arrays into offense/defense lists."""
    home = summary.get("home") or []
    away = summary.get("away") or []
    if summary.get("homeIsOffense"):
        return home, away
    return away, home


# The same summaryPlay response that carries the lineups also says what
# situation the play was run in. Keeping it is free (no extra request) and is
# what lets the app split snaps by down, distance and field position.
SITUATION_KEYS = ("quarter", "down", "yardsToGo", "gameClock",
                  "yardlineSide", "yardlineNumber", "isGoalToGo",
                  "isRedzonePlay", "isSTPlay", "isNoPlay",
                  "possessionTeamId", "expectedPoints", "expectedPointsAdded")


def situation_fields(summary):
    """Down/distance/field-position/score fields out of a summaryPlay response.

    Scores come back home/visitor; they're stored offense/defense-relative so
    downstream code doesn't have to know which team was home.
    """
    sp = (summary or {}).get("play") or {}
    out = {k: sp.get(k) for k in SITUATION_KEYS}
    home, visitor = sp.get("preSnapHomeScore"), sp.get("preSnapVisitorScore")
    off, dfn = (home, visitor) if summary.get("homeIsOffense") else (visitor, home)
    out["offScore"], out["defScore"] = off, dfn
    return out


def build_game_json(session, auth, seq_game_id, game, week, week_slug):
    game_uuid = game["gameId"]
    plays_raw = fetch_playlist(session, auth, game_uuid)
    if plays_raw is None:
        raise RuntimeError(f"No playlist for {game_uuid} (auth problem?)")

    plays_out = []
    ngs_game_id = None
    for i, pl in enumerate(plays_raw):
        play_id = pl.get("playId") or pl.get("sequence")
        if play_id is None:
            continue
        summary = fetch_summary_play(session, game_uuid, play_id)
        time.sleep(DELAY)
        if summary is None:
            print(f"    ! playId {play_id}: no summary, keeping metadata only")
            summary = {}
        sp = summary.get("play") or {}
        if ngs_game_id is None:
            ngs_game_id = summary.get("gameId")
        offense, defense = player_dicts(summary)
        desc = pl.get("playDescription") or sp.get("playDescription") or ""
        raw_type = pl.get("playType") or sp.get("playType") or ""
        plays_out.append({
            "nflPlayId": play_id,
            "nflPlayType": normalize_play_type(raw_type, desc),
            "nflPlayDescription": desc,
            "nflPlayFullDescription": sp.get("playDescriptionWithJerseyNumbers") or desc,
            "nflPlayUrl": f"{BASE}/api/plays/summaryPlay?gameId={game_uuid}&playId={play_id}",
            **situation_fields(summary),
            "offensePlayers": offense,
            "defensePlayers": defense,
        })
        if (i + 1) % 25 == 0:
            print(f"    {i + 1}/{len(plays_raw)} plays")

    return {
        "gameId": seq_game_id,
        "seasonKey": f"{SEASON}PRE",
        "season": SEASON,
        "seasonType": "PRE",
        "weekId": week_slug,
        "week": week,
        "status": game.get("displayStatus", ""),
        "fapiGameId": game_uuid,
        "ngsGameId": ngs_game_id,
        "awayTeamId": (game.get("awayTeam") or {}).get("teamId"),
        "homeTeamId": (game.get("homeTeam") or {}).get("teamId"),
        "startDate": game.get("startTime", ""),
        "plays": plays_out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, default=None, help="preseason week (0=HOF)")
    ap.add_argument("--check", action="store_true",
                    help="only report which scheduled games are on disk, fetch nothing")
    args = ap.parse_args()

    if args.check:  # public endpoints only, so this works with a dead token
        sys.exit(1 if coverage_report() else 0)

    status = auth_status()
    print(f"auth: {status['message']}")
    if status["state"] not in ("ok", "unknown"):
        print("The play-list endpoint needs your NFL Pro login. See the")
        print("docstring at the top of this script for how to create auth.txt.")
        sys.exit(1)
    auth = load_auth_headers()

    os.makedirs(OUT_DIR, exist_ok=True)
    session = requests.Session()

    weeks = get_json(session, f"{BASE}/api/schedules/weeks", params={"season": SEASON})
    pre_weeks = [w for w in weeks["weeks"] if w["seasonType"] == "PRE"]
    if args.week is not None:
        pre_weeks = [w for w in pre_weeks if w["week"] == args.week]

    # Collect completed games across weeks, in (week, startTime) order
    todo = []
    for w in pre_weeks:
        data = get_json(session, f"{BASE}/api/scores/live/games",
                        params={"season": SEASON, "seasonType": "PRE", "week": w["week"]})
        games = (data or {}).get("games", [])
        finals = [g for g in games if g.get("gameState") == "POST" or g.get("phase") == "FINAL"]
        finals.sort(key=lambda g: g.get("startTime", ""))
        for g in finals:
            todo.append((w["week"], w["weekSlug"], g))
        print(f"Week {w['week']} ({w['weekSlug']}): {len(finals)} completed of {len(games)} listed")

    # Stable sequential ids: sort ALL completed preseason games by week+time.
    # NOTE: ids are assigned in this order, so re-runs after more games finish
    # keep earlier ids stable (new games always sort later).
    todo.sort(key=lambda t: (t[0], t[2].get("startTime", "")))

    plan = [(SEASON * 1000 + n, week, slug, game)
            for n, (week, slug, game) in enumerate(todo, start=1)]
    todo_now = [p for p in plan
                if not os.path.exists(os.path.join(OUT_DIR, f"game_{p[0]}.json"))]
    # counted up front so a caller can turn this into a progress bar
    print(f"{len(todo_now)} game(s) to fetch, {len(plan) - len(todo_now)} already on disk")

    for seq_game_id, week, slug, game in plan:
        out_path = os.path.join(OUT_DIR, f"game_{seq_game_id}.json")
        if os.path.exists(out_path):
            print(f"game_{seq_game_id}.json exists, skipping")
            continue
        print(f"Fetching game {seq_game_id} (week {week}, {game['gameId']}) ...")
        gj = build_game_json(session, auth, seq_game_id, game, week, slug)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(gj, f)
        print(f"  wrote {out_path} ({len(gj['plays'])} plays)")

    coverage_report(session=session)
    print("Done. Now run: python preprocess_2026.py")


if __name__ == "__main__":
    main()
