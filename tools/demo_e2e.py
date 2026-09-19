"""离线端到端自测：假 MinerU + 假 LLM + 真实 pandoc/XeLaTeX。

    python tools/demo_e2e.py

流程与真实运行完全一致，只是 MinerU 与 LLM 换成本地假服务，因此无需任何
API key 就能验证：分块 OCR → 合并 → 结构修复 → LLM 校对 → pandoc 生成 TeX
→ XeLaTeX 编译出 PDF 的完整链路。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.mock_services import start_servers  # noqa: E402


def make_demo_pdf(path: Path, pages: int = 3) -> Path:
    """用 PyMuPDF 生成一个多页 PDF（内容是占位文本，OCR 由假服务接管）。"""
    import fitz  # PyMuPDF

    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text(
            (72, 96),
            f"Demo source page {index + 1}",
            fontsize=18,
        )
        page.insert_text(
            (72, 132),
            "This page is a placeholder for the offline pdf2tex-web demo.",
            fontsize=11,
        )
    document.save(str(path))
    document.close()
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "data" / "demo-e2e")
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--no-clean", action="store_true", help="保留上一次的输出目录")
    args = parser.parse_args()

    missing = [tool for tool in ("pandoc", "xelatex") if shutil.which(tool) is None]
    if missing:
        print(f"缺少外部工具：{', '.join(missing)}，无法完成端到端演示。", file=sys.stderr)
        return 2

    out_dir: Path = args.out.resolve()
    if out_dir.exists() and not args.no_clean:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf = make_demo_pdf(out_dir / "input.pdf", args.pages)

    servers = start_servers()
    print(f"假 MinerU: {servers.mineru_url}")
    print(f"假 LLM:   {servers.llm_url}")

    os.environ["MINERU_API_TOKEN"] = "mock-token-for-offline-demo"
    os.environ["MINERU_BASE_URL"] = servers.mineru_url

    from app.contracts import JobOptions, LLMSettings
    from app.pipeline import run_pipeline

    job_dir = out_dir / "job"
    job_dir.mkdir(parents=True, exist_ok=True)

    stages: list[str] = []
    try:
        result = run_pipeline(
            job_dir=job_dir,
            pdf=pdf,
            options=JobOptions(language="ch", chunk_size=50, proofread=True),
            llm=LLMSettings(
                api_key="mock-key",
                base_url=servers.llm_url,
                model="mock-model",
            ),
            log=lambda line: print(f"  | {line}"),
            progress=lambda stage, label, fraction: _progress(stages, stage, label, fraction),
        )
    finally:
        servers.shutdown()

    print("\n=== 结果 ===")
    print(f"阶段顺序: {' -> '.join(stages)}")
    print(f"Markdown : {result.markdown}")
    print(f"TeX      : {result.tex}")
    print(f"PDF      : {result.pdf}")
    print(f"质量门   : {json.dumps(result.quality, ensure_ascii=False)}")
    print(f"需要复核 : {result.needs_review} {result.review_reasons}")

    if result.pdf is None or not Path(result.pdf).is_file():
        print("端到端演示失败：没有产出 PDF。", file=sys.stderr)
        return 1

    from pypdf import PdfReader

    pages = len(PdfReader(str(result.pdf)).pages)
    print(f"产出 PDF 页数: {pages}")
    if pages < 1:
        print("端到端演示失败：PDF 页数为 0。", file=sys.stderr)
        return 1
    print("端到端演示通过。")
    return 0


def _progress(stages: list[str], stage: str, label: str, fraction: float) -> None:
    if not stages or stages[-1] != stage:
        stages.append(stage)
        print(f"[{stage}] {label} {fraction:.0%}")
    elif fraction >= 1.0:
        print(f"[{stage}] {label} 完成")


if __name__ == "__main__":
    raise SystemExit(main())
