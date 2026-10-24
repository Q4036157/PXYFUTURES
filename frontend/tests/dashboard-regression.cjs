const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const { chromium } = require(process.env.PXY_PLAYWRIGHT_MODULE || 'playwright')
const dist = process.env.PXY_LAYOUT_DIST || path.resolve(__dirname, '../dist')
const periods = [300, 600].map((duration_seconds, id) => ({ id: id + 1, label: `${duration_seconds / 60}分钟`, duration_seconds, m4: 240, m3: 60, m2: 21, m1: 4, note: '' }))
const oneMinutePeriod = { id: 3, label: '1分钟', duration_seconds: 60, m4: 200, m3: 100, m2: 21, m1: 4, note: '' }
const fixturePeriods = [...periods, oneMinutePeriod]
const fixture = [{ id: 1, name: 'AA', sort_order: 0, contracts: ['SR701', 'AP701', 'FG701'].map((code, i) => ({ id: i + 1, symbol: `CZCE.${code}`, exchange: 'CZCE', code, name: code, periods: fixturePeriods, group_id: 1, sort_order: i })) }, { id: 2, name: 'BB', sort_order: 1, contracts: [] }]
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.wav': 'audio/wav' }
const signalBaseTime = Date.now() - 120000
function signal(minutes = 0, contract = fixture[0].contracts[0], period = periods[0]) {
  const time = new Date(signalBaseTime + minutes * 60000).toISOString()
  return { contract_id: contract.id, symbol: contract.symbol, name: contract.name, period, trend: { m3: '空', m4: '空' }, cross_type: '金叉', cross_time: time, label: '反弹', state_since: time, state_bar_count: 1, ma_values: { M1: 1, M2: 2, M3: 3, M4: 4 }, bar_close: 1, bar_time: time, confirmed_bar_time: time, confirmed_at: new Date().toISOString(), data_source: 'live', data_status: 'current', data_updated_at: time }
}
async function main() {
  const screenshotDir = process.env.PXY_SCREENSHOT_DIR || require('node:os').tmpdir()
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } })
  const page = await context.newPage()
  const state = structuredClone(fixture), errors = [], orderRequests = [], results = []
  const contractRequests = []
  let sockets = [], scanSignals = [], failOrder = false, releaseOrder = null, holdOrder = false
  let followupRules = [], followupNotifications = [], followupRequests = [], nextFollowupId = 1
  let followupAlreadyMatches = false, failFollowupSave = false, failFollowupDelete = false
  let failSourceReplace = false
  let rejectDuplicateFollowup = false
  let holdSuggestions = false, releaseSuggestions = null, failSuggestions = false
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => {
    window.__alertSoundPlays = []
    window.__nativeMediaPlay = HTMLMediaElement.prototype.play
    HTMLMediaElement.prototype.play = function () {
      if (!this.muted) {
        window.__alertSoundPlays.push(new URL(this.src).pathname)
        queueMicrotask(() => this.dispatchEvent(new Event('ended')))
      }
      return Promise.resolve()
    }
    if (!localStorage.getItem('pxy-futures-alert-preferences-v1')) {
      localStorage.setItem('pxy-futures-alert-preferences-v1', JSON.stringify({ durations: [300, 600], conditions: [], states: [] }))
    }
  })
  await page.routeWebSocket('**/*', socket => { sockets.push(socket); socket.onMessage(() => {}) })
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), parts = url.pathname.split('/'), last = parts.at(-1), method = request.method()
    if (url.pathname.includes('/api/')) {
      const body = request.postDataJSON()
      let data
      if (url.pathname.includes('/api/followup-alerts')) {
        if (last === 'claim') {
          data = { notifications: followupNotifications.splice(0) }
        } else if (last === 'source' && method === 'POST') {
          if (failSourceReplace) return route.fulfill({ status: 409, json: { detail: '当前收盘状态已更新，请重新查看对比后更换' } })
          const rule = followupRules.find(rule => rule.id === Number(parts.at(-2)))
          assert.deepEqual(body.previous, rule.source)
          assert.deepEqual(body.observed, rule.source_current)
          const { confirmed_bar_time, ...baseline } = rule.source_current
          rule.source = baseline
          rule.source_changed = false
          data = { rules: followupRules }
        } else if (method === 'DELETE') {
          if (failFollowupDelete) return route.fulfill({ status: 500, json: { detail: '模拟二级条件删除失败' } })
          followupRules = followupRules.filter(rule => rule.id !== Number(last))
          followupNotifications = followupNotifications.filter(rule => rule.id !== Number(last))
          return route.fulfill({ status: 204 })
        } else if (method === 'POST') {
          if (rejectDuplicateFollowup) return route.fulfill({ status: 409, json: { detail: '已有完全相同的条件单，请修改固定条件或变换条件后再保存' } })
          if (failFollowupSave) return route.fulfill({ status: 500, json: { detail: '模拟二级条件保存失败' } })
          followupRequests.push(body)
          const rule = {
            ...body, id: nextFollowupId++, symbol: fixture[0].contracts[0].symbol,
            source: { ...body.source, period_label: `${body.source.duration_seconds / 60}分钟` },
            last_triggered_at: followupAlreadyMatches ? '2026-10-23T12:00:00+08:00' : null,
            last_triggered_state: followupAlreadyMatches ? '下跌' : null,
            last_bar_time: null, matched: followupAlreadyMatches,
            trigger_count: followupAlreadyMatches ? 1 : 0,
            status: followupAlreadyMatches ? '条件已满足' : '监控中',
          }
          rule.source_current = { ...rule.source, confirmed_bar_time: '2026-10-23T12:00:00+08:00' }
          rule.source_changed = false
          followupRules.push(rule)
          if (followupAlreadyMatches) followupNotifications.push(structuredClone(rule))
          data = { created_id: rule.id, rules: followupRules }
        } else data = { rules: followupRules }
      } else if (last === 'groups') data = state
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
      else if (last === 'suggestions') {
        const query = url.searchParams.get('query')?.toUpperCase()
        if (holdSuggestions && query === 'SH2701') await new Promise(resolve => { releaseSuggestions = resolve })
        if (failSuggestions) return route.fulfill({ status: 503, json: { detail: '模拟候选查询暂不可用，可输入完整合约后验证' } })
        data = query?.startsWith('SH') ? [{ value: 'SH2701', display: '烧碱SH2701', exchange: 'CZCE', symbol: 'CZCE.SH701' }] : []
      }
      else if (last === 'contracts' && method === 'POST') {
        contractRequests.push(body)
        const code = body.exchange === 'CZCE' ? body.code.replace(/^([A-Z]+)\d(\d{3})$/, '$1$2') : body.code
        if (state[0].contracts.some(contract => contract.symbol === `${body.exchange}.${code}`)) {
          return route.fulfill({ status: 409, json: { detail: '该合约已添加' } })
        }
        data = { ...body, code, id: 4, symbol: `${body.exchange}.${code}`, periods, sort_order: 3 }
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
  const assertViewportDialog = async dialog => {
    await dialog.waitFor()
    await page.waitForTimeout(350)
    const fit = await dialog.evaluate(root => {
      const node = root.matches('.followup-dialog') ? root : root.querySelector('.followup-dialog')
      const bounds = node.getBoundingClientRect()
      const overlay = node.closest('.el-overlay').getBoundingClientRect()
      const footer = node.querySelector('.el-dialog__footer').getBoundingClientRect()
      const button = node.querySelector('.el-dialog__footer button:last-child')
      const action = button.getBoundingClientRect()
      return {
        outsidePanel: !node.closest('.followup-center'),
        overlayFillsViewport: Math.abs(overlay.top) < 1 && Math.abs(overlay.left) < 1
          && Math.abs(overlay.width - innerWidth) < 1 && Math.abs(overlay.height - innerHeight) < 1,
        centered: Math.abs((bounds.left + bounds.right) / 2 - innerWidth / 2) < 2
          && Math.abs((bounds.top + bounds.bottom) / 2 - innerHeight / 2) < 2,
        fits: bounds.top >= 11 && bounds.bottom <= innerHeight - 11 && bounds.left >= 11 && bounds.right <= innerWidth - 11,
        footerVisible: footer.top >= bounds.top && footer.bottom <= bounds.bottom
          && button.contains(document.elementFromPoint((action.left + action.right) / 2, (action.top + action.bottom) / 2)),
      }
    })
    assert.ok(Object.values(fit).every(Boolean), `弹窗必须独立居中且底部按钮完整可见：${JSON.stringify(fit)}`)
  }
  const send = async items => {
    scanSignals = items
    sockets.at(-1).send(JSON.stringify({ type: 'snapshot', signals: items, requested: items.length, failed: 0, trigger: 'bar_closed', server_sent_at: new Date().toISOString() }))
    await page.waitForTimeout(120)
  }
  try {
    await page.goto('http://futures-regression.local/futures-app/')
    await page.waitForSelector('.signal-table tbody tr')
    assert.equal(await page.getByRole('button', { name: '1分钟', exact: true }).count(), 1, '全品种预警必须提供1分钟周期')
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
    const codeInput = dialog.getByPlaceholder('例如 pv、fg 或 RB2609')
    const exchangeSelect = dialog.locator('.el-select').first()
    for (const code of ['SH270', 'SH2700', 'SH2713', 'RB701', 'P701']) {
      await codeInput.fill(code)
      await page.waitForTimeout(50)
      assert.equal(await dialog.getByRole('button', { name: '验证并添加', exact: true }).isDisabled(), true, `${code}不能作为完整合约提交`)
      assert.ok((await dialog.locator('.recognition').first().innerText()).includes('月份为01至12'))
    }
    holdSuggestions = true
    const slowRequest = page.waitForRequest(request => request.url().includes('/suggestions?query=SH2701'))
    await codeInput.fill('SH2701')
    await slowRequest
    await dialog.getByRole('status').getByText('正在查询当前可用合约…', { exact: true }).waitFor()
    assert.equal(await dialog.getByRole('button', { name: '验证并添加', exact: true }).isDisabled(), false)
    assert.equal(await page.locator('.el-autocomplete__popper:visible').count(), 0, '慢查询不能用下拉加载框遮住交易所')
    await codeInput.fill('p2701')
    await page.waitForTimeout(50)
    assert.equal((await exchangeSelect.innerText()).trim(), '大商所 DCE')
    assert.ok(releaseSuggestions)
    holdSuggestions = false
    releaseSuggestions()
    await dialog.getByRole('status').getByText('暂无匹配候选；可输入完整合约代码后点击验证。', { exact: true }).waitFor()
    assert.equal(await page.getByRole('option', { name: '烧碱SH2701 CZCE' }).isVisible(), false, '迟到结果不能覆盖新输入')
    failSuggestions = true
    await codeInput.fill('FG701')
    await dialog.getByRole('status').getByText('模拟候选查询暂不可用，可输入完整合约后验证', { exact: true }).waitFor()
    assert.equal(await dialog.getByRole('button', { name: '验证并添加', exact: true }).isDisabled(), false)
    failSuggestions = false
    holdSuggestions = true
    const timeoutRequest = page.waitForRequest(request => request.url().includes('/suggestions?query=SH2701'))
    await codeInput.fill('SH2701')
    await timeoutRequest
    await dialog.getByRole('status').getByText('候选查询暂不可用；可输入完整合约代码后点击验证。', { exact: true }).waitFor({ timeout: 5000 })
    holdSuggestions = false
    releaseSuggestions()
    assert.equal(await dialog.getByRole('button', { name: '验证并添加', exact: true }).isDisabled(), false)
    results.push('拒绝无效月份和非郑商所三位年月；慢查询不遮挡表单，旧请求取消，查询失败可继续验证')
    for (const [code, exchange] of [
      ['SH2701', '郑商所 CZCE'], ['p2701', '大商所 DCE'], ['rb2701', '上期所 SHFE'],
      ['sc2701', '上期能源 INE'], ['if2612', '中金所 CFFEX'], ['lc2701', '广期所 GFEX'],
      ['pv2701', '大商所 DCE'],
      ['SH701', '郑商所 CZCE'],
    ]) {
      await codeInput.fill(code)
      await page.waitForTimeout(50)
      assert.equal((await exchangeSelect.innerText()).trim(), exchange, `输入${code}后交易所应同步更新`)
    }
    for (const code of ['UNKNOWN', '']) {
      await codeInput.fill(code)
      await page.waitForTimeout(50)
      assert.equal((await exchangeSelect.innerText()).trim(), '输入代码后自动选择', '未知或清空代码不能保留上一个交易所')
    }
    await codeInput.pressSequentially('sh')
    await page.getByRole('option', { name: '烧碱SH2701 CZCE' }).click()
    assert.equal(await codeInput.inputValue(), 'SH2701')
    assert.equal((await exchangeSelect.innerText()).trim(), '郑商所 CZCE')
    assert.ok((await dialog.locator('.recognition').first().innerText()).includes('数据代码 SH701'))
    await page.screenshot({ path: path.join(screenshotDir, 'pxyfutures-contract-exchange.png') })
    await codeInput.fill('CF2701')
    await dialog.getByRole('button', { name: '验证并添加', exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('.contract-row').length === 4)
    assert.equal((await codes()).at(-1), 'CF701')
    assert.equal(contractRequests.at(-1).code, 'CF2701', '完整年份必须保留到后端验证')
    await page.reload()
    await page.waitForSelector('.signal-table tbody tr')
    assert.equal((await codes()).at(-1), 'CF701')
    assert.equal(state[0].contracts.at(-1).exchange, 'CZCE')
    await page.getByRole('button', { name: '向该分组添加合约', exact: true }).click()
    await codeInput.fill('CF701')
    await dialog.getByRole('button', { name: '验证并添加', exact: true }).click()
    await page.getByText('该合约已添加', { exact: true }).waitFor()
    assert.equal(await page.locator('.contract-row').count(), 4)
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    results.push('自动选择六个交易所、候选选择、四位年月提交后保存标准合约、三位别名拒绝重复、刷新保留顺序')
    const first = signal()
    const nextBarTime = new Date(Date.parse(first.cross_time) + periods[0].duration_seconds * 1000).toISOString()
    const next = { ...first, bar_time: nextBarTime, confirmed_bar_time: nextBarTime, confirmed_at: new Date().toISOString(), state_bar_count: 2 }
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
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    const countTitle = await page.locator('.alert-new-badge').getAttribute('title')
    assert.ok(countTitle.includes('原始信号K线：') && countTitle.includes('已统计至收盘K线：'))
    assert.ok(countTitle.includes('含信号K线共2根，未收盘不计'))
    await page.reload()
    await page.waitForSelector('.signal-table tbody tr')
    await page.waitForFunction(() => document.querySelector('.alert-new-badge')?.textContent === '+2', null, { timeout: 7000 })
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+2'])
    results.push('原始信号+1提示，下一根收盘+2不重响，悬停可核对起止时间，刷新恢复+2')
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    await send([first])
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+2'])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    results.push('状态持续K线数不受重复消息影响，迟到消息不回退或重弹')
    const other = signal(2, fixture[0].contracts[1]), otherPeriod = signal(0, fixture[0].contracts[0], periods[1])
    await send([next, other, otherPeriod])
    assert.deepEqual((await page.locator('.alert-new-badge').allTextContents()).sort(), ['+1', '+1', '+2'])
    assert.deepEqual(await page.locator('.alert-period-heading > span').allTextContents(), ['10分钟', '5分钟'])
    assert.deepEqual(await page.getByRole('region', { name: '5分钟预警', exact: true }).locator('.alert-new-badge').allTextContents(), ['+1', '+2'])
    assert.equal((await page.locator('.alert-result-text').first().textContent()).includes('10分钟'), true)
    results.push('周期从大到小分组，组内+1排在+2之前')
    await closePopup()
    sockets.at(-1).close({ code: 1000 })
    await page.waitForTimeout(1500)
    await send([next, other, otherPeriod])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    assert.deepEqual((await page.locator('.alert-new-badge').allTextContents()).sort(), ['+1', '+1', '+2'])
    results.push('不同合约和周期独立排序，断线重连不重复预警')
    await send([{ ...signal(2), data_status: 'historical', data_source: 'historical' }])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    await send([{ ...signal(3), cross_time: null }])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    await send([signal(4, fixture[0].contracts[0], { ...periods[0], duration_seconds: 1800, label: '30分钟' })])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    await send([next, other, otherPeriod])
    assert.deepEqual((await page.locator('.alert-new-badge').allTextContents()).sort(), ['+1', '+1', '+2'])
    results.push('历史行情、无交叉与未选周期不触发或增加计数')
    const phaseTime = new Date(Date.parse(next.confirmed_bar_time) + periods[0].duration_seconds * 1000).toISOString()
    const phaseChanged = { ...next, label: '上涨', trend: { m3: '多', m4: '多' }, state_bar_count: 3, bar_time: phaseTime, confirmed_bar_time: phaseTime }
    await send([phaseChanged])
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+3'])
    assert.ok((await page.locator('.alert-result-text').textContent()).includes('上涨'))
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    await send([next])
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+3'])
    scanSignals = [phaseChanged]
    await page.reload()
    await page.waitForSelector('.alert-new-badge')
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+3'])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    await send([phaseChanged])
    assert.deepEqual(await page.locator('.alert-new-badge').allTextContents(), ['+3'])
    assert.equal(await page.locator('.market-alert-popup').count(), 0)
    results.push('方向和阶段变化继续+3，迟到快照不回退，恢复历史计数不重复提示')
    const alertPanel = page.getByRole('region', { name: '全品种预警', exact: true })
    await alertPanel.getByRole('button', { name: '5分钟', exact: true }).click()
    for (const label of ['30分钟', '15分钟', '3分钟']) {
      await alertPanel.getByRole('button', { name: label, exact: true }).click()
    }
    const groupPeriod = minutes => ({ ...periods[0], label: `${minutes}分钟`, duration_seconds: minutes * 60 })
    const groupSignal = (minutes, count, contractIndex, time) => ({
      ...signal(time, state[0].contracts[contractIndex], groupPeriod(minutes)),
      state_bar_count: count,
    })
    const groupedSignals = [
      groupSignal(3, 1, 0, 30), groupSignal(30, 10, 0, 20),
      groupSignal(15, 2, 1, 40), groupSignal(30, 2, 1, 19),
      groupSignal(30, 500, 2, 25), groupSignal(30, 1, 3, 18),
      groupSignal(3, 3, 1, 29),
    ]
    await send(groupedSignals)
    assert.deepEqual(await page.locator('.alert-period-heading > span').allTextContents(), ['30分钟', '15分钟', '10分钟', '3分钟'])
    const thirtyGroup = page.getByRole('region', { name: '30分钟预警', exact: true })
    assert.deepEqual(await thirtyGroup.locator('.alert-new-badge').allTextContents(), ['+1', '+2', '+10', '+500'])
    assert.equal(await page.getByRole('region', { name: '15分钟预警', exact: true }).locator('.alert-result-item').count(), 1)
    assert.equal(await page.getByRole('region', { name: '10分钟预警', exact: true }).locator('.alert-period-empty').textContent(), '暂无匹配预警')
    assert.deepEqual(await page.getByRole('region', { name: '3分钟预警', exact: true }).locator('.alert-new-badge').allTextContents(), ['+1', '+3'])
    const borders = await page.locator('.alert-period-heading').evaluateAll(headings => headings.map(heading => {
      const style = getComputedStyle(heading)
      return { width: parseFloat(style.borderTopWidth), color: style.borderTopColor }
    }))
    assert.ok(borders.every(border => border.width >= 2 && border.color === 'rgb(255, 138, 91)'), '每个选中周期应有彩色分隔线')
    await alertPanel.getByRole('button', { name: '多多死叉', exact: true }).click()
    assert.equal(await page.locator('.alert-result-item').count(), 0)
    assert.equal(await page.locator('.alert-period-empty').count(), 4)
    await alertPanel.getByRole('button', { name: '多多死叉', exact: true }).click()
    await alertPanel.getByRole('button', { name: '15分钟', exact: true }).click()
    assert.deepEqual(await page.locator('.alert-period-heading > span').allTextContents(), ['30分钟', '10分钟', '3分钟'])
    await alertPanel.getByRole('button', { name: '15分钟', exact: true }).click()
    await send([...groupedSignals].reverse())
    assert.deepEqual(await thirtyGroup.locator('.alert-new-badge').allTextContents(), ['+1', '+2', '+10', '+500'])
    await page.reload()
    await page.waitForSelector('.alert-new-badge')
    assert.deepEqual(await page.locator('.alert-period-heading > span').allTextContents(), ['30分钟', '15分钟', '10分钟', '3分钟'])
    assert.deepEqual(await thirtyGroup.locator('.alert-new-badge').allTextContents(), ['+1', '+2', '+10', '+500'])
    results.push('多周期分隔、数值排序含+500、空组、筛选、取消选择、乱序推送和刷新恢复')
    state[0].contracts.push({ ...fixture[0].contracts[0], id: 5, exchange: 'DCE', code: 'PVC2609', symbol: 'DCE.pvc2609', name: 'PVC2609', market_status: 'expired', sort_order: 4 })
    await page.reload()
    await page.waitForSelector('.contract-expiry-tag')
    await page.waitForSelector('.alert-new-badge')
    await page.setViewportSize({ width: 1440, height: 1100 })
    if (await page.locator('.market-alert-popup').count()) await closePopup()
    const firstSource = page.locator('.alert-result-link').first()
    assert.equal(await firstSource.evaluate(node => node.tagName), 'BUTTON')
    await firstSource.click()
    const secondaryDialog = page.getByRole('dialog', { name: '设置二级预警', exact: true })
    await secondaryDialog.waitFor()
    await assertViewportDialog(secondaryDialog)
    assert.equal(await secondaryDialog.locator('.alert-control-row').count(), 3)
    assert.equal(await secondaryDialog.getByRole('button', { name: '15分钟', exact: true }).isDisabled(), true)
    assert.equal(await secondaryDialog.getByRole('button', { name: '确定', exact: true }).isDisabled(), true)
    await secondaryDialog.getByRole('button', { name: '5分钟', exact: true }).click()
    await secondaryDialog.getByRole('button', { name: '空空死叉', exact: true }).click()
    await secondaryDialog.getByRole('button', { name: '下跌', exact: true }).click()
    await secondaryDialog.screenshot({ path: path.join(screenshotDir, 'pxyfutures-followup-settings.png') })
    failFollowupSave = true
    await secondaryDialog.getByRole('button', { name: '确定', exact: true }).click()
    await page.getByText('模拟二级条件保存失败', { exact: true }).waitFor()
    assert.equal(await secondaryDialog.isVisible(), true)
    failFollowupSave = false
    followupAlreadyMatches = true
    const soundsBeforeFollowup = await page.evaluate(() => window.__alertSoundPlays.length)
    await secondaryDialog.getByRole('button', { name: '确定', exact: true }).click()
    await secondaryDialog.waitFor({ state: 'hidden' })
    await page.locator('.followup-popup').waitFor()
    assert.deepEqual(followupRequests.at(-1).conditions, ['空空死叉'])
    assert.deepEqual(followupRequests.at(-1).states, ['下跌'])
    assert.equal(await page.locator('.followup-rule').count(), 1)
    assert.equal((await page.locator('.followup-time').textContent()).trim(), '2026-10-23 12:00')
    assert.equal(await page.evaluate(() => window.__alertSoundPlays.length), soundsBeforeFollowup + 5)
    assert.ok(await page.evaluate(() => window.__alertSoundPlays.slice(-5).every(src => /\/Sell-.*\.wav$/.test(src))))
    const popupPosition = await page.locator('.followup-popup').evaluate(node => {
      const popup = node.getBoundingClientRect(), panel = node.closest('.followup-center').getBoundingClientRect()
      return { absolute: getComputedStyle(node).position === 'absolute', within: popup.left >= panel.left && popup.right <= panel.right + 1, top: Math.round(popup.top - panel.top) }
    })
    assert.ok(popupPosition.absolute && popupPosition.within && popupPosition.top >= 10 && popupPosition.top <= 12)
    await page.locator('.followup-center').screenshot({ path: path.join(screenshotDir, 'pxyfutures-followup-trigger.png') })
    assert.equal(await page.locator('.followup-center .alert-new-badge').count(), 0)
    await page.getByRole('button', { name: '关闭二级预警提示', exact: true }).click()
    await page.waitForTimeout(3200)
    assert.equal(await page.locator('.followup-popup').count(), 0)
    assert.equal(await page.evaluate(() => window.__alertSoundPlays.length), soundsBeforeFollowup + 5)
    await page.reload()
    await page.waitForSelector('.followup-rule')
    assert.equal((await page.locator('.followup-time').textContent()).trim(), '2026-10-23 12:00')
    assert.equal(await page.locator('.followup-popup').count(), 0)
    assert.equal(await page.evaluate(() => window.__alertSoundPlays.length), 0)
    results.push('一级整行可点击、三栏设置、保存失败保留选择、已满足立即提醒、状态提示音、红框内右上角提示、刷新不重响')
    await page.locator('.alert-result-link').first().click()
    await secondaryDialog.waitFor()
    await secondaryDialog.getByRole('button', { name: '5分钟', exact: true }).click()
    await secondaryDialog.getByRole('button', { name: '空空死叉', exact: true }).click()
    await secondaryDialog.getByRole('button', { name: '下跌', exact: true }).click()
    rejectDuplicateFollowup = true
    const soundsBeforeDuplicate = await page.evaluate(() => window.__alertSoundPlays.length)
    await secondaryDialog.getByRole('button', { name: '确定', exact: true }).click()
    await page.getByText('已有完全相同的条件单，请修改固定条件或变换条件后再保存', { exact: true }).waitFor()
    assert.equal(await secondaryDialog.isVisible(), true)
    assert.equal(await secondaryDialog.getByRole('button', { name: '空空死叉', exact: true }).getAttribute('aria-pressed'), 'true')
    assert.equal(await page.locator('.followup-rule').count(), 1)
    assert.equal((await page.locator('.followup-time').textContent()).trim(), '2026-10-23 12:00')
    assert.equal(await page.evaluate(() => window.__alertSoundPlays.length), soundsBeforeDuplicate)
    await secondaryDialog.getByRole('button', { name: '取消', exact: true }).click()
    rejectDuplicateFollowup = false
    results.push('重复条件返回明确提示，保留设置及原条件单、触发时间，不新增行也不重复响铃')
    followupAlreadyMatches = false
    await page.locator('.alert-result-link').first().click()
    await secondaryDialog.waitFor()
    await secondaryDialog.getByRole('button', { name: '10分钟', exact: true }).click()
    await secondaryDialog.getByRole('button', { name: '反弹', exact: true }).click()
    await secondaryDialog.getByRole('button', { name: '确定', exact: true }).click()
    await secondaryDialog.waitFor({ state: 'hidden' })
    assert.equal(await page.locator('.followup-rule').count(), 2)
    assert.deepEqual(followupRequests.at(-1).conditions, [])
    assert.equal(await page.locator('.followup-time').last().textContent(), '')
    const twoConditions = await page.locator('.followup-target').allTextContents()
    assert.deepEqual(await page.locator('.followup-columns > span').allTextContents(), ['固定条件', '状态', '变换条件', '删除', '触发时间（北京时间）'])
    assert.equal(await page.locator('.followup-source-dot.normal').count(), 2)
    assert.ok((await page.locator('.followup-source').first().innerText()).startsWith('CZCE.'))
    assert.equal(await page.locator('.followup-rule').first().innerText().then(text => /一级条件|二级条件|\+\d/.test(text)), false)
    const sourceBefore = structuredClone(followupRules[0].source)
    const priorTrigger = await page.locator('.followup-time').first().textContent()
    for (const rule of followupRules) {
      rule.source_current = { ...rule.source, condition: '空空死叉', state: '下跌', confirmed_bar_time: '2026-10-23T12:05:00+08:00' }
      rule.source_changed = true
    }
    await page.waitForFunction(() => document.querySelectorAll('.followup-source-dot.changed').length === 2, null, { timeout: 6000 })
    assert.ok((await page.locator('.followup-source-dot').first().getAttribute('title')).includes('反弹 → 下跌'))
    followupRules[1].source_current = { ...followupRules[1].source, confirmed_bar_time: '2026-10-23T12:10:00+08:00' }
    followupRules[1].source_changed = false
    await page.waitForFunction(() => document.querySelectorAll('.followup-source-dot.normal').length === 1, null, { timeout: 6000 })
    followupRules[1].source_current = { ...followupRules[1].source, condition: '空空死叉', state: '下跌', confirmed_bar_time: '2026-10-23T12:15:00+08:00' }
    followupRules[1].source_changed = true
    await page.locator('.followup-source').first().click()
    const reviewDialog = page.getByRole('dialog', { name: '固定条件对比' })
    await reviewDialog.waitFor()
    await assertViewportDialog(reviewDialog)
    assert.ok((await reviewDialog.innerText()).includes('当前已收盘状态'))
    assert.ok((await reviewDialog.innerText()).includes('空空死叉'))
    await reviewDialog.screenshot({ path: path.join(screenshotDir, 'pxyfutures-fixed-condition-review.png') })
    failSourceReplace = true
    await reviewDialog.getByRole('button', { name: '更换', exact: true }).click()
    await reviewDialog.getByText('当前收盘状态已更新，请重新查看对比后更换', { exact: true }).waitFor()
    assert.deepEqual(followupRules[0].source, sourceBefore)
    failSourceReplace = false
    await reviewDialog.getByRole('button', { name: '更换', exact: true }).click()
    await reviewDialog.waitFor({ state: 'hidden' })
    assert.equal(followupRules[0].source.state, '下跌')
    assert.deepEqual(followupRules[1].source, sourceBefore)
    assert.equal(await page.locator('.followup-source-dot.changed').count(), 1)
    assert.equal(await page.locator('.followup-time').first().textContent(), priorTrigger)
    assert.deepEqual(await page.locator('.followup-target').allTextContents(), twoConditions)
    await page.locator('.followup-center').screenshot({ path: path.join(screenshotDir, 'pxyfutures-fixed-condition-table.png') })
    await page.reload()
    await page.waitForSelector('.followup-rule')
    assert.equal(await page.locator('.followup-source-dot.normal').count(), 1)
    assert.equal(await page.locator('.followup-source-dot.changed').count(), 1)
    results.push('固定条件竖列、红蓝圆点、状态恢复、对比框、更换失败保留、单行更换与刷新恢复，目标与时间不变')
    await alertPanel.getByRole('button', { name: '上涨', exact: true }).click()
    assert.deepEqual(await page.locator('.followup-target').allTextContents(), twoConditions)
    await alertPanel.getByRole('button', { name: '上涨', exact: true }).click()
    const followupPanel = page.locator('.followup-center')
    await followupPanel.getByRole('button', { name: '折叠', exact: true }).click()
    await page.locator('.followup-list').waitFor({ state: 'hidden' })
    assert.equal(await followupPanel.getByRole('button', { name: '展开', exact: true }).getAttribute('aria-expanded'), 'false')
    await page.reload()
    await page.waitForFunction(() => document.querySelectorAll('.followup-rule').length === 2)
    assert.equal(await page.locator('.followup-list').isVisible(), false)
    assert.ok(await followupPanel.evaluate(node => node.getBoundingClientRect().height < 80))
    const tonesWhileCollapsed = await page.evaluate(() => window.__alertSoundPlays.length)
    followupRules[0].last_triggered_at = '2026-10-23T13:30:00+08:00'
    followupRules[0].trigger_count += 1
    followupNotifications.push(structuredClone(followupRules[0]))
    await page.waitForFunction(() => document.querySelector('.followup-time')?.textContent.includes('13:30'), null, { timeout: 5000 })
    assert.equal(await page.locator('.followup-rule').count(), 2)
    assert.equal(await page.locator('.followup-popup').count(), 1)
    assert.equal(await page.locator('.followup-list').isVisible(), false)
    assert.equal(await page.evaluate(() => window.__alertSoundPlays.length), tonesWhileCollapsed + 5)
    assert.ok(await page.locator('.followup-popup').evaluate(node => node.scrollHeight <= node.clientHeight + 1))
    await page.getByRole('button', { name: '关闭二级预警提示', exact: true }).click()
    await followupPanel.screenshot({ path: path.join(screenshotDir, 'pxyfutures-followup-collapsed.png') })
    await followupPanel.getByRole('button', { name: '展开', exact: true }).click()
    await page.locator('.followup-list').waitFor({ state: 'visible' })
    await page.setViewportSize({ width: 1230, height: 1100 })
    await followupPanel.screenshot({ path: path.join(screenshotDir, 'pxyfutures-followup-fit-1230.png') })
    await page.reload()
    await page.waitForSelector('.followup-rule')
    assert.equal(await page.locator('.followup-list').isVisible(), true)
    await followupPanel.getByRole('button', { name: '二级条件监控', exact: true }).click()
    await page.locator('.followup-list').waitFor({ state: 'hidden' })
    await page.getByRole('button', { name: '恢复布局', exact: true }).click()
    await page.locator('.followup-list').waitFor({ state: 'visible' })
    results.push('二级面板折叠展开与刷新记忆、标题点击、恢复布局、折叠时继续更新及声音弹窗提醒')
    failFollowupDelete = true
    await page.getByRole('button', { name: '删除二级条件单', exact: true }).first().click()
    await page.getByText('模拟二级条件删除失败', { exact: true }).waitFor()
    assert.equal(await page.locator('.followup-rule').count(), 2)
    failFollowupDelete = false
    await page.getByRole('button', { name: '删除二级条件单', exact: true }).first().click()
    await page.waitForFunction(() => document.querySelectorAll('.followup-rule').length === 1)
    assert.ok((await page.locator('.followup-target').textContent()).includes('10分钟'))
    await page.reload()
    await page.waitForSelector('.followup-rule')
    assert.equal(await page.locator('.followup-rule').count(), 1)
    results.push('同品种多条独立条件、可只选状态、待触发时间留空、主筛选不影响条件单、再次触发更新原行、删除失败与独立删除持久化')
    followupRules[0].last_triggered_at = '2026-10-23T13:30:00+08:00'
    followupRules[0].trigger_count = 1
    await page.waitForFunction(() => document.querySelector('.followup-time')?.textContent.includes('13:30'), null, { timeout: 5000 })
    for (const width of [1440, 1230, 1024, 960, 721, 720, 390, 320]) {
      await page.setViewportSize({ width, height: 1100 })
      await page.waitForTimeout(100)
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `页面溢出：${width}`)
      const followupBounds = await page.locator('.followup-center').evaluate(node => {
        const bounds = node.getBoundingClientRect()
        return { right: bounds.right, width: bounds.width, rowSingleLine: getComputedStyle(node.querySelector('.followup-rule-text')).whiteSpace === 'nowrap' }
      })
      assert.ok(followupBounds.right <= width && followupBounds.width > 0 && followupBounds.rowSingleLine, `二级监控布局异常：${width}`)
      const followupFit = await page.locator('.followup-list').evaluate(node => ({
        noHorizontalScroll: node.scrollWidth <= node.clientWidth + 1,
        cellsFit: [...node.querySelectorAll('.followup-rule > *')].every(cell => {
          const row = cell.parentElement.getBoundingClientRect(), bounds = cell.getBoundingClientRect()
          return bounds.width > 0 && bounds.left >= row.left - 1 && bounds.right <= row.right + 1
        }),
        timeFits: [...node.querySelectorAll('.followup-time')].every(cell => cell.scrollWidth <= cell.clientWidth + 1),
      }))
      assert.ok(followupFit.noHorizontalScroll && followupFit.cellsFit, `二级面板仍需左右滚动或列被裁切：${width}`)
      if (width >= 721) assert.ok(followupFit.timeFits, `桌面触发时间未完整显示：${width}`)
      if (width === 1024) await followupPanel.screenshot({ path: path.join(screenshotDir, 'pxyfutures-followup-fit-1024.png') })
      const bounds = await page.locator('.contract-row').evaluateAll(rows => rows.map(row => ({
        row: row.getBoundingClientRect(),
        action: row.querySelector('.contract-actions').getBoundingClientRect(),
        exchange: row.querySelector('.contract-code small').getBoundingClientRect(),
        code: row.querySelector('.contract-code strong').getBoundingClientRect(),
        codeFits: row.querySelector('.contract-code strong').scrollWidth <= row.querySelector('.contract-code strong').clientWidth + 1,
      })).map(item => ({
        fits: item.action.right <= item.row.right + 1,
        codeWidth: item.code.width,
        codeFits: item.codeFits,
        singleLine: item.exchange.right <= item.code.left && item.exchange.top < item.code.bottom && item.code.top < item.exchange.bottom,
      })))
      assert.ok(bounds.every(item => item.fits && item.codeWidth > 30 && item.codeFits), `合约代码或按钮挤压：${width}`)
      assert.ok(bounds.every(item => item.singleLine), `交易所与合约代码未在同一行：${width}`)
      const alertLayout = await page.locator('.alert-period-group').evaluateAll(groups => groups.map(group => {
        const grid = group.querySelector('.alert-result-grid')
        const rows = [...group.querySelectorAll('.alert-result-item')]
        return {
          columns: grid ? getComputedStyle(grid).gridTemplateColumns.split(' ').length : null,
          singleLine: rows.every(row => getComputedStyle(row).whiteSpace === 'nowrap'),
          badgesFit: rows.every(row => row.querySelector('.alert-new-badge').getBoundingClientRect().right <= row.getBoundingClientRect().right + 1),
          endsBeforeNext: !group.nextElementSibling || group.getBoundingClientRect().bottom <= group.nextElementSibling.getBoundingClientRect().top + 1,
        }
      }))
      assert.ok(alertLayout.every(group => group.singleLine && group.badgesFit && group.endsBeforeNext), `周期组或计数布局异常：${width}`)
      assert.ok(alertLayout.every(group => group.columns === null || group.columns === (width <= 720 ? 1 : 2)), `周期组列数异常：${width}`)
      if ([1440, 390].includes(width)) await page.screenshot({ path: path.join(screenshotDir, `pxyfutures-sort-count-${width}.png`), fullPage: true })
    }
    assert.deepEqual(errors, [])
    results.push('八种桌面和手机宽度二级面板无左右滚动、桌面时间完整、单行分栏，合约代码与按钮可见、无页面异常')
    const savedRules = structuredClone(followupRules)
    followupRules[0].last_triggered_at = null
    followupRules[0].trigger_count = 1
    await page.reload()
    await page.waitForSelector('.followup-rule')
    assert.equal((await page.locator('.followup-time').first().textContent()).trim(), '', '无可靠触发时间时保持空白，不能显示状态说明代替时间')
    assert.equal(await page.getByText('已满足，起点待确认', { exact: true }).count(), 0)
    followupRules[0].last_triggered_at = '2026-10-22T22:40:00+08:00'
    await page.waitForFunction(() => document.querySelector('.followup-time')?.textContent === '2026-10-22 22:40', null, { timeout: 6000 })
    await page.reload()
    await page.waitForSelector('.followup-rule')
    assert.equal((await page.locator('.followup-time').first().textContent()).trim(), '2026-10-22 22:40')
    followupRules = structuredClone(savedRules)
    results.push('触发时间栏只展示有效时间，旧记录缺失时间时留空，时间恢复后正确显示且刷新保留')
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 720 })
      const heights = []
      for (const count of [0, 2, 7, 9]) {
        followupRules = Array.from({ length: count }, (_, index) => ({ ...structuredClone(savedRules[0]), id: index + 100 }))
        await page.reload()
        await page.waitForFunction(expected => document.querySelectorAll('.followup-rule').length === expected, count)
        await page.locator('.followup-list').waitFor({ state: 'visible' })
        const layout = await page.locator('.followup-list').evaluate(node => {
          const viewport = node.getBoundingClientRect()
          const header = node.querySelector('.followup-columns').getBoundingClientRect()
          const rows = [...node.querySelectorAll('.followup-rule')].map(row => row.getBoundingClientRect())
          return {
            height: viewport.height,
            fullyVisible: rows.filter(row => row.top >= header.bottom - 1 && row.bottom <= viewport.bottom + 1).length,
            scrolls: node.scrollHeight > node.clientHeight + 1,
            noHorizontalScroll: node.scrollWidth <= node.clientWidth + 1,
          }
        })
        heights.push(layout.height)
        assert.equal(layout.fullyVisible, Math.min(count, 7), `必须完整显示最多7条：${width}/${count}`)
        assert.equal(layout.scrolls, count > 7, `只有超过7条才滚动：${width}/${count}`)
        assert.ok(layout.noHorizontalScroll)
        if (count === 9) {
          await page.locator('.followup-list').evaluate(node => { node.scrollTop = node.scrollHeight })
          assert.ok(await page.locator('.followup-rule').last().evaluate(node => {
            const row = node.getBoundingClientRect(), viewport = node.parentElement.getBoundingClientRect()
            return row.bottom <= viewport.bottom + 1 && row.top >= viewport.top
          }), '最后一条必须能滚动到完整显示')
        }
      }
      assert.ok(heights[0] < heights[1] && heights[1] < heights[2] && Math.abs(heights[2] - heights[3]) < 1, '少于7条随内容收缩，超过7条高度不增加')
    }
    followupRules = savedRules
    await page.reload()
    await page.waitForSelector('.followup-rule')
    for (const viewport of [{ width: 1440, height: 600 }, { width: 390, height: 600 }]) {
      await page.setViewportSize(viewport)
      await page.locator('.alert-result-link').first().click()
      await assertViewportDialog(secondaryDialog)
      await page.screenshot({ path: path.join(screenshotDir, `pxyfutures-dialog-viewport-${viewport.width}.png`) })
      await secondaryDialog.getByRole('button', { name: '取消', exact: true }).click()
      await secondaryDialog.waitFor({ state: 'hidden' })
      await page.locator('.followup-source').first().click()
      await assertViewportDialog(reviewDialog)
      await page.screenshot({ path: path.join(screenshotDir, `pxyfutures-review-viewport-${viewport.width}.png`) })
      await reviewDialog.getByRole('button', { name: '关闭', exact: true }).click()
      await reviewDialog.waitFor({ state: 'hidden' })
    }
    results.push('二级列表0/2/7/9条按内容收缩、完整容纳7条及超出滚动；两类弹窗在桌面和手机短窗口独立居中且底部按钮可点击')
    await page.setViewportSize({ width: 1440, height: 1100 })
    for (const [index, label] of ['上涨', '回调', '多震荡', '下跌', '反弹', '空震荡'].entries()) {
      const time = new Date(Date.now() - 20000 + index * 1000).toISOString()
      const current = { ...signal(0, fixture[0].contracts[0], periods[1]), label, cross_time: time, confirmed_bar_time: time }
      const before = await page.evaluate(() => window.__alertSoundPlays.length)
      await send([current])
      const played = await page.evaluate(start => window.__alertSoundPlays.slice(start), before)
      assert.equal(played.length, 5, `${label}必须播放5遍`)
      const kind = index < 3 ? 'Buy' : 'Sell'
      assert.ok(played.every(src => new RegExp(`/${kind}-.*\\.wav$`).test(src)), `${label}声音不匹配`)
      await send([current])
      assert.equal(await page.evaluate(() => window.__alertSoundPlays.length), before + 5, '重复推送不能重响')
    }
    for (const label of ['上涨', '回调', '多震荡', '下跌', '反弹', '空震荡']) {
      const before = await page.evaluate(() => window.__alertSoundPlays.length)
      followupRules[0].last_triggered_state = label
      followupRules[0].trigger_count += 1
      followupNotifications.push(structuredClone(followupRules[0]))
      await page.waitForFunction(expected => window.__alertSoundPlays.length >= expected, before + 5, { timeout: 6000 })
      const played = await page.evaluate(start => window.__alertSoundPlays.slice(start), before)
      assert.equal(played.length, 5)
      const kind = ['上涨', '回调', '多震荡'].includes(label) ? 'Buy' : 'Sell'
      assert.ok(played.every(src => new RegExp(`/${kind}-.*\\.wav$`).test(src)), `二级实际${label}声音不匹配`)
      await page.getByRole('button', { name: '关闭二级预警提示', exact: true }).click()
    }
    const decoded = await page.evaluate(async () => {
      const context = new AudioContext()
      try {
        const urls = [...document.querySelectorAll('audio')].map(node => node.src)
        const assets = [...new Set(window.__alertSoundPlays)]
        return await Promise.all([...new Set([...urls, ...assets])].map(async url => {
          const response = await fetch(url)
          const buffer = await context.decodeAudioData(await response.arrayBuffer())
          return { duration: buffer.duration, audible: buffer.getChannelData(0).some(value => Math.abs(value) > .001) }
        }))
      } finally { await context.close() }
    })
    assert.equal(decoded.length, 2)
    assert.ok(decoded.every(item => item.duration > 0 && item.audible), '用户音频必须可解码且包含声音')
    results.push('一级与二级六种实际状态分别选择Buy或Sell且完整5遍，重复消息不重响，两份用户WAV可解码且非静音')
    await page.evaluate(() => {
      window.__nativeAudioEnds = []
      HTMLMediaElement.prototype.play = function () {
        if (!this.muted) this.addEventListener('ended', () => window.__nativeAudioEnds.push(new URL(this.src).pathname), { once: true })
        return window.__nativeMediaPlay.call(this)
      }
    })
    await page.locator('.followup-header').click()
    followupRules[0].last_triggered_state = '上涨'
    followupRules[0].trigger_count += 1
    followupNotifications.push(structuredClone(followupRules[0]))
    await page.waitForFunction(() => window.__nativeAudioEnds.length === 5, null, {
      timeout: Math.ceil(Math.max(...decoded.map(item => item.duration)) * 5000 + 12000),
    })
    assert.ok(await page.evaluate(() => window.__nativeAudioEnds.every(src => /\/Buy-.*\.wav$/.test(src))))
    results.push('恢复浏览器原生音频播放后，实际收到Buy音频完整结束事件5次')
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ passed: results.length, results }, null, 2))
  } finally { await context.close(); await browser.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
