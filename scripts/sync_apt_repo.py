#!/usr/bin/env python3
"""Incrementally archive packages from a Debian-style APT repository."""

from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import lzma
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from email.utils import parsedate_to_datetime


USER_AGENT = "mowang-archive-sync/1.0"
INDEX_CANDIDATES = ("Packages.xz", "Packages.bz2", "Packages.gz", "Packages")
REQUEST_INTERVAL = max(0, float(os.environ.get("REQUEST_INTERVAL", "3")))
_last_request = 0.0


def fetch(url: str, destination: Path | None = None) -> bytes | None:
    global _last_request
    encoded_url = quote(url, safe=":/?&=%")
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            time.sleep(max(0, REQUEST_INTERVAL - (time.monotonic() - _last_request)))
            _last_request = time.monotonic()
            request = Request(encoded_url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=120) as response:
                if destination is None:
                    return response.read()
                with destination.open("wb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
                return None
        except HTTPError as error:
            last_error = error
            if error.code in (404, 410):
                raise FileNotFoundError(f"HTTP {error.code}: {url}") from error
            if error.code not in (429, 500, 502, 503, 504):
                raise
            if attempt < 4:
                delay = 60 * (2 ** attempt)
                retry_after = error.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                reset = error.headers.get("X-RateLimit-Reset")
                if reset and reset.isdigit():
                    delay = max(delay, int(reset) - time.time() + 1)
                if delay > 7200:
                    raise RuntimeError("Server requests a wait longer than two hours; retry next scheduled run") from error
                print(f"HTTP {error.code}; waiting {delay:.0f}s before retry", flush=True)
                time.sleep(delay)
        except Exception as error:  # Network errors vary by Python/OpenSSL version.
            last_error = error
            if attempt < 4:
                time.sleep(5 * (2 ** attempt))
    raise RuntimeError(f"Failed to download {url}: {last_error}")


def load_upstream_index(source: str) -> str:
    base = source.rstrip("/") + "/"
    errors: list[str] = []
    for name in INDEX_CANDIDATES:
        url = urljoin(base, name)
        try:
            payload = fetch(url)
            assert payload is not None
            if name.endswith(".xz"):
                payload = lzma.decompress(payload)
            elif name.endswith(".bz2"):
                payload = bz2.decompress(payload)
            elif name.endswith(".gz"):
                payload = gzip.decompress(payload)
            print(f"Using upstream index: {url}")
            return payload.decode("utf-8", errors="replace")
        except Exception as error:
            errors.append(f"{name}: {error}")
    raise RuntimeError("No readable Packages index found:\n" + "\n".join(errors))


def parse_packages(text: str) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    normalized = text.replace("\r\n", "\n").strip()
    for paragraph in re.split(r"\n\s*\n", normalized):
        fields: dict[str, str] = {}
        current = ""
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")) and current:
                fields[current] += "\n" + line
                continue
            key, separator, value = line.partition(":")
            if separator:
                current = key
                fields[key] = value.lstrip()
        if fields.get("Package"):
            packages.append(fields)
    return packages


def safe_component(value: str, limit: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9.+~_-]", "_", value)
    return (cleaned or "unknown")[:limit]


def existing_hashes() -> set[str]:
    index = Path("Packages")
    if not index.exists():
        return set()
    return {
        package["SHA256"].lower()
        for package in parse_packages(index.read_text(encoding="utf-8", errors="replace"))
        if package.get("SHA256")
    }


def download_package(source: str, package: dict[str, str]) -> Path:
    required = ("Package", "Version", "Architecture", "SHA256", "Filename")
    missing = [field for field in required if not package.get(field)]
    if missing:
        raise ValueError(f"Package entry is missing fields: {', '.join(missing)}")

    digest = package["SHA256"].lower()
    architecture = safe_component(package["Architecture"])
    filename = "_".join(
        (
            safe_component(package["Package"]),
            safe_component(package["Version"]),
            architecture,
            digest,
        )
    ) + ".deb"
    destination_dir = Path("debs") / architecture
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / filename
    package_url = urljoin(source.rstrip("/") + "/", package["Filename"].lstrip("./"))

    with tempfile.NamedTemporaryFile(dir=destination_dir, suffix=".part", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        fetch(package_url, temporary)
        hasher = hashlib.sha256()
        with temporary.open("rb") as downloaded:
            while chunk := downloaded.read(1024 * 1024):
                hasher.update(chunk)
        actual_digest = hasher.hexdigest()
        if actual_digest != digest:
            raise ValueError(
                f"SHA256 mismatch for {package_url}: expected {digest}, got {actual_digest}"
            )
        expected_size = package.get("Size")
        if expected_size and temporary.stat().st_size != int(expected_size):
            raise ValueError(f"Size mismatch for {package_url}")
        subprocess.run(
            ["dpkg-deb", "--info", os.fspath(temporary)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def write_indexes() -> None:
    result = subprocess.run(
        ["apt-ftparchive", "packages", "debs"],
        check=True,
        stdout=subprocess.PIPE,
    )
    paragraphs = [item.strip() for item in result.stdout.split(b"\n\n") if item.strip()]
    payload = b"\n\n".join(sorted(paragraphs)) + (b"\n" if paragraphs else b"")
    Path("Packages").write_bytes(payload)
    Path("Packages.bz2").write_bytes(bz2.compress(payload, compresslevel=9))
    Path("Packages.xz").write_bytes(lzma.compress(payload, preset=9 | lzma.PRESET_EXTREME))
    with Path("Packages.gz").open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as zipped:
            zipped.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", required=True,
                        help="APT source in priority order; repeat for additional sources")
    parser.add_argument("--max-downloads", type=int, default=100)
    args = parser.parse_args()
    if args.max_downloads < 0:
        parser.error("--max-downloads must be 0 or greater")

    known = existing_hashes()
    missing: list[tuple[str, dict[str, str]]] = []
    queued = set(known)
    for source in args.source:
        upstream = parse_packages(load_upstream_index(source))
        source_missing = 0
        for package in upstream:
            digest = package.get("SHA256", "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"Missing or invalid SHA256 in {source}: {package.get('Package')}")
            if digest not in queued:
                missing.append((source, package))
                queued.add(digest)
                source_missing += 1
        print(f"{source}: {len(upstream)} entries, {source_missing} new unique packages")
    print(f"Missing: {len(missing)}; download limit: {args.max_downloads or 'all'}")

    downloaded = 0
    unavailable = 0
    for source, package in missing:
        if args.max_downloads and downloaded >= args.max_downloads:
            break
        try:
            destination = download_package(source, package)
        except FileNotFoundError as error:
            unavailable += 1
            print(f"Unavailable, will retry next run: {error}", flush=True)
            continue
        downloaded += 1
        print(f"[{downloaded}] Downloaded {destination}", flush=True)

    print(f"Downloaded: {downloaded}; unavailable: {unavailable}")

    write_indexes()


if __name__ == "__main__":
    main()
