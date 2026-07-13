import { Route, Routes } from "react-router-dom";
import Login from "../pages/Login";
import MobileLayout from "./MobileLayout";
import MobileList from "./MobileList";
import MobileReader from "./MobileReader";
import MobileSearch from "./MobileSearch";
import DefaultBrowseRedirect from "../components/DefaultBrowseRedirect";
import ReviewQueue from "../pages/ReviewQueue";
import { ShareNavigate } from "../lib/shareNavigation";

// 移动端路由树：URL 与桌面端保持一致（链接可跨设备共享），仅呈现结构不同。
export default function MobileApp() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<MobileLayout />}>
        <Route path="/" element={null} />
        <Route path="/w/:wiki" element={<DefaultBrowseRedirect />} />
        <Route path="/w/:wiki/browse" element={<MobileList />} />
        <Route path="/w/:wiki/browse/:type" element={<MobileList />} />
        <Route path="/w/:wiki/page/*" element={<MobileReader kind="page" />} />
        <Route path="/w/:wiki/raw/*" element={<MobileReader kind="raw" />} />
        <Route path="/w/:wiki/review" element={<ReviewQueue dense />} />
        <Route path="/w/:wiki/search" element={<MobileSearch />} />
      </Route>
      <Route path="*" element={<ShareNavigate to="/" replace />} />
    </Routes>
  );
}
