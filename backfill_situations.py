"""Add down/distance/field-position/score to game JSONs already on disk.

The lineups in game_*.json came from /api/plays/summaryPlay, and that same
response says what situation the play was run in - fetch_2026.py used to keep
the players and drop the rest. This re-reads the endpoint and writes those
fields back into the files in place, so old games carry what new fetches now
keep (see fetch_2026.situation_fields).

No NFL Pro token needed: summaryPlay is public, and every stored play already
holds its own summaryPlay URL. Re-running is cheap - plays that already have
the fields are skipped unless --force.

Usage:
    python backfill_situations.py                  # games_2026/ and ./game_2025*.json
    python backfill_situations.py games_2026       # just one folder
    python backfill_situations.py --workers 6      # requests in flight (default 6)
"""

import argparse
import glob
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from fetch_2026 import get_json, situation_fields

DEFAULT_TARGETS = ["games_2026", "."]
MARKER = "possessionTeamId"  # present == this play has already been backfilled

_local = threading.local()


def session():
    """One requests.Session per worker thread (Sessions aren't thread-safe)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = _local.session = requests.Session()
    return s


def game_files(targets):
    paths = []
    for t in targets:
        if os.path.isdir(t):
            paths += glob.glob(os.path.join(t, "game_*.json"))
        else:
            paths += glob.glob(t)
    return sorted(set(os.path.normpath(p) for p in paths))


def fetch_one(play):
    """-> (play, situation dict or None). Runs on a worker thread."""
    url = play.get("nflPlayUrl")
    if not url:
        return play, None
    try:
        summary = get_json(session(), url)
    except requests.RequestException:
        return play, None
    if not summary:
        return play, None
    return play, situation_fields(summary)


def backfill_game(path, workers, force):
    """-> (filled, failed, skipped) for one game file, saved in place."""
    with open(path, encoding="utf-8") as f:
        game = json.load(f)
    plays = game.get("plays") or []
    todo = [p for p in plays if force or MARKER not in p]
    skipped = len(plays) - len(todo)
    if not todo:
        print(f"{os.path.basename(path)}: {skipped} plays already done, skipping")
        return 0, 0, skipped

    filled = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for play, sit in pool.map(fetch_one, todo):
            if sit is None:
                failed += 1
                continue
            play.update(sit)
            filled += 1

    with open(path, "w", encoding="utf-8") as f:
        json.dump(game, f)
    note = f", {failed} without a summary" if failed else ""
    print(f"{os.path.basename(path)}: {filled} plays filled{note}"
          + (f", {skipped} already done" if skipped else ""))
    return filled, failed, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="*", default=DEFAULT_TARGETS,
                    help="folders or globs of game_*.json (default: games_2026 and .)")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel requests to pro.nfl.com (default 6)")
    ap.add_argument("--force", action="store_true",
                    help="refetch plays that already have the fields")
    args = ap.parse_args()

    paths = game_files(args.targets or DEFAULT_TARGETS)
    if not paths:
        raise SystemExit(f"No game_*.json files in {args.targets}")
    print(f"{len(paths)} game file(s) to backfill")

    totals = [0, 0, 0]
    for i, path in enumerate(paths, start=1):
        print(f"  ({i}/{len(paths)}) {path}", flush=True)
        for slot, value in enumerate(backfill_game(path, args.workers, args.force)):
            totals[slot] += value

    print(f"done: {totals[0]} plays filled, {totals[1]} with no summary, "
          f"{totals[2]} already had the fields")
    print("Now re-run the preprocess step to get the fields into the CSVs.")


if __name__ == "__main__":
    main()
