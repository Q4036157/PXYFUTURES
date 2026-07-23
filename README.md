# 貔貅元智能期货（PXYFUTURES）

PXYFUTURES 是一个开源的期货多周期均线看板，使用 FastAPI、Vue 3 和 TypeScript 构建。项目聚焦行情展示和信号分析，不包含交易、下单或持仓功能。

## 功能

- 展示期货合约的多周期 K 线与均线状态
- 按有效交易时间重建盘中周期，处理午休、日盘和夜盘边界
- 识别 M1/M2 金叉、死叉，并结合 M3/M4 位置进行信号分类
- 支持本地独立运行和主平台 `/futures-app/` 子路径部署
- 支持打包 Windows 单文件客户端

## 技术栈

- 后端：Python 3.11+、FastAPI、TqSdk
- 前端：Vue 3、TypeScript、Vite、Element Plus
- 测试与检查：pytest、Ruff、vue-tsc

## 快速开始

### 后端

```powershell
cd backend
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\uvicorn.exe app.main:app --reload --port 3022
```

### 前端

另开一个 PowerShell 窗口：

```powershell
cd frontend
Copy-Item .env.example .env.development
npm ci
npm run dev
```

浏览器打开 `http://127.0.0.1:3021`。

独立部署时无需设置 `JWT_SECRET`，首次进入页面时设置本地访问密码。集成主平台时，两个服务必须配置相同的 `JWT_SECRET`，前端将复用主平台的 `token`。

天勤账号可以填写在本地 `.env`，也可以通过应用设置页面保存。真实凭据只存放在本地，不得提交到 Git。

## 信号规则

- 使用最新 K 线（包括盘中进行中的 K 线）计算实时均线和信号，数值与行情软件盘中显示保持一致。
- 3 分钟至 4 小时的盘中周期按有效交易时间由基础分钟线重建：午休不计入周期，日盘与夜盘分开结算，收市前未满周期的 K 线仍参与盘中计算。日线及更长周期使用交易日 K 线。
- M3/M4 当前均线值高于上一根 K 线为“多”，否则为“空”。
- M1 使用博易短线公式 `(EMA(CLOSE,M1)+MA(CLOSE,M1*2)+MA(CLOSE,M1*4))/3`。
- M1 短线上穿 M2 为金叉，下穿为死叉；M2/M3/M4 使用各自周期的普通 MA。
- M1/M2 交点低于 `min(M3, M4)`：金叉为反弹，死叉为下跌。
- 交点位于两条长均线之间（包括边界）：M3 在上为多震荡，M4 在上为空震荡。
- 交点高于 `max(M3, M4)`：金叉为上涨，死叉为回调。
- 最近一次交叉的分类持续显示，直到反向交叉；服务重启时从历史 K 线恢复。

## 测试

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .

cd ..\frontend
npm run build
```

GitHub Actions 会在每次推送和 Pull Request 时自动执行这些检查。

## Windows 客户端

双击 `QIHUOBAT\打包客户版EXE.bat`，或在 PowerShell 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_windows.ps1
```

产物位于本地 `release` 目录。该目录不会提交到 Git；正式版本应通过 GitHub Releases 发布。

## 参与开发

欢迎通过 Issue 讨论功能和缺陷，并通过 Pull Request 提交改进。开始前请阅读 [贡献指南](CONTRIBUTING.md) 和 [安全政策](SECURITY.md)。

## 风险声明

本项目仅用于软件开发、行情展示和技术研究，不构成投资建议或交易信号承诺。期货交易具有高风险，使用者应独立判断并自行承担使用本软件及相关数据产生的风险。项目贡献者不对数据延迟、计算偏差、服务中断或任何交易损失负责。

## 许可证

本项目采用 [MIT License](LICENSE)。
