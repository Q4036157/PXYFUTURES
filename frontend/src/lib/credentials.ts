export interface TqCredentialStatus {
  configured: boolean
  status: 'unconfigured' | 'unknown' | 'valid' | 'invalid' | 'unavailable'
  checked_at: string | null
  message: string
  source: string
  connection_status?: 'connecting' | 'initializing' | 'connected' | 'disconnected' | 'unknown'
  connection_message?: string
}

interface CredentialRequestError {
  code?: string
  response?: { status?: number; data?: { detail?: unknown } }
}

export function credentialRequestFailure(error: unknown): Pick<TqCredentialStatus, 'status' | 'message'> {
  const failure = (error ?? {}) as CredentialRequestError
  if (!failure.response && (failure.code === 'ECONNABORTED' || failure.code === 'ETIMEDOUT')) {
    return {
      status: 'unknown',
      message: '请求超时，保存结果尚未确认，请稍后重新打开账号窗口查看状态，避免重复提交',
    }
  }
  if (!failure.response) {
    return { status: 'unknown', message: '无法连接本机或工作站服务，保存结果尚未确认，请恢复连接后重新打开账号窗口查看状态' }
  }
  const detail = failure.response.data?.detail
  return {
    status: failure.response.status === 422 ? 'invalid' : 'unavailable',
    message: typeof detail === 'string' && detail.trim()
      ? detail
      : '账号验证服务暂不可用，请稍后重试',
  }
}

export function connectionAlertType(status: TqCredentialStatus['connection_status']): 'success' | 'warning' | 'info' {
  if (status === 'connected') return 'success'
  if (status === 'disconnected') return 'warning'
  return 'info'
}
