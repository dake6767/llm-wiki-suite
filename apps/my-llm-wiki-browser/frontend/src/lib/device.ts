import { useEffect, useState } from "react";

// 移动端单列结构只用于「窄屏触屏设备」（竖屏手机）。
// 平板、横屏手机等较宽视口改走桌面端三栏体验——更接近用户的阅读习惯。
// 判定 = UA 是触屏设备 且 视口宽度 < 阈值；?ui=mobile|desktop 可强制覆盖（落盘 localStorage，便于调试）。
const MOBILE_UA =
  /Android|webOS|iPhone|iPod|iPad|BlackBerry|IEMobile|Opera Mini|Mobile/i;
const DESKTOP_MIN_WIDTH = 768; // ≥ 此宽度（平板/横屏）走桌面端

function forcedUI(): "mobile" | "desktop" | null {
  try {
    const q = new URLSearchParams(window.location.search).get("ui");
    if (q === "mobile" || q === "desktop") {
      localStorage.setItem("forceUI", q);
      return q;
    }
    const saved = localStorage.getItem("forceUI");
    if (saved === "mobile" || saved === "desktop") return saved;
  } catch {
    // localStorage 不可用时忽略
  }
  return null;
}

export function computeMobile(): boolean {
  if (typeof navigator === "undefined") return false;
  const forced = forcedUI();
  if (forced) return forced === "mobile";
  if (!MOBILE_UA.test(navigator.userAgent)) return false; // 桌面 UA 一律桌面端
  return window.innerWidth < DESKTOP_MIN_WIDTH; // 触屏设备：窄屏移动、宽屏桌面
}

// 随视口宽度/方向变化实时切换布局结构。
export function useIsMobile(): boolean {
  const [mobile, setMobile] = useState(computeMobile);
  useEffect(() => {
    const onChange = () => setMobile(computeMobile());
    window.addEventListener("resize", onChange);
    window.addEventListener("orientationchange", onChange);
    return () => {
      window.removeEventListener("resize", onChange);
      window.removeEventListener("orientationchange", onChange);
    };
  }, []);
  return mobile;
}
