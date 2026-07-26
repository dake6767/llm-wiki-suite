# Purpose-complete Wiki initialization

Use this flow when the user asks to create or initialize a Wiki. A topic
sentence is enough input. The user is choosing a knowledge destination, not
volunteering to become its information architect.

## Product boundaries

Keep the three configuration layers distinct:

| Layer | Decision it controls | Owner |
|---|---|---|
| `wikis.json.description` | Does a captured source belong in this Wiki? | Skill-generated routing scope |
| `purpose.md` | What should ingest notice, preserve, compare, and avoid? | Skill-drafted editorial brief |
| `schema.md` | What is a valid, consistent Wiki page? | Bundled project contract |

Do not ask an ordinary user to write `purpose.md` or customize `schema.md`.
Infer a complete first draft from their topic and intended use. The user can
refine it conversationally later.

`schema.md` is copied from the bundled, project-tested template. Do not generate
a technology/history/life variant at creation time. Leave its domain table
empty until real content reveals stable areas, normally after a dozen-plus
sources. Tags also emerge from the corpus; never ask for a tag taxonomy during
initialization.

## Creation flow

1. List registered Wikis and resolve the initialized collection root.
2. Turn the user's request into one in-memory Wiki brief:
   - `name`: short human-facing label;
   - `slug`: safe sibling directory name;
   - `routing_description`: one concrete sentence describing what belongs;
   - `archetype`: the closest purpose profile below;
   - `goal`: domain plus intended outcome;
   - `key_questions`: 3–5 questions that should guide extraction;
   - `in_scope` and `out_of_scope`;
   - `working_thesis`: a provisional stance, not a fabricated conclusion;
   - `evidence_standard`;
   - `success_criteria`.
3. Continue without a blocking question when a useful, reversible default is
   available. Ask one concise question only when:
   - the proposed routing scope materially overlaps an existing Wiki;
   - the topic could mean two substantially different intended uses (for
     example, medical evidence review versus a personal symptom journal);
   - a requested location cannot be resolved safely.
4. Render the brief into the exact Purpose contract below. Do not leave
   placeholders.
5. Stage it as a UTF-8 Markdown file in a fresh temporary directory using the
   host's ordinary file-writing mechanism.
6. Run the canonical initializer with `--purpose-file`.
7. Verify the created `purpose.md`, the standard `schema.md`, and the exact
   registry entry before reporting success.

## Routing description

The registry description is a routing boundary, not a slogan or research goal.
It is the only per-Wiki text the classifier reads before choosing a destination.

Make it:

- concrete enough to distinguish this Wiki from every registered neighbor;
- one sentence or compact slash-separated list;
- centered on subjects and source kinds likely to appear in captured material;
- broad enough for legitimate adjacent sources, but explicit about the Wiki's
  thematic center.

Good:

```text
宋代政治、制度、财政、军事、社会文化、人物及相关史料研究
```

Too vague:

```text
历史知识
```

Too procedural:

```text
帮助我深入学习并建立第二大脑
```

The routing description and Purpose are generated from the same brief, but they
serve different decisions. Do not copy the description verbatim as the entire
Goal.

## Archetype selection

Choose the closest profile from the user's wording. A Wiki may borrow one or two
rules from another profile, but do not inflate the Purpose with every possible
concern.

### Technical / product

Use for software, AI, engineering, products, standards, implementation, or
technical decision-making.

Questions should cover:

- mechanisms and operating boundaries;
- implementation or deployment choices;
- trade-offs such as quality, latency, cost, complexity, and safety;
- which conclusions depend on versions, hardware, datasets, or environment.

Evidence should distinguish official documentation, source code, reproducible
tests, third-party claims, and inference. Performance and compatibility claims
need version/date/environment context.

### Academic / research

Use for papers, a literature review, a research program, or a question intended
to produce a defensible synthesis.

Questions should cover the central research question, competing methods,
evidence strength, limitations, replication, and open gaps. Evidence rules
should require source linkage and distinguish direct findings from inference.
Success criteria should describe what the Wiki must be able to compare or
support, not how many PDFs it stores.

### History / humanities

Use for history, philosophy, literature, religion, cultural studies, or other
interpretive corpora.

Questions should cover chronology or context, actors and institutions, primary
versus secondary material, competing interpretations, and unresolved disputes.
Evidence rules should record authorship, time, audience, provenance, and
position. Never assume a newer source automatically supersedes an older one;
historical sources are evidence situated in time, not software releases.

### Life / practical decisions

Use for health, exercise, cooking, travel, home, shopping, personal finance,
hobbies, or self-improvement.

Questions should cover who a recommendation fits, conditions, cost, risks,
alternatives, personal results, and when to reassess. Keep the owner's
`source_type: note` experience separate from external evidence. Medical,
financial, legal, and safety-sensitive material needs authoritative, current
sources and a clear boundary against treating the Wiki as professional advice.
Avoid unnecessary sensitive personal data.

### Project / team

Use for a product, organization, client, software project, or team memory.

Questions should cover goals, architecture or operating model, decisions and
trade-offs, current state, ownership, dependencies, risks, and what superseded
what. Evidence should favor primary project artifacts and preserve decision
rationale rather than reconstructing it from memory.

### General

Use only when no stronger profile fits. Focus on understanding the subject,
comparing important viewpoints, tracing evidence, and identifying open
questions. A broad topic still needs explicit boundaries.

## Purpose contract

Generate this exact section structure:

```markdown
# Project Purpose

## Goal

<What this Wiki should understand, decide, or help build, and for what use.>

## Key Questions

1. <Question>
2. <Question>
3. <Question>

## Scope

**In scope:**
- <Concrete inclusion>

**Out of scope:**
- <Concrete exclusion>

## Working Thesis

> <Current provisional stance and its conditions.>

## Evidence Standard

- <How sources and claims should be evaluated in this domain.>

## Success Criteria

- <What useful questions or decisions the mature Wiki should support.>
```

Quality rules:

- Write the Purpose in the user's language unless they requested another.
- Make Goal decision-oriented: “understand,” “compare,” “decide,” or “build,”
  not “collect content about X.”
- Write 3–5 questions by default; the initializer accepts 3–7.
- Give both positive and negative scope. Out-of-scope items prevent page sprawl.
- Never emit `TBD`, empty bullets, blank numbered items, or template comments.
- Do not invent a factual conclusion just to fill Working Thesis. At an early
  stage, use a process-level stance such as “do not assume one cause; establish
  chronology and evidence first.”
- Add only evidence rules that change how ingest should treat this domain.
- Make success criteria observable in use, not corpus-size targets.

## Schema policy

Always let `init_wiki.py` copy its bundled `schema.md`. Do not write a custom
schema file beside the staged Purpose.

At creation time:

- leave the controlled domain table empty;
- do not predeclare tags;
- do not add page types from an archetype;
- express domain-specific evidence and emphasis in `purpose.md`.

After the Wiki has enough real material, the maintainer health flow may propose
3–6 low-cardinality domains. Show each proposed value, its boundary, and a few
example pages before applying it. Do not silently change a mature taxonomy.

## Command and verification

Create a sibling under the initialized collection:

```bash
python3 <skill>/scripts/init_wiki.py --slug <directory-name> \
  --name "<name>" \
  --description "<routing description>" \
  --purpose-file "<temporary complete purpose.md>"
```

For an intentional path override, replace `--slug` with `--path <root>`.
`--purpose-file` validates the complete Purpose before the initializer creates
anything.

Before reporting success:

1. Read the created `purpose.md`; confirm all seven sections are populated and
   no placeholders remain.
2. Confirm `schema.md` matches the bundled standard template and its domain
   table is still empty.
3. Run `wikis.py list --json`; confirm the exact path, name, description, and
   default flag.
4. Summarize “will include,” “will exclude,” and the initial evidence stance in
   plain language. Tell the user the domain taxonomy will be proposed after the
   corpus matures.

## Repositioning an existing Wiki

When the user changes a Wiki's intended scope, treat it as one configuration
operation:

- Changes only to questions, thesis, evidence, or success criteria update
  `purpose.md` but not routing.
- Changes to what belongs in or out update `purpose.md` and the corresponding
  `wikis.json.description` together.
- A name change updates the registry name as well.

Read both current values, propose a coherent diff, preserve unrelated
user-authored Purpose sections, apply only after the requested direction is
clear, then verify both destinations. The registry remains the routing source of
truth; do not add another persistent profile file.
