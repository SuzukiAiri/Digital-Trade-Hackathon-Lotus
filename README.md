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
python -m pip install -e .
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Full PDF-capable install:

```bash
python -m pip install -e ".[pdf]"
```

The `pdf` extra installs `docling==2.112.0` and its resolver may install a large PyTorch stack. Plan for several GB of environment and cache usage on a fresh machine, especially on CPU-only systems. Docling is not vendored in this repository; no model cache, virtual environment, or converted PDF corpus is included.

If PDF processing is requested without the extra, install it with:

```bash
pip install -e ".[pdf]"
```

## OpenAI Configuration

Online mapping and live final audit use the OpenAI API. Offline demo and release verification do not require an API key.

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
python -m pip install -e ".[pdf]"
python -m rdtii_tool --help
python -m rdtii_tool demo --mode offline --output-dir demo_output
python scripts/verify_release.py
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[pdf]"
python -m rdtii_tool --help
python -m rdtii_tool demo --mode offline --output-dir demo_output
python scripts/verify_release.py
```

Optional PDF install check:

```bash
python -m rdtii_tool demo --mode pdf --output-dir demo_pdf_output
```

The PDF demo verifies that `docling==2.112.0` is importable and writes a tiny synthetic PDF fixture. It does not run OCR, call a model, or crawl the web.

## Full Zone 1 Runs

Full Zone 1 may download or parse large official legal corpora. It is not a 10-minute demo.

```bash
python -m rdtii_tool build-corpus --economy singapore --zone 1
python -m rdtii_tool build-corpus --economy australia --zone 1
python -m rdtii_tool build-corpus --economy malaysia --zone 1
```

## Full Zone 2 Mapping

These commands may call OpenAI when cache misses exist and may use Docling for document-direct PDF inputs.

```bash
python -m rdtii_tool map-rdtii --economy singapore --pillars 6 7
python -m rdtii_tool map-rdtii --economy australia --pillars 6 7
python -m rdtii_tool map-rdtii --economy malaysia --pillars 6 7
```

Optional P4 extension:

```bash
python -m rdtii_tool map-rdtii --economy singapore --pillars 4
python -m rdtii_tool map-rdtii --economy australia --pillars 4
python -m rdtii_tool map-rdtii --economy malaysia --pillars 4
```

## Final Audit and Export

Final audit uses the same global batch audit flow for completed local rows. Cache-only mode avoids API calls.

These commands require a working `outputs/corpus/<economy>/submission/` directory produced by the full local pipeline. The release package intentionally includes only protected `final_submit` outputs, so a fresh release clone can verify the submitted files but cannot directly rerun `final-audit` or `export-submission` until the missing submission workspace has been regenerated.

```bash
RDTII_FINAL_AUDIT_MODE=cache_only python -m rdtii_tool final-audit --economy singapore --pillars 6 7
RDTII_FINAL_AUDIT_MODE=cache_only python -m rdtii_tool final-audit --economy australia --pillars 6 7
RDTII_FINAL_AUDIT_MODE=cache_only python -m rdtii_tool final-audit --economy malaysia --pillars 6 7
```

Offline export:

```bash
python -m rdtii_tool export-submission --economies singapore australia malaysia --pillars 6 7
```

`final-audit` fails closed when prefinal inputs are missing or empty, and will not create GPT audit requests for empty input. `export-submission` preflights every required `final_rows.jsonl` before creating final output directories.

## Release Outputs

The protected final submit package is:

```text
outputs/corpus/singapore/final_submit/
outputs/corpus/australia/final_submit/
outputs/corpus/malaysia/final_submit/
```

Each country directory contains CSV and JSON files with matching row counts. The release intentionally excludes:

- `outputs/final_submission/`
- `outputs/corpus/*/submission/`
- full Zone 1 corpora
- raw, normalized, metadata, provisions, Docling artifacts, PDF text, mappings, staging, failed runs, backups, and caches

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

## Tests

Run the release verification and unit tests:

```bash
python scripts/verify_release.py
python -m unittest discover -s tests
python -m pip check
```

The release tests are offline and should not call OpenAI, Docling OCR, or network services.

## Runtime and Cost

Quick Start offline demo: usually under one minute after dependencies install.

Full Zone 1 + Zone 2 reproduction: may take hours for three economies, especially where PDF materialization is required.

OpenAI cost depends on cache state, number of candidate provisions, reviewer calls, PDF document-direct calls, and final audit mode. Cache-only final audit and the offline demo do not create API calls.

## Known Limitations

- Full corpus and mapping runs are intentionally excluded from the release package.
- P4 is optional extension scope; P6/P7 remain the core competition submission.
- Docling is installed through the `pdf` extra and may be heavy on CPU-only environments.
- Malaysia PDF quality depends on source PDF text extraction and Docling availability.
- The release does not include alternative LLM providers or alternative OCR engines.

## Security

Before publishing, verify:

```bash
python scripts/verify_release.py
```

The verifier checks for API-key patterns, local absolute paths, oversized files, excluded output directories, final submit hashes, CSV/JSON parity, Discovery Tags, and duplicate canonical records.

## License and Team

Prepared for the UN Digital Trade Hackathon by Team Lotus.

Add the final repository license file before public reuse beyond the hackathon submission context.
