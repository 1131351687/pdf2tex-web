# 接口契约（冻结）

本文件是并行开发的唯一接口依据。各模块必须按此实现，不要改动本文件约定的
函数签名、数据字段和 HTTP 形状；如需扩展，只能新增可选字段。

## 模块划分与文件归属

| 文件 | 归属 | 职责 |
| --- | --- | --- |
| `app/contracts.py` | 已完成 | 数据类型与常量 |
| `app/settings.py` | 主程 | `.env` 读写、密钥掩码、默认值 |
| `app/jobs.py` | 主程 | 任务队列、状态机、SSE 事件总线、落盘 |
| `app/main.py` | 主程 | FastAPI 路由、静态页面托管 |
| `app/pipeline.py` | 子代理 A | 端到端转换编排 |
| `app/llm.py` | 子代理 B | OpenAI 兼容客户端 |
| `app/proofread.py` | 子代理 B | Markdown 分块与 LLM 校对 |
| `app/repair_loop.py` | 子代理 B | XeLaTeX 编译 + LLM 修复循环 |
| `app/static/index.html` | 子代理 C | 单页界面 |
| `app/static/app.js` | 子代理 C | 前端逻辑（原生 JS） |
| `app/static/style.css` | 子代理 C | 样式 |

子代理只允许修改自己名下的文件，以及自己名下的 `tests/test_<module>.py`。
不要修改 `vendor/pdf2tex/` 下的任何文件。

## Python 数据结构（`app/contracts.py`）

```python
LLMSettings(api_key: str, base_url: str, model: str, timeout: float = 120.0,
            max_retries: int = 2)

JobOptions(language="ch", chunk_size=50, min_chunk_pages=10, max_depth=4,
           retries=1, ocr_workers=1, proofread=False, proofread_max_chars=12000,
           proofread_max_chunks=800, llm_repair=True, repair_rounds=5,
           cjk_font="SimSun", main_font="DejaVu Sans",
           mono_font="DejaVu Sans Mono", math_font="Cambria Math",
           no_pdf=False)

JobRecord(id, filename, status, stage, stage_label, progress, message,
          created_at, updated_at, options: dict, outputs: dict,
          quality: dict, warnings: list[str], errors: list[str],
          proofread: dict | None, llm_repair: dict | None)
```

`status` 取值：`queued` | `running` | `done` | `needs_review` | `failed`。

`JobRecord.outputs` 的键：`markdown`、`compat_markdown`、`tex`、`pdf`、`log`、
`summary`，值为相对 `data/jobs/<job_id>/` 的路径字符串；不存在的产物不出现在字典里。

阶段键固定为：`prepare`、`ocr`、`merge`、`repair`、`proofread`、`pandoc`、
`validate`、`llm_repair`、`finalize`。

## 流水线（`app/pipeline.py`，子代理 A）

```python
class PipelineError(RuntimeError): ...

@dataclass
class PipelineResult:
    markdown: Path | None
    compat_markdown: Path | None
    tex: Path | None
    pdf: Path | None
    log_path: Path
    quality: dict
    warnings: list[str]
    needs_review: bool
    review_reasons: list[str]
    proofread: dict | None = None
    repair: dict | None = None

def run_pipeline(
    *,
    job_dir: Path,
    pdf: Path,
    options: JobOptions,
    llm: LLMSettings | None,
    log: Callable[[str], None],
    progress: Callable[[str, str, float], None],
) -> PipelineResult
```

要求：

- `progress(stage_key, stage_label_zh, fraction)`，`fraction ∈ [0, 1]` 是该阶段
  内部进度；`log(line)` 输出单行文本，不得包含密钥。
- MinerU OCR 复用 `vendor/pdf2tex/scripts/`：按 `options.chunk_size` 分块
  （`extract_pdf_pages.py`），逐块跑 `mineru_pipeline.py v4`，失败按 skill 规则
  拆半重试；子进程环境注入 `MINERU_API_TOKEN`，并支持 `MINERU_BASE_URL`
  覆盖（通过模块属性，不修改 vendor 文件）。
- Markdown 合并用 `merge_volumes.py`，结构修复用 `repair_latex.py` 与
  `fix_html_tables.py --patch-md`，校验用 `validate_markdown.py`。
- `options.proofread` 打开且 `llm` 不为 `None` 时，在修复后调用
  `app.proofread.proofread_markdown`。
- 用 `pandoc` 生成 `.tex`（`--standalone --pdf-engine=xelatex -H <header>`，
  header 由 `options` 的字体字段生成）。
- 编译与修复调用 `app.repair_loop.compile_with_repair`。
- 质量门按 `vendor/pdf2tex/SKILL.md`：`passed==True`、`timed_out==False`、
  `returncode==0`、`error_count==0`，CJK 时 `missing_glyph_count==0`。
  未通过时 `needs_review=True` 并把原因写入 `review_reasons`，而不是抛异常。
- 产物路径写回 `job_dir`：`book/`（Markdown/TeX/PDF）、`book/validation/`、
  `logs/`、`run-summary.json`。

## LLM（`app/llm.py`，子代理 B）

```python
class LLMError(RuntimeError): ...

class LLMClient:
    def __init__(self, settings: LLMSettings) -> None: ...
    def chat(self, messages: Sequence[Mapping[str, str]], *,
             temperature: float = 0.2,
             max_tokens: int | None = None) -> str: ...
    def chat_json(self, messages: Sequence[Mapping[str, str]], *,
                  temperature: float = 0.0) -> Any: ...

def build_client(settings: LLMSettings | None) -> LLMClient | None
```

- 基于 `openai` SDK，`base_url` 可指向任何 OpenAI 兼容服务。
- `chat_json` 需容忍 ```` ```json ```` 代码块与前后散文，只提取第一个 JSON 值；
  解析失败抛 `LLMError`。
- 网络/鉴权错误统一包装成 `LLMError`，消息中不得包含 api_key。

## 校对（`app/proofread.py`，子代理 B）

```python
@dataclass
class Chunk:
    index: int
    start: int
    end: int
    text: str

@dataclass
class ProofreadOutcome:
    markdown: str
    total_chunks: int
    applied: int
    skipped: int
    warnings: list[str]

def split_markdown(text: str, *, max_chars: int = 6000,
                   max_chunks: int = 200) -> list[Chunk]

def proofread_markdown(text: str, client: LLMClient, *,
                       max_chars: int = 6000, max_chunks: int = 200,
                       log: Callable[[str], None] | None = None,
                       progress: Callable[[float], None] | None = None,
                       ) -> ProofreadOutcome
```

- 分块优先在标题、空行等结构边界切分；单块不得超过 `max_chars`；块数超过
  `max_chunks` 时抛 `ValueError`（由流水线转成 `needs_review`）。
- 提示词要求：只修 OCR 错误（错别字、公式符号、表格错位），保持 Markdown
  结构、图片引用、数学定界符不变，只输出修订后的 Markdown。
- 每个块单独校验，任何一条不满足就丢弃该块结果、保留原文并计入 `skipped`：
  - 非空且长度在原文 0.5–1.5 倍之间；
  - `$$` 出现次数奇偶性与原文一致；
  - 图片引用集合（`![...](...)` 中的路径）与原文完全一致；
  - 不含 `U+FFFD`、不含 ```` ``` ```` 包裹整块的外壳。

## 编译与修复（`app/repair_loop.py`，子代理 B）

```python
@dataclass
class CompileOutcome:
    passed: bool
    rounds_used: int
    report: dict
    tex: Path
    log_path: Path
    warnings: list[str]

def compile_tex(tex: Path, work_dir: Path) -> tuple[dict, Path]

def compile_with_repair(tex: Path, work_dir: Path, client: LLMClient | None,
                        *, rounds: int = 5,
                        log: Callable[[str], None] | None = None,
                        progress: Callable[[float], None] | None = None,
                        ) -> CompileOutcome
```

- `compile_tex` 调用 `vendor/pdf2tex/scripts/validate_latex.py`，返回
  `(compile_report, log_path)`。
- `compile_with_repair` 在 `passed` 为假且有 `client` 时循环：从报告/日志定位
  错误行，截取该行附近 ≤120 行的 TeX 片段，要求模型返回
  `{"patches":[{"find": "...", "replace": "..."}]}`，仅当 `find` 在源文件中
  恰好出现一次才应用；应用失败或仍不通过则继续下一轮，直到 `rounds` 用尽。
- 每轮都要写 `log`，最后一轮结束返回 `CompileOutcome`（不抛异常）。
- 改写的 TeX 必须就地覆盖 `tex` 之前先备份到 `work_dir/backup-r<n>.tex`。

## HTTP 接口（`app/main.py`，主程）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 返回 `app/static/index.html` |
| GET | `/static/*` | 静态资源 |
| GET | `/api/health` | `{"ok": true, "tools": {"pandoc": bool, "xelatex": bool}, "python": "3.13.11"}` |
| GET | `/api/settings` | 密钥掩码 + 默认参数，见下 |
| PUT | `/api/settings` | 保存 `.env`，返回与 GET 相同结构 |
| GET | `/api/jobs` | `{"jobs": [JobRecord...]}`，按创建时间倒序 |
| POST | `/api/jobs` | multipart：`file` + 可选 `options`（JSON 字符串）→ JobRecord |
| GET | `/api/jobs/{id}` | JobRecord |
| POST | `/api/jobs/{id}/retry` | 复用已有 OCR 产物重跑后续阶段 → JobRecord |
| GET | `/api/jobs/{id}/events` | SSE，见下 |
| GET | `/api/jobs/{id}/download/{kind}` | `kind ∈ markdown|compat_markdown|tex|pdf|log|summary` |

`GET /api/settings` 响应：

```json
{
  "mineru_token": {"set": true, "mask": "******abcd"},
  "llm": {"api_key": {"set": true, "mask": "******wxyz"},
          "base_url": "https://api.deepseek.com",
          "model": "deepseek-chat"},
  "defaults": {"language": "ch", "proofread": false, "llm_repair": true,
               "repair_rounds": 5, "chunk_size": 50, "ocr_workers": 1,
               "cjk_font": "SimSun", "main_font": "DejaVu Sans",
               "mono_font": "DejaVu Sans Mono", "math_font": "Cambria Math"}
}
```

`PUT /api/settings` 请求体：

```json
{"env": {"MINERU_API_TOKEN": "...", "LLM_API_KEY": "...",
         "LLM_BASE_URL": "...", "LLM_MODEL": "..."},
 "defaults": {"language": "ch", "proofread": false, "llm_repair": true,
              "repair_rounds": 5, "chunk_size": 50, "ocr_workers": 1}}
```

字段缺省表示不修改，空字符串表示清除。响应不得回显完整密钥。

SSE 事件：每条 `data:` 是 JSON。

```json
{"type": "log", "line": "chunk pages-0001-0050: done", "level": "info"}
{"type": "status", "job": { ...JobRecord... }}
```

- 连接建立后先推一条 `status`，随后每次状态/阶段变化和每行日志都推。
- 任务进入终态（`done`/`needs_review`/`failed`）后补推一条 `status` 再结束流。
- 前端断线重连时用 `GET /api/jobs/{id}` 补齐最新状态。

## 前端（`app/static/*`，子代理 C）

- 原生 HTML/CSS/JS，无构建步骤、无外部 CDN 依赖。
- 三块区域：密钥设置、上传与选项、任务列表与实时日志。
- 通过 `fetch` 调 REST，通过 `EventSource` 订阅 SSE；终态后停止订阅。
- `POST /api/jobs` 用 `FormData`，`options` 字段是 `JSON.stringify` 后的字符串。
- 提供 md/tex/pdf/log 下载按钮，未产出的产物按钮禁用。
- 界面文案中文，错误信息展示后端返回的 `detail` 文本。
