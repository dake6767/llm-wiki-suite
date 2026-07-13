// 访客态的三块 UI（docs/19 §4.2 / §4.4）：
// - GuestBanner：顶部淡色提示「你正在浏览分享的 wiki（持续更新）」。
// - ShareBlockedGuide：进入 `/share/<grant_id>/` 但缺少 #key 时的引导页；绝不回退 Owner。
// - ShareExpired：grant 撤销/过期（401）时访客看到的失效页，是访客对撤销机制的全部感知。

export function GuestBanner() {
  return (
    <div className="flex shrink-0 items-center justify-center border-b border-[color:var(--rule)] bg-cinnabar/5 px-4 py-1.5 font-serif text-xs text-cinnabar-deep">
      <span>你正在浏览分享的 Wiki（持续更新）</span>
    </div>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center bg-paper px-6 text-ink">
      <div className="w-[26rem] max-w-full text-center">{children}</div>
    </div>
  );
}

export function ShareBlockedGuide() {
  return (
    <Centered>
      <div className="seal mx-auto mb-6 h-14 w-14 text-3xl leading-none">链</div>
      <h1 className="mb-3 font-display text-xl font-semibold">分享凭证不完整或格式已失效</h1>
      <p className="font-serif text-sm leading-relaxed text-ink-faint">
        新分享链接必须包含 /share/&lt;grant_id&gt;/ 路径和 URL 末尾的 #key 访问凭证。
        为了保护作者内容，系统不会回退到 Owner 身份。
      </p>
      <p className="mt-3 font-serif text-sm leading-relaxed text-ink-faint">
        请重新复制完整链接；正常的站内链接、书签和新标签都会保留 #key。
      </p>
    </Centered>
  );
}

export function ShareExpired() {
  return (
    <Centered>
      <div className="seal mx-auto mb-6 h-14 w-14 text-3xl leading-none">停</div>
      <h1 className="mb-3 font-display text-xl font-semibold">分享已失效</h1>
      <p className="font-serif text-sm leading-relaxed text-ink-faint">
        这个分享链接已过期或被作者撤销，无法再访问其中的内容。
      </p>
      <p className="mt-3 font-serif text-sm leading-relaxed text-ink-faint">
        如仍需查看，请向作者索取新的分享链接。
      </p>
    </Centered>
  );
}
