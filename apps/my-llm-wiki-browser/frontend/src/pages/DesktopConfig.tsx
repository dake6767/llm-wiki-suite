import { useEffect, useMemo, useState } from "react";
import {
  getAutostartConfig,
  getServerConfig,
  listConfigWikis,
  restartServer,
  setAutostart,
  setDefaultWiki,
  updateServerPort,
  type WikiConfigInfo,
} from "../api/client";
import SharePanel from "../components/SharePanel";
import { BrandLogo } from "../components/BrandLogo";

export type BrowserSettingsTab = "wikis" | "runtime" | "share";

export const browserSettingsTabs: Array<{
  key: BrowserSettingsTab;
  label: string;
  eyebrow: string;
}> = [
  { key: "wikis", label: "Wiki 目录", eyebrow: "Library roots" },
  { key: "runtime", label: "运行入口", eyebrow: "Entrypoints" },
  { key: "share", label: "分享管理", eyebrow: "Sharing" },
];

const TAB_TITLES: Record<BrowserSettingsTab, string> = {
  wikis: "Wiki 目录",
  runtime: "运行入口",
  share: "分享管理",
};

export default function DesktopConfig() {
  const [active, setActive] = useState<BrowserSettingsTab>("wikis");

  return (
    <main className="min-h-full bg-paper text-ink">
      <div className="grid min-h-screen grid-cols-[230px_minmax(0,1fr)]">
        <aside className="spine flex flex-col border-r border-[rgba(221,211,192,0.14)] bg-spine text-cream">
          <div className="border-b border-[rgba(221,211,192,0.14)] px-6 py-6">
            <BrandLogo className="mb-4 h-11 w-11" />
            <p className="eyebrow text-cream-soft">LLM-Wiki Desktop</p>
            <h1 className="font-display mt-2 text-2xl font-semibold">设置</h1>
          </div>

          <nav className="flex-1 p-3">
            {browserSettingsTabs.map((tab) => {
              const selected = active === tab.key;
              return (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => setActive(tab.key)}
                  className={[
                    "mb-2 w-full rounded-md border px-4 py-3 text-left transition",
                    selected
                      ? "border-[rgba(221,211,192,0.32)] bg-[rgba(221,211,192,0.11)] text-cream"
                      : "border-transparent text-cream-soft hover:border-[rgba(221,211,192,0.16)] hover:bg-[rgba(221,211,192,0.06)] hover:text-cream",
                  ].join(" ")}
                >
                  <span className="font-display block text-base">
                    {tab.label}
                  </span>
                  <span className="font-mono mt-1 block text-[0.66rem] uppercase tracking-[0.18em] opacity-70">
                    {tab.eyebrow}
                  </span>
                </button>
              );
            })}
          </nav>
        </aside>

        <BrowserSettingsSection active={active} />
      </div>
    </main>
  );
}

export function BrowserSettingsSection({
  active,
}: {
  active: BrowserSettingsTab;
}) {
  const [wikis, setWikis] = useState<WikiConfigInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listConfigWikis()
      .then((items) => {
        if (!cancelled) {
          setWikis(items);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "配置读取失败");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const totalPages = useMemo(
    () => wikis.reduce((sum, wiki) => sum + wiki.page_count, 0),
    [wikis],
  );

  return (
    <section className="min-w-0 overflow-auto">
      <div className="mx-auto max-w-6xl px-8 py-8">
        <header className="mb-8 flex flex-wrap items-end justify-between gap-5 border-b border-[var(--rule)] pb-6">
          <div>
            <p className="eyebrow">Desktop control panel</p>
            <h2 className="font-display mt-2 text-4xl font-semibold">
              {TAB_TITLES[active]}
            </h2>
          </div>
          <div className="grid grid-cols-2 gap-3 text-right">
            <Metric label="Wiki" value={String(wikis.length)} />
            <Metric label="Pages" value={String(totalPages)} />
          </div>
        </header>

        {active === "wikis" ? (
          <WikiDirectoryPanel
            wikis={wikis}
            loading={loading}
            error={error}
            onWikisChange={setWikis}
          />
        ) : active === "runtime" ? (
          <RuntimePanel />
        ) : (
          <SharingPanel wikis={wikis} />
        )}
      </div>
    </section>
  );
}

// 设置页的「分享管理」区块：复用同一 SharePanel 组件（无双份实现）。定位偏治理与盘点，
// 因脱离浏览上下文，创建带 wiki 选择器；高频创建路径仍在浏览界面顶栏（docs/19 §4.5）。
function SharingPanel({ wikis }: { wikis: WikiConfigInfo[] }) {
  return (
    <div>
      <p className="mb-5 max-w-2xl font-serif text-sm leading-relaxed text-ink">
        每个 Wiki 可指定一条通用分享，日常分享页面会复用它；也可以为特定对象新建独立分享，
        分别设置有效期和撤销。所有链接都是整库只读授权：它们可以从某个页面打开，但对方仍可
        浏览该 Wiki 的其他页面。RAW 目录与维护队列不在分享范围内。
      </p>
      <SharePanel
        open
        wikiOptions={wikis}
        embedded
        initialView="manage"
      />
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-24 border-l border-[var(--rule)] pl-4">
      <div className="font-display text-3xl font-semibold tabular-nums">
        {value}
      </div>
      <div className="font-mono mt-1 text-[0.68rem] uppercase tracking-[0.16em] text-ink-faint">
        {label}
      </div>
    </div>
  );
}

function WikiDirectoryPanel({
  wikis,
  loading,
  error,
  onWikisChange,
}: {
  wikis: WikiConfigInfo[];
  loading: boolean;
  error: string | null;
  onWikisChange: (wikis: WikiConfigInfo[]) => void;
}) {
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  async function makeDefault(key: string) {
    setSaveError(null);
    setSavingKey(key);
    try {
      onWikisChange(await setDefaultWiki(key));
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "设置失败");
    } finally {
      setSavingKey(null);
    }
  }

  if (loading) {
    return <PanelMessage title="读取中" body="正在读取本机 Wiki 注册表。" />;
  }
  if (error) {
    return <PanelMessage title="读取失败" body={error} tone="error" />;
  }
  if (!wikis.length) {
    return (
      <PanelMessage title="没有发现 Wiki" body="当前配置下没有可用目录。" />
    );
  }

  return (
    <div className="overflow-hidden rounded-md border border-[var(--rule)] bg-[rgba(255,255,255,0.22)]">
      <div className="grid grid-cols-[220px_minmax(0,1fr)] border-b border-[var(--rule)] bg-paper-2/70 px-5 py-3 font-mono text-[0.7rem] uppercase tracking-[0.16em] text-ink-faint">
        <span>Wiki</span>
        <span>Directories</span>
      </div>
      {saveError ? (
        <p className="border-b border-[var(--rule-soft)] bg-cinnabar/10 px-5 py-3 text-sm text-cinnabar">
          {saveError}
        </p>
      ) : null}
      {wikis.map((wiki) => (
        <article
          key={wiki.key}
          className="grid grid-cols-[220px_minmax(0,1fr)] gap-5 border-b border-[var(--rule-soft)] px-5 py-5 last:border-b-0"
        >
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="font-display truncate text-xl font-semibold">
                {wiki.name}
              </h3>
              {wiki.default ? (
                <span className="rounded-sm bg-cinnabar px-1.5 py-0.5 font-mono text-[0.62rem] uppercase tracking-[0.14em] text-paper">
                  default
                </span>
              ) : (
                <button
                  type="button"
                  disabled={savingKey !== null}
                  onClick={() => void makeDefault(wiki.key)}
                  title="打开 WIKI 时默认进入这个库"
                  className="rounded-sm border border-[var(--rule)] px-1.5 py-0.5 font-mono text-[0.62rem] uppercase tracking-[0.14em] text-ink-faint transition hover:border-cinnabar hover:text-cinnabar disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {savingKey === wiki.key ? "设置中…" : "设为默认"}
                </button>
              )}
            </div>
            <p className="font-mono mt-1 text-xs text-ink-faint">
              {wiki.key} · {wiki.page_count} pages
            </p>
            {wiki.description ? (
              <p className="mt-3 text-sm leading-6 text-ink-soft">
                {wiki.description}
              </p>
            ) : null}
          </div>

          <dl className="grid min-w-0 gap-3">
            <PathRow label="Root" value={wiki.root_dir} />
            <PathRow label="Wiki" value={wiki.wiki_dir} />
            <PathRow label="Raw" value={wiki.raw_dir} />
            <PathRow label="Assets" value={wiki.assets_dir} />
          </dl>
        </article>
      ))}
    </div>
  );
}

function RuntimePanel() {
  return (
    <div className="grid gap-4">
      <ServerPortPanel />
      <AutostartPanel />
      <PanelMessage
        title="入口在托盘菜单"
        body="本地 WIKI 和线上 WIKI 已移动到系统托盘。这里后续会承载中继、索引、embedding 等配置。"
      />
    </div>
  );
}

type PortPhase = "idle" | "confirming" | "saving" | "restarting";

function ServerPortPanel() {
  const [currentPort, setCurrentPort] = useState<number | null>(null);
  const [port, setPort] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [phase, setPhase] = useState<PortPhase>("idle");

  useEffect(() => {
    let cancelled = false;
    getServerConfig()
      .then((cfg) => {
        if (!cancelled) {
          setCurrentPort(cfg.port);
          setPort(String(cfg.port));
        }
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "端口读取失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const parsed = Number(port);
  const valid = Number.isInteger(parsed) && parsed >= 1024 && parsed <= 65535;
  const changed = currentPort !== null && valid && parsed !== currentPort;
  const busy = phase === "saving" || phase === "restarting";

  async function saveAndRestart() {
    setError(null);
    setPhase("saving");
    try {
      await updateServerPort(parsed);
      setPhase("restarting");
      await restartServer();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
      setPhase("idle");
    }
  }

  if (loading) {
    return <PanelMessage title="读取中" body="正在读取本地服务端口。" />;
  }
  if (phase === "restarting") {
    return (
      <PanelMessage
        title="正在重启"
        body={`应用将以端口 ${parsed} 重新打开，请稍候…`}
      />
    );
  }

  return (
    <div className="rounded-md border border-[var(--rule)] bg-paper-2/55 px-6 py-5">
      <h3 className="font-display text-xl font-semibold">本地服务端口</h3>
      <p className="mt-2 text-sm leading-6 text-ink-soft">
        浏览器与系统托盘访问本地 WIKI 所用的端口。修改后需重启应用生效。
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <input
          type="number"
          min={1024}
          max={65535}
          value={port}
          disabled={busy}
          onChange={(event) => {
            setPort(event.target.value);
            if (phase === "confirming") setPhase("idle");
          }}
          className="font-mono w-40 rounded-sm border border-[var(--rule)] bg-paper/80 px-3 py-2 text-sm text-ink outline-none focus:border-cinnabar disabled:opacity-60"
        />
        <span className="font-mono text-xs text-ink-faint">
          当前：{currentPort}
        </span>
      </div>

      {!valid && port !== "" ? (
        <p className="mt-2 text-sm text-cinnabar">端口需为 1024–65535 的整数。</p>
      ) : null}
      {error ? <p className="mt-2 text-sm text-cinnabar">{error}</p> : null}

      {phase === "confirming" ? (
        <div className="mt-4 rounded-md border border-cinnabar/40 bg-cinnabar/10 px-4 py-3">
          <p className="text-sm leading-6 text-ink">
            确定将端口改为 <span className="font-mono">{parsed}</span> 吗？
            这会立即重启应用；已打开的 WIKI 页面需用新端口重新打开。
          </p>
          <div className="mt-3 flex gap-3">
            <button
              type="button"
              onClick={saveAndRestart}
              disabled={busy}
              className="rounded-sm bg-cinnabar px-4 py-2 text-sm font-semibold text-paper transition hover:opacity-90 disabled:opacity-60"
            >
              确认重启
            </button>
            <button
              type="button"
              onClick={() => setPhase("idle")}
              disabled={busy}
              className="rounded-sm border border-[var(--rule)] px-4 py-2 text-sm text-ink-soft transition hover:bg-paper/60"
            >
              取消
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setPhase("confirming")}
          disabled={!changed}
          className="mt-4 rounded-sm bg-cinnabar px-4 py-2 text-sm font-semibold text-paper transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          保存并重启
        </button>
      )}
    </div>
  );
}

function AutostartPanel() {
  const [supported, setSupported] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getAutostartConfig()
      .then((cfg) => {
        if (!cancelled) {
          setSupported(cfg.supported);
          setEnabled(cfg.enabled);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "自启状态读取失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function toggle() {
    setError(null);
    setSaving(true);
    try {
      const next = await setAutostart(!enabled);
      setEnabled(next.enabled);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  // 纯浏览器访问（非桌面外壳）拿不到自启钩子，整块隐藏。
  if (!loading && !supported && !error) return null;

  return (
    <div className="rounded-md border border-[var(--rule)] bg-paper-2/55 px-6 py-5">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h3 className="font-display text-xl font-semibold">开机自启动</h3>
          <p className="mt-2 text-sm leading-6 text-ink-soft">
            登录系统后自动在后台启动应用，本地 WIKI 与托盘随之就绪。
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          disabled={loading || saving || !supported}
          onClick={toggle}
          className={[
            "relative h-7 w-12 shrink-0 rounded-full border transition disabled:cursor-not-allowed disabled:opacity-50",
            enabled
              ? "border-cinnabar bg-cinnabar"
              : "border-[var(--rule)] bg-paper/80",
          ].join(" ")}
        >
          <span
            className={[
              "absolute top-0.5 h-5 w-5 rounded-full transition-all",
              enabled ? "left-6 bg-paper" : "left-0.5 bg-ink-faint",
            ].join(" ")}
          />
        </button>
      </div>
      {error ? <p className="mt-2 text-sm text-cinnabar">{error}</p> : null}
    </div>
  );
}

function PathRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid min-w-0 grid-cols-[72px_minmax(0,1fr)] items-baseline gap-3">
      <dt className="font-mono text-[0.68rem] uppercase tracking-[0.16em] text-ink-faint">
        {label}
      </dt>
      <dd className="font-mono min-w-0 overflow-hidden text-ellipsis whitespace-nowrap rounded-sm border border-[var(--rule-soft)] bg-paper/70 px-3 py-2 text-sm text-ink-soft">
        {value}
      </dd>
    </div>
  );
}

function PanelMessage({
  title,
  body,
  tone = "normal",
}: {
  title: string;
  body: string;
  tone?: "normal" | "error";
}) {
  return (
    <div
      className={[
        "rounded-md border px-6 py-5",
        tone === "error"
          ? "border-cinnabar/40 bg-cinnabar/10"
          : "border-[var(--rule)] bg-paper-2/55",
      ].join(" ")}
    >
      <h3 className="font-display text-xl font-semibold">{title}</h3>
      <p className="mt-2 text-sm leading-6 text-ink-soft">{body}</p>
    </div>
  );
}
