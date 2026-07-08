import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useNavigate } from "react-router-dom";
import { assetUrl } from "../api/client";

// 渲染后端已重写过的 markdown：
// - 内部链接形如 /w/{wiki}/page/... 或 /w/{wiki}/search?... -> 走前端路由
// - 图片 URL 形如 /api/v1/wikis/{wiki}/assets/... -> 拼上令牌
export default function Markdown({ content }: { content: string }) {
  const navigate = useNavigate();
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
                  href={url}
                  onClick={(e) => {
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
