//! Unified suite home under `~/.my-llm-wiki/`.
//!
//! Everything the suite persists lives under a single branded home:
//!
//! ```text
//! ~/.my-llm-wiki/
//!   wikis.json          # wiki registry / routing
//!   connector/          # server-port, token, identity.json, relay-enabled
//!   setup-state.json     # Setup Core ownership and distribution state
//!   providers.json       # user-selected capability providers
//!   packs/               # immutable official capability packs
//! ```

use std::path::PathBuf;

/// 用户家目录。`HOME` 优先（Unix 惯例，测试也用它注入假家目录）；Windows 上
/// 双击 / 开始菜单 / 自启动拉起的 GUI 进程没有 `HOME`（那是 Git Bash 会话内
/// 才有的变量），回退原生的 `USERPROFILE`——否则 token、注册表、缓存都会
/// 解析失败或落到进程 CWD。全套件的家目录解析都必须走这里。
pub fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
}

/// `~/.my-llm-wiki/`.
pub fn suite_home() -> Option<PathBuf> {
    home_dir().map(|h| h.join(".my-llm-wiki"))
}

/// `~/.my-llm-wiki/connector/` — runtime state (port, token, relay identity).
pub fn connector_dir() -> Option<PathBuf> {
    suite_home().map(|h| h.join("connector"))
}

/// `~/.my-llm-wiki/wikis.json` — the wiki registry.
pub fn registry_path() -> Option<PathBuf> {
    suite_home().map(|h| h.join("wikis.json"))
}
