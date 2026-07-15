export interface PeriodConfig {
  id: number | null
  label: string
  duration_seconds: number
  m4: number
  m3: number
  m2: number
  m1: number
  note: string
}

export interface ContractConfig {
  id: number
  symbol: string
  exchange: string
  code: string
  name: string
  periods: PeriodConfig[]
}

export interface SignalSnapshot {
  period: PeriodConfig
  trend: { m3: '多' | '空'; m4: '多' | '空' }
  cross_type: '金叉' | '死叉' | null
  label: string
  state_since: string | null
  ma_values: Record<'M1' | 'M2' | 'M3' | 'M4', number>
  bar_close: number
  bar_time: string
  data_source: 'live' | 'cache'
  data_updated_at: string | null
}

export interface ContractSignals {
  contract_id: number
  symbol: string
  signals: SignalSnapshot[]
}
