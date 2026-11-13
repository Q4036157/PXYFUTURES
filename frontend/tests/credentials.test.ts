import assert from 'node:assert/strict'
import test from 'node:test'
import { connectionAlertType, credentialRequestFailure } from '../src/lib/credentials.ts'

test('请求超时保留保存结果未知，不误报密码错误', () => {
  for (const code of ['ECONNABORTED', 'ETIMEDOUT']) {
    const result = credentialRequestFailure({ code })
    assert.equal(result.status, 'unknown')
    assert.match(result.message, /保存结果尚未确认/)
    assert.match(result.message, /避免重复提交/)
  }
})

test('网络断开和明确的账号拒绝分别处理', () => {
  const network = credentialRequestFailure({ code: 'ERR_NETWORK' })
  assert.equal(network.status, 'unknown')
  assert.match(network.message, /无法连接本机或工作站服务/)
  const rejected = credentialRequestFailure({
    response: { status: 422, data: { detail: '天勤账号或密码错误，请重新输入' } },
  })
  assert.equal(rejected.status, 'invalid')
  assert.match(rejected.message, /账号或密码错误/)
})

test('保留后端服务失败说明，非文本错误使用可读提示', () => {
  assert.deepEqual(credentialRequestFailure({
    response: { status: 503, data: { detail: '天勤连接超时' } },
  }), { status: 'unavailable', message: '天勤连接超时' })
  assert.deepEqual(credentialRequestFailure({
    response: { status: 500, data: { detail: [{ msg: '内部错误' }] } },
  }), { status: 'unavailable', message: '账号验证服务暂不可用，请稍后重试' })
})

test('行情状态只有实际连接成功显示绿色', () => {
  assert.equal(connectionAlertType('connected'), 'success')
  assert.equal(connectionAlertType('disconnected'), 'warning')
  for (const status of ['connecting', 'initializing', 'unknown', undefined] as const) {
    assert.equal(connectionAlertType(status), 'info')
  }
})
