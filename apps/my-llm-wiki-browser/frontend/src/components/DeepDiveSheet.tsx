import { useEffect, useState } from "react";
import type { ReviewItem } from "../api/client";
import { copyToClipboard } from "../lib/reviewPrompt";
import ReviewItemCard from "./ReviewItemCard";

// 「挖」浮标弹出的深挖方向面板。窄屏底部上滑、宽屏右侧抽屉（同一组件响应式切换）。
// 内容是与当前 source 相关的 review 待办，每条可复制交给 agent 的提示词。
export default function DeepDiveSheet({
  open,
  items,
  root,
  onClose,
}: {
  open: boolean;
  items: ReviewItem[];
  root: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (open) setCopied(null);
  }, [open]);

  async function onCopy(key: string, text: string) {
    const ok = await copyToClipboard(text);
    if (ok) {
      setCopied(key);
      window.setTimeout(() => setCopied((c) => (c === key ? null : c)), 1800);
    }
  }

  return (
    <>
      {/* 背板 */}
      <div
        onClick={onClose}
        className={`fixed inset-0 z-40 bg-spine/30 transition-opacity duration-300 ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      {/* 面板：窄屏底部上滑，sm+ 右侧抽屉 */}
      <aside
        className={`fixed z-50 flex flex-col bg-paper shadow-2xl transition-transform duration-300 ease-[cubic-bezier(0.2,0.7,0.2,1)] inset-x-0 bottom-0 max-h-[82vh] rounded-t-2xl border-t border-[color:var(--rule)] sm:inset-y-0 sm:right-0 sm:bottom-auto sm:left-auto sm:h-full sm:max-h-none sm:w-[32rem] sm:max-w-[92vw] sm:rounded-t-none sm:border-t-0 sm:border-l ${
          open
            ? "translate-y-0 sm:translate-x-0"
            : "translate-y-full sm:translate-y-0 sm:translate-x-full"
        }`}
      >
        {/* 头部 */}
        <div className="flex shrink-0 items-center gap-3 border-b border-[color:var(--rule)] px-6 py-4">
          <div className="eyebrow text-cinnabar">可深挖 · Deep Dive</div>
          <span className="tnum font-mono text-xs text-ink-faint">
            {items.length} 个方向
          </span>
          <button
            onClick={onClose}
            title="关闭 (Esc)"
            className="ml-auto font-mono text-sm text-ink-faint transition-colors hover:text-cinnabar"
          >
            ✕
          </button>
        </div>

        {/* 内容 */}
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          <p className="mb-5 font-serif text-sm leading-relaxed text-ink-soft">
            维护 agent 从这条源里读出的可深挖方向。浏览器内不能直接发起 research，
            点「复制提示词」把方向拼成指令，交给带{" "}
            <code className="font-mono text-xs">my-llm-wiki-maintainer</code>{" "}
            skill 的 agent 执行。
          </p>
          {items.length === 0 ? (
            <p className="font-serif text-sm italic text-ink-faint">
              这条源暂无待深挖的方向。
            </p>
          ) : (
            <ul className="space-y-6">
              {items.map((item, i) => {
                const key = String(item.id ?? `${i}-${item.title ?? ""}`);
                return (
                  <li
                    key={key}
                    className="border-t border-[color:var(--rule)] pt-5 first:border-t-0 first:pt-0"
                  >
                    <ReviewItemCard
                      item={item}
                      root={root}
                      cardKey={key}
                      copiedKey={copied}
                      onCopy={onCopy}
                    />
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </aside>
    </>
  );
}
