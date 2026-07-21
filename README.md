# My LLM Wiki

> 把网页、文档和视频变成可搜索、可引用、能跳回原始来源的本地知识。

My LLM Wiki 是一组 agent skills 与跨平台桌面应用。它把网页、公众号、视频、X、本地
文档和个人笔记保存为不可变 RAW，再增量编译成相互链接的 Markdown Wiki。知识文件留在
自己的电脑里，同时可供 Browser、Obsidian 和 AI Agent 检索。

项目源于 Andrej Karpathy 的 [LLM Wiki 理念](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)：
LLM 不只回答一次问题，而是持续把原始资料编译进一份可维护、可追溯的长期知识库。

![My LLM Wiki Browser](assets/wiki-browser-frame.jpg)

## 从原始资料到可验证知识

```text
网页 / 文档 / 视频 / X / 笔记
  ↓  可替换的采集适配器
不可变 RAW 原件
  ↓  提取、合并、互链、Review
Markdown Wiki
  ↓
Browser / Obsidian / 有界全文检索 / AI Agent
```

视频场景会优先读取字幕；没有字幕时使用选定的本地 ASR。保存结果是带来源与跳转时间戳
的完整转写，而不是一段脱离原文的摘要：

```markdown
**[23:56](https://www.youtube.com/watch?v=<id>&t=1436s)**
这里是该时刻开始的转写内容……
```

因此检索结果既能说明“视频讲了什么”，也能回到原片的准确位置核验。

![从在线视频到 RAW、Wiki 与检索](assets/video-to-wiki-workflow.png)

## 核心能力

| 环节 | 能力 | 主要产物 |
| --- | --- | --- |
| 采集 | 网页、公众号、文档、笔记、视频、X 帖文与 bookmarks | 自包含 Markdown、已本地化资源、来源元数据 |
| 编译 | 实体、概念、来源摘要、关联关系与轻量 Research 线索 | 可审阅、可追溯、相互链接的 Wiki 页面 |
| 维护 | Review、冲突哨兵、Deep Research、去重、增量 lint | 持续演进的知识库 |
| 检索 | FTS5 与 top-k 有界页面读取 | 带引用回答和可控大小的上下文 |
| 使用 | Browser、Obsidian、CLI；显式开启后的 MCP/远程分享 | 同一份本地知识服务于人和 Agent |

采集工具是可替换适配器。opencli、agent-reach、yt-dlp、本地 ASR 或自定义脚本取得的内容
都进入同一份 [`RAW 契约`](skills/my-llm-wiki/references/raw-contract.md)；抓取路径变化不需要
迁移已经积累的知识。

每次编译还会给出 `0–3` 个证据缺口、来源冲突或可深挖方向。只有用户选择某个方向时才进入
Deep Research，新证据仍先保存为 RAW，再编译回 Wiki。

## 本地优先与远程访问

Wiki、RAW、图片和运行状态都是普通本地文件。生成的知识库兼容 Obsidian：页面使用
Markdown、YAML frontmatter 与 `[[wikilinks]]`，初始化时也会创建 `.obsidian/` 配置。
Browser 直接读取这些文件并建立本地 FTS5 索引，不要求把知识迁移到云笔记或托管向量库。

用户可显式开启 relay，让手机或异地浏览器读取电脑上的同一份 Wiki。relay 默认关闭；
访客只能访问获准 Wiki 的页面、附件、目录、搜索和图谱，RAW、Review 与 MCP 不开放。

“本地优先”不等于托管 relay 的流量端到端不可见。中继能够看到传输明文，但当前协议不把
Wiki 正文写入 Durable Object 存储。敏感知识库可以始终关闭 relay，只使用本地 Browser、
Obsidian 或 CLI。分享授权当前以整个 Wiki 为边界，不是单页授权。

## 快速开始

把仓库地址交给能执行本地命令的 agent：

```text
安装 https://github.com/dake6767/llm-wiki-suite 。读取仓库根目录 AGENTS.md，
先一次性列出宿主、skills、受管组件、Browser 和冲突替换选项；我确认一次后持续执行到终态。
```

中国大陆网络可把地址换成只读镜像
`https://gitee.com/dake6767/llm-wiki-suite`。GitHub 仍是 canonical 源。

Protocol 5 在 Windows、macOS 和 Linux 上使用同一条交互：

```text
只读检查 → 一次选择并确认全部选项 → 冻结计划 → 无人值守执行 → 集中显示结果/人工动作
```

安装过程中不会逐步追问，也不会使用全局 pip/npm、Homebrew、apt、winget 或用户 PATH
补依赖。Skills、Documents、Web/OpenCLI、Video、中文/非中文 ASR 与可选 Browser 都是
显式选项。自动步骤完成后，才集中显示加载 Chrome 扩展等必须由人完成的动作。

Windows 由 agent 下载并校验原生执行核心，正常安装不打开 Setup 窗口，也不要求预装 Git、
Python、Node 或 Git Bash；双击 Setup 仍可作为恢复界面。macOS/Linux 使用 Python 执行
核心完成同一协议，不需要额外 GUI 安装器。

所有平台的安装事实源是：

```text
~/.my-llm-wiki/install-state.json
```

它记录宿主、skills 来源、受管组件、私有运行时、工具 argv、待处理动作和 doctor 结果。
安装可以安全重跑、repair 或按所有权 uninstall；Wiki 与 RAW 始终作为用户数据保留。

完整的 agent 操作契约、镜像路由、Windows 数据盘与恢复规则见 [`AGENTS.md`](AGENTS.md)。
开发 checkout 可从只读检查开始：

```bash
bash bootstrap.sh --repo "$PWD" inspect --out inspection.json --json
```

安装完成后，直接给 agent 一份真实资料：

```text
把这个视频保存到我的 Wiki，保留完整转写和可跳转时间戳，然后整理成知识页面：<URL>
```

MCP 不属于初始安装；只有用户之后明确提出时才单独配置。

## 组件状态

| 组件 | 职责 | 状态 |
| --- | --- | --- |
| [`my-llm-wiki`](skills/my-llm-wiki/SKILL.md) | Wiki 路由、网页/文档/note 采集、RAW 契约 | Stable |
| [`my-llm-wiki-video`](skills/my-llm-wiki-video/SKILL.md) | 在线视频 → 带时间戳完整转写 | Preview |
| [`my-llm-wiki-x`](skills/my-llm-wiki-x/SKILL.md) | X 单帖/长文与 bookmarks 增量同步 | Preview |
| [`my-llm-wiki-maintainer`](skills/my-llm-wiki-maintainer/SKILL.md) | 编译、Review、Research、去重与 lint | Stable |
| [`my-llm-wiki-search`](skills/my-llm-wiki-search/SKILL.md) | 只读检索、引用回答和上下文预算 | Preview |
| [`cn-mirrors`](skills/cn-mirrors/SKILL.md) | 受限网络探测与分生态镜像路由 | Stable |
| [`My LLM Wiki Browser`](apps/my-llm-wiki-browser/) | 阅读、FTS、MCP、远程访问与分享 | Released · v1.0.37 |

Preview 表示能力已在 suite 中参与安装，但平台覆盖或独立发布仍在收敛。实际生命周期、依赖
和 capability 以 [`registry/skills.json`](registry/skills.json) 为准。

## 仓库结构

```text
llm-wiki-suite/
├── bootstrap.sh                    # Protocol 5 获取与命令分发
├── AGENTS.md                       # agent 的一次选择安装协议
├── apps/my-llm-wiki-browser/       # Tauri / Rust / React / MCP / relay
├── registry/
│   ├── skills.json                 # skills、依赖、bundles、capabilities
│   ├── bootstrap.json              # 平台入口、宿主与 Release 路由
│   └── agent-components.lock.json  # macOS/Linux 受管组件上游锁
├── scripts/
│   ├── agent_install.py            # macOS/Linux 执行核心
│   ├── windows_setup.py            # Windows 原生执行核心
│   ├── install-browser.py          # Browser Release 安装
│   └── doctor.py                   # 最终健康检查
└── skills/<slug>/                  # SKILL.md、scripts、references、assets
```

Browser 是可选组件。没有 Browser 时，采集、编译、维护和本地有界检索仍可使用；安装后
增加桌面阅读、FTS 索引、MCP、远程访问和分享能力。

## 开发与维护

提交前至少运行：

```bash
python3 -m compileall -q scripts skills/my-llm-wiki/scripts
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/check_approval_safety.py
```

Browser 的前端构建、Rust 测试与 Tauri 开发命令见
[`apps/my-llm-wiki-browser/README.md`](apps/my-llm-wiki-browser/README.md)。新增或调整 skill
时，同时更新 [`registry/skills.json`](registry/skills.json)；`requires` 是运行时依赖，
`bundles` 是门面默认 companion，`capabilities` 用于限定 doctor 的检查范围。
