# llm_client.py
# ----------------------------------------------------------------------
# Provider-agnostic client for any OpenAI-compatible chat-completions API.
# Used with open-weight models: Kimi K3 (Moonshot), Llama 4 (Groq, Together,
# OpenRouter) or a local server (Ollama / vLLM / llama.cpp). Stdlib only.
#
# Model ids and base URLs change often; every preset can be overridden in
# the UI or via environment variables, so verify them in the provider's
# console before relying on them.
# ----------------------------------------------------------------------

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Provider:
    label: str
    base_url: str
    model: str
    key_env: str
    temperature: Optional[float] = 0.1   # None = omit (some reasoning models fix it)
    needs_key: bool = True


PROVIDERS: Dict[str, Provider] = {
    "kimi-k3": Provider("Kimi K3 (Moonshot AI)", "https://api.moonshot.ai/v1", "kimi-k3", "MOONSHOT_API_KEY", temperature=None),
    # Groq no longer lists Llama 4 (checked against its /models endpoint); gpt-oss-120b is its strongest open-weight model
    "gpt-oss-groq": Provider("gpt-oss-120b, open weights (Groq, free tier)", "https://api.groq.com/openai/v1",
                             "openai/gpt-oss-120b", "GROQ_API_KEY"),
    "qwen-groq": Provider("Qwen3.8 27B, open weights (Groq, free tier)", "https://api.groq.com/openai/v1",
                          "qwen/qwen3.8-27b", "GROQ_API_KEY"),
    "llama4-together": Provider("Llama 4 Maverick (Together AI)", "https://api.together.xyz/v1",
                                "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8", "TOGETHER_API_KEY"),
    "llama4-openrouter": Provider("Llama 4 Maverick (OpenRouter)", "https://openrouter.ai/api/v1",
                                  "meta-llama/llama-4-maverick", "OPENROUTER_API_KEY"),
    "local": Provider("Local server (Ollama / vLLM)", "http://localhost:11434/v1", "llama4", "LOCAL_LLM_API_KEY",
                      needs_key=False),
    "custom": Provider("Custom OpenAI-compatible", "", "", "LLM_API_KEY"),
}

DEFAULT_PROVIDER = "gpt-oss-groq"


class LLMError(RuntimeError):
    pass


def _retry_delay(err: "urllib.error.HTTPError", body: str, attempt: int) -> float:
    """Honour the provider's own wait hint on rate limits (Retry-After header or 'try again in 3.4s')."""
    hdr = err.headers.get("Retry-After") if err.headers else None
    try:
        if hdr:
            return min(float(hdr) + 0.5, 30.0)
    except ValueError:
        pass
    m = re.search(r"try again in (?:(\d+)m)?\s*(\d+(?:\.\d+)?)s", body or "")
    if m:
        return min(int(m.group(1) or 0) * 60 + float(m.group(2)) + 0.5, 30.0)
    return 1.5 * (attempt + 1)


class LLMClient:
    def __init__(self, base_url: str, model: str, api_key: str = "", temperature: Optional[float] = 0.1,
                 timeout: int = 90, retries: int = 4):
        if not base_url or not model:
            raise LLMError("Base URL and model name are required.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.retries = retries
        self._json_mode = True   # turned off if the server rejects response_format

    @classmethod
    def from_provider(cls, key: str, api_key: str = "", base_url: str = "", model: str = "") -> "LLMClient":
        p = PROVIDERS[key]
        return cls(
            base_url=base_url or p.base_url,
            model=model or p.model,
            api_key=api_key or os.environ.get(p.key_env, ""),
            temperature=p.temperature,
        )

    # ------------------------------------------------------------------
    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "tenancy-audit/2.0",   # default Python-urllib UA is blocked by Cloudflare (error 1010)
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def chat(self, messages: List[Dict[str, str]], json_output: bool = False, max_tokens: int = 4096) -> str:
        payload: Dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if json_output and self._json_mode:
            payload["response_format"] = {"type": "json_object"}

        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                data = self._post(payload)
                return (data["choices"][0]["message"].get("content") or "").strip()
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code in (400, 422) and "response_format" in payload:
                    # server doesn't support JSON mode - retry once without it
                    payload.pop("response_format", None)
                    self._json_mode = False
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(_retry_delay(e, body, attempt))
                    last = e
                    continue
                raise LLMError(f"HTTP {e.code} from {self.base_url}: {body}") from e
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as e:
                last = e
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise LLMError(f"LLM request failed: {last}")

    def ping(self) -> str:
        return self.chat([{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=16)


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating code fences and surrounding prose."""
    if not text:
        raise ValueError("empty reply")
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.IGNORECASE)
    # drop reasoning blocks some open models emit
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise ValueError("no JSON found")
        end = max(t.rfind("}"), t.rfind("]"))
        return json.loads(t[start:end + 1])
