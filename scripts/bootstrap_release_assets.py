"""Fetch and verify frozen replay assets for a release checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "release-assets.json"
DEFAULT_CACHE_DIR = ROOT / ".rdtii_release_assets"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/bootstrap_release_assets.py",
        description="Download/extract frozen RDTII replay assets with SHA256 verification.",
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--asset-dir", help="Use already-downloaded asset files from this directory.")
    parser.add_argument("--base-url", help="Override the manifest base_url for downloads.")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--output-root", default=str(ROOT))
    parser.add_argument("--economies", nargs="+", choices=("singapore", "australia", "malaysia"), help="Economies to restore.")
    parser.add_argument("--only", nargs="*", help="Optional asset filenames to process.")
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_or_download(asset: dict, *, asset_dir: Path | None, base_url: str, cache_dir: Path) -> Path:
    filename = str(asset["filename"])
    target = cache_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    if asset_dir is not None:
        source = asset_dir / filename
        if not source.exists():
            raise RuntimeError(f"release asset missing from --asset-dir: {source}")
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        return target
    url = str(asset.get("url") or "").strip()
    if not url:
        if not base_url:
            raise RuntimeError(f"asset has no url and no base_url is configured: {filename}")
        url = f"{base_url.rstrip('/')}/{filename}"
    parsed = urlparse(url)
    if parsed.scheme == "file":
        source = Path(urllib.request.url2pathname(parsed.path))
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        return target
    urllib.request.urlretrieve(url, target)  # noqa: S310 - release URL is explicit manifest input.
    return target


def _verify(asset: dict, path: Path) -> None:
    expected_size = int(asset["size"])
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(f"{path.name} size mismatch: expected {expected_size}, got {actual_size}")
    expected_hash = str(asset["sha256"]).lower()
    actual_hash = _sha256(path)
    if actual_hash != expected_hash:
        raise RuntimeError(f"{path.name} SHA256 mismatch: expected {expected_hash}, got {actual_hash}")


def _safe_extract_tar_gz(path: Path, output_root: Path) -> None:
    output_root = output_root.resolve()
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            parts = Path(member.name).parts
            if "final_submit" in parts:
                raise RuntimeError(f"release replay assets must not contain final_submit files: {member.name}")
            target = (output_root / member.name).resolve()
            if os.path.commonpath([str(output_root), str(target)]) != str(output_root):
                raise RuntimeError(f"unsafe archive member path: {member.name}")
        archive.extractall(output_root, members=members)


def _verify_required_paths(asset: dict, output_root: Path) -> None:
    missing = []
    for value in asset.get("required_paths", []):
        path = output_root / str(value)
        if not path.exists():
            missing.append(str(value))
    if missing:
        raise RuntimeError(f"{asset['filename']} did not create required paths: {', '.join(missing)}")


def main() -> int:
    args = _parser().parse_args()
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        raise RuntimeError(f"release asset manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = list(manifest.get("assets") or [])
    if not assets:
        raise RuntimeError(f"release asset manifest contains no assets: {manifest_path}")
    only = set(args.only or [])
    economies = set(args.economies or [])
    if economies:
        assets = [asset for asset in assets if str(asset.get("economy") or "") in economies]
        if not assets:
            raise RuntimeError("--economies did not match any manifest assets")
    if only:
        assets = [asset for asset in assets if str(asset.get("filename")) in only]
        if not assets:
            raise RuntimeError("--only did not match any manifest assets")
    asset_dir = Path(args.asset_dir) if args.asset_dir else None
    cache_dir = Path(args.cache_dir)
    output_root = Path(args.output_root)
    base_url = str(args.base_url or manifest.get("base_url") or "")

    for asset in assets:
        path = _copy_or_download(asset, asset_dir=asset_dir, base_url=base_url, cache_dir=cache_dir)
        _verify(asset, path)
        if str(asset.get("format") or "").casefold() != "tar.gz":
            raise RuntimeError(f"unsupported asset format for {path.name}: {asset.get('format')}")
        _safe_extract_tar_gz(path, output_root)
        _verify_required_paths(asset, output_root)
        print(f"verified and extracted {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
