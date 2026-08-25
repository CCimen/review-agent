#!/usr/bin/env python3
"""Validate the public documentation manifest and optional built route set."""

from __future__ import annotations

import json
import re
from argparse import ArgumentParser
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "website" / "public-documents.json"
DOCS_ROUTE_BASE = "/docs"
ALLOWED_STATUSES = {"current", "transitional", "target", "proposal"}
FORBIDDEN_PREFIXES = ("docs/goals/", "docs-internal/", "review-learning/", "bootstrap/")
LOCAL_PATH = re.compile(r"(?:^|[\s(`])(?:/Users/|/home/)")
SECRET_MARKERS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
)


def front_matter(text: str, relative_path: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError(f"{relative_path}: missing YAML front matter")
    parts = text.split("---\n", 2)
    if len(parts) != 3:
        raise ValueError(f"{relative_path}: incomplete YAML front matter")

    values: dict[str, str] = {}
    for line in parts[1].splitlines():
        if not line or line.startswith((" ", "#")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def validate_document(relative_path: str) -> list[str]:
    errors: list[str] = []
    if relative_path.startswith(FORBIDDEN_PREFIXES):
        return [f"{relative_path}: private or internal path is not publishable"]

    path = (ROOT / relative_path).resolve()
    if not path.is_relative_to(ROOT):
        return [f"{relative_path}: public document path leaves the repository"]
    if not path.is_file():
        return [f"{relative_path}: public document does not exist"]

    text = path.read_text(encoding="utf-8")
    try:
        metadata = front_matter(text, relative_path)
    except ValueError as exc:
        return [str(exc)]

    status = metadata.get("status")
    if status not in ALLOWED_STATUSES:
        choices = ", ".join(sorted(ALLOWED_STATUSES))
        errors.append(f"{relative_path}: status must be one of {choices}")

    verified = metadata.get("last_verified", "")
    try:
        date.fromisoformat(verified)
    except ValueError:
        errors.append(f"{relative_path}: last_verified must be an ISO date")

    if LOCAL_PATH.search(text):
        errors.append(f"{relative_path}: contains a local absolute filesystem path")
    if any(marker.search(text) for marker in SECRET_MARKERS):
        errors.append(f"{relative_path}: contains a likely credential or private key")
    return errors


def document_route(relative_path: str) -> str:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    slug = front_matter(text, relative_path).get("slug", "")
    if not slug.startswith("/") or slug == "/":
        raise ValueError(f"{relative_path}: slug must be an absolute non-root path")
    return f"{DOCS_ROUTE_BASE}{slug.rstrip('/')}"


def built_route(path: Path, build_directory: Path) -> str:
    relative = path.relative_to(build_directory)
    if relative.name == "index.html":
        parent = relative.parent.as_posix()
        return "/" if parent == "." else f"/{parent}"
    return f"/{relative.with_suffix('').as_posix()}"


def validate_built_routes(build_directory: Path, documents: list[str]) -> list[str]:
    if not build_directory.is_dir():
        return [f"{build_directory}: documentation build directory does not exist"]

    # The local-search theme owns one public, prerendered search page.
    expected = {"/", "/404", "/search"}
    try:
        document_routes = {document_route(document) for document in documents}
        expected.update(document_routes)
    except ValueError as exc:
        return [str(exc)]

    actual = {built_route(path, build_directory) for path in build_directory.rglob("*.html")}
    errors: list[str] = []
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        errors.append(f"built documentation is missing routes: {', '.join(missing)}")
    if unexpected:
        errors.append(f"built documentation has unexpected routes: {', '.join(unexpected)}")
    errors.extend(validate_search_index(build_directory, document_routes))
    return errors


def validate_search_index(
    build_directory: Path, expected_routes: set[str]
) -> list[str]:
    # docusaurus-search-local 0.55.3 emits [{"documents": [{"u": ...}]}].
    index_path = build_directory / "search-index.json"
    if not index_path.is_file():
        return ["built documentation is missing search-index.json"]

    try:
        chunks = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"built search index is unreadable: {exc}"]
    if not isinstance(chunks, list):
        return ["built search index must be a JSON array"]

    indexed_routes: set[str] = set()
    for chunk_number, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            return [f"built search index chunk {chunk_number} must be an object"]
        entries = chunk.get("documents")
        if not isinstance(entries, list):
            return [f"built search index chunk {chunk_number} has no document list"]
        for entry_number, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict) or not isinstance(entry.get("u"), str):
                return [
                    "built search index chunk "
                    f"{chunk_number} entry {entry_number} has no URL"
                ]
            path = urlsplit(entry["u"]).path
            docs_offset = path.find(f"{DOCS_ROUTE_BASE}/")
            if docs_offset < 0:
                return [f"built search index contains a non-document URL: {entry['u']}"]
            indexed_routes.add(path[docs_offset:].rstrip("/"))

    if indexed_routes == expected_routes:
        return []

    errors: list[str] = []
    missing = sorted(expected_routes - indexed_routes)
    unexpected = sorted(indexed_routes - expected_routes)
    if missing:
        errors.append(f"built search index is missing routes: {', '.join(missing)}")
    if unexpected:
        errors.append(f"built search index has unexpected routes: {', '.join(unexpected)}")
    return errors


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument(
        "--build-dir",
        type=Path,
        help="also require built HTML routes and the search index to match the public manifest",
    )
    args = parser.parse_args()

    documents = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(documents, list) or not all(isinstance(item, str) for item in documents):
        raise SystemExit("website/public-documents.json must be a JSON string array")

    errors: list[str] = []
    if len(documents) != len(set(documents)):
        errors.append("website/public-documents.json contains duplicate paths")
    for document in documents:
        errors.extend(validate_document(document))
    if args.build_dir is not None:
        build_directory = args.build_dir
        if not build_directory.is_absolute():
            build_directory = ROOT / build_directory
        errors.extend(validate_built_routes(build_directory.resolve(), documents))

    if errors:
        for error in errors:
            print(f"docs check: {error}")
        return 1

    suffix = " and built routes" if args.build_dir is not None else ""
    print(f"Docs contract OK: {len(documents)} public documents{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
