import { forwardRef, useCallback } from "react";
import {
  Link,
  Navigate,
  useNavigate,
  type LinkProps,
  type NavigateFunction,
  type NavigateProps,
  type NavigateOptions,
  type To,
} from "react-router-dom";
import { shareAwareTo } from "./shareSession";

// 所有站内导航统一经过这里，让 Guest URL 的 #key 在点击、重定向、右键新标签和
// 程序化跳转时都不丢失。Owner 路由不做任何改写。
export const ShareLink = forwardRef<HTMLAnchorElement, LinkProps>(
  function ShareLink({ to, ...props }, ref) {
    return <Link ref={ref} to={shareAwareTo(to)} {...props} />;
  },
);

export function ShareNavigate({ to, ...props }: NavigateProps) {
  return <Navigate to={shareAwareTo(to)} {...props} />;
}

export function useShareNavigate(): NavigateFunction {
  const navigate = useNavigate();
  return useCallback(
    ((to: To | number, options?: NavigateOptions) => {
      if (typeof to === "number") return navigate(to);
      return navigate(shareAwareTo(to), options);
    }) as NavigateFunction,
    [navigate],
  );
}
