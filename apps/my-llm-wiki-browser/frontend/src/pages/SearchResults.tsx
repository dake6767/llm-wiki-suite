import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  groupSearchHits,
  search,
  type SearchHit,
  type SearchResponse,
} from "../api/client";

export default function SearchResults() {
  const { wiki } = useParams();
  const [sp] = useSearchParams();
  const q = sp.get("q") || "";
  const tag = sp.get("tag") || undefined;
  const [res, setRes] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!wiki || (!q && !tag)) return;
    let cancelled = false;
    const load = (showLoading = false) => {
      if (showLoading) setLoading(true);
      search(wiki, q, undefined, tag)
        .then((result) => {
          if (!cancelled) setRes(result);
        })
        .catch(() => {
          if (!cancelled) setRes(null);
        })
        .finally(() => {
          if (!cancelled && showLoading) setLoading(false);
        });
    };
    load(true);
    return () => {
      cancelled = true;
    };
  }, [wiki, q, tag]);

  const tagOnly = !q && !!tag;
  const groups = res ? groupSearchHits(res.hits) : [];

  return (
    <div className="reveal mx-auto max-w-3xl px-10 py-14">
      <header className="mb-8">
        <div className="eyebrow mb-3 flex items-center gap-3">
          <span>{tagOnly ? "标签 · Tag" : "检索 · Search"}</span>
          <span className="h-px w-8 bg-[color:var(--rule)]" />
          <span className="tnum">
            {loading ? "…" : `${res?.total ?? 0} 条`}
            {tag && !tagOnly ? ` · #${tag}` : ""}
          </span>
        </div>
        <h1 className="font-display text-4xl font-semibold tracking-tight">
          {tagOnly ? (
            <>
              <span className="text-brass">#</span>
              <span className="text-cinnabar">{tag}</span>
            </>
          ) : (
            <>
              「<span className="text-cinnabar">{q}</span>」
            </>
          )}
        </h1>
      </header>

      <div className="space-y-10">
        {groups.map((group) => (
          <section key={group.type}>
            <div className="eyebrow mb-3 flex items-center gap-3 border-b border-[color:var(--rule)] pb-2">
              <span className="text-brass">{group.label}</span>
              <span className="h-px flex-1 bg-[color:var(--rule-soft)]" />
              <span className="tnum text-ink-faint">{group.hits.length} 条</span>
            </div>
            <ul className="space-y-0">
              {group.hits.map((h, i) => (
                <SearchResultItem
                  key={h.path}
                  hit={h}
                  index={i}
                  wiki={wiki || ""}
                />
              ))}
            </ul>
          </section>
        ))}
      </div>

      {res && res.total === 0 && !loading && (
        <p className="py-10 text-center font-serif text-sm italic text-ink-faint">
          未在本卷中找到匹配的页面。
        </p>
      )}
    </div>
  );
}

function SearchResultItem({
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
      <Link
        to={`/w/${wiki}/page/${hit.path}`}
        className="group block border-b border-[color:var(--rule-soft)] py-5 transition-colors"
      >
        <div className="mb-1.5 flex items-baseline gap-3">
          <span className="tnum font-mono text-xs text-ink-faint">
            {String(index + 1).padStart(2, "0")}
          </span>
          <span className="font-display text-lg text-ink transition-colors group-hover:text-cinnabar">
            {hit.title}
          </span>
        </div>
        <p
          className="pl-8 font-serif text-sm leading-relaxed text-ink-soft"
          dangerouslySetInnerHTML={{ __html: hit.snippet }}
        />
      </Link>
    </li>
  );
}
