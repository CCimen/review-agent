"""Fixed OpenAI embedding requests owned by the credential-bearing gateway."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
from typing import cast
import urllib.error
import urllib.request

from .code_graph_contract import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MAX_RESPONSE_BYTES,
    EMBEDDING_MAX_TEXT_CHARS,
    EMBEDDING_MAX_TEXTS,
    EMBEDDING_MODEL,
    GraphError,
)
from .source_control import SameOriginHttpsRedirectHandler


def embedding_inputs(value: Mapping[str, object]) -> list[str]:
    if (set(value) not in ({"model", "input"}, {"model", "input", "dimensions"})
            or value.get("model") != EMBEDDING_MODEL
            or value.get("dimensions", EMBEDDING_DIMENSIONS) != EMBEDDING_DIMENSIONS):
        raise GraphError("invalid embedding request")
    raw = value.get("input")
    if not isinstance(raw, list):
        raise GraphError("embedding inputs must be a list")
    values = cast(list[object], raw)
    if not 1 <= len(values) <= EMBEDDING_MAX_TEXTS:
        raise GraphError("embedding request exceeds the text limit")
    if any(not isinstance(text, str) or not text or len(text) > EMBEDDING_MAX_TEXT_CHARS for text in values):
        raise GraphError("embedding text exceeds its bound")
    # At most 8,192 UTF-8 bytes per text bounds the tokenizer's byte input too.
    return [cast(str, text)[:2048] for text in values]


def openai_embeddings(
    texts: list[str], api_key: str, *, opener: urllib.request.OpenerDirector | None = None
) -> dict[str, object]:
    """Return validated vectors only; never serialize provider errors or headers."""
    try:
        key = api_key.strip()
        if not key or len(key) > 4096 or any(character.isspace() for character in key):
            raise GraphError("embedding credential is unavailable")
        payload = json.dumps({
            "model": EMBEDDING_MODEL, "input": texts,
            "dimensions": EMBEDDING_DIMENSIONS, "encoding_format": "float",
        }).encode("utf-8")
        request = urllib.request.Request(
            "https://api.openai.com/v1/embeddings", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        transport = opener or urllib.request.build_opener(SameOriginHttpsRedirectHandler())
        with transport.open(request, timeout=30) as response:
            raw = response.read(EMBEDDING_MAX_RESPONSE_BYTES + 1)
        if len(raw) > EMBEDDING_MAX_RESPONSE_BYTES:
            raise GraphError("embedding response exceeds its bound")
        decoded: object = json.loads(raw)
        if not isinstance(decoded, dict):
            raise GraphError("invalid embedding response")
        body = cast(dict[str, object], decoded)
        rows = body.get("data")
        if not isinstance(rows, list) or len(cast(list[object], rows)) != len(texts):
            raise GraphError("embedding response count does not match its input")
        vectors: dict[int, list[float]] = {}
        for row in cast(list[object], rows):
            if not isinstance(row, dict):
                raise GraphError("invalid embedding row")
            fields = cast(dict[str, object], row)
            index, vector = fields.get("index"), fields.get("embedding")
            if (type(index) is not int or not 0 <= index < len(texts) or index in vectors
                    or not isinstance(vector, list) or len(cast(list[object], vector)) != EMBEDDING_DIMENSIONS):
                raise GraphError("invalid embedding vector")
            values = cast(list[object], vector)
            if any(type(number) not in {int, float} or not math.isfinite(cast(float, number)) for number in values):
                raise GraphError("invalid embedding value")
            vectors[index] = [float(cast(float, number)) for number in values]
        return {
            "model": EMBEDDING_MODEL,
            "data": [{"index": index, "embedding": vectors[index]} for index in range(len(texts))],
        }
    except urllib.error.HTTPError as exc:
        exc.close()
        raise GraphError("embedding provider request failed") from None
    except (OSError, ValueError, UnicodeError) as exc:
        if isinstance(exc, GraphError):
            raise
        raise GraphError("embedding provider is unavailable") from None
