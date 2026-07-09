// API client：统一带令牌、处理 401。

import { withBase } from "../lib/basePath";

const TOKEN_KEY = "llm_wiki_token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export class AuthError extends Error {}

async function api<T>(path: string): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(withBase(path), { headers });
  if (res.status === 401) throw new AuthError("未授权");
  if (!res.ok) throw new Error(`请求失败 ${res.status}`);
  return res.json() as Promise<T>;
}

// 带方法/请求体的写操作（PUT/POST 等）。错误时尽量带上后端 detail 文案。
async function send<T>(
  path: string,
  method: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(withBase(path), {
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

// 给 <img> 用：加 BASE 前缀 + 把令牌拼到 URL（后端支持 ?token=）
export function assetUrl(rawUrl: string): string {
  const url = withBase(rawUrl);
  const token = getToken();
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
export const getPage = (wiki: string, path: string) =>
  api<Page>(`/api/v1/wikis/${wiki}/pages/${path}`);
export const getRaw = (wiki: string, path: string) =>
  api<Page>(`/api/v1/wikis/${wiki}/raw/${path}`);
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
