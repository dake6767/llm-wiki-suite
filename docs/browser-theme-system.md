# Browser 可扩展主题系统

Browser 的主题由两个正交维度组成：

```text
主题家族 family × 外观模式 mode
```

`family` 决定视觉语言，包括字体、圆角、纹理、标题标记、chrome 气质与局部动效；
`mode` 只决定该家族使用亮色、暗色或跟随系统。新增科技风、自然风等主题时，不再复制一套
“亮 / 暗主题”状态逻辑。

## 内置主题家族

| ID | 名称 | 视觉语言 |
| --- | --- | --- |
| `archive` | 典藏 Archive | 暖纸、书脊、朱砂、衬线手稿与纸张颗粒 |
| `signal` | 信号 Signal | IBM Plex、冷青 HUD、终端直角、网格与扫描线 |
| `field` | 自然 Field | Newsreader、鼠尾草、陶土、有机圆角与植物标本纹理 |

每个家族完整支持 `light`、`dark` 和 `system` 三种模式。

## 状态与 DOM 契约

偏好分别保存为：

```text
localStorage["my-llm-wiki.theme-family"] = "archive|signal|field"
localStorage["my-llm-wiki.theme-mode"]   = "light|dark|system"
```

旧版 `my-llm-wiki.theme = paper|ink|system` 会自动迁移到
`archive + light|dark|system`。

运行时 DOM 暴露：

```text
data-theme="signal-dark"        # 便于调试的完整解析结果
data-theme-family="signal"      # 视觉家族
data-theme-mode="dark"          # 实际渲染模式
data-theme-preference="system"  # 用户选择，可能与解析模式不同
```

## Token 分层

### 1. 组件语义 token

组件继续使用稳定的 Tailwind 别名，不感知具体家族：

| 角色 | Tailwind 别名 |
| --- | --- |
| 画布 / 次级表面 | `paper`, `paper-2`, `paper-3` |
| 主 / 次 / 弱文字 | `ink`, `ink-soft`, `ink-faint` |
| 品牌动作 | `cinnabar`, `cinnabar-deep`, `on-accent` |
| 导航 chrome | `spine`, `spine-2`, `cream`, `cream-soft` |
| 辅助强调 | `brass` |
| 错误状态 | `danger`, `danger-strong`, `danger-surface` |

`cinnabar` 是历史命名的品牌强调角色，不代表所有主题都必须使用红色。错误色必须使用
`danger`，不能跟随品牌强调色变成 cyan 或绿色。

### 2. 家族形态 token

每个家族定义：

```text
--theme-font-display
--theme-font-body
--theme-font-index
--theme-control-radius
--theme-card-radius
--theme-eyebrow-spacing
--theme-body-leading
--theme-texture-image
--theme-texture-size
```

### 3. 模式配色 token

家族的每个实际模式完整提供：

```text
--theme-canvas
--theme-surface-subtle
--theme-surface-muted
--theme-text
--theme-text-muted
--theme-text-subtle
--theme-accent
--theme-accent-strong
--theme-on-accent
--theme-chrome
--theme-chrome-raised
--theme-on-chrome
--theme-on-chrome-muted
--theme-ornament
--theme-rule
--theme-rule-soft
--theme-chrome-rule
--theme-selection
--theme-scrollbar
--theme-scrollbar-hover
--theme-texture-opacity
--theme-texture-blend
--theme-code-text
--theme-shadow-soft
--theme-input-fill
--theme-input-fill-focus
```

## 新增主题家族

1. 在 `src/lib/theme.tsx` 扩展 `ThemeFamily`，并向 `THEME_FAMILIES` 注册名称和描述。
2. 在 `THEME_COLORS` 注册亮、暗模式的浏览器 `theme-color`。
3. 在 `src/index.css` 增加一个家族形态块，以及 `light`、`dark` 两个完整配色块。
4. 在 `.theme-family-preview` 增加该家族的静态缩略预览。
5. 只在视觉语言确实需要时增加家族选择器；业务组件内禁止判断 family。
6. 校验正文、小号索引、强调文字和按钮至少满足 WCAG AA 4.5:1。
7. 验证 375、768、1024、1440px，不允许主题选择器或新形态产生横向滚动。

状态实现位于 `src/lib/theme.tsx`，选择器位于
`src/components/ThemeSwitcher.tsx`，所有主题 token 位于 `src/index.css`。
