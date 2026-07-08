import { useEffect, useState } from "react";

// 正文字号（rem），作用于 .prose-wiki（阅读区与抽屉共享）。
const DEFAULT = 1.06;
const MIN = 0.85;
const MAX = 1.6;
const STEP = 0.05;
const KEY = "proseSize";

const EVENT = "prosesizechange";
const clamp = (n: number) => Math.min(Math.max(n, MIN), MAX);

function read(): number {
  const s = Number(localStorage.getItem(KEY));
  return s >= MIN && s <= MAX ? s : DEFAULT;
}

function applySize(size: number) {
  document.documentElement.style.setProperty("--prose-size", `${size}rem`);
  localStorage.setItem(KEY, String(size));
  // 广播给其它控件实例（如阅读区与抽屉同时存在时保持同步）
  window.dispatchEvent(new CustomEvent<number>(EVENT, { detail: size }));
}

// 应用上次保存的字号（在应用挂载时调用一次）。
export function initProseSize() {
  const s = Number(localStorage.getItem(KEY));
  if (s >= MIN && s <= MAX)
    document.documentElement.style.setProperty("--prose-size", `${s}rem`);
}

// 阅读区头部的字号调节：A−／重置／A＋。
export default function FontSizeControl() {
  const [size, setSize] = useState(read);

  // 同步其它实例的改动
  useEffect(() => {
    const onChange = (e: Event) =>
      setSize((e as CustomEvent<number>).detail);
    window.addEventListener(EVENT, onChange);
    return () => window.removeEventListener(EVENT, onChange);
  }, []);

  function set(n: number) {
    const c = clamp(Number(n.toFixed(2)));
    setSize(c);
    applySize(c);
  }

  const pct = Math.round((size / DEFAULT) * 100);

  return (
    <div className="flex items-center gap-1 font-mono text-ink-faint sm:gap-1.5">
      <button
        onClick={() => set(size - STEP)}
        disabled={size <= MIN + 1e-6}
        title="缩小正文字号"
        className="flex h-9 min-w-9 items-center justify-center rounded text-base transition-colors active:text-cinnabar disabled:opacity-30 sm:h-auto sm:min-w-0 sm:px-1 sm:text-sm sm:hover:text-cinnabar"
      >
        A−
      </button>
      <button
        onClick={() => set(DEFAULT)}
        title={`重置字号（当前 ${pct}%）`}
        className="tnum flex h-9 min-w-10 items-center justify-center rounded text-[0.78rem] tabular-nums transition-colors active:text-cinnabar sm:h-auto sm:min-w-0 sm:text-[0.66rem] sm:hover:text-cinnabar"
      >
        {pct}%
      </button>
      <button
        onClick={() => set(size + STEP)}
        disabled={size >= MAX - 1e-6}
        title="放大正文字号"
        className="flex h-9 min-w-9 items-center justify-center rounded text-lg transition-colors active:text-cinnabar disabled:opacity-30 sm:h-auto sm:min-w-0 sm:px-1 sm:text-base sm:hover:text-cinnabar"
      >
        A＋
      </button>
    </div>
  );
}
