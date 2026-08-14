"""Fetch 2026 preseason play-by-play + on-field lineups from pro.nfl.com.

Produces games_2026/game_2026NNN.json files in the same shape as the 2025
game_2025NNN.json files (metadata + plays[] with offensePlayers/defensePlayers).

Auth: only the play-list endpoint (/api/secured/plays/playlist/game) needs a
logged-in NFL Pro session. Everything else (schedule, summaryPlay lineups) is
public. To supply auth, open pro.nfl.com in Chrome while logged in, open
DevTools > Network, click any request to pro.nfl.com/api/..., right-click >
Copy > Copy as cURL, and paste the whole thing into a file named auth.txt in
this folder. This script pulls the Cookie and Authorization headers out of it.
(The app's sidebar "Update 2026 data" panel does the same paste with a validity
check; the Authorization bearer is a JWT good for about an hour.)

Usage:
    python fetch_2026.py            # fetch all completed 2026 preseason games
    python fetch_2026.py --week 1   # only preseason week 1 (0 = HOF)
"""

import argparse
import base64
import datetime
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


def check_auth_live(session=None):
    """Actually call the secured endpoint once. -> (ok, message)"""
    auth = load_auth_headers()
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
    args = ap.parse_args()

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

    for n, (week, slug, game) in enumerate(todo, start=1):
        seq_game_id = SEASON * 1000 + n
        out_path = os.path.join(OUT_DIR, f"game_{seq_game_id}.json")
        if os.path.exists(out_path):
            print(f"game_{seq_game_id}.json exists, skipping")
            continue
        print(f"Fetching game {seq_game_id} (week {week}, {game['gameId']}) ...")
        gj = build_game_json(session, auth, seq_game_id, game, week, slug)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(gj, f)
        print(f"  wrote {out_path} ({len(gj['plays'])} plays)")

    print("Done. Now run: python preprocess_2026.py")


if __name__ == "__main__":
    main()
