const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const { chromium } = require(process.env.PXY_PLAYWRIGHT_MODULE || 'playwright')
const dist = process.env.PXY_LAYOUT_DIST || path.resolve(__dirname, '../dist')
const periods = [300, 600].map((duration_seconds, id) => ({ id: id + 1, label: `${duration_seconds / 60}分钟`, duration_seconds, m4: 240, m3: 60, m2: 21, m1: 4, note: '' }))
const fixture = [{ id: 1, name: 'AA', sort_order: 0, contracts: ['SR701', 'AP701', 'FG701'].map((code, i) => ({ id: i + 1, symbol: `CZCE.${code}`, exchange: 'CZCE', code, name: code, periods, group_id: 1, sort_order: i })) }, { id: 2, name: 'BB', sort_order: 1, contracts: [] }]
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.wav': 'audio/wav' }
const signalBaseTime = Date.now() - 120000
function signal(minutes = 0, contract = fixture[0].contracts[0], period = periods[0]) {
  const time = new Date(signalBaseTime + minutes * 60000).toISOString()
  return { contract_id: contract.id, symbol: contract.symbol, name: contract.name, period, trend: { m3: '空', m4: '空' }, cross_type: '金叉', cross_time: time, label: '反弹', state_since: time, ma_values: { M1: 1, M2: 2, M3: 3, M4: 4 }, bar_close: 1, bar_time: time, confirmed_bar_time: time, confirmed_at: new Date().toISOString(), data_source: 'live', data_status: 'current', data_updated_at: time }
}
async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } })
  const page = await context.newPage()
  const state = structuredClone(fixture), errors = [], orderRequests = [], results = []
  let sockets = [], scanSignals = [], failOrder = false, releaseOrder = null, holdOrder = false
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => localStorage.setItem('pxy-futures-alert-preferences-v1', JSON.stringify({ durations: [300, 600], conditions: [], states: [] })))
  await page.routeWebSocket('**/*', socket => { sockets.push(socket); socket.onMessage(() => {}) })
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), parts = url.pathname.split('/'), last = parts.at(-1), method = request.method()
    if (url.pathname.includes('/api/')) {
      const body = request.postDataJSON()
      let data
      if (last === 'groups') data = state
      else if (last === 'order') {
        orderRequests.push(body.contract_ids)
        if (holdOrder) await new Promise(resolve => { releaseOrder = resolve })
        if (failOrder) return route.fulfill({ status: 500, json: { detail: '模拟保存失败' } })
        const group = state.find(item => item.id === Number(parts.at(-2)))
        group.contracts = body.contract_ids.map(id => group.contracts.find(item => item.id === id))
        data = { ok: true }
      } else if (last === 'group') {
        const source = state.find(group => group.contracts.some(item => item.id === Number(parts.at(-2))))
        const [moved] = source.contracts.splice(source.contracts.findIndex(item => item.id === Number(parts.at(-2))), 1)
        moved.group_id = body.group_id
        state.find(group => group.id === body.group_id).contracts.push(moved)
        data = moved
      } else if (last === 'signals') data = { contract_id: Number(parts.at(-2)), symbol: 'CZCE.SR701', market_status: 'active', signals: periods.map(period => signal(0, fixture[0].contracts[0], period)), warnings: [] }
      else if (last === 'scan') data = { signals: scanSignals, requested: 6, failed: 0 }
      else if (last === 'suggestions') data = []
      else if (last === 'contracts' && method === 'POST') {
        data = { ...body, id: 4, symbol: `${body.exchange}.${body.code}`, periods, sort_order: 3 }
        state[0].contracts.push(data)
      } else throw Error(`未预期接口 ${method} ${url.pathname}`)
      return route.fulfill({ json: data })
    }
    const file = path.join(dist, url.pathname.slice('/futures-app/'.length) || 'index.html')
    return route.fulfill(fs.existsSync(file) ? { body: fs.readFileSync(file), contentType: mime[path.extname(file)] || 'application/octet-stream' } : { status: 404, body: '资源不存在' })
  })
  const codes = () => page.locator('.contract-code strong').allTextContents()
  const closePopup = async () => {
    await page.getByRole('button', { name: '关闭预警弹窗', exact: true }).click()
    await page.locator('.market-alert-popup').waitFor({ state: 'detached' })
  }
  const send = async items => {
    scanSignals = items
    sockets.at(-1).send(JSON.stringify({ type: 'snapshot', signals: items, requested: items.length, failed: 0, trigger: 'bar_closed', server_sent_at: new Date().toISOString() }))
    await page.waitForTimeout(120)
  }
  try {
    await page.goto('http://futures-regression.local/futures-app/')
    await page.waitForSelector('.signal-table tbody tr')
    assert.deepEqual(await codes(), ['SR701', 'AP701', 'FG701'])
    assert.equal(await page.getByRole('button', { name: '上移合约', exact: true }).first().isDisabled(), true)
    assert.equal(await page.getByRole('button', { name: '下移合约', exact: true }).last().isDisabled(), true)
    await page.getByRole('button', { name: '下移合约', exact: true }).first().click()
    await page.waitForFunction(() => document.querySelector('.contract-code strong')?.textContent === 'AP701')
    await page.waitForFunction(() => !document.querySelectorAll('[aria-label="下移合约"]')[0].disabled)
    assert.deepEqual(orderRequests.at(-1), [2, 1, 3])
    assert.equal(await page.locator('.signal-panel-header strong').textContent(), 'SR701')
    await page.getByRole('button', { name: '上移合约', exact: true }).last().click()
    await page.waitForFunction(() => document.querySelectorAll('.contract-code strong')[1]?.textContent === 'FG701')
    await page.waitForFunction(() => !document.querySelectorAll('[aria-label="下移合约"]')[0].disabled)
    await page.reload()
    await page.waitForSelector('.signal-table tbody tr')
    assert.deepEqual(await codes(), ['AP701', 'FG701', 'SR701'])
    results.push('上下按钮保存顺序、首尾禁用、点击不切换合约、刷新保留')
    holdOrder = true
    await page.getByRole('button', { name: '下移合约', exact: true }).first().click()
    await page.waitForFunction(() => [...document.querySelectorAll('.contract-actions button')].every(button => button.disabled))
    assert.equal(await page.locator('.contract-row[draggable="true"]').count(), 0)
    holdOrder = false
    releaseOrder()
    await page.waitForFunction(() => !document.querySelectorAll('[aria-label="下移合约"]')[0].disabled)
    failOrder = true
    const beforeFailure = await codes()
    await page.getByRole('button', { name: '下移合约', exact: true }).first().click()
    await page.getByText('模拟保存失败', { exact: true }).waitFor()
    assert.deepEqual(await codes(), beforeFailure)
    failOrder = false
    results.push('保存期间禁用并发排序，保存失败恢复服务器顺序')
    await page.getByRole('button', { name: '向该分组添加合约', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '添加合约', exact: true })
    await dialog.getByPlaceholder('例如 pv、fg 或 RB2609').fill('CF701')
    await dialog.getByRole('button', { name: '验证并添加', exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('.contract-row').length === 4)
    assert.equal((await codes()).at(-1), 'CF701')
    await page.reload()
    await page.waitForSelector('.signal-table tbody tr')
    assert.equal((await codes()).at(-1), 'CF701')
    results.push('新合约追加在自定义顺序末尾并刷新保留')
    const first = signal(), next = signal(1)
    await send([first, first])
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+1'])
    await send([first]); await send([first])
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+1'])
    await closePopup()
    await page.waitForTimeout(5100)
    await page.waitForTimeout(350)
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+1'])
    await send([next])
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+2'])
    await closePopup()
    await send([first])
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+2'])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    results.push('重复批次、WebSocket重复与HTTP轮询不累计，真实新事件+2，迟到消息不回退或重弹')
    const other = signal(0, fixture[0].contracts[1]), otherPeriod = signal(0, fixture[0].contracts[0], periods[1])
    await send([next, other, otherPeriod])
    assert.deepEqual((await page.locator('.alert-new-badge').allTextContents()).sort(), ['+1', '+1', '+2'])
    await closePopup()
    sockets.at(-1).close({ code: 1000 })
    await page.waitForTimeout(1500)
    await send([next, other, otherPeriod])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    assert.deepEqual((await page.locator('.alert-new-badge').allTextContents()).sort(), ['+1', '+1', '+2'])
    results.push('不同合约和周期独立累计，断线重连不重复预警')
    await send([{ ...signal(2), data_status: 'historical', data_source: 'historical' }])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    await send([{ ...signal(3), cross_time: null }])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    await send([signal(4, fixture[0].contracts[0], { ...periods[0], duration_seconds: 1800, label: '30分钟' })])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    await send([next, other, otherPeriod])
    assert.deepEqual((await page.locator('.alert-new-badge').allTextContents()).sort(), ['+1', '+1', '+2'])
    results.push('历史行情、无交叉与未选周期不触发或增加计数')
    const screenshotDir = process.env.PXY_SCREENSHOT_DIR || require('node:os').tmpdir()
    for (const width of [1440, 1024, 960, 721, 720, 390, 320]) {
      await page.setViewportSize({ width, height: 1100 })
      await page.waitForTimeout(100)
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `页面溢出：${width}`)
      const bounds = await page.locator('.contract-row').evaluateAll(rows => rows.map(row => ({ row: row.getBoundingClientRect().right, action: row.querySelector('.contract-actions').getBoundingClientRect().right, code: row.querySelector('.contract-code strong').getBoundingClientRect() })).map(item => ({ fits: item.action <= item.row + 1, codeWidth: item.code.width })))
      assert.ok(bounds.every(item => item.fits && item.codeWidth > 30), `合约代码或按钮挤压：${width}`)
      if ([1440, 390].includes(width)) await page.screenshot({ path: path.join(screenshotDir, `pxyfutures-sort-count-${width}.png`), fullPage: true })
    }
    assert.deepEqual(errors, [])
    results.push('七种桌面和手机宽度无溢出、代码与按钮可见、无页面异常')
    console.log(JSON.stringify({ passed: results.length, results }, null, 2))
  } finally { await context.close(); await browser.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
