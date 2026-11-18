#!/usr/bin/env python3
"""
Parseur de la consommation réelle (tokens et coût) à partir de la sortie JSON des workers CLI
(opencode, claude, agy). Stdlib uniquement, aucun appel réseau ni LLM.
"""
from __future__ import annotations

import json
from typing import Any


def _int(val: Any) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _float(val: Any) -> float:
    try:
        return float(val or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _iter_json_objects(text: str) -> list[dict[str, Any]]:
    """Extrait les objets JSON des lignes commençant par '{', en ignorant le bruit stderr/stdout."""
    objs: list[dict[str, Any]] = []
    if not text:
        return objs
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            objs.append(parsed)
    return objs


def parse_usage(fmt: str, text: str) -> dict[str, Any] | None:
    """Parse la sortie d'un worker selon `fmt` et retourne les métriques normalisées ou None."""
    objs = _iter_json_objects(text)
    if not objs:
        return None

    if fmt == "opencode_json":
        found = False
        tokens_in = 0
        tokens_out = 0
        tokens_cache = 0
        cost_usd = 0.0
        for obj in objs:
            part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
            ev_type = obj.get("type") or part.get("type")
            if ev_type not in ("step_finish", "step-finish"):
                continue
            found = True
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            tokens_in += _int(tokens.get("input"))
            tokens_out += _int(tokens.get("output")) + _int(tokens.get("reasoning"))
            tokens_cache += _int(cache.get("read")) + _int(cache.get("write"))
            cost_usd += _float(part.get("cost"))
        if not found:
            return None
        return {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_cache": tokens_cache,
            "cost_usd": cost_usd,
            "cost_kind": "measured",
        }

    if fmt == "claude_json":
        target: dict[str, Any] | None = None
        for obj in objs:
            if "total_cost_usd" in obj or "usage" in obj:
                target = obj
        if target is None:
            return None
        usage = target.get("usage") if isinstance(target.get("usage"), dict) else {}
        return {
            "tokens_in": _int(usage.get("input_tokens")),
            "tokens_out": _int(usage.get("output_tokens")),
            "tokens_cache": _int(usage.get("cache_creation_input_tokens"))
            + _int(usage.get("cache_read_input_tokens")),
            "cost_usd": _float(target.get("total_cost_usd")),
            "cost_kind": "api_equivalent",
        }

    if fmt == "agy_json":
        target = None
        for obj in objs:
            if "usage" in obj and isinstance(obj.get("usage"), dict):
                target = obj
        if target is None:
            return None
        usage = target["usage"]
        return {
            "tokens_in": _int(usage.get("input_tokens")),
            "tokens_out": _int(usage.get("output_tokens")) + _int(usage.get("thinking_tokens")),
            "tokens_cache": _int(usage.get("cache_read_tokens")),
            "cost_usd": None,
            "cost_kind": "tokens_only",
        }

    return None
