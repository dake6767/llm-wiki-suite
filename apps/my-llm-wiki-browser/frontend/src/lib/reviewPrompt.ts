// 浏览器不能直接发起 review，改为把 review 项拼成可复制给 agent（带 my-llm-wiki-maintainer
// skill）的提示词。字段在 App / skill 两套 schema 间做容错取用。

import type { ReviewItem } from "../api/client";

const CLOSED_STATUSES = [
  "resolved",
  "done",
  "closed",
  "skipped",
  "skip",
  "researched",
  "queued-for-research",
];

export function isReviewOpen(item: ReviewItem): boolean {
  if (item.resolved === true) return false;
  if (!item.status) return true;
  return !CLOSED_STATUSES.includes(item.status);
}

export function reviewItemBody(item: ReviewItem): string {
  return (item.body || item.description || "").trim();
}

export function reviewItemSource(item: ReviewItem): string {
  return (item.source || item.sourcePath || "").trim();
}

// 取排序用时间戳（毫秒）：createdAt 数值优先，其次解析 created 日期串，缺省 0。
export function reviewItemTime(item: ReviewItem): number {
  if (typeof item.createdAt === "number") return item.createdAt;
  if (item.created) {
    const t = Date.parse(item.created);
    if (!Number.isNaN(t)) return t;
  }
  return 0;
}

export const REVIEW_TYPE_LABELS: Record<string, string> = {
  suggestion: "可深挖",
  "missing-page": "缺页",
  duplicate: "疑重复",
  contradiction: "冲突",
  confirm: "待确认",
};

export const reviewTypeLabel = (type?: string) =>
  (type && REVIEW_TYPE_LABELS[type]) || type || "待办";

// 按类型给出收尾动作说明——suggestion/missing-page 走 deep research，其余各按语义。
function actionForType(type?: string): string {
  switch (type) {
    case "contradiction":
      return "请核对这条冲突：检索权威证据判断哪方成立，据此更新相关 wiki 页并说明依据；完成后在 review 队列标记处理结果。";
    case "duplicate":
      return "请判断是否为重复/别名；若确为重复，按 dedup 流程合并到规范页（需先与我确认保留哪个规范名），并修正全库引用。";
    case "confirm":
      return "这条需要我来定夺，请把可选项与利弊列清楚交给我，再按我的选择执行。";
    case "missing-page":
      return "请为该缺失的概念/实体检索证据、创建 wiki 页并 ingest 回知识库；完成后在 review 队列标记为已处理。";
    default:
      return "请检索证据并归档到 raw/sources/research/，在知识库自身语气与链接约定下合成 wiki 页后 ingest 回库；完成后在 .llm-wiki/review.json 中把此项标记为已处理。";
  }
}

// 单条 review 项的提示词。
export function singleItemPrompt(root: string, item: ReviewItem): string {
  const title = item.title || "(无标题)";
  const body = reviewItemBody(item);
  const source = reviewItemSource(item);
  const queries = item.searchQueries || [];
  const lines: string[] = [];
  lines.push("使用 my-llm-wiki-maintainer skill 维护下面这个知识库，处理其中一条 review 待办。");
  lines.push("");
  lines.push(`知识库 root：${root}`);
  lines.push(`类型：${item.type || "item"}`);
  lines.push(`标题：${title}`);
  if (body) lines.push(`方向：${body}`);
  if (queries.length) {
    lines.push("建议检索词：");
    queries.forEach((q) => lines.push(`- ${q}`));
  }
  if (source) lines.push(`来源：${source}`);
  lines.push("");
  lines.push(actionForType(item.type));
  return lines.join("\n");
}

// 整个待办队列的批处理提示词。
export function batchPrompt(root: string, items: ReviewItem[]): string {
  const open = items.filter(isReviewOpen);
  const lines: string[] = [];
  lines.push("使用 my-llm-wiki-maintainer skill 维护下面这个知识库，处理它的 review 队列。");
  lines.push("");
  lines.push(`知识库 root：${root}`);
  lines.push(`review 队列：.llm-wiki/review.json（共 ${open.length} 条待办）`);
  lines.push("");
  lines.push(
    "请逐条处理：suggestion / missing-page 执行 deep research（检索 → 归档 RAW → 合成 wiki 页 → ingest）；contradiction / confirm 先交我判断再执行；duplicate 走 dedup 合并（需我确认规范名）。每条完成后在队列中标记状态。",
  );
  lines.push("");
  lines.push("待办清单：");
  open.forEach((item, i) => {
    const title = item.title || "(无标题)";
    const type = item.type || "item";
    lines.push(`${i + 1}. [${type}] ${title}`);
    const body = reviewItemBody(item);
    if (body) lines.push(`   方向：${body}`);
    const queries = item.searchQueries || [];
    if (queries.length) lines.push(`   检索词：${queries.join(" / ")}`);
    const source = reviewItemSource(item);
    if (source) lines.push(`   来源：${source}`);
  });
  return lines.join("\n");
}

// 复制到剪贴板：优先 async clipboard（localhost / https 均为安全上下文），失败回退 textarea。
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // 落到下面的兜底方案
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
