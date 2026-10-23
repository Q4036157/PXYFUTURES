export type AlertCondition =
  | '多多金叉' | '多空金叉' | '空多金叉' | '空空金叉'
  | '多多死叉' | '多空死叉' | '空多死叉' | '空空死叉'
export type AlertState = '上涨' | '回调' | '多震荡' | '下跌' | '反弹' | '空震荡'

export const commonPeriods = [
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
export const alertConditionOptions: AlertCondition[] = [
  '多多金叉', '多空金叉', '空多金叉', '空空金叉',
  '多多死叉', '多空死叉', '空多死叉', '空空死叉',
]
export const alertStateOptions: AlertState[] = ['上涨', '回调', '多震荡', '下跌', '反弹', '空震荡']
