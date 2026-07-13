import { useParams } from "react-router-dom";
import { wikiDefaultPath } from "../api/client";
import { ShareNavigate } from "../lib/shareNavigation";

export default function DefaultBrowseRedirect() {
  const { wiki } = useParams();
  return <ShareNavigate to={wiki ? wikiDefaultPath(wiki) : "/"} replace />;
}
