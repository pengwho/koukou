"""LLM 供應者：Claude（Anthropic API）或任何 OpenAI 相容端點（Ollama、LM Studio 等本機模型）。

只用標準函式庫發 HTTP，不需要額外 SDK。兩種供應者都回傳同樣格式：
    ({"results": [{"id": 0, "category": "...", "confidence": 0.9}, ...]}, Usage)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .. import credentials

DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"
ANTHROPIC_KEY = "anthropic_api_key"


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    seconds: float = 0.0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.calls += other.calls
        self.seconds += other.seconds


class Provider(Protocol):
    name: str
    model: str

    def classify(self, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], Usage]: ...


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float = 90) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json", **headers}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise LLMError(f"HTTP {e.code}：{detail}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"連不上 {url}：{e.reason}") from e


@dataclass
class AnthropicProvider:
    api_key: str
    model: str = DEFAULT_ANTHROPIC_MODEL
    name: str = "anthropic"
    base_url: str = "https://api.anthropic.com"

    def classify(self, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        started = time.monotonic()
        body = _post_json(
            f"{self.base_url}/v1/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            {
                "model": self.model,
                "max_tokens": 4096,
                "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                # 用 tool use 強制輸出符合 schema 的 JSON
                "tools": [{"name": "record_categories", "description": "記錄每個品項的分類",
                           "input_schema": schema}],
                "tool_choice": {"type": "tool", "name": "record_categories"},
            },
        )
        usage = Usage(
            input_tokens=body.get("usage", {}).get("input_tokens", 0),
            output_tokens=body.get("usage", {}).get("output_tokens", 0),
            calls=1, seconds=time.monotonic() - started,
        )
        for block in body.get("content", []):
            if block.get("type") == "tool_use":
                return block.get("input") or {}, usage
        raise LLMError("模型沒有回傳分類結果")


@dataclass
class OpenAICompatProvider:
    """Ollama（http://localhost:11434/v1）、LM Studio 或其他 OpenAI 相容 API。"""

    base_url: str
    model: str
    api_key: str = ""
    name: str = "openai-compatible"

    def classify(self, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        started = time.monotonic()
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = _post_json(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers,
            {
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system + "\n\n只輸出 JSON，格式：" +
                     json.dumps(schema, ensure_ascii=False)},
                    {"role": "user", "content": user},
                ],
            },
        )
        u = body.get("usage") or {}
        usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), 1, time.monotonic() - started)
        try:
            text = body["choices"][0]["message"]["content"]
            return json.loads(text), usage
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise LLMError(f"模型回傳的不是 JSON：{str(body)[:200]}") from e


@dataclass
class ProviderInfo:
    configured: bool
    name: str = ""
    model: str = ""
    hint: str = ""
    provider: Provider | None = field(default=None, repr=False)


def from_environment() -> ProviderInfo:
    """依設定挑供應者：KOUKOU_AI_PROVIDER=anthropic（預設）或 openai。"""
    kind = os.environ.get("KOUKOU_AI_PROVIDER", "anthropic").strip().lower()
    if kind in ("openai", "ollama", "openai-compatible"):
        base = os.environ.get("KOUKOU_AI_BASE_URL", "http://localhost:11434/v1")
        model = os.environ.get("KOUKOU_AI_MODEL", "")
        if not model:
            return ProviderInfo(False, "openai-compatible", "", "請設定 KOUKOU_AI_MODEL，例如 qwen2.5:7b")
        p = OpenAICompatProvider(base, model, os.environ.get("KOUKOU_AI_API_KEY", ""))
        return ProviderInfo(True, p.name, model, f"本機模型 {base}", p)
    key = credentials.get_secret(ANTHROPIC_KEY) or os.environ.get("ANTHROPIC_API_KEY", "")
    model = os.environ.get("KOUKOU_AI_MODEL", DEFAULT_ANTHROPIC_MODEL)
    if not key:
        return ProviderInfo(False, "anthropic", model, "執行 koukou ai setup 設定 Claude API 金鑰")
    p = AnthropicProvider(key, model)
    return ProviderInfo(True, p.name, model, "Claude", p)
