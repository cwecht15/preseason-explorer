# Preseason Explorer

Streamlit app for exploring NFL preseason on-field lineups: who plays with whom,
starter usage by team and coach, snap-order depth charts, and drive-by-drive
rotations. Hosted on Streamlit Community Cloud.

## Views

- **Player Explorer** — pick a player: co-players on the field, snap timeline,
  QB-anchored usage, weekly snap share, raw play list with NFL Pro links.
- **Team Explorer** — snap-count depth chart per unit, week-1 starter flags,
  drive-by-drive rotation matrix.
- **Starter Trends** — how much each team/coach plays its real week-1 starters,
  2024–2025, with a 2026 coach-by-coach outlook.

## Data pipeline

All from the pro.nfl.com API. Only the play-list endpoint needs an NFL Pro
login: paste a Chrome "Copy as cURL" into `auth.txt` (gitignored).

### Updating the data

Run the local app (`streamlit run preseason_app.py`) and use the sidebar's
**⬇️ Update 2026 data** panel. Its header always names the step that's waiting
on you — `⛔ step 1: new token needed`, `📤 step 3: 4 file(s) to publish`, or
`✅ ready`. All three steps have to happen on your machine: Streamlit Cloud
serves the CSVs committed to this repo and wipes its own filesystem on restart,
so nothing you fetch there would survive or be visible anyway.

1. **NFL Pro token.** The Authorization bearer is a JWT good for about an hour,
   so it's usually what's broken. On pro.nfl.com while logged in: DevTools →
   Network → click any `/api/secured/…` request → right-click → Copy → Copy as
   cURL, paste it in the box, **Save auth**. The paste is validated before it's
   written, so junk or an already-dead token can't clobber a working
   `auth.txt`. **Test auth** spends one real request for a definitive answer.
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

The panel is hidden when the app runs on Streamlit Cloud (see
`running_locally()`), since that deployment is public — a token box on it would
be a token box for anyone who finds the URL.

Unattended updates (a cron/Action refreshing the data on its own) aren't
possible as things stand: the token dies after an hour, so a stored secret
would always be stale by the next run.

The same pipeline by hand:

```
python fetch_2026.py               # preseason game JSONs -> games_2026/
python preprocess_2026.py          # -> data_2026/ CSVs (side-repair included)
python fetch_reg_wk1.py --season N # REG wk1 opening lineups (starter ground truth)
python starter_trends.py           # starter usage per team-week (+ per starter)
python starter_summary.py          # season summaries + coach join
git add data_2026 games_2026 starter_*.csv && git commit -m "data: update" && git push
```

Coach data comes from the local NFL_Data Postgres (`coaching` /
`coaching_current`) via `hc_by_season.csv`.

Note: the 2024/2025 source JSONs label offense/defense by home/away, not
possession — `preprocess_2026.py` repairs sides from the play descriptions.
Raw 2025 game JSONs (57 MB) are not committed; refetch with the pipeline.

## Run

```
streamlit run preseason_app.py
```

Deployed via Streamlit Community Cloud from `cwecht15/preseason-explorer`
(branch `main`, `preseason_app.py`) — every push redeploys automatically. The
older `cwecht15/preseason-app` repo is legacy; its history was absorbed here
and it is no longer the deploy source. The update panel (auth / fetch /
publish) is local-only; see "Updating the data" above.
