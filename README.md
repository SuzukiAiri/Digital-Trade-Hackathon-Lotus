# RDTII Regulatory Intelligence Tool

This repository contains the Team Lotus release package for the UN Digital Trade Hackathon RDTII workflow. It supports official legal-source collection and regulatory mapping for Singapore, Australia, and Malaysia.

Core competition scope: Pillars 6 and 7.

Optional extension: Pillar 4 is included in the code and final submit files as an additional experiment. It does not replace the core P6/P7 submission.

## What It Does

Task 1 / Zone 1 builds a canonical corpus from official legal sources. It records document metadata, source URLs, law names, article or section references, and per-document provisions. PDF document-direct inputs are materialized through Docling when the optional PDF dependency is installed.

Task 2 / Zone 2 maps provisions to RDTII indicators. The production flow is:

```text
Zone 1 canonical corpus
-> Candidate Router
-> Mapper / PDF Mapper
-> Reviewer
-> Deterministic Resolver
-> Framework aggregation
-> Citation validation
-> Human decisions when required
-> GPT-5.6 final audit
-> final rows and submission export
```

Accepted rows use the official 13-column submission schema:

```text
Economy
Law Name
Law Number / Ref
Last Amended
Indicator ID
Article / Section
Discovery Tag
Location Reference
Verbatim Snippet
Mapping Rationale
Source URL
Confidence
Notes
```

## Supported Scope

Economies: Singapore, Australia, Malaysia.

Pillars:

- Core: `--pillars 6 7`
- Optional extension: `--pillars 4`

The CLI intentionally does not support combined `--pillars 4 6 7` runs.

## Installation

The lightweight install supports CLI help, the offline demo, structured-source code paths, and output verification.

Linux/macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . -c constraints-py311.txt
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e . -c constraints-py311.txt
```

Full PDF-capable install:

```bash
python -m pip install -e ".[pdf]"
```

The `pdf` extra installs `docling==2.112.0`. Docling is not vendored in this repository; no model cache, virtual environment, or converted PDF corpus is included.
Docling's current dependency graph may install PyTorch and CUDA-related wheels even on CPU-only Linux machines, depending on pip resolution and platform wheels. In Linux testing, the PDF-capable virtual environment was about 5.5 GB and the pip cache about 2.8 GB. Use a machine with generous free disk space, clear pip caches when appropriate, and prefer the lightweight install unless you need PDF materialization or the PDF demo.

If PDF processing is requested without the extra, install it with:

```bash
pip install -e ".[pdf]"
```

## OpenAI Configuration

Online mapping and live final audit use the OpenAI API. The offline demo and cache-only replay do not require an API key.

Set environment variables outside Git:

```bash
export OPENAI_API_KEY="your-openai-api-key"
export RDTII_FINAL_AUDIT_MODE=cache_only
```

Optional model overrides:

```bash
export RDTII_MAPPER_MODEL="gpt-5.4-nano"
export RDTII_REVIEW_MODEL="gpt-5.4-mini"
export RDTII_PDF_MAPPER_MODEL="gpt-5.4-mini"
```

Defaults in the code:

- Mapper: `gpt-5.4-nano`
- Reviewer: `gpt-5.4-mini`
- PDF Mapper: `gpt-5.4-mini`
- Final audit: GPT-5.6 global audit path when live mode is enabled

Do not commit `.env`, `API.txt`, API keys, model caches, or generated corpora.

## Quick Start

This Quick Start runs offline, uses small packaged fixtures, does not call OpenAI, does not call Docling, and writes to an isolated demo directory.

Linux/macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . -c constraints-py311.txt
python -m rdtii_tool --help
python -m rdtii_tool demo --mode offline --output-dir demo_output
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e . -c constraints-py311.txt
python -m rdtii_tool --help
python -m rdtii_tool demo --mode offline --output-dir demo_output
```

Optional PDF install check:

```bash
python -m rdtii_tool demo --mode pdf --output-dir demo_pdf_output
```

The PDF demo verifies that `docling==2.112.0` is importable and writes a tiny synthetic PDF fixture. It does not run OCR, call a model, or crawl the web.

## Reviewer Replay

The reviewer path restores frozen release assets and replays the core P6/P7 workflow without API calls. The assets contain the Zone 1 canonical corpus, source manifests and coverage reports, per-document provisions, Docling/PDF text artifacts, Mapper/Reviewer/PDF Mapper caches, human decisions, final-audit cache, and P6/P7 submission workspace files. P4 support files may be present in the assets because the protected final submit files include P4, but the normal reproducibility flow below is P6/P7.

Restore all three economy assets:

```bash
python scripts/bootstrap_release_assets.py --economies singapore australia malaysia
```

For an offline audit with already-downloaded assets:

```bash
python scripts/bootstrap_release_assets.py --economies singapore australia malaysia --asset-dir /path/to/release-assets
```

Run P6/P7 cache-only mapping replay:

```bash
python -m rdtii_tool map-rdtii --economy singapore --pillars 6 7 --cache-only
python -m rdtii_tool map-rdtii --economy australia --pillars 6 7 --cache-only
python -m rdtii_tool map-rdtii --economy malaysia --pillars 6 7 --cache-only
```

Technical repairs, citation repairs, old-cache incompatibilities, and framework mismatches are reported as warnings and repair-queue entries. They do not block output when the command completes the main processing and writes valid rows. In that case the CLI prints `completed_with_warnings` and exits 0. The command fails only for missing core inputs, unreadable files, program exceptions, or failure to produce any valid output.

Run final audit in cache-only mode:

```bash
RDTII_FINAL_AUDIT_MODE=cache_only python -m rdtii_tool final-audit --economy singapore --pillars 6 7
RDTII_FINAL_AUDIT_MODE=cache_only python -m rdtii_tool final-audit --economy australia --pillars 6 7
RDTII_FINAL_AUDIT_MODE=cache_only python -m rdtii_tool final-audit --economy malaysia --pillars 6 7
```

Export P6/P7 submission rows:

```bash
python -m rdtii_tool export-submission --economies singapore australia malaysia --pillars 6 7
```

Do not use `outputs/corpus/<economy>/final_submit/<economy>_p4_p6_p7.csv` or `.json` as final-audit input. Those files are the protected merged final submit result for inspection and hash comparison, not unreconciled P6/P7 rows to audit again.

## Live Runs

The live Zone 1 commands rebuild corpora from official sources and may be slow or affected by upstream rate limiting and temporary site blocks:

```bash
python -m rdtii_tool build-corpus --economy singapore --zone 1
python -m rdtii_tool build-corpus --economy australia --zone 1
python -m rdtii_tool build-corpus --economy malaysia --zone 1
```

Live Zone 2 uses the same commands without `--cache-only`. `OPENAI_API_KEY` is needed only when a Mapper, Reviewer, PDF Mapper, or live final-audit cache miss must be resolved:

```bash
python -m rdtii_tool map-rdtii --economy singapore --pillars 6 7
python -m rdtii_tool map-rdtii --economy australia --pillars 6 7
python -m rdtii_tool map-rdtii --economy malaysia --pillars 6 7
```

## Release Outputs

The protected final submit package is:

```text
outputs/corpus/singapore/final_submit/
outputs/corpus/australia/final_submit/
outputs/corpus/malaysia/final_submit/
```

Each country directory contains CSV and JSON files with matching row counts. The Git-tracked release intentionally excludes:

- `outputs/final_submission/`
- `outputs/corpus/*/submission/`
- full Zone 1 corpora
- raw, normalized, metadata, provisions, Docling artifacts, PDF text, mappings, staging, failed runs, backups, and caches

Those excluded working files are restored by `scripts/bootstrap_release_assets.py`. Release assets must not contain `final_submit` files, virtual environments, API keys, `.env`, local package caches, temporary staging directories, failed-run directories, or user-machine absolute paths.

## Discovery Tag

Discovery Tag is deterministic. It is computed against the supplied baseline inventory:

```text
Singapore, Malaysia, Australia, Legal Inventory.csv
```

`KNOWN` means the same economy, indicator, and canonical legal measure was matched to the baseline. `NEW` means the accepted measure was not present in that baseline. The exporter must fail closed if the baseline is missing; it must not silently mark everything as `NEW`.

## Citation Validation

Provision-level rows require an article or section, verbatim snippet, source URL, and mapping rationale. PDF/document-direct rows are validated against page-aware Docling artifacts when those artifacts are used. The final audit also checks citation fidelity, duplicate measures, metadata pollution, and conflicts between rationale and evidence.

## Human Review and Final Audit

Persistent human decisions have highest priority. The authority order is:

1. Persistent human decision
2. GPT-5.6 final audit decision
3. Reviewer and deterministic resolver decision
4. Raw Mapper or PDF Mapper claim

Human accepts are protected from lower-level changes but still must satisfy citation/export requirements. Human rejects remain excluded.

## Verification

Run a lightweight dependency check after installation:

```bash
python -m pip check
```

This check is offline and should not call OpenAI, Docling OCR, or network services.

## Runtime and Cost

Quick Start offline demo: usually under one minute after dependencies install.

Full Zone 1 + Zone 2 reproduction: may take hours for three economies, especially where PDF materialization is required.

OpenAI cost depends on cache state, number of candidate provisions, reviewer calls, PDF document-direct calls, and final audit mode. Cache-only final audit and the offline demo do not create API calls.

## Known Limitations

- The Git repository intentionally excludes large working corpora and caches; `scripts/bootstrap_release_assets.py` restores them from GitHub Release assets.
- P4 is optional extension scope; P6/P7 remain the core competition submission.
- Docling is installed through the `pdf` extra and may be heavy on CPU-only environments.
- Malaysia PDF quality depends on source PDF text extraction and Docling availability.
- The release does not include alternative LLM providers or alternative OCR engines.

## License and Team

Prepared for the UN Digital Trade Hackathon by Team Lotus.

Add the final repository license file before public reuse beyond the hackathon submission context.
