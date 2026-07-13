import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { initProseSize } from "./components/FontSizeControl";
import { bootstrapTokenFromUrl } from "./lib/bootstrapToken";
import {
  bootstrapShareFromUrl,
  markOpenSharePanel,
  routerBasePath,
} from "./lib/shareSession";
import "./index.css";

// 新分享协议：从 `/share/<grant_id>/#key=<secret>` 建立访客会话；key 常驻 fragment，
// 当前页面地址本身始终可复制、刷新和转发。
bootstrapShareFromUrl();
bootstrapTokenFromUrl(); // 链接带 ?token= 时自动登录并从地址栏剥掉

// 托盘「分享 Wiki…」深链 ?share=open：置一次性标记后剥除该参数，由分享面板消费。
try {
  const u = new URL(window.location.href);
  if (u.searchParams.get("share") === "open") {
    markOpenSharePanel();
    u.searchParams.delete("share");
    const clean =
      u.pathname + (u.searchParams.toString() ? `?${u.searchParams}` : "") + u.hash;
    window.history.replaceState(null, "", clean);
  }
} catch {
  /* ignore */
}
initProseSize(); // 应用上次保存的正文字号

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* Owner basename=/<uid>；Guest basename=/<uid>/share/<grant_id>。 */}
    <BrowserRouter basename={routerBasePath() || undefined}>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
