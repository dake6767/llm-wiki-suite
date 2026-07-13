import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { assetUrl, decorateInternalHref } from "../api/client";
import { useShareNavigate } from "./shareNavigation";

// 渲染后端已重写过的 markdown：
// - 内部链接形如 /w/{wiki}/page/... 或 /w/{wiki}/search?... -> 走前端路由；
//   href 经 decorateInternalHref 附带动态 Guest basename 与 #key，复制/新标签可访问。
// - 图片 URL 形如 /api/v1/wikis/{wiki}/assets/... -> 拼上令牌
export default function Markdown({ content }: { content: string }) {
  const navigate = useShareNavigate();
  return (
    <div className="prose-wiki">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a({ href, children, ...props }) {
            const url = href || "";
            if (url.startsWith("/w/")) {
              return (
                <a
                  href={decorateInternalHref(url)}
                  onClick={(e) => {
                    // 保留浏览器原生的新标签/新窗口手势；href 已含 Guest basename + #key。
                    if (
                      e.button !== 0 ||
                      e.metaKey ||
                      e.ctrlKey ||
                      e.shiftKey ||
                      e.altKey
                    ) return;
                    e.preventDefault();
                    navigate(url);
                  }}
                  {...props}
                >
                  {children}
                </a>
              );
            }
            return (
              <a href={url} target="_blank" rel="noreferrer" {...props}>
                {children}
              </a>
            );
          },
          img({ src, ...props }) {
            const s = typeof src === "string" ? src : "";
            const finalSrc = s.startsWith("/api/") ? assetUrl(s) : s;
            return <img src={finalSrc} loading="lazy" {...props} />;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
