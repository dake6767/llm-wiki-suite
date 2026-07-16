import { useEffect, useMemo, useState } from "react";
import { getBrowseTree, listConfigWikis, type TreeNode } from "../api/client";
import { credentialMode } from "../lib/shareSession";
import { copyToClipboard } from "../lib/reviewPrompt";

// 首次引导卡：wiki 刚初始化、编译层还没有任何内容页时，在落地页给 Owner 一段
// 可复制给 agent（Claude Code / Hermes / Codex 等装有 my-llm-wiki 技能的助手）的
// 提示词，引导体验「抓取 → 沉淀 RAW → 整理成 wiki」的完整闭环。
// 纯浏览器叠加层：不写任何 wiki 文件；一旦库里出现内容页即自动消失。

const URL_PLACEHOLDER = "<把这一行换成你想收藏的链接>";

// 模板页（index/log/overview）落在 _root 桶，RAW 层是 raw 桶，都不算「已有内容」。
const NON_CONTENT_TYPES = new Set(["_root", "raw"]);

function buildCapturePrompt(root: string, url: string): string {
  return [
    "使用 my-llm-wiki 技能，把下面这篇内容抓取沉淀到我的知识库，并直接整理成 wiki 页面：",
    "",
    url.trim() || URL_PLACEHOLDER,
    "",
    `知识库 root：${root}`,
  ].join("\n");
}

// RAW 已有沉淀但从未整理（例如只跑了抓取一步）时，引导改为清空 inbox。
function buildFlushPrompt(root: string, rawCount: number): string {
  return [
    `使用 my-llm-wiki-maintainer 技能，处理下面这个知识库的 inbox：把尚未整理的 RAW 来源（现有 ${rawCount} 篇）逐篇 ingest 成 wiki 页面。`,
    "",
    `知识库 root：${root}`,
  ].join("\n");
}

export default function FirstCaptureGuide({ wiki }: { wiki: string }) {
  const isOwner = credentialMode() === "owner";
  const [rawCount, setRawCount] = useState(0);
  const [root, setRoot] = useState("");
  const [empty, setEmpty] = useState(false);
  const [url, setUrl] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!wiki || !isOwner) return;
    let cancelled = false;
    setEmpty(false);
    setRoot("");
    getBrowseTree(wiki)
      .then((tree: TreeNode[]) => {
        if (cancelled) return;
        const content = tree
          .filter((n) => !NON_CONTENT_TYPES.has(n.type))
          .reduce((s, n) => s + n.count, 0);
        if (content > 0) return;
        setRawCount(tree.find((n) => n.type === "raw")?.count ?? 0);
        setEmpty(true);
        // root 路径是提示词的必要信息，只在确认要展示后才取（Owner-only 配置 API）。
        return listConfigWikis().then((wikis) => {
          if (cancelled) return;
          setRoot(wikis.find((w) => w.key === wiki)?.root_dir ?? "");
        });
      })
      .catch(() => {
        // 树或配置读不到就静默不展示，落地页保持原样。
      });
    return () => {
      cancelled = true;
    };
  }, [wiki, isOwner]);

  const prompt = useMemo(() => {
    if (!root) return "";
    return rawCount > 0
      ? buildFlushPrompt(root, rawCount)
      : buildCapturePrompt(root, url);
  }, [root, rawCount, url]);

  if (!isOwner || !empty || !root) return null;

  async function onCopy() {
    const ok = await copyToClipboard(prompt);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    }
  }

  return (
    <section className="mb-12 border border-[color:var(--rule)] bg-spine/5 px-6 py-5">
      <div className="eyebrow mb-3 flex items-center gap-3 text-cinnabar">
        <span>{rawCount > 0 ? "待整理 · Inbox" : "开始 · First Capture"}</span>
        <span className="h-px flex-1 bg-[color:var(--rule)]" />
      </div>
      <p className="mb-4 font-serif text-sm leading-relaxed text-ink">
        {rawCount > 0 ? (
          <>
            RAW 层已有 {rawCount} 篇沉淀，还没有整理成 wiki
            页面。把下面的提示词发给你的 agent，把它们编译进知识库。
          </>
        ) : (
          <>
            这卷知识库刚完成初始化，还没有任何整理好的页面。把下面的提示词发给装有{" "}
            <span className="font-mono text-xs">my-llm-wiki</span> 技能的
            agent（Claude Code、Hermes、Codex 等），体验一次「抓取 → 沉淀 →
            整理」的完整流程。
          </>
        )}
      </p>
      {rawCount === 0 && (
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="可选：先把想收藏的链接贴在这里，会自动填进提示词"
          className="mb-3 w-full border border-[color:var(--rule)] bg-paper px-3 py-2 font-mono text-xs text-ink placeholder:text-ink-faint focus:border-cinnabar focus:outline-none"
        />
      )}
      <pre className="overflow-x-auto whitespace-pre-wrap break-all border border-[color:var(--rule)] bg-paper px-4 py-3 font-mono text-xs leading-relaxed text-ink">
        {prompt}
      </pre>
      <div className="mt-3 flex items-center gap-4">
        <button
          onClick={onCopy}
          className="font-mono text-sm text-cinnabar transition-colors hover:text-ink"
        >
          {copied ? "已复制 ✓" : "复制提示词 →"}
        </button>
        <span className="flex-1" />
        <button
          onClick={() => window.location.reload()}
          className="font-mono text-xs text-ink-faint transition-colors hover:text-cinnabar"
          title="agent 整理完成后，刷新即可看到编译出的页面"
        >
          整理完成？刷新 ↻
        </button>
      </div>
    </section>
  );
}
