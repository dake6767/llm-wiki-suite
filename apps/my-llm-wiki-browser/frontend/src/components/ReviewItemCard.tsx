import type { ReviewItem } from "../api/client";
import {
  isReviewOpen,
  reviewItemBody,
  reviewItemSource,
  reviewTypeLabel,
  singleItemPrompt,
} from "../lib/reviewPrompt";

// 单条 review 项的展示卡片内容（不含外层容器/边距，由调用方决定）。
// 待审队列与 source 页的「挖」抽屉共用，保证两处样式与复制交互一致。
export default function ReviewItemCard({
  item,
  root,
  cardKey,
  copiedKey,
  onCopy,
}: {
  item: ReviewItem;
  root: string;
  cardKey: string;
  copiedKey: string | null;
  onCopy: (key: string, text: string) => void;
}) {
  const open = isReviewOpen(item);
  const body = reviewItemBody(item);
  const source = reviewItemSource(item);
  const queries = item.searchQueries || [];
  return (
    <>
      <div className="mb-2 flex items-center gap-2">
        <span className="rounded-sm bg-paper-3 px-2 py-0.5 font-mono text-[0.7rem] uppercase tracking-wide text-ink-soft">
          {reviewTypeLabel(item.type)}
        </span>
        {!open && (
          <span className="font-mono text-[0.7rem] text-ink-faint">已处理</span>
        )}
        {(item.created || item.createdAt) && (
          <span className="ml-auto font-mono text-[0.7rem] text-ink-faint">
            {item.created ||
              (item.createdAt
                ? new Date(item.createdAt).toISOString().slice(0, 10)
                : "")}
          </span>
        )}
      </div>
      <h2 className="font-display text-xl font-semibold leading-snug">
        {item.title || "(无标题)"}
      </h2>
      {body && (
        <p className="mt-2 font-serif text-[0.95rem] leading-relaxed text-ink-soft">
          {body}
        </p>
      )}
      {queries.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {queries.map((q, qi) => (
            <span
              key={qi}
              className="rounded-full border border-[color:var(--rule)] px-2.5 py-0.5 font-mono text-xs text-ink-faint"
            >
              {q}
            </span>
          ))}
        </div>
      )}
      {source && (
        <p className="mt-3 font-mono text-xs text-ink-faint">来源：{source}</p>
      )}
      <button
        onClick={() => onCopy(cardKey, singleItemPrompt(root, item))}
        className="mt-4 inline-flex items-center gap-2 rounded border border-cinnabar/40 bg-cinnabar/5 px-3 py-1.5 font-mono text-xs text-cinnabar-deep transition-colors hover:bg-cinnabar/10"
      >
        {copiedKey === cardKey ? "已复制 ✓" : "复制提示词"}
      </button>
    </>
  );
}
