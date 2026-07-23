import { useEffect, useMemo, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";

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

interface WikiStatus {
  path: string;
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

interface ProviderSpec {
  commands: Record<string, string[]>;
  python_profiles: Record<string, string[]>;
  environment: Record<string, Record<string, string>>;
}

interface ProviderConfig {
  schema: number;
  policy: "official-preferred";
  overrides: Record<string, string>;
  providers: Record<string, ProviderSpec>;
}

const PROVIDER_CAPABILITIES = [
  ["capture.web.authenticated", "登录态网页抓取"],
  ["capture.video.captions", "视频字幕获取"],
  ["capture.video.metadata", "视频元数据"],
  ["media.extract-audio", "音频提取 / FFmpeg"],
  ["document.to-markdown", "文档转 Markdown"],
  ["python.asr-zh", "中文 ASR"],
  ["python.asr-other", "非中文 ASR"],
] as const;

interface Progress {
  phase: string;
  message: string;
  current: number;
  total: number;
  detail_percent?: number;
}

type View = "loading" | "welcome" | "hosts" | "review" | "progress" | "complete" | "manage";

const STEP_LABELS = ["欢迎", "选择宿主", "确认", "安装", "完成"];

export default function SetupApp() {
  const [view, setView] = useState<View>("loading");
  const [inspection, setInspection] = useState<SetupInspection | null>(null);
  const [status, setStatus] = useState<SetupResult | null>(null);
  const [selectedHosts, setSelectedHosts] = useState<Set<string>>(new Set());
  const [approvedConflicts, setApprovedConflicts] = useState<Set<string>>(new Set());
  const [installToolchain, setInstallToolchain] = useState(true);
  const [progress, setProgress] = useState<Progress>({
    phase: "preparing",
    message: "安装任务已启动",
    current: 0,
    total: 1,
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [update, setUpdate] = useState<UpdateResult | null>(null);
  const [browserUpdate, setBrowserUpdate] = useState<BrowserUpdateStatus | null>(null);
  const [providerConfig, setProviderConfig] = useState<ProviderConfig | null>(null);

  useEffect(() => {
    let mounted = true;
    const unlisten = listen<Progress>("setup-progress", (event) => {
      if (mounted) setProgress(event.payload);
    });
    Promise.all([
      invoke<SetupInspection>("setup_inspect"),
      invoke<SetupResult>("setup_status"),
      invoke<BrowserUpdateStatus>("setup_browser_update_status"),
      invoke<ProviderConfig>("setup_provider_config"),
    ])
      .then(([nextInspection, nextStatus, nextBrowserUpdate, nextProviderConfig]) => {
        if (!mounted) return;
        setInspection(nextInspection);
        setStatus(nextStatus);
        setBrowserUpdate(nextBrowserUpdate);
        setProviderConfig(nextProviderConfig);
        setSelectedHosts(new Set());
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
      total: selectedHosts.size + 1 + Number(installToolchain),
    });
    try {
      const result = await invoke<SetupResult>("setup_apply", {
        request: {
          hosts: [...selectedHosts],
          replace: [...approvedConflicts],
          install_official_toolchain: installToolchain,
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

  async function repair() {
    setBusy(true);
    setError(null);
    try {
      const result = await invoke<SetupResult>("setup_repair");
      setStatus(result);
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
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
    setError(null);
    try {
      await invoke("setup_open_wiki");
    } catch (reason) {
      setError(messageOf(reason));
    }
  }

  async function saveProviderConfig(config: ProviderConfig) {
    setBusy(true);
    setError(null);
    try {
      setProviderConfig(await invoke<ProviderConfig>("setup_save_provider_config", { config }));
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  }

  if (view === "loading") return <LoadingScreen />;
  if (view === "manage") {
    return (
      <Management
        status={status}
        update={update}
        browserUpdate={browserUpdate}
        providerConfig={providerConfig}
        busy={busy}
        error={error}
        onRepair={() => void repair()}
        onCheck={() => void checkUpdate(false)}
        onUpdate={() => void checkUpdate(true)}
        onRestart={() => void restartForUpdate()}
        onSaveProvider={saveProviderConfig}
      />
    );
  }

  const step = viewStep(view);
  return (
    <main className="setup-shell">
      <SetupRail active={step} version={inspection?.distribution_version} />
      <section className="setup-stage">
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
            onToggleConflict={toggleConflict}
            onToggleToolchain={() => setInstallToolchain((value) => !value)}
            onBack={() => setView("hosts")}
            onApply={() => void apply()}
            disabled={busy || !selectedHosts.size || !conflictsApproved}
          />
        ) : view === "progress" ? (
          <ProgressView
            progress={progress}
            tasks={[
              ...[...selected]
                .sort((left, right) => left.id.localeCompare(right.id))
                .map((host) => `为 ${host.label} 激活 Skills Pack`),
              "初始化 Wiki 与 RAW 目录",
              ...(installToolchain ? ["安装并校验官方工具链"] : []),
            ]}
          />
        ) : (
          <Complete status={status} onManage={() => setView("manage")} onOpenWiki={() => void openWiki()} />
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
        <Principle number="02" title="替换有自由" body="任务中的明确选择和长期 Provider 偏好始终优先。" />
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

function Review({ hosts, conflicts, approved, installToolchain, onToggleConflict, onToggleToolchain, onBack, onApply, disabled }: {
  hosts: HostInspection[];
  conflicts: SkillDestination[];
  approved: Set<string>;
  installToolchain: boolean;
  onToggleConflict: (path: string) => void;
  onToggleToolchain: () => void;
  onBack: () => void;
  onApply: () => void;
  disabled: boolean;
}) {
  return (
    <div className="setup-copy reveal">
      <p className="eyebrow">03 / One confirmation</p>
      <h1>一次确认，之后无人值守。</h1>
      <div className="review-sheet">
        <ReviewRow label="Skills Pack" value={`完整安装 · ${hosts.length} 个宿主`} note={hosts.map((host) => host.label).join("、")} />
        <ReviewRow label="官方工具链" value={installToolchain ? "安装并默认优先" : "不安装"} note={installToolchain ? "Web、Video、Documents 基线；ASR 按需下载" : "Skills 将使用 Agent、系统或自定义 Provider"} action={<button className="text-toggle" onClick={onToggleToolchain}>{installToolchain ? "改用开放路径" : "恢复推荐设置"}</button>} />
        <ReviewRow label="Wiki" value="初始化或复用" note="~/wikis/my-llm-wiki" />
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

function ProgressView({ progress, tasks }: { progress: Progress; tasks: string[] }) {
  const ratio = progress.total ? Math.round((progress.current / progress.total) * 100) : 0;
  const starting = progress.current === 0;
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
      {progress.detail_percent !== undefined ? (
        <div className="detail-progress">
          <div>
            <span>当前操作</span>
            <b>{progress.detail_percent > 0 ? `${progress.detail_percent}%` : "正在连接…"}</b>
          </div>
          <div className={progress.detail_percent === 0 ? "is-indeterminate" : ""}>
            <i style={progress.detail_percent === 0 ? undefined : { width: `${progress.detail_percent}%` }} />
          </div>
        </div>
      ) : null}
      <ol className="progress-tasks" aria-label="安装任务">
        {tasks.map((task, index) => {
          const state = index < progress.current ? "done" : index === progress.current ? "active" : "pending";
          return (
            <li className={state} key={`${index}-${task}`}>
              <i aria-hidden="true">{state === "done" ? "✓" : String(index + 1).padStart(2, "0")}</i>
              <span>{task}</span>
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

function Complete({ status, onManage, onOpenWiki }: {
  status: SetupResult | null;
  onManage: () => void;
  onOpenWiki: () => void;
}) {
  return (
    <div className="setup-copy reveal complete-view">
      <div className="completion-seal">就绪</div>
      <p className="eyebrow">05 / Ready</p>
      <h1>工作台已经备好。</h1>
      <p className="setup-lead">{Object.keys(status?.hosts ?? {}).length} 个 Agent 宿主已获得完整 Skills Pack，Wiki 已打开。官方工具链会作为默认 Provider，但不会限制你的选择。</p>
      {status?.backups.length ? <p className="backup-note">已保留 {status.backups.length} 份外来 Skill 备份。</p> : null}
      {status?.actions.length ? <section className="manual-actions"><p className="eyebrow">还需你完成</p>{status.actions.map((action) => <article key={action.id}><h2>{action.title}</h2><p>{action.detail}</p></article>)}</section> : null}
      <div className="setup-actions"><SecondaryButton onClick={onManage}>查看 Skills 与工具链</SecondaryButton><PrimaryButton onClick={onOpenWiki}>打开 Wiki</PrimaryButton></div>
    </div>
  );
}

function Management({ status, update, browserUpdate, providerConfig, busy, error, onRepair, onCheck, onUpdate, onRestart, onSaveProvider }: {
  status: SetupResult | null;
  update: UpdateResult | null;
  browserUpdate: BrowserUpdateStatus | null;
  providerConfig: ProviderConfig | null;
  busy: boolean;
  error: string | null;
  onRepair: () => void;
  onCheck: () => void;
  onUpdate: () => void;
  onRestart: () => void;
  onSaveProvider: (config: ProviderConfig) => Promise<void>;
}) {
  const ready = status?.state === "ready";
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
          <p>Provider policy: <b>official-preferred</b></p>
          {Object.entries(status?.packs ?? {}).filter(([id]) => id !== "toolchain-base").map(([id, pack]) => <div className="pack-status" key={id}><b>{id}</b><span>{pack.healthy ? `v${pack.version}` : "需要修复"}</span></div>)}
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
      {providerConfig ? <ProviderPanel config={providerConfig} busy={busy} onSave={onSaveProvider} /> : null}
      {status?.actions.length ? <section className="manual-actions"><p className="eyebrow">Manual actions</p>{status.actions.map((action) => <article key={action.id}><h2>{action.title}</h2><p>{action.detail}</p></article>)}</section> : null}
      {!ready ? <footer className="repair-bar"><div><b>状态不完整或文件已损坏</b><span>Repair 只恢复当前版本，不会升级或触碰 Wiki。</span></div><PrimaryButton disabled={busy} onClick={onRepair}>{busy ? "处理中…" : "修复当前版本"}</PrimaryButton></footer> : null}
    </main>
  );
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

function ProviderPanel({ config, busy, onSave }: {
  config: ProviderConfig;
  busy: boolean;
  onSave: (config: ProviderConfig) => Promise<void>;
}) {
  const [providerId, setProviderId] = useState("");
  const [entry, setEntry] = useState("opencli");
  const [executable, setExecutable] = useState("");
  const [fixedArgs, setFixedArgs] = useState("");
  const customIds = Object.keys(config.providers).sort();

  function setOverride(capability: string, provider: string) {
    const overrides = { ...config.overrides };
    if (provider) overrides[capability] = provider; else delete overrides[capability];
    void onSave({ ...config, overrides });
  }

  function removeProvider(id: string) {
    const providers = { ...config.providers };
    delete providers[id];
    const overrides = Object.fromEntries(
      Object.entries(config.overrides).filter(([, provider]) => provider !== id),
    );
    void onSave({ ...config, providers, overrides });
  }

  function addProvider(event: React.FormEvent) {
    event.preventDefault();
    const id = providerId.trim();
    const path = executable.trim();
    if (!id || !path) return;
    const argv = [path, ...fixedArgs.split(/\r?\n/).map((value) => value.trim()).filter(Boolean)];
    const pythonProfile = entry === "asr-zh" || entry === "asr-other";
    const current = config.providers[id] ?? { commands: {}, python_profiles: {}, environment: {} };
    const spec: ProviderSpec = {
      commands: pythonProfile ? current.commands : { ...current.commands, [entry]: argv },
      python_profiles: pythonProfile ? { ...current.python_profiles, [entry]: argv } : current.python_profiles,
      environment: current.environment,
    };
    void onSave({ ...config, providers: { ...config.providers, [id]: spec } });
    setProviderId("");
    setExecutable("");
    setFixedArgs("");
  }

  return (
    <section className="provider-panel">
      <header>
        <div><p className="eyebrow">Provider resolver</p><h2>默认有保证，替换有自由</h2></div>
        <span>official-preferred</span>
      </header>
      <p className="provider-intro">留空时优先使用官方验证工具链，再回退到系统与自定义 Provider。这里只保存长期偏好；单次任务中的明确选择仍然优先。</p>
      <div className="provider-routes">
        {PROVIDER_CAPABILITIES.map(([capability, label]) => (
          <label key={capability}>
            <span><b>{label}</b><small>{capability}</small></span>
            <select disabled={busy} value={config.overrides[capability] ?? ""} onChange={(event) => setOverride(capability, event.target.value)}>
              <option value="">自动（推荐）</option>
              <option value="official">固定使用官方工具链</option>
              <option value="system">固定使用系统工具</option>
              {customIds.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
          </label>
        ))}
      </div>
      {customIds.length ? <div className="custom-provider-list">{customIds.map((id) => <div key={id}><span><b>{id}</b><small>{Object.keys(config.providers[id].commands).concat(Object.keys(config.providers[id].python_profiles)).join(" · ")}</small></span><button disabled={busy} onClick={() => removeProvider(id)}>移除</button></div>)}</div> : null}
      <details className="provider-builder">
        <summary>注册自定义 Provider</summary>
        <form onSubmit={addProvider}>
          <label><span>Provider ID</span><input value={providerId} onChange={(event) => setProviderId(event.target.value)} placeholder="my-whisper-server" /></label>
          <label><span>提供能力</span><select value={entry} onChange={(event) => setEntry(event.target.value)}><option value="opencli">OpenCLI / 网页抓取</option><option value="yt-dlp">yt-dlp / 视频</option><option value="ffmpeg">FFmpeg / 音频</option><option value="markitdown">MarkItDown / 文档</option><option value="asr-zh">中文 ASR Python</option><option value="asr-other">非中文 ASR Python</option></select></label>
          <label className="provider-path"><span>可执行文件绝对路径</span><input value={executable} onChange={(event) => setExecutable(event.target.value)} placeholder="/absolute/path/to/executable" /></label>
          <label className="provider-path"><span>固定 argv（每行一个参数，可留空）</span><textarea value={fixedArgs} onChange={(event) => setFixedArgs(event.target.value)} rows={3} /></label>
          <PrimaryButton disabled={busy || !providerId.trim() || !executable.trim()}>保存 Provider</PrimaryButton>
        </form>
      </details>
    </section>
  );
}
