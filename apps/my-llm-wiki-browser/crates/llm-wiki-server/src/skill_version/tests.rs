use super::*;

#[test]
fn semver_parse_and_canonical() {
    let v = SemVer::parse("1.3.0").unwrap();
    assert_eq!(v.canonical(), "1.3.0");
    let v = SemVer::parse(" 2.0.1-rc.1+build.5 ").unwrap();
    assert_eq!(v.canonical(), "2.0.1-rc.1+build.5");
}

#[test]
fn semver_rejects_injection() {
    // 注入文本无法通过 SemVer 解析（这是防间接 prompt injection 的核心）。
    for bad in [
        "1.0.0; rm -rf /",
        "$(whoami)",
        "1.0",
        "latest",
        "1.0.0`id`",
        "1.0.0 && echo hi",
        "999.0.0\nmalicious",
    ] {
        assert!(SemVer::parse(bad).is_none(), "should reject: {bad}");
    }
}

#[test]
fn semver_ordering() {
    let a = SemVer::parse("1.3.0").unwrap();
    let b = SemVer::parse("1.2.9").unwrap();
    assert!(a.gt(&b));
    assert!(!b.gt(&a));
    // 假高版本仍是合法 semver（说明为何不能持久化为排序锚——见 §2.3）。
    let fake = SemVer::parse("999.0.0").unwrap();
    assert!(fake.gt(&a));
    // prerelease 小于正式版。
    let pre = SemVer::parse("1.3.0-rc.1").unwrap();
    let rel = SemVer::parse("1.3.0").unwrap();
    assert!(rel.gt(&pre));
    // SemVer 标准按点分标识符比较，而非整串 ASCII 比较。
    let rc10 = SemVer::parse("1.3.0-rc.10").unwrap();
    let rc2 = SemVer::parse("1.3.0-rc.2").unwrap();
    assert!(rc10.gt(&rc2));
    assert!(SemVer::parse("01.3.0").is_none());
}

#[test]
fn validate_candidate_happy() {
    let text = r#"{
        "schema": 1,
        "pack_version": "1.3.0",
        "source_commit": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        "released_at": "2026-07-13T09:00:00Z",
        "pack_notes": "search 拆分补丁"
    }"#;
    let c = validate_candidate(text).unwrap();
    assert_eq!(c.pack_version.canonical(), "1.3.0");
    assert_eq!(c.source_commit.as_deref().unwrap().len(), 40);
    assert_eq!(c.notes.as_deref(), Some("search 拆分补丁"));
}

#[test]
fn validate_candidate_rejects() {
    // schema 必须 === 1
    assert!(validate_candidate(r#"{"schema":2,"pack_version":"1.0.0"}"#).is_none());
    // 缺 schema
    assert!(validate_candidate(r#"{"pack_version":"1.0.0"}"#).is_none());
    // pack_version 注入
    assert!(validate_candidate(r#"{"schema":1,"pack_version":"1.0.0;rm -rf /"}"#).is_none());
    // 非法 JSON
    assert!(validate_candidate("not json").is_none());
    // 超大响应
    let huge = format!(
        r#"{{"schema":1,"pack_version":"1.0.0","pad":"{}"}}"#,
        "x".repeat(17 * 1024)
    );
    assert!(validate_candidate(&huge).is_none());
}

#[test]
fn validate_candidate_drops_bad_source_commit() {
    let text = r#"{"schema":1,"pack_version":"1.0.0","source_commit":"nothex"}"#;
    let c = validate_candidate(text).unwrap();
    assert!(c.source_commit.is_none());
}

#[test]
fn validate_candidate_notes_stripped_of_control_chars() {
    let text = "{\"schema\":1,\"pack_version\":\"1.0.0\",\"pack_notes\":\"line1\\nline2\"}";
    let c = validate_candidate(text).unwrap();
    assert_eq!(c.notes.as_deref(), Some("line1 line2"));
}

#[test]
fn official_remote_normalization() {
    for url in [
        "https://github.com/dake6767/llm-wiki-suite",
        "https://github.com/dake6767/llm-wiki-suite.git",
        "https://github.com/dake6767/llm-wiki-suite/",
        "git@github.com:dake6767/llm-wiki-suite.git",
        "git@github.com:DAKE6767/LLM-WIKI-SUITE",
        "ssh://git@github.com/dake6767/llm-wiki-suite.git",
    ] {
        assert!(is_official_remote(url), "should be official: {url}");
    }
    for url in [
        "http://github.com/dake6767/llm-wiki-suite",
        "https://github.com/evil/llm-wiki-suite",
        "https://gitlab.com/dake6767/llm-wiki-suite",
        "https://github.com/dake6767/other-repo",
        "https://github.com/dake6767/llm-wiki-suite-evil",
    ] {
        assert!(!is_official_remote(url), "should NOT be official: {url}");
    }
}

#[test]
fn gitlink_prompt_contains_data_block_and_bootstrap() {
    let c = Candidate {
        pack_version: SemVer::parse("1.3.0").unwrap(),
        source_commit: Some("a".repeat(40)),
        released_at: None,
        notes: Some("should not leak into prompt".into()),
        min_app_version: None,
    };
    let prompt = build_gitlink_prompt("/Users/x/suite", &c);
    assert!(prompt.contains("bootstrap.sh --repo"));
    assert!(prompt.contains("1.3.0"));
    assert!(prompt.contains("/Users/x/suite"));
    assert!(prompt.contains("merge-base --is-ancestor"));
    // notes 绝不进提示词。
    assert!(!prompt.contains("should not leak"));
    // 数据块是可解析 JSON（提取 ```json 块）。
    let start = prompt.find("```json").unwrap() + 7;
    let end = prompt[start..].find("```").unwrap() + start;
    let json: serde_json::Value = serde_json::from_str(prompt[start..end].trim()).unwrap();
    assert_eq!(json["candidate_version"], "1.3.0");
    assert_eq!(json["class"], "GitLink");
}

#[test]
fn gitlink_prompt_omits_source_commit_when_absent() {
    let c = Candidate {
        pack_version: SemVer::parse("1.3.0").unwrap(),
        source_commit: None,
        released_at: None,
        notes: None,
        min_app_version: None,
    };
    let prompt = build_gitlink_prompt("/Users/x/suite", &c);
    assert!(prompt.contains("只核 `pack_version`"));
    let start = prompt.find("```json").unwrap() + 7;
    let end = prompt[start..].find("```").unwrap() + start;
    let json: serde_json::Value = serde_json::from_str(prompt[start..end].trim()).unwrap();
    assert!(json.get("source_commit").is_none());
}

#[test]
fn unknown_prompt_is_diagnose_only() {
    let prompt = build_unknown_prompt("拷贝安装");
    assert!(prompt.contains("只做诊断"));
    assert!(prompt.contains("不要执行任何"));
    assert!(!prompt.contains("bootstrap.sh --repo")); // 绝不触发自动更新
}

// ——— 源解析：临时目录 + 真实 symlink + 真实 git 检出（GitLink / Copy / Mixed / Absent）———

fn init_official_checkout(dir: &std::path::Path, slug: &str, pack_version: &str) {
    use std::process::Command;
    std::fs::create_dir_all(dir.join("skills").join(slug)).unwrap();
    std::fs::create_dir_all(dir.join("registry")).unwrap();
    std::fs::write(
        dir.join("registry/skills.json"),
        format!(
            r#"{{"version":3,"pack_version":"{pack_version}","skills":[{{"slug":"{slug}"}}]}}"#
        ),
    )
    .unwrap();
    let git = |args: &[&str]| {
        Command::new("git")
            .arg("-C")
            .arg(dir)
            .args(args)
            .output()
            .unwrap();
    };
    git(&["init", "-q"]);
    git(&[
        "remote",
        "add",
        "origin",
        "https://github.com/dake6767/llm-wiki-suite.git",
    ]);
}

#[cfg(unix)]
fn link_skill_dir(source: &std::path::Path, target: &std::path::Path) {
    std::os::unix::fs::symlink(source, target).unwrap();
}

#[cfg(windows)]
fn link_skill_dir(source: &std::path::Path, target: &std::path::Path) {
    // 与 scripts/install.sh 在 Windows Git Bash 下的真实安装方式一致：junction
    // 不需要管理员或 Developer Mode。
    let status = std::process::Command::new("cmd")
        .args(["/C", "mklink", "/J"])
        .arg(target)
        .arg(source)
        .status()
        .unwrap();
    assert!(status.success(), "mklink /J failed");
}

#[test]
fn resolve_gitlink_reads_installed() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("suite");
    init_official_checkout(&repo, "my-llm-wiki", "1.2.0");
    let host = tmp.path().join(".claude/skills");
    std::fs::create_dir_all(&host).unwrap();
    link_skill_dir(&repo.join("skills/my-llm-wiki"), &host.join("my-llm-wiki"));

    let resolved = resolve_source(&[host], &["my-llm-wiki".to_string()]);
    assert_eq!(resolved.info.class, SourceClass::GitLink);
    let top = resolved.toplevel.clone().unwrap();
    // canonicalize 可能带 /private 前缀，比对 basename 即可。
    assert!(top.ends_with("suite"));
    assert_eq!(read_installed_version(&top).unwrap().canonical(), "1.2.0");
}

#[test]
fn resolve_real_dir_is_copy_unknown() {
    let tmp = tempfile::tempdir().unwrap();
    let host = tmp.path().join(".claude/skills");
    // 占了内置 slug 槽位的真实目录 → CopyPlain → Unknown（fail-safe）。
    std::fs::create_dir_all(host.join("my-llm-wiki")).unwrap();
    let resolved = resolve_source(&[host], &["my-llm-wiki".to_string()]);
    assert_eq!(resolved.info.class, SourceClass::Unknown);
}

#[test]
fn resolve_symlink_to_nonofficial_is_unknown() {
    let tmp = tempfile::tempdir().unwrap();
    let repo = tmp.path().join("suite");
    use std::process::Command;
    std::fs::create_dir_all(repo.join("skills/my-llm-wiki")).unwrap();
    std::fs::create_dir_all(repo.join("registry")).unwrap();
    std::fs::write(
        repo.join("registry/skills.json"),
        r#"{"version":3,"pack_version":"1.0.0","skills":[{"slug":"my-llm-wiki"}]}"#,
    )
    .unwrap();
    Command::new("git")
        .arg("-C")
        .arg(&repo)
        .args(["init", "-q"])
        .output()
        .unwrap();
    Command::new("git")
        .arg("-C")
        .arg(&repo)
        .args([
            "remote",
            "add",
            "origin",
            "https://github.com/evil/llm-wiki-suite.git",
        ])
        .output()
        .unwrap();
    let host = tmp.path().join(".claude/skills");
    std::fs::create_dir_all(&host).unwrap();
    link_skill_dir(&repo.join("skills/my-llm-wiki"), &host.join("my-llm-wiki"));

    let resolved = resolve_source(&[host], &["my-llm-wiki".to_string()]);
    assert_eq!(resolved.info.class, SourceClass::Unknown);
}

#[test]
fn resolve_absent_when_nothing_installed() {
    let tmp = tempfile::tempdir().unwrap();
    let host = tmp.path().join(".claude/skills");
    std::fs::create_dir_all(&host).unwrap();
    let resolved = resolve_source(&[host], &["my-llm-wiki".to_string()]);
    assert_eq!(resolved.info.class, SourceClass::Absent);
}

#[test]
fn resolve_mixed_checkouts_is_unknown() {
    let tmp = tempfile::tempdir().unwrap();
    let repo_a = tmp.path().join("suiteA");
    let repo_b = tmp.path().join("suiteB");
    init_official_checkout(&repo_a, "my-llm-wiki", "1.0.0");
    init_official_checkout(&repo_b, "my-llm-wiki", "1.0.0");
    let host1 = tmp.path().join(".claude/skills");
    let host2 = tmp.path().join(".codex/skills");
    std::fs::create_dir_all(&host1).unwrap();
    std::fs::create_dir_all(&host2).unwrap();
    link_skill_dir(
        &repo_a.join("skills/my-llm-wiki"),
        &host1.join("my-llm-wiki"),
    );
    link_skill_dir(
        &repo_b.join("skills/my-llm-wiki"),
        &host2.join("my-llm-wiki"),
    );

    let resolved = resolve_source(&[host1, host2], &["my-llm-wiki".to_string()]);
    assert_eq!(resolved.info.class, SourceClass::Unknown);
}

fn test_manager(app_version: &str) -> SkillVersionManager {
    SkillVersionManager::new(SkillVersionConfig {
        app_version: app_version.into(),
        builtin_slugs: vec![],
        host_targets: vec![],
        endpoints: vec![],
        state_path: None,
        on_change: None,
        notify: None,
    })
}

fn gitlink_source() -> ResolvedSource {
    ResolvedSource {
        info: SourceInfo {
            class: SourceClass::GitLink,
            path: Some("/tmp/suite".into()),
            reason: None,
        },
        toplevel: Some(PathBuf::from("/tmp/suite")),
    }
}

#[test]
fn missing_candidate_is_unknown_not_up_to_date() {
    let info = test_manager("1.0.9").compose(
        &gitlink_source(),
        Some(SemVer::parse("1.0.0").unwrap()),
        None,
    );
    assert_eq!(info.state, SkillState::Unknown);
    assert!(!info.update_available);
    assert!(info.source.reason.unwrap().contains("无法"));
}

#[test]
fn unmet_min_app_is_unknown_without_update_prompt() {
    let candidate = Candidate {
        pack_version: SemVer::parse("1.1.0").unwrap(),
        source_commit: None,
        released_at: None,
        notes: None,
        min_app_version: Some(SemVer::parse("2.0.0").unwrap()),
    };
    let info = test_manager("1.0.9").compose(
        &gitlink_source(),
        Some(SemVer::parse("1.0.0").unwrap()),
        Some(candidate),
    );
    assert_eq!(info.state, SkillState::Unknown);
    assert!(info.min_app_unsatisfied);
    assert!(!info.update_available);
    assert!(info.update_prompt.is_empty());
}

#[tokio::test]
async fn check_emits_checking_then_final_state() {
    let states = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let observed = states.clone();
    let manager = SkillVersionManager::new(SkillVersionConfig {
        app_version: "1.0.9".into(),
        builtin_slugs: vec![],
        host_targets: vec![],
        endpoints: vec![],
        state_path: None,
        on_change: Some(std::sync::Arc::new(move |info| {
            observed.lock().unwrap().push(info.state.clone());
        })),
        notify: None,
    });

    let final_info = manager.check().await;
    assert_eq!(final_info.state, SkillState::Idle);
    assert_eq!(
        *states.lock().unwrap(),
        vec![SkillState::Checking, SkillState::Idle]
    );
}

async fn version_endpoint(body: String) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let app = axum::Router::new().route(
        "/",
        axum::routing::get(move || {
            let body = body.clone();
            async move { body }
        }),
    );
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    format!("http://{address}/")
}

#[tokio::test]
async fn fetch_latest_rejects_invalid_and_oversize_then_falls_back() {
    let invalid =
        version_endpoint(r#"{"schema":1,"pack_version":"1.0.0; touch /tmp/pwned"}"#.into()).await;
    let oversized = version_endpoint("x".repeat(MAX_VERSION_BYTES + 1)).await;
    let valid = version_endpoint(r#"{"schema":1,"pack_version":"1.2.3"}"#.into()).await;
    let manager = SkillVersionManager::new(SkillVersionConfig {
        app_version: "1.0.9".into(),
        builtin_slugs: vec![],
        host_targets: vec![],
        endpoints: vec![invalid, oversized, valid],
        state_path: None,
        on_change: None,
        notify: None,
    });

    let candidate = manager.fetch_latest().await.unwrap();
    assert_eq!(candidate.pack_version.canonical(), "1.2.3");
}

fn update_candidate() -> Candidate {
    Candidate {
        pack_version: SemVer::parse("1.1.0").unwrap(),
        source_commit: None,
        released_at: None,
        notes: None,
        min_app_version: None,
    }
}

#[test]
fn notification_is_marked_only_after_success() {
    for (notification_result, remains_pending) in [(false, true), (true, false)] {
        let manager = SkillVersionManager::new(SkillVersionConfig {
            app_version: "1.0.9".into(),
            builtin_slugs: vec![],
            host_targets: vec![],
            endpoints: vec![],
            state_path: None,
            on_change: None,
            notify: Some(std::sync::Arc::new(move |_| notification_result)),
        });
        let info = manager.compose(
            &gitlink_source(),
            Some(SemVer::parse("1.0.0").unwrap()),
            Some(update_candidate()),
        );
        manager.inner.lock().unwrap().last = info;

        manager.emit_pending_notification();

        assert_eq!(manager.pending_notification().is_some(), remains_pending);
    }
}
