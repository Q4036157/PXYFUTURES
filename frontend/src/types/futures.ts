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
  group_id: number | null
  sort_order: number
  market_status?: 'active' | 'expired' | 'unknown'
}

export interface ContractGroup {
  id: number
  name: string
  sort_order: number
  contracts: ContractConfig[]
}

export interface SignalSnapshot {
  period: PeriodConfig
  trend: { m3: '多' | '空'; m4: '多' | '空' }
  cross_type: '金叉' | '死叉' | null
  cross_time: string | null
  label: string
  alignment_state?: '顺空' | '顺多' | '逆空' | '逆多' | null
  state_since: string | null
  state_bar_count: number
  ma_values: Record<'M1' | 'M2' | 'M3' | 'M4', number>
  bar_close: number
  bar_time: string
  confirmed_bar_time: string
  confirmed_at: string
  data_source: 'live' | 'cache' | 'historical'
  data_status?: 'current' | 'historical' | 'expired'
  data_updated_at: string | null
}

export interface AlertSignalSnapshot extends SignalSnapshot {
  contract_id: number
  symbol: string
  name: string
}

export interface AlertScanResult {
  signals: AlertSignalSnapshot[]
  requested: number
  failed: number
  excluded_contracts?: number
}

export interface AlertStreamSnapshot extends AlertScanResult {
  type: 'snapshot'
  trigger: 'initial' | 'bar_closed'
  source_received_at?: string
  server_sent_at: string
}

export interface ContractSignals {
  contract_id: number
  symbol: string
  signals: SignalSnapshot[]
  warnings?: string[]
  market_status?: 'active' | 'expired' | 'unknown'
}

export interface FollowupSource {
  duration_seconds: number
  period_label: string
  condition: string | null
  state: string
}

export interface FollowupRule {
  id: number
  contract_id: number
  symbol: string
  source: FollowupSource
  source_current?: (FollowupSource & { confirmed_bar_time: string }) | null
  source_changed?: boolean | null
  duration_seconds: number
  conditions: string[]
  states: string[]
  last_triggered_at: string | null
  last_triggered_state?: string | null
  last_bar_time: string | null
  matched: boolean
  trigger_count: number
  status: string
}
