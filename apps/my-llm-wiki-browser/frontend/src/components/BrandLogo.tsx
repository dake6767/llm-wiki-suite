import type { CSSProperties } from "react";
import { withBase } from "../lib/basePath";

export function BrandLogo({ className = "" }: { className?: string }) {
  return (
    <img
      src={withBase("/apple-touch-icon.png")}
      alt=""
      aria-hidden="true"
      className={`block shrink-0 rounded-[22%] ${className}`}
    />
  );
}

export function BrandMark({ className = "" }: { className?: string }) {
  const logoMask = `url("${withBase("/icon.svg")}")`;
  const style: CSSProperties = {
    backgroundColor: "currentColor",
    WebkitMaskImage: logoMask,
    maskImage: logoMask,
    WebkitMaskPosition: "center",
    maskPosition: "center",
    WebkitMaskRepeat: "no-repeat",
    maskRepeat: "no-repeat",
    WebkitMaskSize: "contain",
    maskSize: "contain",
  };

  return <span aria-hidden="true" className={`block ${className}`} style={style} />;
}
