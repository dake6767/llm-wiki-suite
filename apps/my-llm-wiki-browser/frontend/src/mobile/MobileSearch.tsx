import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import {
  groupSearchHits,
  search,
  type SearchHit,
  type SearchResponse,
} from "../api/client";
import { ShareLink, useShareNavigate } from "../lib/shareNavigation";

// 移动端检索：顶部输入框 + 命中列表；支持 ?q= 全文与 ?tag= 标签浏览。
export default function MobileSearch() {
  const { wiki } = useParams();
  const navigate = useShareNavigate();
  const [sp] = useSearchParams();
  const q = sp.get("q") || "";
  const tag = sp.get("tag") || undefined;
  const [term, setTerm] = useState(q);
  const [res, setRes] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => setTerm(q), [q]);

  useEffect(() => {
    if (!wiki || (!q && !tag)) {
      setRes(null);
      return;
    }
    setLoading(true);
    search(wiki, q, undefined, tag)
      .then(setRes)
      .catch(() => setRes(null))
      .finally(() => setLoading(false));
  }, [wiki, q, tag]);

  const tagOnly = !q && !!tag;
  const groups = res ? groupSearchHits(res.hits) : [];

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const t = term.trim();
    if (wiki && t) navigate(`/w/${wiki}/search?q=${encodeURIComponent(t)}`);
  }

  return (
    <div className="flex h-full flex-col">
      <form onSubmit={submit} className="shrink-0 px-4 pb-3 pt-4">
        <div className="flex items-center gap-2 rounded border border-[color:var(--rule)] bg-paper-2/50 px-3 py-2.5 focus-within:border-cinnabar">
          <span className="font-mono text-cinnabar">⌕</span>
          <input
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="检索本卷…"
            autoFocus={!q && !tag}
            enterKeyHint="search"
            className="w-full bg-transparent font-serif text-base text-ink placeholder:text-ink-faint focus:outline-none"
          />
        </div>
        <div className="eyebrow mt-2 flex items-center gap-2">
          <span>{tagOnly ? "标签 · Tag" : "检索 · Search"}</span>
          {tagOnly && <span className="text-cinnabar">#{tag}</span>}
          <span className="ml-auto tnum text-ink-faint">
            {loading ? "…" : res ? `${res.total} 条` : ""}
          </span>
        </div>
      </form>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-8">
        {groups.map((group) => (
          <section key={group.type} className="pt-4">
            <div className="eyebrow flex items-center gap-2 border-b border-[color:var(--rule)] pb-2">
              <span className="text-brass">{group.label}</span>
              <span className="ml-auto tnum text-ink-faint">
                {group.hits.length} 条
              </span>
            </div>
            <ul>
              {group.hits.map((h, i) => (
                <MobileSearchResultItem
                  key={h.path}
                  hit={h}
                  index={i}
                  wiki={wiki || ""}
                />
              ))}
            </ul>
          </section>
        ))}
        {res && res.total === 0 && !loading && (
          <p className="py-12 text-center font-serif text-sm italic text-ink-faint">
            未在本卷中找到匹配的页面。
          </p>
        )}
        {!res && !loading && (
          <p className="py-12 text-center font-serif text-sm italic text-ink-faint">
            输入关键词检索本卷。
          </p>
        )}
      </div>
    </div>
  );
}

function MobileSearchResultItem({
  hit,
  index,
  wiki,
}: {
  hit: SearchHit;
  index: number;
  wiki: string;
}) {
  return (
    <li>
      <ShareLink
        to={`/w/${wiki}/page/${hit.path}`}
        className="block border-b border-[color:var(--rule-soft)] py-4 text-ink transition-colors active:text-cinnabar"
      >
        <div className="mb-1 flex items-baseline gap-2">
          <span className="tnum font-mono text-xs text-ink-faint">
            {String(index + 1).padStart(2, "0")}
          </span>
          <span className="font-display text-base">{hit.title}</span>
        </div>
        <p
          className="font-serif text-sm leading-relaxed text-ink-soft"
          dangerouslySetInnerHTML={{ __html: hit.snippet }}
        />
      </ShareLink>
    </li>
  );
}
