import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type ThemeFamily = "archive" | "signal" | "field";
export type ThemeMode = "system" | "light" | "dark";
export type ResolvedThemeMode = Exclude<ThemeMode, "system">;

export interface ThemeSelection {
  family: ThemeFamily;
  mode: ThemeMode;
}

export interface ThemeFamilyDefinition {
  id: ThemeFamily;
  label: string;
  englishLabel: string;
  description: string;
}

export const THEME_FAMILIES: readonly ThemeFamilyDefinition[] = [
  {
    id: "archive",
    label: "典藏",
    englishLabel: "Archive",
    description: "暖纸、书脊与朱砂，适合沉浸式长文阅读。",
  },
  {
    id: "signal",
    label: "信号",
    englishLabel: "Signal",
    description: "冷青 HUD、网格和终端排版，具有明确的科技感。",
  },
  {
    id: "field",
    label: "自然",
    englishLabel: "Field",
    description: "鼠尾草、陶土与有机曲线，像一本植物观察志。",
  },
] as const;

export const THEME_MODES: ReadonlyArray<{
  id: ThemeMode;
  label: string;
  description: string;
}> = [
  { id: "light", label: "亮", description: "使用当前主题的亮色外观" },
  { id: "dark", label: "暗", description: "使用当前主题的暗色外观" },
  { id: "system", label: "自动", description: "跟随系统外观" },
];

const FAMILY_STORAGE_KEY = "my-llm-wiki.theme-family";
const MODE_STORAGE_KEY = "my-llm-wiki.theme-mode";
const LEGACY_STORAGE_KEY = "my-llm-wiki.theme";
const DARK_MEDIA_QUERY = "(prefers-color-scheme: dark)";
const DEFAULT_SELECTION: ThemeSelection = { family: "archive", mode: "system" };

const THEME_COLORS: Record<
  ThemeFamily,
  Record<ResolvedThemeMode, string>
> = {
  archive: { light: "#f6f2e9", dark: "#181714" },
  signal: { light: "#eaf4f4", dark: "#061217" },
  field: { light: "#eef1e5", dark: "#151d18" },
};

interface ThemeContextValue extends ThemeSelection {
  resolvedMode: ResolvedThemeMode;
  setFamily: (family: ThemeFamily) => void;
  setMode: (mode: ThemeMode) => void;
  setTheme: (selection: ThemeSelection) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function isThemeFamily(value: string | null): value is ThemeFamily {
  return THEME_FAMILIES.some((family) => family.id === value);
}

function isThemeMode(value: string | null): value is ThemeMode {
  return value === "system" || value === "light" || value === "dark";
}

function legacySelection(value: string | null): ThemeSelection | null {
  if (value === "paper") return { family: "archive", mode: "light" };
  if (value === "ink") return { family: "archive", mode: "dark" };
  if (value === "system") return DEFAULT_SELECTION;
  return null;
}

function storedSelection(): ThemeSelection {
  try {
    const family = window.localStorage.getItem(FAMILY_STORAGE_KEY);
    const mode = window.localStorage.getItem(MODE_STORAGE_KEY);
    if (isThemeFamily(family) || isThemeMode(mode)) {
      return {
        family: isThemeFamily(family) ? family : DEFAULT_SELECTION.family,
        mode: isThemeMode(mode) ? mode : DEFAULT_SELECTION.mode,
      };
    }
    return (
      legacySelection(window.localStorage.getItem(LEGACY_STORAGE_KEY)) ??
      DEFAULT_SELECTION
    );
  } catch {
    return DEFAULT_SELECTION;
  }
}

function persistSelection(selection: ThemeSelection) {
  try {
    window.localStorage.setItem(FAMILY_STORAGE_KEY, selection.family);
    window.localStorage.setItem(MODE_STORAGE_KEY, selection.mode);
    window.localStorage.removeItem(LEGACY_STORAGE_KEY);
  } catch {
    // A private or locked-down webview may reject storage; the session still works.
  }
}

function systemMode(): ResolvedThemeMode {
  return window.matchMedia?.(DARK_MEDIA_QUERY).matches ? "dark" : "light";
}

function resolveMode(mode: ThemeMode): ResolvedThemeMode {
  return mode === "system" ? systemMode() : mode;
}

function syncThemeMeta(family: ThemeFamily, mode: ResolvedThemeMode) {
  let meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.append(meta);
  }
  meta.content = THEME_COLORS[family][mode];
}

export function applyTheme(selection: ThemeSelection): ResolvedThemeMode {
  const resolvedMode = resolveMode(selection.mode);
  const root = document.documentElement;
  root.dataset.theme = `${selection.family}-${resolvedMode}`;
  root.dataset.themeFamily = selection.family;
  root.dataset.themeMode = resolvedMode;
  root.dataset.themePreference = selection.mode;
  root.style.colorScheme = resolvedMode;
  syncThemeMeta(selection.family, resolvedMode);
  return resolvedMode;
}

/** Apply the persisted theme family and mode before React renders. */
export function initializeTheme() {
  return applyTheme(storedSelection());
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [selection, setSelectionState] =
    useState<ThemeSelection>(storedSelection);
  const [systemPreference, setSystemPreference] =
    useState<ResolvedThemeMode>(systemMode);

  const resolvedMode =
    selection.mode === "system" ? systemPreference : selection.mode;

  const setTheme = useCallback((next: ThemeSelection) => {
    persistSelection(next);
    setSelectionState(next);
    applyTheme(next);
  }, []);

  const setFamily = useCallback(
    (family: ThemeFamily) => setTheme({ ...selection, family }),
    [selection, setTheme],
  );

  const setMode = useCallback(
    (mode: ThemeMode) => setTheme({ ...selection, mode }),
    [selection, setTheme],
  );

  useEffect(() => {
    const media = window.matchMedia(DARK_MEDIA_QUERY);
    const onSystemThemeChange = () => {
      const nextSystemMode = media.matches ? "dark" : "light";
      setSystemPreference(nextSystemMode);
      if (selection.mode === "system") applyTheme(selection);
    };
    media.addEventListener("change", onSystemThemeChange);
    return () => media.removeEventListener("change", onSystemThemeChange);
  }, [selection]);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (
        event.key !== FAMILY_STORAGE_KEY &&
        event.key !== MODE_STORAGE_KEY &&
        event.key !== LEGACY_STORAGE_KEY &&
        event.key !== null
      ) {
        return;
      }
      const next = storedSelection();
      setSelectionState(next);
      applyTheme(next);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const value = useMemo(
    () => ({
      ...selection,
      resolvedMode,
      setFamily,
      setMode,
      setTheme,
    }),
    [selection, resolvedMode, setFamily, setMode, setTheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
