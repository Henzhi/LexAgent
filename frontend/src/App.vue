<template>
  <router-view />
</template>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body, #app { height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Hiragino Sans GB', 'Microsoft YaHei', Roboto, 'Helvetica Neue', Arial, sans-serif;
  background: #FFFFFF;
  color: #1F1F1F;
  font-size: 15px;
  -webkit-font-smoothing: antialiased;
}

/* ---- Design Tokens: Light (default) ---- */
:root,
:root[data-theme='light'] {
  --color-primary: #4F46E5;        /* 品牌主色：靛蓝（法律/庄重） */
  --color-primary-hover: #4338CA;
  --color-primary-light: #EEF2FF;
  --color-primary-border: #C7D2FE;
  --color-surface: #FFFFFF;
  --color-surface-soft: #F9FAFB;
  --color-surface-hover: #F3F4F6;
  --color-border: #E5E7EB;
  --color-border-light: #F3F4F6;
  --color-text: #1F1F1F;
  --color-text-secondary: #4B5563;
  --color-text-muted: #9CA3AF;
  --color-error: #DC2626;
  --color-error-light: #FEF2F2;
  --color-success: #059669;
  --color-danger: #EF4444;
  --color-danger-hover: #DC2626;
  --color-sidebar-bg: #F9FAFB;
  --color-sidebar-hover: #F3F4F6;
  --radius: 10px;
  --radius-lg: 14px;
  --transition: 150ms ease;
  --shadow-card: 0 2px 8px rgba(0, 0, 0, 0.04);
  --shadow-pop: 0 8px 24px rgba(0, 0, 0, 0.12);
}

/* ---- Design Tokens: Dark ---- */
:root[data-theme='dark'] {
  --color-primary: #818CF8;        /* 提亮，保证深底可读 */
  --color-primary-hover: #A5B4FC;
  --color-primary-light: rgba(99, 102, 241, 0.16);
  --color-primary-border: #6366F1;
  --color-surface: #17181C;
  --color-surface-soft: #1F2026;
  --color-surface-hover: #2A2C33;
  --color-border: #2F3038;
  --color-border-light: #26272E;
  --color-text: #ECEDF0;
  --color-text-secondary: #A9ABB5;
  --color-text-muted: #6F7180;
  --color-error: #F87171;
  --color-error-light: rgba(248, 113, 113, 0.12);
  --color-success: #34D399;
  --color-danger: #F87171;
  --color-danger-hover: #EF4444;
  --color-sidebar-bg: #1F2026;
  --color-sidebar-hover: #2A2C33;
  --shadow-card: 0 2px 8px rgba(0, 0, 0, 0.3);
  --shadow-pop: 0 8px 24px rgba(0, 0, 0, 0.5);
}

body {
  background: var(--color-surface);
  color: var(--color-text);
  transition: background-color 200ms ease, color 200ms ease;
}

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: #D1D5DB; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #9CA3AF; }
::-webkit-scrollbar-track { background: transparent; }
:root[data-theme='dark'] ::-webkit-scrollbar-thumb { background: #3F404A; }
:root[data-theme='dark'] ::-webkit-scrollbar-thumb:hover { background: #565863; }

button { font-family: inherit; }
input, textarea, select { font-family: inherit; }
a { color: var(--color-primary); text-decoration: none; }
</style>
