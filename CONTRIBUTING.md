# 参与贡献

感谢你参与 PXYFUTURES。提交代码前，请先阅读本项目的开发约定。

## 开发流程

1. Fork 仓库并从 `main` 创建功能分支。
2. 一个分支只处理一个明确问题，避免混入无关改动。
3. 为均线算法、K 线聚合和交叉状态等行为变更补充确定性测试。
4. 在本地完成全部检查后提交 Pull Request（PR）。
5. 在 PR 中说明改动目的、验证方式和可能影响；界面改动请附截图。

建议使用清晰的分支名，例如 `feat/watchlist`、`fix/kline-boundary`。

## 本地检查

后端：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

前端：

```powershell
cd frontend
npm ci
npm run build
```

## 提交约定

提交信息建议采用以下前缀：

- `feat:` 新功能
- `fix:` 缺陷修复
- `test:` 测试变更
- `docs:` 文档变更
- `refactor:` 不改变外部行为的重构
- `build:` 构建或依赖变更
- `chore:` 其他维护工作

## 数据与凭据

不得提交天勤账号、密码、JWT 密钥、数据库、日志、虚拟环境、前端构建产物或客户端 EXE。只能提交不含真实凭据的 `.env.example`。

安全漏洞不要创建公开 Issue，请按 [SECURITY.md](SECURITY.md) 中的方式报告。
