import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  clearToken,
  getReview,
  getBrowseTree,
  typeLabel,
  wikiDefaultPath,
  wikiRecentPath,
  type TreeNode,
  type WikiInfo,
} from "../api/client";
import { useShareNavigate } from "../lib/shareNavigation";
import { BASE } from "../lib/basePath";
import { clearActiveShare, isGuest } from "../lib/shareSession";
import { BrandLogo } from "../components/BrandLogo";
import ThemeSwitcher from "../components/ThemeSwitcher";

// 移动端左侧抽屉：知识库切换 + 类目索引 + 退出。沿用书脊（spine）配色。
export default function MobileNavSheet({
  wikis,
  current,
  open,
  onClose,
}: {
  wikis: WikiInfo[];
  current?: WikiInfo;
  open: boolean;
  onClose: () => void;
}) {
  const navigate = useShareNavigate();
  const guest = isGuest();
  const { pathname } = useLocation();
  const wiki = pathname.match(/^\/w\/([^/]+)/)?.[1];
  const type =
    (/^\/w\/[^/]+\/raw\//.test(pathname) ? "raw" : "") ||
    pathname.match(/^\/w\/[^/]+\/browse\/([^/]+)/)?.[1] ||
    pathname.match(/^\/w\/[^/]+\/page\/([^/]+)\/.+/)?.[1];
  const onReview = /^\/w\/[^/]+\/review(\/|$)/.test(pathname);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [reviewOpen, setReviewOpen] = useState(0);

  useEffect(() => {
    if (!wiki) {
      setTree([]);
      setReviewOpen(0);
      return;
    }
    let cancelled = false;
    const load = () => {
      getBrowseTree(wiki)
        .then((items) => {
          if (!cancelled) setTree(items);
        })
        .catch(() => {
          if (!cancelled) setTree([]);
        });
      if (!guest) {
        getReview(wiki)
          .then((res) => {
            if (!cancelled) setReviewOpen(res.open_count);
          })
          .catch(() => {
            if (!cancelled) setReviewOpen(0);
          });
      } else {
        setReviewOpen(0);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [wiki, pathname, guest]);

  // 关闭时锁定背景滚动
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  function go(to: string) {
    navigate(to);
    onClose();
  }

  return (
    <>
      <div
        onClick={onClose}
        className={`fixed inset-0 z-40 bg-spine/40 transition-opacity duration-300 ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      <aside
        className={`spine fixed left-0 top-0 z-50 flex h-full w-[82%] max-w-xs flex-col bg-spine text-cream shadow-2xl transition-transform duration-300 ease-[cubic-bezier(0.2,0.7,0.2,1)] ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* 印记 */}
        <div
          className="flex items-center gap-3 px-5 pb-5 pt-6"
          style={{ borderBottom: "1px solid var(--rule-cream)" }}
        >
          <BrandLogo className="h-10 w-10" />
          <span className="leading-tight">
            <span className="block font-display text-lg font-semibold tracking-wide text-cream">
              LLM&nbsp;Wiki
            </span>
            <span className="eyebrow block text-cream-soft">The&nbsp;Collection</span>
          </span>
        </div>

        {/* 知识库切换 */}
        <div
          className="px-5 py-4"
          style={{ borderBottom: "1px solid var(--rule-cream)" }}
        >
          <label className="eyebrow mb-2 block text-cream-soft">知识库 · Vol.</label>
          <select
            className="spine-select"
            value={wiki || ""}
            onChange={(e) => go(wikiDefaultPath(e.target.value))}
          >
            {wikis.map((w) => (
              <option key={w.key} value={w.key}>
                {w.name}（{w.page_count}）
              </option>
            ))}
          </select>
          {current?.description && (
            <p className="mt-3 font-serif text-xs italic leading-relaxed text-cream-soft">
              {current.description}
            </p>
          )}
        </div>

        {/* 类目索引 */}
        <nav className="flex-1 overflow-y-auto px-5 py-4">
          <div className="eyebrow mb-3 text-cream-soft">类目 · Index</div>
          <button
            onClick={() => go(wiki ? wikiRecentPath(wiki) : "/")}
            className="group flex w-full items-baseline py-2.5 text-left"
          >
            <span
              className={`mr-2 inline-block w-0.5 self-stretch ${
                !type ? "bg-cinnabar" : "bg-transparent"
              }`}
            />
            <span
              className={`font-serif text-base ${
                !type && !onReview ? "text-cinnabar" : "text-cream"
              }`}
            >
              全部 · 最近
            </span>
          </button>
          {wiki && !guest && (
            <button
              onClick={() => go(`/w/${wiki}/review`)}
              className="group flex w-full items-baseline py-2.5 text-left"
            >
              <span
                className={`mr-2 inline-block w-0.5 self-stretch ${
                  onReview ? "bg-cinnabar" : "bg-transparent"
                }`}
              />
              <span
                className={`font-serif text-base ${
                  onReview ? "text-cinnabar" : "text-cream"
                }`}
              >
                待审 · Review
              </span>
              <span
                className="mx-2 flex-1 self-center border-b border-dotted"
                style={{ borderColor: "var(--rule-cream)" }}
              />
              {reviewOpen > 0 ? (
                <span className="tnum rounded-full bg-cinnabar px-1.5 font-mono text-xs text-on-accent">
                  {reviewOpen}
                </span>
              ) : (
                <span className="tnum font-mono text-xs text-cream-soft">00</span>
              )}
            </button>
          )}
          {tree.map((node) => {
            const active = type === node.type;
            return (
              <button
                key={node.type}
                onClick={() => go(`/w/${wiki}/browse/${node.type}`)}
                className="group flex w-full items-baseline py-2.5 text-left"
              >
                <span
                  className={`mr-2 inline-block w-0.5 self-stretch ${
                    active ? "bg-cinnabar" : "bg-transparent"
                  }`}
                />
                <span
                  className={`font-serif text-base ${
                    active ? "text-cinnabar" : "text-cream"
                  }`}
                >
                  {typeLabel(node.type)}
                </span>
                <span
                  className="mx-2 flex-1 self-center border-b border-dotted"
                  style={{ borderColor: "var(--rule-cream)" }}
                />
                <span
                  className={`tnum font-mono text-xs ${
                    active ? "text-cinnabar" : "text-cream-soft"
                  }`}
                >
                  {String(node.count).padStart(2, "0")}
                </span>
              </button>
            );
          })}
        </nav>

        <div
          className="px-4 py-3"
          style={{ borderTop: "1px solid var(--rule-cream)" }}
        >
          <div className="eyebrow mb-2 px-1 text-cream-soft">主题家族 · Theme</div>
          <ThemeSwitcher tone="spine" className="w-full" />
        </div>

        <div
          className="flex items-center justify-between px-5 py-4"
          style={{ borderTop: "1px solid var(--rule-cream)" }}
        >
          <span className="eyebrow text-cream-soft/70">
            {guest ? "分享浏览" : "文件即真相"}
          </span>
          <button
            onClick={() => {
              if (guest) {
                clearActiveShare();
                window.location.href = `${BASE}/`;
              } else {
                clearToken();
                navigate("/login");
              }
            }}
            className="font-mono text-xs text-cream-soft transition-colors active:text-cinnabar"
          >
            {guest ? "退出访客预览 →" : "退出 →"}
          </button>
        </div>
      </aside>
    </>
  );
}
