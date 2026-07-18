import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { AuthError, getReview, type ReviewItem } from "../api/client";
import {
  copyToClipboard,
  isReviewOpen,
  reviewItemTime,
} from "../lib/reviewPrompt";
import ReviewItemCard from "../components/ReviewItemCard";

// 阅读区的「待审 · Review」视图：展示该卷的 review 队列。浏览器不能直接跑 review，
// 因此每条待办都提供「复制提示词」，把待办拼成可交给 agent 的指令。
// dense=true 为移动端单列布局（更紧凑的边距/字号），逻辑与桌面完全一致。
export default function ReviewQueue({ dense = false }: { dense?: boolean }) {
  const { wiki } = useParams();
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [root, setRoot] = useState("");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  // 复制反馈：记住刚复制条目的 key，短暂高亮。
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    if (!wiki) return;
    let cancelled = false;
    setLoading(true);
    setErr("");
    getReview(wiki)
      .then((res) => {
        if (cancelled) return;
        setItems(res.items);
        setRoot(res.root);
      })
      .catch((e) => {
        if (cancelled) return;
        setErr(e instanceof AuthError ? "未授权，请重新登录。" : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [wiki]);

  async function copy(key: string, text: string) {
    const ok = await copyToClipboard(text);
    if (ok) {
      setCopied(key);
      window.setTimeout(() => setCopied((c) => (c === key ? null : c)), 1800);
    } else {
      setErr("复制失败，请手动选择文本复制。");
    }
  }

  if (!wiki)
    return (
      <div className="p-16 font-serif text-ink-faint">请自左侧选择一卷知识库。</div>
    );

  // 待办在前、已处理在后；组内按时间倒序（最新在前）。
  const ordered = [...items].sort((a, b) => {
    const openDelta = Number(isReviewOpen(b)) - Number(isReviewOpen(a));
    if (openDelta !== 0) return openDelta;
    return reviewItemTime(b) - reviewItemTime(a);
  });
  const openCount = items.filter(isReviewOpen).length;

  return (
    <div
      className={
        dense
          ? "h-full overflow-y-auto px-4 py-6"
          : "reveal mx-auto max-w-3xl px-10 py-14"
      }
    >
      <header className="mb-8">
        <div className="eyebrow mb-4 flex items-center gap-3">
          <span>Review Queue</span>
          <span className="h-px w-8 bg-[color:var(--rule)]" />
          <span className="tnum">
            {String(openCount).padStart(2, "0")} 待办 / {String(items.length).padStart(2, "0")} 全部
          </span>
        </div>
        <h1
          className={`font-display font-semibold leading-[1.1] tracking-tight ${
            dense ? "text-2xl" : "text-4xl"
          }`}
        >
          待审队列
        </h1>
        <p className="mt-4 max-w-xl font-serif text-sm leading-relaxed text-ink-soft">
          这些是维护 agent 沉淀下来的可深挖方向与待办。浏览器内无法直接发起 research，
          点「复制提示词」把待办拼成指令，粘贴给带 <code className="font-mono text-xs">my-llm-wiki-maintainer</code> skill 的 agent 执行即可。
        </p>
      </header>

      {loading ? (
        <p className="font-serif text-sm italic text-ink-faint">载入中…</p>
      ) : err ? (
        <p className="rounded border border-cinnabar/30 bg-cinnabar/5 px-4 py-3 font-mono text-sm text-cinnabar-deep">
          {err}
        </p>
      ) : items.length === 0 ? (
        <p className="font-serif text-sm italic text-ink-faint">
          本卷暂无 review 待办。ingest / update 新源后会在此沉淀可深挖方向。
        </p>
      ) : (
        <ul className="space-y-6">
          {ordered.map((item, i) => {
            const open = isReviewOpen(item);
            const key = String(item.id ?? `${i}-${item.title ?? ""}`);
            return (
              <li
                key={key}
                className={`border-t border-[color:var(--rule)] pt-5 ${
                  open ? "" : "opacity-55"
                }`}
              >
                <ReviewItemCard
                  item={item}
                  root={root}
                  cardKey={key}
                  copiedKey={copied}
                  onCopy={copy}
                />
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
