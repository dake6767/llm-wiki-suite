# Project side — making an agent-first project installable from mainland China

The SKILL.md routing table is for the *consumer* seat: someone on a restricted
network installing tools. This file is the *maintainer* seat: you publish a
project (repo + releases + install protocol) and want a mainland-China machine
to be able to install and update it without hand-holding. Four layers, in
order of leverage.

## 1. Repo mirror — GitHub stays the source of truth

Don't move; **mirror**. Keep GitHub canonical (issues/PRs/Actions stay there)
and auto-push a read-only mirror that CN machines clone and pull from.

- **Where:** Gitee (gitee.com) or CNB (cnb.cool). Both are domestic-fast and
  both require real-name verification; Gitee additionally holds new *public*
  repos for manual review before they're visible — budget for that delay.
  Check both platforms' current policies before committing; they shift.
- **How (auto-sync):** don't rely on the platform's "import repo" button (its
  free-tier sync is manually triggered). Push from GitHub Actions instead —
  on every push to the default branch, a job does `git push --mirror` to the
  mirror remote over SSH (deploy key with write access stored as an Actions
  secret; `Yikun/hub-mirror-action` is the ready-made wrapper). Sync lag:
  seconds, zero human steps.
- **Consumer contract:** document the mirror URL next to the GitHub URL in the
  README/install protocol, with the convention: `origin` → whichever host is
  reachable, `upstream` → GitHub. An install protocol that says "updates are
  one `git pull`" is only true in CN if that pull has a domestic remote.

## 2. Release assets — never make install scripts hit api.github.com bare

GitHub Releases are doubly fragile in CN: the API call to *find* the asset
(`api.github.com`) and the CDN download (`objects.githubusercontent.com`) both
fail. Two patterns:

- **Own proxy (best):** a tiny edge worker (e.g. Cloudflare Worker on a custom
  domain) exposing two endpoints — a *manifest* endpoint that proxies/caches
  the release metadata (`…/latest.json`, with asset URLs rewritten to the
  proxy), and a *download* endpoint that streams
  `github.com/<o>/<r>/releases/download/<tag>/<asset>` through. This is the
  pattern behind an auto-updater that works in CN (feed + download both go
  through the worker, GitHub direct as fallback). Note `*.workers.dev` itself
  is blocked in CN — the worker needs a custom domain.
- **Mirror releases (simpler, manual-ish):** attach the same assets to the
  Gitee release (their release attachments serve domestically), and teach the
  install script to try mirror-first.

Either way, the install script's contract is: **probe, then pick** — try the
fast path (GitHub direct) with a short timeout, fall back to the
proxy/mirror. Never hardcode only one.

## 3. Install manifest — the `install` / `install_cn` dual-command convention

Wherever the project lists tool install commands machine-readably (a
`bootstrap.json`, a docs table an agent reads), give each entry two fields:

```json
{
  "name": "yt-dlp",
  "install": "brew install yt-dlp",
  "install_cn": "python3 -m pip install -U yt-dlp -i https://pypi.tuna.tsinghua.edu.cn/simple"
}
```

The `install_cn` variant is not just "same command + mirror flag" — sometimes
it's a **different channel** (yt-dlp above: the pip build self-updates through
the PyPI mirror, while brew/winget builds self-update against GitHub Releases
and stay stale in CN). Doctor/preflight scripts pick the variant by probing
(`net_probe.py --json`, or an inline 10-liner hitting api.github.com with a
3 s timeout — keep scripts dependency-free rather than importing across
skills).

## 4. Dependency placement — prefer ecosystems with domestic CDNs

When the project chooses where its heavyweight dependencies come from, that
choice decides whether CN users need any of the above at all:

- **Models:** ModelScope over Hugging Face for Chinese-audience models —
  FunASR/SenseVoice pull from ModelScope by default, which is why a CN machine
  can hit four environment pitfalls and still download models flawlessly.
  Where only HF hosts it, document `HF_ENDPOINT=https://hf-mirror.com` in the
  install docs rather than leaving users to discover it mid-failure.
- **Python/npm deps:** nothing to do at the project level — consumers mirror
  the index (§SKILL.md). Just don't `pip install git+https://github.com/…` in
  setup paths; that re-couples pip to GitHub reachability.
- **Never default to third-party gh-proxy prefixes in project code or docs**
  — they're user-side last resorts with a trust caveat, not infrastructure.
  If the project needs accelerated GitHub access as a *feature*, that's §2:
  run your own proxy.
