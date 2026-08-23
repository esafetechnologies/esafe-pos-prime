import 'vue'

declare module 'vue' {
  interface ComponentCustomProperties {
    __: (
      msg: string,
      replace?: Record<string, string> | string[],
      context?: string
    ) => string
  }
}
