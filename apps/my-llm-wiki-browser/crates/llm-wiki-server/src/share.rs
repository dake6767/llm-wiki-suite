//! ShareGrant 模型与持久化存储（docs/19「线上分享权限方案」）。
//!
//! 两类 principal 中的 **ShareGrant**：受限、独立撤销的分享凭证。核心不变量：
//! - `grant_id`（`s_` 前缀的公开短 id）出现在管理 API 路径、列表、日志里，可安全传播；
//! - `secret`（144-bit 随机）只在 grants.json、创建响应、link 端点响应三处出现；
//! - 明文落盘（与 master token 同姿态），撤销即清空 secret；
//! - grants.json 原子写入（同目录临时文件、创建时即 0600、失败即清理）。

use std::path::{Path, PathBuf};
use std::sync::{Mutex, RwLock};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::{Deserialize, Serialize};

/// grants.json 顶层 schema 版本，为后续字段迁移预留。
const SCHEMA_VERSION: u32 = 1;

/// last_accessed 的合并落盘节流间隔（秒）：内存实时更新，最多每 5 分钟落盘一次，
/// 不为访问时间精度做每次请求的全量重写。
const LAST_ACCESSED_FLUSH_SECS: i64 = 300;

/// unix 秒。
pub type Timestamp = i64;

pub fn now_ts() -> Timestamp {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// 分享范围。一期恒为 `WholeWiki`；`FrozenSet`/`IndexAnchor` 是已认可的后续演进
/// （docs/19 §7），此处预留枚举成员以避免后续存储迁移。
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Scope {
    #[default]
    WholeWiki,
    FrozenSet {
        pages: Vec<String>,
        assets: Vec<String>,
    },
    IndexAnchor {
        index_page: String,
        live: bool,
    },
}

/// 单个分享授权。序列化后即 grants.json 中的一条记录。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShareGrant {
    /// 公开标识：`s_` + 短随机串。管理 API 路径、列表、日志均用它。
    pub grant_id: String,
    /// 秘密凭证本体：base64url(18 字节)。撤销时置空。
    pub secret: String,
    /// 人类可读标签，撤销时便于辨认（"给小王的分享"）。
    pub label: String,
    /// 绑定单个 wiki key。
    pub wiki: String,
    #[serde(default)]
    pub scope: Scope,
    /// P0 恒 false；创建 API 拒绝 true，服务端对 Share 无条件拒绝 raw 路由。
    #[serde(default)]
    pub include_raw: bool,
    pub created_at: Timestamp,
    /// `None` = 永久（高级选项）。
    #[serde(default)]
    pub expires_at: Option<Timestamp>,
    #[serde(default)]
    pub last_accessed: Option<Timestamp>,
    /// 软删除：置 true 并清空 secret，保留审计元数据。
    #[serde(default)]
    pub revoked: bool,
}

impl ShareGrant {
    /// 该 grant 当前是否可用于鉴权（未撤销且未过期）。
    pub fn is_active(&self, now: Timestamp) -> bool {
        if self.revoked || self.secret.is_empty() {
            return false;
        }
        match self.expires_at {
            Some(exp) => now < exp,
            None => true,
        }
    }
}

/// 鉴权解析结果。`Expired`/`Invalid`/`NotFound` 在 HTTP 层一律映射为 401。
pub enum GrantAuth {
    Valid(ShareGrant),
    Expired,
    Invalid,
    NotFound,
}

#[derive(Debug, Serialize, Deserialize)]
struct GrantData {
    schema_version: u32,
    grants: Vec<ShareGrant>,
}

impl Default for GrantData {
    fn default() -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            grants: Vec::new(),
        }
    }
}

/// 进程内的 grant 存储。`path` 为 `None`（测试/纯浏览器无家目录场景）时纯内存，
/// 写操作不落盘。并发安全：axum 同进程有并发请求，"单机"不等于"单写者"。
pub struct GrantStore {
    path: Option<PathBuf>,
    inner: RwLock<GrantData>,
    /// 上次 last_accessed 落盘时间（unix 秒），用于节流。
    last_flush: Mutex<Timestamp>,
}

impl GrantStore {
    /// 从磁盘加载（缺失/损坏则以空存储启动，损坏会告警但不阻塞服务）。
    pub fn load(path: Option<PathBuf>) -> Self {
        let data = match &path {
            Some(p) => match std::fs::read(p) {
                Ok(bytes) => serde_json::from_slice::<GrantData>(&bytes).unwrap_or_else(|err| {
                    tracing::warn!(error = %err, "grants.json 解析失败，以空存储启动（原文件保留）");
                    GrantData::default()
                }),
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => GrantData::default(),
                Err(err) => {
                    tracing::warn!(error = %err, "读取 grants.json 失败，以空存储启动");
                    GrantData::default()
                }
            },
            None => GrantData::default(),
        };
        Self {
            path,
            inner: RwLock::new(data),
            last_flush: Mutex::new(0),
        }
    }

    /// 创建一个新 grant 并落盘。`expires_at = None` 表示永久。
    pub fn create(
        &self,
        wiki: String,
        label: String,
        expires_at: Option<Timestamp>,
    ) -> std::io::Result<ShareGrant> {
        let grant = ShareGrant {
            grant_id: generate_grant_id(),
            secret: generate_secret(),
            label,
            wiki,
            scope: Scope::WholeWiki,
            include_raw: false,
            created_at: now_ts(),
            expires_at,
            last_accessed: None,
            revoked: false,
        };
        {
            let mut data = self.inner.write().expect("grant store poisoned");
            data.grants.push(grant.clone());
            self.persist(&data)?;
        }
        Ok(grant)
    }

    /// 列出所有 grant（含 secret；调用方负责在响应前剥除）。
    pub fn list(&self) -> Vec<ShareGrant> {
        self.inner
            .read()
            .expect("grant store poisoned")
            .grants
            .clone()
    }

    /// 取单个 grant 快照。
    pub fn get(&self, grant_id: &str) -> Option<ShareGrant> {
        self.inner
            .read()
            .expect("grant store poisoned")
            .grants
            .iter()
            .find(|g| g.grant_id == grant_id)
            .cloned()
    }

    /// 续期/改期，返回更新后的快照；grant 不存在返回 None。
    pub fn renew(
        &self,
        grant_id: &str,
        expires_at: Option<Timestamp>,
    ) -> std::io::Result<Option<ShareGrant>> {
        let mut data = self.inner.write().expect("grant store poisoned");
        let Some(grant) = data.grants.iter_mut().find(|g| g.grant_id == grant_id) else {
            return Ok(None);
        };
        grant.expires_at = expires_at;
        let snapshot = grant.clone();
        self.persist(&data)?;
        Ok(Some(snapshot))
    }

    /// 撤销：置 revoked 并清空 secret。返回更新后的快照；不存在返回 None。
    pub fn revoke(&self, grant_id: &str) -> std::io::Result<Option<ShareGrant>> {
        let mut data = self.inner.write().expect("grant store poisoned");
        let Some(grant) = data.grants.iter_mut().find(|g| g.grant_id == grant_id) else {
            return Ok(None);
        };
        grant.revoked = true;
        grant.secret = String::new();
        let snapshot = grant.clone();
        self.persist(&data)?;
        Ok(Some(snapshot))
    }

    /// 鉴权解析：按 grant_id 索引 + secret 常数时间比对 + 过期/撤销检查。
    /// 命中有效 grant 时更新内存中的 last_accessed（节流落盘）。
    pub fn resolve(&self, grant_id: &str, secret: &str, now: Timestamp) -> GrantAuth {
        let mut data = self.inner.write().expect("grant store poisoned");
        let Some(grant) = data.grants.iter_mut().find(|g| g.grant_id == grant_id) else {
            return GrantAuth::NotFound;
        };
        if grant.revoked || grant.secret.is_empty() {
            return GrantAuth::Invalid;
        }
        if !ct_eq(secret.as_bytes(), grant.secret.as_bytes()) {
            return GrantAuth::Invalid;
        }
        if let Some(exp) = grant.expires_at
            && now >= exp
        {
            return GrantAuth::Expired;
        }
        grant.last_accessed = Some(now);
        let snapshot = grant.clone();
        // last_accessed 合并落盘：节流，避免每次请求全量重写。
        if self.should_flush(now) {
            let _ = self.persist(&data);
        }
        GrantAuth::Valid(snapshot)
    }

    /// 是否到了 last_accessed 落盘窗口（同时更新窗口时间戳）。
    fn should_flush(&self, now: Timestamp) -> bool {
        let mut last = self.last_flush.lock().expect("flush clock poisoned");
        if now - *last >= LAST_ACCESSED_FLUSH_SECS {
            *last = now;
            true
        } else {
            false
        }
    }

    /// 原子写入 grants.json：同目录临时文件、Unix 下创建时即 0600、失败即清理、
    /// 成功后 rename 覆盖。`path` 为 None 时为无操作（纯内存）。
    fn persist(&self, data: &GrantData) -> std::io::Result<()> {
        let Some(path) = &self.path else {
            return Ok(());
        };
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let bytes = serde_json::to_vec_pretty(data)
            .map_err(|err| std::io::Error::new(std::io::ErrorKind::InvalidData, err))?;
        write_atomic_0600(path, &bytes)
    }
}

/// 分享 token 语法 `<grant_id>.<secret>` 的拆分（grant_id 自带 `s_` 前缀，无二层前缀）。
/// 只在第一个 `.` 处切分——secret 是 base64url 不含 `.`，grant_id 也不含。
pub fn split_share_token(token: &str) -> Option<(&str, &str)> {
    let (grant_id, secret) = token.split_once('.')?;
    if grant_id.is_empty() || secret.is_empty() {
        return None;
    }
    Some((grant_id, secret))
}

fn generate_grant_id() -> String {
    let mut bytes = [0u8; 9];
    getrandom::fill(&mut bytes).expect("system RNG unavailable");
    format!("s_{}", URL_SAFE_NO_PAD.encode(bytes))
}

fn generate_secret() -> String {
    let mut bytes = [0u8; 18];
    getrandom::fill(&mut bytes).expect("system RNG unavailable");
    URL_SAFE_NO_PAD.encode(bytes)
}

/// 常数时间比较（长度不同直接返回 false，只泄露长度）。
pub fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b) {
        diff |= x ^ y;
    }
    diff == 0
}

static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

fn write_atomic_0600(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let counter = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let tmp = parent.join(format!(
        ".{}.{}.{}.tmp",
        path.file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("grants.json"),
        std::process::id(),
        counter,
    ));

    let result = write_tmp_then_rename(&tmp, path, bytes);
    if result.is_err() {
        // 写入/rename 失败：清理临时文件，不残留明文 secret。
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

fn write_tmp_then_rename(tmp: &Path, dest: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    {
        #[cfg(unix)]
        let mut file = {
            use std::os::unix::fs::OpenOptionsExt;
            std::fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .mode(0o600)
                .open(tmp)?
        };
        #[cfg(not(unix))]
        let mut file = std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(tmp)?;
        file.write_all(bytes)?;
        file.flush()?;
        file.sync_all()?;
    }
    std::fs::rename(tmp, dest)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn create_resolve_revoke_lifecycle() {
        let store = GrantStore::load(None);
        let grant = store
            .create("demo".into(), "小王".into(), None)
            .unwrap();
        assert!(grant.grant_id.starts_with("s_"));
        assert_eq!(grant.secret.len(), 24);

        let now = now_ts();
        // 有效
        assert!(matches!(
            store.resolve(&grant.grant_id, &grant.secret, now),
            GrantAuth::Valid(_)
        ));
        // 错误 secret
        assert!(matches!(
            store.resolve(&grant.grant_id, "wrong", now),
            GrantAuth::Invalid
        ));
        // 未知 grant
        assert!(matches!(
            store.resolve("s_nope", &grant.secret, now),
            GrantAuth::NotFound
        ));

        // 撤销后 secret 清空、鉴权失败
        let revoked = store.revoke(&grant.grant_id).unwrap().unwrap();
        assert!(revoked.revoked);
        assert!(revoked.secret.is_empty());
        assert!(matches!(
            store.resolve(&grant.grant_id, &grant.secret, now),
            GrantAuth::Invalid
        ));
    }

    #[test]
    fn expired_grant_is_rejected() {
        let store = GrantStore::load(None);
        let now = now_ts();
        let grant = store
            .create("demo".into(), "过期".into(), Some(now - 10))
            .unwrap();
        assert!(matches!(
            store.resolve(&grant.grant_id, &grant.secret, now),
            GrantAuth::Expired
        ));
    }

    #[test]
    fn persists_atomically_and_reloads() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("connector").join("grants.json");
        let grant = {
            let store = GrantStore::load(Some(path.clone()));
            store.create("demo".into(), "持久".into(), None).unwrap()
        };
        // 重新加载能读回
        let store = GrantStore::load(Some(path.clone()));
        let loaded = store.get(&grant.grant_id).unwrap();
        assert_eq!(loaded.secret, grant.secret);

        // 0600 权限
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600);
        }
    }

    #[test]
    fn split_token_rejects_malformed() {
        assert_eq!(split_share_token("s_abc.def"), Some(("s_abc", "def")));
        assert_eq!(split_share_token("nodot"), None);
        assert_eq!(split_share_token(".secret"), None);
        assert_eq!(split_share_token("grant."), None);
    }
}
