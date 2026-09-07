"""Thin OpenAI chat client with on-disk caching, retries and cost accounting.

Only ``gpt-5.6-luna`` is allowed in this project (see CLAUDE.md).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from openai import OpenAI, APIError, RateLimitError, APIConnectionError, APITimeoutError

log = logging.getLogger(__name__)

MODEL = "gpt-5.6-luna"
# USD per 1M tokens, as given by the user (2026-09).
PRICE = {"input": 0.20, "cached": 0.02, "output": 1.20}


@dataclass
class Usage:
    calls: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, u) -> None:
        with self.lock:
            self.calls += 1
            pt = u.prompt_tokens or 0
            ct = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
            self.prompt_tokens += pt
            self.cached_tokens += ct
            self.completion_tokens += u.completion_tokens or 0

    @property
    def cost_usd(self) -> float:
        uncached = self.prompt_tokens - self.cached_tokens
        return (uncached * PRICE["input"] + self.cached_tokens * PRICE["cached"]
                + self.completion_tokens * PRICE["output"]) / 1e6

    def to_dict(self) -> dict:
        return {"calls": self.calls, "cache_hits": self.cache_hits,
                "prompt_tokens": self.prompt_tokens, "cached_tokens": self.cached_tokens,
                "completion_tokens": self.completion_tokens, "cost_usd": round(self.cost_usd, 4)}


class LLM:
    def __init__(self, model: str = MODEL, cache_dir: Path | None = None,
                 max_retries: int = 6, timeout: float = 300.0, budget_usd: float | None = None):
        if model != MODEL:
            raise ValueError(f"only {MODEL} is allowed, got {model}")
        self.model = model
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=timeout, max_retries=0)
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self.budget_usd = budget_usd
        self.usage = Usage()

    # ------------------------------------------------------------------ #
    def _cache_path(self, key: str) -> Path | None:
        return self.cache_dir / f"{key}.json" if self.cache_dir else None

    def chat(self, system: str | None, user: str, *, json_mode: bool = True,
             temperature: float | None = None, tag: str = "") -> str:
        """Return the assistant text. Cached by (model, system, user)."""
        h = hashlib.sha256(json.dumps([self.model, system, user, json_mode]).encode()).hexdigest()[:32]
        cp = self._cache_path(h)
        if cp and cp.exists():
            with self.usage.lock:
                self.usage.cache_hits += 1
            return json.loads(cp.read_text())["content"]

        if self.budget_usd is not None and self.usage.cost_usd > self.budget_usd:
            raise RuntimeError(f"LLM budget exceeded: ${self.usage.cost_usd:.2f} > ${self.budget_usd}")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        kwargs = {"model": self.model, "messages": messages}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if temperature is not None:
            kwargs["temperature"] = temperature

        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                t0 = time.time()
                resp = self.client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""
                self.usage.add(resp.usage)
                log.debug("llm %s ok in %.1fs (%s tok)", tag, time.time() - t0, resp.usage.total_tokens)
                if cp:
                    cp.write_text(json.dumps({"tag": tag, "content": content,
                                              "usage": resp.usage.model_dump()}))
                return content
            except (RateLimitError, APIConnectionError, APITimeoutError) as e:
                if attempt == self.max_retries:
                    raise
                log.warning("llm %s: %s — retry %d in %.0fs", tag, type(e).__name__, attempt + 1, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            except APIError as e:
                if attempt == self.max_retries or getattr(e, "status_code", 500) < 500:
                    raise
                log.warning("llm %s: %s — retry %d in %.0fs", tag, e, attempt + 1, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise RuntimeError("unreachable")


def parse_json(text: str) -> dict:
    """Parse a JSON object, tolerating ``` fences."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in LLM output: {text[:200]!r}")
    return json.loads(t[start:end + 1])
