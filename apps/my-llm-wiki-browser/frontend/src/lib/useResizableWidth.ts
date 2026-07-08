import { useRef, useState } from "react";

const clamp = (n: number, lo: number, hi: number) => Math.min(Math.max(n, lo), hi);

// 左锚定列的可拖拽宽度（手柄在右边缘）。Pointer 事件同时覆盖触屏与鼠标，宽度记忆到 localStorage。
export function useResizableWidth<T extends HTMLElement>(
  key: string,
  initial: number,
  min: number,
  max: number,
) {
  const [width, setWidth] = useState<number>(() => {
    try {
      const saved = Number(localStorage.getItem(key));
      if (saved >= min && saved <= max) return saved;
    } catch {
      // 忽略
    }
    return initial;
  });
  const ref = useRef<T>(null);

  function startResize(e: React.PointerEvent) {
    e.preventDefault();
    // 列左边缘在本次拖拽中保持不动（左锚定），起手时取一次即可。
    const left = ref.current?.getBoundingClientRect().left ?? 0;
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    const onMove = (ev: PointerEvent) => {
      const hi = Math.min(max, window.innerWidth - 240); // 始终给其余内容留 ≥240px
      setWidth(clamp(ev.clientX - left, min, hi));
    };
    const onUp = () => {
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      setWidth((w) => {
        try {
          localStorage.setItem(key, String(Math.round(w)));
        } catch {
          // 忽略
        }
        return w;
      });
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  return { ref, width, startResize };
}
