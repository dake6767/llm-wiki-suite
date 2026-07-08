import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  clearToken,
  getReview,
  getTree,
  typeLabel,
  wikiDefaultPath,
  wikiRecentPath,
  type TreeNode,
  type WikiInfo,
} from "../api/client";
import { useResizableWidth } from "../lib/useResizableWidth";
import McpConnectModal from "./McpConnectModal";
import ResizeHandle from "./ResizeHandle";

export default function Sidebar({
  wikis,
  current,
}: {
  wikis: WikiInfo[];
  current?: WikiInfo;
}) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  // 祖先布局拿不到子路由参数，从 pathname 解析。
  const wiki = pathname.match(/^\/w\/([^/]+)/)?.[1];
  // 类目高亮：browse/:type，或 page/<类目>/...（须有嵌套段，根级页不算类目）。
  const type =
    pathname.match(/^\/w\/[^/]+\/browse\/([^/]+)/)?.[1] ||
    pathname.match(/^\/w\/[^/]+\/page\/([^/]+)\/.+/)?.[1];
  const onReview = /^\/w\/[^/]+\/review(\/|$)/.test(pathname);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [reviewOpen, setReviewOpen] = useState(0);
  const [q, setQ] = useState("");
  const [mcpOpen, setMcpOpen] = useState(false);
  // 书脊侧栏可拖拽调宽
  const { ref, width, startResize } = useResizableWidth<HTMLElement>(
    "sidebarWidth",
    288,
    200,
    420,
  );

  useEffect(() => {
    if (!wiki) {
      setTree([]);
      return;
    }
    let cancelled = false;
    getTree(wiki)
      .then((items) => {
        if (!cancelled) setTree(items);
      })
      .catch(() => {
        if (!cancelled) setTree([]);
      });
    return () => {
      cancelled = true;
    };
  }, [wiki]);

  // review 待办计数随导航刷新（agent 处理完队列后回到浏览器即见新数）。
  useEffect(() => {
    if (!wiki) {
      setReviewOpen(0);
      return;
    }
    let cancelled = false;
    getReview(wiki)
      .then((res) => {
        if (!cancelled) setReviewOpen(res.open_count);
      })
      .catch(() => {
        if (!cancelled) setReviewOpen(0);
      });
    return () => {
      cancelled = true;
    };
  }, [wiki, pathname]);

  function onSearch(e: React.FormEvent) {
    e.preventDefault();
    if (wiki && q.trim())
      navigate(`/w/${wiki}/search?q=${encodeURIComponent(q.trim())}`);
  }

  return (
    <aside
      ref={ref}
      style={{ width }}
      className="spine relative flex shrink-0 flex-col bg-spine text-cream"
    >
      {/* 印记 / wordmark */}
      <Link
        to="/"
        className="flex items-center gap-3 px-5 pb-5 pt-6"
        style={{ borderBottom: "1px solid var(--rule-cream)" }}
      >
        <span className="seal h-10 w-10 text-2xl leading-none">藏</span>
        <span className="leading-tight">
          <span className="block font-display text-lg font-semibold tracking-wide text-paper">
            LLM&nbsp;Wiki
          </span>
          <span className="eyebrow block text-cream-soft">The&nbsp;Collection</span>
        </span>
      </Link>

      {/* 知识库切换 */}
      <div className="px-5 py-4" style={{ borderBottom: "1px solid var(--rule-cream)" }}>
        <label className="eyebrow mb-2 block text-cream-soft">
          知识库 · Vol.
        </label>
        <select
          className="spine-select"
          value={wiki || ""}
          onChange={(e) => navigate(wikiDefaultPath(e.target.value))}
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

      {/* 搜索 */}
      <form
        onSubmit={onSearch}
        className="px-5 py-4"
        style={{ borderBottom: "1px solid var(--rule-cream)" }}
      >
        <div className="flex items-center gap-2 border-b border-[color:var(--rule-cream)] pb-1.5 focus-within:border-cinnabar">
          <span className="font-mono text-sm text-cinnabar">⌕</span>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="检索本卷…"
            className="w-full bg-transparent font-serif text-sm text-cream placeholder:text-cream-soft/70 focus:outline-none"
          />
        </div>
      </form>

      {/* 分类索引 */}
      <nav className="flex-1 overflow-y-auto px-5 py-4">
        <div className="eyebrow mb-3 text-cream-soft">类目 · Index</div>
        <Link
          to={wiki ? wikiRecentPath(wiki) : "/"}
          className="group flex items-baseline py-2 transition-colors"
        >
          <span
            className={`mr-2 inline-block w-0.5 self-stretch transition-colors ${
              !type ? "bg-cinnabar" : "bg-transparent group-hover:bg-cinnabar/40"
            }`}
          />
          <span
            className={`font-serif text-[0.95rem] transition-colors ${
              !type && !onReview ? "text-cinnabar" : "text-cream group-hover:text-paper"
            }`}
          >
            全部 · 最近
          </span>
        </Link>
        {wiki && (
          <Link
            to={`/w/${wiki}/review`}
            className="group flex items-baseline py-2 transition-colors"
          >
            <span
              className={`mr-2 inline-block w-0.5 self-stretch transition-colors ${
                onReview ? "bg-cinnabar" : "bg-transparent group-hover:bg-cinnabar/40"
              }`}
            />
            <span
              className={`font-serif text-[0.95rem] transition-colors ${
                onReview ? "text-cinnabar" : "text-cream group-hover:text-paper"
              }`}
            >
              待审 · Review
            </span>
            <span
              className="mx-2 flex-1 self-center border-b border-dotted"
              style={{ borderColor: "var(--rule-cream)" }}
            />
            {reviewOpen > 0 ? (
              <span className="tnum rounded-full bg-cinnabar px-1.5 font-mono text-xs text-paper">
                {reviewOpen}
              </span>
            ) : (
              <span
                className={`tnum font-mono text-xs ${
                  onReview ? "text-cinnabar" : "text-cream-soft"
                }`}
              >
                00
              </span>
            )}
          </Link>
        )}
        {tree.map((node) => {
          const active = type === node.type;
          return (
            <Link
              key={node.type}
              to={`/w/${wiki}/browse/${node.type}`}
              className="group flex items-baseline py-2 transition-colors"
            >
              <span
                className={`mr-2 inline-block w-0.5 self-stretch transition-colors ${
                  active ? "bg-cinnabar" : "bg-transparent group-hover:bg-cinnabar/40"
                }`}
              />
              <span
                className={`font-serif text-[0.95rem] transition-colors ${
                  active
                    ? "text-cinnabar"
                    : "text-cream group-hover:text-paper"
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
            </Link>
          );
        })}
      </nav>

      <div
        className="flex items-center justify-between px-5 py-4"
        style={{ borderTop: "1px solid var(--rule-cream)" }}
      >
        <span className="flex items-center gap-2">
          <span className="eyebrow text-cream-soft/70">文件即真相</span>
          {/* MCP 入口：复制注册提示词给其他 agent（consult-first 读回路的入口） */}
          <button
            onClick={() => setMcpOpen(true)}
            title="连接 MCP：复制注册提示词给 agent"
            aria-label="连接 MCP"
            className="flex items-center gap-1 text-cream-soft/70 transition-colors hover:text-cinnabar"
          >
            <svg
              viewBox="0 0 24 24"
              className="h-3.5 w-3.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M12 22v-5" />
              <path d="M9 8V2" />
              <path d="M15 8V2" />
              <path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z" />
            </svg>
            <span className="font-mono text-[0.65rem] tracking-wider">MCP</span>
          </button>
        </span>
        <button
          onClick={() => {
            clearToken();
            navigate("/login");
          }}
          className="font-mono text-xs text-cream-soft transition-colors hover:text-cinnabar"
        >
          退出 →
        </button>
      </div>

      <McpConnectModal open={mcpOpen} onClose={() => setMcpOpen(false)} />

      <ResizeHandle dark onPointerDown={startResize} />
    </aside>
  );
}
