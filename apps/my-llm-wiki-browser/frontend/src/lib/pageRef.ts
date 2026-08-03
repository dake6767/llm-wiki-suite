/** Decode the opaque page reference emitted by the maintainer's browser-share helper. */
export function decodePageRef(value: string): string | null {
  if (!value || !/^[A-Za-z0-9_-]+$/.test(value)) return null;

  try {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/")
      .padEnd(Math.ceil(value.length / 4) * 4, "=");
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    const path = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    const parts = path.split("/");
    if (
      !path ||
      path.startsWith("/") ||
      path.endsWith("/") ||
      path.includes("\\") ||
      path.includes("\0") ||
      parts.some((part) => !part || part === "." || part === "..")
    ) return null;
    return path;
  } catch {
    return null;
  }
}

/** Encode each semantic path segment before handing it back to React Router. */
export function pageRoutePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}
