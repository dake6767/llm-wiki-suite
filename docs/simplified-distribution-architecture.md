# My LLM Wiki 精简分发架构

状态：v2 实施中
受众：My LLM Wiki 的产品、Skills、Browser 与发布维护者
范围：Skills、Browser、官方工具链的安装、更新、修复与开放 Provider 机制

## 1. 决策摘要

My LLM Wiki 保留两条产品路径：

1. **一键路径**：安装 Browser，由 Browser 内的无界面 Setup Core 安装完整
   Skills Pack、官方验证工具链并初始化 Wiki。项目对这条路径提供开箱可用保证。
2. **开放路径**：用户直接安装 Skills，使用 Agent 已有工具、系统工具或自定义
   Adapter。Skills 仍遵守同一套 SOP 和产物契约，但项目不负责配置第三方环境。

官方工具链是默认、推荐、完整验证的执行基线，不是 Skills 的唯一运行时。
用户明确指定其他工具时，用户选择优先。没有用户偏好时，Skills 优先使用官方
Provider；官方 Provider 不可用时，才选择其他满足契约的 Provider。

本提案不取消 Runtime Pack。它取消的是围绕 Runtime Pack 建立的通用安装平台：
不再让用户在初次安装时组合 skill 子集、五类组件、失败策略、包管理器和平台运行时；
也不再为 Windows 与 macOS/Linux 维护两套等价的安装状态机。

## 2. 背景

项目最初以 Skills 与 Browser 为核心。为了处理真实用户环境，安装体系逐步承担了：

- 多 Agent 宿主检测与 skill 安装；
- Python、Node、FFmpeg、OpenCLI、文档工具和 ASR 环境管理；
- GitHub、中国大陆网络与不同包生态的下载路由；
- Windows 原生 Setup 与 macOS/Linux Python 执行核心；
- Browser 下载、Wiki 初始化、doctor、repair、uninstall、receipt 和全局回滚；
- 一次性选择、冻结 plan、session journal 与终态解释。

这些能力分别有合理来源，但组合后使安装器接近一个跨平台包管理器。复杂度还通过
测试矩阵、Release 制品、锁文件、协议 schema 和维护文档继续扩散。

用户环境和网络问题不能被忽略：一个没有 FFmpeg、Node、OpenCLI 或可用 ASR 的 Agent，
不应在第一次真实任务里临时猜测包管理器、处理 Python 冲突或研究镜像。因此项目仍需
提供一套经过验证的官方工具链。精简的目标是缩小安装语义，而不是把环境问题重新交给用户。

## 3. 产品原则

### 3.1 默认有保证，替换有自由，失败能回退

- **默认有保证**：一键路径默认安装并优先使用官方验证工具链。
- **替换有自由**：用户可以按能力或单次任务指定其他 Provider。
- **失败能回退**：自定义 Provider 不健康或产物不合格时，可以切回官方 Provider。

### 3.2 SOP 与工具解耦

Skill 定义工作流程、输入要求、质量门槛和产物契约，不把某个具体工具写成唯一实现。

例如视频 SOP 可以要求：

```text
识别来源
→ 优先取得原字幕
→ 必要时提取音频
→ 生成带时间戳转写
→ 校验完整性
→ 写入不可变 RAW
```

官方 `yt-dlp + FFmpeg + ASR` Provider 是这套 SOP 的参考实现。其他工具只要满足同样的
字幕、时间戳、来源和 RAW 契约，也可以完成对应步骤。

### 3.3 用预构建制品解决环境问题

一键路径不在用户机器上通过 pip、npm、brew、apt、winget 等组合生产环境。CI 为每个
支持的平台和架构构建、测试并发布自包含制品。用户机器只执行下载、哈希校验、解压、
原子激活和健康检查。

### 3.4 在制品边界解决网络问题

安装和更新只访问项目声明的 Release 路线：项目 CDN 优先，GitHub Releases 作为
canonical fallback。安装阶段不再探测和组合 PyPI、npm、系统包管理器等生态。
ASR 模型仍可首次使用时下载，但必须由固定 manifest 声明来源、镜像、大小和校验信息。

### 3.5 Browser 是一键路径的控制面，不是 Skills 的强依赖

Browser 负责一键路径的安装与可视化状态；Setup Core 同时提供结构化 CLI，供 Agent
无界面调用。直接安装 Skills 的开放路径不要求 Browser 存在。

## 4. 目标架构

```text
                         一键路径
                 ┌─────────────────────┐
                 │ Browser UI / CLI    │
                 └──────────┬──────────┘
                            │
                 ┌──────────▼──────────┐
                 │ Setup Core (Rust)   │
                 │ skills/toolchain/wiki│
                 └──────┬───────┬──────┘
                        │       │
                Skills Pack   Official Toolchain Packs
                        │       │
                        └───┬───┘
                            │
                       Provider Resolver
                            │
         ┌──────────────────┼──────────────────┐
         │                  │                  │
  Official Providers  Existing/Agent Tools  Custom Adapters
         └──────────────────┼──────────────────┘
                            │
                    RAW / Wiki Contracts

                         开放路径
              Direct Skills Install
                        │
                Provider Resolver
                        │
          Existing Tools / Custom Adapters
```

### 4.1 Setup Core

Setup Core 应位于 Browser 的 Rust workspace 中，作为独立 library/CLI 能力，而不是把
现有 Python 安装脚本逐行翻译成 Rust。Browser UI 与无界面 CLI 调用同一实现。

目标命令面保持很小：

```bash
my-llm-wiki setup --host codex [--wiki-path /absolute/wiki/path] --json
my-llm-wiki add-host --host claude --json
my-llm-wiki status --json
my-llm-wiki update --check --json
my-llm-wiki update --json
my-llm-wiki repair --json
my-llm-wiki uninstall --host codex --json
```

`setup` 是首次安装的一次性决定（安装位置、Wiki、官方工具链），需要发布清单；
`add-host` 只把已经装好的那份 Skills Pack 链接给另一个已知宿主，不联网、不下载，
也不改动 packs、Wiki 和其它宿主。移除仍走 `uninstall --host`。

Setup Core 只负责：

- 检测已知 Agent 宿主并安装完整 Skills Pack；
- 在安装完成后按宿主粒度增删 Skills Pack；
- 确保官方基础工具链可用；
- 按需确保 ASR 等大型能力包可用；
- 按一个经过联合验证的发布组合更新 Browser、Skills 和已安装的官方工具链；
- 初始化或复用 Wiki；
- 保存最小所有权和版本状态；
- 返回必须由用户完成的 GUI 动作；
- 为 Browser UI 和 Agent CLI 返回同一结构化结果。

Setup Core 不负责：

- 任意 skill 子集和依赖闭包；
- 通用 Python/npm/系统包管理；
- 修改用户的全局 shell、pip、npm 或包管理器配置；
- Hermes 等宿主的安全配置；
- 自动加载 Chrome unpacked extension；
- 为第三方 Provider 安装依赖；
- 运行一个可恢复到任意中间状态的通用事务引擎。

### 4.2 Setup GUI 与界面边界

Setup GUI 不依赖 `127.0.0.1` 本地 Web 服务。它是随 Browser 安装包发布、由 Tauri
直接加载的内置页面，通过 Rust IPC 调用 Setup Core。

当前 Browser 的设置页虽然显示在 Tauri 窗口内，实际仍会导航到本地服务的
`/desktop/config`。该页面依赖 server、token 和 Wiki registry 已经可用，无法承担
“这些前置条件尚未建立”时的首次安装和修复。因此目标架构明确分开以下界面：

| 界面 | 技术边界 | 职责 |
| --- | --- | --- |
| Setup & Toolchain | Tauri 内置页面 + Rust IPC | 安装、更新、修复、Provider 设置 |
| Browser Settings | `127.0.0.1/desktop/config` | Wiki、服务端口、分享和运行设置 |
| Wiki Browser | `127.0.0.1` Web UI | 阅读、搜索、Review 和内容管理 |
| Agent CLI | Setup Core CLI | 无界面 setup、status、update、repair |

Browser 启动时先读取 Setup Core 的最小状态，再决定是否启动和打开本地 Web 服务：

```text
Browser 启动
→ Setup Core status
   ├─ ready
   │   → 启动 127.0.0.1 服务
   │   → 打开 Browser Settings 或默认 Wiki
   │
   ├─ not-configured
   │   → 打开内置 Setup GUI
   │   → 安装 Skills + 官方工具链 + 初始化 Wiki
   │   → 启动 127.0.0.1 服务
   │   → 跳转到 Browser
   │
   └─ needs-repair
       → 打开内置 Setup GUI 的修复模式
```

首次 Setup GUI 保持为一个短流程：

1. **欢迎**：说明一键路径与开放路径；
2. **宿主选择**：检测 Codex、Claude 等已知宿主，只选择 skill 安装目标；
3. **一次确认**：展示下载体积、官方工具链、可编辑的 Wiki 存放路径和准确冲突；
4. **执行进度**：只展示 Skills、Toolchain、Wiki 等产品阶段，不暴露 pip/npm 实现；
5. **完成与人工动作**：打开 Wiki，并集中展示 Chrome 扩展等必须由用户完成的步骤。

Documents、Web、Video、ASR 和 optional failure policy 不进入首次 GUI。高级入口可以允许：

- 不安装官方工具链，改走开放路径；
- 仅使用 Browser 打开已有 Wiki；
- 为具体 capability 保存 Provider 覆盖；
- 对外来 skill 冲突逐项选择备份替换或跳过该宿主。

前端应使用单独的构建入口，而不是把 Setup 简单加入会由本地服务或 relay 提供的普通
Browser 路由：

```text
frontend/
├── index.html          # 由 127.0.0.1 服务提供的 Browser UI
├── setup.html          # 打包进 Tauri 的本地 Setup UI
└── src/
    ├── browser/...
    ├── setup/...
    └── shared/...      # 品牌、按钮、进度和状态组件
```

Setup 页面调用一组很小的 Tauri commands：

```text
setup_inspect
setup_apply
setup_status
setup_update
setup_repair
```

长任务在 Setup Core 中异步运行，通过 Tauri event/channel 推送进度。GUI 只是状态投影：
关闭窗口不取消已经开始的操作，重新打开后通过 `setup_status` 恢复展示。是否允许取消由
具体阶段的原子边界决定，不能通过关闭窗口隐式中断。

安装完成后，托盘保留两个不同入口：

```text
Browser 设置…
Skills 与工具链…
```

“Browser 设置…”继续打开本地 Web 设置页；“Skills 与工具链…”重新打开 Tauri 内置
Setup GUI，并显示 Skills Pack、官方工具链、Provider、update、repair 和人工动作状态。
首次安装和后续工具链管理共用相同的组件与 Setup Core，不复制业务逻辑。

安装、覆盖、更新和修复等文件写操作不作为本地 HTTP API 暴露，也不得被 relay 转发。
只有随应用打包且通过 Tauri capability/ACL 授权的 Setup 页面可以调用 mutation commands。
本地 Web UI 最多读取经过认证的摘要状态，不能直接触发 Setup Core 写操作。

### 4.3 Skills Pack

一键路径始终安装当前发布的完整 Skills Pack，不在安装时暴露 skill 子集、bundle 或
dependency closure。Skills Pack 是平台无关、带版本和内容摘要的不可变制品。

Browser 安装包可以内置一份发布时的 baseline Skills Pack，使 Browser 下载完成后即使
网络暂时不可用，也可以完成首次 skill 安装。有网络时，Setup Core 再检查独立的 Skills
Release，并原子更新到更高版本。

开放路径仍允许用户通过 Agent 自带的 skill installer、仓库复制或其他生态工具独立安装
一个或多个 skill；这条路径不写入一键安装状态，也不自动安装官方工具链。

### 4.4 官方工具链

面向用户只呈现一个产品概念：**官方验证工具链**。它默认安装、默认优先，但可以取消或
被用户 Provider 覆盖。

内部可以按体积和首次使用时机拆成少量不可变下载单元：

```text
toolchain-base
├── FFmpeg
├── yt-dlp
├── aria2c
├── private Node + OpenCLI
└── document conversion runtime

asr-zh
├── isolated Python runtime
├── SenseVoice/FunASR dependencies
└── model manifest

asr-other
├── isolated Python runtime
├── faster-whisper/CTranslate2 dependencies
└── model manifest

models
└── first-use model files, separately cached and verified where possible
```

这些是内部制品，不是初次安装时的用户选择。`toolchain-base` 属于一键路径的默认基线；
ASR runtime 和模型在第一次遇到“无字幕且需要转写”的任务时按需获取。

默认只保证跨平台 CPU 基线。CUDA、ROCm、特殊 GPU 和用户自建推理服务属于自定义
Provider，不进入官方安装矩阵。

### 4.5 Provider Resolver

Skills 依赖能力，不依赖 Runtime Pack 的目录或 receipt。Resolver 按以下顺序选择 Provider：

1. 当前任务中用户明确指定的 Provider；
2. 用户保存的长期能力覆盖；
3. 健康的官方 Provider；
4. 当前 Agent、Browser 或系统中满足契约的其他 Provider；
5. 可用的自定义 Adapter；
6. 没有可用 Provider 时，建议安装或修复官方工具链。

单次选择不自动持久化。只有用户明确表达“以后都使用”时，才写入长期覆盖。

示例配置：

```yaml
provider_policy: official-preferred

overrides:
  capture.web: opencli-system
  transcribe.audio: my-whisper-server
```

配置只保存偏好和 Provider 标识，不保存由 shell 拼接的命令。自定义命令必须使用结构化
argv 或受控 Adapter 配置。

### 4.6 Provider 契约与质量门槛

Provider 可以完成一个完整抓取，也可以只提供流程中的一个能力。不同阶段允许组合不同
Provider。例如字幕由站点 API 获取、音频由系统 FFmpeg 提取、转写由用户自己的服务完成。

核心能力示例：

```text
capture.web.public
capture.web.authenticated
capture.assets.localize
capture.video.metadata
capture.video.captions
media.extract-audio
media.parallel-download
transcribe.audio.timestamped
document.to-markdown
```

每项能力都应定义输入、输出、错误和质量要求。Skill 在写入 RAW 前执行统一校验，而不是
因为 Provider 命令成功退出就直接接受结果。

官方 Provider 的优势来自完整 SOP 测试和项目支持承诺，不来自绕过质量校验。

## 5. 两条产品路径

### 5.1 一键路径：项目保证开箱可用

```text
下载标准 Browser 安装包
→ 安装并启动 Browser
→ 选择一个或多个 Agent 宿主
→ Setup Core 安装完整 Skills Pack
→ Setup Core 确保 toolchain-base 健康
→ 初始化或打开 Wiki
→ 返回完成状态和少量人工动作
```

用户首次安装不再选择 Documents、Web、Video、中文 ASR、非中文 ASR。Browser 可以在
安装说明中明确告知：官方工具链默认启用，但用户之后可以为任何能力选择其他 Provider。
Setup 完成后 Wiki 立即可用；同一完成页提供独立的中文 ASR“安装并预热”操作，由 Browser
后台准备 runtime、fsmn-vad 与 SenseVoiceSmall，不阻塞网页采集体验。

OpenCLI Browser Bridge 仍受 Chrome 安全边界限制。Setup Core 可以准备并展示扩展目录、
检测连接状态和提供验证按钮，但不能替用户自动加载 unpacked extension 或完成第三方登录。

### 5.2 开放路径：用户控制工具链

```text
通过 Agent/仓库安装 Skills
→ Skills 探测可用 Provider
→ 使用用户现有或自定义工具
→ 对产物执行同样的契约校验
```

开放路径允许之后单独接入官方 Toolchain Provider，但不以此作为 Skills 安装成功的条件。
项目保证契约兼容，不保证第三方工具的安装、网络、升级和稳定性。

## 6. 安装状态与原子性

目标状态只记录完成幂等操作所需的最小信息，例如：

```json
{
  "schema": 1,
  "channel": "stable",
  "distribution_version": "1.2.0",
  "skills_pack_version": "1.2.0",
  "hosts": {
    "codex": {
      "skills_dir": "/absolute/path",
      "owned_skills": ["my-llm-wiki", "my-llm-wiki-video"]
    }
  },
  "packs": {
    "toolchain-base": {"version": "1.1.0", "status": "healthy"},
    "asr-zh": {"version": "1.1.0", "status": "not-installed"}
  }
}
```

精确字段由实现阶段确定，但不再保存完整 inspection、selection、plan、plan hash、session
journal 和全局事务历史。

原子性缩小到具体目标：

- Skills Pack 先写入 staging，再逐个原子替换项目拥有的 skill；
- Toolchain Pack 解压到版本目录，健康检查通过后切换 `current`；
- 外来或无法证明所有权的 skill 目录永不自动覆盖；
- 一个宿主失败不要求回滚已经成功更新的另一个宿主；
- repair 等价于重新校验并重新激活当前发布制品；
- uninstall 只删除状态中证明由 Setup Core 拥有的路径，并始终保留 Wiki 与 RAW。

## 7. 下载与发布模型

每个 Browser Release manifest 至少声明：

- pack id、版本、平台和架构；
- 压缩与展开大小；
- SHA-256；
- 项目 CDN URL；
- GitHub Releases canonical URL；
- postcheck 或由 Setup Core 实现的健康检查 id；
- Provider 和 capability 列表；
- runtime/model 的兼容范围。

Runtime pack 使用独立的 `pack_version`。同一版本的全部平台制品只在
`packs-v<pack_version>` prerelease 构建、postcheck 和发布一次；后续 Browser
Release 的联合 distribution manifest 直接引用这些不可变 URL。只有完整 Python
requirements、OpenCLI npm lock、直接上游锁或 pack 构建逻辑变化时才提升
`pack_version`，已发布的 pack release 不覆盖、不追加、不重建。

Setup Core 使用统一下载策略：

```text
项目 CDN
→ 连接或传输失败时尝试 GitHub Releases
→ 两条路线均失败时返回可重试错误
```

不再先探测 GitHub、PyPI、npm 和 Hugging Face 并生成安装计划。模型下载可以有独立的
上游与中国大陆镜像，但同样由 manifest 固定声明，不修改用户全局环境。

Browser 本身使用 DMG、NSIS EXE、AppImage/DEB 和应用自身 updater；不再为同一平台
同时发布 MSI、RPM 等重复安装格式。
初次获取 Browser 可以由下载页、稳定项目 URL 或一个很薄的只读 bootstrap 指引完成；
不需要为此保留完整的 suite checkout 和 Python 安装核心。

### 7.1 统一发布组合与 update 语义

`my-llm-wiki update` 是 Setup Core 的一等能力。它不分别追逐 Browser、Skills 和各个
Runtime Pack 的“最新版本”，而是读取一个经过 CI 联合验证的 distribution manifest。
manifest 至少固定：

- distribution version 和 release channel；
- Browser 当前版本与最低兼容版本；
- Skills Pack 的精确版本和摘要；
- `toolchain-base` 的精确版本；
- 已发布可选 pack 的兼容版本；
- 数据格式或 Provider contract 的兼容范围。

命令语义保持简单：

```bash
# 只读检查，不下载或修改状态
my-llm-wiki update --check --json

# 更新 Setup Core 拥有的完整发布组合
my-llm-wiki update --json
```

首版不提供 `--skills-only`、`--toolchain-only` 或任意版本拼装参数，避免重新形成组件更新
矩阵。更新遵循以下规则：

- Skills Pack 和 `toolchain-base` 更新到 manifest 中互相验证过的版本；
- 只更新已经安装的 ASR 等按需 pack，不因 update 安装用户尚未使用的 pack；
- Browser 使用自身平台 updater。需要替换正在运行的应用时，命令返回
  `restart-required`；重启后重新运行同一个幂等 update 即可，不创建可恢复事务；
- 如果新 Skills/Toolchain 要求更高 Browser 版本，先完成 Browser 更新，旧 Setup Core
  不提前激活不兼容制品；
- 每个 pack 在独立版本目录完成校验和 postcheck 后才切换 `current`，失败时继续使用上一
  个健康版本；
- 只更新 Setup Core 能证明拥有的 skill 目录。通过开放路径安装的外来 Skills 只报告
  `not-owned`，不覆盖；
- 用户 Provider 偏好、自定义 Adapter、Wiki 和 RAW 不属于 update 的修改范围；
- 模型默认不因常规 update 重新下载。只有已安装 runtime 与现有模型不兼容时，才在下次
  使用该能力时获取兼容模型。

`status` 应报告当前 distribution version、各 owned artifact 的实际版本、健康状态和
是否存在可用更新。`repair` 只恢复当前选定 distribution 的缺失或损坏内容，不隐式升级；
升级只由 `update` 触发。

## 8. 当前职责的去向

| 当前职责 | 目标去向 |
| --- | --- |
| Agent host 检测与完整 Skills Pack 安装 | Setup Core |
| 首次 Setup、工具链状态与修复 GUI | Tauri 内置 Setup 页面 |
| Skill subset、requires、bundles 安装规划 | 删除 |
| Browser Release 下载与安装 | 标准平台安装包/下载页 |
| Runtime Pack 下载、校验、激活与联合更新 | Setup Core |
| Python/npm/系统包安装路线 | 删除 |
| Runtime Pack 构建与 SOP postcheck | CI/Release |
| Wiki 初始化和注册 | Setup Core，复用 skill 内的格式契约 |
| Provider 路由和产物校验 | Skills + 小型 Resolver |
| Browser 本地阅读、索引、分享 | Browser |
| MCP | Browser 服务端 + Setup Core CLI 的原生 stdio bridge |
| Chrome 扩展加载和第三方登录 | Browser 展示人工动作 |
| Protocol 5 inspection/selection/plan/session | 删除 |
| Windows Setup GUI/headless 双重核心 | 删除，由标准 Browser 安装包 + Setup Core 取代 |
| 全局 repair/uninstall 状态机 | Setup Core 的最小所有权操作 |

## 9. 安全与信任边界

- Browser 安装包、Skills Pack、Toolchain Pack 和模型 manifest 必须来自项目声明的来源；
- 所有不可变 pack 在激活前验证 SHA-256；发布签名可作为后续增强，但不能替代哈希；
- Setup Core 不调用 shell 字符串、`eval` 或用户 PATH 中的同名工具来安装官方 Provider；
- 自定义 Provider 必须显式配置，且调用使用 argv 数组；
- 不修改全局 pip、npm、Homebrew、apt、winget、shell profile 或代理配置；
- 只覆盖和删除能够证明由 Setup Core 拥有的目标；
- Wiki、RAW 和用户自定义 Provider 配置不随应用卸载自动删除；
- token-bearing 本地 URL 不写入日志或安装结果。

## 10. 实施记录与切换策略

v2 采用硬切换，不读取、导入或双写旧安装回执，也不保留旧命令参数与状态机的兼容层。
旧版创建的 Wiki 和 RAW 本来就是普通 Markdown/文件目录，Browser 可以直接打开；旧安装器
拥有的 Skills/运行时不被新 Core 冒充所有权。用户需要时由新 Setup 逐项识别外来 Skill，
经明确授权后备份替换。

已完成的结构性切换：

- Skills 的工具解析已改为 Provider Resolver，官方、system 与 custom Provider 共享契约；
- Browser workspace 已新增独立 Rust Setup library 与 `my-llm-wiki` CLI；
- 首次 Setup 与后续 Skills/工具链管理使用 Tauri 内置页面，不依赖本地 HTTP 服务；
- 完整 Skills Pack 在 Browser/CLI 编译时嵌入，官方 runtime 使用不可变 distribution pack；
- Browser、Skills 与已安装 pack 读取同一个 distribution manifest 完成联合更新；
- MCP stdio bridge 已进入同一 CLI，不再要求 Python 脚本或永久 suite checkout；
- 旧 bootstrap、跨平台 Python/Windows 双安装器、Setup EXE、component receipt、独立 doctor、
  Browser installer、Skills 滚动发布与对应 CI/测试已删除；
- Browser Release 只构建标准 Browser 与共享 CLI，并引用已发布的不可变
  `pack_version`；新的 pack 版本才在全部平台构建 `toolchain-base` 与按需 ASR packs。

发布前仍需完成的是跨平台 Release 实跑和真实空白环境验收，而不是兼容旧架构。

## 11. 验收标准

### 一键路径

- 在没有 Git、Python、Node、FFmpeg 和 OpenCLI 的支持平台上，只安装 Browser 即可完成
  Skills、toolchain-base 和 Wiki 初始化；
- 首次 Setup GUI 在 Wiki registry 和 `127.0.0.1` 服务尚未就绪时仍可完整工作；
- Setup 完成后可以在同一进程启动本地服务并进入 Browser，不要求用户手工寻找入口；
- 关闭和重新打开 Setup 窗口不会取消或丢失后台操作状态；
- Agent 可以通过无界面 CLI 完成正常 setup，并获得结构化终态；
- `update --check` 保持只读，`update` 将所有 owned artifacts 更新到同一个已验证发布组合；
- Browser 需要重启更新时返回 `restart-required`，重启后重跑不会重复或损坏已完成步骤；
- 项目 CDN 不可用时自动回退 GitHub Releases；
- 第一次处理有字幕视频不要求安装 ASR；用户可在 Setup 完成页显式后台预热，也可延迟到
  第一次真正需要 ASR 时再下载对应 runtime/model；
- 官方 Provider 健康时，Skill 默认选择它并完成已发布 SOP；
- 安装或修复不触碰外来 skill 目录、全局包管理器或用户 Wiki 数据；`wikis.json` 是用户的
  路由表，安装只会追加自己创建的那一条（且仅在它是唯一一条时取 `default`），修复只读不写；
  用户删掉的 Wiki 不会被修复重建，state 记录的路径失效时改为沿用注册表里仍在的 Wiki；
- Setup mutation 不能通过本地 HTTP 路由或远程 relay 调用。

### 开放路径

- Skills 可以在没有 Browser、Setup Core 和官方工具链时安装；
- 用户显式指定的 Provider 优先于官方默认；
- 自定义 Provider 产物接受同一套 RAW、字幕、来源和完整性校验；
- Provider 不满足能力或质量要求时，Skill 给出明确差异，并允许切回官方方案。

### 维护复杂度

- 只有一个 setup 语义实现供 Browser UI 与 Agent CLI 调用；
- Windows、macOS、Linux 不再拥有独立安装状态机；
- 初次安装没有 skill/component/failure-policy 的组合矩阵；
- Runtime 更新以不可变 pack 替换为主，不执行生态包管理器升级；
- 更新只解析一个 distribution manifest，不重新引入独立组件版本组合；
- 删除旧体系后，安装相关源码、测试和文档应显著少于当前 Protocol 5 体系。

## 12. 已确定的实现选择

1. `toolchain-base` 作为一次选择安装；ASR runtime/model 可在 Setup 完成页显式后台预热，
   否则延迟到首次真实需要；
2. Browser 与独立 CLI 都嵌入完整 Skills Pack，更新跟随联合 distribution；
3. CLI 名为 `my-llm-wiki`，Browser 安装到 `~/.my-llm-wiki/bin/`，Release 同时提供独立 zip；
4. Provider 配置为 `~/.my-llm-wiki/providers.json` schema 1，Setup GUI 管理长期覆盖；
5. 官方 ASR CPU 基线为中文 SenseVoice/FunASR、其他语言 faster-whisper；中文模型可在
   Setup 完成页预热并离线验收，未预热时首次使用下载；
6. 不迁移旧回执，不支持双状态；
7. MCP stdio bridge 已由共享 Rust CLI 提供；
8. Linux 继续发布 AppImage/deb，开放 Skills 路径不依赖 Browser 安装格式；
9. Setup 使用独立隐藏窗口，完成后同进程启动本地服务并切换主 Browser；
10. 当前长操作以 pack/host 原子边界保证可重入，GUI 关闭不作为取消信号；主动取消后续单独设计。
11. 安装位置在确认页一次选定（CLI 为 `setup --install-root`），默认 `~/.my-llm-wiki`。
    选了别处时 `~/.my-llm-wiki` 变成指向它的目录链接（Windows 用 junction，不需要提权），
    真实根路径写进 state。这样 Skills 进程、已注册的 MCP server 和 `wikis.json` 全部沿用
    `~/.my-llm-wiki/...` 解析，不需要为「安装在哪」新增第二套发现机制；重装系统后把新
    `~/.my-llm-wiki` 指回原目录即可复用既有 packs 与模型。目标目录必须为空或已是一份安装，
    Setup Core 不合并无关目录，也不为了满足新选择而移动或删除既有安装。
12. Skills Pack 在安装根下只展开一份（`<root>/skills/<slug>`），各宿主的
    `<host>/skills/<slug>` 是指向它的链接；无法建链接的目标回退为独立拷贝并在 state 里记
    `mode: "copy"`。卸载只摘链接，绝不跟随链接递归删除。
13. 宿主是安装后可改的：「Skills 与工具链」管理页按宿主粒度增删 Skills Pack，新增等价本地
    `add-host`（不联网、不改动 packs 与 Wiki），移除等价 `uninstall --host`（只摘链接）；
    外来 skill 一律逐项精确授权。最后一个宿主不在 GUI 里移除——那是整体卸载，仍只有 CLI
    的 `uninstall --all`。

核心边界保持不变：一键路径由 Browser/Setup Core 和官方工具链保证，开放路径由 Skills 与
Provider 契约保证；两条路径共享 SOP 和数据契约，但不共享一个强制运行时。
