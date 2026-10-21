import assert from 'node:assert/strict'
import test from 'node:test'
import { AlertCounter, alertEventKey, mergeAlertSnapshot } from '../src/lib/alertCounter.ts'

function signal(time = '2026-10-22T09:00:00+08:00') {
  return { contract_id: 1, symbol: 'CZCE.FG701', period: { duration_seconds: 300 }, cross_type: '金叉', cross_time: time, confirmed_bar_time: time }
}

test('同一信号重复、同批重复、时间格式等价及乱序消息只累计一次', () => {
  const counter = new AlertCounter()
  const first = signal()
  assert.equal(counter.accept(first), true)
  for (let i = 0; i < 3; i++) assert.equal(counter.accept(first), false)
  assert.equal(counter.accept(signal('2026-10-22T01:00:00Z')), false)
  assert.equal(counter.count(first), 1)
  const next = signal('2026-10-22T09:05:00+08:00')
  assert.equal(counter.accept(next), true)
  assert.equal(counter.accept(first), false)
  assert.equal(counter.count(next), 2)
  assert.equal(counter.accept({ ...next, cross_type: '死叉' }), false)
  assert.equal(alertEventKey(first), alertEventKey(signal('2026-10-22T01:00:00Z')))
})

test('不同合约、周期及更换代码独立计数，无效事件不累计', () => {
  const counter = new AlertCounter()
  for (const item of [signal(), { ...signal(), contract_id: 2 }, { ...signal(), period: { duration_seconds: 600 } }, { ...signal(), symbol: 'CZCE.FG705' }]) {
    assert.equal(counter.accept(item), true)
    assert.equal(counter.count(item), 1)
  }
  assert.equal(counter.accept(signal('invalid')), false)
  assert.equal(counter.accept({ ...signal(), cross_type: null }), false)
  assert.equal(counter.accept({ ...signal(), cross_time: null }), false)
})

test('移除合约释放去重状态，其他合约继续累计', () => {
  const counter = new AlertCounter()
  const other = { ...signal(), contract_id: 2 }
  counter.accept(signal())
  counter.accept(other)
  counter.retain(new Set([JSON.stringify([other.contract_id, other.symbol])]))
  assert.equal(counter.count(signal()), 0)
  assert.equal(counter.accept(signal()), true)
  assert.equal(counter.accept({ ...other, cross_time: '2026-10-22T09:05:00+08:00' }), true)
  assert.equal(counter.count({ ...other, cross_time: '2026-10-22T09:05:00+08:00' }), 2)
})

test('迟到快照不覆盖较新确认K线，同批重复合并、缺失行正常移除', () => {
  const first = signal()
  const next = signal('2026-10-22T09:05:00+08:00')
  assert.deepEqual(mergeAlertSnapshot([next], [first, first]), [next])
  assert.deepEqual(mergeAlertSnapshot([first], [next, first]), [next])
  assert.deepEqual(mergeAlertSnapshot([first], []), [])
  assert.deepEqual(mergeAlertSnapshot([first], [{ ...first, cross_type: '死叉' }]), [{ ...first, cross_type: '死叉' }])
})
