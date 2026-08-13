# Preseason Explorer

Streamlit app for exploring NFL preseason on-field lineups: who plays with whom,
starter usage by team and coach, snap-order depth charts, and drive-by-drive
rotations. Live at https://preseason-fp.fly.dev.

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

Deploy: `fly deploy` (data CSVs are baked into the image; the in-app fetch
buttons only work locally where `auth.txt` and the Postgres DB exist).
