# Route-regression evals (trigger quality)

A small trigger/route suite for guarding `SKILL.md`'s frontmatter `description`
against regressions when it is edited. Complements `evals/evals.json` (which tests
**output** quality, not routing).

- `trigger_cases.json` — `should_trigger` (the surfaces this skill owns: archive a
  link, sync X bookmarks, file a note, distill a video, init a wiki) + `should_not_trigger`
  (the confusable near-neighbors it must reject: summarize/analyze, search, judge,
  rip a video FILE, convert a PDF just to read, generate the wiki's derived pages,
  sync notes between apps).
- `semantic_config.json` — weighted positive/negative concepts for the semantic scorer.

## Run (compare a candidate description against the current one)

Using the `yao-meta-skill` evaluator:

```bash
python3 <yao-meta-skill>/scripts/trigger_eval.py \
  --description-file <skill>/SKILL.md \
  --baseline-description-file <old SKILL.md> \
  --cases <skill>/evals/route/trigger_cases.json \
  --semantic-config <skill>/evals/route/semantic_config.json \
  --threshold 0.33
```

Read the `comparison` block: `false_positive_delta` / `false_negative_delta` must be
`≤ 0` (no new misfires, no new misses) to promote a description change. Precision
should stay at `1.0` — a false positive means the new wording started catching a
near-neighbor it should reject.

Note: absolute recall reflects this scorer's literal-phrase-overlap design and the
hand-built config, not a hard routing failure — the decision-relevant signal is the
**delta vs the baseline description** across thresholds.
