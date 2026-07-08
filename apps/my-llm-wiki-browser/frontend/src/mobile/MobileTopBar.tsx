import { useLocation, useNavigate } from "react-router-dom";
import {
  typeLabel,
  wikiDefaultPath,
  wikiRecentPath,
  type WikiInfo,
} from "../api/client";
import FontSizeControl from "../components/FontSizeControl";

// 顶栏随路由自适应：
// - 列表/类目：☰ 菜单 + 卷名·类目 + ⌕ 搜索
// - 阅读（page/raw）：← 返回 + 类目名 + 字号调节
// - 搜索：← 返回 + 搜索
export default function MobileTopBar({
  current,
  onMenu,
}: {
  current?: WikiInfo;
  onMenu: () => void;
}) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const wiki = pathname.match(/^\/w\/([^/]+)/)?.[1] || "";
  const rest = pathname.replace(/^\/w\/[^/]+\/?/, "");

  const onReader = /^(page|raw)\//.test(rest);
  const onSearch = rest.startsWith("search");

  // 阅读页返回：带类目段则回该类目列表，否则回本卷「最近」。
  // 搜索页返回默认入口（来源）。
  function back() {
    if (onSearch) {
      navigate(wiki ? wikiDefaultPath(wiki) : "/");
      return;
    }
    const inner = rest.replace(/^(page|raw)\//, "");
    const seg = inner.includes("/") ? inner.split("/")[0] : "";
    navigate(
      seg ? `/w/${wiki}/browse/${seg}` : wiki ? wikiRecentPath(wiki) : "/",
    );
  }

  let title: string;
  if (onSearch) title = "检索 · Search";
  else if (onReader) {
    const inner = rest.replace(/^(page|raw)\//, "");
    const seg = inner.includes("/") ? inner.split("/")[0] : "";
    title = seg ? typeLabel(seg) : current?.name || wiki;
  } else {
    const type = rest.startsWith("browse/") ? rest.slice("browse/".length) : "";
    title = `${current?.name || wiki}${type ? ` · ${typeLabel(type)}` : " · 最近"}`;
  }

  return (
    <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b border-[color:var(--rule)] bg-paper/95 px-4 backdrop-blur">
      {onReader || onSearch ? (
        <button
          onClick={back}
          aria-label="返回"
          className="-ml-1 flex h-9 w-9 items-center justify-center text-xl text-ink transition-colors active:text-cinnabar"
        >
          ‹
        </button>
      ) : (
        <button
          onClick={onMenu}
          aria-label="菜单"
          className="-ml-1 flex h-9 w-9 items-center justify-center text-lg text-ink transition-colors active:text-cinnabar"
        >
          ☰
        </button>
      )}

      <h1 className="min-w-0 flex-1 truncate font-display text-base font-semibold tracking-tight">
        {title}
      </h1>

      {onReader ? (
        <FontSizeControl />
      ) : !onSearch ? (
        <button
          onClick={() => navigate(`/w/${wiki}/search`)}
          aria-label="搜索"
          className="flex h-9 w-9 items-center justify-center text-lg text-cinnabar"
        >
          ⌕
        </button>
      ) : null}
    </header>
  );
}
