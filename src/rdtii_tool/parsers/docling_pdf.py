"""Docling-backed PDF normalization for document-direct Zone 1 inputs."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DOCLING_ARTIFACT_SCHEMA_VERSION = "rdtii-docling-pdf-artifact-v1"
MIN_DOCUMENT_CHARS = 80
MAX_REPLACEMENT_CHAR_RATIO = 0.02
MIN_NON_EMPTY_PAGE_RATIO = 0.2
DEFAULT_MAX_PAGES = 500
_CONVERTER_CACHE: dict[str, Any] = {}
_CONFIG_LOGGED = False
_GPU_ENV_CHECKED = False
_RUNTIME_STATS: dict[str, Any] = {
    "native_converter_initialized_count": 0,
    "ocr_converter_initialized_count": 0,
    "native_converter_initialization_time": 0.0,
    "ocr_converter_initialization_time": 0.0,
    "native_conversion_total_time": 0.0,
    "ocr_conversion_total_time": 0.0,
    "native_conversion_count": 0,
    "ocr_conversion_count": 0,
    "native_document_times": [],
    "ocr_document_times": [],
}


@dataclass(frozen=True)
class DoclingRuntimeProfile:
    mode: str
    name: str
    workers: int
    num_threads: int
    page_batch_size: int
    layout_batch_size: int
    ocr_batch_size: int
    table_batch_size: int
    queue_max_size: int
    device: str
    pipeline_class: str
    ocr_backend: str
    gpu_name: str = ""
    vram_gb: float = 0.0


def docling_mode() -> str:
    mode = os.environ.get("RDTII_DOCLING_MODE", "cpu").strip().casefold() or "cpu"
    if mode not in {"cpu", "gpu"}:
        raise DoclingPdfError("invalid_docling_mode")
    return mode


def get_docling_runtime_profile() -> DoclingRuntimeProfile:
    mode = docling_mode()
    if mode == "cpu":
        return DoclingRuntimeProfile(
            mode="cpu",
            name="cpu-stable",
            workers=1,
            num_threads=2,
            page_batch_size=4,
            layout_batch_size=4,
            ocr_batch_size=4,
            table_batch_size=4,
            queue_max_size=100,
            device="cpu",
            pipeline_class="StandardPdfPipeline",
            ocr_backend="onnxruntime",
        )
    gpu = _gpu_info_or_raise()
    vram = float(gpu["vram_gb"])
    if vram < 8:
        name, workers, threads, page, layout, ocr, queue = "gpu-<8gb", 1, 4, 16, 16, 8, 32
    elif vram < 12:
        name, workers, threads, page, layout, ocr, queue = "gpu-8-12gb", 1, 4, 16, 16, 8, 32
    elif vram < 24:
        name, workers, threads, page, layout, ocr, queue = "gpu-12-24gb", 1, 8, 64, 64, 32, 128
    else:
        name, workers, threads, page, layout, ocr, queue = "gpu-24gb-plus", 2, 6, 32, 32, 16, 64
    return DoclingRuntimeProfile(
        mode="gpu",
        name=name,
        workers=workers,
        num_threads=threads,
        page_batch_size=page,
        layout_batch_size=layout,
        ocr_batch_size=ocr,
        table_batch_size=4,
        queue_max_size=queue,
        device="cuda",
        pipeline_class="ThreadedStandardPdfPipeline",
        ocr_backend=_rapidocr_gpu_backend(),
        gpu_name=str(gpu["name"]),
        vram_gb=vram,
    )


def docling_worker_count() -> int:
    return get_docling_runtime_profile().workers


def describe_docling_runtime() -> dict[str, Any]:
    profile = get_docling_runtime_profile()
    return {
        "selected_mode": profile.mode,
        "profile": profile.name,
        "gpu_name": profile.gpu_name,
        "vram_gb": round(profile.vram_gb, 2),
        "docling_version": importlib.metadata.version("docling"),
        "pipeline_class": profile.pipeline_class,
        "page_batch_size": profile.page_batch_size,
        "layout_batch_size": profile.layout_batch_size,
        "ocr_batch_size": profile.ocr_batch_size,
        "table_batch_size": profile.table_batch_size,
        "queue_max_size": profile.queue_max_size,
        "docling_workers": profile.workers,
        "cpu_threads": profile.num_threads,
        "ocr_backend": profile.ocr_backend,
    }


class DoclingPdfError(RuntimeError):
    pass


def docling_max_pages() -> int:
    raw = os.environ.get("RDTII_DOCLING_MAX_PAGES", str(DEFAULT_MAX_PAGES)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise DoclingPdfError("invalid_docling_max_pages") from exc
    if value <= 0:
        raise DoclingPdfError("invalid_docling_max_pages")
    return value


def pdf_page_count(path: Path) -> int:
    try:
        import pypdfium2 as pdfium  # type: ignore
    except Exception as exc:
        raise DoclingPdfError(f"pdf_page_count_import_failed:{type(exc).__name__}") from exc
    try:
        pdf = pdfium.PdfDocument(str(path))
        try:
            return int(len(pdf))
        finally:
            try:
                pdf.close()
            except Exception:
                pass
    except Exception as exc:
        raise DoclingPdfError(f"pdf_page_count_failed:{type(exc).__name__}") from exc


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_hash(value: str) -> str:
    return hashlib.sha256(_normalise_text(value).encode("utf-8")).hexdigest()


def extract_docling_pdf_artifact(
    *,
    pdf_path: Path,
    artifact_path: Path,
    document_id: str,
    source_url: str = "",
    title: str = "",
    artifacts_path: str | Path | None = None,
    use_local_temp: bool = True,
) -> dict[str, Any]:
    """Create or reuse a single page-aware Docling artifact for a local PDF."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists() or not pdf_path.is_file():
        raise DoclingPdfError("pdf_source_missing")
    source_hash = sha256_path(pdf_path)
    if artifact_path.exists():
        try:
            cached = json.loads(artifact_path.read_text(encoding="utf-8"))
            if (
                cached.get("schema_version") == DOCLING_ARTIFACT_SCHEMA_VERSION
                and cached.get("source_content_hash") == source_hash
                and _artifact_quality_ok(cached)[0]
            ):
                cached["artifact_status"] = "reused"
                return cached
        except Exception:
            pass

    native_payload = _convert_with_docling(
        pdf_path=pdf_path,
        document_id=document_id,
        source_url=source_url,
        title=title,
        source_hash=source_hash,
        artifacts_path=artifacts_path,
        do_ocr=False,
        use_local_temp=use_local_temp,
    )
    payload = native_payload
    ok, reason = _artifact_quality_ok(payload)
    if not ok:
        payload = _convert_with_docling(
            pdf_path=pdf_path,
            document_id=document_id,
            source_url=source_url,
            title=title,
            source_hash=source_hash,
            artifacts_path=artifacts_path,
            do_ocr=True,
            use_local_temp=use_local_temp,
        )
        payload["native_character_count"] = int(native_payload.get("character_count") or 0)
        ok, reason = _artifact_quality_ok(payload)
        if not ok:
            raise DoclingPdfError(reason)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = artifact_path.with_suffix(artifact_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(artifact_path)
    payload["artifact_status"] = "created"
    return payload


def get_docling_runtime_stats() -> dict[str, Any]:
    stats = dict(_RUNTIME_STATS)
    native_times = list(stats.get("native_document_times") or [])
    ocr_times = list(stats.get("ocr_document_times") or [])
    stats["native_average_warm_time"] = sum(native_times[1:]) / len(native_times[1:]) if len(native_times) > 1 else 0.0
    stats["ocr_average_warm_time"] = sum(ocr_times[1:]) / len(ocr_times[1:]) if len(ocr_times) > 1 else 0.0
    return stats


def reset_docling_runtime_stats() -> None:
    for key, value in list(_RUNTIME_STATS.items()):
        if isinstance(value, list):
            value.clear()
        elif isinstance(value, int):
            _RUNTIME_STATS[key] = 0
        else:
            _RUNTIME_STATS[key] = 0.0


def load_docling_artifact(path: str | Path) -> dict[str, Any]:
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    ok, reason = _artifact_quality_ok(artifact)
    if not ok:
        raise DoclingPdfError(reason)
    return artifact


def page_text(artifact: dict[str, Any], page_number: int) -> str:
    for page in artifact.get("pages") or []:
        if int(page.get("page_number") or 0) == int(page_number):
            return str(page.get("text") or "")
    return ""


def document_text(artifact: dict[str, Any]) -> str:
    return "\n\n".join(str(page.get("text") or "").strip() for page in artifact.get("pages") or [] if str(page.get("text") or "").strip())


def quote_in_page(artifact: dict[str, Any], quote: str, page_number: int) -> bool:
    return _normalise_for_lookup(quote) in _normalise_for_lookup(page_text(artifact, page_number))


def _convert_with_docling(
    *,
    pdf_path: Path,
    document_id: str,
    source_url: str,
    title: str,
    source_hash: str,
    artifacts_path: str | Path | None,
    do_ocr: bool,
    use_local_temp: bool,
) -> dict[str, Any]:
    converter = _get_converter(do_ocr=do_ocr, artifacts_path=artifacts_path)
    start = time.perf_counter()
    source_path = _local_temp_pdf_copy(pdf_path) if use_local_temp else pdf_path
    try:
        try:
            result = converter.convert(source_path)
        except Exception as exc:
            if docling_mode() == "gpu" and _is_cuda_oom(exc):
                _empty_cuda_cache()
                raise DoclingPdfError("gpu_oom") from exc
            raise
    finally:
        if source_path != pdf_path:
            try:
                source_path.unlink()
            except FileNotFoundError:
                pass
    elapsed = time.perf_counter() - start
    if do_ocr:
        _RUNTIME_STATS["ocr_conversion_count"] += 1
        _RUNTIME_STATS["ocr_conversion_total_time"] += elapsed
        _RUNTIME_STATS["ocr_document_times"].append(elapsed)
    else:
        _RUNTIME_STATS["native_conversion_count"] += 1
        _RUNTIME_STATS["native_conversion_total_time"] += elapsed
        _RUNTIME_STATS["native_document_times"].append(elapsed)
    doc = result.document
    raw = doc.export_to_dict() if hasattr(doc, "export_to_dict") else doc.model_dump(mode="json")
    pages = _pages_from_docling(raw)
    full_text = "\n\n".join(page["text"] for page in pages if page["text"].strip())
    return {
        "schema_version": DOCLING_ARTIFACT_SCHEMA_VERSION,
        "document_id": document_id,
        "title": title,
        "source_url": source_url,
        "extractor": "docling",
        "extractor_version": importlib.metadata.version("docling"),
        "source_content_hash": source_hash,
        "document_text_hash": text_hash(full_text),
        "page_count": len(pages),
        "character_count": len(full_text),
        "native_character_count": len(full_text) if not do_ocr else 0,
        "final_character_count": len(full_text),
        "extraction_pass": "ocr_fallback" if do_ocr else "native_text",
        "ocr_used": bool(do_ocr),
        "remote_services_enabled": False,
        "pages": pages,
    }


def _get_converter(*, do_ocr: bool, artifacts_path: str | Path | None) -> Any:
    profile = get_docling_runtime_profile()
    cache_key = (
        f"{Path(artifacts_path).resolve() if artifacts_path else '__default__'}:"
        f"mode={profile.mode}:profile={profile.name}:ocr={int(do_ocr)}:"
        f"pipeline={profile.pipeline_class}:threads={profile.num_threads}:"
        f"page={profile.page_batch_size}:layout={profile.layout_batch_size}:"
        f"ocrbatch={profile.ocr_batch_size}:queue={profile.queue_max_size}"
    )
    converter = _CONVERTER_CACHE.get(cache_key)
    if converter is not None:
        return converter
    start = time.perf_counter()
    try:
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.datamodel.settings import settings
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except Exception as exc:
        if isinstance(exc, ModuleNotFoundError) and str(exc).find("docling") >= 0:
            raise DoclingPdfError(
                "PDF processing requires the optional dependency:\n"
                "pip install -e \".[pdf]\""
            ) from exc
        raise DoclingPdfError(f"docling_import_failed:{type(exc).__name__}") from exc

    if profile.mode == "gpu":
        _preflight_gpu_or_raise()
        settings.perf.page_batch_size = profile.page_batch_size
        try:
            from docling.pipeline.threaded_standard_pdf_pipeline import ThreadedStandardPdfPipeline
        except Exception as exc:
            raise DoclingPdfError(f"docling_threaded_pipeline_unavailable:{type(exc).__name__}") from exc
        accelerator_device = AcceleratorDevice.CUDA
        pipeline_cls = ThreadedStandardPdfPipeline
    else:
        accelerator_device = AcceleratorDevice.CPU
        pipeline_cls = None

    options_kwargs: dict[str, Any] = {
        "enable_remote_services": False,
        "do_ocr": do_ocr,
        "force_backend_text": False,
        "generate_page_images": False,
        "generate_picture_images": False,
        "generate_table_images": False,
        "accelerator_options": AcceleratorOptions(num_threads=profile.num_threads, device=accelerator_device),
    }
    if profile.mode == "gpu":
        options_kwargs.update(
            {
                "layout_batch_size": profile.layout_batch_size,
                "ocr_batch_size": profile.ocr_batch_size,
                "table_batch_size": profile.table_batch_size,
                "queue_max_size": profile.queue_max_size,
            }
        )
        if do_ocr and profile.ocr_backend == "torch":
            options_kwargs["ocr_options"] = RapidOcrOptions(backend="torch")
    if artifacts_path:
        options_kwargs["artifacts_path"] = Path(artifacts_path)
    opts = PdfPipelineOptions(**options_kwargs)
    _disable_optional_docling_enrichment(opts)
    format_option_kwargs: dict[str, Any] = {"pipeline_options": opts}
    if pipeline_cls is not None:
        format_option_kwargs["pipeline_cls"] = pipeline_cls
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(**format_option_kwargs)})
    _CONVERTER_CACHE[cache_key] = converter
    _log_docling_config_once(profile)
    elapsed = time.perf_counter() - start
    if do_ocr:
        _RUNTIME_STATS["ocr_converter_initialized_count"] += 1
        _RUNTIME_STATS["ocr_converter_initialization_time"] += elapsed
    else:
        _RUNTIME_STATS["native_converter_initialized_count"] += 1
        _RUNTIME_STATS["native_converter_initialization_time"] += elapsed
    return converter


def _disable_optional_docling_enrichment(opts: Any) -> None:
    for attr in (
        "do_table_structure",
        "do_picture_classification",
        "do_picture_description",
        "do_formula_enrichment",
        "do_code_enrichment",
        "generate_page_images",
        "generate_picture_images",
        "generate_table_images",
    ):
        if hasattr(opts, attr):
            try:
                setattr(opts, attr, False)
            except Exception:
                pass


def _gpu_info_or_raise() -> dict[str, Any]:
    try:
        import torch  # type: ignore
    except Exception as exc:
        raise DoclingPdfError(f"docling_gpu_torch_missing:{type(exc).__name__}") from exc
    cuda_version = getattr(getattr(torch, "version", None), "cuda", None)
    if not cuda_version:
        raise DoclingPdfError("docling_gpu_torch_not_cuda_build")
    try:
        available = bool(torch.cuda.is_available())
    except Exception as exc:
        raise DoclingPdfError(f"docling_gpu_cuda_check_failed:{type(exc).__name__}") from exc
    if not available:
        raise DoclingPdfError("docling_gpu_cuda_unavailable")
    try:
        index = int(torch.cuda.current_device())
        props = torch.cuda.get_device_properties(index)
        name = str(torch.cuda.get_device_name(index))
        total = float(getattr(props, "total_memory")) / 1_000_000_000
    except Exception as exc:
        raise DoclingPdfError(f"docling_gpu_info_unavailable:{type(exc).__name__}") from exc
    return {"name": name, "vram_gb": total, "cuda_version": str(cuda_version)}


def _preflight_gpu_or_raise() -> None:
    global _GPU_ENV_CHECKED
    if _GPU_ENV_CHECKED:
        return
    _gpu_info_or_raise()
    try:
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.pipeline.threaded_standard_pdf_pipeline import ThreadedStandardPdfPipeline
    except Exception as exc:
        raise DoclingPdfError(f"docling_gpu_preflight_failed:{type(exc).__name__}") from exc
    _ = AcceleratorOptions(num_threads=get_docling_runtime_profile().num_threads, device=AcceleratorDevice.CUDA)
    _ = PdfPipelineOptions()
    _ = ThreadedStandardPdfPipeline
    _GPU_ENV_CHECKED = True


def _rapidocr_gpu_backend() -> str:
    try:
        import rapidocr  # type: ignore

        engine_type = getattr(rapidocr, "EngineType", None)
        if engine_type is not None and hasattr(engine_type, "TORCH"):
            return "torch"
    except Exception:
        pass
    return "onnxruntime"


def _is_cuda_oom(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".casefold()
    return "cuda" in text and ("out of memory" in text or "oom" in text)


def _empty_cuda_cache() -> None:
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _log_docling_config_once(profile: DoclingRuntimeProfile) -> None:
    global _CONFIG_LOGGED
    if _CONFIG_LOGGED:
        return
    _CONFIG_LOGGED = True
    details = describe_docling_runtime()
    print(
        "Docling runtime | "
        f"mode={details['selected_mode']} | "
        f"gpu={details['gpu_name'] or 'n/a'} | "
        f"vram_gb={details['vram_gb']} | "
        f"docling={details['docling_version']} | "
        f"pipeline={details['pipeline_class']} | "
        f"page_batch={details['page_batch_size']} | "
        f"layout_batch={details['layout_batch_size']} | "
        f"ocr_batch={details['ocr_batch_size']} | "
        f"table_batch={details['table_batch_size']} | "
        f"queue={details['queue_max_size']} | "
        f"workers={details['docling_workers']} | "
        f"threads={details['cpu_threads']} | "
        f"ocr_backend={details['ocr_backend']}",
        flush=True,
    )


def _local_temp_pdf_copy(pdf_path: Path) -> Path:
    with tempfile.NamedTemporaryFile(prefix="rdtii-docling-", suffix=".pdf", delete=False) as handle:
        tmp_path = Path(handle.name)
    shutil.copyfile(pdf_path, tmp_path)
    return tmp_path


def _pages_from_docling(raw: dict[str, Any]) -> list[dict[str, Any]]:
    page_numbers: set[int] = set()
    raw_pages = raw.get("pages") or {}
    if isinstance(raw_pages, dict):
        for key, value in raw_pages.items():
            try:
                page_numbers.add(int(value.get("page_no") or key))
            except Exception:
                continue
    elif isinstance(raw_pages, list):
        for index, value in enumerate(raw_pages, start=1):
            try:
                page_numbers.add(int(value.get("page_no") or value.get("page_number") or index))
            except Exception:
                page_numbers.add(index)
    page_items: dict[int, list[tuple[int, str, list[dict[str, Any]]]]] = defaultdict(list)
    for index, item in enumerate(raw.get("texts") or []):
        text = _normalise_text(str(item.get("text") or item.get("orig") or ""))
        if not text:
            continue
        provs = item.get("prov") or []
        if not provs:
            continue
        seen_pages: set[int] = set()
        for prov in provs:
            try:
                page_no = int(prov.get("page_no") or 0)
            except Exception:
                continue
            if page_no <= 0 or page_no in seen_pages:
                continue
            seen_pages.add(page_no)
            page_numbers.add(page_no)
            page_items[page_no].append((index, text, [_clean_provenance(prov)]))
    pages: list[dict[str, Any]] = []
    for page_no in sorted(page_numbers):
        chunks = [text for _idx, text, _prov in sorted(page_items.get(page_no, []), key=lambda row: row[0])]
        text = _normalise_text("\n".join(chunks))
        provenances = [prov for _idx, _text, provs in sorted(page_items.get(page_no, []), key=lambda row: row[0]) for prov in provs]
        pages.append(
            {
                "page_number": page_no,
                "text": text,
                "text_hash": text_hash(text),
                "provenance": provenances[:200],
            }
        )
    return pages


def _clean_provenance(prov: dict[str, Any]) -> dict[str, Any]:
    cleaned = {"page_no": int(prov.get("page_no") or 0)}
    if "charspan" in prov:
        cleaned["charspan"] = prov.get("charspan")
    bbox = prov.get("bbox")
    if isinstance(bbox, dict):
        cleaned["bbox"] = {key: bbox.get(key) for key in ("l", "t", "r", "b", "coord_origin") if key in bbox}
    return cleaned


def _artifact_quality_ok(artifact: dict[str, Any]) -> tuple[bool, str]:
    pages = artifact.get("pages") or []
    if int(artifact.get("page_count") or len(pages) or 0) <= 0:
        return False, "docling_page_count_zero"
    text = document_text(artifact)
    char_count = int(artifact.get("character_count") or len(text))
    if char_count < MIN_DOCUMENT_CHARS:
        return False, "docling_text_too_short"
    non_empty = sum(1 for page in pages if str(page.get("text") or "").strip())
    if pages and non_empty / max(len(pages), 1) < MIN_NON_EMPTY_PAGE_RATIO:
        return False, "docling_too_many_empty_pages"
    if text and text.count("\ufffd") / max(len(text), 1) > MAX_REPLACEMENT_CHAR_RATIO:
        return False, "docling_replacement_char_ratio_high"
    for page in pages:
        if int(page.get("page_number") or 0) <= 0:
            return False, "docling_invalid_page_number"
    return True, ""


def _normalise_text(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalise_for_lookup(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
