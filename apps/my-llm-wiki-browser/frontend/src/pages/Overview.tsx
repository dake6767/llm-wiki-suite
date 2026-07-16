import { useEffect, useState } from "react";
import { useOutletContext, useParams } from "react-router-dom";
import {
  getPage,
  getTree,
  type LayoutContext,
  type Page,
} from "../api/client";
import Markdown from "../lib/Markdown";
import FirstCaptureGuide from "../components/FirstCaptureGuide";

// 阅读区落地页：本卷报头 + overview 概览。类目浏览已由中栏 PageList 承担。
export default function Overview() {
  const { wiki } = useParams();
  const { current } = useOutletContext<LayoutContext>();
  const [overview, setOverview] = useState<Page | null>(null);
  const [total, setTotal] = useState(0);

  useEffect(() => {
    if (!wiki) return;
    let cancelled = false;
    const load = () => {
      getPage(wiki, "overview")
        .then((page) => {
          if (!cancelled) setOverview(page);
        })
        .catch(() => {
          if (!cancelled) setOverview(null);
        });
      getTree(wiki)
        .then((t) => {
          if (!cancelled) setTotal(t.reduce((s, n) => s + n.count, 0));
        })
        .catch(() => {
          if (!cancelled) setTotal(0);
        });
    };
    setOverview(null);
    load();
    return () => {
      cancelled = true;
    };
  }, [wiki]);

  if (!wiki)
    return (
      <div className="p-16 font-serif text-ink-faint">请自左侧选择一卷知识库。</div>
    );

  return (
    <div className="reveal mx-auto max-w-3xl px-10 py-14">
      <header className="mb-12">
        <div className="eyebrow mb-4 flex items-center gap-3">
          <span>Collection</span>
          <span className="h-px w-8 bg-[color:var(--rule)]" />
          <span className="tnum">{String(total).padStart(3, "0")} 篇</span>
        </div>
        <h1 className="font-display text-5xl font-semibold leading-[1.05] tracking-tight">
          {current?.name || wiki}
        </h1>
        {current?.description && (
          <p className="mt-4 max-w-xl font-serif text-lg italic leading-relaxed text-ink-soft">
            {current.description}
          </p>
        )}
      </header>

      {/* 空库首次引导：只在编译层无内容页时出现，纯前端叠加，不写 wiki 文件 */}
      <FirstCaptureGuide wiki={wiki} />

      {overview ? (
        <article>
          <div className="eyebrow mb-4 flex items-center gap-3">
            <span>序 · Overview</span>
            <span className="h-px flex-1 bg-[color:var(--rule)]" />
          </div>
          <Markdown content={overview.body} />
        </article>
      ) : (
        <p className="font-serif text-sm italic text-ink-faint">
          本卷暂无 overview 概览页。自中栏选择一篇开始阅读。
        </p>
      )}
    </div>
  );
}
