import { useEffect, useState } from "react";
import { useOutletContext, useParams } from "react-router-dom";
import {
  AuthError,
  getPage,
  getRaw,
  stripTitleH1,
  typeLabel,
  type LayoutContext,
  type Page,
} from "../api/client";
import Markdown from "../lib/Markdown";
import PreviewSheet, { type SheetTarget } from "./PreviewSheet";
import MarginaliaFab, { ShareWikiIcon } from "../components/MarginaliaFab";
import DeepDiveSheet from "../components/DeepDiveSheet";
import { useDeepDive } from "../lib/useDeepDive";
import { ShareLink } from "../lib/shareNavigation";
import SharePanel from "../components/SharePanel";
import { copyToClipboard } from "../lib/reviewPrompt";
import { isGuest, shareEntryLink } from "../lib/shareSession";

function rawPathFromSource(src: string): string {
  let s = src.trim();
  if (s.endsWith(".md")) s = s.slice(0, -3);
  if (s.startsWith("raw/")) s = s.slice(4);
  if (!s.startsWith("sources/") && !s.startsWith("assets/")) s = `sources/${s}`;
  return s;
}

export default function MobileReader({ kind }: { kind: "page" | "raw" }) {
  const params = useParams();
  const wiki = params.wiki!;
  const path = params["*"] || "";
  const { wikis } = useOutletContext<LayoutContext>();
  const guest = isGuest();
  const [page, setPage] = useState<Page | null>(null);
  const [err, setErr] = useState("");
  const [sheet, setSheet] = useState<SheetTarget | null>(null);
  const [deepDive, setDeepDive] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [shareFeedback, setShareFeedback] = useState<"copied" | "failed" | null>(null);
  const dive = useDeepDive(wiki, page);

  // 深挖面板与预览浮层互斥：开一个即关另一个。
  const openSheet = (t: SheetTarget) => {
    setDeepDive(false);
    setSheet(t);
  };

  async function onShare() {
    if (!guest) {
      setShareOpen(true);
      return;
    }
    const link = shareEntryLink();
    const feedback = link && (await copyToClipboard(link)) ? "copied" : "failed";
    setShareFeedback(feedback);
    window.setTimeout(
      () => setShareFeedback((current) => (current === feedback ? null : current)),
      1600,
    );
  }

  useEffect(() => {
    setPage(null);
    setErr("");
    setSheet(null);
    const fn = kind === "raw" ? getRaw : getPage;
    fn(wiki, path)
      .then(setPage)
      .catch((e) => setErr(e instanceof AuthError ? "未授权" : "页面不存在"));
  }, [wiki, path, kind]);

  if (err) return <div className="p-10 font-serif text-ink-faint">{err}</div>;
  if (!page)
    return <div className="p-10 font-mono text-sm text-ink-faint">载入手稿…</div>;

  const fm = page.frontmatter;
  const related = Array.isArray(fm.related) ? (fm.related as string[]) : [];
  const sourceUrl = typeof fm.source_url === "string" ? fm.source_url : "";
  const author = typeof fm.author === "string" ? fm.author : "";
  const publishTime =
    typeof fm.publish_time === "string" ? fm.publish_time : "";

  return (
    <div className="h-full overflow-y-auto">
      <article className="reveal mx-auto max-w-2xl px-5 py-8">
        <header className="mb-6">
          <div className="eyebrow mb-2 flex flex-wrap items-center gap-2">
            <span className="text-cinnabar">
              {kind === "raw" ? "原始源 · Provenance" : typeLabel(page.type)}
            </span>
            {fm.created ? (
              <>
                <span className="text-ink-faint/50">·</span>
                <span className="tnum">创建 {String(fm.created)}</span>
              </>
            ) : null}
          </div>
          <h1 className="font-display text-3xl font-semibold leading-[1.15] tracking-tight">
            {page.title}
          </h1>
          {page.tags.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1">
              {page.tags.map((t) => (
                <ShareLink
                  key={t}
                  to={`/w/${wiki}/search?tag=${encodeURIComponent(t)}`}
                  className="font-mono text-xs uppercase tracking-wide text-brass transition-colors active:text-cinnabar"
                >
                  #{t}
                </ShareLink>
              ))}
            </div>
          )}
        </header>

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
              className="ml-auto font-mono text-xs text-cinnabar underline decoration-cinnabar/40 underline-offset-4"
              title={sourceUrl}
            >
              查看原文 →
            </a>
          </div>
        )}

        <Markdown content={stripTitleH1(page.body, page.title)} />
      </article>

      {/* 右下角浮标：点开从底部上滑该类目列表，再下钻半屏预览 */}
      <MarginaliaFab
        items={[
          {
            key: "sources",
            glyph: "源",
            label: "来源",
            count: page.sources.length,
            onClick: () =>
              openSheet({
                mode: "list",
                title: `来源 · Sources (${page.sources.length})`,
                items: page.sources.map((s) => ({
                  key: s,
                  label: s.split("/").pop()?.replace(/\.md$/, "") || s,
                  target: {
                    mode: "doc",
                    wiki,
                    kind: "raw",
                    path: rawPathFromSource(s),
                    sourcePage: path,
                  },
                })),
              }),
          },
          {
            key: "related",
            glyph: "联",
            label: "相关",
            count: related.length,
            onClick: () =>
              openSheet({
                mode: "list",
                title: `相关 · Related (${related.length})`,
                items: related.map((r) => ({
                  key: r,
                  label: r,
                  target: { mode: "search", wiki, term: r },
                })),
              }),
          },
          {
            key: "backlinks",
            glyph: "引",
            label: "引用",
            count: page.backlinks.length,
            onClick: () =>
              openSheet({
                mode: "list",
                title: `引用 · Backlinks (${page.backlinks.length})`,
                items: page.backlinks.map((b) => ({
                  key: b.path,
                  label: b.title,
                  target: {
                    mode: "doc",
                    wiki,
                    kind: "page",
                    path: b.path,
                    title: b.title,
                  },
                })),
              }),
          },
          {
            key: "deep-dive",
            glyph: "挖",
            label: "可深挖",
            count: dive.items.length,
            onClick: () => {
              setSheet(null);
              setDeepDive(true);
            },
          },
          {
            key: "share",
            icon: <ShareWikiIcon />,
            label: guest ? "复制当前分享链接" : "分享整个 Wiki",
            count: 0,
            always: kind === "page",
            hint: guest
              ? shareFeedback === "copied"
                ? "分享链接已复制"
                : shareFeedback === "failed"
                  ? "复制失败，请重试"
                  : undefined
              : undefined,
            onClick: onShare,
          },
        ]}
      />

      <PreviewSheet target={sheet} onClose={() => setSheet(null)} />
      <DeepDiveSheet
        open={deepDive}
        items={dive.items}
        root={dive.root}
        onClose={() => setDeepDive(false)}
      />
      {!guest && (
        <SharePanel
          open={shareOpen}
          onClose={() => setShareOpen(false)}
          wikiOptions={wikis}
          lockedWiki={wiki}
          deepLinkPath={`/w/${wiki}/page/${path}`}
        />
      )}
    </div>
  );
}
