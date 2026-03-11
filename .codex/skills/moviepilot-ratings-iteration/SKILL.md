---
name: moviepilot-ratings-iteration
description: Workflow for updating MultiRatingsRecommend rating priority, fallback rules, and Douban safety while keeping code, package metadata, and docs in sync.
---

# moviepilot-ratings-iteration

Use this skill when changing rating priority, fallback behavior, or Douban risk controls in this plugin.

## Target repo
- `moviepilot-multiratingsrecommend`

## Core files
- `plugins.v2/multiratingsrecommend/__init__.py`
- `package.v2.json`
- `README.md`

## Required behavior baseline
- Card top-right score: use lower of Douban and TMDB when both exist.
- If Douban and TMDB are both missing, fallback to IMDb.
- If previous three are missing, fallback to Bangumi when the item has Bangumi score.
- Do not write rating hint text into overview.
- Detail page tagline must be ratings only, fixed order: `TMDB / Douban / IMDb / Bangumi`.

## Editing workflow
1. Update scoring logic in `_enrich_media`, `_select_primary_rating`, and rating extraction helpers.
2. Keep display order consistent via `_display_ratings`.
3. Keep plugin UI text and status text aligned with behavior in `get_form` and `get_page`.
4. Update `plugin_version`, `package.v2.json` version/history, and README version line.

## Douban safety checks
- Keep rate limiting and block fuse logic intact.
- Prefer cached Douban rating when available.
- Keep web fallback quota and detail-only controls enabled by default.
- Avoid adding new high-frequency Douban calls in list context without a clear throttle strategy.

## Validation commands
- `rg -n "_select_primary_rating|_display_ratings|_merge_rating_tagline|_strip_rating_overview" plugins.v2/multiratingsrecommend/__init__.py`
- `python3 -m py_compile plugins.v2/multiratingsrecommend/__init__.py`
- `git diff -- plugins.v2/multiratingsrecommend/__init__.py package.v2.json README.md`

## Done criteria
- Scoring behavior matches baseline in this file.
- No old strategy text remains.
- Python file compiles.
