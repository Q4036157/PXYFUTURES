# 貔貅元智能期货

独立运行的期货多周期均线看板，不包含交易、下单或持仓功能。

## 已确认的信号规则

- 使用最新 K 线（包括盘中进行中的 K 线）计算实时均线和信号，数值与行情软件盘中显示保持一致。
- 3 分钟至 4 小时的盘中周期按有效交易时间由基础分钟线重建：午休不计入周期，日盘与夜盘分开结算，收市前未满周期的 K 线仍参与盘中计算；用于匹配博易云的周期分段方式。日线及更长周期使用交易日 K 线。
- M3/M4 当前均线值高于上一根 K 线为“多”，否则为“空”。
- M1 使用博易短线公式 `(EMA(CLOSE,M1)+MA(CLOSE,M1*2)+MA(CLOSE,M1*4))/3`。
- M1 短线上穿 M2 为金叉，下穿为死叉；M2/M3/M4 仍为各自周期的普通 MA。
- 交叉 K 线收盘价低于 `min(M3, M4)`：金叉为反弹、死叉为下跌。
- 收盘价位于两条长均线之间（包括边界）：震荡。
- 收盘价高于 `max(M3, M4)`：金叉为上涨、死叉为回调。
- 最近一次交叉的分类持续显示，直到反向交叉；服务重启时从历史 K 线恢复。

## 运行

后端：

```powershell
cd backend
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\uvicorn.exe app.main:app --reload --port 3022
```

前端：

```powershell
cd frontend
Copy-Item .env.example .env.development
npm install
npm run dev
```

独立部署不设置 `JWT_SECRET`，首次在本地页面设置访问密码。集成主平台时，两边配置相同的 `JWT_SECRET`，前端会自动复用主平台的 `token`。
