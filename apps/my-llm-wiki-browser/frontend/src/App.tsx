import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Workspace from "./components/Workspace";
import BrowseLanding from "./components/BrowseLanding";
import DefaultBrowseRedirect from "./components/DefaultBrowseRedirect";
import Login from "./pages/Login";
import DesktopConfig from "./pages/DesktopConfig";
import Overview from "./pages/Overview";
import PageView from "./pages/PageView";
import ReviewQueue from "./pages/ReviewQueue";
import SearchResults from "./pages/SearchResults";
import { useIsMobile } from "./lib/device";
import MobileApp from "./mobile/MobileApp";
import { credentialMode } from "./lib/shareSession";
import { ShareBlockedGuide } from "./components/ShareStates";
import { ShareNavigate } from "./lib/shareNavigation";
import PageRefRedirect from "./components/PageRefRedirect";

export default function App() {
  // 窄屏触屏设备走单列阅读器；平板/横屏等宽视口走桌面三栏（随方向变化实时切换）。
  // ?ui=mobile|desktop 可强制覆盖。（hook 必须在任何条件返回之前调用。）
  const mobile = useIsMobile();

  // `/share/<grant_id>/` 缺少或不匹配 #key 时绝不静默回退 Owner。优先于一切渲染，
  // 避免任何以 Owner 凭证发起的 API 调用。
  if (credentialMode() === "blocked") return <ShareBlockedGuide />;

  if (mobile) return <MobileApp />;

  return (
    <Routes>
      <Route path="/desktop/config" element={<DesktopConfig />} />
      <Route path="/login" element={<Login />} />
      <Route element={<Layout />}>
        {/* 根路径由 Layout 自动跳默认 wiki */}
        <Route path="/" element={null} />
        <Route path="/w/:wiki" element={<Workspace />}>
          {/* 中栏列表常显，右栏阅读区按子路由切换 */}
          <Route index element={<DefaultBrowseRedirect />} />
          <Route path="browse" element={<Overview />} />
          <Route path="browse/:type" element={<BrowseLanding />} />
          <Route path="open/:pageRef" element={<PageRefRedirect />} />
          <Route path="page/*" element={<PageView kind="page" />} />
          <Route path="raw/*" element={<PageView kind="raw" />} />
          <Route path="review" element={<ReviewQueue />} />
          <Route path="search" element={<SearchResults />} />
        </Route>
      </Route>
      <Route path="*" element={<ShareNavigate to="/" replace />} />
    </Routes>
  );
}
