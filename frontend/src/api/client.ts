import axios from 'axios'

const HOST_TOKEN_KEY = 'pxyfutures_host_token'

export function consumeHostToken(): string | null {
  const token = new URLSearchParams(window.location.hash.slice(1)).get('host_token')
  if (!token) return null
  sessionStorage.setItem(HOST_TOKEN_KEY, token)
  // token 位于 fragment，不会发送给服务器；读取后立即从地址栏清除。
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
  return token
}

export const client = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

client.interceptors.request.use((config) => {
  // 体验站由主平台下发 app_session；它比主平台完整 access token 优先。
  // 期货服务不再接受本地密码或主平台完整 JWT。
  const token = sessionStorage.getItem(HOST_TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})
