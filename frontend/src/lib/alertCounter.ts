interface CountableSignal {
  contract_id: number
  symbol: string
  period: { duration_seconds: number }
  cross_type: string | null
  cross_time: string | null
}

interface CounterEntry {
  time: number
  count: number
}

export function mergeAlertSnapshot<T extends CountableSignal & { confirmed_bar_time: string | null }>(
  previous: T[], incoming: T[],
): T[] {
  const previousRows = new Map(previous.map((signal) => [alertRowKey(signal), signal]))
  const rows = new Map<string, T>()
  for (const signal of incoming) {
    const key = alertRowKey(signal)
    const current = rows.get(key) ?? previousRows.get(key)
    const older = current && Date.parse(signal.confirmed_bar_time ?? '') < Date.parse(current.confirmed_bar_time ?? '')
    rows.set(key, older ? current : signal)
  }
  return [...rows.values()]
}

export function alertRowKey(signal: CountableSignal): string {
  return JSON.stringify([signal.contract_id, signal.symbol, signal.period.duration_seconds])
}

export function alertEventKey(signal: CountableSignal): string {
  return JSON.stringify([alertRowKey(signal), signal.cross_type, Date.parse(signal.cross_time ?? '')])
}

// 每行只保留最新确认时间，重复和迟到事件均不累计。
export class AlertCounter {
  private readonly rows = new Map<string, CounterEntry>()

  accept(signal: CountableSignal): boolean {
    const time = Date.parse(signal.cross_time ?? '')
    if (!signal.cross_type || !Number.isFinite(time)) return false
    const key = alertRowKey(signal)
    const previous = this.rows.get(key)
    if (previous && time <= previous.time) return false
    this.rows.set(key, { time, count: (previous?.count ?? 0) + 1 })
    return true
  }

  count(signal: CountableSignal): number {
    const entry = this.rows.get(alertRowKey(signal))
    return entry?.time === Date.parse(signal.cross_time ?? '') ? entry.count : 0
  }

  retain(contractKeys: Set<string>): void {
    for (const key of this.rows.keys()) {
      const [id, symbol] = JSON.parse(key) as [number, string, number]
      if (!contractKeys.has(JSON.stringify([id, symbol]))) this.rows.delete(key)
    }
  }
}
