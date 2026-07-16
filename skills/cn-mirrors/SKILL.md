---
name: cn-mirrors
description: >-
  Adapt installs and downloads to the machine's network environment — mainland-
  China networks above all. Probe which dev hosts are actually reachable
  (scripts/net_probe.py), then route the failing ecosystem through its official
  domestic mirror: pip → TUNA, npm → npmmirror, Hugging Face → hf-mirror via
  HF_ENDPOINT, Homebrew → TUNA bottles, GitHub clone/releases → the project's
  Gitee/CNB mirror or an accelerator prefix. Use whenever an install or
  download times out, crawls, or errors against github.com / pypi.org /
  registry.npmjs.org / huggingface.co / brew — "pip 装不上", "npm 超时",
  "clone 卡住", "模型下载失败", "GitHub 打不开", "下载太慢", "换镜像", "加速",
  "国内网络 / 大陆网络装不了" — or BEFORE installing a toolchain on a machine
  that may sit behind such a network (probe first, pick commands second). Also
  carries the PROJECT-maintainer guide (references/project-side.md) for making
  your own agent-first project installable from mainland China: Gitee/CNB
  auto-sync mirrors, release proxying, and structured per-ecosystem routes. Prefers
  session-scoped flags over global config rewrites; official public mirrors
  only — this is NOT a VPN / proxy / circumvention tool, and it never routes
  credentialed operations (git push, publish, login) through third parties.
---

# cn-mirrors — network-environment adaptation for installs & downloads

On a mainland-China network, the default install path of almost every dev
ecosystem points at a host that is blocked or throttled: GitHub (clone,
releases, raw), PyPI CDN, the npm registry, Hugging Face. The fix is never a
workaround or a tunnel — each ecosystem has an **official, public domestic
mirror**; the whole job is (1) confirm which hosts are actually unreachable
*right now*, (2) put the mirror flag on the failing command, (3) re-run and
verify. This file is that playbook. It is deliberately self-contained: copy it
into any project and it still works.

Two hard principles:

- **Session-scoped first.** Use per-command flags / env vars (`-i`,
  `--registry`, `HF_ENDPOINT=…`). Rewriting the user's global `pip.conf` /
  `.npmrc` / shell profile is a real config change — offer the command, let
  the user run it. Never do it silently.
- **Mirrors, not tunnels.** Everything below is a public mirror service
  (TUNA/USTC/Aliyun, npmmirror, hf-mirror, ModelScope). Do not route
  credentialed operations — `git push`, `npm publish`, `huggingface-cli
  login`, anything with a token — through third-party accelerators, ever.

---

## 1. Probe before prescribing

A timeout on one install command is already a signal, but the probe tells you
the *shape* of the environment in ~5 s (and distinguishes "restricted network"
from "this machine is just offline"):

```bash
python3 <skill>/scripts/net_probe.py            # human-readable
python3 <skill>/scripts/net_probe.py --json     # for scripts (preflight/doctor)
```

Pure stdlib, no curl, works on macOS / Linux / Windows. It reports each dev
host as `ok` / `slow` / `blocked`, plus a one-word verdict:

| Verdict | Meaning | What to do |
|---------|---------|------------|
| `open` | international hosts fine | change nothing — mirrors would only add moving parts |
| `restricted` | domestic controls reachable, dev hosts blocked/slow | mainland profile → route per §2 |
| `mixed` | some blocked (partial proxy/VPN, flaky link) | mirror only the ecosystems that failed |
| `offline` | nothing reachable | not a mirror problem — fix connectivity first |

The verdict is **per-session**: a VPN/proxy toggling changes it. Re-probe
after any network change; don't cache the answer into config.

## 2. The routing table

Put the *session* form on the failing command. Only apply the *permanent* form
with the user's explicit go-ahead.

| Ecosystem | Symptom | Session-scoped route | Permanent (user's call) |
|-----------|---------|----------------------|-------------------------|
| **pip / pipx** | PyPI download KB/s or timeout | `pip install <pkg> -i https://pypi.tuna.tsinghua.edu.cn/simple` (pipx: `--pip-args '-i …'`) | `pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple` |
| **npm** | `registry.npmjs.org` timeout | `npm i -g <pkg> --registry=https://registry.npmmirror.com` | `npm config set registry https://registry.npmmirror.com` |
| **Hugging Face** | model/dataset download hangs (faster-whisper, transformers, …) | `HF_ENDPOINT=https://hf-mirror.com <command>` | export in shell profile |
| **ModelScope** | — (no symptom) | nothing to do: modelscope.cn is a domestic CDN. FunASR/SenseVoice pull from it by default — zero config in CN. (Reverse case: an **overseas** machine finding ModelScope slow can switch such libs to their HF hub option.) | — |
| **Homebrew** | bottle downloads crawl (ghcr.io) | `export HOMEBREW_API_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles/api HOMEBREW_BOTTLE_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles` then `brew install …` | persist the exports (full recipe: TUNA's homebrew help page) |
| **git clone (GitHub)** | clone stalls at `Connecting…` | project has a mirror (Gitee/CNB — check its README) → clone that URL, keep `upstream` pointed at GitHub. No mirror → accelerator prefix: `git clone https://ghfast.top/https://github.com/<owner>/<repo>` — see the trust caveat below | ask the project to publish a mirror (§3) |
| **GitHub releases / raw** | asset download fails | project's own download proxy/CDN first (check its install docs); else the same accelerator prefix on the full `https://github.com/…/releases/download/…` or `raw.githubusercontent.com/…` URL | — |
| **yt-dlp specifically** | `yt-dlp -U` fails; brew/winget lag | install via pip instead: `python3 -m pip install -U yt-dlp -i https://pypi.tuna.tsinghua.edu.cn/simple` — updates then flow through the PyPI mirror instead of GitHub Releases | — |
| **winget (Windows)** | install "succeeds" per manifest but download fails | winget's own CDN is fine in CN, but **packages whose installer fetches from GitHub** (e.g. FFmpeg builds) still fail — fall back to the tool's non-GitHub distribution (ffmpeg: gyan.dev builds) or pip where one exists | — |
| **Docker** | `docker pull` timeout | registry accelerators churn (post-2023 many public ones died) — use your cloud vendor's personal accelerator (Aliyun ACR) rather than a hardcoded public one | — |

**Accelerator-prefix trust caveat** (`ghfast.top`, `gh-proxy.com`, …): these
are third-party relays — contents pass through someone else's server. Use them
only for **public** repos/assets, verify what you got (commit hash you
expected, checksum if the release publishes one), and never send credentials
or private-repo traffic through them. Domains rotate; if one is dead, search
for a live gh-proxy instance or fall back to a real mirror. A project you
control should never *default* to these — publish a proper mirror instead (§3).

## 3. Making your OWN project installable from CN

That's the maintainer side — repo auto-sync mirrors (Gitee/CNB), release
proxying, the structured `install.<os>.<route>.steps` convention for install
manifests — and it lives in `references/project-side.md`. Read it when you're
*publishing* an agent-first project, not when you're just trying to install
something.

## 4. Verify, then stop

The loop closes only when the **original failing command succeeds** (or its
cheap equivalent: `pip download -d /tmp/probe <pkg> -i <mirror>`, `npm ping
--registry=<mirror>`, a `git ls-remote` on the mirror URL). Report which
ecosystems were re-routed and with which flags, so the user can reproduce it
by hand. If the user wants it permanent, hand over the "permanent" column
command — applying it is their call.
