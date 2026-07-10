# llm-wiki-suite

llm-wiki 生态的 monorepo：统一管理 **Skills** 与 **My LLM Wiki Browser**。
强调「可复用的方法与资产」，而不是 prompt 堆叠。

每个 skill 都应服务真实任务结果，可复用、可维护、可评估。

本文件面向人类读者；给 agent 的端到端安装协议（工具链检测、无人值守配置、Browser 安装等）见 [`AGENTS.md`](AGENTS.md)。

结构与发布约定参考自 [`yaojingang/yao-open-skills`](https://github.com/yaojingang/yao-open-skills)，并按本仓库实际情况做了调整。

## 仓库结构

```
llm-wiki-suite/
├── bootstrap.sh              # 可独立下载的一键 skills 同步入口
├── README.md                 # 本文件
├── apps/
│   └── my-llm-wiki-browser/  # Tauri 桌面 / Web / MCP / relay 浏览器应用
├── AGENTS.md                 # 给 agent 的端到端安装协议
├── registry/
│   ├── skills.json           # ★ 安装事实源：install.sh / doctor.py 据此决定装哪些 skill
│   └── bootstrap.json        # ★ agent 安装 / 首次引导的机器可读配置（含 Browser 下载与构建）
├── scripts/
│   └── install-browser.py    # Browser release-first 安装 / source fallback
└── skills/<slug>/             # 每个 skill 的完整实现（SKILL.md + scripts/references/assets）
```

> **安装事实源**：`registry/skills.json` 与 `registry/bootstrap.json` 由安装/体检脚本读取，是「装哪些、怎么装」的唯一依据。下面的目录表是给人看的说明，手动维护即可（增删 skill/app 时顺手改一下）。

## Skills

| Skill | 说明 | 标签 | 状态 | 入口 |
| --- | --- | --- | --- | --- |
| **my-llm-wiki** | RAW 抓取门面与共享核心：wiki 路由、Adapter/RAW 契约、网页/公众号/文档/note、normalization 与 synthesis handoff | knowledge-base, rag, archive, raw | ✅ published | [源码](skills/my-llm-wiki) · [SKILL](skills/my-llm-wiki/SKILL.md) |
| **my-llm-wiki-video** | 在线视频 → 带跳转时间戳的忠实转写；字幕优先、ASR fallback、封面本地化，不保存视频文件 | video, transcript, asr, timestamps | 🧪 unpublished | [源码](skills/my-llm-wiki-video) · [SKILL](skills/my-llm-wiki-video/SKILL.md) |
| **my-llm-wiki-x** | X 单条/长文完整抓取与 bookmarks 增量同步：逐条正文媒体、tweet-id 去重、可恢复批处理 | x, twitter, bookmarks, raw | 🧪 unpublished | [源码](skills/my-llm-wiki-x) · [SKILL](skills/my-llm-wiki-x/SKILL.md) |
| **my-llm-wiki-maintainer** | 把 RAW 源编译成相互链接的 wiki 页面并维护：review / deep research / 去重 / lint / 回存；O(top-k) 有界检索 + 上下文预算 | knowledge-base, wiki, compile, citations | ✅ published | [源码](skills/my-llm-wiki-maintainer) · [SKILL](skills/my-llm-wiki-maintainer/SKILL.md) |
| **my-llm-wiki-search** | 只读检索与带引用回答的用户入口：三层检索（Browser MCP → HTTP → 本地有界），硬上下文预算，不写任何东西 | knowledge-base, search, query, read-only | 🧪 unpublished | [源码](skills/my-llm-wiki-search) · [SKILL](skills/my-llm-wiki-search/SKILL.md) |
| **cn-mirrors** | 网络环境自适应：探测受限网络（大陆典型），把 pip/npm/brew/GitHub/HF 的安装下载路由到官方国内镜像；附项目侧发布指南（镜像仓库 / release 代理 / install_cn 约定） | network, mirrors, install, bootstrap | ✅ published | [源码](skills/cn-mirrors) · [SKILL](skills/cn-mirrors/SKILL.md) |

## Apps

| App | 说明 | 标签 | 状态 | 入口 |
| --- | --- | --- | --- | --- |
| **my-llm-wiki-browser** | 桌面 / Web / MCP wiki 浏览器：读本机 Markdown 知识库，本地/远程访问，MCP 供 agent 检索 | desktop, tauri, mcp | 🧪 local | [源码](apps/my-llm-wiki-browser) |

## 本地安装（同步到各 agent）

合集仓库本身**不要**直接放进 `~/.claude/skills/`（会被当成一个超大 skill 误加载）。
新机器只需下载根目录的 `bootstrap.sh`：它会优先复用已有开发 checkout 或已安装
skill 指向的 checkout；找不到时才克隆到稳定目录 `~/.my-llm-wiki/suite`，再按
registry 把 active skills 同步到 Codex、Claude、Hermes、Agents 和 Workbuddy。
建议先下载并审阅，再执行，不使用 `curl | bash`：

```bash
curl -fsSLo bootstrap.sh \
  https://raw.githubusercontent.com/dake6767/llm-wiki-suite/main/bootstrap.sh
less bootstrap.sh
bash bootstrap.sh                         # 全部 active skills → 全部默认 agent
bash bootstrap.sh my-llm-wiki-x           # X leaf + 必需的共享 core
bash bootstrap.sh --dry-run                # 只预览
bash bootstrap.sh --update                 # 仅 clean checkout 执行 ff-only 更新
```

默认用**软链**，所以 checkout 是长期 source of truth，不能放在临时目录；更新一次
checkout，所有 agent 同步生效。脚本只检测所选 skill 的抓取技术栈并给建议，绝不
自动安装 opencli、yt-dlp、ffmpeg 等外部工具。

已经进入仓库开发时，也可以直接用底层 `scripts/install.sh` 精细控制目标目录。
运行期产物（`data/`、`reports/`）已 gitignore，agent 写进链接目录也不会污染仓库。

```bash
scripts/install.sh --dry-run                 # 预览（默认同步到 registry 声明的五类 agent）
scripts/install.sh                            # 软链安装所有 active skill
scripts/install.sh my-llm-wiki                # 装门面及其默认捆绑的 video / X leaf
scripts/install.sh my-llm-wiki-video          # 装 video leaf 及其必需的 my-llm-wiki core
scripts/install.sh --target ~/.codex/skills --target ~/.agents/skills
scripts/install.sh --copy --force             # 隔离拷贝，替换已存在的（旧的备份为 .bak-时间戳）
```

- 目标已是指向本合集的正确软链 → 标记 `up to date`，幂等。
- 目标是**真实目录**（如独立 git 仓库）→ 默认**安全跳过**，需 `--force` 才替换（替换前自动 `mv` 成 `.bak-时间戳`）。

装完（或平时排障）跑一次 suite 体检，一条命令看 skills 软链、wiki 注册表、Browser 可达性和按 skill 选择的抓取 capability：

```bash
python3 scripts/doctor.py            # 人读摘要
python3 scripts/doctor.py --json     # 机器可读（供脚本 / watch 用）
python3 scripts/doctor.py --skills my-llm-wiki-x  # 只检查 X leaf + 所需 core
python3 scripts/doctor.py --target ~/.codex/skills # 检查自定义安装目标
```

每项报 `ok`/`warn`/`error`/`skip`，仅 `error` 非零退出；各状态的含义与处理方式见
[`AGENTS.md`](AGENTS.md)。

### 抓取工具链（按 capability 检测）

skills 自己不带 fetcher。`registry/skills.json` 只声明 capability profile；
`doctor.py --skills …` 按所选 skill 计算检测范围，例如单装 X 不检查
yt-dlp/ffmpeg/markitdown。缺失工具的安装命令由 doctor 的 `toolchain` 一节给出，
必须经用户确认，绝不静默执行。完整协议（工具清单位置、daemon agent 的 PATH
注意事项、无人值守审批配置等）见 [`AGENTS.md`](AGENTS.md)。

## 新增 / 更新一个 skill

简版流程：

1. 把 skill 源码放到 `skills/<slug>/`（建议用 `git archive` 只导出 tracked 文件，自动避开缓存/输出物）。
2. 删除不可发布内容（账号 / token / cookie / 私有路径 / 本地生成的输出物）。
3. 在 `registry/skills.json` 增改条目（`install.sh` / `doctor.py` 据此工作）；
   运行时依赖用 `requires`，门面默认一起安装的 companion skills 用 `bundles`，
   用户功能需要的技术栈用 `capabilities` profile 声明。
4. 每个 skill 以自带的 `SKILL.md` 作为使用说明入口。
5. 手动在上面的 Skills 表加一行（纯说明，不影响安装）。
6. 提交前运行 `python3 scripts/check_approval_safety.py` 与
   `python3 -m unittest scripts.test_approval_safe -v`，确保命令示例能安全通过
   agent 的命令审批（先落盘再交给仓库内固定脚本；禁止 `curl | python`、
   `python -c`、临时 heredoc 和宽范围 `rm -rf`）。
7. push 后把该条目 `sync_status` 标为 `published`。

## My LLM Wiki Browser 融合

Browser 作为 app 纳入 `apps/my-llm-wiki-browser/`，不安装到 agent 的 skills 目录。
使用与构建说明见 [`apps/my-llm-wiki-browser/README.md`](apps/my-llm-wiki-browser/README.md)。
安装策略是 **优先下载 GitHub Release**（`scripts/install-browser.py`），没有匹配
release 时才回退源码构建；具体命令与回退条件见 [`AGENTS.md`](AGENTS.md)。
