import { useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  addedDateLabel,
  byAddedDesc,
  getTree,
  typeLabel,
  type PageRef,
  type TreeNode,
} from "../api/client";

function parse(pathname: string) {
  const m = pathname.match(/^\/w\/([^/]+)(?:\/(.*))?$/);
  const wiki = m ? decodeURIComponent(m[1]) : "";
  const rest = m?.[2] || "";
  const type = rest.startsWith("browse/")
    ? decodeURIComponent(rest.slice("browse/".length))
    : "";
  return { wiki, type };
}

// 移动端列表：按加入库时间倒序，顶部可过滤；点条目进入阅读页。
export default function MobileList() {
  const { pathname } = useLocation();
  const { wiki, type } = parse(pathname);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    if (!wiki) {
      setTree([]);
      return;
    }
    let cancelled = false;
    const load = () => {
      getTree(wiki)
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

  const base: PageRef[] = useMemo(() => {
    if (type) return tree.find((n) => n.type === type)?.pages ?? [];
    return tree.flatMap((n) => n.pages);
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

  return (
    <div className="flex h-full flex-col">
      {/* 过滤条 */}
      <div className="shrink-0 px-4 pb-3 pt-4">
        <div className="eyebrow mb-2 flex items-center gap-2">
          <span className="text-cinnabar">{heading}</span>
          <span className="text-ink-faint/50">·</span>
          <span className="tnum text-ink-faint">{pages.length} 篇</span>
          <span className="ml-auto text-ink-faint/70">加入库 ↓</span>
        </div>
        <div className="flex items-center gap-2 rounded border border-[color:var(--rule)] bg-paper-2/50 px-3 py-2 focus-within:border-cinnabar">
          <span className="font-mono text-sm text-cinnabar">⌕</span>
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="按标题 / 标签过滤…"
            className="w-full bg-transparent font-serif text-[0.95rem] text-ink placeholder:text-ink-faint focus:outline-none"
          />
        </div>
      </div>

      {/* 列表 */}
      <ul className="min-h-0 flex-1 overflow-y-auto px-4 pb-8">
        {pages.map((p) => {
          const date = addedDateLabel(p);
          return (
            <li key={p.path}>
              <Link
                to={`/w/${wiki}/page/${p.path}`}
                className="block border-b border-[color:var(--rule-soft)] py-3.5 text-ink transition-colors active:text-cinnabar"
              >
                <div className="flex items-baseline gap-2">
                  <span className="font-display text-[1.02rem] leading-snug">
                    {p.title}
                  </span>
                  {date && (
                    <span className="tnum ml-auto shrink-0 font-mono text-[0.66rem] text-ink-faint">
                      {date}
                    </span>
                  )}
                </div>
                {p.tags.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-x-2.5 gap-y-1">
                    {p.tags.slice(0, 4).map((t) => (
                      <span
                        key={t}
                        className="font-mono text-[0.62rem] uppercase tracking-wide text-brass"
                      >
                        #{t}
                      </span>
                    ))}
                  </div>
                )}
              </Link>
            </li>
          );
        })}
        {pages.length === 0 && (
          <li className="py-12 text-center font-serif text-sm italic text-ink-faint">
            {wiki ? "无结果" : "请选择知识库"}
          </li>
        )}
      </ul>
    </div>
  );
}
