import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { initProseSize } from "./components/FontSizeControl";
import { BASE } from "./lib/basePath";
import { bootstrapTokenFromUrl } from "./lib/bootstrapToken";
import "./index.css";

bootstrapTokenFromUrl(); // 链接带 ?token= 时自动登录并从地址栏剥掉
initProseSize(); // 应用上次保存的正文字号

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* basename 让路由在 /<uid>/ 下也按站内路径解析；根部署为 ""（即 /）。 */}
    <BrowserRouter basename={BASE || undefined}>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
