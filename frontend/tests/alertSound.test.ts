import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { alertSoundKinds, StateAlertSound } from '../src/lib/alertSound.ts'

function fixture(autoEnd = true) {
  const played: string[] = []
  const players: FakeAudio[] = []
  let active = 0, overlaps = 0, blocked = 0
  class FakeAudio extends EventTarget {
    muted = false
    currentTime = 0
    preload = ''
    playing = false
    rejectPlay = false
    src: string
    constructor(src: string) { super(); this.src = src }
    play(): Promise<void> {
      if (this.rejectPlay) return Promise.reject(new Error('模拟播放失败'))
      if (this.muted) return Promise.resolve()
      played.push(this.src)
      this.playing = true
      active += 1
      if (active > 1) overlaps += 1
      if (autoEnd) queueMicrotask(() => this.end())
      return Promise.resolve()
    }
    pause(): void {
      if (this.playing) active -= 1
      this.playing = false
    }
    end(): void { this.pause(); this.dispatchEvent(new Event('ended')) }
  }
  const sound = new StateAlertSound({ buy: 'Buy.wav', sell: 'Sell.wav' }, () => { blocked += 1 }, src => {
    const audio = new FakeAudio(src)
    players.push(audio)
    return audio as unknown as HTMLAudioElement
  })
  return { sound, played, players, overlaps: () => overlaps, blocked: () => blocked }
}

const flush = () => new Promise<void>(resolve => setImmediate(resolve))

for (const state of ['上涨', '回调', '多震荡', '下跌', '反弹', '空震荡']) {
  test(`${state}完整播放对应音频5遍`, async () => {
    const { sound, played, overlaps } = fixture()
    sound.playStates([state])
    await flush()
    const expected = ['上涨', '回调', '多震荡'].includes(state) ? 'Buy.wav' : 'Sell.wav'
    assert.deepEqual(played, Array(5).fill(expected))
    assert.equal(overlaps(), 0)
    sound.dispose()
  })
}

test('同批同方向只播5遍，混合方向及随后到达的预警依次完整播放', async () => {
  const { sound, played, overlaps } = fixture()
  sound.playStates(['反弹', '下跌', '上涨', '回调'])
  sound.playStates(['多震荡'])
  await flush()
  assert.deepEqual(played, [...Array(5).fill('Sell.wav'), ...Array(10).fill('Buy.wav')])
  assert.equal(overlaps(), 0)
  sound.dispose()
})

test('静音解锁不算预警次数，立即到达的预警仍完整播放', async () => {
  const { sound, played } = fixture()
  sound.unlock()
  sound.playStates(['下跌'])
  await flush()
  assert.deepEqual(played, Array(5).fill('Sell.wav'))
  sound.dispose()
})

test('一遍结束前不启动下一遍，关闭页面取消当前声音和待播放队列', async () => {
  const { sound, played, players } = fixture(false)
  sound.playStates(['上涨', '下跌'])
  await flush()
  assert.deepEqual(played, ['Buy.wav'])
  players[0]!.end()
  await flush()
  assert.deepEqual(played, ['Buy.wav', 'Buy.wav'])
  sound.dispose()
  await flush()
  assert.equal(players[0]!.playing, false)
  assert.equal(played.length, 2)
  sound.playStates(['下跌'])
  await flush()
  assert.equal(played.length, 2)
})

test('播放失败不会无限重试，未知状态不猜声音，之后新预警可重试', async () => {
  const { sound, played, players, blocked } = fixture()
  assert.deepEqual(alertSoundKinds([null, undefined, '', '未知']), [])
  sound.unlock()
  await flush()
  players.forEach(audio => { audio.rejectPlay = true })
  sound.playStates(['下跌', '上涨'])
  await flush()
  assert.equal(blocked(), 1)
  assert.equal(played.length, 0)
  players.forEach(audio => { audio.rejectPlay = false })
  sound.playStates(['反弹'])
  await flush()
  assert.deepEqual(played, Array(5).fill('Sell.wav'))
  sound.dispose()
})

test('两份用户音频均为有效WAV文件', () => {
  for (const name of ['Buy', 'Sell']) {
    const data = readFileSync(new URL(`../src/assets/${name}.wav`, import.meta.url))
    assert.equal(data.toString('ascii', 0, 4), 'RIFF')
    assert.equal(data.toString('ascii', 8, 12), 'WAVE')
    assert.ok(data.length > 1000)
  }
})
