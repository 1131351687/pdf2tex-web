---
name: pdf2tex
description: Convert math-heavy PDFs to Markdown and LaTeX with MinerU, audit scans, repair structural OCR defects, merge volumes, and validate XeLaTeX output.
---

# pdf2tex

## What this skill does

Use this skill to turn a book, paper, or exam PDF into an auditable Markdown and
LaTeX project. The workflow covers:

- source inspection and scan-page auditing;
- MinerU OCR to Markdown, structured JSON, images, and native TeX;
- deterministic repair of common OCR/LaTeX failures;
- optional volume/chapter merging;
- XeLaTeX validation and quality-gate reporting.

MinerU V4 with `model_version=vlm` is the preferred OCR path. The Agent endpoint
is a fallback for small inputs only: each Agent input must be at most 20 physical
pages. Split larger PDFs locally first and keep the subset-to-source page manifest.

## Security and prerequisites

- Read the MinerU token only from `MINERU_API_TOKEN`. Never place a token value in
  `SKILL.md`, a script, a config file, a report, or a task prompt.
- MinerU requires a valid `MINERU_API_TOKEN`. If it is missing, stop before OCR
  and ask the user to provide the token for the current run.
- When the user does not know where to get a token, guide them to visit
  [https://mineru.net/](https://mineru.net/), sign in, open the API/token
  management page, create or copy their personal API token, and then provide it
  only as the current-session `MINERU_API_TOKEN` value. Do not ask the user to
  paste it into documentation, source files, reports, or chat as a file artifact;
  if it was pasted into chat, do not write it into any file or log.
- OCR language comes from `--language` or `MINERU_LANGUAGE` and defaults to `ch`.
  The language and source PDF hash are recorded in `state.json`; resume with the
  same PDF and language.
- Keep the original PDF, physical page range, OCR output directory, and generated
  reports together as run metadata.
- Use XeLaTeX for MinerU's generated TeX.
- Expected Python packages: `requests`, `pypdf` or `PyPDF2`, `PyMuPDF` (`fitz`),
  and `numpy`.
- Expected external tools: `xelatex`; optional `pandoc` and `pdftotext` for the
  Markdown-to-PDF and heuristic comparison paths.
- Never reconstruct a missing source page from another edition or translation and
  present it as the source text. Mark the gap and preserve the verified boundary.

## End-to-end flow

### Relationship to the official MinerU skill

This `pdf2tex` skill is for full-book conversion into Markdown and LaTeX with
V4 OCR, structural repair, merging, and XeLaTeX validation. The official
`mineru` skill is preferred for ordinary document reading, inspection, search,
citation, and supported PDF/image/Office parsing.

If the official global `mineru` skill is absent, install or update it with:

```bash
npx skills add opendatalab/MinerU --skill mineru --global --yes
```

If `npx` is unavailable, fetch
[https://gcore.jsdelivr.net/gh/opendatalab/MinerU/skills/mineru/SKILL.md](https://gcore.jsdelivr.net/gh/opendatalab/MinerU/skills/mineru/SKILL.md)
and save it as `mineru/SKILL.md` in the agent's global skills directory.

Do not treat the two skills as competing for the same task: use `mineru` for
reading and document QA, and use `pdf2tex` when the user wants a converted,
auditable Markdown/LaTeX/PDF deliverable.

### One-command run

For most scanned books, prefer the orchestration script:

```powershell
python scripts/pdf2tex_run.py '<input.pdf>' '<output-dir>' --language ch
```

It audits the scan, splits the PDF into API-safe chunks, runs MinerU V4 for
each chunk, splits and retries failed chunks automatically, keeps source-page
manifests and `page-map.json`, merges Markdown, repairs math tags and tables,
generates Pandoc TeX, validates with XeLaTeX, and writes `run-summary.json`.
Use `--plan-only` to inspect the plan before spending OCR quota and `--no-pdf`
for Markdown-only output.

The script does not require a local MinerU installation; it uses the MinerU
remote V4 API. It still requires `MINERU_API_TOKEN`, Python dependencies,
Pandoc, and XeLaTeX.

1. Inspect the source and audit page integrity.
   - Confirm the PDF is text-based or image-only.
   - For scans, run `audit_scan_pages.py` around suspicious regions.
   - Resolve physical-page versus printed-page mapping before repairing text.
2. Run OCR.
   - Prefer `mineru_pipeline.py v4` and keep its output directory.
   - Use `mineru_pipeline.py agent` only for inputs of at most 20 pages.
   - Preserve `state.json`, `result/full.md`, images, structured JSON, and
     `result/full.fixed.tex` when generated.
3. Choose one output track.
   - Native MinerU TeX: repair and compile `full.fixed.tex` directly with XeLaTeX.
   - Markdown to pandoc: keep content repairs in Markdown or its header include;
     pandoc regenerates the LaTeX preamble, so native preamble edits are not
     enough there.
4. Apply structural repairs.
   - Run `repair_latex.py` on the relevant Markdown or TeX when tags or arrays
     are malformed.
   - Run `fix_html_tables.py` when MinerU emits raw HTML tables with
     `colspan`/`rowspan`.
   - Use `splice_markdown_section.py` only for a uniquely bounded section, and
     inspect its dry-run before writing.
5. Merge only after the source blocks pass inspection.
   - Use `normalize_book_md.py` for heading normalization.
   - Use `merge_volumes.py` for Markdown volumes.
   - Use `merge_tex.py` for native MinerU TeX chapters.
6. Validate and gate.
   - Run `validate_latex.py`, inspect its JSON fields, and visually compare
     formula-dense and table-dense pages with the source.
   - Treat `benchmark_ocr.py` as a heuristic, never as mathematical ground truth.
7. Record the result.
   - Keep the audit report, repair reports, compile report, source mapping,
     backup paths, and unresolved human-review items.

## Output tracks

### Native MinerU TeX

Input: `result/full.tex` and its images.

`mineru_pipeline.py v4` conservatively creates `result/full.fixed.tex` by applying
`finalize_mineru_tex.py`, which also invokes the structural repair from
`repair_latex.py` when both scripts are present. The native TeX carries its own
preamble and goes directly to XeLaTeX.

```powershell
$env:MINERU_API_TOKEN = '<token>'
python scripts/mineru_pipeline.py v4 '<input.pdf>' '<output-dir>' --language ch
python scripts/fix_html_tables.py '<output-dir>/result/full.md' `
  --patch-tex '<output-dir>/result/full.fixed.tex'
python scripts/validate_latex.py `
  '<output-dir>/result/full.fixed.tex' '<output-dir>/validation'
```

`fix_html_tables.py --patch-tex` pairs tables by ordinal. It aborts when the
Markdown and TeX table counts differ. Use `--force` only after checking the
mapping by hand.

### Markdown to pandoc

Input: Markdown plus an explicit pandoc header or template.

Apply content and table fixes to the Markdown because pandoc regenerates the
preamble. A fix such as adding a package through `finalize_mineru_tex.py` does not
reach this output.

```powershell
python scripts/repair_latex.py '<book.md>' '<book.fixed.md>' --report repair.json
python scripts/fix_html_tables.py '<book.md>' --patch-md '<book.compat.md>'
pandoc '<book.compat.md>' --pdf-engine=xelatex -H '<header.tex>' -o '<book.pdf>'
```

## Script map

- `mineru_pipeline.py`: resumable MinerU Agent/V4 client. Uses atomic
  `state.json`, verifies the input hash and OCR language, disables inherited
  proxies, performs safe ZIP extraction, downloads through `.part` files, and
  asks V4 for Markdown, structured JSON, images, and LaTeX. V4 creates
  `result/full.fixed.tex` when `result/full.tex` exists.
- `pdf2tex_run.py`: end-to-end orchestrator with chunking, automatic
  split-and-retry, page mapping, Markdown QA, TeX validation, and run summaries.
- `extract_pdf_pages.py`: extracts 1-based physical pages into a smaller PDF and
  optionally writes a subset-page-to-source-page JSON manifest. Use it to create
  Agent inputs of at most 20 pages.
- `audit_scan_pages.py`: detects whole-page near-duplicates, repeated
  header/footer page-number fingerprints, suspicious pages, and likely missing
  pages. It can derive a physical-to-printed mapping from explicit anchors, but
  marks extrapolated values as derived.
- `normalize_book_md.py`: removes pre-chapter matter and normalizes chapter,
  section, and other heading levels for pandoc bookmarks.
- `merge_volumes.py`: merges OCR Markdown blocks into configured volume files
  using a JSON config and copies referenced images into one `images/` directory.
- `merge_tex.py`: merges native MinerU TeX chapters into volume TeX files using
  a JSON config. Each output volume declares inclusive `chapter_ranges`, and the
  ranges must not overlap. See `examples/merge_tex.example.json`.
- `finalize_mineru_tex.py`: conservative native-TeX repairs only, including
  `arydshln` for `\hdashline`, a Cyrillic-capable main font when needed, safe
  trimming of over-wide all-dot array rows, and delegation to `repair_latex.py`.
- `repair_latex.py`: removes multiple `\tag` commands inside one equation and
  widens simple over-wide `array`/`tabular` column specifications. It never
  renumbers equations.
- `fix_html_tables.py`: expands `colspan`/`rowspan` into an absolute-column grid,
  rebuilds aligned `longtable` or Markdown pipe tables, performs math-aware LaTeX
  escaping, reports bare backslashes outside math, and aborts on table-count
  mismatch during TeX patching.
- `validate_latex.py`: compiles a staged copy with XeLaTeX and writes
  `compile-report.json`. It reports `returncode`, timeout state, errors, missing
  glyphs and fonts, and preview PDF page count.
- `splice_markdown_section.py`: replaces one uniquely anchored Markdown region.
  It requires exactly one start and one end match, supports dry-run, and creates a
  timestamped backup when writing over the source.
- `benchmark_ocr.py`: compares Markdown outputs with `pdftotext`-derived
  heuristics such as word recall, math spans, mojibake, and TeX artifacts. This is
  a smoke test, not ground truth for mathematics.

## Technical guardrails

- Physical PDF pages are 1-based. Never confuse them with printed book page
  numbers.
- The Agent endpoint accepts at most 20 pages per input. A subset PDF must retain
  its manifest so OCR pages can be mapped back to the source.
- Resume only against the same PDF hash and language recorded in `state.json`.
- A PDF generated after TeX errors is a preview, not an accepted deliverable.
- For the native TeX track, keep the MinerU preamble and compile
  `full.fixed.tex` with XeLaTeX. For the pandoc track, put preamble fixes in the
  Markdown header or pandoc template.
- Multiple `\tag` commands in one equation are invalid. Repeated equation
  numbers across equations are legal and often intentional. Do not globally
  renumber equations because in-text references depend on the displayed numbers.
- `array` and `tabular` rows must not contain more cells than their column
  specification. Repair only when the transformation is lossless; otherwise
  stop for review.
- HTML table patching is ordinal. Do not force a pairing unless the table order
  and count have been verified manually.
- A non-unique Markdown anchor is an error. Do not replace it with a broad
  search-and-replace operation.
- Keep image references relative to the Markdown or TeX file that uses them.
- `merge_tex.py` requires explicit, non-overlapping `chapter_ranges` in its JSON
  config. Do not patch hard-coded chapter ranges into the script.
- A heuristic `pdftotext` recall score cannot prove OCR correctness for formulas,
  tables, or missing pages.

## Quality gate

Do not call a conversion complete until all applicable checks pass:

1. Run `repair_latex.py` and require empty `tag_fixes` and `array_fixes` in its
   report after the repair pass.
2. Run `fix_html_tables.py <md>` in diagnostic mode and require no bare-backslash
   hazards outside math.
3. Run `validate_latex.py` and require:
   - `passed == true`;
   - `timed_out == false`;
   - `returncode == 0`;
   - `error_count == 0`.
4. For CJK or any expected non-Latin text, additionally require
   `missing_glyph_count == 0`. A zero compile return code alone does not detect
   silent glyph fallback and tofu boxes.
5. Check Markdown for unbalanced math delimiters, replacement characters, and
   severe OCR artifacts.
6. Spot-check formula-dense, table-dense, and page-boundary regions against
   rendered source pages.
7. For scans, record the physical-to-printed mapping, duplicate pages, and any
   missing-page decision in the audit report.

## Stop-for-human-review conditions

Stop and ask for review instead of guessing when any of these occurs:

- a source page is missing, duplicated, or has an unresolved physical-to-printed
  mapping;
- a Markdown start or end anchor is not unique;
- HTML table counts differ during TeX patching;
- a formula or table may be a merge of two source problems;
- a repair would delete mathematical data, renumber equations, or alter source
  meaning;
- XeLaTeX still fails, times out, reports errors, or reports missing CJK glyphs;
- the only proposed fill for a missing source page comes from another edition or
  translation;
- OCR quality is uncertain in a formula- or proof-critical passage.
