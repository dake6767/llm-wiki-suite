import { useEffect, useRef, useState } from "react";
import { useShareNavigate } from "../lib/shareNavigation";
import FontSizeControl from "./FontSizeControl";
import {
  PreviewBody,
  previewEyebrow,
  previewTitle,
  usePreview,
  type DrawerTarget,
  type SheetTarget,
} from "./PreviewBodies";
import { isGuest } from "../lib/shareSession";

export type { DrawerTarget, SheetTarget };

export default function ReferenceDrawer({
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

  // 可拖拽宽度（记忆上次设置）。
  const [width, setWidth] = useState<number>(() => {
    const saved = Number(localStorage.getItem("drawerWidth"));
    return saved >= 360 ? saved : 576;
  });
  const dragging = useRef(false);

  function startResize(e: React.PointerEvent) {
    e.preventDefault();
    dragging.current = true;
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    const onMove = (ev: PointerEvent) => {
      if (!dragging.current) return;
      const w = Math.min(
        Math.max(window.innerWidth - ev.clientX, 360),
        window.innerWidth * 0.95,
      );
      setWidth(w);
    };
    const onUp = () => {
      dragging.current = false;
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      setWidth((w) => {
        localStorage.setItem("drawerWidth", String(Math.round(w)));
        return w;
      });
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  // Esc 关闭
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
        className={`fixed inset-0 z-40 bg-spine/30 transition-opacity duration-300 ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      {/* 抽屉 */}
      <aside
        style={{ width, maxWidth: "95vw" }}
        className={`fixed right-0 top-0 z-50 flex h-full flex-col border-l border-[color:var(--rule)] bg-paper shadow-2xl transition-transform duration-300 ease-[cubic-bezier(0.2,0.7,0.2,1)] ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
      >
        {/* 左边缘拖拽手柄：拖动调宽 */}
        <div
          onPointerDown={startResize}
          title="拖动调整宽度"
          className="group absolute left-0 top-0 z-10 h-full w-2 -translate-x-1/2 cursor-col-resize"
        >
          <div className="mx-auto h-full w-px bg-[color:var(--rule)] transition-colors group-hover:w-0.5 group-hover:bg-cinnabar" />
        </div>

        {/* 头部 */}
        <div className="flex shrink-0 items-center gap-3 border-b border-[color:var(--rule)] px-6 py-4">
          {drilled && (
            <button
              onClick={() => setCur(root)}
              title="返回列表"
              className="-ml-1 text-lg text-ink transition-colors hover:text-cinnabar"
            >
              ‹
            </button>
          )}
          <div className="eyebrow text-cinnabar">{previewEyebrow(cur)}</div>
          <h2 className="min-w-0 flex-1 truncate font-display text-lg font-semibold">
            {previewTitle(cur)}
          </h2>
          {cur?.mode === "doc" && <FontSizeControl />}
          {canMaximize && (
            <button
              onClick={maximize}
              title="最大化：跳转到该页"
              className="font-mono text-xs text-ink-soft transition-colors hover:text-cinnabar"
            >
              最大化 ⤢
            </button>
          )}
          <button
            onClick={onClose}
            title="关闭 (Esc)"
            className="font-mono text-sm text-ink-faint transition-colors hover:text-cinnabar"
          >
            ✕
          </button>
        </div>

        {/* 内容 */}
        <div
          className="min-h-0 flex-1 overflow-y-auto px-6 py-6"
          onClickCapture={(e) => {
            // 抽屉正文里的内部链接：放行导航，同时关闭抽屉。
            const a = (e.target as HTMLElement).closest("a");
            if (a && new URL(a.href, location.origin).pathname.includes("/w/")) onClose();
          }}
        >
          <PreviewBody cur={cur} onDrill={setCur} />
        </div>
      </aside>
    </>
  );
}
