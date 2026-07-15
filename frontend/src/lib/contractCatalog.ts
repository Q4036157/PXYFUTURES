export const PRODUCT_EXCHANGES: Record<string, string> = {
  AP: 'CZCE', CF: 'CZCE', CJ: 'CZCE', FG: 'CZCE', MA: 'CZCE', OI: 'CZCE', PF: 'CZCE', PK: 'CZCE', PM: 'CZCE', PR: 'CZCE', PX: 'CZCE', RI: 'CZCE', RM: 'CZCE', RS: 'CZCE', SA: 'CZCE', SF: 'CZCE', SH: 'CZCE', SM: 'CZCE', SR: 'CZCE', TA: 'CZCE', UR: 'CZCE', WH: 'CZCE', ZC: 'CZCE', LR: 'CZCE',
  A: 'DCE', B: 'DCE', BB: 'DCE', C: 'DCE', CS: 'DCE', EB: 'DCE', EG: 'DCE', FB: 'DCE', I: 'DCE', J: 'DCE', JD: 'DCE', JM: 'DCE', L: 'DCE', LH: 'DCE', M: 'DCE', P: 'DCE', PG: 'DCE', PP: 'DCE', RR: 'DCE', V: 'DCE', Y: 'DCE',
  AG: 'SHFE', AL: 'SHFE', AO: 'SHFE', AU: 'SHFE', BR: 'SHFE', BU: 'SHFE', CU: 'SHFE', HC: 'SHFE', NI: 'SHFE', PB: 'SHFE', RB: 'SHFE', RU: 'SHFE', SN: 'SHFE', SP: 'SHFE', SS: 'SHFE', WR: 'SHFE', ZN: 'SHFE',
  BC: 'INE', EC: 'INE', LU: 'INE', NR: 'INE', SC: 'INE',
  IC: 'CFFEX', IF: 'CFFEX', IH: 'CFFEX', IM: 'CFFEX', T: 'CFFEX', TF: 'CFFEX', TL: 'CFFEX', TS: 'CFFEX',
  LC: 'GFEX', PS: 'GFEX', PT: 'GFEX', SI: 'GFEX',
}

const exchangeNames: Record<string, string> = { CZCE: '郑商所', DCE: '大商所', SHFE: '上期所', INE: '上期能源', CFFEX: '中金所', GFEX: '广期所' }
const productAliases: Record<string, string> = { PVC: 'V', PV: 'V' }

export interface ContractRecognition { code: string; exchange: string | null; exchangeName: string | null; complete: boolean }

export function recognizeContract(input: string): ContractRecognition {
  const code = input.replace(/\s+/g, '').toUpperCase()
  const letters = (code.match(/^[A-Z]+/)?.[0] || '')
  // 不用宽松 startsWith：PV 不能误识别成 P，交易所必须准确。
  const product = productAliases[letters] ?? (letters in PRODUCT_EXCHANGES ? letters : null)
  const exchange = product ? PRODUCT_EXCHANGES[product] ?? null : null
  const suffix = code.slice(letters.length)
  const standardCode = product ? `${product}${suffix}` : code
  return { code: standardCode, exchange, exchangeName: exchange ? exchangeNames[exchange] ?? null : null, complete: Boolean(product && /^\d{3,4}$/.test(suffix)) }
}
