import { useEffect, useState } from "react";
import { getReview, type Page, type ReviewItem } from "../api/client";
import { isReviewOpen, reviewItemTime } from "./reviewPrompt";

// 归一化仓库内路径：去 .md、去开头斜杠、剥掉 raw/ 或 wiki/ 顶层前缀，
// 使不同写法（raw 源、wiki 页、frontmatter sources）能对齐比较。
function core(path: string): string {
  return path
    .trim()
    .replace(/\.md$/i, "")
    .replace(/^\/+/, "")
    .replace(/^(raw|wiki)\//, "");
}

// 一条 review 项是否指向当前页。命中信号有两类：
//   - review.source / sourcePath 命中本页路径本身（raw 源页）或本页 frontmatter 的 sources
//     （wiki 来源摘要页——其 slug 与 raw 源不同名，只能靠 sources 关联）。
//   - review.affectedPages 命中本页路径（concept / entity 页）。
export function itemMatchesPage(item: ReviewItem, page: Page): boolean {
  const pageRefs = new Set(
    [page.path, ...(page.sources ?? [])]
      .filter((x): x is string => typeof x === "string" && x.length > 0)
      .map(core),
  );
  if (pageRefs.size === 0) return false;
  const itemRefs = [item.source, item.sourcePath, ...(item.affectedPages ?? [])]
    .filter((x): x is string => typeof x === "string" && x.length > 0)
    .map(core);
  return itemRefs.some((r) => pageRefs.has(r));
}

// 取当前页可深挖的方向：该 wiki 中 open 且指向本页的 review 项。
export function useDeepDive(wiki: string | undefined, page: Page | null) {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [root, setRoot] = useState("");

  useEffect(() => {
    if (!wiki || !page) {
      setItems([]);
      return;
    }
    let cancelled = false;
    getReview(wiki)
      .then((res) => {
        if (cancelled) return;
        setRoot(res.root);
        setItems(
          res.items
            .filter((it) => isReviewOpen(it) && itemMatchesPage(it, page))
            .sort((a, b) => reviewItemTime(b) - reviewItemTime(a)),
        );
      })
      .catch(() => {
        if (!cancelled) setItems([]);
      });
    return () => {
      cancelled = true;
    };
  }, [wiki, page]);

  return { items, root };
}
