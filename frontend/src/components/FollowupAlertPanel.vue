<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Close } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { client } from '@/api/client'
import { alertConditionOptions, alertStateOptions, commonPeriods } from '@/lib/alertOptions'
import type { AlertSignalSnapshot, ContractConfig, FollowupRule } from '@/types/futures'

const props = defineProps<{ contracts: ContractConfig[], collapsed: boolean }>()
const emit = defineEmits<{ toggle: [] }>()
const rules = ref<FollowupRule[]>([])
const dialogVisible = ref(false)
const source = ref<AlertSignalSnapshot | null>(null)
const duration = ref<number | null>(null)
const conditions = ref<string[]>([])
const states = ref<string[]>([])
const saving = ref(false)
const deletingId = ref<number | null>(null)
const replacing = ref(false)
const reviewVisible = ref(false)
const review = ref<FollowupRule | null>(null)
const reviewError = ref('')
const errorText = ref('')
const popupItems = ref<FollowupRule[]>([])
const sourceContract = computed(() => props.contracts.find(c => c.id === source.value?.contract_id))
const pendingAction = computed(() => saving.value || replacing.value || deletingId.value !== null)
const reviewLatest = computed(() => rules.value.find(rule => rule.id === review.value?.id))
const reviewStale = computed(() => review.value && (
  JSON.stringify(review.value.source_current) !== JSON.stringify(reviewLatest.value?.source_current)
  || JSON.stringify(review.value.source) !== JSON.stringify(reviewLatest.value?.source)
))
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

function currentSourceText(rule: FollowupRule): string {
  const current = rule.source_current
  return current ? [rule.symbol, current.period_label, current.condition, current.state].filter(Boolean).join(' · ') : '当前收盘状态暂不可用，等待行情更新'
}

function sourceHint(rule: FollowupRule): string {
  if (rule.source_changed == null) return '固定条件状态暂不可用，点击查看'
  return rule.source_changed
    ? `固定条件已变化：${rule.source.state} → ${rule.source_current?.state}；${currentSourceText(rule)}；点击查看并更换`
    : `固定条件未变化：${currentSourceText(rule)}；点击查看`
}

async function openReview(rule: FollowupRule): Promise<void> {
  if (pendingAction.value) return
  review.value = JSON.parse(JSON.stringify(rule)) as FollowupRule
  reviewError.value = ''
  reviewVisible.value = true
  await refresh()
  const latest = rules.value.find(item => item.id === rule.id)
  if (reviewVisible.value && review.value?.id === rule.id && latest) {
    review.value = JSON.parse(JSON.stringify(latest)) as FollowupRule
  }
}

async function replaceSource(): Promise<void> {
  const item = review.value
  if (!item?.source_current || !item.source_changed || reviewStale.value || pendingAction.value) return
  replacing.value = true
  reviewError.value = ''
  try {
    await refreshTask
    const { data } = await client.post<{ rules: FollowupRule[] }>(`/followup-alerts/${item.id}/source`, {
      previous: item.source, observed: item.source_current,
    })
    rules.value = data.rules
    reviewVisible.value = false
    ElMessage.success('固定条件已更换，变换条件继续监控')
  } catch (error: any) {
    reviewError.value = error.response?.data?.detail || '更换失败，原固定条件已保留，请重试'
  } finally {
    replacing.value = false
  }
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
    if (review.value?.id === rule.id) reviewVisible.value = false
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
  <section class="followup-center" :class="{ collapsed }" aria-label="二级条件监控">
    <header class="followup-header" @click="emit('toggle')"><h2><button type="button" :aria-expanded="!collapsed" aria-controls="followup-list" @click.stop="emit('toggle')">二级条件监控</button></h2><div class="followup-header-actions"><span>{{ rules.length }} 条条件单</span><button type="button" class="panel-toggle" :aria-expanded="!collapsed" aria-controls="followup-list" @click.stop="emit('toggle')">{{ collapsed ? '展开' : '折叠' }}</button></div></header>
    <p v-if="errorText" v-show="!collapsed" class="followup-error" role="status">{{ errorText }}</p>
    <div id="followup-list" v-show="!collapsed" class="followup-list">
      <div class="followup-columns" aria-hidden="true"><span>固定条件</span><span>状态</span><span>变换条件</span><span>删除</span><span>触发时间（北京时间）</span></div>
      <div v-for="rule in rules" :key="rule.id" class="followup-rule" :data-rule-id="rule.id">
        <button type="button" class="followup-source followup-rule-text" :title="sourceHint(rule)" :disabled="pendingAction" @click="openReview(rule)">{{ sourceText(rule) }}</button>
        <div class="followup-state-cell"><button type="button" class="followup-source-dot" :class="{ changed: rule.source_changed === true, normal: rule.source_changed === false }" :title="sourceHint(rule)" :aria-label="sourceHint(rule)" :disabled="pendingAction" @click="openReview(rule)"><span /></button></div>
        <span class="followup-target followup-rule-text" :title="`${targetText(rule)}；${rule.status}`">{{ targetText(rule) }}</span>
        <div class="followup-delete-cell"><button type="button" class="followup-delete" aria-label="删除二级条件单" :title="`删除 ${targetText(rule)}`" :disabled="pendingAction" @click="remove(rule)"><el-icon><Close /></el-icon></button></div>
        <time class="followup-time" :datetime="rule.last_triggered_at || undefined" :title="`${triggerTime(rule.last_triggered_at)}；${rule.status}；最近一次进入目标条件的首根K线收盘时间（北京时间）`">{{ triggerTime(rule.last_triggered_at) || (rule.trigger_count ? '已满足，起点待确认' : '') }}</time>
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
      <p v-if="source" class="followup-dialog-source">固定条件：{{ source.symbol }} · {{ source.period.label }} · {{ conditionText(source) }} · {{ source.label }}</p>
      <div class="alert-control-row"><strong>预警周期</strong><div class="alert-options"><button v-for="period in commonPeriods" :key="period.durationSeconds" type="button" :disabled="saving || !hasPeriod(period.durationSeconds)" :title="hasPeriod(period.durationSeconds) ? period.label : '请先为该合约配置此周期的均线参数'" :class="{ active: duration === period.durationSeconds }" :aria-pressed="duration === period.durationSeconds" @click="duration = period.durationSeconds">{{ period.label }}</button></div></div>
      <div class="alert-control-row conditions"><strong>预警条件</strong><div class="alert-options"><button v-for="condition in alertConditionOptions" :key="condition" type="button" :disabled="saving" :class="{ active: conditions.includes(condition), death: condition.endsWith('死叉') }" :aria-pressed="conditions.includes(condition)" @click="conditions = toggle(conditions, condition)">{{ condition }}</button></div></div>
      <div class="alert-control-row states"><strong>状态条件</strong><div class="alert-options"><button v-for="state in alertStateOptions" :key="state" type="button" :disabled="saving" :class="{ active: states.includes(state) }" :aria-pressed="states.includes(state)" @click="states = toggle(states, state)">{{ state }}</button></div></div>
      <p class="followup-choice-note">条件、状态留空表示不限；同栏任选，跨栏同时满足。灰色周期需先配置均线。</p>
      <template #footer><el-button :disabled="saving" @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="saving" :disabled="!duration" @click="save">确定</el-button></template>
    </el-dialog>

    <el-dialog v-model="reviewVisible" class="followup-dialog followup-review-dialog" title="固定条件对比" width="640px" align-center :close-on-click-modal="!replacing" :close-on-press-escape="!replacing" :show-close="!replacing" destroy-on-close>
      <div v-if="review" class="followup-review">
        <div><strong>已保存条件</strong><p>{{ sourceText(review) }}</p></div>
        <div :class="{ 'source-review-changed': review.source_changed }"><strong>当前已收盘状态</strong><p>{{ currentSourceText(review) }}</p><small>{{ review.source_changed == null ? '等待有效收盘行情' : review.source_changed ? '固定条件已变化，请结合盘面判断是否继续等待。' : '当前状态与已保存条件一致。' }}</small></div>
        <div><strong>变换条件</strong><p>{{ targetText(review) }}</p><small>继续监控和提醒；更换只更新当前这一行的固定条件。</small></div>
        <p v-if="reviewStale" class="followup-review-error" role="status">条件已更新，请刷新对比后再操作。</p>
        <p v-if="reviewError" class="followup-review-error" role="alert">{{ reviewError }}</p>
      </div>
      <template #footer><el-button :disabled="replacing" @click="reviewVisible = false">关闭</el-button><el-button v-if="review" :disabled="replacing" @click="openReview(review)">刷新对比</el-button><el-button type="primary" :loading="replacing" :disabled="!review?.source_current || !review.source_changed || reviewStale" @click="replaceSource">更换</el-button></template>
    </el-dialog>
  </section>
</template>
