import { useEffect, useState } from "react";
import {
  createShare,
  getShareConfig,
  getShareLink,
  listShares,
  renewShare,
  revokeShare,
  setDefaultShare,
  type ShareGrantView,
} from "../api/client";
import { copyToClipboard } from "../lib/reviewPrompt";
import { linkForContext } from "../lib/shareLinks";

// 「分享管理」面板（docs/19 §4.5）。同一组件复用于两处入口：
// - 浏览界面顶栏（lockedWiki=当前库，创建为主）；
// - 设置页（带 wiki 选择器，偏治理/盘点）。
// 能力固定只读、整库：「通用分享」供页面深链复用，「独立分享」供按对象撤销。

type WikiOption = { key: string; name: string; page_count: number };

type SharePanelProps = {
  open: boolean;
  onClose?: () => void;
  wikiOptions: WikiOption[];
  lockedWiki?: string;
  deepLinkPath?: string;
  embedded?: boolean;
  initialView?: "create" | "manage";
  initialCreateKind?: "default" | "independent";
};

const EXPIRY_OPTIONS: Array<{ label: string; days: number | null }> = [
  { label: "7 天", days: 7 },
  { label: "30 天（默认）", days: 30 },
  { label: "永久（高级）", days: null },
];

function fmtDate(secs: number | null | undefined): string {
  if (!secs) return "永久";
  const d = new Date(secs * 1000);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

function fmtLastAccessed(secs: number | null): string {
  if (!secs) return "从未访问";
  return `最近访问 ${fmtDate(secs)}`;
}

function scopeLabel(scope: ShareGrantView["scope"]): string {
  switch (scope.kind) {
    case "frozen_set":
      return `冻结页面集（${scope.pages.length} 页）`;
    case "index_anchor":
      return scope.live ? "索引锚点（持续同步）" : "索引锚点（固定范围）";
    default:
      return "整个 Wiki（持续同步）";
  }
}

export default function SharePanel({
  open,
  onClose,
  wikiOptions,
  lockedWiki,
  deepLinkPath,
  embedded = false,
  initialView = "create",
  initialCreateKind = "independent",
}: SharePanelProps) {
  const [view, setView] = useState<"create" | "manage">(initialView);
  const [createKind, setCreateKind] = useState<"default" | "independent">(
    initialCreateKind,
  );
  const [wiki, setWiki] = useState(lockedWiki || wikiOptions[0]?.key || "");
  const [label, setLabel] = useState("");
  const [expiryIdx, setExpiryIdx] = useState(1); // 默认 30 天
  const [creating, setCreating] = useState(false);
  const [createdLink, setCreatedLink] = useState<string | null>(null);
  const [createWarning, setCreateWarning] = useState<string | undefined>();
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [relayConnected, setRelayConnected] = useState<boolean | null>(null);
  const [shares, setShares] = useState<ShareGrantView[]>([]);
  const [shareLinks, setShareLinks] = useState<Record<string, string>>({});
  const [loadingList, setLoadingList] = useState(false);
  const [confirmingRevoke, setConfirmingRevoke] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  const current = wikiOptions.find((w) => w.key === wiki);

  useEffect(() => {
    if (!open) return;
    setError(null);
    setCreatedLink(null);
    setCopied(false);
    setConfirmingRevoke(null);
    setView(initialView);
    setCreateKind(initialCreateKind);
    setWiki(lockedWiki || wikiOptions[0]?.key || "");
    getShareConfig()
      .then((c) => setRelayConnected(c.relay_connected))
      .catch(() => setRelayConnected(null));
    if (embedded) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose?.();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [
    open,
    lockedWiki,
    wikiOptions,
    onClose,
    embedded,
    initialView,
    initialCreateKind,
  ]);

  async function refreshList() {
    setLoadingList(true);
    setError(null);
    setShareLinks({});
    try {
      const allItems = await listShares();
      const items = lockedWiki
        ? allItems.filter((item) => item.wiki === lockedWiki)
        : allItems;
      setShares(items);
      const results = await Promise.allSettled(
        items
          .filter((item) => item.active)
          .map(async (item) => {
            const result = await getShareLink(item.grant_id);
            return [
              item.grant_id,
              linkForContext(result.link, deepLinkPath),
            ] as const;
          }),
      );
      const links = results.flatMap((result) =>
        result.status === "fulfilled" ? [result.value] : [],
      );
      setShareLinks(Object.fromEntries(links));
      if (results.some((result) => result.status === "rejected")) {
        setError("部分分享链接读取失败，可点击「复制链接」重试");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "读取分享列表失败");
    } finally {
      setLoadingList(false);
    }
  }

  useEffect(() => {
    if (open && view === "manage") refreshList();
  }, [open, view, deepLinkPath, lockedWiki]);

  async function onCreate() {
    setCreating(true);
    setError(null);
    setCreatedLink(null);
    try {
      const days = EXPIRY_OPTIONS[expiryIdx].days;
      const res = await createShare(
        wiki,
        createKind === "default" ? "通用分享" : label.trim(),
        days,
        createKind === "default",
      );
      const link = linkForContext(res.link, deepLinkPath);
      setCreatedLink(link);
      setCreateWarning(res.warning);
      const ok = await copyToClipboard(link);
      setCopied(ok);
      setLabel("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "创建分享失败");
    } finally {
      setCreating(false);
    }
  }

  async function onCopyExisting(grantId: string) {
    try {
      const stored = shareLinks[grantId];
      const link = stored || linkForContext((await getShareLink(grantId)).link, deepLinkPath);
      const ok = await copyToClipboard(link);
      if (ok) {
        setError(null);
        flashCopied(grantId);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "复制链接失败");
    }
  }

  const [copiedRow, setCopiedRow] = useState<string | null>(null);
  function flashCopied(id: string) {
    setCopiedRow(id);
    window.setTimeout(() => setCopiedRow((v) => (v === id ? null : v)), 1600);
  }

  async function onRenew(grantId: string) {
    try {
      await renewShare(grantId, 30);
      refreshList();
    } catch (e) {
      setError(e instanceof Error ? e.message : "续期失败");
    }
  }

  async function onRevoke(grantId: string) {
    setRevokingId(grantId);
    try {
      await revokeShare(grantId);
      setConfirmingRevoke(null);
      await refreshList();
    } catch (e) {
      setError(e instanceof Error ? e.message : "关闭分享失败");
    } finally {
      setRevokingId(null);
    }
  }

  async function onSetDefault(grantId: string) {
    try {
      await setDefaultShare(grantId);
      await refreshList();
    } catch (e) {
      setError(e instanceof Error ? e.message : "设为通用分享失败");
    }
  }

  async function onCopyCreated() {
    if (!createdLink) return;
    const ok = await copyToClipboard(createdLink);
    setCopied(ok);
  }

  return (
    <>
      {!embedded && (
        <div
          onClick={onClose}
          className={`fixed inset-0 z-40 bg-spine/30 transition-opacity duration-300 ${
            open ? "opacity-100" : "pointer-events-none opacity-0"
          }`}
        />
      )}
      <div
        role={embedded ? "region" : "dialog"}
        aria-modal={embedded ? undefined : "true"}
        aria-label="分享管理"
        className={
          embedded
            ? "flex w-full flex-col overflow-hidden rounded-md border border-[color:var(--rule)] bg-paper"
            : `fixed left-1/2 top-1/2 z-50 flex max-h-[85vh] w-[38rem] max-w-[93vw] -translate-x-1/2 -translate-y-1/2 flex-col border border-[color:var(--rule)] bg-paper shadow-2xl transition-opacity duration-200 ${
                open ? "opacity-100" : "pointer-events-none opacity-0"
              }`
        }
      >
        {/* 头部 + 视图切换 */}
        <div className="flex shrink-0 items-center gap-4 border-b border-[color:var(--rule)] px-6 py-4">
          <div className="eyebrow text-cinnabar">分享 · Share</div>
          <nav className="flex gap-3">
            <button
              onClick={() => {
                setCreateKind("independent");
                setCreatedLink(null);
                setView("create");
              }}
              className={`font-mono text-xs ${
                view === "create" ? "text-cinnabar" : "text-ink-faint hover:text-ink"
              }`}
            >
              新建
            </button>
            <button
              onClick={() => setView("manage")}
              className={`font-mono text-xs ${
                view === "manage" ? "text-cinnabar" : "text-ink-faint hover:text-ink"
              }`}
            >
              管理
            </button>
          </nav>
          <span className="flex-1" />
          {!embedded && (
            <button
              onClick={onClose}
              className="font-mono text-sm text-ink-faint transition-colors hover:text-cinnabar"
              aria-label="关闭"
            >
              ✕
            </button>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {relayConnected === false && (
            <div className="mb-4 border border-cinnabar/30 bg-cinnabar/5 px-3 py-2 font-serif text-xs text-cinnabar-deep">
              线上访问未开启，此时创建的链接对外不可用。请在托盘开启中继服务后再分享。
            </div>
          )}
          {error && (
            <div className="mb-4 border border-cinnabar/40 bg-cinnabar/5 px-3 py-2 font-mono text-xs text-cinnabar-deep">
              {error}
            </div>
          )}

          {view === "create" ? (
            <div>
              <p className="mb-4 font-serif text-sm leading-relaxed text-ink">
                {createKind === "default"
                  ? "首次启用此 Wiki 的通用分享。以后分享页面时会复用这条链接，不再重复创建授权——"
                  : "创建一条可独立管理的链接，把整个 Wiki 分享给特定对象——"}
                <span className="text-cinnabar-deep">
                  对方将持续看到此 Wiki 的全部页面，包括你之后新增的内容
                </span>
                （RAW 目录与维护队列不在分享范围内；页面显式引用的来源可在「源」弹窗预览）。
              </p>
              {deepLinkPath && (
                <p className="mb-4 border-l-2 border-brass bg-brass/5 px-3 py-2 font-serif text-xs leading-relaxed text-ink-soft">
                  链接会直接打开当前页；授权范围仍是整个 Wiki，对方可继续浏览库内其他页面。
                </p>
              )}

              <label className="eyebrow mb-1 block text-ink-faint">知识库</label>
              {lockedWiki ? (
                <div className="mb-4 font-serif text-sm text-ink">
                  {current?.name || wiki}
                  {current ? `（${current.page_count} 页）` : ""}
                </div>
              ) : (
                <select
                  className="mb-4 w-full border border-[color:var(--rule)] bg-paper px-2 py-1.5 font-serif text-sm text-ink"
                  value={wiki}
                  onChange={(e) => setWiki(e.target.value)}
                >
                  {wikiOptions.map((w) => (
                    <option key={w.key} value={w.key}>
                      {w.name}（{w.page_count} 页）
                    </option>
                  ))}
                </select>
              )}

              {createKind === "independent" && (
                <>
                  <label className="eyebrow mb-1 block text-ink-faint">
                    备注（可选，便于日后辨认）
                  </label>
                  <input
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    placeholder="给小王的分享"
                    className="mb-4 w-full border-b border-[color:var(--rule)] bg-transparent pb-1.5 font-serif text-sm text-ink focus:border-cinnabar focus:outline-none"
                  />
                </>
              )}

              <label className="eyebrow mb-1 block text-ink-faint">有效期</label>
              <select
                className="mb-5 w-full border border-[color:var(--rule)] bg-paper px-2 py-1.5 font-serif text-sm text-ink"
                value={expiryIdx}
                onChange={(e) => setExpiryIdx(Number(e.target.value))}
              >
                {EXPIRY_OPTIONS.map((o, i) => (
                  <option key={i} value={i}>
                    {o.label}
                  </option>
                ))}
              </select>

              {createdLink ? (
                <div className="border border-[color:var(--rule)] bg-spine/5 px-3 py-3">
                  <div className="eyebrow mb-1 text-cinnabar">
                    {copied ? "链接已复制 ✓" : "分享链接"}
                  </div>
                  <div className="mb-2 break-all font-mono text-xs text-ink">
                    {createdLink}
                  </div>
                  {createWarning && (
                    <div className="mb-2 font-serif text-xs text-cinnabar-deep">
                      {createWarning}
                    </div>
                  )}
                  <button
                    onClick={onCopyCreated}
                    className="font-mono text-xs text-cinnabar hover:text-ink"
                  >
                    再次复制 →
                  </button>
                </div>
              ) : (
                <button
                  onClick={onCreate}
                  disabled={creating || !wiki}
                  className="w-full bg-cinnabar py-2.5 font-mono text-sm font-medium text-on-accent transition-colors hover:bg-cinnabar-deep disabled:opacity-50"
                >
                  {creating
                    ? "创建中…"
                    : createKind === "default"
                      ? "启用并复制当前页链接 →"
                      : "创建独立分享并复制 →"}
                </button>
              )}
            </div>
          ) : (
            <div>
              {loadingList ? (
                <p className="font-serif text-sm text-ink-faint">读取中…</p>
              ) : shares.length === 0 ? (
                <p className="font-serif text-sm text-ink-faint">
                  还没有创建过分享。切到「新建」创建第一条。
                </p>
              ) : (
                <ul className="divide-y divide-[color:var(--rule)]">
                  {shares.map((s) => (
                    <li key={s.grant_id} className="py-3">
                      <div className="flex items-baseline gap-2">
                        <span className="font-serif text-sm text-ink">
                          {s.label}
                        </span>
                        <span className="font-mono text-xs text-ink-faint">
                          · {s.wiki}
                        </span>
                        {s.is_default && (
                          <span className="rounded-sm border border-brass/40 bg-brass/5 px-1.5 py-0.5 font-mono text-[0.65rem] text-brass">
                            通用
                          </span>
                        )}
                        <span className="flex-1" />
                        {s.revoked ? (
                          <span className="font-mono text-xs text-ink-faint">
                            已关闭
                          </span>
                        ) : !s.active ? (
                          <span className="font-mono text-xs text-cinnabar-deep">
                            已过期
                          </span>
                        ) : (
                          <span className="font-mono text-xs text-ink">
                            生效中
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 font-mono text-[0.7rem] text-ink-faint">
                        到期 {fmtDate(s.expires_at)} · {fmtLastAccessed(s.last_accessed)}
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1.5 font-mono text-[0.68rem]">
                        <span className="rounded-sm border border-brass/30 bg-brass/5 px-2 py-1 text-brass">
                          范围：{scopeLabel(s.scope)}
                        </span>
                        <span className="rounded-sm border border-[color:var(--rule)] px-2 py-1 text-ink-soft">
                          只读 · 页面 / 附件 / 目录 / 搜索 / 图谱
                          {s.include_raw
                            ? " / RAW"
                            : " · 仅来源引用预览，不含 RAW 目录 / 审阅 / MCP"}
                        </span>
                      </div>
                      {s.active && shareLinks[s.grant_id] && (
                        <div className="mt-2 break-all border border-[color:var(--rule)] bg-spine/5 px-2.5 py-2 font-mono text-[0.68rem] leading-relaxed text-ink-soft">
                          {shareLinks[s.grant_id]}
                        </div>
                      )}
                      {!s.revoked && (
                        <div className="mt-1.5 flex flex-wrap items-center gap-3">
                          {s.active && (
                            <button
                              onClick={() => onCopyExisting(s.grant_id)}
                              className="font-mono text-xs text-cinnabar hover:text-ink"
                            >
                              {copiedRow === s.grant_id ? "已复制 ✓" : "复制链接"}
                            </button>
                          )}
                          {s.active && !s.is_default && (
                            <button
                              onClick={() => onSetDefault(s.grant_id)}
                              className="font-mono text-xs text-brass hover:text-ink"
                            >
                              设为通用
                            </button>
                          )}
                          <button
                            onClick={() => onRenew(s.grant_id)}
                            className="font-mono text-xs text-ink-faint hover:text-ink"
                          >
                            续期 30 天
                          </button>
                          {confirmingRevoke === s.grant_id ? (
                            <>
                              <span className="font-serif text-xs text-cinnabar-deep">
                                关闭后链接立即失效且无法恢复
                              </span>
                              <button
                                onClick={() => onRevoke(s.grant_id)}
                                disabled={revokingId === s.grant_id}
                                className="font-mono text-xs font-medium text-cinnabar hover:text-cinnabar-deep disabled:opacity-50"
                              >
                                {revokingId === s.grant_id ? "关闭中…" : "确认关闭"}
                              </button>
                              <button
                                onClick={() => setConfirmingRevoke(null)}
                                disabled={revokingId === s.grant_id}
                                className="font-mono text-xs text-ink-faint hover:text-ink disabled:opacity-50"
                              >
                                取消
                              </button>
                            </>
                          ) : (
                            <button
                              onClick={() => setConfirmingRevoke(s.grant_id)}
                              className="font-mono text-xs text-cinnabar hover:text-cinnabar-deep"
                            >
                              关闭分享
                            </button>
                          )}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
