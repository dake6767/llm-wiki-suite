import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import {
  AuthError,
  listWikis,
  wikiDefaultPath,
  type WikiInfo,
} from "../api/client";
import MobileNavSheet from "./MobileNavSheet";
import MobileTopBar from "./MobileTopBar";
import { useShareNavigate } from "../lib/shareNavigation";
import { isGuest } from "../lib/shareSession";
import { GuestBanner, ShareExpired } from "../components/ShareStates";

// 移动端外壳：顶栏 + 单列内容 + 左侧抽屉。数据加载/重定向与桌面 Layout 对齐，URL 保持一致。
export default function MobileLayout() {
  const [wikis, setWikis] = useState<WikiInfo[]>([]);
  const [err, setErr] = useState("");
  const [expired, setExpired] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const guest = isGuest();
  const navigate = useShareNavigate();
  const { pathname } = useLocation();
  const wiki = pathname.match(/^\/w\/([^/]+)/)?.[1];

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      listWikis()
        .then((items) => {
          if (cancelled) return;
          setWikis(items);
          setErr("");
        })
        .catch((e) => {
          if (cancelled) return;
          if (e instanceof AuthError) {
            if (guest) setExpired(true);
            else navigate("/login");
          }
          else setErr(String(e));
        });
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [navigate, guest]);

  useEffect(() => {
    if (!wiki && wikis.length && pathname === "/") {
      const def = wikis.find((w) => w.default) || wikis[0];
      navigate(wikiDefaultPath(def.key), { replace: true });
    } else if (wiki && wikis.length && !wikis.some((w) => w.key === wiki)) {
      const def = wikis.find((w) => w.default) || wikis[0];
      navigate(wikiDefaultPath(def.key), { replace: true });
    }
  }, [wiki, wikis, navigate, pathname]);

  // 路由切换时收起抽屉
  useEffect(() => setNavOpen(false), [pathname]);

  const current = wikis.find((w) => w.key === wiki);

  if (expired) return <ShareExpired />;

  return (
    <div className="flex h-full flex-col bg-paper text-ink">
      <MobileTopBar current={current} onMenu={() => setNavOpen(true)} />
      {guest && <GuestBanner />}
      {err && (
        <div className="mx-4 mt-4 rounded border border-cinnabar/30 bg-cinnabar/5 px-4 py-3 font-mono text-sm text-cinnabar-deep">
          {err}
        </div>
      )}
      <main className="min-h-0 flex-1">
        <Outlet context={{ wikis, current }} />
      </main>
      <MobileNavSheet
        wikis={wikis}
        current={current}
        open={navOpen}
        onClose={() => setNavOpen(false)}
      />
    </div>
  );
}
