import { getShareLink, listShares } from "../api/client";
import { copyToClipboard } from "./reviewPrompt";

export type DefaultShareCopyResult =
  | "copied"
  | "missing"
  | "unavailable"
  | "failed";

export function linkForContext(link: string, deepLinkPath?: string): string {
  if (!deepLinkPath) return link;
  try {
    const url = new URL(link);
    const shareRoot = url.pathname.replace(/\/$/, "");
    url.pathname = `${shareRoot}/${deepLinkPath.replace(/^\//, "")}`;
    return url.href;
  } catch {
    return link;
  }
}

/** 复用 Wiki 的通用 grant，并把其根链接改写为当前页面深链。 */
export async function copyDefaultShareLink(
  wiki: string,
  deepLinkPath?: string,
): Promise<DefaultShareCopyResult> {
  try {
    const shares = await listShares();
    const grant = shares.find(
      (item) => item.wiki === wiki && item.active && item.is_default,
    );
    if (!grant) return "missing";
    const response = await getShareLink(grant.grant_id);
    if (response.warning) return "unavailable";
    return (await copyToClipboard(linkForContext(response.link, deepLinkPath)))
      ? "copied"
      : "failed";
  } catch {
    return "failed";
  }
}
