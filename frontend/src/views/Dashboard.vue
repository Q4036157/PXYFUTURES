<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowDown, ArrowUp, Back, Close, Delete, EditPen, Plus, Refresh, Setting } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { alertWebSocketUrl, client } from '@/api/client'
import { recognizeContract } from '@/lib/contractCatalog'
import { AlertCounter, alertEventKey, mergeAlertSnapshot } from '@/lib/alertCounter'
import alertSoundUrl from '@/assets/ok3.wav?url'
import type {
  AlertScanResult,
  AlertSignalSnapshot,
  AlertStreamSnapshot,
  ContractConfig,
  ContractGroup,
  ContractSignals,
  PeriodConfig,
  SignalSnapshot,
} from '@/types/futures'

type AlertCondition =
  | '多多金叉' | '多空金叉' | '空多金叉' | '空空金叉'
  | '多多死叉' | '多空死叉' | '空多死叉' | '空空死叉'

type AlertState = '上涨' | '回调' | '多震荡' | '下跌' | '反弹' | '空震荡'
type PanelKey = 'groups' | 'setup' | 'signals' | 'alerts'
type PanelSize = { width?: number; height?: number }

const contracts = ref<ContractConfig[]>([])
const contractGroups = ref<ContractGroup[]>([])
const activeGroupId = ref<number | null>(null)
const activeGroup = computed(() => contractGroups.value.find((group) => group.id === activeGroupId.value))
const selectedId = ref<number | null>(null)
const signals = ref<SignalSnapshot[]>([])
const periodNotes = ref<Record<number, string>>({})
const selectedDuration = ref<number | null>(null)
const refreshing = ref(false)
const errorText = ref('')
const adding = ref(false)
const changingContract = ref(false)
const suggestionLoading = ref(false)
const addVisible = ref(false)
const contractEditVisible = ref(false)
const periodVisible = ref(false)
const credentialsVisible = ref(false)
const editPeriod = ref<PeriodConfig | null>(null)
const editingContract = ref<ContractConfig | null>(null)
const form = ref({ exchange: '', code: '', name: '', group_id: null as number | null })
const contractEditForm = ref({ exchange: '', code: '', name: '' })
const periodForm = ref({ label: '日线', duration_seconds: 86400, m4: 240, m3: 60, m2: 21, m1: 4 })
const credentials = ref({ username: '', password: '' })
let timer: number | undefined
let contractStatusTimer: number | undefined
let alertTimer: number | undefined
let alertSocketReconnectTimer: number | undefined
let alertSocketHeartbeatTimer: number | undefined
interface RequestController {
  readonly aborted: boolean
  signal?: AbortSignal
  abort: () => void
}

interface TqCredentialStatus {
  configured: boolean
  status: 'unconfigured' | 'unknown' | 'valid' | 'invalid' | 'unavailable'
  checked_at: string | null
  message: string
  source: string
}

let signalAbortController: RequestController | null = null
let refreshingContractId: number | null = null
const dirtyNoteDurations = new Set<number>()
const savingNoteKeys = new Set<string>()
const pendingNoteValues = new Map<string, string>()
const noteSaveTimers = new Map<string, number>()
const selectedAlertDurations = ref<number[]>([])
const selectedAlertConditions = ref<AlertCondition[]>([])
const selectedAlertStates = ref<AlertState[]>([])
const alertSignals = ref<AlertSignalSnapshot[]>([])
const alertScanning = ref(false)
const alertScanError = ref('')
const alertFailedCount = ref(0)
const alertExcludedCount = ref(0)
const alertPopupSignals = ref<AlertSignalSnapshot[]>([])
const alertPopupVisible = ref(false)
const alertPopupBeijingTime = ref('')
const contractOrderSaving = ref(false)
let contractLoadVersion = 0
const alertStreamConnected = ref(false)
const notificationPermission = ref<NotificationPermission | 'unsupported'>(
  typeof Notification === 'undefined' ? 'unsupported' : Notification.permission,
)
const credentialStatus = ref<TqCredentialStatus | null>(null)
const credentialsSaving = ref(false)
const alertCounter = new AlertCounter()
const soundTimers = new Set<number>()
const SIGNAL_WARNING_RETRY_MS = 15 * 60 * 1_000
const ALERT_FRESHNESS_MS = 3 * 60 * 1_000
const ALERT_SAFETY_SCAN_MS = 5_000
let alertAudio: HTMLAudioElement | null = null
let alertRescanPending = false
let alertSocket: WebSocket | null = null
let alertSocketReconnectDelay = 1_000
let dashboardMounted = false
let signalRetryAfter = 0
const platformReturnVisible = window.location.pathname === '/futures-app'
  || window.location.pathname.startsWith('/futures-app/')
  || window.self !== window.top

const alertStateOptions: AlertState[] = ['上涨', '回调', '多震荡', '下跌', '反弹', '空震荡']
const alertPreferencesKey = 'pxy-futures-alert-preferences-v1'
const panelLayoutKey = 'pxy-futures-panel-layout-v3'
const collapsedPanels = ref<Record<PanelKey, boolean>>({
  groups: false,
  setup: false,
  signals: false,
  alerts: false,
})
const panelSizes = ref<Record<PanelKey, PanelSize>>({
  groups: {},
  setup: {},
  signals: {},
  alerts: {},
})
const groupsPanelRef = ref<HTMLElement | null>(null)
const setupPanelRef = ref<HTMLElement | null>(null)
const signalsPanelRef = ref<HTMLElement | null>(null)
const alertsPanelRef = ref<HTMLElement | null>(null)
const panelRefs = {
  groups: groupsPanelRef,
  setup: setupPanelRef,
  signals: signalsPanelRef,
  alerts: alertsPanelRef,
}
let panelResizeObserver: ResizeObserver | null = null
const draggedContractId = ref<number | null>(null)
const draggedSourceGroupId = ref<number | null>(null)

const commonPeriods = [
  { label: '日线', durationSeconds: 86400 },
  { label: '4小时', durationSeconds: 14400 },
  { label: '2小时', durationSeconds: 7200 },
  { label: '1小时', durationSeconds: 3600 },
  { label: '30分钟', durationSeconds: 1800 },
  { label: '15分钟', durationSeconds: 900 },
  { label: '10分钟', durationSeconds: 600 },
  { label: '5分钟', durationSeconds: 300 },
  { label: '3分钟', durationSeconds: 180 },
]

const alertConditionOptions: AlertCondition[] = [
  '多多金叉', '多空金叉', '空多金叉', '空空金叉',
  '多多死叉', '多空死叉', '空多死叉', '空空死叉',
]

const selectedContract = computed(() => contracts.value.find((item) => item.id === selectedId.value) || null)
const selectedSignal = computed(() => (
  signals.value.find((signal) => signal.period.duration_seconds === selectedDuration.value)
  || signals.value[0]
  || null
))
const recognition = computed(() => recognizeContract(form.value.code))
const contractEditRecognition = computed(() => recognizeContract(contractEditForm.value.code))
const matchingAlertSignals = computed(() => {
  const durations = new Set(selectedAlertDurations.value)
  const conditions = new Set(selectedAlertConditions.value)
  const states = new Set(selectedAlertStates.value)
  return alertSignals.value
    .filter((signal) => {
      const condition = alertCondition(signal)
      const state = alertState(signal)
      const matchesCondition = !conditions.size || (condition !== null && conditions.has(condition))
      const matchesState = !states.size || (state !== null && states.has(state))
      return durations.has(signal.period.duration_seconds) && matchesCondition && matchesState
    })
    .sort((left, right) => {
      const latestEntryFirst = Date.parse(right.state_since ?? '') - Date.parse(left.state_since ?? '')
      if (Number.isFinite(latestEntryFirst) && latestEntryFirst !== 0) return latestEntryFirst
      const longerStateFirst = right.state_bar_count - left.state_bar_count
      if (longerStateFirst) return longerStateFirst
      const durationOrder = commonPeriods.findIndex(
        (period) => period.durationSeconds === left.period.duration_seconds,
      ) - commonPeriods.findIndex(
        (period) => period.durationSeconds === right.period.duration_seconds,
      )
      return durationOrder || left.symbol.localeCompare(right.symbol)
    })
})

const alertStatusText = computed(() => {
  if (!selectedAlertDurations.value.length) {
    return '预警未开启，请选择监测周期'
  }
  if (alertScanning.value) return '正在扫描自选合约'
  if (alertScanError.value) return alertScanError.value
  if (alertFailedCount.value) {
    return `当前匹配 ${matchingAlertSignals.value.length} 条，${alertFailedCount.value} 条等待行情缓存`
  }
  const channel = alertStreamConnected.value ? '实时推送 · 5秒校验' : '连接恢复中 · 5秒校验'
  const excluded = alertExcludedCount.value ? ` · 已排除 ${alertExcludedCount.value} 个到期合约` : ''
  return `当前匹配 ${matchingAlertSignals.value.length} 条 · ${channel}${excluded}`
})

function alertCondition(signal: AlertSignalSnapshot): AlertCondition | null {
  if (!signal.cross_type) return null
  return `${signal.trend.m4}${signal.trend.m3}${signal.cross_type}` as AlertCondition
}

function alertState(signal: AlertSignalSnapshot): AlertState | null {
  return alertStateOptions.includes(signal.label as AlertState) ? signal.label as AlertState : null
}

function alertText(signal: AlertSignalSnapshot): string {
  return `${signal.symbol} · ${signal.period.label} · ${alertCondition(signal)} · ${signal.label}`
}

function alertCount(signal: AlertSignalSnapshot): number {
  return signal.state_bar_count
}

function isFreshAlertSignal(signal: AlertSignalSnapshot): boolean {
  if (signal.data_status === 'historical' || signal.data_status === 'expired' || signal.data_source === 'historical') return false
  const confirmedAt = Date.parse(signal.confirmed_at)
  if (!Number.isFinite(confirmedAt)) return false
  const age = Date.now() - confirmedAt
  const maxAge = signal.period.duration_seconds >= 86_400 ? 7 * 86_400_000 : ALERT_FRESHNESS_MS
  return age >= -10_000 && age <= maxAge
}

function persistAlertPreferences(): void {
  try {
    localStorage.setItem(alertPreferencesKey, JSON.stringify({
      durations: selectedAlertDurations.value,
      conditions: selectedAlertConditions.value,
      states: selectedAlertStates.value,
    }))
  } catch {
    // 无法持久化时，当前页面的监测设置仍然有效。
  }
}

function loadAlertPreferences(): void {
  try {
    const saved = JSON.parse(localStorage.getItem(alertPreferencesKey) || '{}')
    if (Array.isArray(saved.durations)) selectedAlertDurations.value = [...new Set<number>(
      saved.durations.filter((value: unknown): value is number =>
        typeof value === 'number' && commonPeriods.some((period) => period.durationSeconds === value)),
    )]
    if (Array.isArray(saved.conditions)) selectedAlertConditions.value = [...new Set<AlertCondition>(
      saved.conditions.filter((value: AlertCondition) => alertConditionOptions.includes(value)),
    )]
    if (Array.isArray(saved.states)) selectedAlertStates.value = [...new Set<AlertState>(
      saved.states.filter((value: AlertState) => alertStateOptions.includes(value)),
    )]
  } catch {
    // 未保存、损坏或禁用存储时保持未开启，不擅自改变用户预警选择。
  }
}

function enableAllAlertPeriods(): void {
  enableBrowserAlerts()
  selectedAlertDurations.value = commonPeriods.map((period) => period.durationSeconds)
  persistAlertPreferences()
  syncAlertSubscription()
  if (!alertStreamConnected.value) void refreshAlertSignals()
}

function getAlertAudio(): HTMLAudioElement {
  if (!alertAudio) {
    alertAudio = new Audio(alertSoundUrl)
    alertAudio.preload = 'auto'
  }
  return alertAudio
}

function enableBrowserAlerts(): void {
  const audio = getAlertAudio()
  audio.muted = true
  void audio.play().then(() => {
    audio.pause()
    audio.currentTime = 0
    audio.muted = false
  }).catch(() => undefined)

  if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
    void Notification.requestPermission().then((permission) => {
      notificationPermission.value = permission
    })
  }
}

function toggleAlertDuration(durationSeconds: number): void {
  enableBrowserAlerts()
  selectedAlertDurations.value = selectedAlertDurations.value.includes(durationSeconds)
    ? selectedAlertDurations.value.filter((value) => value !== durationSeconds)
    : [...selectedAlertDurations.value, durationSeconds]
  persistAlertPreferences()
  syncAlertSubscription()
  if (!alertStreamConnected.value) void refreshAlertSignals()
}

function toggleAlertCondition(condition: AlertCondition): void {
  enableBrowserAlerts()
  selectedAlertConditions.value = selectedAlertConditions.value.includes(condition)
    ? selectedAlertConditions.value.filter((value) => value !== condition)
    : [...selectedAlertConditions.value, condition]
  persistAlertPreferences()
  syncAlertSubscription()
  if (!alertStreamConnected.value) void refreshAlertSignals()
}

function toggleAlertState(state: AlertState): void {
  enableBrowserAlerts()
  selectedAlertStates.value = selectedAlertStates.value.includes(state)
    ? selectedAlertStates.value.filter((value) => value !== state)
    : [...selectedAlertStates.value, state]
  persistAlertPreferences()
  syncAlertSubscription()
  if (!alertStreamConnected.value) void refreshAlertSignals()
}

function playAlertSound(): void {
  const audio = getAlertAudio()
  for (let index = 0; index < 5; index += 1) {
    const soundTimer = window.setTimeout(() => {
      soundTimers.delete(soundTimer)
      audio.pause()
      audio.currentTime = 0
      audio.muted = false
      void audio.play().catch(() => {
        alertScanError.value = '浏览器已阻止声音，请点击任一预警按钮后继续监测'
      })
    }, index * 1_000)
    soundTimers.add(soundTimer)
  }
}

function currentBeijingTime(): string {
  const time = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).format(new Date())
  return `北京时间 ${time}`
}

function closeAlertPopup(): void {
  alertPopupVisible.value = false
}

function showAlertNotification(items: AlertSignalSnapshot[]): void {
  const beijingTime = currentBeijingTime()
  alertPopupBeijingTime.value = beijingTime
  playAlertSound()
  if (!document.hidden) {
    alertPopupSignals.value = items
    alertPopupVisible.value = true
    return
  }

  if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
    const preview = items.slice(0, 5).map(alertText).join('\n')
    const more = items.length > 5 ? `\n另有 ${items.length - 5} 条信号` : ''
    new Notification(`智能期货预警 · ${items.length}条新信号`, {
      body: `${beijingTime}\n${preview}${more}`,
      tag: 'pxy-futures-alert',
    })
  }
}

function applyAlertSnapshot(data: AlertScanResult, allowNotification = true): void {
  alertSignals.value = mergeAlertSnapshot(alertSignals.value, data.signals)
  alertFailedCount.value = data.failed
  alertExcludedCount.value = data.excluded_contracts ?? 0
  if (!allowNotification) return

  const selectedConditions = new Set(selectedAlertConditions.value)
  const selectedStates = new Set(selectedAlertStates.value)
  const newSignals = [...alertSignals.value].sort((left, right) =>
    Date.parse(left.cross_time ?? '') - Date.parse(right.cross_time ?? ''),
  ).filter((signal) => {
    const condition = alertCondition(signal)
    const state = alertState(signal)
    if (selectedConditions.size && (!condition || !selectedConditions.has(condition))) return false
    if (selectedStates.size && (!state || !selectedStates.has(state))) return false
    if (!selectedAlertDurations.value.includes(signal.period.duration_seconds)) return false
    if (!signal.cross_time || signal.cross_time !== signal.confirmed_bar_time) return false
    if (!isFreshAlertSignal(signal)) return false
    return alertCounter.accept(signal)
  })
  if (newSignals.length) {
    showAlertNotification(newSignals)
  }
}

async function refreshAlertSignals(allowNotification = true): Promise<void> {
  if (alertScanning.value) {
    alertRescanPending = true
    return
  }
  if (!selectedAlertDurations.value.length) {
    alertSignals.value = []
    alertFailedCount.value = 0
    alertScanError.value = ''
    return
  }

  alertScanning.value = true
  alertScanError.value = ''
  try {
    const { data } = await client.post<AlertScanResult>('/contracts/alerts/scan', {
      durations: selectedAlertDurations.value,
    })
    applyAlertSnapshot(data, allowNotification)
  } catch (error: any) {
    alertScanError.value = error.response?.data?.detail || '预警扫描暂时不可用'
  } finally {
    alertScanning.value = false
    if (alertRescanPending) {
      alertRescanPending = false
      void refreshAlertSignals(allowNotification)
    }
  }
}

function monitoredAlertDurations(): number[] {
  return selectedAlertDurations.value
}

function syncAlertSubscription(): void {
  if (alertSocket?.readyState !== WebSocket.OPEN) return
  alertSocket.send(JSON.stringify({
    type: 'subscribe',
    durations: monitoredAlertDurations(),
  }))
}

function clearAlertSocketTimers(): void {
  if (alertSocketReconnectTimer) {
    window.clearTimeout(alertSocketReconnectTimer)
    alertSocketReconnectTimer = undefined
  }
  if (alertSocketHeartbeatTimer) {
    window.clearInterval(alertSocketHeartbeatTimer)
    alertSocketHeartbeatTimer = undefined
  }
}

function scheduleAlertSocketReconnect(): void {
  if (!dashboardMounted || alertSocketReconnectTimer) return
  alertSocketReconnectTimer = window.setTimeout(() => {
    alertSocketReconnectTimer = undefined
    connectAlertStream()
  }, alertSocketReconnectDelay)
  alertSocketReconnectDelay = Math.min(15_000, alertSocketReconnectDelay * 2)
}

function connectAlertStream(): void {
  if (!dashboardMounted || alertSocket?.readyState === WebSocket.OPEN
    || alertSocket?.readyState === WebSocket.CONNECTING) return

  const socket = new WebSocket(alertWebSocketUrl())
  alertSocket = socket
  socket.addEventListener('open', () => {
    if (alertSocket !== socket) return
    alertStreamConnected.value = true
    alertScanError.value = ''
    alertSocketReconnectDelay = 1_000
    syncAlertSubscription()
    if (alertSocketHeartbeatTimer) window.clearInterval(alertSocketHeartbeatTimer)
    alertSocketHeartbeatTimer = window.setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'ping' }))
      }
    }, 20_000)
  })
  socket.addEventListener('message', (event) => {
    if (alertSocket !== socket) return
    try {
      const message = JSON.parse(String(event.data)) as AlertStreamSnapshot | { type: string }
      if (message.type === 'snapshot') {
        applyAlertSnapshot(message as AlertStreamSnapshot)
        alertScanning.value = false
      } else if (message.type === 'invalid_subscription') {
        alertScanError.value = '预警周期配置无效'
      }
    } catch {
      alertScanError.value = '实时预警消息解析失败'
    }
  })
  socket.addEventListener('close', (event) => {
    if (alertSocket !== socket) return
    alertSocket = null
    alertStreamConnected.value = false
    if (alertSocketHeartbeatTimer) {
      window.clearInterval(alertSocketHeartbeatTimer)
      alertSocketHeartbeatTimer = undefined
    }
    if (event.code === 4401) {
      const loginUrl = `/login?redirect=${encodeURIComponent('/apps/launch/futures')}`
      const targetWindow = window.top ?? window
      targetWindow.location.replace(loginUrl)
      return
    }
    scheduleAlertSocketReconnect()
  })
  socket.addEventListener('error', () => socket.close())
}

function createRequestController(): RequestController {
  if (typeof AbortController !== 'undefined') {
    const controller = new AbortController()
    return {
      get aborted() { return controller.signal.aborted },
      signal: controller.signal,
      abort: () => controller.abort(),
    }
  }

  let aborted = false
  return {
    get aborted() { return aborted },
    abort: () => { aborted = true },
  }
}

interface ContractSuggestion {
  value: string
  display: string
  exchange: string
  symbol: string
}

function maText(name: 'M1' | 'M2' | 'M3' | 'M4'): string {
  const value = selectedSignal.value?.ma_values[name]
  return typeof value === 'number' ? value.toFixed(2) : '--'
}

function longTrendText(name: 'm3' | 'm4'): string {
  const trend = selectedSignal.value?.trend[name]
  return trend ? `（${trend}）` : ''
}

function dataStatusText(signal: SignalSnapshot): string {
  if (signal.data_status === 'expired' || signal.data_source === 'historical') return '到期合约 · 历史行情'
  if (signal.data_status === 'historical') return '历史行情 · 非实时'
  const syncedAt = signal.data_updated_at?.replace('T', ' ').slice(0, 19)
  const source = signal.data_source === 'live' ? '行情连接正常' : '本地缓存'
  return syncedAt ? `${source} · 同步时间 ${syncedAt}` : source
}

function displayContractCode(code: string): string {
  return /^V\d/.test(code) ? `PVC${code.slice(1)}` : code
}

function syncPeriodNotes(items: SignalSnapshot[]): void {
  for (const signal of items) {
    const duration = signal.period.duration_seconds
    if (!dirtyNoteDurations.has(duration)) {
      periodNotes.value[duration] = signal.period.note || ''
    }
  }
}

function noteKey(contractId: number, durationSeconds: number): string {
  return `${contractId}:${durationSeconds}`
}

async function persistPeriodNote(
  contractId: number,
  durationSeconds: number,
  note: string,
): Promise<void> {
  const key = noteKey(contractId, durationSeconds)
  if (savingNoteKeys.has(key)) {
    pendingNoteValues.set(key, note)
    return
  }
  savingNoteKeys.add(key)
  try {
    await client.put(`/contracts/${contractId}/periods/${durationSeconds}/note`, { note })
    if (selectedId.value === contractId && periodNotes.value[durationSeconds] === note) {
      dirtyNoteDurations.delete(durationSeconds)
      const signal = signals.value.find((item) => item.period.duration_seconds === durationSeconds)
      if (signal) signal.period.note = note
    }
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '备注保存失败')
  } finally {
    savingNoteKeys.delete(key)
    const pendingNote = pendingNoteValues.get(key)
    if (pendingNote !== undefined) {
      pendingNoteValues.delete(key)
      if (pendingNote !== note) void persistPeriodNote(contractId, durationSeconds, pendingNote)
    }
  }
}

function schedulePeriodNoteSave(signal: SignalSnapshot, value: string): void {
  const contractId = selectedId.value
  if (!contractId) return
  const duration = signal.period.duration_seconds
  const key = noteKey(contractId, duration)
  periodNotes.value[duration] = value
  dirtyNoteDurations.add(duration)
  const previousTimer = noteSaveTimers.get(key)
  if (previousTimer) window.clearTimeout(previousTimer)
  const timer = window.setTimeout(() => {
    noteSaveTimers.delete(key)
    void persistPeriodNote(contractId, duration, value)
  }, 600)
  noteSaveTimers.set(key, timer)
}

function flushPeriodNote(signal: SignalSnapshot): void {
  const contractId = selectedId.value
  if (!contractId) return
  const duration = signal.period.duration_seconds
  if (!dirtyNoteDurations.has(duration)) return
  const key = noteKey(contractId, duration)
  const timer = noteSaveTimers.get(key)
  if (timer) {
    window.clearTimeout(timer)
    noteSaveTimers.delete(key)
  }
  void persistPeriodNote(contractId, duration, periodNotes.value[duration] || '')
}

async function fetchContractSuggestions(
  query: string,
  callback: (items: ContractSuggestion[]) => void,
): Promise<void> {
  if (!recognizeContract(query).exchange) {
    callback([])
    return
  }
  suggestionLoading.value = true
  try {
    const { data } = await client.get<ContractSuggestion[]>('/contracts/suggestions', { params: { query } })
    callback(data.map((item) => ({ ...item, display: displayContractCode(item.value) })))
  } catch {
    callback([])
  } finally {
    suggestionLoading.value = false
  }
}

function selectContractSuggestion(item: ContractSuggestion): void {
  form.value.code = item.display
  form.value.exchange = item.exchange
}

function openContractEdit(contract: ContractConfig): void {
  editingContract.value = contract
  contractEditForm.value = {
    exchange: contract.exchange,
    code: displayContractCode(contract.code),
    name: contract.name,
  }
  contractEditVisible.value = true
}

function handleContractEditCodeInput(): void {
  const result = recognizeContract(contractEditForm.value.code)
  contractEditForm.value.code = contractEditForm.value.code.replace(/\s+/g, '').toUpperCase()
  if (result.exchange) contractEditForm.value.exchange = result.exchange
}

function selectContractEditSuggestion(item: ContractSuggestion): void {
  contractEditForm.value.code = item.display
  contractEditForm.value.exchange = item.exchange
}

function flattenContractGroups(groups: ContractGroup[]): ContractConfig[] {
  return groups.flatMap((group) => group.contracts)
}

async function loadContracts(allowDuringOrderSave = false): Promise<void> {
  if (contractOrderSaving.value && !allowDuringOrderSave) return
  const version = ++contractLoadVersion
  const { data } = await client.get<ContractGroup[]>('/contracts/groups')
  if (version !== contractLoadVersion || (contractOrderSaving.value && !allowDuringOrderSave)) return
  contractGroups.value = data
  contracts.value = flattenContractGroups(data)
  alertCounter.retain(new Set(contracts.value.map((contract) => JSON.stringify([contract.id, contract.symbol]))))
  if (!data.some((group) => group.id === activeGroupId.value)) {
    activeGroupId.value = data[0]?.id || null
  }
  if (!contracts.value.some((contract) => contract.id === selectedId.value) && contracts.value.length) {
    await selectContract(contracts.value[0]!.id)
  }
}

function openAddContract(groupId: number | null = activeGroupId.value): void {
  form.value = { exchange: '', code: '', name: '', group_id: groupId }
  addVisible.value = true
}

async function createGroup(): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt('请输入分组名称', '添加分组', {
      confirmButtonText: '添加',
      cancelButtonText: '取消',
      inputValue: `自定义${contractGroups.value.length + 1}`,
      inputPattern: /\S+/,
      inputErrorMessage: '分组名称不能为空',
    })
    const { data } = await client.post<ContractGroup>('/contracts/groups', { name: value.trim() })
    contractGroups.value.push(data)
    activeGroupId.value = data.id
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(error.response?.data?.detail || '添加分组失败')
    }
  }
}

async function renameGroup(group: ContractGroup): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt('请输入新的分组名称', '重命名分组', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValue: group.name,
      inputPattern: /\S+/,
      inputErrorMessage: '分组名称不能为空',
    })
    const { data } = await client.put<ContractGroup>(`/contracts/groups/${group.id}`, { name: value.trim() })
    const index = contractGroups.value.findIndex((item) => item.id === group.id)
    if (index >= 0) contractGroups.value[index] = data
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(error.response?.data?.detail || '重命名分组失败')
    }
  }
}

function beginContractDrag(contract: ContractConfig, groupId: number, event: DragEvent): void {
  if (contractOrderSaving.value) {
    event.preventDefault()
    return
  }
  draggedContractId.value = contract.id
  draggedSourceGroupId.value = groupId
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', String(contract.id))
  }
}

function finishContractDrag(): void {
  draggedContractId.value = null
  draggedSourceGroupId.value = null
}

function allowContractDrop(event: DragEvent): void {
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
}

function navigateGroupTabs(event: KeyboardEvent, index: number): void {
  const count = contractGroups.value.length
  let nextIndex: number
  if (event.key === 'ArrowRight') nextIndex = (index + 1) % count
  else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + count) % count
  else if (event.key === 'Home') nextIndex = 0
  else if (event.key === 'End') nextIndex = count - 1
  else return
  event.preventDefault()
  activeGroupId.value = contractGroups.value[nextIndex]!.id
  const tabs = (event.currentTarget as HTMLElement).parentElement?.querySelectorAll<HTMLButtonElement>('[role=tab]')
  tabs?.[nextIndex]?.focus()
}

async function dropContract(group: ContractGroup, index: number): Promise<void> {
  const contractId = draggedContractId.value
  const sourceGroupId = draggedSourceGroupId.value
  finishContractDrag()
  if (!contractId || !sourceGroupId || contractOrderSaving.value) return
  await moveContractTo(contractId, sourceGroupId, group.id, index)
}

async function moveContract(group: ContractGroup, index: number, direction: -1 | 1): Promise<void> {
  if (contractOrderSaving.value || index + direction < 0 || index + direction >= group.contracts.length) return
  const contract = group.contracts[index]
  if (contract) await moveContractTo(contract.id, group.id, group.id, index + (direction === 1 ? 2 : -1))
}

async function moveContractTo(contractId: number, sourceGroupId: number, targetGroupId: number, index: number): Promise<void> {
  const sourceGroup = contractGroups.value.find((item) => item.id === sourceGroupId)
  const targetGroup = contractGroups.value.find((item) => item.id === targetGroupId)
  if (!sourceGroup || !targetGroup) return
  const sourceIndex = sourceGroup.contracts.findIndex((item) => item.id === contractId)
  if (sourceIndex < 0) return
  if (sourceGroup.id === targetGroup.id && (index === sourceIndex || index === sourceIndex + 1)) return
  const originalGroups = contractGroups.value.map((item) => ({ ...item, contracts: item.contracts.map((contract) => ({ ...contract })) }))
  const [moved] = sourceGroup.contracts.splice(sourceIndex, 1)
  if (!moved) return
  contractOrderSaving.value = true
  contractLoadVersion += 1
  if (sourceGroup.id === targetGroup.id && sourceIndex < index) index -= 1
  moved.group_id = targetGroup.id
  targetGroup.contracts.splice(Math.max(0, index), 0, moved)
  contracts.value = flattenContractGroups(contractGroups.value)
  activeGroupId.value = targetGroup.id
  try {
    if (sourceGroup.id !== targetGroup.id) {
      await client.put(`/contracts/${contractId}/group`, { group_id: targetGroup.id })
      await client.put(`/contracts/groups/${sourceGroup.id}/order`, {
        contract_ids: sourceGroup.contracts.map((item) => item.id),
      })
    }
    await client.put(`/contracts/groups/${targetGroup.id}/order`, {
      contract_ids: targetGroup.contracts.map((item) => item.id),
    })
  } catch (error: any) {
    contractGroups.value = originalGroups
    contracts.value = flattenContractGroups(originalGroups)
    await loadContracts(true).catch(() => { errorText.value = '重新加载合约失败，请刷新页面核对顺序' })
    ElMessage.error(error.response?.data?.detail || '保存合约顺序失败')
  } finally {
    contractOrderSaving.value = false
  }
}

function togglePanel(panel: PanelKey): void {
  collapsedPanels.value[panel] = !collapsedPanels.value[panel]
  persistPanelLayout()
}

function panelStyle(panel: PanelKey): Record<string, string> {
  const size = panelSizes.value[panel]
  return {
    ...(size.width ? { width: `${size.width}px` } : {}),
    ...(!collapsedPanels.value[panel] && size.height ? { height: `${size.height}px` } : {}),
  }
}

function persistPanelLayout(): void {
  try {
    localStorage.setItem(panelLayoutKey, JSON.stringify({
      collapsed: collapsedPanels.value,
      sizes: panelSizes.value,
    }))
  } catch {
    // 浏览器禁用存储或空间不足时，仍允许调整当前页面。
  }
}

function loadPanelLayout(): void {
  try {
    localStorage.removeItem('pxy-futures-panel-layout-v1')
    localStorage.removeItem('pxy-futures-panel-layout-v2')
    const saved = JSON.parse(localStorage.getItem(panelLayoutKey) || '{}') as {
      collapsed?: Partial<Record<PanelKey, boolean>>
      sizes?: Partial<Record<PanelKey, PanelSize>>
    }
    for (const panel of Object.keys(panelRefs) as PanelKey[]) {
      const collapsed = saved?.collapsed?.[panel]
      if (typeof collapsed === 'boolean') collapsedPanels.value[panel] = collapsed
      const size = saved?.sizes?.[panel]
      for (const dimension of ['width', 'height'] as const) {
        const value = size?.[dimension]
        if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
          panelSizes.value[panel][dimension] = Math.round(value)
        }
      }
    }
  } catch {
    // 缓存损坏或无法访问时，使用默认自适应布局。
  }
}

function resetPanelLayout(): void {
  for (const panel of Object.keys(panelRefs) as PanelKey[]) {
    collapsedPanels.value[panel] = false
    panelSizes.value[panel] = {}
    panelRefs[panel].value?.style.removeProperty('width')
    panelRefs[panel].value?.style.removeProperty('height')
  }
  persistPanelLayout()
}

function observePanelSizes(): void {
  if (typeof ResizeObserver === 'undefined') return
  panelResizeObserver = new ResizeObserver((entries) => {
    let changed = false
    for (const entry of entries) {
      const panel = (Object.entries(panelRefs) as [PanelKey, { value: HTMLElement | null }][])
        .find(([, panelRef]) => panelRef.value === entry.target)?.[0]
      if (!panel || collapsedPanels.value[panel]) continue
      const element = entry.target as HTMLElement
      // 原生拖动写入的像素尺寸包含边框；不回写内容区或自动布局尺寸。
      const width = element.style.width.endsWith('px')
        ? Math.round(Number.parseFloat(element.style.width)) : undefined
      const height = element.style.height.endsWith('px')
        ? Math.round(Number.parseFloat(element.style.height)) : undefined
      if (width === undefined && height === undefined) continue
      const previous = panelSizes.value[panel]
      if (previous.width !== width || previous.height !== height) {
        panelSizes.value[panel] = { width, height }
        changed = true
      }
    }
    if (changed) persistPanelLayout()
  })
  for (const panelRef of Object.values(panelRefs)) {
    if (panelRef.value) panelResizeObserver.observe(panelRef.value, { box: 'border-box' })
  }
}

async function selectContract(id: number): Promise<void> {
  selectedId.value = id
  const contract = contracts.value.find((item) => item.id === id)
  if (contract?.group_id) activeGroupId.value = contract.group_id
  signalRetryAfter = 0
  signals.value = []
  periodNotes.value = {}
  dirtyNoteDurations.clear()
  selectedDuration.value = null
  await refreshSignals()
}

async function refreshSignals(force = false): Promise<void> {
  const contractId = selectedId.value
  if (!contractId || (refreshing.value && refreshingContractId === contractId)) return
  if (!force && Date.now() < signalRetryAfter) return

  signalAbortController?.abort()
  const controller = createRequestController()
  signalAbortController = controller
  refreshingContractId = contractId
  refreshing.value = true
  errorText.value = ''
  try {
    const { data } = await client.get<ContractSignals>(`/contracts/${contractId}/signals`, {
      signal: controller.signal,
    })
    if (selectedId.value !== contractId || signalAbortController !== controller) return
    signals.value = data.signals
    if (selectedContract.value && data.market_status) selectedContract.value.market_status = data.market_status
    const warnings = data.warnings ?? []
    errorText.value = warnings.join('；')
    signalRetryAfter = warnings.length ? Date.now() + SIGNAL_WARNING_RETRY_MS : 0
    syncPeriodNotes(data.signals)
    if (!data.signals.some((signal) => signal.period.duration_seconds === selectedDuration.value)) {
      selectedDuration.value = data.signals[0]?.period.duration_seconds || null
    }
  } catch (error: any) {
    if (controller.aborted) return
    errorText.value = error.response?.data?.detail || '读取均线数据失败'
    if (error.response?.status === 422) {
      signalRetryAfter = Date.now() + SIGNAL_WARNING_RETRY_MS
    }
  } finally {
    if (signalAbortController === controller) {
      signalAbortController = null
      refreshingContractId = null
      refreshing.value = false
    }
  }
}

async function createContract(): Promise<void> {
  if (!recognition.value.complete || adding.value) {
    ElMessage.warning('请输入完整合约代码，例如 FG609、RB2609')
    return
  }
  adding.value = true
  try {
    const { data } = await client.post<ContractConfig>('/contracts', form.value)
    addVisible.value = false
    form.value = { exchange: '', code: '', name: '', group_id: activeGroupId.value }
    await loadContracts()
    await selectContract(data.id)
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '添加失败')
  } finally {
    adding.value = false
  }
}

async function updateContract(): Promise<void> {
  const contract = editingContract.value
  if (!contract || !contractEditRecognition.value.complete || changingContract.value) {
    ElMessage.warning('请选择完整的当前可用合约，例如 PP2701')
    return
  }
  changingContract.value = true
  try {
    const { data } = await client.put<ContractConfig>(`/contracts/${contract.id}`, contractEditForm.value)
    const index = contracts.value.findIndex((item) => item.id === contract.id)
    if (index >= 0) contracts.value[index] = data
    contractEditVisible.value = false
    editingContract.value = null
    if (selectedId.value === contract.id) {
      signals.value = []
      await refreshSignals(true)
    }
    ElMessage.success(`已更换为 ${displayContractCode(data.code)}，原周期配置已保留`)
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '更换合约失败')
  } finally {
    changingContract.value = false
  }
}

function handleContractCodeInput(): void {
  const result = recognizeContract(form.value.code)
  // 保留用户输入的 PVC/PV 显示名，只规范大小写；提交时由后端转为数据源标准代码 V。
  form.value.code = form.value.code.replace(/\s+/g, '').toUpperCase()
  if (result.exchange) form.value.exchange = result.exchange
}

function openPeriod(period?: PeriodConfig): void {
  editPeriod.value = period || null
  periodForm.value = period
    ? { label: period.label, duration_seconds: period.duration_seconds, m4: period.m4, m3: period.m3, m2: period.m2, m1: period.m1 }
    : { label: '日线', duration_seconds: 86400, m4: 240, m3: 60, m2: 21, m1: 4 }
  periodVisible.value = true
}

function selectCommonPeriod(durationSeconds: number): void {
  const selected = commonPeriods.find((period) => period.durationSeconds === durationSeconds)
  if (selected) periodForm.value.label = selected.label
}

async function savePeriod(): Promise<void> {
  if (!selectedId.value) return
  try {
    await client.put(`/contracts/${selectedId.value}/periods`, periodForm.value)
    periodVisible.value = false
    await loadContracts()
    await refreshSignals(true)
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '保存失败')
  }
}

async function removeContract(contract: ContractConfig): Promise<void> {
  await ElMessageBox.confirm(`确认删除 ${contract.symbol}？`, '删除合约', { type: 'warning' })
  await client.delete(`/contracts/${contract.id}`)
  await loadContracts()
  if (selectedId.value === contract.id) {
    selectedId.value = null
    signals.value = []
  }
  if (contracts.value.length) await selectContract(contracts.value[0]!.id)
}

async function removePeriod(period: PeriodConfig): Promise<void> {
  if (!selectedId.value) return
  await ElMessageBox.confirm(`确认删除 ${period.label} 配置？`, '删除周期', { type: 'warning' })
  await client.delete(`/contracts/${selectedId.value}/periods/${period.duration_seconds}`)
  await loadContracts()
  await refreshSignals(true)
}

async function saveCredentials(): Promise<void> {
  if (credentialsSaving.value) return
  credentialsSaving.value = true
  try {
    const { data } = await client.put<TqCredentialStatus>(
      '/settings/tq-credentials',
      credentials.value,
    )
    credentialStatus.value = data
    credentials.value = { username: '', password: '' }
    credentialsVisible.value = false
    ElMessage.success('天勤账号已通过真实认证并保存')
    await refreshSignals(true)
  } catch (error: any) {
    const detail = error.response?.data?.detail || '认证失败，请检查网络后重试'
    credentialStatus.value = {
      configured: credentialStatus.value?.configured ?? false,
      status: error.response?.status === 422 ? 'invalid' : 'unavailable',
      checked_at: new Date().toISOString(),
      message: detail,
      source: 'save_validation',
    }
    ElMessage.error(detail)
  } finally {
    credentialsSaving.value = false
  }
}

async function openCredentials(): Promise<void> {
  credentialsVisible.value = true
  try {
    const { data } = await client.get<TqCredentialStatus>('/settings/tq-credentials')
    credentialStatus.value = data
  } catch {
    credentialStatus.value = null
  }
}

function credentialAlertType(): 'success' | 'error' | 'warning' | 'info' {
  if (credentialStatus.value?.status === 'valid') return 'success'
  if (credentialStatus.value?.status === 'invalid') return 'error'
  if (credentialStatus.value?.status === 'unavailable') return 'warning'
  return 'info'
}

onMounted(async () => {
  dashboardMounted = true
  loadPanelLayout()
  loadAlertPreferences()
  window.requestAnimationFrame(observePanelSizes)
  try {
    await loadContracts()
    timer = window.setInterval(() => void refreshSignals(), 3_000)
    contractStatusTimer = window.setInterval(() => void loadContracts().catch(() => undefined), 60_000)
    alertTimer = window.setInterval(
      () => void refreshAlertSignals(true),
      ALERT_SAFETY_SCAN_MS,
    )
    connectAlertStream()
  } catch (error: any) {
    if (error.response?.status === 401) {
      const loginUrl = `/login?redirect=${encodeURIComponent('/apps/launch/futures')}`
      const targetWindow = window.top ?? window
      targetWindow.location.replace(loginUrl)
      return
    }
    errorText.value = error.response?.data?.detail || '加载合约失败'
  }
})

onBeforeUnmount(() => {
  dashboardMounted = false
  panelResizeObserver?.disconnect()
  panelResizeObserver = null
  if (timer) window.clearInterval(timer)
  if (contractStatusTimer) window.clearInterval(contractStatusTimer)
  if (alertTimer) window.clearInterval(alertTimer)
  clearAlertSocketTimers()
  if (alertSocket) {
    alertSocket.onclose = null
    alertSocket.close(1000, '页面已关闭')
    alertSocket = null
  }
  for (const soundTimer of soundTimers) window.clearTimeout(soundTimer)
  soundTimers.clear()
  alertAudio?.pause()
  signalAbortController?.abort()
  for (const signal of signals.value) flushPeriodNote(signal)
  for (const noteTimer of noteSaveTimers.values()) window.clearTimeout(noteTimer)
})
</script>

<template>
  <main class="workspace">
    <header class="topbar">
      <div class="app-brand">
        <span class="app-brand-mark">P</span>
        <div class="app-brand-copy"><h1>智能期货</h1><span>数据 · 均线 · 预警</span></div>
      </div>
      <div class="topbar-actions">
        <a
          v-if="platformReturnVisible"
          class="platform-return"
          href="/dashboard"
          target="_top"
          title="返回平台主页"
          aria-label="返回平台主页"
        >
          <el-icon><Back /></el-icon>
          <span>返回平台主页</span>
        </a>
        <el-button plain @click="resetPanelLayout">恢复布局</el-button>
        <el-button :icon="Refresh" :loading="refreshing" plain @click="refreshSignals(true)">更新数据</el-button>
        <el-button :icon="Setting" plain @click="openCredentials">天勤账号</el-button>
        <el-button type="primary" :icon="Plus" @click="openAddContract()">添加合约</el-button>
      </div>
    </header>

    <div class="workspace-layout">
      <aside ref="groupsPanelRef" class="contract-groups-panel resizable-panel" :class="{ collapsed: collapsedPanels.groups }" :style="panelStyle('groups')" aria-label="交易合约列表">
        <header class="panel-header contract-groups-header">
          <div><h2>交易合约</h2><span>WATCHLIST · 拖动调整顺序</span></div>
          <button type="button" class="panel-toggle" @click="togglePanel('groups')">{{ collapsedPanels.groups ? '展开' : '折叠' }}</button>
        </header>
        <div v-if="!collapsedPanels.groups" class="group-columns">
          <div class="group-tabs" role="tablist" aria-label="合约分组">
            <button
              v-for="(group, index) in contractGroups"
              :id="`group-tab-${group.id}`"
              :key="group.id"
              type="button"
              class="group-tab"
              role="tab"
              :class="{ active: group.id === activeGroupId }"
              :aria-selected="group.id === activeGroupId"
              :aria-controls="`group-panel-${group.id}`"
              :aria-label="`分组：${group.name}`"
              :tabindex="group.id === activeGroupId ? 0 : -1"
              :title="`${group.name} · 拖入合约可移到此组`"
              @click="activeGroupId = group.id"
              @keydown="navigateGroupTabs($event, index)"
              @dragover="allowContractDrop"
              @drop.prevent="dropContract(group, group.contracts.length)"
            >{{ group.name }}</button>
            <button type="button" class="group-tab group-tab-add" title="添加分组" aria-label="添加分组" @click="createGroup"><el-icon><Plus /></el-icon></button>
          </div>
          <article v-if="activeGroup" :id="`group-panel-${activeGroup.id}`" :key="activeGroup.id" class="contract-group active" role="tabpanel" :aria-labelledby="`group-tab-${activeGroup.id}`">
            <header class="contract-group-header">
              <span class="group-name" :title="activeGroup.name">{{ activeGroup.name }}</span>
              <div class="group-actions">
                <el-button :icon="Plus" text title="向该分组添加合约" aria-label="向该分组添加合约" :disabled="contractOrderSaving" @click.stop="openAddContract(activeGroup.id)" />
                <el-button :icon="EditPen" text title="重命名分组" aria-label="重命名分组" :disabled="contractOrderSaving" @click.stop="renameGroup(activeGroup)" />
              </div>
            </header>
            <div class="contract-group-list" @dragover="allowContractDrop" @drop="dropContract(activeGroup, activeGroup.contracts.length)">
              <div
                v-for="(contract, index) in activeGroup.contracts"
                :key="contract.id"
                class="contract-row"
                :class="{ active: contract.id === selectedId, dragging: contract.id === draggedContractId, historical: contract.market_status === 'expired' }"
                :title="contract.market_status === 'expired' ? '已到期，仅保留历史查看，已退出实时监控' : contract.name"
                :draggable="!contractOrderSaving"
                @click="selectContract(contract.id)"
                @dragstart="beginContractDrag(contract, activeGroup.id, $event)"
                @dragend="finishContractDrag"
                @dragover="allowContractDrop"
                @drop.stop="dropContract(activeGroup, index)"
              >
                <span class="contract-code"><small>{{ contract.exchange }}</small><strong>{{ displayContractCode(contract.code) }}</strong><span v-if="contract.market_status === 'expired'" class="contract-expiry-tag">到期</span></span>
                <span class="contract-actions" @dragstart.prevent>
                  <el-button :icon="ArrowUp" text title="上移" aria-label="上移合约" :disabled="contractOrderSaving || index === 0" @click.stop="moveContract(activeGroup, index, -1)" />
                  <el-button :icon="ArrowDown" text title="下移" aria-label="下移合约" :disabled="contractOrderSaving || index === activeGroup.contracts.length - 1" @click.stop="moveContract(activeGroup, index, 1)" />
                  <el-button :icon="EditPen" text aria-label="更换合约" :disabled="contractOrderSaving" @click.stop="openContractEdit(contract)" />
                  <el-button :icon="Delete" text type="danger" aria-label="删除该合约" :disabled="contractOrderSaving" @click.stop="removeContract(contract)" />
                </span>
              </div>
              <p v-if="!activeGroup.contracts.length" class="group-empty">该分组暂无合约</p>
              <button type="button" class="group-add-contract" :disabled="contractOrderSaving" @click.stop="openAddContract(activeGroup.id)"><el-icon><Plus /></el-icon>添加合约</button>
            </div>
          </article>
        </div>
      </aside>

      <div class="workspace-main">
      <section class="setup-section">
      <div ref="setupPanelRef" class="setup-details-panel resizable-panel" :class="{ collapsed: collapsedPanels.setup }" :style="panelStyle('setup')">
        <header class="panel-header">
          <div><span>当前合约设置</span><span v-if="selectedContract">{{ selectedContract.exchange }}</span><strong v-if="selectedContract">{{ displayContractCode(selectedContract.code) }}</strong></div>
          <button type="button" class="panel-toggle" @click="togglePanel('setup')">{{ collapsedPanels.setup ? '展开' : '折叠' }}</button>
        </header>
        <div v-if="!collapsedPanels.setup" class="setup-details-content">
          <div v-if="selectedContract" class="setup-table-wrap">
            <table class="setup-table"><thead><tr><th>周期设置</th><th>M4设置</th><th>M3设置</th><th>M2设置</th><th>M1设置</th><th></th></tr></thead><tbody>
              <tr v-for="period in selectedContract.periods" :key="period.duration_seconds"><td>{{ period.label }}</td><td>{{ period.m4 }}</td><td>{{ period.m3 }}</td><td>{{ period.m2 }}</td><td>{{ period.m1 }}</td><td class="row-actions"><el-button :icon="EditPen" text title="编辑周期" @click="openPeriod(period)" /><el-button :icon="Delete" text type="danger" title="删除周期" @click="removePeriod(period)" /></td></tr>
            </tbody></table>
            <el-button class="add-period" :icon="Plus" text @click="openPeriod()">添加周期</el-button>
          </div>
          <div v-else class="empty-signals">添加并选择一个合约后配置周期</div>
        </div>
      </div>
    </section>

    <section class="monitor-section">
      <div ref="signalsPanelRef" class="signal-area resizable-panel" :class="{ collapsed: collapsedPanels.signals }" :style="panelStyle('signals')">
        <header class="panel-header signal-panel-header">
          <div><span>均线信号</span><strong v-if="selectedContract">{{ displayContractCode(selectedContract.code) }}</strong></div>
          <button type="button" class="panel-toggle" @click="togglePanel('signals')">{{ collapsedPanels.signals ? '展开' : '折叠' }}</button>
        </header>
        <div v-if="!collapsedPanels.signals">
          <div v-if="errorText" class="data-error">{{ errorText }}</div>
          <table v-if="signals.length" class="signal-table"><thead><tr><th>周期</th><th>均线4</th><th>均线3</th><th>均线1上穿/下穿均线2</th><th>备注</th></tr></thead><tbody><tr v-for="signal in signals" :key="signal.period.duration_seconds" :class="{ selected: selectedDuration === signal.period.duration_seconds }" @click="selectedDuration = signal.period.duration_seconds"><td><strong>{{ signal.period.label }}</strong></td><td><span class="trend" :class="signal.trend.m4 === '多' ? 'bullish' : 'bearish'">{{ signal.trend.m4 }}</span></td><td><span class="trend" :class="signal.trend.m3 === '多' ? 'bullish' : 'bearish'">{{ signal.trend.m3 }}</span></td><td class="signal-state-cell"><div class="state signal-state-layout" :class="signal.cross_type === '金叉' ? 'golden' : signal.cross_type === '死叉' ? 'death' : ''"><span>{{ signal.label }}</span><span class="cross-type">{{ signal.cross_type || '' }}</span></div></td><td class="period-note-cell" @click.stop><el-input :model-value="periodNotes[signal.period.duration_seconds] || ''" maxlength="500" size="small" @input="schedulePeriodNoteSave(signal, $event)" @blur="flushPeriodNote(signal)" /></td></tr></tbody></table>
          <div v-else-if="!contracts.length" class="empty-workspace">添加并选择一个合约</div><div v-else-if="!errorText" class="empty-signals">当前合约暂无周期配置</div>
          <footer v-if="selectedContract" class="ma-footer"><span>均线数值 · {{ displayContractCode(selectedContract.code) }} · {{ selectedSignal?.period.label || '--' }}：</span><strong>M4：{{ maText('M4') }}{{ longTrendText('m4') }}</strong><strong>M3：{{ maText('M3') }}{{ longTrendText('m3') }}</strong><strong>M2：{{ maText('M2') }}</strong><strong>M1：{{ maText('M1') }}</strong><small v-if="selectedSignal">{{ selectedSignal.period.label }} · {{ selectedSignal.bar_time.replace('T', ' ').slice(0, 19) }} · {{ dataStatusText(selectedSignal) }}</small><small v-else>等待可用的实时 K 线数据</small></footer>
        </div>
      </div>

      <section ref="alertsPanelRef" class="alert-center resizable-panel" :class="{ collapsed: collapsedPanels.alerts }" :style="panelStyle('alerts')" aria-label="全品种预警">
        <header class="alert-center-header">
          <div><span class="alert-kicker">MARKET SENTINEL</span><h2>全品种预警</h2></div>
          <div class="alert-header-actions"><span class="alert-status" :class="{ scanning: alertScanning }">{{ alertStatusText }}</span><button type="button" class="panel-toggle" @click="togglePanel('alerts')">{{ collapsedPanels.alerts ? '展开' : '折叠' }}</button></div>
        </header>
        <div v-if="!collapsedPanels.alerts">
          <div v-if="!selectedAlertDurations.length" class="alert-inactive" role="status"><span>尚未开启预警。选择监测周期后生效，设置会自动保存。</span><button type="button" @click="enableAllAlertPeriods">开启全部周期</button></div>
          <div class="alert-control-row"><strong>预警周期</strong><div class="alert-options"><button v-for="period in commonPeriods" :key="period.durationSeconds" type="button" :class="{ active: selectedAlertDurations.includes(period.durationSeconds) }" :aria-pressed="selectedAlertDurations.includes(period.durationSeconds)" @click="toggleAlertDuration(period.durationSeconds)">{{ period.label }}</button></div></div>
          <div class="alert-control-row conditions"><strong>预警条件</strong><div class="alert-options"><button v-for="condition in alertConditionOptions" :key="condition" type="button" :class="{ active: selectedAlertConditions.includes(condition), death: condition.endsWith('死叉') }" :aria-pressed="selectedAlertConditions.includes(condition)" @click="toggleAlertCondition(condition)">{{ condition }}</button></div></div>
          <div class="alert-control-row states"><strong>状态条件</strong><div class="alert-options"><button v-for="state in alertStateOptions" :key="state" type="button" :class="{ active: selectedAlertStates.includes(state) }" :aria-pressed="selectedAlertStates.includes(state)" @click="toggleAlertState(state)">{{ state }}</button></div></div>
          <div class="alert-results"><div v-if="matchingAlertSignals.length" class="alert-result-grid"><div v-for="signal in matchingAlertSignals" :key="`${signal.contract_id}-${signal.period.duration_seconds}`" class="alert-result-item"><span class="signal-pulse"></span><span class="alert-result-text">{{ alertText(signal) }}</span><span v-if="alertCount(signal)" class="alert-new-badge" title="该预警状态已持续的已收盘K线根数">+{{ alertCount(signal) }}</span></div></div><div v-else class="alert-empty">{{ alertStatusText }}</div></div>
          <footer class="alert-center-footer"><span>仅监测当前账号自选合约已配置的周期</span><span v-if="notificationPermission === 'denied'">Windows通知未授权，声音和页面弹窗仍可使用</span><span v-else-if="notificationPermission === 'granted'">Windows通知已开启</span></footer>
        </div>
      </section>
    </section>

      </div>
    </div>

    <Transition name="alert-popup">
      <aside v-if="alertPopupVisible" class="market-alert-popup" role="alert" aria-live="assertive">
        <div class="popup-scanline"></div>
        <button type="button" class="popup-close" aria-label="关闭预警弹窗" @click="closeAlertPopup"><el-icon><Close /></el-icon></button>
        <header>
          <span>MARKET SIGNAL</span>
          <strong>智能期货预警</strong>
          <time>{{ alertPopupBeijingTime }}</time>
          <small>{{ alertPopupSignals.length }} 条新信号</small>
        </header>
        <div class="popup-signals">
          <p v-for="signal in alertPopupSignals.slice(0, 5)" :key="alertEventKey(signal)">{{ alertText(signal) }}</p>
          <p v-if="alertPopupSignals.length > 5">另有 {{ alertPopupSignals.length - 5 }} 条信号，请查看预警列表</p>
        </div>
      </aside>
    </Transition>

    <el-dialog v-model="addVisible" title="添加合约" width="420px" destroy-on-close>
      <el-form label-position="top" @submit.prevent="createContract"><el-form-item label="合约代码"><el-autocomplete v-model="form.code" class="full-width" value-key="value" placeholder="例如 pv、fg 或 RB2609" :fetch-suggestions="fetchContractSuggestions" :loading="suggestionLoading" :trigger-on-focus="false" @input="handleContractCodeInput" @select="selectContractSuggestion"><template #default="{ item }"><div class="contract-suggestion"><strong>{{ item.display }}</strong><span>{{ item.exchange }}</span></div></template></el-autocomplete><div class="recognition" :class="{ invalid: form.code && !recognition.exchange, incomplete: recognition.exchange && !recognition.complete }"><template v-if="recognition.exchange && recognition.complete">已识别：{{ recognition.exchangeName }} {{ recognition.exchange }} · 数据代码 {{ recognition.code }}</template><template v-else-if="recognition.exchange">已识别{{ recognition.exchangeName }}，请选择下方当前可用合约，或继续输入交割月份</template><template v-else-if="form.code">未识别品种代码，请检查品种简称和月份。</template><template v-else>输入品种前缀后自动列出当前可用合约，大小写均可；PVC 可输入 pv 或 pvc。</template></div></el-form-item><el-form-item label="交易所"><el-select v-model="form.exchange" placeholder="输入代码后自动选择"><el-option label="郑商所 CZCE" value="CZCE" /><el-option label="大商所 DCE" value="DCE" /><el-option label="上期所 SHFE" value="SHFE" /><el-option label="中金所 CFFEX" value="CFFEX" /><el-option label="广期所 GFEX" value="GFEX" /></el-select></el-form-item><el-form-item label="显示名称"><el-input v-model="form.name" placeholder="可留空" /></el-form-item><el-form-item label="所属分组"><el-select v-model="form.group_id" class="full-width"><el-option v-for="group in contractGroups" :key="group.id" :label="group.name" :value="group.id" /></el-select></el-form-item></el-form>
      <template #footer><el-button :disabled="adding" @click="addVisible = false">取消</el-button><el-button type="primary" :loading="adding" @click="createContract">验证并添加</el-button></template>
    </el-dialog>

    <el-dialog v-model="contractEditVisible" title="更换合约" width="420px" destroy-on-close>
      <el-form label-position="top" @submit.prevent="updateContract"><el-form-item label="新合约代码"><el-autocomplete v-model="contractEditForm.code" class="full-width" value-key="value" placeholder="例如 pp 或 PP2701" :fetch-suggestions="fetchContractSuggestions" :loading="suggestionLoading" :trigger-on-focus="false" @input="handleContractEditCodeInput" @select="selectContractEditSuggestion"><template #default="{ item }"><div class="contract-suggestion"><strong>{{ item.display }}</strong><span>{{ item.exchange }}</span></div></template></el-autocomplete><div class="recognition" :class="{ invalid: contractEditForm.code && !contractEditRecognition.exchange, incomplete: contractEditRecognition.exchange && !contractEditRecognition.complete }"><template v-if="contractEditRecognition.exchange && contractEditRecognition.complete">已识别：{{ contractEditRecognition.exchangeName }} {{ contractEditRecognition.exchange }} · {{ contractEditRecognition.code }}</template><template v-else-if="contractEditRecognition.exchange">请选择下方当前可用合约，或继续输入交割月份</template><template v-else-if="contractEditForm.code">未识别品种代码，请检查品种简称和月份。</template></div></el-form-item></el-form>
      <template #footer><el-button :disabled="changingContract" @click="contractEditVisible = false">取消</el-button><el-button type="primary" :loading="changingContract" @click="updateContract">验证并更换</el-button></template>
    </el-dialog>

    <el-dialog v-model="periodVisible" class="period-dialog" :title="editPeriod ? '编辑周期' : '配置周期'" width="640px" destroy-on-close>
      <el-form label-position="top" @submit.prevent="savePeriod"><el-form-item label="常用周期"><el-select v-model="periodForm.duration_seconds" class="full-width" @change="selectCommonPeriod"><el-option v-for="period in commonPeriods" :key="period.durationSeconds" :label="period.label" :value="period.durationSeconds" /></el-select></el-form-item><div class="period-inputs"><el-form-item label="M4"><el-input-number v-model="periodForm.m4" :min="1" /></el-form-item><el-form-item label="M3"><el-input-number v-model="periodForm.m3" :min="1" /></el-form-item><el-form-item label="M2"><el-input-number v-model="periodForm.m2" :min="1" /></el-form-item><el-form-item label="M1"><el-input-number v-model="periodForm.m1" :min="1" /></el-form-item></div></el-form>
      <template #footer><el-button @click="periodVisible = false">取消</el-button><el-button type="primary" @click="savePeriod">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="credentialsVisible" title="天勤账号" width="420px" destroy-on-close>
      <el-alert v-if="credentialStatus" :title="credentialStatus.message" :type="credentialAlertType()" :closable="false" show-icon />
      <el-form class="credentials-form" label-position="top" @submit.prevent="saveCredentials"><el-form-item label="用户名"><el-input v-model="credentials.username" autocomplete="username" /></el-form-item><el-form-item label="密码"><el-input v-model="credentials.password" type="password" show-password autocomplete="current-password" /></el-form-item></el-form>
      <template #footer><el-button @click="credentialsVisible = false">取消</el-button><el-button type="primary" :loading="credentialsSaving" @click="saveCredentials">验证并保存</el-button></template>
    </el-dialog>
  </main>
</template>
