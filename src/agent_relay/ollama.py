from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"


class OllamaError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class OllamaConfig:
    host: str = "http://localhost:11434"
    default_model: str | None = DEFAULT_OLLAMA_MODEL
    api_key: str | None = None
    timeout_seconds: float = 120.0
    temperature: float = 0.0
    num_predict: int = 4096
    think: bool | str = False
    seed: int | None = None

    @classmethod
    def from_env(cls) -> OllamaConfig:
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").strip()
        default_model = (
            os.environ.get("LOCAL_MODEL")
            or os.environ.get("OLLAMA_MODEL")
            or DEFAULT_OLLAMA_MODEL
        )
        api_key = os.environ.get("OLLAMA_API_KEY") or None
        try:
            timeout_seconds = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120"))
        except ValueError as exc:
            raise OllamaError("OLLAMA_TIMEOUT_SECONDS must be numeric") from exc
        try:
            temperature = float(os.environ.get("OLLAMA_TEMPERATURE", "0"))
        except ValueError as exc:
            raise OllamaError("OLLAMA_TEMPERATURE must be numeric") from exc
        try:
            num_predict = int(os.environ.get("OLLAMA_NUM_PREDICT", "4096"))
        except ValueError as exc:
            raise OllamaError("OLLAMA_NUM_PREDICT must be an integer") from exc
        seed_value = os.environ.get("OLLAMA_SEED")
        try:
            seed = int(seed_value) if seed_value is not None and seed_value.strip() else None
        except ValueError as exc:
            raise OllamaError("OLLAMA_SEED must be an integer when provided") from exc
        think_value = os.environ.get("OLLAMA_THINK", "false").strip().lower()
        if think_value not in {
            "true", "false", "1", "0", "yes", "no",
            "low", "medium", "high",
        }:
            raise OllamaError(
                "OLLAMA_THINK must be false, true, low, medium, or high"
            )
        if timeout_seconds <= 0:
            raise OllamaError("OLLAMA_TIMEOUT_SECONDS must be positive")
        if num_predict <= 0:
            raise OllamaError("OLLAMA_NUM_PREDICT must be positive")
        return cls(
            host=host or "http://localhost:11434",
            default_model=default_model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            num_predict=num_predict,
            think=(
                think_value
                if think_value in {"low", "medium", "high"}
                else think_value in {"true", "1", "yes"}
            ),
            seed=seed,
        )


@dataclass(frozen=True)
class OllamaGeneration:
    model: str
    text: str
    duration_seconds: float
    raw: Mapping[str, Any]


class OllamaClient:
    """Small HTTP client for Ollama and Ollama-compatible local gateways."""

    def __init__(self, config: OllamaConfig | None = None) -> None:
        self.config = config or OllamaConfig.from_env()

    @property
    def host(self) -> str:
        return self.config.host.rstrip("/")

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
            headers["X-Api-Key"] = self.config.api_key

        request = Request(
            f"{self.host}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            detail = self._error_detail(body)
            raise OllamaError(
                f"Ollama request failed with HTTP {exc.code}: {detail}",
                status_code=exc.code,
            ) from exc
        except URLError as exc:
            raise OllamaError(f"cannot reach Ollama at {self.host}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise OllamaError(f"Ollama request timed out after {self.config.timeout_seconds:g}s") from exc

        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OllamaError("Ollama returned invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise OllamaError("Ollama returned a non-object JSON response")
        if value.get("error"):
            raise OllamaError(f"Ollama returned an error: {value['error']}")
        return value

    @staticmethod
    def _error_detail(body: str) -> str:
        try:
            value = json.loads(body)
        except json.JSONDecodeError:
            return body[:300] or "no response body"
        if isinstance(value, Mapping) and value.get("error"):
            return str(value["error"])[:300]
        return body[:300] or "no response body"

    def list_models(self) -> list[dict[str, Any]]:
        value = self._request("/api/tags")
        models = value.get("models", [])
        if not isinstance(models, list):
            raise OllamaError("Ollama tags response did not contain a models list")
        return [dict(item) for item in models if isinstance(item, Mapping)]

    def version(self) -> str | None:
        value = self._request("/api/version")
        version = value.get("version")
        return str(version) if version is not None else None

    def generate(
        self,
        system_prompt: str,
        prompt: str,
        *,
        model: str | None = None,
        temperature: float | None = None,
        num_predict: int | None = None,
        json_mode: bool = False,
        think: bool | str | None = None,
        seed: int | None = None,
    ) -> OllamaGeneration:
        selected_model = (model or self.config.default_model or "").strip()
        if not selected_model:
            raise OllamaError(
                "no model configured; pass model= or set LOCAL_MODEL/OLLAMA_MODEL"
            )
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": selected_model,
            "system": system_prompt,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": (
                    self.config.temperature if temperature is None else temperature
                ),
                "num_predict": (
                    self.config.num_predict if num_predict is None else num_predict
                ),
            },
        }
        if json_mode:
            payload["format"] = "json"
        payload["think"] = self.config.think if think is None else think
        selected_seed = self.config.seed if seed is None else seed
        if selected_seed is not None:
            payload["options"]["seed"] = selected_seed
        value = self._request(
            "/api/generate",
            method="POST",
            payload=payload,
        )
        elapsed = time.perf_counter() - started
        text = value.get("response")
        if not isinstance(text, str):
            message = value.get("message")
            if isinstance(message, Mapping):
                text = message.get("content")
        if not isinstance(text, str):
            raise OllamaError("Ollama response did not contain generated text")
        if not text.strip():
            raise OllamaError(
                "Ollama returned empty generated text; increase OLLAMA_NUM_PREDICT"
            )
        return OllamaGeneration(
            model=str(value.get("model") or selected_model),
            text=text,
            duration_seconds=elapsed,
            raw=value,
        )
