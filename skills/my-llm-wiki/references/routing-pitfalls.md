# Routing pitfalls — real failure modes the auto-classify rule doesn't catch

The SKILL.md §1 rule ("clear best match → route, ambiguous → ask") is sound in
isolation, but **the auto-classify failure happens at the genre/topic seam**:
the source *plausibly* fits a topical/institutional wiki on keywords alone,
but the *genre* says it's a personal/emotional capture. The classifier
default-routes on the topic and gets it wrong.

This file is a catalogue of those traps with the right correction. Read it
once when classifying into ≥2 wikis; the heuristic table near the bottom is the
short version.

## The hybrid trap (the recurring failure)

**Pattern:** source topic is a historical/cultural/institutional subject (侨批,
晚清, 民国, 金融史, 明朝, …) that a topical wiki is built to host. The **body**
is a 影评 / 读后感 / 个人感悟 / 温情散文 / 公众号随笔 — a personal/情感
register, not academic/historical.

**Why the auto-classifier fails:** the keyword surface ("侨批", "南洋华侨",
"家书") scores well against "中国近代史:晚清/民国/社会生活/思想文化" — a
plausible institutional fit. The user, however, almost always sees this kind of
piece as **life/personal** — it's about being moved, not about learning
history. They will say "this belongs in life" after the fact, and you'll be
migrating RAW (delete + re-capture + re-image, with risk of losing the exact
body text).

**Heuristic for the classifier:**

| Wiki surface match | Article genre | Action |
|--------------------|---------------|--------|
| Historical / cultural / institutional wiki | 历史考据 / 学术 / 论文摘要 / 教科书风 | Auto-route to topical wiki |
| Historical / cultural / institutional wiki | 影评 / 读后感 / 个人感悟 / 温情散文 / 公众号随笔 | **Ask.** Lean life. |
| Life / personal wiki | 旅行 / 美食 / 家居 / 健康 / 日常 | Auto-route |
| Life / personal wiki | A historical subject as vehicle (above row) | Ask (same trap from the other side) |
| Tech / programming wiki | Code tutorial / paper / release notes | Auto-route |
| Tech / programming wiki | Personal productivity / career advice | Ask |
| Finance wiki | Market analysis / research / earnings | Auto-route |
| Finance wiki | Personal理财感悟 / 鸡汤 | Ask |

**Why ask even with a "clear" match:** the cost of one `AskUserQuestion` is
small. The cost of mis-routing an immutable RAW file is much larger:
- `rm` the wrong-wiki RAW + asset (breaks any future citation to it).
- Re-fetch the URL (which can return slightly different body text, drift
  timestamp, or fail entirely).
- Re-normalize into the right wiki (slow loop).

If the user has signalled a "只在拿不准时才问我" preference, that explicitly
authorises leaning toward "ask" in hybrid cases — they *want* the question here.

## Worked example (illustrative)

A 公众号 影评 whose *subject* is a historical topic (e.g. 侨批 / 南洋华侨) but
whose *body* is "被剧情和时代情怀狠狠戳中" — pure emotional reflection. The topic
scores well against a 中国近代史 wiki's `description` ("…社会生活 / 思想文化"), so
the auto-classifier routes it there. The user then says it belongs in the **life**
wiki. The fix: ask first. The article *uses* the historical subject as a vehicle
for personal reflection — it is not contributing to that wiki's understanding of
the subject as an institution.

## Other pitfalls (less common, worth knowing)

### "Trending topic" / news cycle mis-routing

A piece about a CEO at a tech company could go to tech wiki or finance wiki.
If the article is about *the company / product / technology*, → tech. If it's
about *market reaction / valuation / investment angle*, → finance. The
heuristic: which wiki's `description` matches the **frame the article uses**,
not the entity it names.

### Tutorial-with-anecdote

A tech tutorial that opens with "I once quit my job and went to Bali…" — the
Bali part isn't the destination; the tech content is. Default to tech; the
travel framing is decorative.

### Cross-cultural recipe

A recipe page from a life blog that includes detailed notes on the author's
Sichuan regional dialect → still life. The linguistic detail is flavor, not a
linguistics paper.

## When to break the heuristic and auto-route anyway

- The user has **explicitly** said "this kind of thing goes to X" before.
- The wiki in question has a very narrow `description` (e.g. "纯历史考据, 不含
  影评") that excludes the article's genre mechanically — the heuristic is
  then unnecessary.
- The article has **both** topical depth AND a personal voice (e.g. a long
  academic-history podcast transcript where the host's anecdotes are
  substantive, not decorative). Use your judgment; the heuristic is a default,
  not a rule.

## What this file is *not*

It's not a per-wiki rule. The classifier still uses each wiki's own
`description` to score. This file just adds a genre filter on top of the
topic filter so the hybrid trap doesn't recur. A future improvement is to
encode the genre heuristic in `scripts/wikis.py` so the agent can ask
mechanically; today it's a manual read for the agent.
