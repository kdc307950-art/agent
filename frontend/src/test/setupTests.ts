import '@testing-library/jest-dom'
import { vi } from 'vitest'

// 为测试提供稳定的 crypto.randomUUID
Object.defineProperty(globalThis, 'crypto', {
  value: {
    randomUUID: () => '00000000-0000-0000-0000-000000000000',
  },
})

// 部分组件依赖 window.confirm
Object.defineProperty(window, 'confirm', {
  writable: true,
  value: vi.fn(),
})

// sessionStorage mock
const sessionStore: Record<string, string> = {}
Object.defineProperty(window, 'sessionStorage', {
  value: {
    getItem: vi.fn((key: string) => sessionStore[key] ?? null),
    setItem: vi.fn((key: string, value: string) => {
      sessionStore[key] = value
    }),
    removeItem: vi.fn((key: string) => {
      delete sessionStore[key]
    }),
    clear: vi.fn(() => {
      Object.keys(sessionStore).forEach((key) => delete sessionStore[key])
    }),
  },
})
