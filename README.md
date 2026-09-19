# pdf2tex-web

把 PDF 变成可编译的 LaTeX 项目和成品 PDF 的本地 Web 应用。

工作流来自 `pdf2tex` skill（MinerU OCR → 结构化修复 → Pandoc/XeLaTeX），
在两端各加了一层可选的大模型能力：

- **LLM 校对**：MinerU 产出的 Markdown 分块送给 OpenAI 兼容模型做 OCR 纠错
  （公式、表格、文字），结构校验不通过就回退原块。
- **LLM 编译修复**：XeLaTeX 报错时，把错误上下文和对应 TeX 片段交给模型生成
  `find/replace` 补丁，自动重编译，直到通过或达到轮次上限。

使用者只需要提供自己的 MinerU API token 和一个 OpenAI 兼容 API（DeepSeek、
OpenAI、Kimi、本地 vLLM 等）即可。

## 快速开始

```powershell
.\run.ps1
```

脚本会创建虚拟环境、安装依赖并启动服务，浏览器打开
<http://127.0.0.1:7860> 即可使用。

本机需要预先具备：

- Python 3.11+
- `pandoc`
- `xelatex`（TeX Live 或 MiKTeX，需带 `xeCJK` 与中文字体）

## 配置

界面右上角可填写并保存密钥，保存后写入项目根目录的 `.env`（已加入
`.gitignore`）。也可以直接复制 `.env.example` 为 `.env` 手工填写：

```dotenv
MINERU_API_TOKEN=
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
```

Token 获取：登录 <https://mineru.net/> 后在 API/Token 管理页面创建个人 token。

> `.env` 会以明文保存密钥，请勿提交到仓库或分享该文件。

## 输出

每个任务在 `data/jobs/<job_id>/` 下产出：

| 文件 | 说明 |
| --- | --- |
| `book/book.md` | MinerU 合并后的 Markdown |
| `book/book.compat.md` | 修复表格/公式后的 Markdown |
| `book/<name>.tex` | Pandoc 生成的 LaTeX 工程 |
| `book/<name>.pdf` | XeLaTeX 编译产物 |
| `run-summary.json` | 质量门字段、分段映射、耗时 |

## 目录结构

```text
app/                 FastAPI 后端与前端页面
vendor/pdf2tex/      vendored pdf2tex skill 脚本（不改动，便于跟随上游升级）
tools/mock_services.py  本地假 MinerU / 假 LLM 服务，用于离线自测
tests/               pytest 用例
docs/INTERFACES.md   模块与 HTTP 接口契约
```

## 测试

```powershell
python -m pytest -q
```

## 许可与致谢

转换流程与脚本来自 `pdf2tex` skill 包；OCR 由 MinerU 提供。

## 并行开发记录

接口契约在 [docs/INTERFACES.md](docs/INTERFACES.md)，流水线、LLM 模块和前端的子代理任务书保存在 [docs/subagent-briefs/](docs/subagent-briefs/)。
