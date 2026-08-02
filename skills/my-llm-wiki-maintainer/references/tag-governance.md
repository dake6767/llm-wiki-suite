# Tag Governance

Use this reference only for an explicit tag audit, consolidation, cleanup, or
bulk canonicalization request. Routine ingest uses the bounded scoped vocabulary
from `ingest-update.md`; it never loads the full audit.

## Outcome and boundaries

Tags are coarse retrieval facets, not page keywords. Governance reduces lexical
fragmentation without collapsing parent/child concepts or deleting useful
long-tail labels.

- Audit is read-only. A candidate list is not permission to edit pages.
- String similarity only recalls candidates. It never establishes synonymy or
  chooses the canonical spelling.
- Apply **Link, don't tag** before mapping variants. If a label resolves to an
  existing `wiki/entities/` or `wiki/concepts/` page, classify it as `link/drop`;
  do not consolidate it into another tag first.
- `tags-rewrite` owns exact tag-to-tag replacement only. Adding body Wikilinks,
  changing `related:`, promoting a tag onto a second page, and deciding to keep a
  long-tail term remain semantic maintenance work.
- Work in batches of 10–20 mappings or about 50 singleton decisions. Keep the
  audit JSON on disk and read only the current batch plus the 3–5 page excerpts
  needed to judge it.

## 1. Establish the live baseline

Run:

```bash
python3 scripts/wiki_ops.py health <root> --json
python3 scripts/wiki_ops.py tags <root> --audit --json --verbose > /tmp/tag-audit.json
```

`--audit` groups lexical candidates by executability:

1. `formatVariants`: case/space/hyphen/underscore-only equivalents. These are
   high-confidence *lexical* matches, but still need a canonical choice and the
   Link-don't-tag check.
2. `semanticReview`: CJK character-overlap candidates. Judge meaning from pages.
3. `containment`: usually parent/child or related concepts (`检索` / `语义检索`).
   Default to **no action** unless page context establishes true synonymy.

Do not interpret the combined `nearDuplicates` compatibility field as a merge
queue. It remains only for callers that consumed the older JSON shape.

Record these baseline metrics for the run: content/tagged pages, tag references,
distinct/established/singleton tags, pages containing a singleton, pages over
five tags, format variants, untagged pages, and current lint warning/error counts.
Do not create a permanent hand-maintained tag vocabulary or global singleton
baseline; counts remain derived from the pages. The run manifest records the
before/after comparison that matters for this operation.

## 2. Classify before writing

For every candidate choose exactly one action:

- `map`: confirmed same meaning; replace an old spelling/alias with one canonical
  tag.
- `promote`: the label is a useful reusable facet and a second page genuinely
  fits it. This is a semantic page edit, not `tags-rewrite`.
- `link/drop`: the label names an existing or warranted concept/entity page;
  ensure `related:` or a body Wikilink carries the graph edge, then remove the
  tag through normal reviewed page maintenance.
- `keep`: useful long-tail facet with no better link or established tag. Record
  the reason in the governance work notes; do not add fake second-page usage.
- `no action`: parent/child, merely related, or false-positive lexical pair.

Canonical choice order:

1. Meaning and language agree.
2. Existing concept/entity page wins as a link, not as a tag.
3. Proper names/products keep their recognized spelling (`SpaceX`, `Claude Code`).
4. Ordinary terms prefer the form already established across more pages.
5. Frequency is only a tie-breaker. Never lowercase a proper name merely because
   the lowercase form currently has more references.

Cross-language pairs (`LLM` / `大模型`, `memory` / `记忆`) require an LLM semantic
pass over bounded batches. Deterministic code must not carry a universal domain
alias dictionary.

## 3. Plan an exact mapping

Prepare a mapping JSON. `expectedCount` is the number of pages referencing the
source tag (one count per page), and `expectedPages` may use paths with or without
the `wiki/` prefix.

```json
{
  "schemaVersion": 1,
  "root": "/absolute/wiki/root",
  "rules": [
    {
      "from": "AI Workflow",
      "to": "ai-workflow",
      "kind": "format",
      "expectedCount": 1,
      "expectedPages": ["wiki/sources/example.md"],
      "reason": "Confirmed formatting-only variant"
    }
  ]
}
```

`kind` is `format` or `alias`. A `format` rule must be equal after Unicode
normalization, case-folding, and removal of spaces/hyphens/underscores. The
canonical target must already exist unless the reviewed rule explicitly sets
`allowNewCanonical: true`. Planning refuses:

- count/page drift;
- chained or cyclic mappings;
- a target that shadows an existing concept/entity page;
- unresolved format variants that the proposed batch would leave behind;
- empty targets (`link/drop` is outside this deterministic command).

Generate the read-only plan:

```bash
python3 scripts/wiki_ops.py tags-rewrite plan <root> \
  --mapping /tmp/tag-mapping.json --out /tmp/tag-plan.json
```

Review the plan's complete affected-page set, before/after tags, per-page hashes,
and baseline digest. Do not apply until the user confirms this exact plan.

## 4. Apply and verify

After confirmation:

```bash
python3 scripts/wiki_ops.py tags-rewrite apply <root> --plan /tmp/tag-plan.json
```

Apply acquires the per-project tag-rewrite lock and performs a full preflight
before the first write: every page and before-hash must still match. It then backs
up each page under `.llm-wiki/agent/page-history/tag-rewrite-<run-id>/`, writes each page
by same-directory atomic replace, and journals each completed page in
`manifest.json`.

Only the complete `tags` field is rewritten. Inline/block form and the existing
quote convention are preserved; quoted commas remain one tag. Replacement
deduplicates a canonical tag already present on the same page.

Postchecks rerun the full deterministic lint and tag audit. Historical findings
do not make a run fail; the manifest reports `applied-with-findings` only for a
new warning/error or an increased format-variant count. Treat that status as a
completed write needing review/rollback, not as an unmodified failure.

After each semantic `promote`, `link/drop`, or over-limit cleanup batch, rerun the
same audit/lint commands and compare the batch-local before/after metrics. Never
use “established tags must increase” as a universal success metric: correct
Link-don't-tag cleanup can reduce established tags.

## 5. Roll back safely

Use the manifest path returned by apply:

```bash
python3 scripts/wiki_ops.py tags-rewrite rollback <root> \
  --manifest <root>/.llm-wiki/agent/page-history/tag-rewrite-<run-id>/manifest.json
```

Rollback first verifies every current page still matches its recorded after-hash
and every backup matches its before-hash. If a user or another process changed a
page after apply, rollback refuses rather than overwriting that newer work. A
partial apply/rollback remains journaled for deliberate recovery.

## Schema note

Canonical naming guidance ships in the bundled schema for new Wikis. Existing
Wikis can have hand-written schema content: inspect `schema-upgrade --diff` and
obey its local-edit refusal. Merge the new tag paragraph manually when the local
schema exceeds the safety threshold; a backup is not justification for silently
discarding local rules.
