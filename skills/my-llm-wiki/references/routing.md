# Multi-wiki routing & the registry

How the skill sends a capture to the **right** wiki when a user keeps more than
one (a tech wiki, a life wiki, …). The short version lives in SKILL.md §1; this
is the full contract.

The split of labor matches the rest of the skill: **deterministic plumbing in a
script, judgment in the agent.** `scripts/wikis.py` owns the table of wikis and
makes no routing decision; the agent reads it and classifies (it already has the
fetched title + body, so it's the right place for the judgment).

## The registry

A single user-level file — shared by every agent for the same OS user (register
once, both Claude and Hermes route the same way):

```
~/.my-llm-wiki/wikis.json    # override: $LLM_WIKI_REGISTRY
```

```json
{
  "version": 1,
  "wikis": [
    {"path": "/Users/me/llm-wiki",      "name": "技术", "description": "AI / 编程 / 工具 / 技术文章与论文", "default": true},
    {"path": "/Users/me/llm-wiki-renwen", "name": "人文", "description": "影视 / 文学 / 文化评论 / 历史 / 艺术 / 思想 / 人物", "default": false}
  ]
}
```

`description` is the **only** field the classifier reads — it's the one line that
says what belongs in this wiki. Keep each wiki's description distinct and concrete;
overlapping descriptions are what make captures ambiguous. The resolver never opens
each wiki's `purpose.md`, so the registry is the single source of truth for routing.

## Managing it

```bash
# list (the agent reads this to classify); --json for the raw table
python3 <skill>/scripts/wikis.py list

# show the directory selected during initial Setup; new wiki repos go here
python3 <skill>/scripts/wikis.py collection-root

# add / update a wiki (upsert by path); --default marks the fallback
python3 <skill>/scripts/wikis.py register --path ~/llm-wiki --name 技术 \
  --description "AI / 编程 / 工具 / 技术文章与论文" --default

# deregister (leaves the wiki on disk untouched)
python3 <skill>/scripts/wikis.py remove --path ~/llm-wiki-old
```

`init_wiki.py` registers automatically (§0), so you usually don't call `register`
by hand — except to **back-register an existing wiki** that predates the registry,
or to **edit a description**. Re-running `init_wiki.py … --description "…"` does the
same upsert and is safe (project files stay untouched on the idempotent path).

For a new additional wiki, use the canonical initializer's collection-aware
form:

```bash
python3 <skill>/scripts/init_wiki.py --slug history-wiki --name 历史 \
  --description "中国史 / 世界史 / 历史人物 / 制度与事件"
```

`--slug` resolves the collection root from Setup's recorded `wiki_path`, then
falls back to the existing registry default or sole registered wiki. The new
repository is created as a sibling of the initialized wiki. If that root cannot
be resolved, the agent asks for an explicit location and uses `--path`; it never
silently chooses the current working directory.

Behaviors worth knowing:
- The **first** wiki registered becomes the default automatically.
- Re-registering a wiki to edit its description **keeps** its default flag; only
  `--default` on another wiki moves the default.
- `remove`-ing the default promotes the first remaining wiki to default.

## How the agent classifies (SKILL.md §1, expanded)

1. Resolve order first: explicit `--wiki` / "存到X库" wins; then an ambient wiki
   (CWD inside one); only then topic classification.
2. Classification runs **after** the temp fetch (§2) — judge the real **title +
   author + body** against each wiki's `description`, not the URL alone (a tech
   blogger's travel post is a *life* capture; a 小红书 note about Python is *tech*).
3. Confidence rule (the user picked **"拿不准才问我"**):
   - **Clear best match** → route automatically; `--wiki <path>` to
     `normalize_raw.py`; state the choice + reason in one clause.
   - **Ambiguous or no match** → `AskUserQuestion` with the candidates (+ "新建一个"
     when nothing fits). Never coin-flip immutable RAW.

Classification is judgment, not keywords — there is deliberately no rules engine.
If a user later wants a hard override ("this domain always → X"), that's a future
addition; today, an explicit instruction per capture is the override.

## Resolver fallback (`normalize_raw.py`)

When you omit `--wiki`, `resolve_wiki()` falls through, highest priority first:

```
--wiki <path>  >  ambient wiki (CWD walk-up)  >  $LLM_WIKI_DEFAULT
               >  registry default  >  the sole registered wiki  >  error
```

The registry steps only ever target a wiki that **exists on disk** (a registered
path whose folder was moved/deleted is skipped, not written into). So a
single-wiki user needs no flags at all, and a multi-wiki user gets a safe default
when the agent doesn't pass an explicit `--wiki`. Existing `--wiki` / ambient /
`$LLM_WIKI_DEFAULT` behavior is unchanged — the registry is purely additive.
