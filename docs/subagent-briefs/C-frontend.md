# 子代理 C 任务书：前端单页界面

工作目录：`D:\try\pdf2tex-web`（git 仓库；不要执行任何 git 命令，提交由主程负责）。

先读：

- `docs/INTERFACES.md`（接口契约，冻结，不得修改）：重点看 HTTP 接口表、settings 的 GET/PUT 形状、SSE 事件格式、前端小节
- `app/contracts.py`（`JobRecord` 字段、status 取值、`STAGES` 阶段键）
- `app/main.py`（真实路由实现，以它为准）

只允许创建/修改：`app/static/index.html`、`app/static/app.js`、`app/static/style.css`。

## 要求

1. 原生 HTML/CSS/JS，无构建步骤、无外部 CDN、无框架，中文界面。
2. 三个区块（工作台布局，不要营销式落地页）：
   - **密钥设置**：MinerU token、LLM api_key（密码输入框）、base_url、model 输入框，「保存到 .env」按钮；已保存时显示后端返回的掩码；提示「留空表示不修改」。
   - **新建任务**：PDF 文件选择（显示文件名与大小）、OCR 语言（默认 `ch`，可选 `en` 等常用值）、两个开关（LLM 校对、LLM 编译修复）、编译修复轮次数字输入、可折叠高级设置（chunk size、cjk/main/mono/math 字体、Markdown-only 选项）、提交按钮（未选文件或未配置 MinerU token 时禁用并给行内提示）。
   - **任务区**：当前任务卡片（状态徽章、阶段标签、进度条、消息）+ 实时日志窗口（等宽字体、自动滚动、可暂停）+ 下载按钮（md/tex/pdf/log）；历史任务列表（状态、时间、文件名，点击查看详情与下载）。
3. 数据流：
   - `GET /api/settings` 初始化表单；`PUT /api/settings` 保存（只提交用户填写的字段，留空的密钥不要覆盖）。
   - `POST /api/jobs` 用 FormData 提交：`file`、`options`（`JSON.stringify`）。提交成功后清空文件选择并订阅。
   - `GET /api/jobs` 列表；`GET /api/jobs/{id}` 详情；`GET /api/jobs/{id}/events` SSE（`data:` 是 JSON，`type` 为 `log` 或 `status`）；任务进入终态后关闭 EventSource 并刷新列表；断线时用详情接口补齐状态。
   - `POST /api/jobs/{id}/retry` 重试按钮（仅 `failed` / `needs_review` 显示）。
   - 下载按钮指向 `/api/jobs/{id}/download/<kind>`，`outputs` 里没有该键时禁用。
   - 页面加载时调 `GET /api/health`，`tools.pandoc` 或 `tools.xelatex` 为 false 时给出醒目提示。
4. 视觉与可访问性：浅色工作台风格；等宽字体显示日志；进度条与徽章用颜色区分 `queued/running/done/needs_review/failed`；输入控件有 `label` 或 `aria-label`；不要卡片套卡片；圆角 ≤ 8px；不要渐变装饰；窄屏（≥360px）文字不溢出。图标用内联 SVG 或纯文字，不引入外部图标库。
5. 错误处理：fetch 失败或后端返回 `{"detail": ...}` 时在页面上显示中文提示条，不要只 `console.log`。
6. `status` 事件里的 `job` 就是 JobRecord，`outputs` 的键为 `markdown|compat_markdown|tex|pdf|log|summary`。

## 自测

`node --check app/static/app.js` 通过；核对所有 fetch 路径与 `docs/INTERFACES.md`、`app/main.py` 一致。最终回复简短：改动文件、界面结构、校验命令与结果、遗留风险。
