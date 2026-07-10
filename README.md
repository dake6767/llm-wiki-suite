# llm-wiki-suite

llm-wiki 生态的 monorepo：统一管理 **Skills** 与 **My LLM Wiki Browser**。
强调「可复用的方法与资产」，而不是 prompt 堆叠。

每个 skill 都应服务真实任务结果，可复用、可维护、可评估。

结构与发布约定参考自 [`yaojingang/yao-open-skills`](https://github.com/yaojingang/yao-open-skills)，并按本仓库实际情况做了调整。

## 仓库结构

```
llm-wiki-suite/
├── README.md                 # 本文件
├── apps/
│   └── my-llm-wiki-browser/  # Tauri 桌面 / Web / MCP / relay 浏览器应用
├── AGENTS.md                 # 给 agent 的端到端安装协议
├── registry/
│   ├── skills.json           # ★ 安装事实源：install.sh / doctor.py 据此决定装哪些 skill
│   └── bootstrap.json        # ★ agent 安装 / 首次引导的机器可读配置（含 Browser 下载与构建）
├── scripts/
│   └── install-browser.py    # Browser release-first 安装 / source fallback
└── skills/<slug>/             # 每个 skill 的完整实现（SKILL.md + README + 资料）
```

> **安装事实源**：`registry/skills.json` 与 `registry/bootstrap.json` 由安装/体检脚本读取，是「装哪些、怎么装」的唯一依据。下面的目录表是给人看的说明，手动维护即可（增删 skill/app 时顺手改一下）。

## Skills

| Skill | 说明 | 标签 | 状态 | 入口 |
| --- | --- | --- | --- | --- |
| **my-llm-wiki** | 抓取外部内容（公众号 / X / 小红书 / 网页 / 视频 / 想法）转自包含 Markdown，沉淀进知识库 RAW 层 | knowledge-base, rag, archive, raw | ✅ published | [源码](skills/my-llm-wiki) · [SKILL](skills/my-llm-wiki/SKILL.md) |
| **my-llm-wiki-maintainer** | 把 RAW 源编译成相互链接的 wiki 页面并维护：review / deep research / 去重 / lint / 带引用查询 | knowledge-base, wiki, compile, citations | ✅ published | [源码](skills/my-llm-wiki-maintainer) · [SKILL](skills/my-llm-wiki-maintainer/SKILL.md) |
| **cn-mirrors** | 网络环境自适应：探测受限网络（大陆典型），把 pip/npm/brew/GitHub/HF 的安装下载路由到官方国内镜像；附项目侧发布指南（镜像仓库 / release 代理 / install_cn 约定） | network, mirrors, install, bootstrap | ✅ published | [源码](skills/cn-mirrors) · [SKILL](skills/cn-mirrors/SKILL.md) |

## Apps

| App | 说明 | 标签 | 状态 | 入口 |
| --- | --- | --- | --- | --- |
| **my-llm-wiki-browser** | 桌面 / Web / MCP wiki 浏览器：读本机 Markdown 知识库，本地/远程访问，MCP 供 agent 检索 | desktop, tauri, mcp | 🧪 local | [源码](apps/my-llm-wiki-browser) |

## 本地安装（同步到各 agent）

合集仓库本身**不要**直接放进 `~/.claude/skills/`（会被当成一个超大 skill 误加载）。
用 `scripts/install.sh` 按 registry 把 active 的 skill 同步进各 agent 的 skills 目录。
默认用**软链**：所有 agent 共享合集这一份 canonical 源，改一次到处生效、不漂移；运行期产物（`data/`、`reports/`）已 gitignore，agent 写进来也不会污染仓库。

```bash
scripts/install.sh --dry-run                 # 预览（默认目标：~/.claude/skills、~/.hermes/skills）
scripts/install.sh                            # 软链安装所有 active skill
scripts/install.sh my-llm-wiki                # 只装某一个
scripts/install.sh --target ~/.codex/skills --target ~/.agents/skills
scripts/install.sh --copy --force             # 隔离拷贝，替换已存在的（旧的备份为 .bak-时间戳）
```

- 目标已是指向本合集的正确软链 → 标记 `up to date`，幂等。
- 目标是**真实目录**（如独立 git 仓库）→ 默认**安全跳过**，需 `--force` 才替换（替换前自动 `mv` 成 `.bak-时间戳`）。

装完（或平时排障）跑一次 suite 体检，一条命令看「整套是否就绪」——skills 软链、wiki 注册表、Browser 可达性、抓取适配器：

```bash
python3 scripts/doctor.py            # 人读摘要
python3 scripts/doctor.py --json     # 机器可读（供脚本 / watch 用）
```

每项报 `ok`/`warn`/`error`/`skip`，仅 `error`（skills 未软链进任何 agent 目录）时进程非零退出。

### 抓取工具链（强烈建议装齐）

skills 自己不带 fetcher，抓取质量取决于本机工具。其中三件对流畅使用**接近必装**
（缺哪件、装什么，`doctor.py` 的 adapters 一节会直接给出命令和项目主页）：

| 工具 | 安装 | 缺了会怎样 |
| --- | --- | --- |
| **opencli** | `npm i -g @jackwener/opencli` | 公众号图片不本地化；小红书/抖音直接抓不了（登录墙）；X 降级 |
| **yt-dlp** | `brew install yt-dlp` | 视频→转写整条路不通（字幕和音频都走它） |
| **ffmpeg** | `brew install ffmpeg` | 视频路径没法提音频/封面 |

机器可读清单在 `registry/bootstrap.json` → `capture_toolchain`；agent 安装时的
推荐话术见 [`AGENTS.md`](AGENTS.md) 第 3 步（原则：大声推荐、附主页链接供甄别，
但装不装由用户拍板，绝不静默安装）。npm 全局 bin 记得同时导出到 `~/.profile`
（守护进程型 agent 只读 bash 登录文件，不读 `~/.zprofile`）。

## 新增 / 更新一个 skill

简版流程：

1. 把 skill 源码放到 `skills/<slug>/`（建议用 `git archive` 只导出 tracked 文件，自动避开缓存/输出物）。
2. 删除不可发布内容（账号 / token / cookie / 私有路径 / 本地生成的输出物）。
3. 在 `registry/skills.json` 增改条目（`install.sh` / `doctor.py` 据此工作）。
4. 每个 skill 以自带的 `SKILL.md` 作为使用说明入口。
5. 手动在上面的 Skills 表加一行（纯说明，不影响安装）。
6. push 后把该条目 `sync_status` 标为 `published`。

## My LLM Wiki Browser 融合

Browser 作为 app 纳入 `apps/my-llm-wiki-browser/`，不安装到 agent 的 skills 目录。
使用与构建说明见 [`apps/my-llm-wiki-browser/README.md`](apps/my-llm-wiki-browser/README.md)。

Browser 安装策略是 **优先下载 GitHub Release**；只有没有匹配 release 时才回退源码构建：

```bash
python3 scripts/install-browser.py --open
python3 scripts/install-browser.py --fallback-source
```

给 agent 的完整安装协议见 [`AGENTS.md`](AGENTS.md)。
