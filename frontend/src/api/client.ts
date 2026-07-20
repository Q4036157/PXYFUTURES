import axios from 'axios'

const LOCAL_TOKEN_KEY = 'pxyfutures_token'
const HOST_TOKEN_KEY = 'pxyfutures_host_token'

export function consumeHostToken(): string | null {
  const token = new URLSearchParams(window.location.hash.slice(1)).get('host_token')
  if (!token) return null
  sessionStorage.setItem(HOST_TOKEN_KEY, token)
  // token 位于 fragment，不会发送给服务器；读取后立即从地址栏清除。
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
  return token
}

export function isHostSession(): boolean {
  return Boolean(sessionStorage.getItem(HOST_TOKEN_KEY))
}

export const client = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

client.interceptors.request.use((config) => {
  // 集成主平台时优先复用其登录态；独立部署时使用本项目的本地会话。
  const token = localStorage.getItem('token')
    || sessionStorage.getItem(HOST_TOKEN_KEY)
    || localStorage.getItem(LOCAL_TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

export function saveLocalToken(token: string): void {
  localStorage.setItem(LOCAL_TOKEN_KEY, token)
}

export function clearLocalToken(): void {
  localStorage.removeItem(LOCAL_TOKEN_KEY)
}
