import { useLayoutEffect, useRef } from "react";
import { Outlet, useLocation, useOutletContext } from "react-router-dom";
import type { LayoutContext } from "../api/client";
import { useResizableWidth } from "../lib/useResizableWidth";
import PageList from "./PageList";
import ResizeHandle from "./ResizeHandle";

// 主从布局：中栏 PageList（常显）+ 右栏阅读区 <Outlet/>。
// 窄屏（< lg）按路由回退：在 page/raw/search 阅读态时显示阅读区，否则显示列表。
export default function Workspace() {
  const ctx = useOutletContext<LayoutContext>();
  const { pathname, search } = useLocation();
  const readerRef = useRef<HTMLElement | null>(null);
  const onReader = /\/(page|raw|search|review)(\/|$)/.test(pathname);
  const sp = new URLSearchParams(search);
  const focus = sp.get("focus") === "1";
  const keepList = sp.get("keepList") === "1";
  // 待审(review)视图占满阅读区，各断点都隐藏中栏列表。
  const hideList =
    (pathname.endsWith("/search") && !keepList) ||
    focus ||
    /\/review(\/|$)/.test(pathname);
  // 中栏列表可拖拽调宽
  const { ref, width, startResize } = useResizableWidth<HTMLDivElement>(
    "listWidth",
    320,
    240,
    520,
  );

  useLayoutEffect(() => {
    readerRef.current?.scrollTo({ top: 0, left: 0 });
  }, [pathname, search]);

  return (
    <div className="flex h-full min-h-0">
      <div
        ref={ref}
        style={{ width }}
        className={`relative shrink-0 flex-col border-r border-[color:var(--rule)] ${
          hideList ? "hidden" : onReader ? "hidden lg:flex" : "flex"
        }`}
      >
        <PageList />
        <ResizeHandle onPointerDown={startResize} />
      </div>
      <section
        ref={readerRef}
        className={`min-w-0 flex-1 overflow-y-auto ${
          onReader ? "block" : "hidden lg:block"
        }`}
      >
        <Outlet context={ctx} />
      </section>
    </div>
  );
}
