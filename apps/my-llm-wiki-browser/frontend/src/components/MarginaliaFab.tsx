import type { ReactNode } from "react";

// 浮标：来源/相关/引用三枚圆钮，带数量角标；某类为空则不显示该钮，全为空则整组隐藏。
export type FabItem = {
  key: string;
  glyph?: string;
  icon?: ReactNode;
  label: string;
  count: number;
  always?: boolean;
  hint?: string;
  onClick: () => void;
  secondaryAction?: {
    label: string;
    onClick: () => void;
  };
};

// 三节点分享图标：表达“把整个 Wiki 的阅读能力分享出去”，避免再用单字缩写。
export function ShareWikiIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className="h-5 w-5 text-ink"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="18" cy="5" r="2.5" />
      <circle cx="6" cy="12" r="2.5" />
      <circle cx="18" cy="19" r="2.5" />
      <path d="m8.2 10.8 7.6-4.5M8.2 13.2l7.6 4.5" />
    </svg>
  );
}

export default function MarginaliaFab({ items }: { items: FabItem[] }) {
  const shown = items.filter((i) => i.always || i.count > 0);
  if (shown.length === 0) return null;

  return (
    <div className="fixed bottom-5 right-4 z-30 flex flex-col items-center gap-3 sm:bottom-auto sm:top-[35vh]">
      {shown.map((it) => (
        <div key={it.key} className="relative">
          <button
            onClick={it.onClick}
            aria-label={it.count > 0 ? `${it.label}（${it.count}）` : it.label}
            title={it.label}
            className="relative flex h-12 w-12 items-center justify-center rounded-full border border-[color:var(--rule)] bg-paper/65 shadow-lg backdrop-blur-md transition-transform active:scale-95"
          >
            {it.icon ?? (
              <span className="font-display text-lg text-ink">{it.glyph}</span>
            )}
            {it.hint && (
              <span
                role="status"
                className="pointer-events-none absolute right-full mr-3 whitespace-nowrap rounded-sm border border-[color:var(--rule)] bg-paper px-3 py-1.5 font-serif text-xs text-ink shadow-lg"
              >
                {it.hint}
              </span>
            )}
            {it.count > 0 && (
              <span className="absolute -right-1 -top-1 flex h-[1.05rem] min-w-[1.05rem] items-center justify-center rounded-full bg-cinnabar px-1 font-mono text-[0.6rem] leading-none text-paper">
                {it.count > 99 ? "99+" : it.count}
              </span>
            )}
          </button>
          {it.secondaryAction && (
            <button
              onClick={it.secondaryAction.onClick}
              aria-label={it.secondaryAction.label}
              title={it.secondaryAction.label}
              className="absolute -bottom-1.5 -right-1.5 flex h-5 w-5 items-center justify-center rounded-full border border-[color:var(--rule)] bg-paper font-mono text-sm leading-none text-cinnabar shadow-md transition-colors hover:bg-cinnabar hover:text-paper"
            >
              +
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
