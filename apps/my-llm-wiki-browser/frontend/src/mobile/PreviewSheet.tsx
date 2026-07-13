import { useEffect } from "react";
import { useShareNavigate } from "../lib/shareNavigation";
import FontSizeControl from "../components/FontSizeControl";
import {
  PreviewBody,
  previewEyebrow,
  previewTitle,
  usePreview,
  type SheetTarget,
} from "../components/PreviewBodies";
import { isGuest } from "../lib/shareSession";

export type { SheetTarget };

// 移动端底部上滑预览：FAB 打开后先列类目条目，点条目下钻到半屏预览（可返回列表）。
export default function PreviewSheet({
  target,
  onClose,
}: {
  target: SheetTarget | null;
  onClose: () => void;
}) {
  const navigate = useShareNavigate();
  const guest = isGuest();
  const { open, root, cur, setCur, drilled } = usePreview(target);
  const canMaximize =
    cur &&
    cur.mode !== "list" &&
    !(guest && cur.mode === "doc" && cur.kind === "raw");

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  function maximize() {
    if (!cur || cur.mode === "list") return;
    if (cur.mode === "search")
      navigate(
        `/w/${cur.wiki}/search?q=${encodeURIComponent(cur.term)}&keepList=1`,
      );
    else
      navigate(
        `/w/${cur.wiki}/${cur.kind}/${cur.path}${
          cur.kind === "raw" ? "?focus=1" : ""
        }`,
      );
    onClose();
  }

  return (
    <>
      {/* 背板 */}
      <div
        onClick={onClose}
        className={`fixed inset-0 z-40 bg-spine/40 transition-opacity duration-300 ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      {/* 底部 sheet */}
      <section
        className={`fixed inset-x-0 bottom-0 z-50 flex max-h-[88vh] flex-col rounded-t-2xl border-t border-[color:var(--rule)] bg-paper shadow-2xl transition-transform duration-300 ease-[cubic-bezier(0.2,0.7,0.2,1)] ${
          open ? "translate-y-0" : "translate-y-full"
        }`}
      >
        {/* 抓手 */}
        <button
          onClick={onClose}
          aria-label="收起预览"
          className="mx-auto mt-2.5 h-1.5 w-10 shrink-0 rounded-full bg-ink-faint/30"
        />
        {/* 头部 */}
        <div className="flex shrink-0 items-center gap-3 border-b border-[color:var(--rule)] px-5 py-3">
          {drilled && (
            <button
              onClick={() => setCur(root)}
              aria-label="返回列表"
              className="-ml-1 text-lg text-ink transition-colors active:text-cinnabar"
            >
              ‹
            </button>
          )}
          <div className="eyebrow text-cinnabar">{previewEyebrow(cur)}</div>
          <h2 className="min-w-0 flex-1 truncate font-display text-base font-semibold">
            {previewTitle(cur)}
          </h2>
          {cur?.mode === "doc" && <FontSizeControl />}
          {canMaximize && (
            <button
              onClick={maximize}
              title="最大化：跳转到该页"
              className="font-mono text-xs text-ink-soft transition-colors active:text-cinnabar"
            >
              ⤢
            </button>
          )}
          <button
            onClick={onClose}
            aria-label="关闭"
            className="font-mono text-sm text-ink-faint transition-colors active:text-cinnabar"
          >
            ✕
          </button>
        </div>

        {/* 内容 */}
        <div
          className="min-h-0 flex-1 overflow-y-auto px-5 py-5"
          onClickCapture={(e) => {
            const a = (e.target as HTMLElement).closest("a");
            if (a && new URL(a.href, location.origin).pathname.includes("/w/")) onClose();
          }}
        >
          <PreviewBody cur={cur} onDrill={setCur} />
        </div>
      </section>
    </>
  );
}
