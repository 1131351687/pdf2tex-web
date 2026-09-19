# pdf2tex skill 发布包

这是一个可独立复制、压缩和分发的 `pdf2tex` 技能包。它把数学密集型 PDF
转换为可审计的 Markdown、原生 LaTeX 和 XeLaTeX 预览，并提供扫描页审计、
结构修复、卷合并和质量门报告。

## 目录

```text
pdf2tex-skill-package/
├── SKILL.md                 # 给 Agent 读取的主技能说明
├── README.md                # 给使用者的安装和快速开始说明
├── requirements.txt         # Python 依赖
├── scripts/                 # 12 个正式技能脚本
└── examples/                # 合并配置和 pandoc 头文件示例
```

## 安装

1. 把包内容放入 Agent 的 skills 目录，并将顶层文件夹命名为 `pdf2tex`。例如
   将 `pdf2tex-skill-package/` 重命名为 `pdf2tex/` 后复制到
   `~/.codex/skills/`，或把内容直接放入已有的 `skills/pdf2tex/` 目录。不同
   环境的根目录可能不同，例如 `~/.dsh/skills/pdf2tex`。
2. 安装 Python 依赖：

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. 确认 `xelatex` 可用。Markdown 到 PDF 路线还需要 `pandoc`；
   `benchmark_ocr.py` 需要可选的 `pdftotext`。
4. MinerU token 只放在当前运行环境的 `MINERU_API_TOKEN` 中，不要写入任何文件。

## 快速开始

### 一键流程

对大型扫描书，优先使用端到端流程：

```powershell
python scripts/pdf2tex_run.py '<input.pdf>' '<output-dir>' --language ch
```

它会自动完成扫描审计、分段 OCR、失败拆半重试、页码映射、Markdown 修复、
TeX 生成、XeLaTeX 验证和运行摘要。`--plan-only` 只查看分段计划，
`--no-pdf` 只产出 Markdown。分段默认 50 页，最大不超过 MinerU V4 的 200 页。

本地不需要安装 MinerU CLI；该流程使用 MinerU V4 API。只需要
`MINERU_API_TOKEN`、Python 依赖、Pandoc 和 XeLaTeX。

### 与官方 MinerU Skill 的配合

本 skill 用于把整本扫描书转换成可审计的 Markdown/LaTeX/PDF。若只是阅读、检索、
引用、检查 PDF/图片/Office 文档，优先使用官方全局 `mineru` skill。

安装或更新官方 skill：

```powershell
npx skills add opendatalab/MinerU --skill mineru --global --yes
```

如果 `npx` 不可用，可从
[https://gcore.jsdelivr.net/gh/opendatalab/MinerU/skills/mineru/SKILL.md](https://gcore.jsdelivr.net/gh/opendatalab/MinerU/skills/mineru/SKILL.md)
下载内容，并保存为当前 Agent 的全局 skills 目录下的 `mineru/SKILL.md`。

两者分工如下：

- 官方 `mineru` skill：文档阅读、问答、检索、引用、通用解析。
- 本 `pdf2tex` skill：整本书转换、OCR 分段、公式修复、合并、XeLaTeX 校验。

Agent 在安装或更新前，应检查全局和项目级 skills 是否已有名字包含
`mineru` 的 skill；发现旧版或重名时，先报告并请求确认，不要直接删除。

### 0. 必需的 MinerU Token

运行 MinerU 前必须先设置 `MINERU_API_TOKEN`。如果这个环境变量不存在，Agent
应先向用户请求 token。不要把 token 写入仓库、配置文件、文档或报告中；只在
当前运行环境中使用。

如果没有 token，请按下面的步骤获取：

1. 打开 [MinerU 官网](https://mineru.net/)。
2. 登录账号，进入 API/Token 管理页面。
3. 创建或复制个人 API Token。
4. 把 token 设置到当前终端会话的 `MINERU_API_TOKEN` 环境变量中，不要写入
   仓库、文档、脚本、配置文件或报告。

给 AI/Agent 的指令：遇到缺少 token 的情况，先引导用户到
[https://mineru.net/](https://mineru.net/) 获取；如果用户把 token 粘贴到对话里，
只能临时写入当前运行环境，绝不能写入任何文件、日志或报告。

### 1. MinerU V4 OCR

```powershell
$env:MINERU_API_TOKEN = '<token>'
python scripts/mineru_pipeline.py v4 '<input.pdf>' '<output-dir>' --language ch
```

V4 输出目录通常包含 `state.json`、`result/full.md`、图片、结构化 JSON，
以及可用的 `result/full.fixed.tex`。

### 2. 原生 MinerU TeX 路线

```powershell
python scripts/fix_html_tables.py '<output-dir>/result/full.md' `
  --patch-tex '<output-dir>/result/full.fixed.tex'
python scripts/validate_latex.py `
  '<output-dir>/result/full.fixed.tex' '<output-dir>/validation'
```

### 3. Markdown 到 pandoc 路线

```powershell
python scripts/repair_latex.py '<book.md>' '<book.fixed.md>' --report repair.json
python scripts/fix_html_tables.py '<book.md>' --patch-md '<book.compat.md>'
pandoc '<book.compat.md>' --pdf-engine=xelatex -H 'examples/pandoc-header.tex' -o '<book.pdf>'
```

pandoc 会重新生成导言区，因此这条路线的内容修复必须落在 Markdown 或
pandoc 头文件中，不能只修 MinerU 原生 TeX 的导言区。

### 4. Agent 小文件路线

Agent 端点每个输入最多 20 个物理页。先用 `extract_pdf_pages.py` 抽取子 PDF，
并保留 JSON 映射：

```powershell
python scripts/extract_pdf_pages.py '<scan.pdf>' '<sub.pdf>' --pages '933-941' `
  --manifest '<sub.manifest.json>'
python scripts/mineru_pipeline.py agent '<sub.pdf>' '<output-dir>' --language ch
```

### 5. 分卷合并

复制并修改 `examples/` 中的配置，填入实际文件路径后可分别合并 Markdown
或原生 TeX：

```powershell
python scripts/merge_volumes.py '<merge-volumes.json>'
python scripts/merge_tex.py '<merge-tex.json>'
```

`merge_tex.py` 的每个卷都必须显式声明不重叠的 `chapter_ranges`；它不会
假定某一本书的章号分配。

## 质量门

完成后至少要确认：

- `repair_latex.py` 报告中的 `tag_fixes` 和 `array_fixes` 均为空；
- `fix_html_tables.py` 诊断模式没有数学模式外的裸反斜杠；
- `validate_latex.py` 的 `passed=true`、`timed_out=false`、`returncode=0`、
  `error_count=0`；
- CJK 文档还要求 `missing_glyph_count=0`，避免静默缺字；
- 公式、表格和跨页边界已与源 PDF 页面做过抽样核对；
- 扫描件的物理页、印刷页、重复页和缺页结论已写入审计报告。

## 重要边界

- 不根据其他版本或译本补写缺失的原始页面，应显式标注缺页。
- 不全局重编号公式；跨公式重复编号通常是按章编号的合法结果。
- `fix_html_tables.py --patch-tex` 按序号配对表格，张数不一致时必须先人工核对。
- `merge_tex.py` 的分卷范围由 JSON 的 `chapter_ranges` 显式给出，各卷范围不得
  重叠；示例见 `examples/merge_tex.example.json`。
- `benchmark_ocr.py` 只是启发式冒烟测试，不是数学内容的真值标准。
