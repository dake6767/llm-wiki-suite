import { setToken } from "../api/client";

// 链接携带 ?token= 时：存入 localStorage，并从地址栏剥掉该参数
// （避免令牌留在浏览器历史 / 分享出去 / 随 referer 外泄）。
//
// 在 React 渲染前调用：token 先落地，Layout 的 listWikis 即可直接放行，
// 不必经过登录页。令牌无效时照常 401 → 跳登录页重输。
//
// 注意：长效令牌出现在 URL 里仍会被中继/CF 看到一次（请求行含 query）。
// 更强的做法是短时效签名链接（见 docs/05 §5.2），此处先满足"带 token 直登"。
export function bootstrapTokenFromUrl(): void {
  try {
    const url = new URL(window.location.href);
    const t = url.searchParams.get("token");
    if (!t) return;
    setToken(t.trim());
    url.searchParams.delete("token");
    const clean =
      url.pathname +
      (url.searchParams.toString() ? `?${url.searchParams}` : "") +
      url.hash;
    window.history.replaceState(null, "", clean);
  } catch {
    /* URL 解析失败则忽略，不影响正常加载 */
  }
}
