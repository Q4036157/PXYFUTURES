<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Close } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { client } from '@/api/client'
import { alertConditionOptions, alertStateOptions, commonPeriods } from '@/lib/alertOptions'
import type { AlertSignalSnapshot, ContractConfig, FollowupRule } from '@/types/futures'

const props = defineProps<{ contracts: ContractConfig[] }>()
const rules = ref<FollowupRule[]>([])
const dialogVisible = ref(false)
const source = ref<AlertSignalSnapshot | null>(null)
const duration = ref<number | null>(null)
const conditions = ref<string[]>([])
const states = ref<string[]>([])
const saving = ref(false)
const deletingId = ref<number | null>(null)
const errorText = ref('')
const popupItems = ref<FollowupRule[]>([])
const sourceContract = computed(() => props.contracts.find(c => c.id === source.value?.contract_id))
const pendingAction = computed(() => saving.value || deletingId.value !== null)
let alive = false
let timer: number | undefined
let refreshTask: Promise<void> | null = null
let audio: AudioContext | null = null

function conditionText(signal: AlertSignalSnapshot): string {
  return signal.cross_type ? `${signal.trend.m4}${signal.trend.m3}${signal.cross_type}` : ''
}

function periodLabel(value: number): string {
  return commonPeriods.find(p => p.durationSeconds === value)?.label ?? `${value / 60}分钟`
}

function sourceText(rule: FollowupRule): string {
  return [rule.symbol, rule.source.period_label, rule.source.condition, rule.source.state]
    .filter(Boolean).join(' · ')
}

function targetText(rule: FollowupRule): string {
  return [rule.symbol, periodLabel(rule.duration_seconds),
    rule.conditions.join('／'), rule.states.join('／')].filter(Boolean).join(' · ')
}

function triggerTime(value: string | null): string {
  if (!value) return ''
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(new Date(value))
}

function hasPeriod(value: number): boolean {
  return sourceContract.value?.periods.some(p => p.duration_seconds === value) ?? false
}

function toggle(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter(item => item !== value) : [...values, value]
}

function unlockSound(): void {
  try {
    audio ??= new AudioContext()
    void audio.resume().catch(() => undefined)
  } catch {
    // 无音频设备时仍保留页面提醒与触发时间。
  }
}

function playSound(): void {
  unlockSound()
  if (!audio) return
  const context = audio
  for (const [index, frequency] of [880, 1174.66, 1567.98, 880, 1174.66, 1567.98].entries()) {
    const oscillator = context.createOscillator()
    const gain = context.createGain()
    const start = context.currentTime + index * 0.22
    oscillator.type = 'sine'
    oscillator.frequency.value = frequency
    gain.gain.setValueAtTime(0, start)
    gain.gain.linearRampToValueAtTime(0.12, start + 0.015)
    gain.gain.exponentialRampToValueAtTime(0.001, start + 0.18)
    oscillator.connect(gain)
    gain.connect(context.destination)
    oscillator.onended = () => { oscillator.disconnect(); gain.disconnect() }
    oscillator.start(start)
    oscillator.stop(start + 0.2)
  }
}

function open(signal: AlertSignalSnapshot): void {
  source.value = JSON.parse(JSON.stringify(signal)) as AlertSignalSnapshot
  duration.value = null
  conditions.value = []
  states.value = []
  dialogVisible.value = true
  unlockSound()
  if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
    void Notification.requestPermission().catch(() => undefined)
  }
}

async function claimNotifications(): Promise<void> {
  const { data } = await client.post<{ notifications: FollowupRule[] }>('/followup-alerts/notifications/claim')
  if (!alive) return
  const activeIds = new Set(rules.value.map(rule => rule.id))
  const items = data.notifications.filter(rule => activeIds.has(rule.id))
  if (!items.length) return
  rules.value = rules.value.map(rule => {
    const notification = items.find(item => item.id === rule.id)
    return notification && notification.trigger_count > rule.trigger_count
      ? { ...rule, trigger_count: notification.trigger_count, last_triggered_at: notification.last_triggered_at }
      : rule
  })
  popupItems.value = items
  playSound()
  if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
    new Notification(`二级预警 · ${items.length}条条件成立`, {
      body: items.slice(0, 5).map(rule => `${targetText(rule)}\n北京时间 ${triggerTime(rule.last_triggered_at)}`).join('\n'),
      tag: 'pxy-futures-followup-alert',
    })
  }
}

function refresh(): Promise<void> {
  if (refreshTask) return refreshTask
  if (pendingAction.value || !alive) return Promise.resolve()
  refreshTask = (async () => {
    try {
      const { data } = await client.get<{ rules: FollowupRule[] }>('/followup-alerts')
      if (!alive) return
      rules.value = data.rules
      // 更新列表后再领取，避免提醒早于条件单显示。
      await claimNotifications()
      errorText.value = ''
    } catch (error: any) {
      if (alive) errorText.value = error.response?.data?.detail || '二级监控连接暂时不可用，正在重试'
    } finally {
      refreshTask = null
    }
  })()
  return refreshTask
}

async function save(): Promise<void> {
  if (!source.value || !duration.value || pendingAction.value) return
  saving.value = true
  try {
    await refreshTask
    const signal = source.value
    const { data } = await client.post<{ rules: FollowupRule[] }>('/followup-alerts', {
      contract_id: signal.contract_id,
      source: { duration_seconds: signal.period.duration_seconds, condition: conditionText(signal), state: signal.label },
      duration_seconds: duration.value,
      conditions: conditions.value,
      states: states.value,
    })
    rules.value = data.rules
    dialogVisible.value = false
    errorText.value = ''
    await claimNotifications()
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '条件单保存或提醒读取失败，请检查下方监控列表')
  } finally {
    saving.value = false
  }
}

async function remove(rule: FollowupRule): Promise<void> {
  if (pendingAction.value) return
  deletingId.value = rule.id
  try {
    await refreshTask
    await client.delete(`/followup-alerts/${rule.id}`)
    rules.value = rules.value.filter(item => item.id !== rule.id)
    popupItems.value = popupItems.value.filter(item => item.id !== rule.id)
    errorText.value = ''
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '条件单删除失败，请重试')
  } finally {
    deletingId.value = null
  }
}

onMounted(() => {
  alive = true
  window.addEventListener('pointerdown', unlockSound, { once: true })
  window.addEventListener('keydown', unlockSound, { once: true })
  void refresh()
  timer = window.setInterval(() => void refresh(), 3_000)
})
onBeforeUnmount(() => {
  alive = false
  if (timer) window.clearInterval(timer)
  window.removeEventListener('pointerdown', unlockSound)
  window.removeEventListener('keydown', unlockSound)
  void audio?.close().catch(() => undefined)
})
defineExpose({ open })
</script>

<template>
  <section class="followup-center" aria-label="二级条件监控">
    <header class="followup-header"><h2>二级条件监控</h2><span>{{ rules.length }} 条条件单</span></header>
    <p v-if="errorText" class="followup-error" role="status">{{ errorText }}</p>
    <div class="followup-list">
      <div v-for="rule in rules" :key="rule.id" class="followup-rule" :data-rule-id="rule.id">
        <div class="followup-rule-text" :title="`一级条件：${sourceText(rule)}；二级条件：${targetText(rule)}；${rule.status}`">
          <span class="followup-source">一级条件：{{ sourceText(rule) }}</span>
          <span class="followup-target">二级条件：{{ targetText(rule) }}</span>
        </div>
        <button type="button" class="followup-delete" aria-label="删除二级条件单" :title="`删除 ${targetText(rule)}`" :disabled="pendingAction" @click="remove(rule)"><el-icon><Close /></el-icon></button>
        <time class="followup-time" :datetime="rule.last_triggered_at || undefined" :title="`${rule.status}；最近触发的收盘时刻（北京时间）`">{{ triggerTime(rule.last_triggered_at) }}<span v-if="rule.last_triggered_at"> 触发</span></time>
      </div>
      <p v-if="!rules.length" class="followup-empty">暂无条件单</p>
    </div>
    <aside v-if="popupItems.length" class="followup-popup" role="alert" aria-label="二级预警提示" aria-live="assertive">
      <button type="button" class="popup-close" aria-label="关闭二级预警提示" @click="popupItems = []"><el-icon><Close /></el-icon></button>
      <strong>二级预警 · 条件已成立</strong>
      <div v-for="rule in popupItems.slice(0, 5)" :key="`${rule.id}-${rule.trigger_count}`"><p>{{ targetText(rule) }}</p><time>北京时间 {{ triggerTime(rule.last_triggered_at) }}</time></div>
      <p v-if="popupItems.length > 5">另有 {{ popupItems.length - 5 }} 条，请查看条件单</p>
    </aside>

    <el-dialog v-model="dialogVisible" class="followup-dialog" title="设置二级预警" width="860px" align-center :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving" destroy-on-close>
      <p v-if="source" class="followup-dialog-source">一级条件：{{ source.symbol }} · {{ source.period.label }} · {{ conditionText(source) }} · {{ source.label }}</p>
      <div class="alert-control-row"><strong>预警周期</strong><div class="alert-options"><button v-for="period in commonPeriods" :key="period.durationSeconds" type="button" :disabled="saving || !hasPeriod(period.durationSeconds)" :title="hasPeriod(period.durationSeconds) ? period.label : '请先为该合约配置此周期的均线参数'" :class="{ active: duration === period.durationSeconds }" :aria-pressed="duration === period.durationSeconds" @click="duration = period.durationSeconds">{{ period.label }}</button></div></div>
      <div class="alert-control-row conditions"><strong>预警条件</strong><div class="alert-options"><button v-for="condition in alertConditionOptions" :key="condition" type="button" :disabled="saving" :class="{ active: conditions.includes(condition), death: condition.endsWith('死叉') }" :aria-pressed="conditions.includes(condition)" @click="conditions = toggle(conditions, condition)">{{ condition }}</button></div></div>
      <div class="alert-control-row states"><strong>状态条件</strong><div class="alert-options"><button v-for="state in alertStateOptions" :key="state" type="button" :disabled="saving" :class="{ active: states.includes(state) }" :aria-pressed="states.includes(state)" @click="states = toggle(states, state)">{{ state }}</button></div></div>
      <p class="followup-choice-note">条件、状态留空表示不限；同栏任选，跨栏同时满足。灰色周期需先配置均线。</p>
      <template #footer><el-button :disabled="saving" @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="saving" :disabled="!duration" @click="save">确定</el-button></template>
    </el-dialog>
  </section>
</template>
