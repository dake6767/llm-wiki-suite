use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};
use std::sync::{
    Arc, Mutex, RwLock, RwLockReadGuard,
    mpsc::{Receiver, RecvTimeoutError},
};
use std::time::Duration;

use axum::{
    Json, Router,
    body::Body,
    extract::{FromRequestParts, Path as AxumPath, Query, State},
    http::{HeaderValue, Request, StatusCode, header, request::Parts},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post, put},
};
use llm_wiki_core::{
    FullTextSearcher, IndexManager, PageRecord, WikiIndex, config::CoreConfig,
    indexer::build_wiki_index, parser, registry::load_registry, to_plain_text,
};
use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde::{Deserialize, Serialize};
use tokio::net::TcpListener;
use tower_http::{
    compression::{CompressionLayer, DefaultPredicate, Predicate, predicate::NotForContentType},
    cors::CorsLayer,
    services::{ServeDir, ServeFile},
    trace::TraceLayer,
};

mod mcp;
pub mod share;
pub mod skill_version;

pub use skill_version::{SkillState, SkillVersionConfig, SkillVersionInfo, SkillVersionManager};

use share::{GrantAuth, GrantStore, Scope, ShareGrant};

pub struct ServerConfig {
    pub frontend_dist: Option<PathBuf>,
    pub index_manager: IndexManager,
    pub searcher: FullTextSearcher,
    pub auth_token: String,
    /// Watch wiki markdown directories and hot-refresh the page/search indexes.
    pub watch_wikis: bool,
    /// Registry/discovery config used to hot-reload added or removed wiki repositories.
    pub registry_config: Option<CoreConfig>,
    /// 当前实际绑定的端口，供设置界面展示。
    pub port: u16,
    /// 由宿主（桌面外壳）注入：持久化端口、重启进程。`None` 时这些操作不可用（如纯浏览器/测试场景）。
    pub control: Option<ServerControl>,
    /// 由宿主注入的**本人线上入口**（含 master token 的 Owner URL）。仅供
    /// `/api/v1/config/share`（本人 agent 配置场景）使用，**绝不**用于构造分享链接。
    /// `None` 表示宿主不支持，返回空状态。
    pub owner_online_url: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>>,
    /// 由宿主注入的**无凭证公网基座** `https://<relay>/<uid>/`，是构造 Share 链接
    /// （`share/<grant_id>/#key=<secret>`）的唯一合法基座。禁止从含 master token 的
    /// URL 做字符串删除。
    /// `None`（中继未连接/宿主不支持）时分享链接回退本地 URL 并带 warning。
    pub public_base_url: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>>,
    /// grants.json 落盘位置（`~/.my-llm-wiki/connector/grants.json`）。`None`（测试/
    /// 纯浏览器无家目录）时 grant 存储纯内存、不落盘。
    pub grants_path: Option<PathBuf>,
}

/// 宿主提供的运行期控制钩子，让 HTTP 层无需依赖 Tauri 即可触发持久化与重启。
#[derive(Clone)]
pub struct ServerControl {
    pub persist_port: Arc<dyn Fn(u16) -> std::io::Result<()> + Send + Sync>,
    pub restart: Arc<dyn Fn() + Send + Sync>,
    /// 开机自启控制；宿主不支持（如纯浏览器场景）时为 `None`。
    pub autostart: Option<AutostartControl>,
    /// 应用更新控制；宿主不支持（如纯浏览器场景）时为 `None`。
    pub update: Option<UpdateControl>,
    /// 技能版本探测器（doc 21）；只读、无 mutating 端点。宿主不支持时为 `None`（前端隐藏面板）。
    pub skill_version: Option<Arc<SkillVersionManager>>,
}

/// 开机自启的读写钩子，由桌面外壳注入（错误以文案形式透传给设置页）。
#[derive(Clone)]
pub struct AutostartControl {
    pub is_enabled: Arc<dyn Fn() -> Result<bool, String> + Send + Sync>,
    pub set_enabled: Arc<dyn Fn(bool) -> Result<(), String> + Send + Sync>,
}

/// 应用更新钩子，由桌面外壳注入。检查/下载是异步长任务，这里不阻塞 HTTP 层：
/// `check`/`install` 只是触发后台任务，进展一律通过 `status` 快照轮询。
#[derive(Clone)]
pub struct UpdateControl {
    pub status: Arc<dyn Fn() -> UpdateStatus + Send + Sync>,
    pub check: Arc<dyn Fn() + Send + Sync>,
    /// 仅在 `available` 态可触发；其余状态返回 Err（文案透传给设置页）。
    pub install: Arc<dyn Fn() -> Result<(), String> + Send + Sync>,
}

/// 更新状态快照。`state` 取值：
/// `idle` | `checking` | `up-to-date` | `available` | `downloading` |
/// `ready-to-restart` | `portable` | `error`。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateStatus {
    pub current_version: String,
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latest_version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
    /// 下载进度（downloading 态）；`total` 未知时为 None。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub downloaded: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub total: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Clone)]
struct AppState {
    manager: Arc<RwLock<IndexManager>>,
    searcher: Arc<Mutex<FullTextSearcher>>,
    auth_token: Arc<String>,
    port: u16,
    control: Option<ServerControl>,
    owner_online_url: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>>,
    public_base_url: Option<Arc<dyn Fn() -> Option<String> + Send + Sync>>,
    grants: Arc<GrantStore>,
    /// 设置默认 wiki 需要改写注册表文件；`None`（如测试/无注册表场景）时该操作不可用。
    registry_config: Option<Arc<CoreConfig>>,
    _wiki_watcher: Option<Arc<Mutex<RecommendedWatcher>>>,
}

pub fn build_router(config: ServerConfig) -> Router {
    let manager = Arc::new(RwLock::new(config.index_manager));
    let searcher = Arc::new(Mutex::new(config.searcher));
    let registry_config = config.registry_config.clone();
    let wiki_watcher = if config.watch_wikis {
        start_wiki_watcher(manager.clone(), searcher.clone(), registry_config.clone())
    } else {
        None
    };
    let state = AppState {
        manager,
        searcher,
        auth_token: Arc::new(config.auth_token),
        port: config.port,
        control: config.control,
        owner_online_url: config.owner_online_url,
        public_base_url: config.public_base_url,
        grants: Arc::new(GrantStore::load(config.grants_path)),
        registry_config: registry_config.map(Arc::new),
        _wiki_watcher: wiki_watcher,
    };
    // 默认拒绝的路由结构（docs/19 §4.3）。分两个鉴权组：
    // - `shared`：Share 可达的读白名单。挂 `require_authenticated`（Owner 或有效 Share），
    //   逐路由由 `ReadableWiki` / `OwnerWiki` extractor 做 principal + 范围判定。
    // - `owner_only`：其余全部（config、share 管理、mcp、未来新增）。挂 `require_owner`，
    //   Share 一律 403。新增 API 默认落这里即对 Share 不可达——权限靠结构，不靠自觉。
    let shared = Router::new()
        .route("/healthz", get(healthz))
        .route("/session", get(get_session))
        .route("/wikis", get(list_wikis))
        // 读类内容（Owner 或范围匹配的 Share；越界 404）
        .route("/wikis/{wiki}/tree", get(wiki_tree))
        .route("/wikis/{wiki}/pages/{*path}", get(get_page))
        .route("/wikis/{wiki}/source-preview", get(get_source_preview))
        .route("/wikis/{wiki}/assets/{*name}", get(get_asset))
        .route("/wikis/{wiki}/graph", get(get_graph))
        .route("/wikis/{wiki}/search", get(search))
        // Owner-only 内容（Share → 404，不暴露存在性）
        .route("/wikis/{wiki}/raw-tree", get(raw_tree))
        .route("/wikis/{wiki}/raw/{*path}", get(get_raw))
        .route("/wikis/{wiki}/review", get(get_review))
        .route_layer(middleware::from_fn_with_state(
            state.clone(),
            require_authenticated,
        ));
    let owner_only = Router::new()
        .route("/config/wikis", get(list_config_wikis))
        .route("/config/wikis/default", put(put_default_wiki))
        .route(
            "/config/server",
            get(get_server_config).put(put_server_config),
        )
        .route("/config/share", get(get_share_config))
        .route("/config/shares", get(list_shares).post(create_share))
        .route("/config/shares/{grant_id}/link", post(share_link))
        .route("/config/shares/{grant_id}/default", post(set_default_share))
        .route(
            "/config/shares/{grant_id}",
            axum::routing::patch(patch_share).delete(delete_share),
        )
        .route(
            "/config/autostart",
            get(get_autostart_config).put(put_autostart_config),
        )
        .route("/config/update", get(get_update_config))
        .route("/config/update/check", post(post_update_check))
        .route("/config/update/install", post(post_update_install))
        .route("/config/skills", get(get_skills_config))
        .route("/config/skills/check", post(post_skills_check))
        .route("/config/skills/dismiss", post(post_skills_dismiss))
        .route("/config/restart", post(restart_server))
        .route_layer(middleware::from_fn_with_state(state.clone(), require_owner));
    let api = shared.merge(owner_only);
    // MCP 端点：Owner-only（Share → 403）。grant 双入口（§7.4）后置。注册 /mcp 与
    // /mcp/ 两个字面路径：axum 0.8 不做尾斜杠重定向，接入文档里写的是带斜杠形式。
    let mcp_routes = Router::new()
        .route(
            "/mcp",
            post(mcp::mcp_post)
                .get(mcp::mcp_method_not_allowed)
                .delete(mcp::mcp_method_not_allowed),
        )
        .route(
            "/mcp/",
            post(mcp::mcp_post)
                .get(mcp::mcp_method_not_allowed)
                .delete(mcp::mcp_method_not_allowed),
        )
        .route_layer(middleware::from_fn_with_state(state.clone(), require_owner));
    let mut app = Router::new()
        .nest("/api/v1", api)
        .merge(mcp_routes)
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    if let Some(dist) = config.frontend_dist {
        app = app.fallback_service(
            ServeDir::new(&dist).fallback(ServeFile::new(dist.join("index.html"))),
        );
    }

    // 公网经中继访问时每个资源都要走一遍 wss 往返。给带内容哈希的打包产物
    // （/assets/index-<hash>.js|css）打上 immutable 长缓存，让浏览器跨导航直接命中本地缓存，
    // 不再为静态资源反复回源；其余响应（含 index.html）标 no-cache，发版后立即拿到新哈希。
    let app = app.layer(middleware::from_fn(cache_headers));

    // 文本资源（JS/CSS/JSON/markdown）回源后即 gzip，使其在 wss 这一段以压缩字节传输，
    // 直接减小公网/本地代理链路上的体积（打包 JS ~451KB → ~140KB）。
    // 默认策略已排除图片/gRPC/SSE（SSE 必须不压缩，否则破坏流式）；再排除 text/html，
    // 因为中继要读 HTML 外壳注入 <base>，压缩会让其无法解析。
    let compression = CompressionLayer::new()
        .compress_when(DefaultPredicate::new().and(NotForContentType::const_new("text/html")));
    app.layer(compression)
}

// 按本机路径设置缓存策略（中继已剥掉 /<uid> 前缀，源站这里看到的就是 /assets、/api、/ 等）。
async fn cache_headers(request: Request<Body>, next: Next) -> Response {
    let path = request.uri().path().to_owned();
    let mut response = next.run(request).await;
    let cache_control = if path.starts_with("/assets/") {
        "public, max-age=31536000, immutable"
    } else if path.starts_with("/api/") {
        return response; // API 响应不在此统一处理，保留各 handler 自身语义。
    } else {
        "no-cache" // SPA 外壳与根文件：始终重新校验，避免缓存到旧 index.html。
    };
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static(cache_control),
    );
    response
}

pub async fn serve(listener: TcpListener, config: ServerConfig) -> std::io::Result<()> {
    let local_addr = listener.local_addr()?;
    tracing::info!("LLM-Wiki server listening on http://{local_addr}");
    axum::serve(listener, build_router(config)).await
}

const WIKI_WATCH_DEBOUNCE_MS: u64 = 700;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum WatchDepth {
    NonRecursive,
    Recursive,
}

impl WatchDepth {
    fn mode(self) -> RecursiveMode {
        match self {
            Self::NonRecursive => RecursiveMode::NonRecursive,
            Self::Recursive => RecursiveMode::Recursive,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::NonRecursive => "non-recursive",
            Self::Recursive => "recursive",
        }
    }
}

#[derive(Default)]
struct PendingChanges {
    reload_registry: bool,
    wiki_keys: BTreeSet<String>,
}

fn start_wiki_watcher(
    manager: Arc<RwLock<IndexManager>>,
    searcher: Arc<Mutex<FullTextSearcher>>,
    registry_config: Option<CoreConfig>,
) -> Option<Arc<Mutex<RecommendedWatcher>>> {
    let (tx, rx) = std::sync::mpsc::channel::<notify::Result<Event>>();
    let watcher = match notify::recommended_watcher(move |event| {
        let _ = tx.send(event);
    }) {
        Ok(watcher) => watcher,
        Err(err) => {
            tracing::warn!(error = ?err, "failed to create wiki directory watcher");
            return None;
        }
    };
    let watcher = Arc::new(Mutex::new(watcher));
    let mut watched = BTreeMap::new();
    sync_watches_from_manager(&watcher, &mut watched, &manager, registry_config.as_ref());
    if watched.is_empty() {
        return None;
    }

    let thread_watcher = watcher.clone();
    if let Err(err) = std::thread::Builder::new()
        .name("llm-wiki-watch".to_string())
        .spawn(move || {
            wiki_watch_loop(
                rx,
                manager,
                searcher,
                registry_config,
                thread_watcher,
                watched,
            )
        })
    {
        tracing::warn!(error = ?err, "failed to spawn wiki watcher thread");
        return None;
    }

    Some(watcher)
}

fn wiki_watch_loop(
    rx: Receiver<notify::Result<Event>>,
    manager: Arc<RwLock<IndexManager>>,
    searcher: Arc<Mutex<FullTextSearcher>>,
    registry_config: Option<CoreConfig>,
    watcher: Arc<Mutex<RecommendedWatcher>>,
    mut watched: BTreeMap<PathBuf, WatchDepth>,
) {
    let mut pending = PendingChanges::default();
    while let Ok(event) = rx.recv() {
        collect_watch_event(event, &manager, registry_config.as_ref(), &mut pending);

        loop {
            match rx.recv_timeout(Duration::from_millis(WIKI_WATCH_DEBOUNCE_MS)) {
                Ok(event) => {
                    collect_watch_event(event, &manager, registry_config.as_ref(), &mut pending)
                }
                Err(RecvTimeoutError::Timeout) => break,
                Err(RecvTimeoutError::Disconnected) => {
                    apply_pending_changes(
                        &pending,
                        &manager,
                        &searcher,
                        registry_config.as_ref(),
                        &watcher,
                        &mut watched,
                    );
                    return;
                }
            }
        }

        apply_pending_changes(
            &pending,
            &manager,
            &searcher,
            registry_config.as_ref(),
            &watcher,
            &mut watched,
        );
        pending = PendingChanges::default();
    }
}

fn collect_watch_event(
    event: notify::Result<Event>,
    manager: &Arc<RwLock<IndexManager>>,
    registry_config: Option<&CoreConfig>,
    pending: &mut PendingChanges,
) {
    let event = match event {
        Ok(event) => event,
        Err(err) => {
            tracing::warn!(error = ?err, "wiki watcher event error");
            return;
        }
    };
    if matches!(event.kind, EventKind::Access(_)) {
        return;
    }

    let Ok(manager) = manager.read() else {
        tracing::warn!("failed to read wiki index while handling watcher event");
        return;
    };
    if let Some(config) = registry_config
        && (event.paths.is_empty()
            || event_touches_registry(&event, config)
            || event_touches_discovery_root(&event, &manager, config))
    {
        pending.reload_registry = true;
    }
    for key in event_wiki_keys(&event, &manager) {
        pending.wiki_keys.insert(key);
    }
}

fn apply_pending_changes(
    pending: &PendingChanges,
    manager: &Arc<RwLock<IndexManager>>,
    searcher: &Arc<Mutex<FullTextSearcher>>,
    registry_config: Option<&CoreConfig>,
    watcher: &Arc<Mutex<RecommendedWatcher>>,
    watched: &mut BTreeMap<PathBuf, WatchDepth>,
) {
    if pending.reload_registry {
        if let Some(config) = registry_config {
            match reload_registry_index(config, manager, searcher) {
                Ok(()) => sync_watches_from_manager(watcher, watched, manager, Some(config)),
                Err(err) => {
                    tracing::warn!(error = %err, "failed to reload wiki registry");
                    refresh_pending_wikis(&pending.wiki_keys, manager, searcher);
                }
            }
            return;
        }
    }

    refresh_pending_wikis(&pending.wiki_keys, manager, searcher);
}

fn event_wiki_keys(event: &Event, manager: &IndexManager) -> BTreeSet<String> {
    let mut keys = BTreeSet::new();
    if event.paths.is_empty() {
        keys.extend(manager.wikis.keys().cloned());
        return keys;
    }

    for path in &event.paths {
        for idx in manager.wikis.values() {
            let wiki_dir = normalize_watch_path(idx.entry.wiki_dir.clone());
            if path_within(path, &wiki_dir) || path_within(path, &idx.entry.wiki_dir) {
                keys.insert(idx.entry.key.clone());
            }
        }
    }
    keys
}

fn event_touches_registry(event: &Event, config: &CoreConfig) -> bool {
    let registry_path = expand_home_path(&config.registry_path);
    let Some(registry_parent) = registry_path.parent() else {
        return false;
    };
    let registry_parent = normalize_watch_path(registry_parent.to_path_buf());
    let registry_path = registry_path
        .file_name()
        .map(|name| registry_parent.join(name))
        .unwrap_or(registry_path);
    event.paths.iter().any(|path| {
        path == &registry_path
            || path == &registry_parent
            || path
                .parent()
                .is_some_and(|parent| parent == registry_parent.as_path())
    })
}

fn event_touches_discovery_root(
    event: &Event,
    manager: &IndexManager,
    config: &CoreConfig,
) -> bool {
    let roots = config
        .wiki_dirs
        .iter()
        .map(expand_home_path)
        .map(normalize_watch_path)
        .filter(|root| root.is_dir())
        .collect::<Vec<_>>();
    if roots.is_empty() {
        return false;
    }

    for path in &event.paths {
        if !roots.iter().any(|root| path_within(path, root)) {
            continue;
        }
        let existing = manager.wikis.values().find(|idx| {
            let root_dir = normalize_watch_path(idx.entry.root_dir.clone());
            path_within(path, &root_dir) || path_within(path, &idx.entry.root_dir)
        });
        let Some(idx) = existing else {
            return true;
        };
        if !idx.entry.root_dir.is_dir() || !idx.entry.wiki_dir.is_dir() {
            return true;
        }
    }

    false
}

fn refresh_pending_wikis(
    keys: &BTreeSet<String>,
    manager: &Arc<RwLock<IndexManager>>,
    searcher: &Arc<Mutex<FullTextSearcher>>,
) {
    for key in keys {
        if let Err(err) = refresh_wiki_index(key, manager, searcher) {
            tracing::warn!(wiki = %key, error = %err, "failed to refresh wiki index");
        }
    }
}

fn refresh_wiki_index(
    key: &str,
    manager: &Arc<RwLock<IndexManager>>,
    searcher: &Arc<Mutex<FullTextSearcher>>,
) -> Result<(), String> {
    let entry = {
        let manager = manager
            .read()
            .map_err(|_| "wiki index lock poisoned".to_string())?;
        manager
            .get(key)
            .map(|idx| idx.entry.clone())
            .ok_or_else(|| format!("wiki not found: {key}"))?
    };

    let index = build_wiki_index(entry).map_err(|err| err.to_string())?;
    let docs = index.search_docs();
    let key = index.entry.key.clone();
    let page_count = index.pages.len();

    {
        let mut manager = manager
            .write()
            .map_err(|_| "wiki index lock poisoned".to_string())?;
        manager.wikis.insert(key.clone(), index);
    }

    searcher
        .lock()
        .map_err(|_| "search index lock poisoned".to_string())?
        .reindex_wiki(&key, &docs)
        .map_err(|err| err.to_string())?;

    tracing::info!(wiki = %key, pages = page_count, "wiki index refreshed");
    Ok(())
}

fn reload_registry_index(
    config: &CoreConfig,
    manager: &Arc<RwLock<IndexManager>>,
    searcher: &Arc<Mutex<FullTextSearcher>>,
) -> Result<(), String> {
    let entries = load_registry(config).map_err(|err| err.to_string())?;
    let next = IndexManager::build(entries.into_values()).map_err(|err| err.to_string())?;
    let docs_by_wiki = next
        .wikis
        .values()
        .map(|idx| (idx.entry.key.clone(), idx.search_docs()))
        .collect::<Vec<_>>();

    searcher
        .lock()
        .map_err(|_| "search index lock poisoned".to_string())?
        .reindex_all(
            docs_by_wiki
                .iter()
                .map(|(key, docs)| (key.as_str(), docs.as_slice())),
        )
        .map_err(|err| err.to_string())?;

    let wiki_count = next.wikis.len();
    let page_count = next
        .wikis
        .values()
        .map(|idx| idx.pages.len())
        .sum::<usize>();
    {
        let mut manager = manager
            .write()
            .map_err(|_| "wiki index lock poisoned".to_string())?;
        *manager = next;
    }

    tracing::info!(
        wikis = wiki_count,
        pages = page_count,
        "wiki registry reloaded"
    );
    Ok(())
}

fn sync_watches_from_manager(
    watcher: &Arc<Mutex<RecommendedWatcher>>,
    watched: &mut BTreeMap<PathBuf, WatchDepth>,
    manager: &Arc<RwLock<IndexManager>>,
    registry_config: Option<&CoreConfig>,
) {
    let Ok(manager) = manager.read() else {
        tracing::warn!("failed to read wiki index while syncing watcher targets");
        return;
    };
    let desired = watch_targets(&manager, registry_config);
    sync_watch_targets(watcher, watched, desired);
}

fn watch_targets(
    manager: &IndexManager,
    registry_config: Option<&CoreConfig>,
) -> BTreeMap<PathBuf, WatchDepth> {
    let mut targets = BTreeMap::new();
    for idx in manager.wikis.values() {
        insert_watch_target(
            &mut targets,
            idx.entry.wiki_dir.clone(),
            WatchDepth::Recursive,
        );
    }

    if let Some(config) = registry_config {
        let registry_path = expand_home_path(&config.registry_path);
        if let Some(parent) = registry_path.parent().filter(|parent| parent.is_dir()) {
            insert_watch_target(&mut targets, parent.to_path_buf(), WatchDepth::NonRecursive);
        }
        for root in &config.wiki_dirs {
            let root = expand_home_path(root);
            insert_watch_target(&mut targets, root, WatchDepth::Recursive);
        }
    }

    targets
        .into_iter()
        .filter(|(path, _)| path.is_dir())
        .collect()
}

fn insert_watch_target(
    targets: &mut BTreeMap<PathBuf, WatchDepth>,
    path: PathBuf,
    depth: WatchDepth,
) {
    let path = normalize_watch_path(path);
    match targets.get(&path).copied() {
        Some(WatchDepth::Recursive) => {}
        Some(WatchDepth::NonRecursive) if depth == WatchDepth::Recursive => {
            targets.insert(path, depth);
        }
        Some(_) => {}
        None => {
            targets.insert(path, depth);
        }
    }
}

fn sync_watch_targets(
    watcher: &Arc<Mutex<RecommendedWatcher>>,
    watched: &mut BTreeMap<PathBuf, WatchDepth>,
    desired: BTreeMap<PathBuf, WatchDepth>,
) {
    let stale = watched
        .iter()
        .filter_map(|(path, depth)| (desired.get(path) != Some(depth)).then_some(path.clone()))
        .collect::<Vec<_>>();
    for path in stale {
        match watcher.lock() {
            Ok(mut watcher) => {
                if let Err(err) = watcher.unwatch(&path) {
                    tracing::warn!(path = %path.display(), error = ?err, "failed to unwatch path");
                }
            }
            Err(_) => tracing::warn!("wiki watcher lock poisoned while unwatching path"),
        }
        watched.remove(&path);
    }

    for (path, depth) in desired {
        if watched.get(&path) == Some(&depth) {
            continue;
        }
        match watcher.lock() {
            Ok(mut watcher) => match watcher.watch(&path, depth.mode()) {
                Ok(()) => {
                    watched.insert(path.clone(), depth);
                    tracing::info!(
                        path = %path.display(),
                        depth = depth.label(),
                        "watching wiki path"
                    );
                }
                Err(err) => {
                    tracing::warn!(
                        path = %path.display(),
                        depth = depth.label(),
                        error = ?err,
                        "failed to watch wiki path"
                    );
                }
            },
            Err(_) => tracing::warn!("wiki watcher lock poisoned while watching path"),
        }
    }
}

fn normalize_watch_path(path: PathBuf) -> PathBuf {
    std::fs::canonicalize(&path).unwrap_or(path)
}

fn expand_home_path(path: impl AsRef<Path>) -> PathBuf {
    let path = path.as_ref();
    let Some(text) = path.to_str() else {
        return path.to_path_buf();
    };
    if text == "~" {
        return llm_wiki_core::paths::home_dir().unwrap_or_else(|| path.to_path_buf());
    }
    if let Some(rest) = text.strip_prefix("~/") {
        return llm_wiki_core::paths::home_dir()
            .unwrap_or_else(|| PathBuf::from("~"))
            .join(rest);
    }
    path.to_path_buf()
}

fn path_within(path: &Path, root: &Path) -> bool {
    path == root || path.starts_with(root)
}

#[derive(Debug, Serialize)]
struct Healthz {
    ok: bool,
}

async fn healthz() -> Json<Healthz> {
    Json(Healthz { ok: true })
}

fn read_manager(state: &AppState) -> Result<RwLockReadGuard<'_, IndexManager>, ApiError> {
    state
        .manager
        .read()
        .map_err(|_| ApiError::internal("索引不可用"))
}

/// 两类 principal（docs/19 §0）。经中间件放入请求扩展，供 extractor / handler 取用。
#[derive(Clone)]
enum Principal {
    Owner,
    Share(ShareGrant),
}

/// 凭证解析结果。`Unauthorized` 涵盖无凭证、master 不符、share 无效/过期/撤销——
/// HTTP 层一律 401。
enum PrincipalOutcome {
    Owner,
    Share(ShareGrant),
    Unauthorized,
}

/// 凭证解析：**先**与 master token 常数时间比对（`LLM_WIKI_WEB_TOKEN` 可自定义，
/// 不能假设不以 `s_` 开头、不含 `.`，所以必须整串先比），未命中再按 share_token
/// 语法 `<grant_id>.<secret>` 拆分、按 grant_id 索引 + secret 常数时间比对。
fn resolve_principal(state: &AppState, request: &Request<Body>) -> PrincipalOutcome {
    let expected = state.auth_token.trim();
    let presented = presented_token(request);

    if !expected.is_empty() {
        if let Some(p) = presented.as_deref()
            && share::ct_eq(p.as_bytes(), expected.as_bytes())
        {
            return PrincipalOutcome::Owner;
        }
    } else if !is_relay_request(request) {
        // master token 为空：本机直连视为 Owner（保留既有开发/本地行为）。中继请求
        // 则不给 Owner，落到 share 解析。
        return PrincipalOutcome::Owner;
    }

    if let Some(p) = presented.as_deref()
        && let Some((grant_id, secret)) = share::split_share_token(p)
    {
        return match state.grants.resolve(grant_id, secret, share::now_ts()) {
            GrantAuth::Valid(grant) => PrincipalOutcome::Share(grant),
            GrantAuth::Expired | GrantAuth::Invalid | GrantAuth::NotFound => {
                PrincipalOutcome::Unauthorized
            }
        };
    }

    PrincipalOutcome::Unauthorized
}

/// 鉴权中间件（Share 可达组）：Owner 或有效 Share 放行，principal 入请求扩展；
/// 其余 401。范围判定推迟到各 handler 的 extractor。
async fn require_authenticated(
    State(state): State<AppState>,
    mut request: Request<Body>,
    next: Next,
) -> Result<Response, ApiError> {
    match resolve_principal(&state, &request) {
        PrincipalOutcome::Owner => {
            request.extensions_mut().insert(Principal::Owner);
            Ok(next.run(request).await)
        }
        PrincipalOutcome::Share(grant) => {
            request.extensions_mut().insert(Principal::Share(grant));
            Ok(next.run(request).await)
        }
        PrincipalOutcome::Unauthorized => Err(ApiError::unauthorized()),
    }
}

/// 鉴权中间件（Owner-only 组）：仅 Owner 放行；已认证的 Share → 403（认证成功但
/// 无权）；无凭证 → 401。
async fn require_owner(
    State(state): State<AppState>,
    mut request: Request<Body>,
    next: Next,
) -> Result<Response, ApiError> {
    match resolve_principal(&state, &request) {
        PrincipalOutcome::Owner => {
            request.extensions_mut().insert(Principal::Owner);
            Ok(next.run(request).await)
        }
        PrincipalOutcome::Share(_) => Err(ApiError::forbidden()),
        PrincipalOutcome::Unauthorized => Err(ApiError::unauthorized()),
    }
}

/// 从请求扩展取 principal（中间件已放入；缺失说明路由未挂鉴权层，属编码错误→401）。
fn principal_from_parts(parts: &Parts) -> Result<Principal, ApiError> {
    parts
        .extensions
        .get::<Principal>()
        .cloned()
        .ok_or_else(ApiError::unauthorized)
}

/// 从路径参数取 `{wiki}`。用 map 反序列化以兼容 `{wiki}` 与 `{wiki}/{*path}` 两种路由。
async fn wiki_path_param<S: Send + Sync>(parts: &mut Parts, state: &S) -> Result<String, ApiError> {
    let params =
        AxumPath::<std::collections::HashMap<String, String>>::from_request_parts(parts, state)
            .await
            .map_err(|_| ApiError::not_found("wiki 不存在"))?;
    params
        .0
        .get("wiki")
        .cloned()
        .ok_or_else(|| ApiError::not_found("wiki 不存在"))
}

/// 读类内容的授权守卫：Owner 通过；Share 仅当 `grant.wiki == 路径 wiki` 通过，
/// 否则 404（不确认存在性）。
struct ReadableWiki {
    #[allow(dead_code)]
    wiki: String,
}

impl<S> FromRequestParts<S> for ReadableWiki
where
    S: Send + Sync,
{
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        let principal = principal_from_parts(parts)?;
        let wiki = wiki_path_param(parts, state).await?;
        match principal {
            Principal::Owner => Ok(ReadableWiki { wiki }),
            Principal::Share(grant) if grant.wiki == wiki => Ok(ReadableWiki { wiki }),
            Principal::Share(_) => Err(ApiError::not_found("wiki 不存在")),
        }
    }
}

/// Owner-only 内容的授权守卫（raw / raw-tree / review）：Owner 通过；Share 无条件
/// 404——RAW/review 结构性不在分享范围内，且不向访客确认其存在。
struct OwnerWiki {
    wiki: String,
}

impl<S> FromRequestParts<S> for OwnerWiki
where
    S: Send + Sync,
{
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        let principal = principal_from_parts(parts)?;
        match principal {
            Principal::Owner => {
                let wiki = wiki_path_param(parts, state).await?;
                Ok(OwnerWiki { wiki })
            }
            Principal::Share(_) => Err(ApiError::not_found("wiki 不存在")),
        }
    }
}

/// 直接取当前 principal（用于 `/wikis` 过滤、`/session`）。
struct CurrentPrincipal(Principal);

impl<S> FromRequestParts<S> for CurrentPrincipal
where
    S: Send + Sync,
{
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        principal_from_parts(parts).map(CurrentPrincipal)
    }
}

fn is_relay_request(request: &Request<Body>) -> bool {
    request
        .headers()
        .get("x-llm-wiki-relay")
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value == "1")
}

fn presented_token(request: &Request<Body>) -> Option<String> {
    if let Some(token) = query_token(request.uri().query()) {
        return Some(token);
    }
    let authorization = request
        .headers()
        .get(header::AUTHORIZATION)?
        .to_str()
        .ok()?;
    authorization
        .strip_prefix("Bearer ")
        .map(|token| token.trim().to_string())
}

fn query_token(query: Option<&str>) -> Option<String> {
    let query = query?;
    query.split('&').find_map(|part| {
        let (key, value) = part.split_once('=')?;
        (key == "token").then(|| {
            urlencoding::decode(value)
                .map(|value| value.into_owned())
                .unwrap_or_else(|_| value.to_string())
        })
    })
}

async fn list_wikis(
    State(state): State<AppState>,
    CurrentPrincipal(principal): CurrentPrincipal,
) -> Result<Json<Vec<WikiInfo>>, ApiError> {
    let manager = read_manager(&state)?;
    // Share principal 只见到自己被授权的那个库。
    let allowed: Option<String> = match &principal {
        Principal::Owner => None,
        Principal::Share(grant) => Some(grant.wiki.clone()),
    };
    let mut out = manager
        .wikis
        .values()
        .filter(|idx| allowed.as_deref().is_none_or(|w| idx.entry.key == w))
        .map(|idx| WikiInfo {
            key: idx.entry.key.clone(),
            name: idx.entry.name.clone(),
            description: idx.entry.description.clone().unwrap_or_default(),
            default: idx.entry.default,
            page_count: idx.pages.len(),
        })
        .collect::<Vec<_>>();
    out.sort_by(|a, b| {
        (std::cmp::Reverse(a.default), &a.name).cmp(&(std::cmp::Reverse(b.default), &b.name))
    });
    Ok(Json(out))
}

async fn list_config_wikis(
    State(state): State<AppState>,
) -> Result<Json<Vec<WikiConfigInfo>>, ApiError> {
    let manager = read_manager(&state)?;
    let mut out = manager
        .wikis
        .values()
        .map(|idx| WikiConfigInfo {
            key: idx.entry.key.clone(),
            name: idx.entry.name.clone(),
            description: idx.entry.description.clone().unwrap_or_default(),
            default: idx.entry.default,
            page_count: idx.pages.len(),
            root_dir: path_label(&idx.entry.root_dir),
            wiki_dir: path_label(&idx.entry.wiki_dir),
            raw_dir: path_label(&idx.entry.raw_dir),
            assets_dir: path_label(&idx.entry.assets_dir),
        })
        .collect::<Vec<_>>();
    out.sort_by(|a, b| {
        (std::cmp::Reverse(a.default), &a.name).cmp(&(std::cmp::Reverse(b.default), &b.name))
    });
    Ok(Json(out))
}

/// 设置默认 wiki：只改注册表文件里的 `default` 字段（未登记的 wiki 会补一条），
/// 写完同步重载索引，响应直接返回新列表。文件变更还会触发 watcher 再重载一次，
/// 幂等无害。
async fn put_default_wiki(
    State(state): State<AppState>,
    Json(update): Json<DefaultWikiUpdate>,
) -> Result<Json<Vec<WikiConfigInfo>>, ApiError> {
    let Some(config) = state.registry_config.clone() else {
        return Err(ApiError::not_supported("当前环境不支持修改默认 WIKI"));
    };
    let root_dir = {
        let manager = read_manager(&state)?;
        manager
            .wikis
            .values()
            .find(|idx| idx.entry.key == update.key)
            .map(|idx| idx.entry.root_dir.clone())
            .ok_or(ApiError::not_found("wiki 不存在"))?
    };
    write_registry_default(&config, &root_dir).map_err(|err| {
        tracing::warn!(error = %err, "failed to write wiki registry");
        ApiError::internal("写入注册表失败")
    })?;
    reload_registry_index(&config, &state.manager, &state.searcher).map_err(|err| {
        tracing::warn!(error = %err, "failed to reload registry after default change");
        ApiError::internal("重载注册表失败")
    })?;
    list_config_wikis(State(state)).await
}

/// 把注册表文件里 `root` 对应的条目标成唯一 default。经 `serde_json::Value` 读改写，
/// 未涉及的字段与条目原样保留（注册表也被 skill 脚本读写，不能丢字段）。
fn write_registry_default(config: &CoreConfig, root: &Path) -> Result<(), String> {
    let registry_path = expand_home_path(&config.registry_path);
    let mut data: serde_json::Value = match std::fs::read_to_string(&registry_path) {
        Ok(text) => serde_json::from_str(&text).map_err(|err| err.to_string())?,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => serde_json::json!({}),
        Err(err) => return Err(err.to_string()),
    };
    let Some(obj) = data.as_object_mut() else {
        return Err("注册表根节点不是 JSON 对象".to_string());
    };
    let wikis = obj
        .entry("wikis")
        .or_insert_with(|| serde_json::Value::Array(Vec::new()));
    let Some(items) = wikis.as_array_mut() else {
        return Err("注册表 wikis 字段不是数组".to_string());
    };

    let mut found = false;
    for item in items.iter_mut() {
        let Some(entry) = item.as_object_mut() else {
            continue;
        };
        let is_target = entry
            .get("path")
            .and_then(|path| path.as_str())
            .is_some_and(|path| expand_home_path(path) == root);
        if is_target {
            found = true;
            entry.insert("default".to_string(), serde_json::Value::Bool(true));
        } else if entry
            .get("default")
            .and_then(|value| value.as_bool())
            .unwrap_or(false)
        {
            entry.insert("default".to_string(), serde_json::Value::Bool(false));
        }
    }
    if !found {
        items.push(serde_json::json!({
            "path": root.to_string_lossy(),
            "default": true,
        }));
    }

    if let Some(parent) = registry_path.parent() {
        std::fs::create_dir_all(parent).map_err(|err| err.to_string())?;
    }
    let text = serde_json::to_string_pretty(&data).map_err(|err| err.to_string())?;
    std::fs::write(&registry_path, text + "\n").map_err(|err| err.to_string())
}

async fn get_server_config(State(state): State<AppState>) -> Json<ServerConfigInfo> {
    Json(ServerConfigInfo { port: state.port })
}

async fn get_share_config(State(state): State<AppState>) -> Json<ShareConfigInfo> {
    // 本人线上入口（含 master token）。仅本端点使用，绝不进入分享链接构造。
    let online_url = state
        .owner_online_url
        .as_ref()
        .and_then(|provider| provider());
    Json(ShareConfigInfo {
        relay_connected: online_url.is_some(),
        online_url,
    })
}

/// 前端进访客模式的依据：owner 或 guest（附授权的 wiki）。
async fn get_session(CurrentPrincipal(principal): CurrentPrincipal) -> Json<SessionInfo> {
    match principal {
        Principal::Owner => Json(SessionInfo {
            principal: "owner",
            wiki: None,
        }),
        Principal::Share(grant) => Json(SessionInfo {
            principal: "guest",
            wiki: Some(grant.wiki),
        }),
    }
}

// ---- grant 管理 API（Owner-only；路径/列表/日志永不含 secret，secret 只出现在
// create 与 link 两个 ★ 响应里，且这两个响应带 no-store）----

fn grant_view(grant: &ShareGrant, now: i64, is_default: bool) -> ShareGrantView {
    ShareGrantView {
        grant_id: grant.grant_id.clone(),
        label: grant.label.clone(),
        wiki: grant.wiki.clone(),
        scope: grant.scope.clone(),
        include_raw: grant.include_raw,
        created_at: grant.created_at,
        expires_at: grant.expires_at,
        last_accessed: grant.last_accessed,
        revoked: grant.revoked,
        active: grant.is_active(now),
        is_default: is_default && grant.is_active(now),
    }
}

/// 从**无凭证**的 `public_base_url` 拼稳定 Guest namespace：
/// `share/<grant_id>/#key=<secret>`。grant_id 可安全进入请求路径；secret 只进 fragment，
/// 页面导航不会把它发给 relay / Cloudflare。中继未连接时回退本地 URL并带 warning。
fn build_share_link(state: &AppState, grant_id: &str, secret: &str) -> (String, Option<String>) {
    if let Some(provider) = &state.public_base_url
        && let Some(base) = provider()
    {
        let base = if base.ends_with('/') {
            base
        } else {
            format!("{base}/")
        };
        return (format!("{base}share/{grant_id}/#key={secret}"), None);
    }
    (
        format!(
            "http://127.0.0.1:{}/share/{grant_id}/#key={secret}",
            state.port
        ),
        Some(
            "线上访问未开启，此链接暂时仅本机可用；开启中继后请在管理面板重新复制链接。"
                .to_string(),
        ),
    )
}

/// 给含 secret 的响应加 no-store（POST 本身不保证不被缓存，真正约束来自 no-store）。
fn no_store_json<T: Serialize>(value: T) -> Response {
    let mut response = Json(value).into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store, private"),
    );
    headers.insert(header::PRAGMA, HeaderValue::from_static("no-cache"));
    response
}

async fn list_shares(State(state): State<AppState>) -> Json<Vec<ShareGrantView>> {
    let now = share::now_ts();
    let defaults = state.grants.default_grants();
    let mut out = state
        .grants
        .list()
        .iter()
        .map(|grant| {
            let is_default = defaults
                .get(&grant.wiki)
                .is_some_and(|grant_id| grant_id == &grant.grant_id);
            grant_view(grant, now, is_default)
        })
        .collect::<Vec<_>>();
    out.sort_by_key(|g| std::cmp::Reverse(g.created_at));
    Json(out)
}

/// ★ 创建 grant → 返回完整分享链接（含 secret）。响应带 no-store。
async fn create_share(
    State(state): State<AppState>,
    Json(req): Json<CreateShareRequest>,
) -> Result<Response, ApiError> {
    let wiki = req.wiki.trim().to_string();
    if wiki.is_empty() {
        return Err(ApiError::bad_request("缺少 wiki"));
    }
    {
        let manager = read_manager(&state)?;
        if manager.get(&wiki).is_none() {
            return Err(ApiError::not_found("wiki 不存在"));
        }
    }
    let label = {
        let trimmed = req.label.trim();
        if trimmed.is_empty() {
            format!("分享 {wiki}")
        } else {
            trimmed.to_string()
        }
    };
    let expires_at = req
        .expires_in_days
        .map(|days| share::now_ts() + i64::from(days) * 86_400);
    let grant = state
        .grants
        .create(wiki, label, expires_at, req.make_default)
        .map_err(|_| ApiError::internal("写入 grants 失败"))?;
    // 日志只记 grant_id，绝不记 secret / 链接。
    tracing::info!(grant_id = %grant.grant_id, wiki = %grant.wiki, "created share grant");
    let (link, warning) = build_share_link(&state, &grant.grant_id, &grant.secret);
    Ok(no_store_json(ShareLinkResponse {
        grant_id: grant.grant_id,
        wiki: grant.wiki,
        label: grant.label,
        expires_at: grant.expires_at,
        link,
        warning,
    }))
}

/// ★ 再次取完整链接（管理界面「复制链接」的后端）。已撤销 → 410。响应带 no-store。
async fn share_link(
    State(state): State<AppState>,
    AxumPath(grant_id): AxumPath<String>,
) -> Result<Response, ApiError> {
    let grant = state
        .grants
        .get(&grant_id)
        .ok_or_else(|| ApiError::not_found("分享不存在"))?;
    if grant.revoked || grant.secret.is_empty() {
        return Err(ApiError::gone());
    }
    let (link, warning) = build_share_link(&state, &grant.grant_id, &grant.secret);
    Ok(no_store_json(ShareLinkResponse {
        grant_id: grant.grant_id,
        wiki: grant.wiki,
        label: grant.label,
        expires_at: grant.expires_at,
        link,
        warning,
    }))
}

/// 把一条有效分享设为所属 Wiki 的通用分享。同一 Wiki 旧的通用项会被替换。
async fn set_default_share(
    State(state): State<AppState>,
    AxumPath(grant_id): AxumPath<String>,
) -> Result<Json<ShareGrantView>, ApiError> {
    let existing = state
        .grants
        .get(&grant_id)
        .ok_or_else(|| ApiError::not_found("分享不存在"))?;
    if !existing.is_active(share::now_ts()) {
        return Err(ApiError::bad_request("只有生效中的分享可以设为通用分享"));
    }
    let updated = state
        .grants
        .set_default(&grant_id, share::now_ts())
        .map_err(|_| ApiError::internal("写入 grants 失败"))?
        .ok_or_else(|| ApiError::bad_request("分享已经失效，请刷新后重试"))?;
    tracing::info!(
        grant_id = %updated.grant_id,
        wiki = %updated.wiki,
        "set default share grant"
    );
    Ok(Json(grant_view(&updated, share::now_ts(), true)))
}

/// 续期/改期。`expires_in_days = null` 表示改为永久。
async fn patch_share(
    State(state): State<AppState>,
    AxumPath(grant_id): AxumPath<String>,
    Json(req): Json<PatchShareRequest>,
) -> Result<Json<ShareGrantView>, ApiError> {
    let expires_at = req
        .expires_in_days
        .map(|days| share::now_ts() + i64::from(days) * 86_400);
    let updated = state
        .grants
        .renew(&grant_id, expires_at)
        .map_err(|_| ApiError::internal("写入 grants 失败"))?;
    match updated {
        Some(grant) => {
            let is_default = state
                .grants
                .default_grants()
                .get(&grant.wiki)
                .is_some_and(|grant_id| grant_id == &grant.grant_id);
            Ok(Json(grant_view(&grant, share::now_ts(), is_default)))
        }
        None => Err(ApiError::not_found("分享不存在")),
    }
}

/// 撤销：置 revoked + 清空 secret。返回更新后的元数据（不含 secret）。
async fn delete_share(
    State(state): State<AppState>,
    AxumPath(grant_id): AxumPath<String>,
) -> Result<Json<ShareGrantView>, ApiError> {
    let revoked = state
        .grants
        .revoke(&grant_id)
        .map_err(|_| ApiError::internal("写入 grants 失败"))?;
    match revoked {
        Some(grant) => {
            tracing::info!(grant_id = %grant.grant_id, "revoked share grant");
            Ok(Json(grant_view(&grant, share::now_ts(), false)))
        }
        None => Err(ApiError::not_found("分享不存在")),
    }
}

/// 持久化新的本地服务端口。仅写入配置，不影响当前进程——需重启生效。
async fn put_server_config(
    State(state): State<AppState>,
    Json(update): Json<ServerConfigUpdate>,
) -> Result<Json<ServerConfigUpdateResult>, ApiError> {
    if update.port < 1024 {
        return Err(ApiError::bad_request("端口需在 1024-65535 之间"));
    }
    let control = state
        .control
        .as_ref()
        .ok_or(ApiError::not_supported("当前环境不支持修改端口"))?;
    (control.persist_port)(update.port).map_err(|_| ApiError::internal("写入端口配置失败"))?;
    Ok(Json(ServerConfigUpdateResult {
        port: update.port,
        restart_required: update.port != state.port,
    }))
}

/// 读取开机自启状态。宿主未注入钩子时 `supported: false`，设置页据此隐藏开关。
async fn get_autostart_config(
    State(state): State<AppState>,
) -> Result<Json<AutostartConfigInfo>, ApiError> {
    let Some(autostart) = state.control.as_ref().and_then(|c| c.autostart.as_ref()) else {
        return Ok(Json(AutostartConfigInfo {
            supported: false,
            enabled: false,
        }));
    };
    let enabled =
        (autostart.is_enabled)().map_err(|_| ApiError::internal("读取开机自启状态失败"))?;
    Ok(Json(AutostartConfigInfo {
        supported: true,
        enabled,
    }))
}

/// 开关开机自启，立即生效（写系统级 LaunchAgent/注册表，由宿主实现）。
async fn put_autostart_config(
    State(state): State<AppState>,
    Json(update): Json<AutostartUpdate>,
) -> Result<Json<AutostartConfigInfo>, ApiError> {
    let autostart = state
        .control
        .as_ref()
        .and_then(|c| c.autostart.as_ref())
        .ok_or(ApiError::not_supported("当前环境不支持开机自启"))?;
    (autostart.set_enabled)(update.enabled)
        .map_err(|_| ApiError::internal("写入开机自启配置失败"))?;
    Ok(Json(AutostartConfigInfo {
        supported: true,
        enabled: update.enabled,
    }))
}

/// 读取更新状态。宿主未注入钩子（纯浏览器/portable 之外的 web-only 场景）时
/// `supported: false`，设置页据此隐藏更新面板。
async fn get_update_config(State(state): State<AppState>) -> Json<UpdateConfigInfo> {
    let Some(update) = state.control.as_ref().and_then(|c| c.update.as_ref()) else {
        return Json(UpdateConfigInfo {
            supported: false,
            status: None,
        });
    };
    Json(UpdateConfigInfo {
        supported: true,
        status: Some((update.status)()),
    })
}

/// 触发一次后台更新检查（幂等：检查/下载进行中时宿主自行忽略），随后轮询 GET。
async fn post_update_check(
    State(state): State<AppState>,
) -> Result<Json<UpdateConfigInfo>, ApiError> {
    let update = state
        .control
        .as_ref()
        .and_then(|c| c.update.as_ref())
        .ok_or(ApiError::not_supported("当前环境不支持应用更新"))?;
    (update.check)();
    Ok(Json(UpdateConfigInfo {
        supported: true,
        status: Some((update.status)()),
    }))
}

/// 触发下载并安装（仅 available 态）。完成后状态变为 ready-to-restart，
/// 由设置页引导用户调 /config/restart。
async fn post_update_install(
    State(state): State<AppState>,
) -> Result<Json<UpdateConfigInfo>, ApiError> {
    let update = state
        .control
        .as_ref()
        .and_then(|c| c.update.as_ref())
        .ok_or(ApiError::not_supported("当前环境不支持应用更新"))?;
    (update.install)().map_err(|_| ApiError::bad_request("当前没有可安装的更新"))?;
    Ok(Json(UpdateConfigInfo {
        supported: true,
        status: Some((update.status)()),
    }))
}

// ——— 技能版本探测（doc 21）：owner-only、只读、无 mutating 端点 ———

/// 读取当前技能版本快照。宿主未注入 manager 时 `supported:false`，前端隐藏面板。
async fn get_skills_config(State(state): State<AppState>) -> Json<SkillsConfigInfo> {
    let Some(manager) = state
        .control
        .as_ref()
        .and_then(|c| c.skill_version.as_ref())
    else {
        return Json(SkillsConfigInfo {
            supported: false,
            info: None,
        });
    };
    Json(SkillsConfigInfo {
        supported: true,
        info: Some(manager.status()),
    })
}

/// 强制刷新一次（解析源 + 拉取校验 latest），返回新快照。
async fn post_skills_check(
    State(state): State<AppState>,
) -> Result<Json<SkillsConfigInfo>, ApiError> {
    let manager = state
        .control
        .as_ref()
        .and_then(|c| c.skill_version.as_ref())
        .ok_or(ApiError::not_supported("当前环境不支持技能版本检查"))?
        .clone();
    let info = manager.check().await;
    Ok(Json(SkillsConfigInfo {
        supported: true,
        info: Some(info),
    }))
}

/// 「本版本不再提醒」。仅写等值去重锚，不触发任何文件系统写入（技能侧）。
async fn post_skills_dismiss(
    State(state): State<AppState>,
    Json(body): Json<SkillsDismiss>,
) -> Result<Json<SkillsConfigInfo>, ApiError> {
    let manager = state
        .control
        .as_ref()
        .and_then(|c| c.skill_version.as_ref())
        .ok_or(ApiError::not_supported("当前环境不支持技能版本检查"))?;
    manager.dismiss(&body.version);
    Ok(Json(SkillsConfigInfo {
        supported: true,
        info: Some(manager.status()),
    }))
}

/// 重启宿主进程，使新端口生效。
async fn restart_server(State(state): State<AppState>) -> Result<Json<RestartResult>, ApiError> {
    let control = state
        .control
        .as_ref()
        .ok_or(ApiError::not_supported("当前环境不支持重启"))?;
    (control.restart)();
    Ok(Json(RestartResult { ok: true }))
}

async fn wiki_tree(
    State(state): State<AppState>,
    ReadableWiki { wiki }: ReadableWiki,
) -> Result<Json<Vec<TreeNode>>, ApiError> {
    let manager = read_manager(&state)?;
    let idx = manager
        .get(&wiki)
        .ok_or(ApiError::not_found("wiki 不存在"))?;
    let mut buckets: BTreeMap<String, Vec<PageRef>> = BTreeMap::new();
    for rec in idx.pages.values() {
        let top = rec
            .path
            .split_once('/')
            .map(|(head, _)| head)
            .unwrap_or("_root")
            .to_string();
        buckets.entry(top).or_default().push(page_ref(rec));
    }
    let mut nodes = buckets
        .into_iter()
        .map(|(page_type, mut pages)| {
            pages.sort_by(|a, b| a.title.cmp(&b.title));
            TreeNode {
                page_type,
                count: pages.len(),
                pages,
            }
        })
        .collect::<Vec<_>>();
    nodes.sort_by(|a, b| type_order(&a.page_type).cmp(&type_order(&b.page_type)));
    Ok(Json(nodes))
}

/// RAW 是一层内容集合，而不是把 `sources/x/`、`sources/wechat/` 等磁盘目录
/// 原样暴露为导航层级。这里递归收集所有 Markdown，返回一个扁平的 RAW 节点。
async fn raw_tree(
    State(state): State<AppState>,
    OwnerWiki { wiki }: OwnerWiki,
) -> Result<Json<TreeNode>, ApiError> {
    let manager = read_manager(&state)?;
    let idx = manager
        .get(&wiki)
        .ok_or(ApiError::not_found("wiki 不存在"))?;
    let raw_root = &idx.entry.raw_dir;
    let mut files = Vec::new();
    collect_raw_markdown(raw_root, &mut files)
        .map_err(|_| ApiError::internal("读取 RAW 索引失败"))?;

    let mut pages = files
        .into_iter()
        .filter_map(|path| raw_page_ref(raw_root, &path))
        .collect::<Vec<_>>();
    pages.sort_by(|a, b| a.title.cmp(&b.title));
    Ok(Json(TreeNode {
        page_type: "raw".to_string(),
        count: pages.len(),
        pages,
    }))
}

async fn get_page(
    State(state): State<AppState>,
    _authz: ReadableWiki,
    AxumPath((wiki, path)): AxumPath<(String, String)>,
) -> Result<Json<Page>, ApiError> {
    let manager = read_manager(&state)?;
    let idx = manager
        .get(&wiki)
        .ok_or(ApiError::not_found("wiki 不存在"))?;
    let path = parser::normalize_page_path(&path);
    let rec = idx
        .pages
        .get(&path)
        .ok_or(ApiError::not_found("页面不存在"))?;
    let body = parser::rewrite_body(&rec.body, &wiki, |target| {
        idx.resolve(target).map(ToString::to_string)
    });
    let outgoing_links = outgoing_links(idx, rec);
    let backlinks = idx
        .backlinks
        .get(&path)
        .into_iter()
        .flatten()
        .filter_map(|path| idx.pages.get(path))
        .map(page_ref)
        .collect();

    Ok(Json(Page {
        wiki,
        path: rec.path.clone(),
        slug: rec.slug.clone(),
        title: rec.title.clone(),
        page_type: rec.page_type.clone(),
        frontmatter: rec.frontmatter.clone(),
        body,
        outgoing_links,
        backlinks,
        sources: rec.sources.clone(),
        source_pages: Vec::new(),
        tags: rec.tags.clone(),
    }))
}

/// Share 可达的受限来源预览：只有 `source` 确实出现在可读编译页 `page` 的
/// frontmatter.sources 中才返回。它不提供 RAW 目录，也不能替代 Owner-only `/raw/*`。
async fn get_source_preview(
    State(state): State<AppState>,
    _authz: ReadableWiki,
    AxumPath(wiki): AxumPath<String>,
    Query(query): Query<SourcePreviewQuery>,
) -> Result<Json<Page>, ApiError> {
    let manager = read_manager(&state)?;
    let idx = manager
        .get(&wiki)
        .ok_or(ApiError::not_found("wiki 不存在"))?;
    let page_path = parser::normalize_page_path(&query.page);
    let page = idx
        .pages
        .get(&page_path)
        .ok_or(ApiError::not_found("页面不存在"))?;
    let requested = canonical_raw_source(&query.source).ok_or(ApiError::not_found("来源不存在"))?;
    let referenced = page
        .sources
        .iter()
        .filter_map(|source| canonical_raw_source(source))
        .any(|source| source == requested);
    if !referenced {
        return Err(ApiError::not_found("来源不存在"));
    }
    Ok(Json(load_raw_page(idx, &wiki, &requested)?))
}

/// 与前端来源入口一致地规范化 RAW 引用，用于做授权比较。返回值不等于放行：最终磁盘
/// 读取仍必须经过 `safe_join`，所以 `..` / 绝对路径无法越界。
fn canonical_raw_source(source: &str) -> Option<String> {
    let source = source.trim().trim_matches('/');
    let source = source.strip_prefix("raw/").unwrap_or(source);
    let source = source
        .strip_suffix(".md")
        .or_else(|| source.strip_suffix(".MD"))
        .unwrap_or(source);
    if source.is_empty() {
        return None;
    }
    Some(
        if source.starts_with("sources/") || source.starts_with("assets/") {
            source.to_string()
        } else {
            format!("sources/{source}")
        },
    )
}

/// 找出由某条 RAW 编译出的 Wiki 来源摘要页。概念、实体等页面也可能在
/// frontmatter.sources 中引用同一 RAW，但「概」入口只代表 wiki/sources 层。
fn wiki_source_pages_for_raw(idx: &WikiIndex, raw_source: &str) -> Vec<PageRef> {
    let Some(requested) = canonical_raw_source(raw_source) else {
        return Vec::new();
    };

    idx.pages
        .values()
        .filter(|page| {
            matches!(page.page_type.as_str(), "source" | "sources")
                || page.path.starts_with("sources/")
        })
        .filter(|page| {
            page.sources
                .iter()
                .filter_map(|source| canonical_raw_source(source))
                .any(|source| source == requested)
        })
        .map(page_ref)
        .collect()
}

/// 读取 `raw/` 下的原始源（含图片重写），用于 Owner 的完整「溯源」查看。
async fn get_raw(
    State(state): State<AppState>,
    _authz: OwnerWiki,
    AxumPath((wiki, path)): AxumPath<(String, String)>,
) -> Result<Json<Page>, ApiError> {
    let manager = read_manager(&state)?;
    let idx = manager
        .get(&wiki)
        .ok_or(ApiError::not_found("wiki 不存在"))?;
    Ok(Json(load_raw_page(idx, &wiki, &path)?))
}

fn load_raw_page(idx: &WikiIndex, wiki: &str, path: &str) -> Result<Page, ApiError> {
    let raw_root = &idx.entry.raw_dir;
    let rel = path.strip_prefix("raw/").unwrap_or(path);

    let mut target = safe_join(raw_root, rel).ok_or(ApiError::bad_request("非法路径"))?;
    if !target.is_file() && !rel.ends_with(".md") {
        // wikilink 风格无扩展名
        target =
            safe_join(raw_root, &format!("{rel}.md")).ok_or(ApiError::bad_request("非法路径"))?;
    }
    if !target.is_file() {
        return Err(ApiError::not_found("原始源不存在"));
    }

    let parsed = parser::parse_file(&target).map_err(|_| ApiError::internal("读取原始源失败"))?;
    let body = parser::rewrite_body(&parsed.body, wiki, |target| {
        idx.resolve(target).map(ToString::to_string)
    });
    let rel_posix = target
        .strip_prefix(raw_root)
        .unwrap_or(&target)
        .components()
        .filter_map(|component| match component {
            Component::Normal(part) => Some(part.to_string_lossy().into_owned()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("/");
    let stem = target
        .file_stem()
        .and_then(|stem| stem.to_str())
        .unwrap_or_default()
        .to_string();
    let title = parsed
        .frontmatter
        .get("title")
        .and_then(|value| value.as_str())
        .map(ToString::to_string)
        .unwrap_or_else(|| stem.clone());
    let page_type = parsed
        .frontmatter
        .get("source_type")
        .and_then(|value| value.as_str())
        .unwrap_or("source")
        .to_string();
    let tags = parsed
        .frontmatter
        .get("tags")
        .and_then(|value| value.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(ToString::to_string))
                .collect()
        })
        .unwrap_or_default();
    let source_pages = wiki_source_pages_for_raw(idx, &rel_posix);

    Ok(Page {
        wiki: wiki.to_string(),
        path: format!("raw/{rel_posix}"),
        slug: stem,
        title,
        page_type,
        frontmatter: parsed.frontmatter,
        body,
        outgoing_links: Vec::new(),
        backlinks: Vec::new(),
        sources: Vec::new(),
        source_pages,
        tags,
    })
}

async fn get_asset(
    State(state): State<AppState>,
    _authz: ReadableWiki,
    AxumPath((wiki, name)): AxumPath<(String, String)>,
) -> Result<Response, ApiError> {
    let assets_dir = {
        let manager = read_manager(&state)?;
        manager
            .get(&wiki)
            .ok_or(ApiError::not_found("wiki 不存在"))?
            .entry
            .assets_dir
            .clone()
    };
    let target = safe_join(&assets_dir, &name).ok_or(ApiError::bad_request("非法路径"))?;
    if !target.is_file() {
        return Err(ApiError::not_found("资源不存在"));
    }
    let ext = target
        .extension()
        .and_then(|ext| ext.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !parser::IMAGE_EXTS.contains(&ext.as_str()) {
        return Err(ApiError::bad_request("不支持的资源类型"));
    }
    let bytes = tokio::fs::read(&target)
        .await
        .map_err(|_| ApiError::internal("读取资源失败"))?;
    Ok(([(header::CONTENT_TYPE, image_content_type(&ext))], bytes).into_response())
}

async fn get_graph(
    State(state): State<AppState>,
    ReadableWiki { wiki }: ReadableWiki,
    Query(query): Query<GraphQuery>,
) -> Result<Json<GraphResponse>, ApiError> {
    let manager = read_manager(&state)?;
    let idx = manager
        .get(&wiki)
        .ok_or(ApiError::not_found("wiki 不存在"))?;
    let keep = |rec: &PageRecord| {
        query.page_type.as_ref().is_none_or(|page_type| {
            rec.path
                .split_once('/')
                .map(|(head, _)| head)
                .unwrap_or(rec.path.as_str())
                == page_type
        })
    };

    let nodes: Vec<GraphNode> = idx
        .pages
        .values()
        .filter(|rec| keep(rec))
        .map(|rec| GraphNode {
            id: rec.path.clone(),
            title: rec.title.clone(),
            page_type: rec.page_type.clone(),
        })
        .collect();
    let node_ids: std::collections::HashSet<&str> =
        nodes.iter().map(|node| node.id.as_str()).collect();

    let mut edges = Vec::new();
    for rec in idx.pages.values() {
        if !node_ids.contains(rec.path.as_str()) {
            continue;
        }
        for target in &rec.targets {
            if let Some(hit) = idx.resolve(target)
                && hit != rec.path
                && node_ids.contains(hit)
            {
                edges.push(GraphEdge {
                    source: rec.path.clone(),
                    target: hit.to_string(),
                });
            }
        }
    }

    Ok(Json(GraphResponse { nodes, edges }))
}

/// 读取该 wiki 的 App 兼容 review 队列（`.llm-wiki/review.json`）。
/// 浏览器本身不能发起 review，本接口只做展示——items 原样透传，前端据此生成
/// 可复制给 agent 的提示词。文件缺失时返回空队列。
async fn get_review(
    State(state): State<AppState>,
    OwnerWiki { wiki }: OwnerWiki,
) -> Result<Json<ReviewResponse>, ApiError> {
    let root_dir = {
        let manager = read_manager(&state)?;
        manager
            .get(&wiki)
            .ok_or(ApiError::not_found("wiki 不存在"))?
            .entry
            .root_dir
            .clone()
    };
    let path = root_dir.join(".llm-wiki").join("review.json");
    let items: Vec<serde_json::Value> = match std::fs::read(&path) {
        Ok(bytes) => {
            let value = serde_json::from_slice::<serde_json::Value>(&bytes)
                .map_err(|_| ApiError::internal("review.json 解析失败"))?;
            // 两种格式并存：App 写 {"items":[...]}，skill 写顶层数组 [...]。
            if let serde_json::Value::Array(arr) = value {
                arr
            } else {
                value
                    .get("items")
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default()
            }
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(_) => return Err(ApiError::internal("读取 review.json 失败")),
    };
    let open_count = items.iter().filter(|item| is_open_review(item)).count();
    Ok(Json(ReviewResponse {
        wiki,
        root: path_label(&root_dir),
        count: items.len(),
        open_count,
        items,
    }))
}

/// 判断一条 review 项是否仍待处理。`resolved:true` 或落入收尾状态集视为已处理，
/// 其余（含缺省 status）一律当作待办，宁可多显不漏。
fn is_open_review(item: &serde_json::Value) -> bool {
    if item.get("resolved").and_then(|v| v.as_bool()) == Some(true) {
        return false;
    }
    match item.get("status").and_then(|v| v.as_str()) {
        Some(status) => !matches!(
            status,
            "resolved"
                | "done"
                | "closed"
                | "skipped"
                | "skip"
                | "researched"
                | "queued-for-research"
        ),
        None => true,
    }
}

/// 把相对路径安全拼到 `root` 下，拒绝 `..` 越界与绝对路径。
fn safe_join(root: &Path, rel: &str) -> Option<PathBuf> {
    let mut out = root.to_path_buf();
    for component in Path::new(rel).components() {
        match component {
            Component::Normal(part) => out.push(part),
            Component::CurDir => {}
            _ => return None,
        }
    }
    Some(out)
}

fn image_content_type(ext: &str) -> &'static str {
    match ext {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "svg" => "image/svg+xml",
        "bmp" => "image/bmp",
        "avif" => "image/avif",
        _ => "application/octet-stream",
    }
}

async fn search(
    State(state): State<AppState>,
    ReadableWiki { wiki }: ReadableWiki,
    Query(query): Query<SearchQuery>,
) -> Result<Json<SearchResponse>, ApiError> {
    let q = query.q.trim();
    if q.is_empty() && query.tag.is_none() && query.page_type.is_none() {
        return Err(ApiError::bad_request("需提供 q，或 tag/type 过滤条件"));
    }

    let limit = query.limit.unwrap_or(50).clamp(1, 200);
    let hits: Vec<SearchHit> = if q.is_empty() {
        let manager = read_manager(&state)?;
        let idx = manager
            .get(&wiki)
            .ok_or(ApiError::not_found("wiki 不存在"))?;
        let mut records = idx
            .pages
            .values()
            .filter(|record| {
                query
                    .tag
                    .as_ref()
                    .is_none_or(|tag| record.tags.iter().any(|candidate| candidate == tag))
                    && query
                        .page_type
                        .as_ref()
                        .is_none_or(|page_type| &record.page_type == page_type)
            })
            .collect::<Vec<_>>();
        records.sort_by(|a, b| a.title.cmp(&b.title));
        records
            .into_iter()
            .take(limit)
            .map(|record| SearchHit {
                path: record.path.clone(),
                slug: record.slug.clone(),
                title: record.title.clone(),
                page_type: record.page_type.clone(),
                snippet: preview(&record.body, 120),
                score: 0.0,
            })
            .collect()
    } else {
        {
            let manager = read_manager(&state)?;
            manager
                .get(&wiki)
                .ok_or(ApiError::not_found("wiki 不存在"))?;
        }
        state
            .searcher
            .lock()
            .map_err(|_| ApiError::internal("搜索索引不可用"))?
            .search(
                Some(&wiki),
                q,
                query.page_type.as_deref(),
                query.tag.as_deref(),
                limit,
            )
            .map_err(|_| ApiError::internal("搜索失败"))?
            .into_iter()
            .map(|result| SearchHit {
                path: result.path,
                slug: result.slug,
                title: result.title,
                page_type: result.page_type,
                snippet: result.snippet,
                score: result.score,
            })
            .collect()
    };

    Ok(Json(SearchResponse {
        query: q.to_string(),
        total: hits.len(),
        hits,
    }))
}

fn outgoing_links(idx: &WikiIndex, rec: &PageRecord) -> Vec<LinkRef> {
    let mut seen = std::collections::BTreeSet::new();
    rec.targets
        .iter()
        .filter_map(|target| {
            if !seen.insert(target) {
                return None;
            }
            let label = target.rsplit('/').next().unwrap_or(target).to_string();
            Some(match idx.resolve(target) {
                Some(hit) => LinkRef {
                    target: Some(hit.to_string()),
                    label: idx
                        .pages
                        .get(hit)
                        .map(|page| page.title.clone())
                        .unwrap_or(label),
                    broken: false,
                },
                None => LinkRef {
                    target: None,
                    label,
                    broken: true,
                },
            })
        })
        .collect()
}

fn page_ref(rec: &PageRecord) -> PageRef {
    PageRef {
        path: rec.path.clone(),
        slug: rec.slug.clone(),
        title: rec.title.clone(),
        page_type: rec.page_type.clone(),
        tags: rec.tags.clone(),
        updated: rec.updated.clone(),
        created: rec.created.clone(),
        mtime: rec.mtime,
    }
}

fn collect_raw_markdown(dir: &Path, out: &mut Vec<PathBuf>) -> std::io::Result<()> {
    if !dir.is_dir() {
        return Ok(());
    }
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let path = entry.path();
        if file_type.is_dir() {
            collect_raw_markdown(&path, out)?;
        } else if file_type.is_file()
            && path
                .extension()
                .and_then(|ext| ext.to_str())
                .is_some_and(|ext| ext.eq_ignore_ascii_case("md"))
        {
            out.push(path);
        }
    }
    Ok(())
}

fn raw_page_ref(raw_root: &Path, path: &Path) -> Option<PageRef> {
    let rel = path.strip_prefix(raw_root).ok()?;
    let rel_posix = rel
        .components()
        .filter_map(|component| match component {
            Component::Normal(part) => Some(part.to_string_lossy().into_owned()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("/");
    let path_without_ext = rel_posix
        .strip_suffix(".md")
        .unwrap_or(&rel_posix)
        .to_string();
    let slug = path.file_stem()?.to_string_lossy().into_owned();
    let parsed = parser::parse_file(path).ok();
    let frontmatter = parsed.as_ref().map(|parsed| &parsed.frontmatter);
    let string_value = |key: &str| {
        frontmatter
            .and_then(|fm| fm.get(key))
            .and_then(|value| value.as_str())
            .map(ToString::to_string)
    };
    let title = string_value("title").unwrap_or_else(|| slug.clone());
    let page_type = string_value("source_type").unwrap_or_else(|| {
        rel.parent()
            .and_then(Path::file_name)
            .and_then(|name| name.to_str())
            .unwrap_or("source")
            .to_string()
    });
    let tags = frontmatter
        .and_then(|fm| fm.get("tags"))
        .and_then(|value| value.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(ToString::to_string))
                .collect()
        })
        .unwrap_or_default();
    let mtime = path
        .metadata()
        .ok()
        .and_then(|meta| meta.modified().ok())
        .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|duration| duration.as_secs_f64());

    Some(PageRef {
        path: path_without_ext,
        slug,
        title,
        page_type,
        tags,
        updated: string_value("updated"),
        created: string_value("created").or_else(|| string_value("captured_at")),
        mtime,
    })
}

fn type_order(page_type: &str) -> (usize, &str) {
    const ORDER: &[&str] = &[
        "entities",
        "concepts",
        "sources",
        "queries",
        "synthesis",
        "comparisons",
    ];
    (
        ORDER
            .iter()
            .position(|candidate| *candidate == page_type)
            .unwrap_or(ORDER.len()),
        page_type,
    )
}

fn preview(body: &str, width: usize) -> String {
    let text = to_plain_text(body);
    if text.len() <= width {
        text
    } else {
        let mut end = width.min(text.len());
        while end > 0 && !text.is_char_boundary(end) {
            end -= 1;
        }
        format!("{}…", &text[..end])
    }
}

fn path_label(path: &std::path::Path) -> String {
    path.to_string_lossy().into_owned()
}

#[derive(Debug, Serialize, Deserialize)]
struct WikiInfo {
    key: String,
    name: String,
    description: String,
    default: bool,
    page_count: usize,
}

#[derive(Debug, Serialize, Deserialize)]
struct WikiConfigInfo {
    key: String,
    name: String,
    description: String,
    default: bool,
    page_count: usize,
    root_dir: String,
    wiki_dir: String,
    raw_dir: String,
    assets_dir: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct TreeNode {
    #[serde(rename = "type")]
    page_type: String,
    count: usize,
    pages: Vec<PageRef>,
}

#[derive(Debug, Serialize, Deserialize)]
struct PageRef {
    path: String,
    slug: String,
    title: String,
    #[serde(rename = "type")]
    page_type: String,
    tags: Vec<String>,
    updated: Option<String>,
    created: Option<String>,
    mtime: Option<f64>,
}

#[derive(Debug, Serialize, Deserialize)]
struct LinkRef {
    target: Option<String>,
    label: String,
    broken: bool,
}

#[derive(Debug, Serialize, Deserialize)]
struct Page {
    wiki: String,
    path: String,
    slug: String,
    title: String,
    #[serde(rename = "type")]
    page_type: String,
    frontmatter: BTreeMap<String, serde_json::Value>,
    body: String,
    outgoing_links: Vec<LinkRef>,
    backlinks: Vec<PageRef>,
    sources: Vec<String>,
    source_pages: Vec<PageRef>,
    tags: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct ServerConfigInfo {
    port: u16,
}

#[derive(Debug, Serialize, Deserialize)]
struct ShareConfigInfo {
    relay_connected: bool,
    online_url: Option<String>,
}

#[derive(Debug, Serialize)]
struct SessionInfo {
    principal: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    wiki: Option<String>,
}

/// grant 列表/管理响应项——**永不含 secret**。
#[derive(Debug, Serialize)]
struct ShareGrantView {
    grant_id: String,
    label: String,
    wiki: String,
    scope: Scope,
    include_raw: bool,
    created_at: i64,
    expires_at: Option<i64>,
    last_accessed: Option<i64>,
    revoked: bool,
    active: bool,
    is_default: bool,
}

/// 创建 / 取链接响应——含完整分享链接（内嵌 secret）。仅这两处 + grants.json 落盘
/// 允许出现 secret，且响应带 no-store。
#[derive(Debug, Serialize)]
struct ShareLinkResponse {
    grant_id: String,
    wiki: String,
    label: String,
    expires_at: Option<i64>,
    link: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    warning: Option<String>,
}

#[derive(Debug, Deserialize)]
struct CreateShareRequest {
    wiki: String,
    #[serde(default)]
    label: String,
    /// `Some(n)` = n 天后过期；`None`/缺省 = 永久（高级选项）。默认 30 天由前端下发。
    #[serde(default)]
    expires_in_days: Option<u32>,
    /// true = 同时设为该 Wiki 的通用分享；独立分享默认 false。
    #[serde(default)]
    make_default: bool,
}

#[derive(Debug, Deserialize)]
struct PatchShareRequest {
    /// `Some(n)` = 自现在起 n 天；`None` = 改为永久。
    #[serde(default)]
    expires_in_days: Option<u32>,
}

#[derive(Debug, Deserialize)]
struct ServerConfigUpdate {
    port: u16,
}

#[derive(Debug, Deserialize)]
struct DefaultWikiUpdate {
    key: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct ServerConfigUpdateResult {
    port: u16,
    restart_required: bool,
}

#[derive(Debug, Serialize, Deserialize)]
struct RestartResult {
    ok: bool,
}

#[derive(Debug, Serialize, Deserialize)]
struct AutostartConfigInfo {
    supported: bool,
    enabled: bool,
}

#[derive(Debug, Deserialize)]
struct AutostartUpdate {
    enabled: bool,
}

#[derive(Debug, Serialize)]
struct UpdateConfigInfo {
    supported: bool,
    #[serde(flatten, skip_serializing_if = "Option::is_none")]
    status: Option<UpdateStatus>,
}

/// 技能版本探测响应（doc 21 §4）：`supported` + flatten 的 `SkillVersionInfo`。
#[derive(Debug, Serialize)]
struct SkillsConfigInfo {
    supported: bool,
    #[serde(flatten, skip_serializing_if = "Option::is_none")]
    info: Option<SkillVersionInfo>,
}

#[derive(Debug, Deserialize)]
struct SkillsDismiss {
    version: String,
}

#[derive(Debug, Deserialize)]
struct GraphQuery {
    #[serde(rename = "type")]
    page_type: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct GraphNode {
    id: String,
    title: String,
    #[serde(rename = "type")]
    page_type: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct GraphEdge {
    source: String,
    target: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct GraphResponse {
    nodes: Vec<GraphNode>,
    edges: Vec<GraphEdge>,
}

#[derive(Debug, Deserialize)]
struct SearchQuery {
    #[serde(default)]
    q: String,
    #[serde(rename = "type")]
    page_type: Option<String>,
    tag: Option<String>,
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
struct SourcePreviewQuery {
    page: String,
    source: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct SearchHit {
    path: String,
    slug: String,
    title: String,
    #[serde(rename = "type")]
    page_type: String,
    snippet: String,
    score: f64,
}

#[derive(Debug, Serialize, Deserialize)]
struct SearchResponse {
    query: String,
    total: usize,
    hits: Vec<SearchHit>,
}

#[derive(Debug, Serialize, Deserialize)]
struct ReviewResponse {
    wiki: String,
    /// 该 wiki 的仓库根路径，供前端拼进给 agent 的提示词。
    root: String,
    count: usize,
    open_count: usize,
    /// review 项原样透传（schema 在 App 与 skill 间略有差异，前端按需容错取字段）。
    items: Vec<serde_json::Value>,
}

#[derive(Debug)]
struct ApiError {
    status: StatusCode,
    message: &'static str,
}

impl ApiError {
    fn not_found(message: &'static str) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            message,
        }
    }

    fn bad_request(message: &'static str) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            message,
        }
    }

    fn internal(message: &'static str) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message,
        }
    }

    fn not_supported(message: &'static str) -> Self {
        Self {
            status: StatusCode::NOT_IMPLEMENTED,
            message,
        }
    }

    fn unauthorized() -> Self {
        Self {
            status: StatusCode::UNAUTHORIZED,
            message: "令牌无效或缺失。",
        }
    }

    fn forbidden() -> Self {
        Self {
            status: StatusCode::FORBIDDEN,
            message: "无权访问该资源。",
        }
    }

    fn gone() -> Self {
        Self {
            status: StatusCode::GONE,
            message: "该分享已撤销。",
        }
    }
}

impl axum::response::IntoResponse for ApiError {
    fn into_response(self) -> axum::response::Response {
        let mut response = (
            self.status,
            Json(serde_json::json!({ "detail": self.message })),
        )
            .into_response();
        if self.status == StatusCode::UNAUTHORIZED {
            response.headers_mut().insert(
                header::WWW_AUTHENTICATE,
                header::HeaderValue::from_static("Bearer"),
            );
        }
        response
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::{Body, to_bytes};
    use axum::http::{Request, StatusCode};
    use llm_wiki_core::{IndexManager, WikiEntry};
    use std::fs;
    use tower::ServiceExt;

    #[tokio::test]
    async fn serves_wikis_tree_and_page() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("demo");
        let wiki_dir = root.join("wiki");
        fs::create_dir_all(wiki_dir.join("entities")).unwrap();
        fs::write(
            wiki_dir.join("entities").join("claude.md"),
            "---\ntitle: Claude\ntype: entities\n---\nhello",
        )
        .unwrap();
        fs::write(
            wiki_dir.join("mcp.md"),
            "---\ntitle: MCP\n---\nsee [[entities/claude|Claude]]",
        )
        .unwrap();

        let entry = WikiEntry {
            key: "demo".to_string(),
            name: "Demo".to_string(),
            description: Some("desc".to_string()),
            root_dir: root.clone(),
            wiki_dir,
            raw_dir: root.join("raw"),
            assets_dir: root.join("raw").join("assets"),
            default: true,
        };
        let manager = IndexManager::build([entry]).unwrap();
        let searcher = FullTextSearcher::in_memory().unwrap();
        for idx in manager.wikis.values() {
            searcher
                .reindex_wiki(&idx.entry.key, &idx.search_docs())
                .unwrap();
        }
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: manager,
            searcher,
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        let wikis: Vec<WikiInfo> = get_json(&app, "/api/v1/wikis").await;
        assert_eq!(wikis[0].key, "demo");
        assert_eq!(wikis[0].page_count, 2);

        let config_wikis: Vec<WikiConfigInfo> = get_json(&app, "/api/v1/config/wikis").await;
        assert_eq!(config_wikis[0].key, "demo");
        assert!(config_wikis[0].wiki_dir.ends_with("wiki"));

        let tree: Vec<TreeNode> = get_json(&app, "/api/v1/wikis/demo/tree").await;
        assert!(tree.iter().any(|node| node.page_type == "entities"));

        let page: Page = get_json(&app, "/api/v1/wikis/demo/pages/mcp").await;
        assert_eq!(page.title, "MCP");
        assert!(page.body.contains("[Claude](/w/demo/page/entities/claude)"));

        let results: SearchResponse = get_json(&app, "/api/v1/wikis/demo/search?q=Claude").await;
        assert!(results.hits.iter().any(|hit| hit.path == "mcp"));

        let graph: GraphResponse = get_json(&app, "/api/v1/wikis/demo/graph").await;
        assert_eq!(graph.nodes.len(), 2);
        // mcp 通过 [[entities/claude]] 链接到 claude
        assert!(
            graph
                .edges
                .iter()
                .any(|edge| edge.source == "mcp" && edge.target == "entities/claude")
        );

        // 按 type 过滤后只剩 entities/claude，跨类目的边被裁掉
        let filtered: GraphResponse =
            get_json(&app, "/api/v1/wikis/demo/graph?type=entities").await;
        assert_eq!(filtered.nodes.len(), 1);
        assert_eq!(filtered.nodes[0].id, "entities/claude");
        assert!(filtered.edges.is_empty());
    }

    #[tokio::test]
    async fn serves_raw_source_and_asset() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("demo");
        let wiki_dir = root.join("wiki");
        let raw_dir = root.join("raw");
        let assets_dir = raw_dir.join("assets");
        fs::create_dir_all(&wiki_dir).unwrap();
        fs::create_dir_all(wiki_dir.join("sources")).unwrap();
        fs::create_dir_all(wiki_dir.join("concepts")).unwrap();
        fs::create_dir_all(raw_dir.join("sources").join("video")).unwrap();
        fs::create_dir_all(raw_dir.join("sources").join("wechat")).unwrap();
        fs::create_dir_all(&assets_dir).unwrap();
        fs::write(
            raw_dir.join("sources").join("video").join("launch.md"),
            "---\ntitle: 衛星上網簡史\nsource_type: video\nsource_url: https://example.com/v\ntags: [space, video]\n---\n正文 ![cover](../../assets/pic.png)",
        )
        .unwrap();
        fs::write(
            raw_dir
                .join("sources")
                .join("wechat")
                .join("agent-memory.md"),
            "---\ntitle: Agent Memory\nsource_type: wechat\ncaptured_at: 2026-07-12\n---\n正文",
        )
        .unwrap();
        fs::write(assets_dir.join("pic.png"), b"\x89PNG\r\n\x1a\n").unwrap();
        fs::write(
            wiki_dir.join("sources").join("satellite-internet.md"),
            "---\ntype: source\ntitle: 衛星上網概览\nsources: [raw/sources/video/launch.md]\n---\n摘要",
        )
        .unwrap();
        fs::write(
            wiki_dir.join("concepts").join("satellite-internet.md"),
            "---\ntype: concept\ntitle: 衛星上網\nsources: [raw/sources/video/launch.md]\n---\n概念",
        )
        .unwrap();

        let entry = WikiEntry {
            key: "demo".to_string(),
            name: "Demo".to_string(),
            description: None,
            root_dir: root.clone(),
            wiki_dir,
            raw_dir,
            assets_dir,
            default: true,
        };
        let manager = IndexManager::build([entry]).unwrap();
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: manager,
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        let raw_tree: TreeNode = get_json(&app, "/api/v1/wikis/demo/raw-tree").await;
        assert_eq!(raw_tree.page_type, "raw");
        assert_eq!(raw_tree.count, 2);
        let video = raw_tree
            .pages
            .iter()
            .find(|page| page.path == "sources/video/launch")
            .unwrap();
        assert_eq!(video.title, "衛星上網簡史");
        assert_eq!(video.page_type, "video");
        let wechat = raw_tree
            .pages
            .iter()
            .find(|page| page.path == "sources/wechat/agent-memory")
            .unwrap();
        assert_eq!(wechat.page_type, "wechat");
        assert_eq!(wechat.created.as_deref(), Some("2026-07-12"));

        // 无扩展名（wikilink 风格）也能解析到 .md
        let raw: Page = get_json(&app, "/api/v1/wikis/demo/raw/sources/video/launch").await;
        assert_eq!(raw.title, "衛星上網簡史");
        assert_eq!(raw.page_type, "video");
        assert_eq!(raw.path, "raw/sources/video/launch.md");
        assert_eq!(
            raw.frontmatter.get("source_url").and_then(|v| v.as_str()),
            Some("https://example.com/v")
        );
        assert!(raw.tags.contains(&"space".to_string()));
        assert_eq!(raw.source_pages.len(), 1);
        assert_eq!(raw.source_pages[0].path, "sources/satellite-internet");
        assert_eq!(raw.source_pages[0].title, "衛星上網概览");
        // 图片被重写到 assets 接口
        assert!(raw.body.contains("/api/v1/wikis/demo/assets/pic.png"));

        // assets 接口返回图片
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/wikis/demo/assets/pic.png")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response
                .headers()
                .get(header::CONTENT_TYPE)
                .and_then(|value| value.to_str().ok()),
            Some("image/png")
        );
    }

    /// 与 serves_wikis_tree_and_page 相同的两页 demo 库，供 MCP 测试复用。
    fn demo_app(auth_token: &str) -> (tempfile::TempDir, Router) {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("demo");
        let wiki_dir = root.join("wiki");
        let raw_dir = root.join("raw");
        fs::create_dir_all(wiki_dir.join("entities")).unwrap();
        fs::create_dir_all(raw_dir.join("sources").join("x")).unwrap();
        fs::write(
            wiki_dir.join("entities").join("claude.md"),
            "---\ntitle: Claude\ntype: entities\n---\nhello",
        )
        .unwrap();
        fs::write(
            wiki_dir.join("mcp.md"),
            "---\ntitle: MCP\nsources: [x/origin.md]\n---\nsee [[entities/claude|Claude]]",
        )
        .unwrap();
        fs::write(
            raw_dir.join("sources").join("x").join("origin.md"),
            "---\ntitle: Origin\n---\nraw body",
        )
        .unwrap();

        let entry = WikiEntry {
            key: "demo".to_string(),
            name: "Demo".to_string(),
            description: Some("desc".to_string()),
            root_dir: root.clone(),
            wiki_dir,
            raw_dir,
            assets_dir: root.join("raw").join("assets"),
            default: true,
        };
        let manager = IndexManager::build([entry]).unwrap();
        let searcher = FullTextSearcher::in_memory().unwrap();
        for idx in manager.wikis.values() {
            searcher
                .reindex_wiki(&idx.entry.key, &idx.search_docs())
                .unwrap();
        }
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: manager,
            searcher,
            auth_token: auth_token.to_string(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });
        (tmp, app)
    }

    async fn mcp_post_raw(app: &Router, token: Option<&str>, body: serde_json::Value) -> Response {
        let mut builder = Request::builder()
            .method("POST")
            .uri("/mcp/")
            .header(header::CONTENT_TYPE, "application/json");
        if let Some(token) = token {
            builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
        }
        app.clone()
            .oneshot(builder.body(Body::from(body.to_string())).expect("request"))
            .await
            .expect("response")
    }

    async fn mcp_rpc(
        app: &Router,
        token: Option<&str>,
        body: serde_json::Value,
    ) -> serde_json::Value {
        let response = mcp_post_raw(app, token, body).await;
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }

    /// tools/call 结果里的 text 载荷（本实现固定单条 text content）。
    fn tool_text(reply: &serde_json::Value) -> String {
        reply["result"]["content"][0]["text"]
            .as_str()
            .expect("text content")
            .to_string()
    }

    #[tokio::test]
    async fn mcp_endpoint_handshake_and_tools() {
        let (_tmp, app) = demo_app("");

        // initialize：回显受支持的版本，声明 tools 能力
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
                "protocolVersion":"2025-03-26","capabilities":{},
                "clientInfo":{"name":"test","version":"0"}}}),
        )
        .await;
        assert_eq!(reply["result"]["protocolVersion"], "2025-03-26");
        assert!(reply["result"]["capabilities"]["tools"].is_object());

        // 通知（无 id）→ 202
        let response = mcp_post_raw(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
        )
        .await;
        assert_eq!(response.status(), StatusCode::ACCEPTED);

        // tools/list → 契约层的 6 个工具
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}),
        )
        .await;
        let tools = reply["result"]["tools"].as_array().unwrap();
        assert_eq!(tools.len(), 6);
        assert!(tools.iter().any(|tool| tool["name"] == "search_wiki"));
        assert!(tools.iter().any(|tool| tool["name"] == "read_pages"));

        // list_wikis
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":3,"method":"tools/call",
                "params":{"name":"list_wikis","arguments":{}}}),
        )
        .await;
        assert_eq!(reply["result"]["isError"], false);
        assert!(tool_text(&reply).contains("\"demo\""));

        // search_wiki → 命中 mcp 页
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":4,"method":"tools/call",
                "params":{"name":"search_wiki","arguments":{"wiki":"demo","query":"Claude"}}}),
        )
        .await;
        assert!(tool_text(&reply).contains("mcp"));

        // search_wiki 省略 wiki → 跨库检索，命中带 wiki 归属，scope=all
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":41,"method":"tools/call",
                "params":{"name":"search_wiki","arguments":{"query":"Claude"}}}),
        )
        .await;
        let text = tool_text(&reply);
        assert!(text.contains("\"scope\": \"all\""));
        assert!(text.contains("\"wiki\": \"demo\""));

        // read_page → 原文正文，wikilink 不被改写
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":5,"method":"tools/call",
                "params":{"name":"read_page","arguments":{"wiki":"demo","path":"mcp"}}}),
        )
        .await;
        let text = tool_text(&reply);
        assert!(text.contains("[[entities/claude|Claude]]"));
        assert!(text.contains("entities/claude")); // outgoingLinks 已解析
        let page: serde_json::Value = serde_json::from_str(&text).expect("read_page payload");
        let legacy_source_path = page["sources"][0]
            .as_str()
            .expect("legacy source path")
            .to_string();

        // read_pages → 有界批量读取：缺页进 missing，正常页带 body
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":51,"method":"tools/call",
                "params":{"name":"read_pages","arguments":{
                    "wiki":"demo","paths":["mcp","entities/claude","nope"]}}}),
        )
        .await;
        assert_eq!(reply["result"]["isError"], false);
        let payload: serde_json::Value =
            serde_json::from_str(&tool_text(&reply)).expect("read_pages payload");
        assert_eq!(payload["pages"].as_array().unwrap().len(), 2);
        assert_eq!(payload["missing"][0], "nope");
        assert!(
            payload["pages"][0]["body"]
                .as_str()
                .unwrap()
                .contains("[[entities/claude|Claude]]")
        );

        // read_pages 预算：maxPages=1 → 第二页落入 omitted；超长正文被截断并标记
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":52,"method":"tools/call",
                "params":{"name":"read_pages","arguments":{
                    "wiki":"demo","paths":["mcp","entities/claude"],"maxPages":1}}}),
        )
        .await;
        let payload: serde_json::Value =
            serde_json::from_str(&tool_text(&reply)).expect("read_pages payload");
        assert_eq!(payload["pages"].as_array().unwrap().len(), 1);
        assert_eq!(payload["omitted"][0], "entities/claude");
        assert_eq!(payload["budget"]["maxPages"], 1);

        // read_raw → raw 层原文（无扩展名也可解析）
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":6,"method":"tools/call",
                "params":{"name":"read_raw","arguments":{"wiki":"demo","path":"sources/x/origin"}}}),
        )
        .await;
        assert!(tool_text(&reply).contains("raw body"));

        // 旧版页面把 sources 记录成相对 raw/sources/ 的 x/...；read_raw 直接接力也应成功。
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":61,"method":"tools/call",
                "params":{"name":"read_raw","arguments":{"wiki":"demo","path":legacy_source_path}}}),
        )
        .await;
        assert_eq!(reply["result"]["isError"], false);
        let payload: serde_json::Value =
            serde_json::from_str(&tool_text(&reply)).expect("legacy read_raw payload");
        assert_eq!(payload["path"], "raw/sources/x/origin.md");
        assert_eq!(payload["body"], "raw body");

        // list_wiki_tree → 类目分组
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":7,"method":"tools/call",
                "params":{"name":"list_wiki_tree","arguments":{"wiki":"demo"}}}),
        )
        .await;
        assert!(tool_text(&reply).contains("entities"));

        // 执行层错误 → isError 工具结果而非协议错误
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":8,"method":"tools/call",
                "params":{"name":"read_page","arguments":{"wiki":"nope","path":"x"}}}),
        )
        .await;
        assert_eq!(reply["result"]["isError"], true);

        // 协议层错误：未知方法 / 未知工具
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":9,"method":"resources/list"}),
        )
        .await;
        assert_eq!(reply["error"]["code"], -32601);
        let reply = mcp_rpc(
            &app,
            None,
            serde_json::json!({"jsonrpc":"2.0","id":10,"method":"tools/call",
                "params":{"name":"nope","arguments":{}}}),
        )
        .await;
        assert_eq!(reply["error"]["code"], -32602);

        // GET → 405 + Allow: POST
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/mcp/")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
        assert_eq!(
            response
                .headers()
                .get(header::ALLOW)
                .and_then(|value| value.to_str().ok()),
            Some("POST")
        );
    }

    #[tokio::test]
    async fn mcp_endpoint_requires_auth_token() {
        let (_tmp, app) = demo_app("secret");
        let ping = serde_json::json!({"jsonrpc":"2.0","id":1,"method":"ping"});

        // 无令牌 / 错令牌 → 401
        let response = mcp_post_raw(&app, None, ping.clone()).await;
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        let response = mcp_post_raw(&app, Some("wrong"), ping.clone()).await;
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);

        // 正确令牌（不带尾斜杠的 /mcp 也通）
        let reply = mcp_rpc(&app, Some("secret"), ping.clone()).await;
        assert!(reply["result"].is_object());
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp")
                    .header(header::CONTENT_TYPE, "application/json")
                    .header(header::AUTHORIZATION, "Bearer secret")
                    .body(Body::from(ping.to_string()))
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn serves_review_queue_with_open_count() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("demo");
        let wiki_dir = root.join("wiki");
        fs::create_dir_all(&wiki_dir).unwrap();
        fs::create_dir_all(root.join(".llm-wiki")).unwrap();
        fs::write(
            root.join(".llm-wiki").join("review.json"),
            r#"{"items":[
                {"type":"suggestion","title":"A","status":"open"},
                {"type":"contradiction","title":"B","resolved":true},
                {"type":"suggestion","title":"C","status":"researched"}
            ]}"#,
        )
        .unwrap();

        let entry = WikiEntry {
            key: "demo".to_string(),
            name: "Demo".to_string(),
            description: None,
            root_dir: root.clone(),
            wiki_dir,
            raw_dir: root.join("raw"),
            assets_dir: root.join("raw").join("assets"),
            default: true,
        };
        let manager = IndexManager::build([entry]).unwrap();
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: manager,
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        let review: ReviewResponse = get_json(&app, "/api/v1/wikis/demo/review").await;
        assert_eq!(review.count, 3);
        // 仅第一条 open：resolved:true 与 status=researched 均视为已处理。
        assert_eq!(review.open_count, 1);
        assert_eq!(review.items[0]["title"], "A");
    }

    #[tokio::test]
    async fn serves_review_queue_top_level_array() {
        // skill 写法：顶层直接是数组（无 items 包裹）。
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("demo");
        fs::create_dir_all(root.join("wiki")).unwrap();
        fs::create_dir_all(root.join(".llm-wiki")).unwrap();
        fs::write(
            root.join(".llm-wiki").join("review.json"),
            r#"[
                {"type":"suggestion","title":"A","resolved":false},
                {"type":"suggestion","title":"B","resolved":true}
            ]"#,
        )
        .unwrap();

        let entry = WikiEntry {
            key: "demo".to_string(),
            name: "Demo".to_string(),
            description: None,
            root_dir: root.clone(),
            wiki_dir: root.join("wiki"),
            raw_dir: root.join("raw"),
            assets_dir: root.join("raw").join("assets"),
            default: true,
        };
        let manager = IndexManager::build([entry]).unwrap();
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: manager,
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });
        let review: ReviewResponse = get_json(&app, "/api/v1/wikis/demo/review").await;
        assert_eq!(review.count, 2);
        assert_eq!(review.open_count, 1);
        assert_eq!(review.items[0]["title"], "A");
    }

    #[tokio::test]
    async fn review_queue_empty_when_file_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("demo");
        fs::create_dir_all(root.join("wiki")).unwrap();
        let entry = WikiEntry {
            key: "demo".to_string(),
            name: "Demo".to_string(),
            description: None,
            root_dir: root.clone(),
            wiki_dir: root.join("wiki"),
            raw_dir: root.join("raw"),
            assets_dir: root.join("raw").join("assets"),
            default: true,
        };
        let manager = IndexManager::build([entry]).unwrap();
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: manager,
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });
        let review: ReviewResponse = get_json(&app, "/api/v1/wikis/demo/review").await;
        assert_eq!(review.count, 0);
        assert_eq!(review.open_count, 0);
    }

    #[test]
    fn refresh_wiki_index_picks_up_new_markdown_and_search_docs() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("demo");
        let wiki_dir = root.join("wiki");
        fs::create_dir_all(wiki_dir.join("sources")).unwrap();
        fs::write(
            wiki_dir.join("sources").join("old.md"),
            "---\ntitle: Old\n---\nold body",
        )
        .unwrap();

        let entry = WikiEntry {
            key: "demo".to_string(),
            name: "Demo".to_string(),
            description: None,
            root_dir: root.clone(),
            wiki_dir: wiki_dir.clone(),
            raw_dir: root.join("raw"),
            assets_dir: root.join("raw").join("assets"),
            default: true,
        };
        let manager = Arc::new(RwLock::new(IndexManager::build([entry]).unwrap()));
        let searcher = Arc::new(Mutex::new(FullTextSearcher::in_memory().unwrap()));
        {
            let manager = manager.read().unwrap();
            for idx in manager.wikis.values() {
                searcher
                    .lock()
                    .unwrap()
                    .reindex_wiki(&idx.entry.key, &idx.search_docs())
                    .unwrap();
            }
        }

        fs::write(
            wiki_dir.join("sources").join("new.md"),
            "---\ntitle: New\n---\nnew body with needle",
        )
        .unwrap();
        refresh_wiki_index("demo", &manager, &searcher).unwrap();

        let manager = manager.read().unwrap();
        let idx = manager.get("demo").unwrap();
        assert!(idx.pages.contains_key("sources/new"));
        assert_eq!(idx.pages.len(), 2);
        drop(manager);

        let hits = searcher
            .lock()
            .unwrap()
            .search(Some("demo"), "needle", None, None, 10)
            .unwrap();
        assert!(hits.iter().any(|hit| hit.path == "sources/new"));
    }

    #[test]
    fn reload_registry_index_adds_and_removes_wikis_and_search_docs() {
        let tmp = tempfile::tempdir().unwrap();
        let alpha = tmp.path().join("alpha");
        let beta = tmp.path().join("beta");
        fs::create_dir_all(alpha.join("wiki").join("sources")).unwrap();
        fs::create_dir_all(beta.join("wiki").join("sources")).unwrap();
        fs::write(
            alpha.join("wiki").join("sources").join("old.md"),
            "---\ntitle: Alpha\n---\nold alpha needle",
        )
        .unwrap();
        fs::write(
            beta.join("wiki").join("sources").join("new.md"),
            "---\ntitle: Beta\n---\nnew beta needle",
        )
        .unwrap();
        let registry = tmp.path().join("wikis.json");
        // 用 serde_json 序列化路径，Windows 反斜杠才能被正确转义成合法 JSON。
        fs::write(
            &registry,
            serde_json::json!({"wikis": [{"path": alpha}]}).to_string(),
        )
        .unwrap();
        let config = CoreConfig {
            registry_path: registry.clone(),
            wiki_dirs: Vec::new(),
            cache_dir: tmp.path().join(".cache"),
        };
        let entries = load_registry(&config).unwrap();
        let manager = Arc::new(RwLock::new(
            IndexManager::build(entries.into_values()).unwrap(),
        ));
        let searcher = Arc::new(Mutex::new(FullTextSearcher::in_memory().unwrap()));
        {
            let manager = manager.read().unwrap();
            let docs_by_wiki = manager
                .wikis
                .values()
                .map(|idx| (idx.entry.key.clone(), idx.search_docs()))
                .collect::<Vec<_>>();
            searcher
                .lock()
                .unwrap()
                .reindex_all(
                    docs_by_wiki
                        .iter()
                        .map(|(key, docs)| (key.as_str(), docs.as_slice())),
                )
                .unwrap();
        }

        fs::write(
            &registry,
            serde_json::json!({"wikis": [{"path": beta}]}).to_string(),
        )
        .unwrap();
        reload_registry_index(&config, &manager, &searcher).unwrap();

        let manager = manager.read().unwrap();
        assert!(!manager.wikis.contains_key("alpha"));
        assert!(manager.wikis.contains_key("beta"));
        drop(manager);

        let searcher = searcher.lock().unwrap();
        assert!(
            searcher
                .search(Some("alpha"), "needle", None, None, 10)
                .unwrap()
                .is_empty()
        );
        let hits = searcher
            .search(Some("beta"), "needle", None, None, 10)
            .unwrap();
        assert!(hits.iter().any(|hit| hit.path == "sources/new"));
    }

    #[test]
    fn safe_join_rejects_traversal_and_absolute() {
        let root = Path::new("/wiki/raw");
        assert_eq!(
            safe_join(root, "sources/a.md"),
            Some(PathBuf::from("/wiki/raw/sources/a.md"))
        );
        assert_eq!(
            safe_join(root, "./sources/a.md"),
            Some(PathBuf::from("/wiki/raw/sources/a.md"))
        );
        assert_eq!(safe_join(root, "../../etc/passwd"), None);
        assert_eq!(safe_join(root, "sources/../../escape"), None);
        assert_eq!(safe_join(root, "/etc/passwd"), None);
    }

    #[tokio::test]
    async fn serves_frontend_shell_for_deep_links() {
        let tmp = tempfile::tempdir().unwrap();
        let dist = tmp.path().join("dist");
        fs::create_dir_all(&dist).unwrap();
        fs::write(
            dist.join("index.html"),
            "<!doctype html><title>shell</title>",
        )
        .unwrap();

        let manager = IndexManager::build(std::iter::empty::<WikiEntry>()).unwrap();
        let app = build_router(ServerConfig {
            frontend_dist: Some(dist),
            index_manager: manager,
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        for uri in [
            "/w/llm-wiki/browse/queries",
            "/share/s_public/w/llm-wiki/page/entities/codex",
        ] {
            let response = app
                .clone()
                .oneshot(
                    Request::builder()
                        .uri(uri)
                        .body(Body::empty())
                        .expect("request"),
                )
                .await
                .expect("response");
            assert_eq!(response.status(), StatusCode::OK, "{uri}");
            let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
            assert!(std::str::from_utf8(&body).unwrap().contains("shell"));
        }
    }

    #[tokio::test]
    async fn put_default_wiki_rewrites_registry_and_reloads() {
        let tmp = tempfile::tempdir().unwrap();
        for key in ["alpha", "beta"] {
            let wiki_dir = tmp.path().join(key).join("wiki");
            fs::create_dir_all(&wiki_dir).unwrap();
            fs::write(wiki_dir.join("home.md"), "---\ntitle: Home\n---\nhi").unwrap();
        }
        let registry_path = tmp.path().join("wikis.json");
        fs::write(
            &registry_path,
            serde_json::json!({
                "wikis": [
                    {"path": tmp.path().join("alpha"), "name": "Alpha", "default": true, "extra": "keep-me"},
                    {"path": tmp.path().join("beta"), "name": "Beta"},
                ]
            })
            .to_string(),
        )
        .unwrap();
        let config = CoreConfig {
            registry_path: registry_path.clone(),
            wiki_dirs: vec![],
            cache_dir: tmp.path().join(".cache"),
        };

        let manager = IndexManager::build(load_registry(&config).unwrap().into_values()).unwrap();
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: manager,
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: Some(config),
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/api/v1/config/wikis/default")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(r#"{"key":"beta"}"#))
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let wikis: Vec<WikiConfigInfo> = serde_json::from_slice(&body).unwrap();
        assert_eq!(wikis[0].key, "beta");
        assert!(wikis[0].default);
        assert!(!wikis.iter().any(|wiki| wiki.key == "alpha" && wiki.default));

        // 文件被改写：default 唯一转移到 beta，其余字段原样保留。
        let saved: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&registry_path).unwrap()).unwrap();
        let items = saved["wikis"].as_array().unwrap();
        let alpha = items.iter().find(|i| i["name"] == "Alpha").unwrap();
        let beta = items.iter().find(|i| i["name"] == "Beta").unwrap();
        assert_eq!(alpha["default"], serde_json::json!(false));
        assert_eq!(alpha["extra"], serde_json::json!("keep-me"));
        assert_eq!(beta["default"], serde_json::json!(true));

        // 索引同步重载：后续 GET 也看到新 default。
        let listed: Vec<WikiConfigInfo> = get_json(&app, "/api/v1/config/wikis").await;
        assert_eq!(listed[0].key, "beta");
        assert!(listed[0].default);

        // 未知 key → 404。
        let missing = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/api/v1/config/wikis/default")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(r#"{"key":"nope"}"#))
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(missing.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn server_config_get_put_and_restart() {
        use std::sync::atomic::{AtomicBool, Ordering};

        let persisted = Arc::new(Mutex::new(None::<u16>));
        let restarted = Arc::new(AtomicBool::new(false));
        let persist_target = persisted.clone();
        let restart_target = restarted.clone();
        let control = ServerControl {
            persist_port: Arc::new(move |port| {
                *persist_target.lock().unwrap() = Some(port);
                Ok(())
            }),
            restart: Arc::new(move || restart_target.store(true, Ordering::SeqCst)),
            autostart: None,
            update: None,
            skill_version: None,
        };
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: IndexManager::build(std::iter::empty::<WikiEntry>()).unwrap(),
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: Some(control),
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        let info: ServerConfigInfo = get_json(&app, "/api/v1/config/server").await;
        assert_eq!(info.port, 8800);

        let put = |body: &'static str| {
            app.clone().oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/api/v1/config/server")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(body))
                    .expect("request"),
            )
        };

        let ok = put(r#"{"port":9000}"#).await.expect("response");
        assert_eq!(ok.status(), StatusCode::OK);
        assert_eq!(*persisted.lock().unwrap(), Some(9000));

        let bad = put(r#"{"port":80}"#).await.expect("response");
        assert_eq!(bad.status(), StatusCode::BAD_REQUEST);

        let restart = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/config/restart")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(restart.status(), StatusCode::OK);
        assert!(restarted.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn autostart_config_get_and_put() {
        let enabled = Arc::new(Mutex::new(false));
        let read_target = enabled.clone();
        let write_target = enabled.clone();
        let control = ServerControl {
            persist_port: Arc::new(|_| Ok(())),
            restart: Arc::new(|| {}),
            autostart: Some(AutostartControl {
                is_enabled: Arc::new(move || Ok(*read_target.lock().unwrap())),
                set_enabled: Arc::new(move |value| {
                    *write_target.lock().unwrap() = value;
                    Ok(())
                }),
            }),
            update: None,
            skill_version: None,
        };
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: IndexManager::build(std::iter::empty::<WikiEntry>()).unwrap(),
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: Some(control),
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        let info: AutostartConfigInfo = get_json(&app, "/api/v1/config/autostart").await;
        assert!(info.supported);
        assert!(!info.enabled);

        let put = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/api/v1/config/autostart")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(r#"{"enabled":true}"#))
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(put.status(), StatusCode::OK);
        assert!(*enabled.lock().unwrap());

        let info: AutostartConfigInfo = get_json(&app, "/api/v1/config/autostart").await;
        assert!(info.enabled);
    }

    #[tokio::test]
    async fn update_config_get_check_and_install() {
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

        let checked = Arc::new(AtomicUsize::new(0));
        let installed = Arc::new(AtomicBool::new(false));
        let check_target = checked.clone();
        let install_state = installed.clone();
        let status_state = installed.clone();
        let control = ServerControl {
            persist_port: Arc::new(|_| Ok(())),
            restart: Arc::new(|| {}),
            autostart: None,
            update: Some(UpdateControl {
                status: Arc::new(move || UpdateStatus {
                    current_version: "1.0.3".into(),
                    state: if status_state.load(Ordering::SeqCst) {
                        "ready-to-restart".into()
                    } else {
                        "available".into()
                    },
                    latest_version: Some("1.0.4".into()),
                    notes: None,
                    downloaded: None,
                    total: None,
                    error: None,
                }),
                check: Arc::new(move || {
                    check_target.fetch_add(1, Ordering::SeqCst);
                }),
                install: Arc::new(move || {
                    install_state.store(true, Ordering::SeqCst);
                    Ok(())
                }),
            }),
            skill_version: None,
        };
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: IndexManager::build(std::iter::empty::<WikiEntry>()).unwrap(),
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: Some(control),
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        let info: serde_json::Value = get_json(&app, "/api/v1/config/update").await;
        assert_eq!(info["supported"], true);
        assert_eq!(info["current_version"], "1.0.3");
        assert_eq!(info["state"], "available");
        assert_eq!(info["latest_version"], "1.0.4");

        let post = |uri: &'static str| {
            app.clone().oneshot(
                Request::builder()
                    .method("POST")
                    .uri(uri)
                    .body(Body::empty())
                    .expect("request"),
            )
        };

        let check = post("/api/v1/config/update/check").await.expect("response");
        assert_eq!(check.status(), StatusCode::OK);
        assert_eq!(checked.load(Ordering::SeqCst), 1);

        let install = post("/api/v1/config/update/install")
            .await
            .expect("response");
        assert_eq!(install.status(), StatusCode::OK);
        assert!(installed.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn update_config_reports_unsupported_without_hooks() {
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: IndexManager::build(std::iter::empty::<WikiEntry>()).unwrap(),
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        let info: serde_json::Value = get_json(&app, "/api/v1/config/update").await;
        assert_eq!(info["supported"], false);
        assert!(info.get("state").is_none());

        let check = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/config/update/check")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(check.status(), StatusCode::NOT_IMPLEMENTED);
    }

    #[tokio::test]
    async fn skills_config_supported_and_owner_only() {
        let manager = Arc::new(SkillVersionManager::new(SkillVersionConfig {
            app_version: "1.0.0".into(),
            builtin_slugs: vec![], // 无 slug → resolve 为 Absent（不发网络也稳定）
            host_targets: vec![],
            endpoints: vec![],
            state_path: None,
            on_change: None,
            notify: None,
        }));
        let control = ServerControl {
            persist_port: Arc::new(|_| Ok(())),
            restart: Arc::new(|| {}),
            autostart: None,
            update: None,
            skill_version: Some(manager),
        };
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: IndexManager::build(std::iter::empty::<WikiEntry>()).unwrap(),
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: "secret".into(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: Some(control),
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        // Owner（master token）可读，supported:true。
        let owner = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/config/skills")
                    .header(header::AUTHORIZATION, "Bearer secret")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(owner.status(), StatusCode::OK);
        let bytes = axum::body::to_bytes(owner.into_body(), usize::MAX)
            .await
            .unwrap();
        let info: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(info["supported"], true);
        assert_eq!(info["changelogUrl"], skill_version::CHANGELOG_URL);

        // 无令牌 → 401（owner-only）。
        let anon = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/config/skills")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(anon.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn skills_config_reports_unsupported_without_hooks() {
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: IndexManager::build(std::iter::empty::<WikiEntry>()).unwrap(),
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });
        let info: serde_json::Value = get_json(&app, "/api/v1/config/skills").await;
        assert_eq!(info["supported"], false);
    }

    #[tokio::test]
    async fn autostart_config_reports_unsupported_without_hooks() {
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: IndexManager::build(std::iter::empty::<WikiEntry>()).unwrap(),
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        });

        let info: AutostartConfigInfo = get_json(&app, "/api/v1/config/autostart").await;
        assert!(!info.supported);

        let put = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/api/v1/config/autostart")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(r#"{"enabled":true}"#))
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_ne!(put.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn share_config_reports_online_url_when_relay_connected() {
        let online_url = Arc::new(Mutex::new(Some(
            "https://wiki.example/demo/?token=secret".to_string(),
        )));
        let provider_url = online_url.clone();
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: IndexManager::build(std::iter::empty::<WikiEntry>()).unwrap(),
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: String::new(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: Some(Arc::new(move || {
                provider_url.lock().ok().and_then(|guard| guard.clone())
            })),
            public_base_url: None,
            grants_path: None,
        });

        let share: ShareConfigInfo = get_json(&app, "/api/v1/config/share").await;
        assert!(share.relay_connected);
        assert_eq!(
            share.online_url.as_deref(),
            Some("https://wiki.example/demo/?token=secret")
        );

        *online_url.lock().unwrap() = None;
        let share: ShareConfigInfo = get_json(&app, "/api/v1/config/share").await;
        assert!(!share.relay_connected);
        assert_eq!(share.online_url, None);
    }

    #[tokio::test]
    async fn api_requires_token_when_configured() {
        let app = empty_app(Some("secret"));

        assert_eq!(
            status(&app, "/api/v1/healthz").await,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            status_with_auth(&app, "/api/v1/healthz", "Bearer wrong").await,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            status_with_auth(&app, "/api/v1/healthz", "Bearer secret").await,
            StatusCode::OK
        );
        assert_eq!(
            status(&app, "/api/v1/healthz?token=secret").await,
            StatusCode::OK
        );
    }

    #[tokio::test]
    async fn relay_requests_are_rejected_without_token() {
        let app = empty_app(None);
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/healthz")
                    .header("x-llm-wiki-relay", "1")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    }

    fn empty_app(auth_token: Option<&str>) -> Router {
        build_router(ServerConfig {
            frontend_dist: None,
            index_manager: IndexManager::build(std::iter::empty::<WikiEntry>()).unwrap(),
            searcher: FullTextSearcher::in_memory().unwrap(),
            auth_token: auth_token.unwrap_or_default().to_string(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: None,
        })
    }

    /// 两个 wiki + 落盘的 grants + master token=secret，用于走查分享鉴权矩阵。
    fn two_wiki_app(grants_path: PathBuf) -> (tempfile::TempDir, Router) {
        let tmp = tempfile::tempdir().unwrap();
        let mut entries = Vec::new();
        for key in ["demo", "other"] {
            let root = tmp.path().join(key);
            let wiki_dir = root.join("wiki");
            let raw_dir = root.join("raw");
            fs::create_dir_all(&wiki_dir).unwrap();
            fs::create_dir_all(raw_dir.join("sources")).unwrap();
            let frontmatter = if key == "demo" {
                format!("---\ntitle: {key}\nsources: [sources/referenced.md]\n---\nhello {key}")
            } else {
                format!("---\ntitle: {key}\n---\nhello {key}")
            };
            fs::write(wiki_dir.join("index.md"), frontmatter).unwrap();
            if key == "demo" {
                fs::write(
                    raw_dir.join("sources/referenced.md"),
                    "---\ntitle: Referenced Source\n---\nallowed preview",
                )
                .unwrap();
                fs::write(
                    raw_dir.join("sources/hidden.md"),
                    "---\ntitle: Hidden Source\n---\nnot referenced",
                )
                .unwrap();
            }
            entries.push(WikiEntry {
                key: key.to_string(),
                name: key.to_string(),
                description: None,
                root_dir: root.clone(),
                wiki_dir,
                raw_dir: raw_dir.clone(),
                assets_dir: raw_dir.join("assets"),
                default: key == "demo",
            });
        }
        let manager = IndexManager::build(entries).unwrap();
        let searcher = FullTextSearcher::in_memory().unwrap();
        for idx in manager.wikis.values() {
            searcher
                .reindex_wiki(&idx.entry.key, &idx.search_docs())
                .unwrap();
        }
        let app = build_router(ServerConfig {
            frontend_dist: None,
            index_manager: manager,
            searcher,
            auth_token: "secret".to_string(),
            watch_wikis: false,
            registry_config: None,
            port: 8800,
            control: None,
            owner_online_url: None,
            public_base_url: None,
            grants_path: Some(grants_path),
        });
        (tmp, app)
    }

    async fn send_json(
        app: &Router,
        method: &str,
        uri: &str,
        token: Option<&str>,
        body: Option<serde_json::Value>,
    ) -> Response {
        let mut builder = Request::builder().method(method).uri(uri);
        if let Some(token) = token {
            builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
        }
        let body = match body {
            Some(value) => {
                builder = builder.header(header::CONTENT_TYPE, "application/json");
                Body::from(value.to_string())
            }
            None => Body::empty(),
        };
        app.clone()
            .oneshot(builder.body(body).expect("request"))
            .await
            .expect("response")
    }

    async fn body_json(response: Response) -> serde_json::Value {
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }

    #[tokio::test]
    async fn share_grant_enforces_scope_and_revocation() {
        let dir = tempfile::tempdir().unwrap();
        let grants_path = dir.path().join("connector").join("grants.json");
        let (_tmp, app) = two_wiki_app(grants_path.clone());

        // Owner 创建 demo 的分享，响应带 no-store，路径只含公开 grant_id，secret 只在 fragment。
        let create = send_json(
            &app,
            "POST",
            "/api/v1/config/shares",
            Some("secret"),
            Some(serde_json::json!({
                "wiki": "demo",
                "label": "通用分享",
                "expires_in_days": 30,
                "make_default": true
            })),
        )
        .await;
        assert_eq!(create.status(), StatusCode::OK);
        assert_eq!(
            create
                .headers()
                .get(header::CACHE_CONTROL)
                .and_then(|v| v.to_str().ok()),
            Some("no-store, private")
        );
        let payload = body_json(create).await;
        let link = payload["link"].as_str().unwrap().to_string();
        let grant_id = payload["grant_id"].as_str().unwrap().to_string();
        let (share_path, secret) = link.split_once("#key=").unwrap();
        assert!(share_path.ends_with(&format!("/share/{grant_id}/")));
        assert!(!share_path.contains(secret));
        let token = format!("{grant_id}.{secret}");
        // grants.json 落盘且含明文 secret
        assert!(grants_path.is_file());

        // Share：/wikis 只见 demo
        let wikis: Vec<WikiInfo> = {
            let resp = send_json(&app, "GET", "/api/v1/wikis", Some(&token), None).await;
            assert_eq!(resp.status(), StatusCode::OK);
            serde_json::from_value(body_json(resp).await).unwrap()
        };
        assert_eq!(wikis.len(), 1);
        assert_eq!(wikis[0].key, "demo");

        // Share：授权库可读；越库 / raw / review → 404；config / mcp → 403
        let ok = |uri: &'static str| {
            let app = app.clone();
            let token = token.clone();
            async move {
                send_json(&app, "GET", uri, Some(&token), None)
                    .await
                    .status()
            }
        };
        assert_eq!(ok("/api/v1/wikis/demo/tree").await, StatusCode::OK);
        assert_eq!(ok("/api/v1/wikis/demo/pages/index").await, StatusCode::OK);
        assert_eq!(
            ok("/api/v1/wikis/demo/source-preview?page=index&source=sources%2Freferenced").await,
            StatusCode::OK
        );
        // 只放行该页声明的来源；其他 RAW、RAW 直链与 RAW 目录仍不可见。
        assert_eq!(
            ok("/api/v1/wikis/demo/source-preview?page=index&source=sources%2Fhidden").await,
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            ok("/api/v1/wikis/demo/raw/sources/referenced").await,
            StatusCode::NOT_FOUND
        );
        assert_eq!(ok("/api/v1/wikis/other/tree").await, StatusCode::NOT_FOUND);
        assert_eq!(
            ok("/api/v1/wikis/other/pages/index").await,
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            ok("/api/v1/wikis/demo/raw-tree").await,
            StatusCode::NOT_FOUND
        );
        assert_eq!(ok("/api/v1/wikis/demo/review").await, StatusCode::NOT_FOUND);
        assert_eq!(ok("/api/v1/config/shares").await, StatusCode::FORBIDDEN);
        assert_eq!(ok("/api/v1/config/share").await, StatusCode::FORBIDDEN);
        let mcp = send_json(
            &app,
            "POST",
            "/mcp",
            Some(&token),
            Some(serde_json::json!({"jsonrpc":"2.0","id":1,"method":"tools/list"})),
        )
        .await;
        assert_eq!(mcp.status(), StatusCode::FORBIDDEN);

        // session 端点区分 principal
        let guest =
            body_json(send_json(&app, "GET", "/api/v1/session", Some(&token), None).await).await;
        assert_eq!(guest["principal"], "guest");
        assert_eq!(guest["wiki"], "demo");
        let owner =
            body_json(send_json(&app, "GET", "/api/v1/session", Some("secret"), None).await).await;
        assert_eq!(owner["principal"], "owner");

        // Owner 管理列表公开权限范围但不公开 secret，供设置页直接展示与治理。
        let listed =
            body_json(send_json(&app, "GET", "/api/v1/config/shares", Some("secret"), None).await)
                .await;
        assert_eq!(listed[0]["scope"]["kind"], "whole_wiki");
        assert_eq!(listed[0]["include_raw"], false);
        assert_eq!(listed[0]["is_default"], true);
        assert!(listed[0].get("secret").is_none());

        // Owner 可另建独立分享，并把它切换为 Wiki 的新通用分享。
        let second = body_json(
            send_json(
                &app,
                "POST",
                "/api/v1/config/shares",
                Some("secret"),
                Some(serde_json::json!({
                    "wiki": "demo",
                    "label": "独立分享",
                    "expires_in_days": 7
                })),
            )
            .await,
        )
        .await;
        let second_id = second["grant_id"].as_str().unwrap();
        let made_default = body_json(
            send_json(
                &app,
                "POST",
                &format!("/api/v1/config/shares/{second_id}/default"),
                Some("secret"),
                None,
            )
            .await,
        )
        .await;
        assert_eq!(made_default["is_default"], true);
        let relisted =
            body_json(send_json(&app, "GET", "/api/v1/config/shares", Some("secret"), None).await)
                .await;
        assert_eq!(
            relisted
                .as_array()
                .unwrap()
                .iter()
                .filter(|item| item["is_default"] == true)
                .count(),
            1
        );

        // 撤销后 share 全部 401；link 端点 410；master token 不受影响
        let del = send_json(
            &app,
            "DELETE",
            &format!("/api/v1/config/shares/{grant_id}"),
            Some("secret"),
            None,
        )
        .await;
        assert_eq!(del.status(), StatusCode::OK);
        assert_eq!(
            ok("/api/v1/wikis/demo/tree").await,
            StatusCode::UNAUTHORIZED
        );
        let link_after = send_json(
            &app,
            "POST",
            &format!("/api/v1/config/shares/{grant_id}/link"),
            Some("secret"),
            None,
        )
        .await;
        assert_eq!(link_after.status(), StatusCode::GONE);
        assert_eq!(
            send_json(&app, "GET", "/api/v1/wikis/demo/tree", Some("secret"), None)
                .await
                .status(),
            StatusCode::OK
        );
    }

    #[tokio::test]
    async fn master_token_with_share_like_shape_still_owner() {
        // master token 自定义为 s_ 开头且含 `.`：解析必须 master 优先，仍判 Owner。
        let dir = tempfile::tempdir().unwrap();
        let (_tmp, app) = {
            let grants_path = dir.path().join("grants.json");
            let tmp = tempfile::tempdir().unwrap();
            let root = tmp.path().join("demo");
            let wiki_dir = root.join("wiki");
            fs::create_dir_all(&wiki_dir).unwrap();
            fs::write(wiki_dir.join("index.md"), "---\ntitle: demo\n---\nhi").unwrap();
            let manager = IndexManager::build([WikiEntry {
                key: "demo".to_string(),
                name: "demo".to_string(),
                description: None,
                root_dir: root.clone(),
                wiki_dir,
                raw_dir: root.join("raw"),
                assets_dir: root.join("raw").join("assets"),
                default: true,
            }])
            .unwrap();
            let app = build_router(ServerConfig {
                frontend_dist: None,
                index_manager: manager,
                searcher: FullTextSearcher::in_memory().unwrap(),
                auth_token: "s_custom.master".to_string(),
                watch_wikis: false,
                registry_config: None,
                port: 8800,
                control: None,
                owner_online_url: None,
                public_base_url: None,
                grants_path: Some(grants_path),
            });
            (tmp, app)
        };
        // 用作 Owner：能读 raw-tree（Owner-only 内容）
        assert_eq!(
            status_with_auth(
                &app,
                "/api/v1/wikis/demo/raw-tree",
                "Bearer s_custom.master"
            )
            .await,
            StatusCode::OK
        );
        // 会话判定为 owner
        let session = body_json(
            send_json(
                &app,
                "GET",
                "/api/v1/session",
                Some("s_custom.master"),
                None,
            )
            .await,
        )
        .await;
        assert_eq!(session["principal"], "owner");
    }

    async fn status(app: &Router, uri: &str) -> StatusCode {
        app.clone()
            .oneshot(
                Request::builder()
                    .uri(uri)
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response")
            .status()
    }

    async fn status_with_auth(app: &Router, uri: &str, authorization: &str) -> StatusCode {
        app.clone()
            .oneshot(
                Request::builder()
                    .uri(uri)
                    .header(header::AUTHORIZATION, authorization)
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response")
            .status()
    }

    async fn get_json<T>(app: &Router, uri: &str) -> T
    where
        T: for<'de> serde::Deserialize<'de>,
    {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(uri)
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&body).unwrap()
    }
}
