/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

/** Frappe translation function — available globally via window.__ */
declare function __(msg: string, replace?: Record<string, string> | string[], context?: string): string

interface Window {
  posPageSetProfile?: (profileName: string) => void
  posPageClearProfile?: () => void
}
