"""Tunable knobs for the Draft Mantis model. Edit here, then `python3 -m mantis build`."""

# ESPN labels a season by the year it ends: 2027 == 2026-27.
ESPN_SEASON = 2027
ESPN_POOL_LIMIT = 400
SEASON_LABEL = "2026-27"

# Basketball-Reference season end-years used for durability / age / usage history.
BREF_LAST = 2026   # 2025-26
BREF_PRIOR = 2025  # 2024-25

# League defaults (the web app lets you change teams & format live).
TEAMS = 12
ROSTER_SIZE = 13

# How much of the per-game line comes from ESPN's 2026-27 projection vs. last season's actuals.
PROJ_WEIGHT = 0.70
MIN_GP_FOR_ACTUALS = 15
CUR_HALF_GAMES = 15   # in season: current-season stats get 50% weight after this many games, 75% after 45

# Expected games = blend of ESPN projected GP and 2-season games-played history.
GP_PROJ_WEIGHT = 0.6
GP_HISTORY_WEIGHTS = (0.65, 0.35)  # last season, season before
GP_FULL_SEASON = 72                 # games at which a player gets full availability credit
AVAIL_DAMPING = 0.7                 # <1 because missed games can be partly streamed

# Multipliers applied to above-replacement value.
AGE_CURVE = [  # (max_age_inclusive, multiplier)
    (22, 1.04), (24, 1.02), (29, 1.00), (31, 0.98), (33, 0.96), (99, 0.93),
]
# Veterans with no 2025-26 games (returning from major injury): projection-only, so haircut for uncertainty.
COMEBACK_MULT = 0.95
INJURY_MULT = {"OUT": 0.93, "DAY_TO_DAY": 0.99, "SUSPENSION": 0.95}

CATS = ["pts", "reb", "ast", "stl", "blk", "tpm", "fgp", "ftp", "to"]
CAT_LABELS = {
    "pts": "PTS", "reb": "REB", "ast": "AST", "stl": "STL", "blk": "BLK",
    "tpm": "3PM", "fgp": "FG%", "ftp": "FT%", "to": "TO",
}

# ESPN default H2H points scoring.
POINTS_SCORING = {
    "pts": 1, "tpm": 1, "fga": -1, "fgm": 2, "fta": -1, "ftm": 1,
    "reb": 1, "ast": 2, "stl": 4, "blk": 4, "to": -2,
}

# ESPN stat-id -> our key (averageStats block).
ESPN_STAT_IDS = {
    "0": "pts", "1": "blk", "2": "stl", "3": "ast", "6": "reb", "11": "to",
    "13": "fgm", "14": "fga", "15": "ftm", "16": "fta", "17": "tpm", "18": "tpa",
    "40": "min", "42": "gp",
}
ESPN_POSITIONS = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}
ESPN_SLOT_POS = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C"}
