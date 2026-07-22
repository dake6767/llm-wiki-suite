import { useEffect, useMemo, useState } from "react";
import { getToken } from "../api/client";
import { BASE } from "../lib/basePath";
import { copyToClipboard } from "../lib/reviewPrompt";

// 「连接 MCP」弹窗：把本 Browser 的 MCP 端点拼成一段可复制的提示词，
// 交给任意 agent（Claude Code / 其他 MCP 客户端）完成注册。
// 本机访问默认引导 suite 自带的 stdio bridge，彻底绕过宿主/系统代理；
// 经中继打开时才给原生 streamable HTTP 配方。

// 提示词是给 agent 的指令而非配置文件：先给意图，再给两条落地路径
// （Claude Code 一行命令 / 通用 mcpServers JSON），最后附 consult-first 建议。
function buildMcpPrompt(): string {
  const endpoint = `${location.origin}${BASE}/mcp/`;
  const token = getToken();
  const auth = token ? `Authorization: Bearer ${token}` : "";
  const loopbackHosts = new Set(["127.0.0.1", "localhost", "::1"]);
  const isLocal = loopbackHosts.has(location.hostname);
  if (isLocal) {
    const cli = /Windows/i.test(navigator.userAgent)
      ? "%USERPROFILE%\\.my-llm-wiki\\bin\\my-llm-wiki.exe"
      : "~/.my-llm-wiki/bin/my-llm-wiki";
    return [
      "请把我的本机 LLM Wiki 知识库注册为 MCP 服务器。默认使用项目自带的 stdio bridge，",
      "不要把 127.0.0.1 HTTP 端点直接写进宿主配置；Hermes、WorkBuddy 等客户端可能会把",
      "loopback 错送进系统代理。bridge 会强制直连，并在运行时读取当前端口和 token。",
      "",
      "Setup Core 已把无依赖 CLI 安装在用户目录。请按当前宿主使用它注册 stdio MCP：",
      "",
      `Claude Code: claude mcp add --scope user my-llm-wiki -- ${cli} mcp-bridge`,
      `Codex: codex mcp add my-llm-wiki -- ${cli} mcp-bridge`,
      `Hermes: hermes mcp add my-llm-wiki --command ${cli} --args mcp-bridge`,
      "",
      "如果宿主没有 MCP 注册命令，请把同一个可执行文件和参数 mcp-bridge 写入其 stdio",
      "配置。不要直接注册 127.0.0.1 HTTP；bridge 会绕过系统代理，并在每次请求时读取",
      "Browser 当前端口与 token。",
      "",
      "注册后请新开会话并验证 list_wikis。Browser 必须在运行，但后续端口变化或 token 重置",
      "不需要重新注册 MCP。",
    ].join("\n");
  }
  const lines = [
    "请把我的远程 LLM Wiki 知识库注册为 MCP 服务器（streamable HTTP）：",
    "",
    `端点：${endpoint}`,
    ...(token ? [`认证头：${auth}`] : []),
    "",
    "如果你是 Claude Code，直接运行：",
    token
      ? `claude mcp add --transport http llm-wiki ${endpoint} --header "${auth}"`
      : `claude mcp add --transport http llm-wiki ${endpoint}`,
    "",
    "其他支持 streamable HTTP 的 MCP 客户端，写入等价配置：",
    JSON.stringify({
      mcpServers: {
        "llm-wiki": {
          type: "http",
          url: endpoint,
          ...(token ? { headers: { Authorization: auth } } : {}),
        },
      },
    }),
    "",
    "注意：MCP 连接在会话启动时建立，注册配置通常要新开会话才生效——如果当前",
    "会话里没出现这些工具，重启会话或重连 MCP 后再验证，不要改用其他接口绕行。",
    "",
    "注册成功后会有这些工具：list_wikis（列出知识库）、search_wiki（全文检索）、",
    "read_page（读取页面）等。请验证 list_wikis 能返回结果，然后建议在我的全局配置",
    "（如 ~/.claude/CLAUDE.md）加一条：回答落在知识库主题范围内的问题前，先用",
    "search_wiki 检索本知识库，引用到的页面在回答中注明。",
  ];
  return lines.join("\n");
}

export default function McpConnectModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  // open 时拼一次即可；token/端点在会话内不变。
  const prompt = useMemo(() => (open ? buildMcpPrompt() : ""), [open]);

  useEffect(() => {
    if (!open) return;
    setCopied(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  async function onCopy() {
    const ok = await copyToClipboard(prompt);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    }
  }

  return (
    <>
      {/* 背板 */}
      <div
        onClick={onClose}
        className={`fixed inset-0 z-40 bg-spine/30 transition-opacity duration-300 ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      {/* 居中卡片 */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label="连接 MCP"
        className={`fixed left-1/2 top-1/2 z-50 flex max-h-[82vh] w-[36rem] max-w-[92vw] -translate-x-1/2 -translate-y-1/2 flex-col border border-[color:var(--rule)] bg-paper shadow-2xl transition-opacity duration-200 ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        {/* 头部 */}
        <div className="flex shrink-0 items-center gap-3 border-b border-[color:var(--rule)] px-6 py-4">
          <div className="eyebrow text-cinnabar">连接 MCP · Connect</div>
          <span className="flex-1" />
          <button
            onClick={onClose}
            className="font-mono text-sm text-ink-faint transition-colors hover:text-cinnabar"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>

        {/* 正文 */}
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          <p className="mb-3 font-serif text-sm leading-relaxed text-ink">
            把下面这段提示词发给任意 agent（Claude Code、Hermes、WorkBuddy
            等 MCP 客户端），它会把本知识库注册成六个只读工具，包括：
            <span className="font-mono text-xs">
              list_wikis / search_wiki / read_page
            </span>
            ——之后那个 agent 回答问题前就能先查你的 wiki。
          </p>
          <pre className="overflow-x-auto whitespace-pre-wrap break-all border border-[color:var(--rule)] bg-spine/5 px-4 py-3 font-mono text-xs leading-relaxed text-ink">
            {prompt}
          </pre>
          <p className="mt-3 font-serif text-xs italic leading-relaxed text-ink-faint">
            提示词里带着访问令牌，只发给你信任的 agent；换令牌后需重新注册。
          </p>
        </div>

        {/* 底部操作 */}
        <div className="flex shrink-0 items-center justify-end border-t border-[color:var(--rule)] px-6 py-4">
          <button
            onClick={onCopy}
            className="font-mono text-sm text-cinnabar transition-colors hover:text-ink"
          >
            {copied ? "已复制 ✓" : "复制提示词 →"}
          </button>
        </div>
      </div>
    </>
  );
}
