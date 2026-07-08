// 浮标：来源/相关/引用三枚圆钮，带数量角标；某类为空则不显示该钮，全为空则整组隐藏。
export type FabItem = {
  key: string;
  glyph: string;
  label: string;
  count: number;
  onClick: () => void;
};

export default function MarginaliaFab({ items }: { items: FabItem[] }) {
  const shown = items.filter((i) => i.count > 0);
  if (shown.length === 0) return null;

  return (
    <div className="fixed bottom-5 right-4 z-30 flex flex-col items-center gap-3 sm:bottom-auto sm:top-[35vh]">
      {shown.map((it) => (
        <button
          key={it.key}
          onClick={it.onClick}
          aria-label={`${it.label}（${it.count}）`}
          className="relative flex h-12 w-12 items-center justify-center rounded-full border border-[color:var(--rule)] bg-paper/65 shadow-lg backdrop-blur-md transition-transform active:scale-95"
        >
          <span className="font-display text-lg text-ink">{it.glyph}</span>
          <span className="absolute -right-1 -top-1 flex h-[1.05rem] min-w-[1.05rem] items-center justify-center rounded-full bg-cinnabar px-1 font-mono text-[0.6rem] leading-none text-paper">
            {it.count > 99 ? "99+" : it.count}
          </span>
        </button>
      ))}
    </div>
  );
}
