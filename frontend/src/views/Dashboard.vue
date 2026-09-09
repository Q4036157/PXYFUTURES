<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Back, Close, Delete, EditPen, Plus, Refresh, Setting } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { alertWebSocketUrl, client } from '@/api/client'
import { recognizeContract } from '@/lib/contractCatalog'
import alertSoundUrl from '@/assets/ok3.wav?url'
import type {
  AlertScanResult,
  AlertSignalSnapshot,
  AlertStreamSnapshot,
  ContractConfig,
  ContractSignals,
  PeriodConfig,
  SignalSnapshot,
} from '@/types/futures'

type AlertCondition =
  | '多多金叉' | '多空金叉' | '空多金叉' | '空空金叉'
  | '多多死叉' | '多空死叉' | '空多死叉' | '空空死叉'

const contracts = ref<ContractConfig[]>([])
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
const form = ref({ exchange: '', code: '', name: '' })
const contractEditForm = ref({ exchange: '', code: '', name: '' })
const periodForm = ref({ label: '日线', duration_seconds: 86400, m4: 240, m3: 60, m2: 21, m1: 4 })
const credentials = ref({ username: '', password: '' })
let timer: number | undefined
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
const alertSignals = ref<AlertSignalSnapshot[]>([])
const alertScanning = ref(false)
const alertScanError = ref('')
const alertFailedCount = ref(0)
const alertPopupSignals = ref<AlertSignalSnapshot[]>([])
const alertPopupVisible = ref(false)
const alertPopupBeijingTime = ref('')
const latestAlertEventKeys = ref<Set<string>>(new Set())
const alertStreamConnected = ref(false)
const notificationPermission = ref<NotificationPermission | 'unsupported'>(
  typeof Notification === 'undefined' ? 'unsupported' : Notification.permission,
)
const credentialStatus = ref<TqCredentialStatus | null>(null)
const credentialsSaving = ref(false)
const seenAlertEvents = new Set<string>()
const soundTimers = new Set<number>()
const SIGNAL_WARNING_RETRY_MS = 15 * 60 * 1_000
const ALERT_FRESHNESS_MS = 3 * 60 * 1_000
let alertAudio: HTMLAudioElement | null = null
let alertRescanPending = false
let alertSocket: WebSocket | null = null
let alertSocketReconnectDelay = 1_000
let dashboardMounted = false
let signalRetryAfter = 0
const platformReturnVisible = window.location.pathname === '/futures-app'
  || window.location.pathname.startsWith('/futures-app/')
  || window.self !== window.top

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
  return alertSignals.value
    .filter((signal) => {
      const condition = alertCondition(signal)
      return durations.has(signal.period.duration_seconds) && condition !== null && conditions.has(condition)
    })
    .sort((left, right) => {
      const durationOrder = commonPeriods.findIndex(
        (period) => period.durationSeconds === left.period.duration_seconds,
      ) - commonPeriods.findIndex(
        (period) => period.durationSeconds === right.period.duration_seconds,
      )
      return durationOrder || left.symbol.localeCompare(right.symbol)
    })
})

const alertStatusText = computed(() => {
  if (!selectedAlertDurations.value.length || !selectedAlertConditions.value.length) {
    return '请选择周期和条件开始监测'
  }
  if (alertScanning.value) return '正在扫描自选合约'
  if (alertScanError.value) return alertScanError.value
  if (alertFailedCount.value) {
    return `当前匹配 ${matchingAlertSignals.value.length} 条，${alertFailedCount.value} 条等待行情缓存`
  }
  const channel = alertStreamConnected.value ? '实时推送' : '连接恢复中'
  return `当前匹配 ${matchingAlertSignals.value.length} 条 · ${channel}`
})

function alertCondition(signal: AlertSignalSnapshot): AlertCondition | null {
  if (!signal.cross_type) return null
  return `${signal.trend.m4}${signal.trend.m3}${signal.cross_type}` as AlertCondition
}

function alertText(signal: AlertSignalSnapshot): string {
  return `${signal.symbol} · ${signal.period.label} · ${alertCondition(signal)} · ${signal.label}`
}

function alertEventKey(signal: AlertSignalSnapshot): string {
  return `${signal.contract_id}:${signal.period.duration_seconds}:${signal.cross_type}:${signal.cross_time}`
}

function isLatestAlertSignal(signal: AlertSignalSnapshot): boolean {
  return latestAlertEventKeys.value.has(alertEventKey(signal))
}

function isFreshAlertSignal(signal: AlertSignalSnapshot): boolean {
  if (signal.period.duration_seconds >= 86_400) return true
  const confirmedAt = Date.parse(signal.confirmed_at)
  if (!Number.isFinite(confirmedAt)) return false
  const age = Date.now() - confirmedAt
  return age >= -10_000 && age <= ALERT_FRESHNESS_MS
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
  syncAlertSubscription()
  if (!alertStreamConnected.value) void refreshAlertSignals()
}

function toggleAlertCondition(condition: AlertCondition): void {
  enableBrowserAlerts()
  selectedAlertConditions.value = selectedAlertConditions.value.includes(condition)
    ? selectedAlertConditions.value.filter((value) => value !== condition)
    : [...selectedAlertConditions.value, condition]
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
  alertSignals.value = data.signals
  alertFailedCount.value = data.failed
  if (!allowNotification) return

  const selectedConditions = new Set(selectedAlertConditions.value)
  const newSignals = data.signals.filter((signal) => {
    const condition = alertCondition(signal)
    if (!condition || !selectedConditions.has(condition)) return false
    if (!signal.cross_time || signal.cross_time !== signal.confirmed_bar_time) return false
    if (!isFreshAlertSignal(signal)) return false
    return !seenAlertEvents.has(alertEventKey(signal))
  })
  for (const signal of newSignals) seenAlertEvents.add(alertEventKey(signal))
  if (newSignals.length) {
    latestAlertEventKeys.value = new Set(newSignals.map(alertEventKey))
    showAlertNotification(newSignals)
  }
}

async function refreshAlertSignals(allowNotification = true): Promise<void> {
  if (alertScanning.value) {
    alertRescanPending = true
    return
  }
  if (!selectedAlertDurations.value.length || !selectedAlertConditions.value.length) {
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
  if (!selectedAlertConditions.value.length) return []
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
  if (signal.data_source === 'live') return '实时行情'
  const syncedAt = signal.data_updated_at?.replace('T', ' ').slice(0, 19)
  return syncedAt ? `本地缓存 · ${syncedAt}` : '本地缓存'
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

async function loadContracts(): Promise<void> {
  const { data } = await client.get<ContractConfig[]>('/contracts')
  contracts.value = data
  if (!data.some((contract) => contract.id === selectedId.value) && data.length) {
    await selectContract(data[0]!.id)
  }
}

async function selectContract(id: number): Promise<void> {
  selectedId.value = id
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
    contracts.value.push(data)
    addVisible.value = false
    form.value = { exchange: '', code: '', name: '' }
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
  contracts.value = contracts.value.filter((item) => item.id !== contract.id)
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
  try {
    await loadContracts()
    timer = window.setInterval(() => void refreshSignals(), 3_000)
    alertTimer = window.setInterval(
      () => void refreshAlertSignals(!alertStreamConnected.value),
      30_000,
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
  if (timer) window.clearInterval(timer)
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
        <el-button :icon="Refresh" :loading="refreshing" plain @click="refreshSignals(true)">更新数据</el-button>
        <el-button :icon="Setting" plain @click="openCredentials">天勤账号</el-button>
        <el-button type="primary" :icon="Plus" @click="addVisible = true">添加合约</el-button>
      </div>
    </header>

    <section v-if="selectedContract" class="setup-section">
      <div class="contract-ident">
        <span>交易所</span><strong>{{ selectedContract.exchange }}</strong>
        <span>交易合约</span><strong>{{ displayContractCode(selectedContract.code) }}</strong>
      </div>
      <div class="setup-table-wrap">
        <table class="setup-table"><thead><tr><th>周期设置</th><th>M4设置</th><th>M3设置</th><th>M2设置</th><th>M1设置</th><th></th></tr></thead><tbody>
          <tr v-for="period in selectedContract.periods" :key="period.duration_seconds"><td>{{ period.label }}</td><td>{{ period.m4 }}</td><td>{{ period.m3 }}</td><td>{{ period.m2 }}</td><td>{{ period.m1 }}</td><td class="row-actions"><el-button :icon="EditPen" text title="编辑周期" @click="openPeriod(period)" /><el-button :icon="Delete" text type="danger" title="删除周期" @click="removePeriod(period)" /></td></tr>
        </tbody></table>
        <el-button class="add-period" :icon="Plus" text @click="openPeriod()">添加周期</el-button>
      </div>
    </section>

    <section class="monitor-section">
      <div class="contract-list-wrap"><h2>交易合约</h2><table class="contract-table"><tbody><tr v-for="contract in contracts" :key="contract.id" :class="{ active: contract.id === selectedId }" @click="selectContract(contract.id)"><td>{{ contract.exchange }}</td><td><strong>{{ displayContractCode(contract.code) }}</strong></td><td class="contract-actions"><el-tooltip content="更换合约" placement="top"><el-button :icon="EditPen" text aria-label="更换合约" @click.stop="openContractEdit(contract)" /></el-tooltip><el-tooltip content="删除该合约" placement="top"><el-button :icon="Delete" text type="danger" aria-label="删除该合约" @click.stop="removeContract(contract)" /></el-tooltip></td></tr></tbody></table><div v-if="!contracts.length" class="empty-list">添加合约后在这里查看</div></div>
      <div class="signal-area">
        <div v-if="errorText" class="data-error">{{ errorText }}</div>
        <table v-if="signals.length" class="signal-table"><thead><tr><th>周期</th><th>均线4</th><th>均线3</th><th>均线1上穿/下穿均线2</th><th>备注</th></tr></thead><tbody><tr v-for="signal in signals" :key="signal.period.duration_seconds" :class="{ selected: selectedDuration === signal.period.duration_seconds }" @click="selectedDuration = signal.period.duration_seconds"><td><strong>{{ signal.period.label }}</strong></td><td><span class="trend" :class="signal.trend.m4 === '多' ? 'bullish' : 'bearish'">{{ signal.trend.m4 }}</span></td><td><span class="trend" :class="signal.trend.m3 === '多' ? 'bullish' : 'bearish'">{{ signal.trend.m3 }}</span></td><td class="signal-state-cell"><div class="state signal-state-layout" :class="signal.cross_type === '金叉' ? 'golden' : signal.cross_type === '死叉' ? 'death' : ''"><span>{{ signal.label }}</span><span class="cross-type">{{ signal.cross_type || '' }}</span></div></td><td class="period-note-cell" @click.stop><el-input :model-value="periodNotes[signal.period.duration_seconds] || ''" maxlength="500" size="small" @input="schedulePeriodNoteSave(signal, $event)" @blur="flushPeriodNote(signal)" /></td></tr></tbody></table>
        <div v-else-if="!contracts.length" class="empty-workspace">添加并选择一个合约</div><div v-else-if="!errorText" class="empty-signals">当前合约暂无周期配置</div>
        <footer v-if="selectedContract" class="ma-footer"><span>均线数值 · {{ displayContractCode(selectedContract.code) }} · {{ selectedSignal?.period.label || '--' }}：</span><strong>M4：{{ maText('M4') }}{{ longTrendText('m4') }}</strong><strong>M3：{{ maText('M3') }}{{ longTrendText('m3') }}</strong><strong>M2：{{ maText('M2') }}</strong><strong>M1：{{ maText('M1') }}</strong><small v-if="selectedSignal">{{ selectedSignal.period.label }} · {{ selectedSignal.bar_time.replace('T', ' ').slice(0, 19) }} · {{ dataStatusText(selectedSignal) }}</small><small v-else>等待可用的实时 K 线数据</small></footer>
        <section class="alert-center" aria-label="全品种预警">
          <header class="alert-center-header">
            <div><span class="alert-kicker">MARKET SENTINEL</span><h2>全品种预警</h2></div>
            <span class="alert-status" :class="{ scanning: alertScanning }">{{ alertStatusText }}</span>
          </header>
          <div class="alert-control-row">
            <strong>预警周期</strong>
            <div class="alert-options">
              <button
                v-for="period in commonPeriods"
                :key="period.durationSeconds"
                type="button"
                :class="{ active: selectedAlertDurations.includes(period.durationSeconds) }"
                :aria-pressed="selectedAlertDurations.includes(period.durationSeconds)"
                @click="toggleAlertDuration(period.durationSeconds)"
              >{{ period.label }}</button>
            </div>
          </div>
          <div class="alert-control-row conditions">
            <strong>预警条件</strong>
            <div class="alert-options">
              <button
                v-for="condition in alertConditionOptions"
                :key="condition"
                type="button"
                :class="{ active: selectedAlertConditions.includes(condition), death: condition.endsWith('死叉') }"
                :aria-pressed="selectedAlertConditions.includes(condition)"
                @click="toggleAlertCondition(condition)"
              >{{ condition }}</button>
            </div>
          </div>
          <div class="alert-results">
            <div v-if="matchingAlertSignals.length" class="alert-result-grid">
              <div v-for="signal in matchingAlertSignals" :key="`${signal.contract_id}-${signal.period.duration_seconds}`" class="alert-result-item">
                <span class="signal-pulse"></span>
                <span class="alert-result-text">{{ alertText(signal) }}</span>
                <span v-if="isLatestAlertSignal(signal)" class="alert-new-badge">NEW</span>
              </div>
            </div>
            <div v-else class="alert-empty">{{ alertStatusText }}</div>
          </div>
          <footer class="alert-center-footer">
            <span>仅监测当前账号自选合约已配置的周期</span>
            <span v-if="notificationPermission === 'denied'">Windows通知未授权，声音和页面弹窗仍可使用</span>
            <span v-else-if="notificationPermission === 'granted'">Windows通知已开启</span>
          </footer>
        </section>
      </div>
    </section>

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
      <el-form label-position="top" @submit.prevent="createContract"><el-form-item label="合约代码"><el-autocomplete v-model="form.code" class="full-width" value-key="value" placeholder="例如 pv、fg 或 RB2609" :fetch-suggestions="fetchContractSuggestions" :loading="suggestionLoading" :trigger-on-focus="false" @input="handleContractCodeInput" @select="selectContractSuggestion"><template #default="{ item }"><div class="contract-suggestion"><strong>{{ item.display }}</strong><span>{{ item.exchange }}</span></div></template></el-autocomplete><div class="recognition" :class="{ invalid: form.code && !recognition.exchange, incomplete: recognition.exchange && !recognition.complete }"><template v-if="recognition.exchange && recognition.complete">已识别：{{ recognition.exchangeName }} {{ recognition.exchange }} · 数据代码 {{ recognition.code }}</template><template v-else-if="recognition.exchange">已识别{{ recognition.exchangeName }}，请选择下方当前可用合约，或继续输入交割月份</template><template v-else-if="form.code">未识别品种代码，请检查品种简称和月份。</template><template v-else>输入品种前缀后自动列出当前可用合约，大小写均可；PVC 可输入 pv 或 pvc。</template></div></el-form-item><el-form-item label="交易所"><el-select v-model="form.exchange" placeholder="输入代码后自动选择"><el-option label="郑商所 CZCE" value="CZCE" /><el-option label="大商所 DCE" value="DCE" /><el-option label="上期所 SHFE" value="SHFE" /><el-option label="中金所 CFFEX" value="CFFEX" /><el-option label="广期所 GFEX" value="GFEX" /></el-select></el-form-item><el-form-item label="显示名称"><el-input v-model="form.name" placeholder="可留空" /></el-form-item></el-form>
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
