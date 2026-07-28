import type { KeyboardEvent, ReactNode } from "react";
import {
  THEME_FAMILIES,
  THEME_MODES,
  useTheme,
  type ThemeFamily,
  type ThemeMode,
} from "../lib/theme";

function nextRadioIndex(
  key: string,
  current: number,
  length: number,
): number {
  if (key === "Home") return 0;
  if (key === "End") return length - 1;
  if (key === "ArrowRight" || key === "ArrowDown") {
    return (current + 1) % length;
  }
  if (key === "ArrowLeft" || key === "ArrowUp") {
    return (current - 1 + length) % length;
  }
  return -1;
}

function selectWithKeyboard(
  event: KeyboardEvent<HTMLButtonElement>,
  index: number,
  length: number,
  select: (index: number) => void,
) {
  const next = nextRadioIndex(event.key, index, length);
  if (next < 0) return;
  event.preventDefault();
  select(next);
  const radios =
    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>(
      '[role="radio"]',
    );
  radios?.[next]?.focus();
}

function ModeIcon({ mode }: { mode: ThemeMode }) {
  const paths: Record<ThemeMode, ReactNode> = {
    light: (
      <>
        <circle cx="12" cy="12" r="3.25" />
        <path d="M12 2.25v2.1M12 19.65v2.1M2.25 12h2.1M19.65 12h2.1M5.1 5.1l1.5 1.5M17.4 17.4l1.5 1.5M18.9 5.1l-1.5 1.5M6.6 17.4l-1.5 1.5" />
      </>
    ),
    dark: (
      <path d="M20.25 15.2A8.5 8.5 0 0 1 8.8 3.75 8.5 8.5 0 1 0 20.25 15.2Z" />
    ),
    system: (
      <>
        <rect x="3" y="4.5" width="18" height="12.5" rx="1.5" />
        <path d="M8.5 20h7M12 17v3" />
      </>
    ),
  };

  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      {paths[mode]}
    </svg>
  );
}

export default function ThemeSwitcher({
  tone = "surface",
  compact = false,
  className = "",
}: {
  tone?: "surface" | "spine";
  compact?: boolean;
  className?: string;
}) {
  const {
    family,
    mode,
    resolvedMode,
    setFamily,
    setMode,
  } = useTheme();

  return (
    <section
      className={`theme-switcher ${className}`}
      data-tone={tone}
      data-compact={compact || undefined}
      data-resolved-mode={resolvedMode}
      aria-label="主题设置"
    >
      <div
        className="theme-family-options"
        role="radiogroup"
        aria-label="主题家族"
      >
        {THEME_FAMILIES.map((option, index) => {
          const selected = family === option.id;
          return (
            <button
              key={option.id}
              type="button"
              className="theme-family-option"
              role="radio"
              aria-checked={selected}
              aria-label={`${option.label} · ${option.englishLabel}：${option.description}`}
              title={option.description}
              tabIndex={selected ? 0 : -1}
              onClick={() => setFamily(option.id)}
              onKeyDown={(event) =>
                selectWithKeyboard(
                  event,
                  index,
                  THEME_FAMILIES.length,
                  (next) =>
                    setFamily(THEME_FAMILIES[next].id as ThemeFamily),
                )
              }
            >
              <span
                className="theme-family-preview"
                data-family-preview={option.id}
                aria-hidden="true"
              />
              <span className="theme-family-label">
                <b>{option.label}</b>
                {!compact && <small>{option.englishLabel}</small>}
              </span>
            </button>
          );
        })}
      </div>

      <div
        className="theme-mode-options"
        role="radiogroup"
        aria-label="外观模式"
      >
        {THEME_MODES.map((option, index) => {
          const selected = mode === option.id;
          return (
            <button
              key={option.id}
              type="button"
              className="theme-mode-option"
              role="radio"
              aria-checked={selected}
              aria-label={option.description}
              title={option.description}
              tabIndex={selected ? 0 : -1}
              onClick={() => setMode(option.id)}
              onKeyDown={(event) =>
                selectWithKeyboard(event, index, THEME_MODES.length, (next) =>
                  setMode(THEME_MODES[next].id),
                )
              }
            >
              <span className="theme-mode-icon">
                <ModeIcon mode={option.id} />
              </span>
              <span>{option.label}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
