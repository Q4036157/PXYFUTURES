export type AlertSoundKind = 'buy' | 'sell'

export function alertSoundKinds(states: readonly (string | null | undefined)[]): AlertSoundKind[] {
  const kinds: AlertSoundKind[] = []
  for (const state of states) {
    const kind = ['上涨', '回调', '多震荡'].includes(state || '') ? 'buy'
      : ['下跌', '反弹', '空震荡'].includes(state || '') ? 'sell' : null
    if (kind && !kinds.includes(kind)) kinds.push(kind)
  }
  return kinds
}

export class StateAlertSound {
  private urls: Record<AlertSoundKind, string>
  private onBlocked: () => void
  private createAudio: (url: string) => HTMLAudioElement
  private players = new Map<AlertSoundKind, HTMLAudioElement>()
  private queue: AlertSoundKind[] = []
  private unlockTask: Promise<void> | null = null
  private unlocked = false
  private running = false
  private disposed = false
  private cancelCurrent: (() => void) | null = null

  constructor(
    urls: Record<AlertSoundKind, string>, onBlocked: () => void = () => undefined,
    createAudio: (url: string) => HTMLAudioElement = url => new Audio(url),
  ) {
    this.urls = urls
    this.onBlocked = onBlocked
    this.createAudio = createAudio
  }

  private player(kind: AlertSoundKind): HTMLAudioElement {
    let audio = this.players.get(kind)
    if (!audio) {
      audio = this.createAudio(this.urls[kind])
      audio.preload = 'auto'
      this.players.set(kind, audio)
    }
    return audio
  }

  unlock(): void {
    if (this.disposed || this.unlocked || this.unlockTask || this.running) return
    // 用户手势内静音解锁两种音频；完成后才开始正式播放，避免中途被暂停。
    this.unlockTask = Promise.allSettled((['buy', 'sell'] as const).map(async kind => {
      const audio = this.player(kind)
      audio.muted = true
      try {
        await audio.play()
        audio.pause()
        audio.currentTime = 0
      } finally {
        audio.muted = false
      }
    })).then(results => {
      this.unlocked = results.every(result => result.status === 'fulfilled')
    }).finally(() => { this.unlockTask = null })
  }

  playStates(states: readonly (string | null | undefined)[]): void {
    if (this.disposed) return
    this.queue.push(...alertSoundKinds(states))
    void this.drain()
  }

  private async drain(): Promise<void> {
    if (this.running || this.disposed || !this.queue.length) return
    this.running = true
    try {
      await this.unlockTask
      while (this.queue.length && !this.disposed) {
        const audio = this.player(this.queue.shift()!)
        // 等待整段音频结束后再重复，不按固定间隔截断文件。
        for (let count = 0; count < 5 && !this.disposed; count += 1) await this.playOnce(audio)
      }
    } catch {
      this.queue = []
      if (!this.disposed) this.onBlocked()
    } finally {
      this.running = false
    }
  }

  private playOnce(audio: HTMLAudioElement): Promise<void> {
    return new Promise((resolve, reject) => {
      let finished = false
      const clean = () => {
        finished = true
        audio.removeEventListener('ended', ended)
        audio.removeEventListener('error', failed)
        this.cancelCurrent = null
      }
      const ended = () => { if (!finished) { clean(); resolve() } }
      const failed = () => { if (!finished) { clean(); audio.pause(); reject(new Error('预警声音播放失败')) } }
      this.cancelCurrent = () => { audio.pause(); ended() }
      audio.addEventListener('ended', ended)
      audio.addEventListener('error', failed)
      audio.currentTime = 0
      audio.muted = false
      try { void audio.play().catch(failed) } catch { failed() }
    })
  }

  dispose(): void {
    this.disposed = true
    this.queue = []
    this.cancelCurrent?.()
    for (const audio of this.players.values()) audio.pause()
    this.players.clear()
  }
}
