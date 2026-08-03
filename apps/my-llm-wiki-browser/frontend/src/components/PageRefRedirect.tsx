import { useParams } from "react-router-dom";
import { decodePageRef, pageRoutePath } from "../lib/pageRef";
import { ShareNavigate } from "../lib/shareNavigation";

/** Resolve an agent-safe opaque page ref into the canonical human-readable route. */
export default function PageRefRedirect() {
  const { wiki, pageRef } = useParams();
  const path = decodePageRef(pageRef || "");
  if (!wiki || !path) {
    return <ShareNavigate to={wiki ? `/w/${wiki}` : "/"} replace />;
  }
  return (
    <ShareNavigate
      to={`/w/${encodeURIComponent(wiki)}/page/${pageRoutePath(path)}`}
      replace
    />
  );
}
