// ShareGrant 访客会话与可传播 URL。
//
// URL 契约：
//   /<uid>/share/<grant_id>/#key=<secret>
//   /<uid>/share/<grant_id>/w/<wiki>/page/<path>#key=<secret>
//   /<uid>/share/<grant_id>/w/<wiki>/open/<page-ref>#key=<secret>
//
// grant_id 是公开标识，进入 pathname，给路由一个稳定的 Guest 命名空间；secret 只在
// fragment 中，页面导航不会把它发给 relay / Cloudflare。URL 是可转发凭证的真相来源，
// sessionStorage 只是本标签页为 API Authorization 缓存组合后的 `<grant_id>.<secret>`。

import type { To } from "react-router-dom";
import { BASE } from "./basePath";

// 分租户：不同 uid 共享同一 relay 域名，sessionStorage 按 uid 隔离，避免串号。
const uid = BASE ? BASE.replace(/^\/+/, "") : "root";
const ACTIVE_KEY = `active_share:${uid}`;

interface ShareRoute {
  grantId: string;
  routerBase: string;
}

function currentShareRoute(): ShareRoute | null {
  try {
    const pathname = window.location.pathname;
    const relative = BASE && pathname.startsWith(`${BASE}/`)
      ? pathname.slice(BASE.length)
      : BASE === "" ? pathname : "";
    const match = relative.match(/^\/share\/([^/]+)(?:\/|$)/);
    if (!match) return null;
    const grantId = decodeURIComponent(match[1]);
    return {
      grantId,
      routerBase: `${BASE}/share/${match[1]}`,
    };
  } catch {
    return null;
  }
}

function fragmentParams(): URLSearchParams {
  try {
    return new URLSearchParams(window.location.hash.replace(/^#/, ""));
  } catch {
    return new URLSearchParams();
  }
}

// 在 React 渲染前调用。新协议只接受 share pathname + #key；旧的
// `/#share=<grant_id>.<secret>` 不再兼容。
export function bootstrapShareFromUrl(): void {
  const route = currentShareRoute();
  if (!route) return;
  try {
    const secret = fragmentParams().get("key")?.trim() || "";
    if (!route.grantId.startsWith("s_") || !secret) return;
    sessionStorage.setItem(ACTIVE_KEY, `${route.grantId}.${secret}`);
  } catch {
    /* URL/storage 不可用则忽略，credentialMode 会进入 blocked 而非回退 Owner */
  }
}

export function getActiveShare(): string | null {
  try {
    return sessionStorage.getItem(ACTIVE_KEY);
  } catch {
    return null;
  }
}

export function clearActiveShare(): void {
  try {
    sessionStorage.removeItem(ACTIVE_KEY);
  } catch {
    /* ignore */
  }
}

// grant_id（公开、非秘密）= share token 的第一个 `.` 前段。
export function shareGrantId(token: string): string {
  const dot = token.indexOf(".");
  return dot > 0 ? token.slice(0, dot) : token;
}

function shareSecret(token: string): string {
  const dot = token.indexOf(".");
  return dot > 0 ? token.slice(dot + 1) : "";
}

export type CredentialMode = "owner" | "guest" | "blocked";

// 路径是 Guest 上下文的硬边界：
// - 非 share 路径永远按 Owner；旧 sessionStorage 残留不能把 Owner 页面变成 Guest。
// - share 路径必须同时持有匹配 grant_id 的 active token，否则 blocked，绝不回退 Owner。
export function credentialMode(): CredentialMode {
  const route = currentShareRoute();
  // 旧 `#share=<token>` 不做兼容解析，但必须明确拦截，避免 Owner 打开旧链接时
  // 静默按 Owner 权限渲染并误以为旧分享仍然有效。
  if (!route) return fragmentParams().has("share") ? "blocked" : "owner";
  const token = getActiveShare();
  const key = fragmentParams().get("key")?.trim() || "";
  if (
    token &&
    shareGrantId(token) === route.grantId &&
    shareSecret(token) === key &&
    key
  ) return "guest";
  return "blocked";
}

export function isGuest(): boolean {
  return credentialMode() === "guest";
}

// BrowserRouter 的动态 basename。Guest 路由复用既有 `/w/...` 路由树，但浏览器地址
// 始终带 `/share/<grant_id>` 前缀；API 仍只使用 BASE，不进入该前缀。
export function routerBasePath(): string {
  return currentShareRoute()?.routerBase || BASE;
}

function keyHash(existingHash = ""): string {
  const token = getActiveShare();
  const secret = token ? shareSecret(token) : "";
  if (!secret || credentialMode() !== "guest") return existingHash;

  const current = new URLSearchParams(existingHash.replace(/^#/, ""));
  const params = new URLSearchParams();
  params.set("key", secret);
  // fragment 已被 capability key 占用；如调用方携带普通锚点，将它编码进 anchor 参数。
  const anchor = current.get("anchor") ||
    (existingHash && !current.has("key") ? existingHash.replace(/^#/, "") : "");
  if (anchor) params.set("anchor", anchor);
  return `#${params.toString()}`;
}

// 给 React Router 的 to 自动续上 #key，覆盖 Link、Navigate 与程序化 navigate。
export function shareAwareTo(to: To): To {
  if (credentialMode() !== "guest") return to;
  if (typeof to === "string") {
    const hashAt = to.indexOf("#");
    const path = hashAt >= 0 ? to.slice(0, hashAt) : to;
    const hash = hashAt >= 0 ? to.slice(hashAt) : "";
    return `${path}${keyHash(hash)}`;
  }
  return { ...to, hash: keyHash(to.hash || "") };
}

// 给原生 <a href>（Markdown 内链、右键新标签）生成带动态 router basename 和 key 的
// 完整站内路径。pathname 中不含 secret，fragment 中保留 secret。
export function shareAwareHref(path: string): string {
  if (credentialMode() !== "guest") return `${BASE}${path}`;
  return `${routerBasePath()}${path}${keyHash()}`;
}

// 托盘「分享 Wiki…」深链 ?share=open 的一次性标记。
const OPEN_PANEL_KEY = "open_share_panel";

export function markOpenSharePanel(): void {
  try {
    sessionStorage.setItem(OPEN_PANEL_KEY, "1");
  } catch {
    /* ignore */
  }
}

export function consumeOpenSharePanel(): boolean {
  try {
    if (sessionStorage.getItem(OPEN_PANEL_KEY) === "1") {
      sessionStorage.removeItem(OPEN_PANEL_KEY);
      return true;
    }
  } catch {
    /* ignore */
  }
  return false;
}

// 当前访客页面本身就是可传播深链；此函数用于显式「复制分享链接」按钮。
export function shareEntryLink(): string | null {
  if (credentialMode() !== "guest") return null;
  return window.location.href;
}
