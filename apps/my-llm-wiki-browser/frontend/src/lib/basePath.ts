// 运行时基路径：支持把整个 SPA 挂在 /<uid>/ 下（路径分租户中继，docs/06 §5.4）。
//
// 取自 <base href>（根部署为 "/"，经中继时由 Worker 改写为 "/<uid>/"）。
// BASE 规整为不带尾斜杠：根为 ""，中继为 "/<uid>"。
// 所有「绝对站内路径」（/api/...、路由 basename）都应经 withBase 加前缀。

function computeBase(): string {
  const href = document.querySelector("base")?.getAttribute("href") || "/";
  try {
    const path = new URL(href, location.origin).pathname; // "/" 或 "/<uid>/"
    return path.replace(/\/+$/, ""); // "" 或 "/<uid>"
  } catch {
    return "";
  }
}

// 启动时定一次即可（<base> 在文档解析阶段就已就位）。
export const BASE = computeBase();

// 给「/」开头的站内绝对路径加上 BASE 前缀；其它（相对/外链）原样返回。
export function withBase(path: string): string {
  return path.startsWith("/") ? BASE + path : path;
}
