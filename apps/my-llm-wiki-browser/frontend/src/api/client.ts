// API client：统一带令牌、处理 401。

import { withBase } from "../lib/basePath";
import {
  credentialMode,
  getActiveShare,
  shareAwareHref,
} from "../lib/shareSession";

const TOKEN_KEY = "llm_wiki_token";
let ownerApiEndpoint: { baseUrl: string; token: string } | null = null;

// Tauri-local 设置中心复用 Browser 的配置组件，但不能把 Setup Core 暴露给
// loopback HTTP。桌面壳只把当前进程的本机 API 地址与 Owner token 注入内存，
// 所有 Skills / 工具链操作仍通过 Tauri command 直达 Setup Core。
export function configureOwnerApiEndpoint(baseUrl: string, token: string) {
  ownerApiEndpoint = {
    baseUrl: baseUrl.replace(/\/+$/, ""),
    token,
  };
}

function requestUrl(path: string): string {
  if (!ownerApiEndpoint) return withBase(path);
  return `${ownerApiEndpoint.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

// Owner（master token）槽——localStorage 全局唯一。仅 Owner 会话使用；访客凭证走
// sessionStorage（shareSession.ts），二者物理隔离，Owner 自测访客链接不会互相覆盖。
export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

// 发往 API 的实际凭证（凭证判定三段序，见 shareSession.credentialMode）：
// - guest → share token；owner → master token；blocked → 不带凭证（应已被引导页拦下）。
export function authToken(): string {
  if (ownerApiEndpoint) return ownerApiEndpoint.token;
  const mode = credentialMode();
  if (mode === "guest") return getActiveShare() || "";
  if (mode === "blocked") return "";
  return getToken();
}

// 原生站内 href（主要是 Markdown）：Guest 使用稳定 `/share/<grant_id>` namespace，
// 并把 secret 续在 #key 中，使右键新标签和复制链接都能直接访问。
export function decorateInternalHref(path: string): string {
  return shareAwareHref(path);
}

export class AuthError extends Error {}

// 中继瞬时不可达时 Worker 直接回这些状态：503=连接器离线/断开（重连空窗），
// 502=连接器发送失败。远程访问经 wss 隧道，链路 RST 后有 1-2s 重连窗口，窗口内
// 落到的 GET 读请求会吃到 503——对只读浏览做短暂重试即可让空窗对用户「隐形」。
// 只用于 api()（GET 读）；写操作 send() 不重试，可能非幂等（如创建分享）。
const RELAY_RETRY_STATUSES = new Set([502, 503]);
// 重试节奏：覆盖典型重连空窗（现约 1-2s）并留余量；若中继真的下线，最坏 ~3.4s 后暴露错误。
const RELAY_RETRY_DELAYS_MS = [400, 1000, 2000];

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function api<T>(path: string): Promise<T> {
  const headers: Record<string, string> = {};
  const token = authToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const url = requestUrl(path);

  let lastError: unknown = new Error("请求失败");
  for (let attempt = 0; attempt <= RELAY_RETRY_DELAYS_MS.length; attempt++) {
    if (attempt > 0) await sleep(RELAY_RETRY_DELAYS_MS[attempt - 1]);
    let res: Response;
    try {
      res = await fetch(url, { headers });
    } catch (err) {
      // fetch 抛出 = CF 边缘/本机网络瞬断（Worker 可达时总会回 HTTP 状态）。同样可重试。
      lastError = err;
      continue;
    }
    if (res.status === 401) throw new AuthError("未授权");
    if (res.ok) return res.json() as Promise<T>;
    // 可重试状态且还有重试额度：记下错误、进入下一轮；否则立即抛出（含 404 等确定性失败）。
    if (RELAY_RETRY_STATUSES.has(res.status) && attempt < RELAY_RETRY_DELAYS_MS.length) {
      lastError = new Error(`请求失败 ${res.status}`);
      continue;
    }
    throw new Error(`请求失败 ${res.status}`);
  }
  throw lastError instanceof Error ? lastError : new Error("请求失败");
}

// 带方法/请求体的写操作（PUT/POST/PATCH/DELETE 等）。错误时尽量带上后端 detail 文案。
async function send<T>(
  path: string,
  method: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {};
  const token = authToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(requestUrl(path), {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) throw new AuthError("未授权");
  if (!res.ok) {
    let detail = `请求失败 ${res.status}`;
    try {
      const data = (await res.json()) as { detail?: string };
      if (data?.detail) detail = data.detail;
    } catch {
      // 忽略无法解析的响应体
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

// 给 <img> 用：加 BASE 前缀 + 把凭证拼到 URL（后端支持 ?token=；访客用 share token）。
// 遗留缺口：<img> 不能带 Authorization 头，资产子请求仍过一次链路（docs/19 §4.2）。
export function assetUrl(rawUrl: string): string {
  const url = withBase(rawUrl);
  const token = authToken();
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

// 去掉正文开头与标题重复的 # H1（wiki 页常在首行重复 frontmatter title）。
// 仅当首个非空行是 H1 且文本与 title 相同才剥离，避免误删有意义的小标题。
export function stripTitleH1(body: string, title: string): string {
  const lines = body.split("\n");
  let i = 0;
  while (i < lines.length && lines[i].trim() === "") i++;
  const m = lines[i]?.match(/^#\s+(.*)$/);
  if (m && m[1].trim() === title.trim()) {
    lines.splice(0, i + 1);
    while (lines.length && lines[0].trim() === "") lines.shift();
    return lines.join("\n");
  }
  return body;
}

// ---- 类型 ----
export interface WikiInfo {
  key: string;
  name: string;
  description: string;
  default: boolean;
  page_count: number;
}
export interface WikiConfigInfo extends WikiInfo {
  root_dir: string;
  wiki_dir: string;
  raw_dir: string;
  assets_dir: string;
}
export interface PageRef {
  path: string;
  slug: string;
  title: string;
  type: string;
  tags: string[];
  updated?: string | null;
  created?: string | null;
  mtime?: number | null;
}

// 「加入库时间」：frontmatter created 优先，缺失回退文件 mtime（秒→毫秒），再缺为 0。
export function addedTime(p: PageRef): number {
  if (p.created) {
    const t = Date.parse(p.created);
    if (!Number.isNaN(t)) return t;
  }
  if (typeof p.mtime === "number") return p.mtime * 1000;
  return 0;
}
export const byAddedDesc = (a: PageRef, b: PageRef) => addedTime(b) - addedTime(a);

// 加入库日期，形如 2026-06-18（无可用时间返回空串）。
export function addedDateLabel(p: PageRef): string {
  const t = addedTime(p);
  if (!t) return "";
  const d = new Date(t);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}
export interface TreeNode {
  type: string;
  count: number;
  pages: PageRef[];
}
export interface LinkRef {
  target: string | null;
  label: string;
  broken: boolean;
}
export interface Page {
  wiki: string;
  path: string;
  slug: string;
  title: string;
  type: string;
  frontmatter: Record<string, unknown>;
  body: string;
  outgoing_links: LinkRef[];
  backlinks: PageRef[];
  sources: string[];
  source_pages: PageRef[];
  tags: string[];
}
export interface SearchHit {
  path: string;
  slug: string;
  title: string;
  type: string;
  snippet: string;
  score: number;
}
export interface SearchResponse {
  query: string;
  total: number;
  hits: SearchHit[];
}
export interface SearchHitGroup {
  type: string;
  label: string;
  hits: SearchHit[];
}
// 经 <Outlet context> 下发给子页面
export interface LayoutContext {
  wikis: WikiInfo[];
  current?: WikiInfo;
}

export interface ServerConfigInfo {
  port: number;
}

// supported=false 表示宿主没注入自启钩子（如纯浏览器访问），设置页隐藏开关。
export interface AutostartConfigInfo {
  supported: boolean;
  enabled: boolean;
}

// review 项在 App 与 maintainer skill 之间字段略有出入，这里做并集容错。
export interface ReviewItem {
  id?: string;
  type?: string;
  title?: string;
  body?: string;
  description?: string;
  searchQueries?: string[];
  source?: string;
  sourcePath?: string;
  affectedPages?: string[];
  status?: string;
  resolved?: boolean;
  created?: string;
  createdAt?: number;
}
export interface ReviewResponse {
  wiki: string;
  root: string;
  count: number;
  open_count: number;
  items: ReviewItem[];
}

// 进访客模式的依据。
export interface SessionInfo {
  principal: "owner" | "guest";
  wiki?: string;
}

// grant 列表项——永不含 secret。
export type ShareScope =
  | { kind: "whole_wiki" }
  | { kind: "frozen_set"; pages: string[]; assets: string[] }
  | { kind: "index_anchor"; index_page: string; live: boolean };

export interface ShareGrantView {
  grant_id: string;
  label: string;
  wiki: string;
  scope: ShareScope;
  include_raw: boolean;
  created_at: number;
  expires_at: number | null;
  last_accessed: number | null;
  revoked: boolean;
  active: boolean;
  is_default: boolean;
}

// 创建 / 取链接响应——含完整分享链接（内嵌 secret）。
export interface ShareLinkResponse {
  grant_id: string;
  wiki: string;
  label: string;
  expires_at: number | null;
  link: string;
  warning?: string;
}

// ---- 调用 ----
export const listWikis = () => api<WikiInfo[]>("/api/v1/wikis");
export const listConfigWikis = () =>
  api<WikiConfigInfo[]>("/api/v1/config/wikis");
// 设为默认 wiki：服务端改写注册表并热重载，返回更新后的完整列表。
export const setDefaultWiki = (key: string) =>
  send<WikiConfigInfo[]>("/api/v1/config/wikis/default", "PUT", { key });
export const getServerConfig = () =>
  api<ServerConfigInfo>("/api/v1/config/server");
// 持久化新端口（需重启生效）。
export const updateServerPort = (port: number) =>
  send<{ port: number; restart_required: boolean }>(
    "/api/v1/config/server",
    "PUT",
    { port },
  );
// 触发桌面进程重启，使新端口生效。
export const restartServer = () =>
  send<{ ok: boolean }>("/api/v1/config/restart", "POST");
export const getAutostartConfig = () =>
  api<AutostartConfigInfo>("/api/v1/config/autostart");
// 开关开机自启，立即生效，无需重启。
export const setAutostart = (enabled: boolean) =>
  send<AutostartConfigInfo>("/api/v1/config/autostart", "PUT", { enabled });
export const getTree = (wiki: string) =>
  api<TreeNode[]>(`/api/v1/wikis/${wiki}/tree`);
export const getRawTree = (wiki: string) =>
  api<TreeNode>(`/api/v1/wikis/${wiki}/raw-tree`);
// Browser 导航同时展示编译页类目与扁平 RAW 层；两套后端索引语义保持独立。
// RAW 层对访客不可分享（raw-tree 返回 404），此时降级为只展示编译层类目——
// 不能让 raw-tree 的失败拖垮整棵导航树，否则访客侧目录/落地页全空（表现为“无法访问”）。
export const getBrowseTree = async (wiki: string): Promise<TreeNode[]> => {
  const tree = await getTree(wiki);
  try {
    const raw = await getRawTree(wiki);
    return [...tree, raw];
  } catch {
    return tree;
  }
};
export const getPage = (wiki: string, path: string) =>
  api<Page>(`/api/v1/wikis/${wiki}/pages/${path}`);
export const getRaw = (wiki: string, path: string) =>
  api<Page>(`/api/v1/wikis/${wiki}/raw/${path}`);
export const getSourcePreview = (wiki: string, page: string, source: string) => {
  const query = new URLSearchParams({ page, source });
  return api<Page>(`/api/v1/wikis/${wiki}/source-preview?${query.toString()}`);
};
export const getReview = (wiki: string) =>
  api<ReviewResponse>(`/api/v1/wikis/${wiki}/review`);
export const search = (
  wiki: string,
  q: string,
  type?: string,
  tag?: string,
) => {
  const p = new URLSearchParams({ q });
  if (type) p.set("type", type);
  if (tag) p.set("tag", tag);
  return api<SearchResponse>(`/api/v1/wikis/${wiki}/search?${p.toString()}`);
};

export interface ShareConfigInfo {
  relay_connected: boolean;
  online_url?: string | null;
}

// 会话与分享管理（管理 API 均 Owner-only）。
export const getSession = () => api<SessionInfo>("/api/v1/session");
export const getShareConfig = () =>
  api<ShareConfigInfo>("/api/v1/config/share");
export const listShares = () => api<ShareGrantView[]>("/api/v1/config/shares");
// expiresInDays: number = n 天后过期；null = 永久（高级选项）。
export const createShare = (
  wiki: string,
  label: string,
  expiresInDays: number | null,
  makeDefault = false,
) =>
  send<ShareLinkResponse>("/api/v1/config/shares", "POST", {
    wiki,
    label,
    expires_in_days: expiresInDays,
    make_default: makeDefault,
  });
export const getShareLink = (grantId: string) =>
  send<ShareLinkResponse>(`/api/v1/config/shares/${grantId}/link`, "POST");
export const setDefaultShare = (grantId: string) =>
  send<ShareGrantView>(`/api/v1/config/shares/${grantId}/default`, "POST");
export const renewShare = (grantId: string, expiresInDays: number | null) =>
  send<ShareGrantView>(`/api/v1/config/shares/${grantId}`, "PATCH", {
    expires_in_days: expiresInDays,
  });
export const revokeShare = (grantId: string) =>
  send<ShareGrantView>(`/api/v1/config/shares/${grantId}`, "DELETE");

export const DEFAULT_BROWSE_TYPE = "sources";
export const wikiDefaultPath = (wiki: string) =>
  `/w/${wiki}/browse/${DEFAULT_BROWSE_TYPE}`;
export const wikiRecentPath = (wiki: string) => `/w/${wiki}/browse`;

export const TYPE_LABELS: Record<string, string> = {
  entities: "实体",
  entity: "实体",
  concepts: "概念",
  concept: "概念",
  sources: "来源",
  source: "来源",
  queries: "问题",
  query: "问题",
  synthesis: "综述",
  comparisons: "对比",
  comparison: "对比",
  overview: "总览",
  _root: "总览",
  page: "页面",
  raw: "RAW · 原始层",
};
export const typeLabel = (t: string) => TYPE_LABELS[t] || t;

const TYPE_ALIASES: Record<string, string> = {
  entity: "entities",
  concept: "concepts",
  source: "sources",
  query: "queries",
  comparison: "comparisons",
  overview: "_root",
};

const SEARCH_GROUP_ORDER = [
  "sources",
  "concepts",
  "entities",
  "queries",
  "synthesis",
  "comparisons",
  "_root",
  "page",
];

function canonicalType(type: string): string {
  return TYPE_ALIASES[type] || type;
}

export function groupSearchHits(hits: SearchHit[]): SearchHitGroup[] {
  const groups = new Map<string, { first: number; hits: SearchHit[] }>();
  hits.forEach((hit, index) => {
    const type = canonicalType(hit.type);
    const group = groups.get(type);
    if (group) group.hits.push(hit);
    else groups.set(type, { first: index, hits: [hit] });
  });

  const rank = (type: string) => {
    const index = SEARCH_GROUP_ORDER.indexOf(type);
    return index === -1 ? SEARCH_GROUP_ORDER.length : index;
  };

  return [...groups.entries()]
    .sort(([aType, a], [bType, b]) => {
      const byRank = rank(aType) - rank(bType);
      return byRank || a.first - b.first || aType.localeCompare(bType);
    })
    .map(([type, group]) => ({
      type,
      label: typeLabel(type),
      hits: group.hits,
    }));
}
