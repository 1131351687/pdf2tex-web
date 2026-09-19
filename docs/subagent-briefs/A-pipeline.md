# 子代理 A 任务书：流水线编排

工作目录：`D:\try\pdf2tex-web`（git 仓库；不要执行任何 git 命令，提交由主程负责）。

先读：

- `docs/INTERFACES.md`（接口契约，冻结，不得修改）
- `app/contracts.py`（已实现的数据类型，不得修改）
- `vendor/pdf2tex/SKILL.md`（工作流与质量门）
- `vendor/pdf2tex/scripts/` 下的 `pdf2tex_run.py`、`mineru_pipeline.py`、`extract_pdf_pages.py`、`merge_volumes.py`、`repair_latex.py`、`fix_html_tables.py`、`validate_markdown.py`、`validate_latex.py`

只允许创建/修改：`app/pipeline.py`、`tests/test_pipeline.py`。

## 实现要求

1. 签名：`run_pipeline(*, job_dir: Path, pdf: Path, options: JobOptions, llm: LLMSettings | None, log, progress) -> PipelineResult`；`progress(stage_key, stage_label_zh, fraction)`，`log(line)`。可增加带默认值的可选关键字参数（便于测试注入），不得改动必填参数。
2. 阶段顺序：`prepare` → `ocr` → `merge` → `repair` → `proofread`（可选）→ `pandoc` → `validate` → `llm_repair`（可选）→ `finalize`。每阶段开始与结束都要回调 progress，fraction ∈ [0,1] 且单调不减。
3. OCR：用 `extract_pdf_pages.py` 按 `options.chunk_size` 分块（≤200 页），逐块跑 `mineru_pipeline.py v4`；子进程环境注入 `MINERU_API_TOKEN`；失败按 skill 规则拆半重试（`min_chunk_pages`/`max_depth`/`retries` 生效）；已成功块要复用（检查 `state.json` 的 `status`/`source_sha256`/`language`）以支持断点续跑。
4. MinerU BASE_URL 覆盖（离线自测必需）：不得修改 vendor 文件。用 `python -c` 包装器在子进程内 import `mineru_pipeline`，设置 `mineru_pipeline.BASE_URL = os.environ["MINERU_BASE_URL"]` 后再调用 `main()`；未设置该环境变量时行为与默认一致。
5. 合并/修复：`merge_volumes.py` 合并 Markdown；依次跑 `repair_latex.py`、`fix_html_tables.py --patch-md`、`validate_markdown.py`；出现替换字符或不平衡 `$$` 时记 warnings 并继续。
6. 校对：`options.proofread` 为真且 `llm` 不为 None 时调用 `app.proofread.proofread_markdown(...)`，汇总进 `PipelineResult.proofread`。校对抛 `ValueError`（超块数上限）时记 warning 并继续，不要让任务崩溃。
7. Pandoc：用 options 的字体字段生成 pandoc header（参考 `pdf2tex_run.py` 的 `build_header`），执行 `pandoc --standalone --pdf-engine=xelatex --resource-path <book_dir> -H <header> -o <name>.tex`。
8. 编译与修复：调用 `app.repair_loop.compile_with_repair(tex, work_dir, client, rounds=options.repair_rounds, log=..., progress=...)`；client 仅在 `options.llm_repair` 且 llm 可用时传入；把 `CompileOutcome` 汇总进 `PipelineResult.repair`。
9. 质量门（按 SKILL.md）：`passed == True`、`timed_out == False`、`returncode == 0`、`error_count == 0`，CJK 时 `missing_glyph_count == 0`。未通过 → `needs_review=True` + `review_reasons` 中文说明，不抛异常。
10. 产物布局：`job_dir/book/`（book.md、book.compat.md、`<name>.tex`、`<name>.pdf`、`validation/`）、`job_dir/logs/`、`job_dir/run-summary.json`、`job_dir/chunks/`。run-summary.json 记录阶段耗时、质量门字段、分块映射、警告，绝不含任何密钥。
11. 缺少 `MINERU_API_TOKEN` / `pandoc` / `xelatex` 时抛 `PipelineError`，消息为面向用户的中文提示。
12. `app/proofread.py` 与 `app/repair_loop.py` 由子代理 B 并行编写，按契约调用即可。

## 测试要求（tests/test_pipeline.py）

不得联网、不得调用真实 MinerU/LLM、不得依赖 pandoc/xelatex 存在。用 monkeypatch 或本地 fake 可执行文件替换子进程调用，覆盖：

- 正常完成路径；
- 某块首次失败后拆半重试成功；
- 质量门不通过进入 `needs_review`；
- 缺 token 抛 `PipelineError`；
- `run-summary.json` 不含密钥；
- 断言 progress 的 stage_key 顺序与 fraction 范围。

完成后运行 `.venv\Scripts\python.exe -m pytest tests/test_pipeline.py -q`（依赖已装好）并确保通过。最终回复简短：改动文件、关键实现决策、测试命令与结果、遗留风险。
