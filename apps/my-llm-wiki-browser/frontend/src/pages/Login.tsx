import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { listWikis, setToken } from "../api/client";

export default function Login() {
  const [token, setTok] = useState("");
  const [err, setErr] = useState("");
  const navigate = useNavigate();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    setToken(token.trim());
    try {
      await listWikis(); // 验证令牌
      navigate("/");
    } catch {
      setErr("令牌无效，或服务端未配置令牌。");
    }
  }

  return (
    <div className="relative flex h-full items-center justify-center overflow-hidden bg-spine text-cream">
      {/* 背景巨字水印 */}
      <span
        className="pointer-events-none absolute select-none font-display font-semibold leading-none text-cream/[0.04]"
        style={{ fontSize: "44rem", top: "-6rem", right: "-6rem" }}
        aria-hidden
      >
        藏
      </span>

      <form onSubmit={submit} className="rise relative z-10 w-[22rem]">
        <div className="mb-7 flex items-center gap-3">
          <span className="seal h-12 w-12 text-3xl leading-none">藏</span>
          <span className="leading-tight">
            <span className="block font-display text-xl font-semibold text-paper">
              LLM&nbsp;Wiki
            </span>
            <span className="eyebrow block text-cream-soft">The&nbsp;Collection</span>
          </span>
        </div>

        <p className="mb-5 font-serif text-sm italic leading-relaxed text-cream-soft">
          凭令牌入藏。本机访问且服务端未设令牌时，可留空直接进入。
        </p>

        <label className="eyebrow mb-2 block text-cream-soft">访问令牌 · Token</label>
        <input
          type="password"
          value={token}
          onChange={(e) => setTok(e.target.value)}
          placeholder="••••••••"
          autoFocus
          className="w-full border-b border-[color:var(--rule-cream)] bg-transparent pb-2 font-mono text-sm text-cream placeholder:text-cream-soft/50 focus:border-cinnabar focus:outline-none"
        />

        {err && (
          <p className="mt-3 font-mono text-xs text-cinnabar">{err}</p>
        )}

        <button
          type="submit"
          className="mt-7 w-full rounded bg-cinnabar py-2.5 font-mono text-sm font-medium tracking-wide text-paper transition-colors hover:bg-cinnabar-deep"
        >
          进入 · ENTER →
        </button>
      </form>
    </div>
  );
}
