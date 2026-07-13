import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  addedDateLabel,
  byAddedDesc,
  getBrowseTree,
  typeLabel,
  type PageRef,
  type TreeNode,
} from "../api/client";
import { ShareLink } from "../lib/shareNavigation";

// 从 /w/:wiki/... 解析出 wiki、活动类目、当前阅读页路径。
function parse(pathname: string) {
  const m = pathname.match(/^\/w\/([^/]+)(?:\/(.*))?$/);
  const wiki = m ? decodeURIComponent(m[1]) : "";
  const rest = m?.[2] || "";
  let type = ""; // "" = 最近（全部）
  let currentPath = "";
  if (rest.startsWith("browse/")) {
    type = decodeURIComponent(rest.slice("browse/".length));
  } else if (rest.startsWith("page/")) {
    // pathname 是 URL 编码的，需解码后才能与列表项的 p.path 相等（高亮当前页）。
    currentPath = decodeURIComponent(rest.slice("page/".length));
    // 仅当页面带类目段（含 /）时按类目过滤；根级页（overview/log 等）保持「最近」。
    type = currentPath.includes("/") ? currentPath.split("/")[0] : "";
  } else if (rest.startsWith("raw/")) {
    currentPath = decodeURIComponent(rest.slice("raw/".length));
    type = "raw";
  }
  return { wiki, type, currentPath };
}

export default function PageList() {
  const { pathname } = useLocation();
  const { wiki, type, currentPath } = parse(pathname);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [filter, setFilter] = useState("");
  const activeRef = useRef<HTMLAnchorElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  useEffect(() => {
    if (!wiki) {
      setTree([]);
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
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [wiki]);

  // 活动类目存在则取该类目页面，否则汇总全部（最近）。
  const base: PageRef[] = useMemo(() => {
    if (type) return tree.find((n) => n.type === type)?.pages ?? [];
    return tree.filter((n) => n.type !== "raw").flatMap((n) => n.pages);
  }, [tree, type]);

  const pages = useMemo(() => {
    let list = base;
    const f = filter.trim().toLowerCase();
    if (f)
      list = list.filter(
        (p) =>
          p.title.toLowerCase().includes(f) ||
          p.tags.some((t) => t.toLowerCase().includes(f)),
      );
    return [...list].sort(byAddedDesc);
  }, [base, filter]);

  const heading = type ? typeLabel(type) : "最近 · Recent";

  useEffect(() => {
    if (!currentPath) return;
    const frame = requestAnimationFrame(() => {
      const active = activeRef.current;
      const list = listRef.current;
      if (!active || !list) return;

      const activeBox = active.getBoundingClientRect();
      const listBox = list.getBoundingClientRect();
      const visiblePadding = 12;
      const above = activeBox.top < listBox.top + visiblePadding;
      const below = activeBox.bottom > listBox.bottom - visiblePadding;
      if (above || below) {
        active.scrollIntoView({ block: "center", inline: "nearest" });
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [currentPath, pages]);

  return (
    <div className="flex h-full flex-col">
      {/* 头部：类目名 + 计数 */}
      <div className="shrink-0 px-5 pb-3 pt-6">
        <div className="eyebrow mb-2 flex items-center gap-2">
          <span className="text-cinnabar">{heading}</span>
          <span className="text-ink-faint/50">·</span>
          <span className="tnum text-ink-faint">{pages.length} 篇</span>
          <span className="ml-auto text-ink-faint/70">加入库 ↓</span>
        </div>
        <div className="flex items-center gap-2 border-b border-[color:var(--rule)] pb-1.5 focus-within:border-cinnabar">
          <span className="font-mono text-sm text-cinnabar">⌕</span>
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="按标题 / 标签过滤…"
            className="w-full bg-transparent font-serif text-sm text-ink placeholder:text-ink-faint focus:outline-none"
          />
        </div>
      </div>

      {/* 列表 */}
      <ul ref={listRef} className="min-h-0 flex-1 overflow-y-auto px-5 pb-6">
        {pages.map((p) => {
          const active = p.path === currentPath;
          const date = addedDateLabel(p);
          return (
            <li key={p.path}>
              <ShareLink
                ref={active ? activeRef : undefined}
                to={`/w/${wiki}/${type === "raw" ? "raw" : "page"}/${p.path}`}
                aria-current={active ? "page" : undefined}
                className={`group block border-b border-[color:var(--rule-soft)] py-2.5 transition-colors ${
                  active
                    ? "-mx-5 bg-cinnabar/[0.06] px-5 text-cinnabar"
                    : "text-ink hover:text-cinnabar"
                }`}
              >
                <div className="flex items-baseline gap-2">
                  <span
                    className={`mr-0.5 inline-block w-0.5 self-stretch ${
                      active ? "bg-cinnabar" : "bg-transparent"
                    }`}
                  />
                  <span className="font-display text-[0.98rem] leading-snug">
                    {p.title}
                  </span>
                  {date && (
                    <span className="tnum ml-auto shrink-0 font-mono text-[0.66rem] text-ink-faint">
                      {date}
                    </span>
                  )}
                </div>
                {p.tags.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-x-2 pl-2.5">
                    {p.tags.slice(0, 3).map((t) => (
                      <span
                        key={t}
                        className="font-mono text-[0.62rem] uppercase tracking-wide text-brass"
                      >
                        #{t}
                      </span>
                    ))}
                  </div>
                )}
                {type === "raw" && (
                  <div className="mt-1 pl-2.5 font-mono text-[0.62rem] uppercase tracking-wide text-ink-faint">
                    {typeLabel(p.type)}
                  </div>
                )}
              </ShareLink>
            </li>
          );
        })}
        {pages.length === 0 && (
          <li className="py-10 text-center font-serif text-sm italic text-ink-faint">
            {wiki ? "无结果" : "请选择知识库"}
          </li>
        )}
      </ul>
    </div>
  );
}
