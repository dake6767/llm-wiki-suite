import { Navigate, useParams } from "react-router-dom";
import { wikiDefaultPath } from "../api/client";

export default function DefaultBrowseRedirect() {
  const { wiki } = useParams();
  return <Navigate to={wiki ? wikiDefaultPath(wiki) : "/"} replace />;
}
