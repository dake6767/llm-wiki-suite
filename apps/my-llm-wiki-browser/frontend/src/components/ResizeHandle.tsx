// 列右边缘的拖拽手柄。touch-none 防止拖动时页面跟着滚动；dark 用于深色书脊侧栏上。
export default function ResizeHandle({
  onPointerDown,
  dark = false,
  title = "拖动调整宽度",
}: {
  onPointerDown: (e: React.PointerEvent) => void;
  dark?: boolean;
  title?: string;
}) {
  return (
    <div
      onPointerDown={onPointerDown}
      title={title}
      className="group absolute right-0 top-0 z-20 h-full w-2.5 cursor-col-resize touch-none"
    >
      <div
        className={`absolute right-0 top-0 h-full w-px transition-all group-hover:w-[3px] group-hover:bg-cinnabar ${
          dark ? "bg-[color:var(--rule-cream)]" : "bg-[color:var(--rule)]"
        }`}
      />
    </div>
  );
}
