# PXYFUTURES 开发规范

- 所有交流、注释和日志使用中文。
- 后端使用FastAPI、type hints和logging；前端使用Vue 3 Composition API与TypeScript严格模式。
- 均线算法、K线聚合和交叉状态修改必须有确定性测试。
- 数据库、天勤凭据、日志、虚拟环境、前端构建产物和客户EXE不得提交Git。
- 保持本地独立运行和主平台 `/futures-app/` 子路径运行同时可用。
- 每次代码修改后运行后端测试、ruff检查和前端生产构建，并单独提交Git。
- 不覆盖或回滚用户已有的未提交修改。

