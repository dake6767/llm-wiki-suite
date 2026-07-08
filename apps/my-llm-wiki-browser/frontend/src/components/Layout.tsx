import { useEffect, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  AuthError,
  listWikis,
  wikiDefaultPath,
  type WikiInfo,
} from "../api/client";
import Sidebar from "./Sidebar";

export default function Layout() {
  const [wikis, setWikis] = useState<WikiInfo[]>([]);
  const [err, setErr] = useState<string>("");
  const navigate = useNavigate();
  const { pathname } = useLocation();
  // 祖先布局拿不到子路由参数，从 pathname 解析当前 wiki。
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
          if (e instanceof AuthError) navigate("/login");
          else setErr(String(e));
        });
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [navigate]);

  // 根路径时自动跳到默认 wiki 的来源类目。
  useEffect(() => {
    if (!wiki && wikis.length && pathname === "/") {
      const def = wikis.find((w) => w.default) || wikis[0];
      navigate(wikiDefaultPath(def.key), { replace: true });
    } else if (wiki && wikis.length && !wikis.some((w) => w.key === wiki)) {
      const def = wikis.find((w) => w.default) || wikis[0];
      navigate(wikiDefaultPath(def.key), { replace: true });
    }
  }, [wiki, wikis, navigate, pathname]);

  const current = wikis.find((w) => w.key === wiki);

  return (
    <div className="flex h-full bg-paper text-ink">
      <Sidebar wikis={wikis} current={current} />
      <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden">
        {err && (
          <div className="mx-auto mt-6 max-w-3xl rounded border border-cinnabar/30 bg-cinnabar/5 px-4 py-3 font-mono text-sm text-cinnabar-deep">
            {err}
          </div>
        )}
        <div className="min-h-0 flex-1">
          <Outlet context={{ wikis, current }} />
        </div>
      </main>
    </div>
  );
}
