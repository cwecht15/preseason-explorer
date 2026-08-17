# Preseason Explorer

Streamlit app for exploring NFL preseason on-field lineups: who plays with whom,
starter usage by team and coach, snap-order depth charts, and drive-by-drive
rotations. Hosted on Streamlit Community Cloud.

## Views

- **Situation Board** — the landing page: every skill player in the league in
  one table, with what he was on the field for. See below.
- **Player Explorer** — pick a player: co-players on the field, snap timeline,
  QB-anchored usage, situational splits, weekly snap share, raw play list with
  NFL Pro links.
- **Team Explorer** — snap-count depth chart per unit, week-1 starter flags,
  drive-by-drive rotation matrix, and a Situations tab splitting every player's
  snaps by down & distance, field zone or personnel.
- **Starter Trends** — how much each team/coach plays its real week-1 starters,
  2024–2025, with a 2026 coach-by-coach outlook.
- **Update data** — coverage (is every game for a week actually in?) and the
  three-step fetch/publish pipeline. Coverage works anywhere; the pipeline
  steps are local-only.

### Situation Board

The app opens here, with data already on screen: one row per player, one column
per situation, all 32 teams at once. Offensive skill positions by default;
Team and Position filters sit at the top, and the Unit toggle switches to
defense.

| Column | |
|---|---|
| Snaps | his pass/rush snaps in the selected weeks |
| QB1 | who that starter actually was, across the games he played — "Beck / Brissett" when it changed week to week |
| w/ QB1 | of the snaps that game's **starting** QB was on the field for, how many he was out there for. On defense the pair is `QB1 faced` / `vs QB1` — the starter he lined up against |
| RZ · GL | inside the 20 (goal line included) · inside the 5 or goal-to-go |
| ShortYd · PassDn | 2 or fewer to go · 3rd/4th & 5+ or 2nd & 8+ (both follow the sidebar sliders) |
| 3rdDn · Q4 | third down · fourth quarter, where late duty marks depth |
| Drives · Drives played | how many of his team's drives he was out there for, and which ones — `Wk 1 D2,D4,D6`. Drive numbers count both teams' possessions in game order, so low numbers are the first group |

**Click any row** to open that player's Player Explorer page.

Cells are `played/chances`, the same drive window the Situations tabs use, so
the two reconcile exactly. **The starter is whoever took the unit's first snap
of that game, not whoever took the most** — in preseason those are opposite
people, and it's resolved per game, so a starter who changes week to week is
handled; the QB1 column names him. The denominator counts only games the player
dressed for, so a week-3 call-up isn't marked down for the starter snaps of
weeks he wasn't there. That's why two Cardinals backs can read `7/10` and
`10/49` — different quarterbacks, different games.

Two notes. The board's `RZ` includes goal-line snaps, while the Situations tab's
`Red zone (6-20)` bucket excludes them (those zones are mutually exclusive) — so
the same player can show a larger RZ number here, by design. And because a
`7/9` cell is text, clicking a column header sorts it alphabetically; use the
**Sort by** control, or switch **Show** to Counts, which sorts properly.

### Situations

The sidebar's **Situation** panel filters every view at once — pick a down, a
distance bucket, a field zone or a personnel grouping and the depth chart,
co-player counts, drive matrix and timelines all narrow to those snaps.

Buckets are computed in the app, not baked into the CSVs, so the definitions
are adjustable under *Definitions*: a passing down defaults to 3rd/4th & 5+ or
2nd & 8+, short yardage to 2 or fewer, and those thresholds are sliders.

**Chances, not just snaps.** A raw bucket count can't tell "he came off the
field for it" from "it never came up while he was in", and against a whole
game's denominator a starter who played one drive looks like he skipped
everything. So the Situations tabs count *chances*: the snaps that were his to
play, shown as `played/chances`. `0/3` means the situation came up three times
while he was in the game and somebody else went out there for it; `0/0` means
it never came up, which is no evidence either way.

What counts as a chance is the **drive** by default — every snap of a drive he
took at least one snap on — because that's the unit preseason rotations swap
in. *First to last snap* is the looser alternative, and spans drives he sat out
entirely. Pair either with the **QB on the field** picker, which is the closest
this data gets to "were the ones out there?": choose the starter and both sides
of the ratio count only his snaps. On a defense the picker lists the
quarterbacks it lined up against, which asks the same question.
Personnel is counted off the positions the NFL lists players at, not where they
actually lined up — a fullback counts as an RB, a tackle-eligible package reads
as six linemen — so "11 personnel" here means 1 RB and 1 TE by roster listing.

## Running it locally

Double-click **`start_app.bat`**. It cd's to the repo, installs anything
missing from `requirements.txt`, starts Streamlit and opens the browser; the
console window it leaves behind is the server, so keep it open while you use
the app and close it (or Ctrl+C) to stop. If it fails, the window stays up with
the error rather than blinking shut.

The equivalent by hand is still `streamlit run preseason_app.py` from this
folder.

## Data pipeline

All from the pro.nfl.com API. Only the play-list endpoint needs an NFL Pro
login: paste a Chrome "Copy as cURL" into `auth.txt` (gitignored).

### Coverage — did every game land?

The **Update data** view opens with a per-week table: how many games the NFL
schedule lists, how many are final, and how many of those are in the data the
app is serving. A week is complete when *In the data* equals *Final*; games
that haven't kicked off yet are counted separately rather than as missing, and
a week the schedule hasn't populated yet says so instead of vanishing from the
table. Any gap is named by matchup — *"2 completed game(s) missing: Wk 1 AZ @
LV, CAR @ BUF"* — so you know what to re-fetch.

It matches on the API's game id, which every play in the CSVs carries in its
NFL Pro link, so it checks the data actually being served rather than what
happens to be on disk. Only public endpoints are involved: it works with a dead
token, and on the hosted app, where it answers "did everything get published?".

The same check from a terminal, exit code 1 if anything is missing:

```
python fetch_2026.py --check
```

A normal `fetch_2026.py` run ends with that report too, so a fetch finishes with
a straight answer instead of a scroll of per-game lines.

### Updating the data

Run the local app (`start_app.bat`) and open the **Update data** view. The
sidebar carries a one-line status from wherever you are — `⛔ step 1: new token
needed`, `📤 step 3: 4 file(s) to publish`, or `✅ ready`. All three steps have
to happen on your machine: Streamlit Cloud serves the CSVs committed to this
repo and wipes its own filesystem on restart, so nothing you fetch there would
survive or be visible anyway.

1. **NFL Pro token.** The Authorization bearer is a JWT good for about an hour,
   so it's usually what's broken. The step links straight to
   [pro.nfl.com/stats/team-offense/season](https://pro.nfl.com/stats/team-offense/season),
   which calls `/api/secured/…` as it loads — so there's always a live request
   to copy instead of clicking around looking for a page that makes one. Log
   in if asked, then DevTools → Network → filter on `secured` → right-click the
   top row → Copy → Copy as cURL, paste it in the box, **Save auth**. Any
   secured request works; they all carry the same bearer. The paste is
   validated before it's written, so junk or an already-dead token can't
   clobber a working `auth.txt`. **Test auth** spends one real request for a
   definitive answer.

   The token isn't in any cookie, so the DevTools copy can't be automated away —
   it's a browser-issued JWT that only the site's own scripts ever hold.
2. **Fetch + preprocess.** Runs `fetch_2026.py` then `preprocess_2026.py`.
   Games already in `games_2026/` are skipped, so re-running is cheap.
   Disabled while the token is bad.
3. **Commit + push.** Commits the changed data files and pushes to the
   upstream branch, which is what makes the hosted app redeploy. The step
   names the remote and repo it's about to push to — this repo has a second
   remote (`app` → the legacy `cwecht15/preseason-app`, no longer deployed),
   and a push landing there looks exactly like a redeploy that did nothing.
   Until you publish, the hosted app still shows the old numbers. If the push
   is rejected because the remote moved on, `git pull --rebase` and republish.

The three steps are hidden when the app runs on Streamlit Cloud (see
`running_locally()`), since that deployment is public — a token box on it would
be a token box for anyone who finds the URL. The coverage table above them
stays, because it needs no token and answers a question worth asking there.

Unattended updates (a cron/Action refreshing the data on its own) aren't
possible as things stand: the token dies after an hour, so a stored secret
would always be stale by the next run.

The same pipeline by hand:

```
python fetch_2026.py               # preseason game JSONs -> games_2026/
python fetch_2026.py --check       # which scheduled games are missing (no token needed)
python backfill_situations.py      # down/distance onto game JSONs fetched before it existed
python preprocess_2026.py          # -> data_2026/ CSVs (side-repair included)
python fetch_reg_wk1.py --season N # REG wk1 opening lineups (starter ground truth)
python starter_trends.py           # starter usage per team-week (+ per starter)
python starter_summary.py          # season summaries + coach join
git add data_2026 games_2026 starter_*.csv && git commit -m "data: update" && git push
```

Coach data comes from the local NFL_Data Postgres (`coaching` /
`coaching_current`) via `hc_by_season.csv`.

`teams.csv` (from `pro.nfl.com/api/teams/all`) carries a `smartId` column next
to the short team id: the schedule endpoint identifies teams only by that long
form, and it's what lets a missing game be reported as "AZ @ LV".

Note: the 2024/2025 source JSONs label offense/defense by home/away, not
possession — `preprocess_2026.py` repairs sides, preferring the API's
`possessionTeamId` and falling back to parsing the play description for files
that predate it. Raw 2025 game JSONs (57 MB) are not committed; refetch with
the pipeline, then run `backfill_situations.py` to get down & distance on them.

`summaryPlay` returns the situation (down, distance, field position, score,
EPA) in the same response as the lineups, so `fetch_2026.py` keeps both and new
games cost no extra requests. `backfill_situations.py` re-reads that endpoint
for games already on disk; it needs no NFL Pro token — summaryPlay is public
and every stored play carries its own URL — and skips plays it has already
done, so re-running is cheap.

## Run

```
streamlit run preseason_app.py
```

Deployed via Streamlit Community Cloud from `cwecht15/preseason-explorer`
(branch `main`, `preseason_app.py`) — every push redeploys automatically. The
older `cwecht15/preseason-app` repo is legacy; its history was absorbed here
and it is no longer the deploy source. The update panel (auth / fetch /
publish) is local-only; see "Updating the data" above.
