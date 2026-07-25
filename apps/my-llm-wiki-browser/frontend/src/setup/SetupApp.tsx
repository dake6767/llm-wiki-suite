import { useEffect, useMemo, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { copyToClipboard } from "../lib/reviewPrompt";

type DestinationState = "absent" | "owned" | "foreign";
type SetupHealth = "ready" | "not-configured" | "needs-repair" | "action-required";

interface SkillDestination {
  slug: string;
  path: string;
  state: DestinationState;
}

interface HostInspection {
  id: string;
  label: string;
  detected: boolean;
  skills_dir: string;
  destinations: SkillDestination[];
}

interface PackStatus {
  id: string;
  version: string | null;
  installed: boolean;
  healthy: boolean;
}

interface ModelCacheStatus {
  id: string;
  path: string;
  ready: boolean;
}

interface WikiStatus {
  path: string;
  collection_root: string;
  registry_path: string;
  ready: boolean;
}

interface SetupInspection {
  distribution_version: string;
  state_path: string;
  cli_path: string | null;
  hosts: HostInspection[];
  wiki: WikiStatus;
  official_toolchain: PackStatus;
  install_root: string;
  install_anchor: string;
  install_root_relocated: boolean;
}

interface InstallRootProbe {
  path: string;
  exists: boolean;
  writable: boolean;
  free_bytes: number | null;
  existing_install: boolean;
}

interface HostResult {
  skills_dir: string;
  installed: string[];
  healthy: boolean;
}

interface ManualAction {
  id: string;
  title: string;
  detail: string;
}

interface SetupResult {
  state: SetupHealth;
  distribution_version: string;
  cli_path: string | null;
  hosts: Record<string, HostResult>;
  wiki: WikiStatus;
  official_toolchain: PackStatus;
  packs: Record<string, PackStatus>;
  model_caches: Record<string, ModelCacheStatus>;
  backups: string[];
  actions: ManualAction[];
}

interface UpdateResult {
  state: "up-to-date" | "available" | "restart-required" | "updated";
  current_version: string;
  latest_version: string;
  restart_required: boolean;
}

interface BrowserUpdateStatus {
  current_version: string;
  state: "idle" | "checking" | "up-to-date" | "available" | "downloading" | "ready-to-restart" | "portable" | "error";
  latest_version?: string;
  notes?: string;
  downloaded?: number;
  total?: number;
  error?: string;
}

interface Progress {
  phase: string;
  message: string;
  current: number;
  total: number;
  detail_percent?: number;
}

interface PackInstallJob {
  id: string;
  state: "idle" | "running" | "installed" | "error";
  message: string;
  detail_percent?: number;
  error?: string;
}

interface RepairJob {
  state: "idle" | "running" | "stopping" | "done" | "error" | "cancelled";
  phase: string;
  message: string;
  current: number;
  total: number;
  detail_percent?: number;
  /** Epoch ms, same clock as Date.now(): both come from this machine. */
  started_at: number;
  /** Epoch ms of the last real step — the basis for the stall warning. */
  updated_at: number;
  error?: string;
}

/** No progress for this long means the run is stuck, not merely slow. */
const REPAIR_STALL_MS = 25_000;

type View = "loading" | "welcome" | "hosts" | "review" | "progress" | "complete" | "manage";

const STEP_LABELS = ["欢迎", "选择宿主", "确认", "安装", "完成"];

export default function SetupApp() {
  const preview = ["127.0.0.1", "localhost"].includes(window.location.hostname)
    ? new URLSearchParams(window.location.search).get("preview")
    : null;
  const previewReview = preview === "review";
  const previewToolchain = preview === "progress-toolchain";
  const previewProgress = preview === "progress" || previewToolchain;
  const previewComplete = preview === "complete";
  // A stuck repair is the state hardest to reach on purpose: it needs a broken
  // install plus a network that neither works nor fails. Reachable here as
  // ?preview=repair, ?preview=repair-stalled and ?preview=repair-done.
  const previewStalled = preview === "repair-stalled";
  const previewRepairDone = preview === "repair-done";
  const previewRepair = preview === "repair" || previewStalled || previewRepairDone;
  const previewMode = previewReview || previewProgress || previewComplete || previewRepair;
  const previewHost = previewProgress
    ? { id: "workbuddy", label: "WorkBuddy", skills_dir: "~/.workbuddy/skills" }
    : { id: "codex", label: "Codex", skills_dir: "~/.codex/skills" };
  const previewInspection: SetupInspection | null = previewMode ? {
    distribution_version: "preview",
    state_path: "",
    cli_path: null,
    hosts: [{
      id: previewHost.id,
      label: previewHost.label,
      detected: true,
      skills_dir: previewHost.skills_dir,
      destinations: [],
    }],
    wiki: { path: "~/wikis/my-llm-wiki", collection_root: "~/wikis", registry_path: "", ready: false },
    official_toolchain: { id: "toolchain-base", version: null, installed: false, healthy: false },
    install_root: "~/.my-llm-wiki",
    install_anchor: "~/.my-llm-wiki",
    install_root_relocated: false,
  } : null;
  const previewBroken = previewRepair && !previewRepairDone;
  const previewStatus: SetupResult | null = previewComplete || previewRepair ? {
    state: previewBroken ? "needs-repair" : "ready",
    distribution_version: "preview",
    cli_path: "~/.my-llm-wiki/bin/my-llm-wiki",
    hosts: {
      codex: {
        skills_dir: "~/.codex/skills",
        installed: ["my-llm-wiki", "my-llm-wiki-search", "my-llm-wiki-video", "my-llm-wiki-x"],
        healthy: true,
      },
    },
    wiki: {
      path: "~/wikis/my-llm-wiki",
      collection_root: "~/wikis",
      registry_path: "~/.my-llm-wiki/wikis.json",
      ready: true,
    },
    official_toolchain: { id: "toolchain-base", version: "preview", installed: !previewBroken, healthy: !previewBroken },
    packs: {
      "toolchain-base": { id: "toolchain-base", version: "preview", installed: !previewBroken, healthy: !previewBroken },
    },
    model_caches: {
      "asr-zh": { id: "asr-zh", path: "~/.my-llm-wiki/models/asr-zh", ready: false },
    },
    backups: [],
    actions: [{
      id: "opencli-browser-bridge",
      title: "加载 OpenCLI Browser Bridge",
      detail: "在 Chrome 扩展管理页开启开发者模式，并加载 ~/.my-llm-wiki/packs/toolchain-base/web/extension。",
    }],
  } : null;
  const [view, setView] = useState<View>(
    previewRepair ? "manage"
      : previewComplete ? "complete"
      : previewProgress ? "progress"
      : previewReview ? "review"
      : "loading",
  );
  const [inspection, setInspection] = useState<SetupInspection | null>(previewInspection);
  const [status, setStatus] = useState<SetupResult | null>(previewStatus);
  const [selectedHosts, setSelectedHosts] = useState<Set<string>>(new Set(previewMode ? [previewHost.id] : []));
  const [approvedConflicts, setApprovedConflicts] = useState<Set<string>>(new Set());
  const [installToolchain, setInstallToolchain] = useState(true);
  const [wikiPath, setWikiPath] = useState(previewMode ? "~/wikis" : "");
  const [installRoot, setInstallRoot] = useState(previewMode ? "~/.my-llm-wiki" : "");
  const [installRootProbe, setInstallRootProbe] = useState<InstallRootProbe | null>(null);
  const [progress, setProgress] = useState<Progress>(previewToolchain ? {
    phase: "toolchain",
    message: "正在下载推荐工具链",
    current: 2,
    total: 3,
    detail_percent: 64,
  } : previewProgress ? {
    phase: "skills",
    message: "正在为 WorkBuddy 激活 my-llm-wiki-video",
    current: 0,
    total: 3,
    detail_percent: 57,
  } : {
    phase: "preparing",
    message: "安装任务已启动",
    current: 0,
    total: 1,
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [update, setUpdate] = useState<UpdateResult | null>(null);
  const [browserUpdate, setBrowserUpdate] = useState<BrowserUpdateStatus | null>(null);
  const [pickingWikiPath, setPickingWikiPath] = useState(false);
  const [pickingInstallRoot, setPickingInstallRoot] = useState(false);
  const [asrInstall, setAsrInstall] = useState<PackInstallJob | null>(null);
  const [repairJob, setRepairJob] = useState<RepairJob | null>(previewRepairDone ? {
    state: "done",
    phase: "pack",
    message: "修复完成，所有组件已通过校验",
    current: 4,
    total: 4,
    started_at: Date.now() - 260_000,
    updated_at: Date.now(),
  } : previewRepair ? {
    state: "running",
    phase: "pack",
    message: "正在下载 toolchain-base 能力包",
    current: 2,
    total: 4,
    detail_percent: 37,
    started_at: Date.now() - 194_000,
    updated_at: Date.now() - (previewStalled ? 96_000 : 1_000),
  } : null);

  useEffect(() => {
    if (previewMode) return;
    let mounted = true;
    const unlisten = listen<Progress>("setup-progress", (event) => {
      if (mounted) setProgress(event.payload);
    }).catch((reason: unknown) => {
      // A rejected listen() means the setup window lacks the core:event ACL
      // grant; surface it instead of silently freezing the progress bar.
      console.error("failed to subscribe to setup-progress events", reason);
      return () => undefined;
    });
    Promise.all([
      invoke<SetupInspection>("setup_inspect"),
      invoke<SetupResult>("setup_status"),
      invoke<BrowserUpdateStatus>("setup_browser_update_status"),
      invoke<PackInstallJob>("setup_pack_install_status", { id: "asr-zh" }),
      // A repair started earlier keeps running in the app process, so reopening
      // or reloading this window must rejoin it rather than offer a fresh
      // button that would only fail on the setup lock.
      invoke<RepairJob>("setup_repair_status"),
    ])
      .then(([nextInspection, nextStatus, nextBrowserUpdate, nextAsrInstall, nextRepair]) => {
        if (!mounted) return;
        setInspection(nextInspection);
        setStatus(nextStatus);
        setBrowserUpdate(nextBrowserUpdate);
        setAsrInstall(nextAsrInstall);
        setRepairJob(nextRepair);
        setSelectedHosts(new Set());
        setWikiPath(nextInspection.wiki.collection_root);
        setInstallRoot(nextInspection.install_root);
        setView(nextStatus.state === "not-configured" ? "welcome" : "manage");
      })
      .catch((reason: unknown) => {
        if (!mounted) return;
        setError(messageOf(reason));
        setView("welcome");
      });
    return () => {
      mounted = false;
      void unlisten.then((dispose) => dispose());
    };
  }, []);

  // Free space is the reason people move the install, so the chosen location is
  // checked while it is being typed rather than only when a multi-gigabyte
  // download runs out of room.
  useEffect(() => {
    if (previewMode) return;
    const path = installRoot.trim();
    if (!path) {
      setInstallRootProbe(null);
      return;
    }
    let mounted = true;
    const timer = setTimeout(() => {
      invoke<InstallRootProbe>("setup_probe_install_root", { path })
        .then((probe) => mounted && setInstallRootProbe(probe))
        .catch(() => mounted && setInstallRootProbe(null));
    }, 250);
    return () => {
      mounted = false;
      clearTimeout(timer);
    };
  }, [installRoot]);

  const pollingBrowserUpdate =
    browserUpdate?.state === "checking" || browserUpdate?.state === "downloading";
  useEffect(() => {
    if (!pollingBrowserUpdate) return;
    const timer = window.setTimeout(() => {
      invoke<BrowserUpdateStatus>("setup_browser_update_status")
        .then(setBrowserUpdate)
        .catch(() => undefined);
    }, browserUpdate?.state === "downloading" ? 1000 : 1500);
    return () => window.clearTimeout(timer);
  }, [pollingBrowserUpdate, browserUpdate]);

  const pollingAsrInstall = asrInstall?.state === "running";
  useEffect(() => {
    if (previewMode || !pollingAsrInstall) return;
    const timer = window.setTimeout(() => {
      invoke<PackInstallJob>("setup_pack_install_status", { id: "asr-zh" })
        .then(setAsrInstall)
        .catch(() => undefined);
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [pollingAsrInstall, asrInstall]);

  useEffect(() => {
    if (previewMode || asrInstall?.state !== "installed" || status?.packs["asr-zh"]?.healthy) return;
    invoke<SetupResult>("setup_status").then(setStatus).catch(() => undefined);
  }, [asrInstall?.state, status]);

  const repairRunning = repairJob?.state === "running" || repairJob?.state === "stopping";
  useEffect(() => {
    if (previewMode || !repairRunning) return;
    const timer = window.setTimeout(() => {
      invoke<RepairJob>("setup_repair_status").then(setRepairJob).catch(() => undefined);
    }, 700);
    return () => window.clearTimeout(timer);
  }, [repairRunning, repairJob]);

  useEffect(() => {
    if (previewMode || !repairJob || repairRunning || repairJob.state === "idle") return;
    invoke<SetupResult>("setup_status").then(setStatus).catch(() => undefined);
  }, [repairJob?.state]);

  const selected = useMemo(
    () => inspection?.hosts.filter((host) => selectedHosts.has(host.id)) ?? [],
    [inspection, selectedHosts],
  );
  const conflicts = useMemo(
    () => selected.flatMap((host) => host.destinations.filter((item) => item.state === "foreign")),
    [selected],
  );
  const conflictsApproved = conflicts.every((item) => approvedConflicts.has(item.path));

  function toggleHost(id: string) {
    setSelectedHosts((current) => toggled(current, id));
  }

  function toggleConflict(path: string) {
    setApprovedConflicts((current) => toggled(current, path));
  }

  async function apply() {
    if (!selectedHosts.size || !conflictsApproved) return;
    setBusy(true);
    setError(null);
    setView("progress");
    setProgress({
      phase: "preparing",
      message: "安装任务已启动",
      current: 0,
      total: selectedHosts.size + 2 + Number(installToolchain),
    });
    try {
      const result = await invoke<SetupResult>("setup_apply", {
        request: {
          hosts: [...selectedHosts],
          replace: [...approvedConflicts],
          install_official_toolchain: installToolchain,
          wiki_path: wikiPath.trim(),
          install_root: installRoot.trim() || null,
        },
      });
      setStatus(result);
      setView("complete");
    } catch (reason) {
      setError(messageOf(reason));
      setView("review");
    } finally {
      setBusy(false);
    }
  }

  async function pickInstallRoot() {
    if (previewMode) return;
    setPickingInstallRoot(true);
    setError(null);
    try {
      const selected = await invoke<string | null>("setup_pick_install_directory", {
        current: installRoot.trim() || null,
      });
      if (selected) setInstallRoot(selected);
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setPickingInstallRoot(false);
    }
  }

  async function pickWikiPath() {
    if (previewMode) return;
    setPickingWikiPath(true);
    setError(null);
    try {
      const selected = await invoke<string | null>("setup_pick_wiki_directory", {
        current: wikiPath.trim() || null,
      });
      if (selected) setWikiPath(selected);
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setPickingWikiPath(false);
    }
  }

  async function repair() {
    setError(null);
    try {
      setRepairJob(await invoke<RepairJob>("setup_start_repair"));
    } catch (reason) {
      setError(messageOf(reason));
    }
  }

  async function stopRepair() {
    setError(null);
    try {
      setRepairJob(await invoke<RepairJob>("setup_stop_repair"));
    } catch (reason) {
      setError(messageOf(reason));
    }
  }

  async function checkUpdate(applyUpdate = false) {
    setBusy(true);
    setError(null);
    try {
      setUpdate(await invoke<UpdateResult>("setup_update", { check: !applyUpdate }));
      setBrowserUpdate(await invoke<BrowserUpdateStatus>("setup_browser_update_status"));
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  }

  async function restartForUpdate() {
    setBusy(true);
    setError(null);
    try {
      await invoke("setup_restart");
    } catch (reason) {
      setError(messageOf(reason));
      setBusy(false);
    }
  }

  async function openWiki() {
    if (previewMode) return;
    setError(null);
    try {
      await invoke("setup_open_wiki");
    } catch (reason) {
      setError(messageOf(reason));
    }
  }

  async function openWikiDirectory() {
    if (previewMode) return;
    setError(null);
    try {
      await invoke("setup_open_wiki_directory");
    } catch (reason) {
      setError(messageOf(reason));
    }
  }

  async function installAsr() {
    if (previewMode) {
      setAsrInstall({
        id: "asr-zh",
        state: "running",
        message: "正在下载并预热 fsmn-vad 与 SenseVoiceSmall",
        detail_percent: 42,
      });
      return;
    }
    setError(null);
    try {
      setAsrInstall(await invoke<PackInstallJob>("setup_start_pack_install", { id: "asr-zh" }));
    } catch (reason) {
      setError(messageOf(reason));
    }
  }

  if (view === "loading") return <LoadingScreen />;
  if (view === "manage") {
    return (
      <Management
        status={status}
        update={update}
        browserUpdate={browserUpdate}
        busy={busy}
        error={error}
        repair={repairJob}
        onStopRepair={() => void stopRepair()}
        onRepair={() => void repair()}
        onCheck={() => void checkUpdate(false)}
        onUpdate={() => void checkUpdate(true)}
        onRestart={() => void restartForUpdate()}
        asrInstall={asrInstall}
        onInstallAsr={() => void installAsr()}
      />
    );
  }

  const step = viewStep(view);
  return (
    <main className="setup-shell">
      <SetupRail active={step} version={inspection?.distribution_version} />
      <section className={`setup-stage${view === "complete" ? " is-complete" : ""}`}>
        {error ? <ErrorBanner message={error} /> : null}
        {view === "welcome" ? (
          <Welcome onContinue={() => setView("hosts")} />
        ) : view === "hosts" ? (
          <HostSelection
            hosts={inspection?.hosts ?? []}
            selected={selectedHosts}
            onToggle={toggleHost}
            onBack={() => setView("welcome")}
            onContinue={() => setView("review")}
          />
        ) : view === "review" ? (
          <Review
            hosts={selected}
            conflicts={conflicts}
            approved={approvedConflicts}
            installToolchain={installToolchain}
            wikiPath={wikiPath}
            pickingWikiPath={pickingWikiPath}
            installRoot={installRoot}
            installRootProbe={installRootProbe}
            pickingInstallRoot={pickingInstallRoot}
            onToggleConflict={toggleConflict}
            onToggleToolchain={() => setInstallToolchain((value) => !value)}
            onWikiPathChange={setWikiPath}
            onPickWikiPath={() => void pickWikiPath()}
            onInstallRootChange={setInstallRoot}
            onPickInstallRoot={() => void pickInstallRoot()}
            onBack={() => setView("hosts")}
            onApply={() => void apply()}
            disabled={
              busy
              || !selectedHosts.size
              || !conflictsApproved
              || !wikiPath.trim()
              || !installRoot.trim()
            }
          />
        ) : view === "progress" ? (
          <ProgressView
            progress={progress}
            tasks={[
              "展开 Skills Pack 到安装目录",
              ...[...selected]
                .sort((left, right) => left.id.localeCompare(right.id))
                .map((host) => `为 ${host.label} 激活 Skills Pack`),
              "初始化 Wiki 与 RAW 目录",
              ...(installToolchain ? ["安装并校验推荐工具链"] : []),
            ]}
          />
        ) : (
          <Complete
            status={status}
            asrInstall={asrInstall}
            onInstallAsr={() => void installAsr()}
            onManage={() => setView("manage")}
            onOpenWiki={() => void openWiki()}
            onOpenWikiDirectory={() => void openWikiDirectory()}
          />
        )}
      </section>
    </main>
  );
}

function SetupRail({ active, version }: { active: number; version?: string }) {
  return (
    <aside className="setup-rail">
      <div>
        <div className="setup-mark">文</div>
        <p className="mt-4 font-display text-xl text-paper">My LLM Wiki</p>
        <p className="font-mono mt-1 text-[0.62rem] uppercase tracking-[0.2em] text-cream-soft">
          Field Kit · {version ? `v${version}` : "local"}
        </p>
      </div>
      <ol className="setup-steps" aria-label="安装步骤">
        {STEP_LABELS.map((label, index) => (
          <li key={label} className={index === active ? "active" : index < active ? "done" : ""}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <b>{label}</b>
          </li>
        ))}
      </ol>
      <p className="setup-rail-note">本页由应用直接加载。安装能力不经过本地 Web 服务。</p>
    </aside>
  );
}

function Welcome({ onContinue }: { onContinue: () => void }) {
  return (
    <div className="setup-copy reveal">
      <p className="eyebrow">Setup / verified baseline</p>
      <h1>把可靠的工具，<br />放在每次工作之前。</h1>
      <p className="setup-lead">
        安装完整 Skills Pack 与项目验证过的工具链。Skill 仍然允许你改用自己的工具，
        但默认路径从第一篇网页、第一段视频开始就可工作。
      </p>
      <div className="setup-principles">
        <Principle number="01" title="默认有保证" body="FFmpeg、OpenCLI、文档转换与视频基础能力采用固定发布组合。" />
        <Principle number="02" title="替换有自由" body="任务里你明确指定的工具始终优先；不想用推荐工具链时，也可以随时换成自己的。" />
        <Principle number="03" title="数据留在本地" body="Wiki 与 RAW 不会随修复、更新或卸载被删除。" />
      </div>
      <div className="setup-actions"><PrimaryButton onClick={onContinue}>开始设置</PrimaryButton></div>
    </div>
  );
}

function Principle({ number, title, body }: { number: string; title: string; body: string }) {
  return (
    <article><span>{number}</span><h2>{title}</h2><p>{body}</p></article>
  );
}

function HostSelection({ hosts, selected, onToggle, onBack, onContinue }: {
  hosts: HostInspection[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onBack: () => void;
  onContinue: () => void;
}) {
  return (
    <div className="setup-copy reveal">
      <p className="eyebrow">02 / Agent hosts</p>
      <h1>Skills 要出现在哪里？</h1>
      <p className="setup-lead">请选择需要接收完整 Skills Pack 的 Agent。检测状态仅作提示，不会代你选择。</p>
      <div className="host-ledger">
        {hosts.map((host) => (
          <label key={host.id} className={selected.has(host.id) ? "selected" : ""}>
            <input type="checkbox" checked={selected.has(host.id)} onChange={() => onToggle(host.id)} />
            <span className="host-check" aria-hidden="true">{selected.has(host.id) ? "✓" : ""}</span>
            <span className="host-name"><b>{host.label}</b><small>{host.skills_dir}</small></span>
            <span className={host.detected ? "detected" : "not-detected"}>{host.detected ? "已检测" : "未检测"}</span>
          </label>
        ))}
      </div>
      <div className="setup-actions"><SecondaryButton onClick={onBack}>返回</SecondaryButton><PrimaryButton disabled={!selected.size} onClick={onContinue}>继续确认</PrimaryButton></div>
    </div>
  );
}

function Review({ hosts, conflicts, approved, installToolchain, wikiPath, pickingWikiPath, installRoot, installRootProbe, pickingInstallRoot, onToggleConflict, onToggleToolchain, onWikiPathChange, onPickWikiPath, onInstallRootChange, onPickInstallRoot, onBack, onApply, disabled }: {
  hosts: HostInspection[];
  conflicts: SkillDestination[];
  approved: Set<string>;
  installToolchain: boolean;
  wikiPath: string;
  pickingWikiPath: boolean;
  installRoot: string;
  installRootProbe: InstallRootProbe | null;
  pickingInstallRoot: boolean;
  onToggleConflict: (path: string) => void;
  onToggleToolchain: () => void;
  onWikiPathChange: (path: string) => void;
  onPickWikiPath: () => void;
  onInstallRootChange: (path: string) => void;
  onPickInstallRoot: () => void;
  onBack: () => void;
  onApply: () => void;
  disabled: boolean;
}) {
  return (
    <div className="setup-copy review-copy reveal">
      <p className="eyebrow">03 / One confirmation</p>
      <h1>一次确认，之后无人值守。</h1>
      <div className="review-sheet">
        <ReviewRow label="Skills Pack" value={`完整安装 · ${hosts.length} 个宿主`} note={hosts.map((host) => host.label).join("、")} />
        <RecommendedToolchain install={installToolchain} onToggle={onToggleToolchain} />
        <InstallLocation
          path={installRoot}
          probe={installRootProbe}
          picking={pickingInstallRoot}
          onChange={onInstallRootChange}
          onPick={onPickInstallRoot}
        />
        <WikiLocation path={wikiPath} picking={pickingWikiPath} onChange={onWikiPathChange} onPick={onPickWikiPath} />
      </div>
      {conflicts.length ? (
        <section className="conflict-box">
          <h2>发现外来 Skill</h2>
          <p>Setup 不会静默覆盖。逐项授权后，原目录会先移入备份。</p>
          {conflicts.map((item) => (
            <label key={item.path}><input type="checkbox" checked={approved.has(item.path)} onChange={() => onToggleConflict(item.path)} /><span><b>备份并替换 {item.slug}</b><small>{item.path}</small></span></label>
          ))}
        </section>
      ) : null}
      <div className="setup-actions"><SecondaryButton onClick={onBack}>返回</SecondaryButton><PrimaryButton disabled={disabled} onClick={onApply}>确认并安装</PrimaryButton></div>
    </div>
  );
}

function ReviewRow({ label, value, note, action }: { label: string; value: string; note: string; action?: React.ReactNode }) {
  return <div><span>{label}</span><div><b>{value}</b><small>{note}</small></div>{action}</div>;
}

function InstallLocation({ path, probe, picking, onChange, onPick }: {
  path: string;
  probe: InstallRootProbe | null;
  picking: boolean;
  onChange: (path: string) => void;
  onPick: () => void;
}) {
  const blocked = probe && probe.exists && !probe.existing_install && !probe.writable;
  return (
    <div className="wiki-location-row">
      <span>安装位置</span>
      <div className="wiki-location-copy">
        <b>选择工具链与 Skills 的安装目录</b>
        <div className="wiki-path-control">
          <input
            aria-label="工具链安装目录"
            value={path}
            onChange={(event) => onChange(event.target.value)}
            placeholder="~/.my-llm-wiki"
            spellCheck={false}
          />
          <button type="button" onClick={onPick} disabled={picking}>
            {picking ? "正在打开…" : "选择文件夹"}
          </button>
        </div>
        <small>
          工具链、语音模型和 Skills 都会装在这里，合计可能超过 5 GB。放到非系统盘可以省下系统盘空间，重装系统后数据也还在。
          {probe?.existing_install
            ? " 该目录已有一份安装，将直接复用。"
            : blocked
              ? " 该目录不可写，请换一个位置。"
              : null}
          {probe?.free_bytes != null ? ` 可用空间 ${formatBytes(probe.free_bytes)}。` : null}
        </small>
      </div>
    </div>
  );
}

function formatBytes(bytes: number) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function WikiLocation({ path, picking, onChange, onPick }: {
  path: string;
  picking: boolean;
  onChange: (path: string) => void;
  onPick: () => void;
}) {
  return (
    <div className="wiki-location-row">
      <span>Wiki</span>
      <div className="wiki-location-copy">
        <b>选择存放 Wiki 的目录</b>
        <div className="wiki-path-control">
          <input
            aria-label="Wiki 存放目录"
            value={path}
            onChange={(event) => onChange(event.target.value)}
            placeholder="~/wikis"
            spellCheck={false}
          />
          <button type="button" onClick={onPick} disabled={picking}>
            {picking ? "正在打开…" : "选择文件夹"}
          </button>
        </div>
        <small>会在此目录下创建默认知识库 <code>my-llm-wiki</code>；日后可让 Agent 在同级新建更多知识库（如 <code>ai-wiki</code>）。可用系统选择器，或填写绝对路径 / 以 ~/ 开头；已有知识库将直接复用。</small>
      </div>
    </div>
  );
}

function RecommendedToolchain({ install, onToggle }: { install: boolean; onToggle: () => void }) {
  const tools = [
    ["OpenCLI", "采集网页及需要登录态的社交内容"],
    ["yt-dlp", "获取在线视频的字幕、元数据与音频"],
    ["FFmpeg", "完成音视频转码与音频提取"],
    ["MarkItDown", "将 PDF、DOCX、PPTX、XLSX、EPUB 转为 Markdown"],
  ];

  return (
    <div className="toolchain-review">
      <span>推荐工具链</span>
      <div className="toolchain-review-copy">
        <b>{install ? "安装并默认优先" : "不安装"}</b>
        {install ? (
          <>
            <p>为了保证 Agent 运行效果，推荐安装以下工具套装：</p>
            <p className="toolchain-isolation-note">
              <strong>隔离安装</strong>
              <span>安装在 My LLM Wiki 私有目录，不覆盖已有的同名工具，也不修改全局 PATH。</span>
            </p>
            <ul>
              {tools.map(([name, purpose]) => (
                <li key={name}><strong>{name}</strong><small>{purpose}</small></li>
              ))}
            </ul>
            <small className="toolchain-asr-note">
              处理中文类视频时，Agent 会先提取音频，再将语音转写为文本。基础安装不会拖慢首次进入 Wiki；完成后可点击“安装并预热”，由 Browser 在后台准备 FunASR、fsmn-vad 与 SenseVoiceSmall。
            </small>
          </>
        ) : (
          <small>不会安装推荐工具套装，将使用你已经安装好的工具。</small>
        )}
      </div>
          <button className="text-toggle" onClick={onToggle}>{install ? "不装这些工具，用我已经安装好的工具" : "恢复推荐设置"}</button>
    </div>
  );
}

function ProgressView({ progress, tasks }: { progress: Progress; tasks: string[] }) {
  const ratio = progress.total ? Math.round((progress.current / progress.total) * 100) : 0;
  const starting = progress.total === 0;
  const complete = progress.total > 0 && progress.current >= progress.total;
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <div className="progress-view rise">
      <p className="eyebrow">04 / Applying</p>
      <div className={`progress-number${starting ? " is-active" : ""}`}>
        {starting ? <>进行中<span className="working-dots" aria-hidden="true"><i /><i /><i /></span></> : <>{String(ratio).padStart(2, "0")}<small>%</small></>}
      </div>
      <h1 aria-live="polite">{progress.message}</h1>
      <div
        className={`progress-track${starting ? " is-indeterminate" : ""}${complete ? " is-complete" : ""}`}
        role="progressbar"
        aria-label="安装整体进度"
        aria-valuemin={0}
        aria-valuemax={progress.total}
        aria-valuenow={starting ? undefined : progress.current}
        aria-valuetext={starting ? "安装已经开始" : `已完成 ${progress.current} / ${progress.total}`}
      >
        <i style={starting ? undefined : { width: `${ratio}%` }} />
      </div>
      <div className="progress-meta">
        <span>整体进度 {progress.current} / {progress.total}</span>
        <span>{formatElapsed(elapsed)}</span>
      </div>
      {!complete ? (
        <div className="detail-progress" aria-live="polite">
          <div>
            <span>{progress.phase === "toolchain" ? "推荐工具链" : "当前操作"}</span>
            <b>{progress.detail_percent !== undefined ? `${progress.detail_percent}%` : "处理中"}</b>
          </div>
          <p>{progress.message}</p>
          {progress.phase === "toolchain" ? <small>包含 OpenCLI · yt-dlp · FFmpeg · MarkItDown</small> : null}
          <div className={progress.detail_percent === undefined ? "is-indeterminate" : ""}>
            <i style={progress.detail_percent === undefined ? undefined : { width: `${progress.detail_percent}%` }} />
          </div>
        </div>
      ) : null}
      <ol className="progress-tasks" aria-label="安装任务">
        {tasks.map((task, index) => {
          const state = index < progress.current ? "done" : index === progress.current ? "active" : "pending";
          return (
            <li className={state} key={`${index}-${task}`}>
              <i aria-hidden="true">{state === "done" ? "✓" : String(index + 1).padStart(2, "0")}</i>
              <span><b>{task}</b>{state === "active" ? <em>{progress.message}</em> : null}</span>
              <small>{state === "done" ? "已完成" : state === "active" ? "正在处理" : "等待"}</small>
            </li>
          );
        })}
      </ol>
      <p>下载、完整性校验或首次健康检查可能需要几分钟；活动指示持续闪动即表示安装仍在进行。</p>
    </div>
  );
}

function formatElapsed(seconds: number) {
  if (seconds < 5) return "刚刚开始";
  if (seconds < 60) return `已用时 ${seconds} 秒`;
  return `已用时 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function formatDuration(milliseconds: number) {
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function Complete({ status, asrInstall, onInstallAsr, onManage, onOpenWiki, onOpenWikiDirectory }: {
  status: SetupResult | null;
  asrInstall: PackInstallJob | null;
  onInstallAsr: () => void;
  onManage: () => void;
  onOpenWiki: () => void;
  onOpenWikiDirectory: () => void;
}) {
  return (
    <div className="setup-copy reveal complete-view">
      <div className="completion-seal">就绪</div>
      <p className="eyebrow">05 / Ready</p>
      <h1>工作台已经备好。</h1>
      <p className="setup-lead">{Object.keys(status?.hosts ?? {}).length} 个 Agent 宿主及 Wiki 已就绪，可以立即抓取网页。中文视频转写可在后台准备，不阻塞使用。</p>
      <AsrInstallCard
        pack={status?.packs["asr-zh"]}
        modelCache={status?.model_caches["asr-zh"]}
        job={asrInstall}
        onInstall={onInstallAsr}
      />
      {status?.backups.length ? <p className="backup-note">已保留 {status.backups.length} 份外来 Skill 备份。</p> : null}
      <div className={`completion-next-steps${status?.actions.length ? "" : " is-single"}`}>
        {status?.actions.length ? <ManualActions actions={status.actions} label="网页采集还需要你完成" /> : null}
        <FirstCaptureCard wikiPath={status?.wiki.path ?? ""} onOpenWikiDirectory={onOpenWikiDirectory} />
      </div>
      <div className="setup-actions"><SecondaryButton onClick={onManage}>查看 Skills 与工具链</SecondaryButton><PrimaryButton onClick={onOpenWiki}>打开本地 Wiki</PrimaryButton></div>
    </div>
  );
}

function ManualActions({ actions, label }: { actions: ManualAction[]; label: string }) {
  const [copied, setCopied] = useState<string | null>(null);

  async function copyActionValue(action: ManualAction) {
    const value = manualActionCopyValue(action);
    if (!value || !await copyToClipboard(value)) return;
    setCopied(action.id);
    window.setTimeout(() => setCopied(null), 1800);
  }

  return (
    <section className="manual-actions">
      <p className="eyebrow">{label}</p>
      {actions.map((action) => {
        const copyValue = manualActionCopyValue(action);
        return (
          <article key={action.id}>
            <h2>{action.title}</h2>
            <p>{action.detail}</p>
            {copyValue ? (
              <button className="manual-action-copy" type="button" onClick={() => void copyActionValue(action)}>
                {copied === action.id ? "已复制 ✓" : "复制插件目录 →"}
              </button>
            ) : null}
          </article>
        );
      })}
    </section>
  );
}

function manualActionCopyValue(action: ManualAction) {
  if (action.id !== "opencli-browser-bridge") return null;
  const marker = "加载 ";
  const start = action.detail.lastIndexOf(marker);
  if (start < 0) return null;
  return action.detail.slice(start + marker.length).trim().replace(/[。.]\s*$/u, "");
}

function FirstCaptureCard({ wikiPath, onOpenWikiDirectory }: { wikiPath: string; onOpenWikiDirectory: () => void }) {
  const [copied, setCopied] = useState(false);
  const command = "抓取并整理到我的 wiki：<粘贴网页或视频链接>";
  const prompt = `${command}\n\nWiki root：${wikiPath}`;

  async function copyPrompt() {
    if (!await copyToClipboard(prompt)) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }

  return (
    <section className="first-capture-card">
      <header>
        <p className="eyebrow">完成后马上试一篇</p>
        <button type="button" onClick={() => void copyPrompt()}>{copied ? "已复制 ✓" : "复制指令 →"}</button>
      </header>
      <h2>把链接交给 Agent</h2>
      <code>{command}</code>
      <div className="first-capture-target">
        <small>目标 · {wikiPath}</small>
        <button type="button" onClick={onOpenWikiDirectory}>前往查看 ↗</button>
      </div>
    </section>
  );
}

function AsrInstallCard({ pack, modelCache, job, onInstall, compact = false }: {
  pack?: PackStatus;
  modelCache?: ModelCacheStatus;
  job: PackInstallJob | null;
  onInstall: () => void;
  compact?: boolean;
}) {
  const ready = Boolean(pack?.healthy && modelCache?.ready) || job?.state === "installed";
  const runtimeReady = pack?.healthy;
  const running = job?.state === "running";
  const failed = job?.state === "error";
  const percent = job?.detail_percent;
  const stateLabel = ready ? "已就绪" : running ? "后台安装中" : failed ? "可重试" : "可选安装";

  return (
    <section className={`asr-install-card${compact ? " is-compact" : ""}${ready ? " is-ready" : ""}`}>
      <div className="asr-install-index">声</div>
      <div className="asr-install-copy">
        <header><h2>中文视频转写</h2><span>{stateLabel}</span></header>
        {ready ? (
          <p>asr-zh、fsmn-vad 与 SenseVoiceSmall 已通过本地离线加载检查。</p>
        ) : running ? (
          <>
            <p>{job?.message}。可以直接打开 Wiki，隐藏本窗口不会中断。</p>
            <div className={`asr-progress${percent === undefined ? " is-indeterminate" : ""}`} role="progressbar" aria-label="中文视频转写组件安装进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
              <i style={percent === undefined ? undefined : { width: `${percent}%` }} />
              <span>{percent === undefined ? "处理中…" : `${percent}%`}</span>
            </div>
          </>
        ) : (
          <p>{runtimeReady ? "asr-zh 已安装；Browser 将继续" : "网页采集已可使用；Browser 会在后台"}下载 fsmn-vad 与 SenseVoiceSmall，总共 1GB。</p>
        )}
        {!ready ? <small>私有目录存储 · 支持断点续传</small> : null}
        {failed && job?.error ? <p className="asr-install-error">{job.error}</p> : null}
      </div>
      {!ready && !running ? <SecondaryButton onClick={onInstall}>{failed ? "继续预热" : "安装并预热"}</SecondaryButton> : null}
    </section>
  );
}

function Management({ status, update, browserUpdate, busy, error, repair, onRepair, onStopRepair, onCheck, onUpdate, onRestart, asrInstall, onInstallAsr }: {
  status: SetupResult | null;
  update: UpdateResult | null;
  browserUpdate: BrowserUpdateStatus | null;
  busy: boolean;
  error: string | null;
  repair: RepairJob | null;
  onRepair: () => void;
  onStopRepair: () => void;
  onCheck: () => void;
  onUpdate: () => void;
  onRestart: () => void;
  asrInstall: PackInstallJob | null;
  onInstallAsr: () => void;
}) {
  const ready = status?.state === "ready";
  const announcingSuccess = useSuccessAnnouncement(repair?.state);
  const browserBusy = browserUpdate?.state === "checking" || browserUpdate?.state === "downloading";
  const browserRestart = browserUpdate?.state === "ready-to-restart";
  const updateAvailable = update?.state === "available" || browserUpdate?.state === "available";
  return (
    <main className="manage-shell">
      <header className="manage-header">
        <div><div className="setup-mark">文</div><div><p className="eyebrow">Local control plane</p><h1>Skills 与工具链</h1></div></div>
        <span className={ready ? "status-ready" : "status-repair"}>{ready ? "运行正常" : "需要处理"}</span>
      </header>
      {error ? <ErrorBanner message={error} /> : null}
      <section className="manage-grid">
        <StatusPanel index="01" title="Skills Pack" healthy={Object.values(status?.hosts ?? {}).every((host) => host.healthy)}>
          {Object.entries(status?.hosts ?? {}).map(([id, host]) => <div className="host-status" key={id}><b>{id}</b><span>{host.installed.length} skills</span><small>{host.skills_dir}</small></div>)}
          {status?.cli_path ? <p className="panel-path">CLI · {status.cli_path}</p> : null}
        </StatusPanel>
        <StatusPanel index="02" title="官方工具链" healthy={status?.official_toolchain.healthy ?? false}>
          <p className="panel-version">{status?.official_toolchain.installed ? `v${status.official_toolchain.version}` : "未安装 · 开放路径"}</p>
          <p>默认优先使用官方工具链，可被系统或任务级工具替换。</p>
          {Object.entries(status?.packs ?? {}).filter(([id]) => id !== "toolchain-base").map(([id, pack]) => <div className="pack-status" key={id}><b>{id}</b><span>{pack.healthy ? `v${pack.version}` : "需要修复"}</span></div>)}
          <AsrInstallCard pack={status?.packs["asr-zh"]} modelCache={status?.model_caches["asr-zh"]} job={asrInstall} onInstall={onInstallAsr} compact />
        </StatusPanel>
        <StatusPanel index="03" title="Wiki" healthy={status?.wiki.ready ?? false}>
          <p className="panel-path">{status?.wiki.path}</p>
        </StatusPanel>
        <StatusPanel index="04" title="联合更新" healthy={!updateAvailable && !browserRestart && browserUpdate?.state !== "error"}>
          <p>{browserUpdateText(browserUpdate, update, status?.distribution_version)}</p>
          {browserUpdate?.state === "downloading" ? <UpdateProgress status={browserUpdate} /> : null}
          {browserUpdate?.state === "error" && browserUpdate.error ? <p className="setup-inline-error">{browserUpdate.error}</p> : null}
          <div className="panel-actions">
            {!browserRestart ? <SecondaryButton disabled={busy || browserBusy} onClick={onCheck}>{browserUpdate?.state === "checking" ? "检查中…" : "检查更新"}</SecondaryButton> : null}
            {updateAvailable ? <PrimaryButton disabled={busy || browserBusy} onClick={onUpdate}>开始更新</PrimaryButton> : null}
            {browserRestart ? <PrimaryButton disabled={busy} onClick={onRestart}>重启完成更新</PrimaryButton> : null}
          </div>
        </StatusPanel>
      </section>
      {status?.actions.length ? <ManualActions actions={status.actions} label="Manual actions" /> : null}
      {!ready || repairRunning(repair) || announcingSuccess ? (
        <RepairBar job={repair} ready={ready} busy={busy} onStart={onRepair} onStop={onStopRepair} />
      ) : null}
    </main>
  );
}

// A repair that works flips every panel to OK and hides the repair bar, which
// reads as the button having done nothing. Hold the finished bar briefly so
// the run gets an ending as visible as its start.
function useSuccessAnnouncement(state: string | undefined) {
  const [announcing, setAnnouncing] = useState(false);
  useEffect(() => {
    if (state !== "done") return;
    setAnnouncing(true);
    const timer = window.setTimeout(() => setAnnouncing(false), 6000);
    return () => window.clearTimeout(timer);
  }, [state]);
  return announcing;
}

function repairRunning(job: RepairJob | null) {
  return job?.state === "running" || job?.state === "stopping";
}

// Repair can spend minutes inside a single download. Everything below exists so
// that wait is legible: what step it is on, when it last moved, and how to get
// out — a bare "处理中…" leaves a stalled network indistinguishable from a hang.
function RepairBar({ job, ready, busy, onStart, onStop }: {
  job: RepairJob | null;
  ready: boolean;
  busy: boolean;
  onStart: () => void;
  onStop: () => void;
}) {
  const running = repairRunning(job);
  const now = useTicker(running);
  const stopping = job?.state === "stopping";
  const succeeded = job?.state === "done" && ready;
  const percent = job && job.total > 0 ? Math.round((job.current / job.total) * 100) : 0;
  const sinceProgress = job && running ? now - job.updated_at : 0;
  const stalled = running && !stopping && sinceProgress > REPAIR_STALL_MS;

  return (
    <footer className={`repair-bar${succeeded ? " is-done" : ""}`}>
      <div className="repair-bar-copy">
        <b>{repairTitle(job)}</b>
        <span>{repairDetail(job, ready)}</span>
        {running ? (
          <>
            <div
              className={`repair-track${job && job.total > 0 ? "" : " is-indeterminate"}`}
              role="progressbar"
              aria-label="修复进度"
              aria-valuemin={0}
              aria-valuemax={job?.total ?? 0}
              aria-valuenow={job && job.total > 0 ? job.current : undefined}
            >
              <i style={job && job.total > 0 ? { width: `${percent}%` } : undefined} />
            </div>
            <small className="repair-bar-meta">
              {job && job.total > 0 ? `第 ${Math.min(job.current + 1, job.total)} / ${job.total} 步` : "正在统计需要修复的项目"}
              {job?.detail_percent !== undefined ? ` · 当前项 ${job.detail_percent}%` : ""}
              {job?.started_at ? ` · 已用时 ${formatDuration(now - job.started_at)}` : ""}
            </small>
          </>
        ) : null}
        {stalled ? (
          <small className="repair-bar-stall" role="status">
            已有 {formatDuration(sinceProgress)}没有新进展，通常是网络不通或下载源被拦截。
            可以继续等待，也可以停止后换网络重试——已下载的部分会保留并续传。
          </small>
        ) : null}
        {job?.state === "error" && job.error ? <small className="repair-bar-detail">{job.error}</small> : null}
      </div>
      {running ? (
        <SecondaryButton disabled={stopping} onClick={onStop}>
          {stopping ? "正在停止…" : "停止修复"}
        </SecondaryButton>
      ) : succeeded ? null : (
        <PrimaryButton disabled={busy} onClick={onStart}>
          {job && job.state !== "idle" ? "重试修复" : "修复当前版本"}
        </PrimaryButton>
      )}
    </footer>
  );
}

function repairTitle(job: RepairJob | null) {
  if (job?.state === "stopping") return "正在停止修复";
  if (job?.state === "running") return job.message;
  if (job?.state === "error") return "修复未完成";
  if (job?.state === "cancelled") return "修复已停止";
  if (job?.state === "done") return "修复完成";
  return "状态不完整或文件已损坏";
}

// `ready` arrives one status refresh after the job reports `done`, so the title
// above never depends on it — only this line sharpens once the refresh lands.
function repairDetail(job: RepairJob | null, ready: boolean) {
  if (job?.state === "stopping") return "会在当前分片结束后停止，已下载的内容会保留。";
  if (job?.state === "running") return `${repairPhaseLabel(job.phase)} · 修复只恢复当前版本，不会升级或触碰 Wiki`;
  if (job?.state === "error" || job?.state === "cancelled") return job.message;
  if (job?.state === "done") {
    return ready ? job.message : `${job.message}；上方仍有标记为 ! 的项目，可以再修复一次。`;
  }
  return "Repair 只恢复当前版本，不会升级或触碰 Wiki。";
}

function repairPhaseLabel(phase: string) {
  return ({
    preparing: "检查当前安装",
    skills: "Skills Pack",
    wiki: "Wiki 与 RAW 目录",
    pack: "能力包",
    toolchain: "推荐工具链",
  } as Record<string, string>)[phase] ?? "修复中";
}

/** A wall clock that only ticks while something is actually being waited on. */
function useTicker(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function StatusPanel({ index, title, healthy, children }: { index: string; title: string; healthy: boolean; children: React.ReactNode }) {
  return <article className="status-panel"><header><span>{index}</span><h2>{title}</h2><i className={healthy ? "ok" : "warn"}>{healthy ? "OK" : "!"}</i></header><div>{children}</div></article>;
}

function LoadingScreen() {
  return <main className="loading-screen"><div className="setup-mark">文</div><p className="eyebrow">Inspecting local state</p></main>;
}

function ErrorBanner({ message }: { message: string }) {
  return <div className="setup-error" role="alert"><b>未能完成</b><span>{message}</span></div>;
}

function PrimaryButton({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className="primary-button" type="button" {...props}>{children}</button>;
}

function SecondaryButton({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className="secondary-button" type="button" {...props}>{children}</button>;
}

function toggled(current: Set<string>, value: string) {
  const next = new Set(current);
  if (next.has(value)) next.delete(value); else next.add(value);
  return next;
}

function messageOf(reason: unknown) {
  return reason instanceof Error ? reason.message : String(reason);
}

function viewStep(view: View) {
  return ({ welcome: 0, hosts: 1, review: 2, progress: 3, complete: 4 } as Partial<Record<View, number>>)[view] ?? 0;
}

function updateText(update: UpdateResult) {
  if (update.state === "up-to-date") return `v${update.current_version} 已是最新发布组合`;
  if (update.state === "available") return `v${update.latest_version} 可用`;
  if (update.state === "restart-required") return `Browser v${update.latest_version} 安装后重启以继续`;
  return `已更新至 v${update.latest_version}`;
}

function browserUpdateText(browser: BrowserUpdateStatus | null, update: UpdateResult | null, distributionVersion?: string) {
  if (browser?.state === "checking") return "正在检查 Browser 与 distribution…";
  if (browser?.state === "downloading") return `正在安装 Browser v${browser.latest_version ?? "—"}`;
  if (browser?.state === "ready-to-restart") return `Browser v${browser.latest_version ?? "—"} 已安装，重启后继续更新`;
  if (browser?.state === "available") return `Browser v${browser.latest_version ?? "—"} 可用`;
  if (browser?.state === "portable") return browser.latest_version ? `便携版发现 v${browser.latest_version}，请手动替换应用` : "便携版需手动替换 Browser";
  if (browser?.state === "error") return "Browser 更新检查失败";
  if (update) return updateText(update);
  return `Browser v${browser?.current_version ?? "—"} · distribution v${distributionVersion ?? "—"}`;
}

function UpdateProgress({ status }: { status: BrowserUpdateStatus }) {
  const percent = status.total ? Math.min(100, Math.round(((status.downloaded ?? 0) / status.total) * 100)) : 12;
  return <div className="manage-update-progress"><i style={{ width: `${percent}%` }} /><span>{status.total ? `${percent}%` : "下载中…"}</span></div>;
}
