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

The bearer token in that paste is a JWT that lasts about an hour, so it is
usually the thing that's broken. Easiest path is the sidebar's **⬇️ Update 2026
data** panel, whose header reads the token's `exp` claim and shows the time left
("✅ 41m left" / "⛔ expired 1d 2h ago"). Paste a fresh Copy-as-cURL there and hit
Save — it validates before writing, so a junk or already-dead paste can't
overwrite a working `auth.txt`. **Test auth** spends one real request against
the secured endpoint; **Fetch new games** stays disabled until the auth is good.
The panel is hidden when the app runs on Streamlit Cloud (see
`running_locally()`), since that deployment is public.

```
python fetch_2026.py               # preseason game JSONs -> games_2026/
python preprocess_2026.py          # -> data_2026/ CSVs (side-repair included)
python fetch_reg_wk1.py --season N # REG wk1 opening lineups (starter ground truth)
python starter_trends.py           # starter usage per team-week (+ per starter)
python starter_summary.py          # season summaries + coach join
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

Deployed via Streamlit Community Cloud from this repo (main branch,
`preseason_app.py`) — every push redeploys automatically. The in-app fetch
buttons only work locally where `auth.txt` and the Postgres DB exist.
