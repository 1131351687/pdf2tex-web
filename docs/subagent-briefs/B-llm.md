# 子代理 B 任务书：LLM 客户端、校对与编译修复

工作目录：`D:\try\pdf2tex-web`（git 仓库；不要执行任何 git 命令，提交由主程负责）。

先读：

- `docs/INTERFACES.md`（接口契约，冻结，不得修改）
- `app/contracts.py`（已实现的数据类型，不得修改）
- `vendor/pdf2tex/SKILL.md`（质量门与修复边界）

只允许创建/修改：`app/llm.py`、`app/proofread.py`、`app/repair_loop.py`、`tests/test_llm_modules.py`。

## A. app/llm.py

- `LLMError`、`LLMClient(settings)`、`chat(messages, temperature=0.2, max_tokens=None) -> str`、`chat_json(messages, temperature=0.0) -> Any`、`build_client(settings) -> LLMClient | None`。
- 基于 openai SDK（`from openai import OpenAI`），`base_url` 指向任何 OpenAI 兼容服务（DeepSeek/Kimi/本地 vLLM）。
- `chat_json` 容忍 ```json 代码块与前后散文，只提取第一个完整 JSON 值；解析失败抛 `LLMError`。
- 所有网络/鉴权/超时异常包装成 `LLMError`，消息里绝不能出现 api_key。
- 重试：使用 `settings.max_retries`，指数退避；仅对超时/5xx/连接错误重试，4xx 鉴权错误不重试。
- `build_client` 在 settings 为 None 或 `settings.enabled` 为假时返回 None。

## B. app/proofread.py

- `Chunk`、`ProofreadOutcome`、`split_markdown(text, max_chars=6000, max_chunks=200)`、`proofread_markdown(text, client, max_chars=6000, max_chunks=200, log=None, progress=None)`。
- 分块优先在 Markdown 标题行、空行等结构边界切分；尽量不切碎 ``` 代码围栏与 `$$` 公式块；单块不得超过 `max_chars`（单个原子块本身更长时允许超长并记 warning）。
- 块数 > `max_chunks` 抛 `ValueError`（中文消息：文档太大或建议关闭校对）。
- 逐块调用 LLM，提示词（中文）要求：只修 OCR 错误（错别字、公式符号、表格错位），保留 Markdown 结构、图片引用、数学定界符，只输出修订后的 Markdown，不要解释。提示词里要把待校对 Markdown 放进 ```markdown 围栏。
- 每块独立校验，任一不满足就保留原文并计入 `skipped`：非空、长度在 0.5–1.5 倍之间、`$$` 奇偶性与原文一致、图片引用路径集合完全一致、不含 U+FFFD、不含把整块包起来的 ``` 外壳。
- `LLMError` 时保留该块原文、记 warning、继续后续块。
- `progress` 回调为 `Callable[[float], None]`，值 0..1。

## C. app/repair_loop.py

- `CompileOutcome`、`compile_tex(tex, work_dir) -> tuple[dict, Path]`、`compile_with_repair(tex, work_dir, client, rounds=5, log=None, progress=None) -> CompileOutcome`。
- `compile_tex` 用 `sys.executable` 调用 `vendor/pdf2tex/scripts/validate_latex.py <tex> <work_dir>`，读取 `compile-report.json` 返回；报告缺失或调用失败给出可读错误信息。
- `compile_with_repair`：`passed` 为真、或 `client` 为 None、或 `rounds <= 0` 时直接返回；否则循环：
  1. 从 `compile-report.json` 的 `errors` 与 `work_dir/<stem>.log` 提取错误行号与消息；
  2. 截取错误行附近 ≤120 行的 TeX 片段（含行号）；
  3. 要求模型返回严格 JSON `{"patches":[{"find": "...", "replace": "..."}], "reason": "..."}`；
  4. 只有 `find` 在文件中恰好出现一次才替换，否则跳过并记 warning；
  5. 应用前把当前 TeX 备份到 `work_dir/backup-r<n>.tex`；
  6. 重新编译并更新 report；通过则提前结束。
- 轮次用尽仍未通过时返回 outcome（`passed=False`、`rounds_used=n`、warnings 含每轮原因），绝不抛异常。
- 日志与 warning 中不得出现 api_key。

## 测试要求（tests/test_llm_modules.py）

不得联网。用 stub/fake 替换底层调用：

- `chat_json` 解析：纯 JSON、```json 代码块、带前后散文、非法 JSON 抛 `LLMError`；
- `chat` 错误包装且消息不含密钥；
- 分块器：按标题切分、超长原子块、块数超限抛 `ValueError`、切分后拼接能还原原文；
- 校对：正常替换、校验失败保留原块、图片引用改变时丢弃、`LLMError` 时保留原文并 warning；
- repair_loop：monkeypatch 编译函数模拟「失败 → 修复后通过」和「始终失败到轮次用尽」，断言备份文件生成、`find` 不唯一时跳过、`passed` 值正确。

完成后运行 `.venv\Scripts\python.exe -m pytest tests/test_llm_modules.py -q`（依赖已装好）并确保通过。最终回复简短：改动文件、关键实现决策、测试命令与结果、遗留风险。
