import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import {
  byAddedDesc,
  getBrowseTree,
  type PageRef,
  type TreeNode,
} from "../api/client";
import Overview from "../pages/Overview";
import { ShareNavigate } from "../lib/shareNavigation";

export default function BrowseLanding() {
  const { wiki, type } = useParams();
  const [first, setFirst] = useState<PageRef | null>(null);
  const [settled, setSettled] = useState(false);

  useEffect(() => {
    if (!wiki || !type) {
      setFirst(null);
      setSettled(true);
      return;
    }

    let cancelled = false;
    setFirst(null);
    setSettled(false);
    getBrowseTree(wiki)
      .then((tree: TreeNode[]) => {
        if (cancelled) return;
        const pages = tree.find((node) => node.type === type)?.pages ?? [];
        setFirst([...pages].sort(byAddedDesc)[0] ?? null);
      })
      .catch(() => {
        if (!cancelled) setFirst(null);
      })
      .finally(() => {
        if (!cancelled) setSettled(true);
      });
    return () => {
      cancelled = true;
    };
  }, [wiki, type]);

  if (first && wiki)
    return (
      <ShareNavigate
        to={`/w/${wiki}/${type === "raw" ? "raw" : "page"}/${first.path}`}
        replace
      />
    );
  if (!settled)
    return (
      <div className="p-16 font-mono text-sm text-ink-faint">载入索引…</div>
    );
  return <Overview />;
}
