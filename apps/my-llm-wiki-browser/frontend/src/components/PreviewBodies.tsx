import { useEffect, useState } from "react";
import {
  getPage,
  getRaw,
  getSourcePreview,
  groupSearchHits,
  search,
  stripTitleH1,
  typeLabel,
  type Page,
  type SearchHit,
} from "../api/client";
import Markdown from "../lib/Markdown";

// 预览目标：doc=预览某页/raw；search=展示某词的搜索结果。
// 桌面右侧抽屉（ReferenceDrawer）与移动端底部 sheet（PreviewSheet）共用。
export type DrawerTarget =
  | {
      mode: "doc";
      wiki: string;
      kind: "page" | "raw";
      path: string;
      title?: string;
      sourcePage?: string;
    }
  | { mode: "search"; wiki: string; term: string };

// 列表层：FAB 打开后先列某类目（来源/相关/引用）的条目，点条目下钻到 doc/search 预览。
export type SheetListItem = { key: string; label: string; target: DrawerTarget };
export type SheetListTarget = { mode: "list"; title: string; items: SheetListItem[] };
// 预览容器可承载三种视图：类目列表、文档、搜索结果。
export type SheetTarget = DrawerTarget | SheetListTarget;

// 抽屉/sheet 共用的视图状态：root=打开时的视图（用于「返回」），cur=当前视图。
// 关闭后仍保留 cur，供滑出/滑落动画期间继续渲染。
export function usePreview(target: SheetTarget | null) {
  const [root, setRoot] = useState<SheetTarget | null>(null);
  const [cur, setCur] = useState<SheetTarget | null>(null);
  useEffect(() => {
    if (target) {
      setRoot(target);
      setCur(target);
    }
  }, [target]);
  const drilled =
    cur != null && root != null && cur !== root && root.mode === "list";
  return { open: target != null, root, cur, setCur, drilled };
}

export function previewEyebrow(cur: SheetTarget | null): string {
  if (cur?.mode === "list") return "旁注";
  if (cur?.mode === "doc" && cur.kind === "raw") return "原始源";
  return "预览";
}

export function previewTitle(cur: SheetTarget | null): string {
  if (cur?.mode === "list") return cur.title;
  if (cur?.mode === "search") return `相关 · ${cur.term}`;
  return cur?.title || cur?.path.split("/").pop() || "预览";
}

// 共用的内容区：类目列表 / 文档 / 搜索结果。onDrill 用于在列表或搜索结果中点条目下钻。
export function PreviewBody({
  cur,
  onDrill,
}: {
  cur: SheetTarget | null;
  onDrill: (t: DrawerTarget) => void;
}) {
  if (cur?.mode === "list")
    return (
      <ul className="reveal -my-1">
        {cur.items.map((it) => (
          <li key={it.key}>
            <button
              onClick={() => onDrill(it.target)}
              className="flex w-full items-baseline gap-3 border-b border-[color:var(--rule-soft)] py-3 text-left transition-colors hover:text-cinnabar"
            >
              <span className="font-serif text-[0.98rem]">{it.label}</span>
              <span className="ml-auto text-cinnabar">›</span>
            </button>
          </li>
        ))}
      </ul>
    );
  if (cur?.mode === "search")
    return (
      <SearchBody
        key={`s:${cur.wiki}:${cur.term}`}
        wiki={cur.wiki}
        term={cur.term}
        onPick={(hit) =>
          onDrill({
            mode: "doc",
            wiki: cur.wiki,
            kind: "page",
            path: hit.path,
            title: hit.title,
          })
        }
      />
    );
  if (cur)
    return (
      <DocBody
        key={`d:${cur.wiki}:${cur.kind}:${cur.path}:${cur.sourcePage || ""}`}
        wiki={cur.wiki}
        kind={cur.kind}
        path={cur.path}
        sourcePage={cur.sourcePage}
      />
    );
  return null;
}

export function DocBody({
  wiki,
  kind,
  path,
  sourcePage,
}: {
  wiki: string;
  kind: "page" | "raw";
  path: string;
  sourcePage?: string;
}) {
  const [page, setPage] = useState<Page | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    setPage(null);
    setErr("");
    const request =
      kind === "raw"
        ? sourcePage
          ? getSourcePreview(wiki, sourcePage, path)
          : getRaw(wiki, path)
        : getPage(wiki, path);
    request
      .then(setPage)
      .catch(() => setErr("无法载入该页"));
  }, [wiki, kind, path, sourcePage]);

  if (err) return <p className="font-serif text-sm text-ink-faint">{err}</p>;
  if (!page)
    return <p className="font-mono text-sm text-ink-faint">载入…</p>;

  const fm = page.frontmatter;
  const sourceUrl = typeof fm.source_url === "string" ? fm.source_url : "";
  const author = typeof fm.author === "string" ? fm.author : "";
  const publishTime =
    typeof fm.publish_time === "string" ? fm.publish_time : "";

  return (
    <article className="reveal">
      <div className="eyebrow mb-2 text-ink-faint">{typeLabel(page.type)}</div>
      <h1 className="mb-5 font-display text-2xl font-semibold leading-tight">
        {page.title}
      </h1>
      {kind === "raw" && sourceUrl && (
        <div className="mb-6 flex flex-wrap items-baseline gap-x-4 gap-y-1 border-l-2 border-cinnabar bg-paper-2/60 px-4 py-3 font-serif text-sm text-ink-soft">
          {author && <span className="text-ink">{author}</span>}
          {publishTime && (
            <span className="tnum text-ink-faint">{publishTime}</span>
          )}
          <a
            href={sourceUrl}
            target="_blank"
            rel="noreferrer"
            className="ml-auto font-mono text-xs text-cinnabar underline decoration-cinnabar/40 underline-offset-4 transition-colors hover:decoration-cinnabar"
            title={sourceUrl}
          >
            查看原文 →
          </a>
        </div>
      )}
      <Markdown content={stripTitleH1(page.body, page.title)} />
    </article>
  );
}

export function SearchBody({
  wiki,
  term,
  onPick,
}: {
  wiki: string;
  term: string;
  onPick: (hit: SearchHit) => void;
}) {
  const [hits, setHits] = useState<SearchHit[] | null>(null);

  useEffect(() => {
    setHits(null);
    search(wiki, term)
      .then((r) => setHits(r.hits))
      .catch(() => setHits([]));
  }, [wiki, term]);

  if (!hits) return <p className="font-mono text-sm text-ink-faint">检索中…</p>;
  if (hits.length === 0)
    return (
      <p className="font-serif text-sm italic text-ink-faint">
        无与「{term}」相关的页面。
      </p>
    );

  return (
    <div className="reveal space-y-5">
      {groupSearchHits(hits).map((group) => (
        <section key={group.type}>
          <div className="eyebrow mb-1 flex items-center gap-2 text-brass">
            <span>{group.label}</span>
            <span className="ml-auto tnum text-ink-faint/70">
              {group.hits.length} 条
            </span>
          </div>
          <ul>
            {group.hits.map((h) => (
              <li key={h.path}>
                <button
                  onClick={() => onPick(h)}
                  className="index-row w-full text-left"
                >
                  <span className="font-display text-base">{h.title}</span>
                  <span className="index-leader" />
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
