"""
ROUTER DE LLM — multi-modelo por agente/stream/tarea
=====================================================
Abstrae la llamada de TEXTO (sin tools ni visión) para poder asignar el modelo por agente/stream:
- model_id tipo "claude-*"        → Anthropic (nativo).
- model_id tipo "proveedor/modelo" (deepseek/deepseek-chat, openai/gpt-4o-mini, google/gemini-*,
  anthropic/claude-*, etc.) → OpenRouter (API compatible con OpenAI).

Diseño DORMIDO: si NO hay OPENROUTER_API_KEY, o el model_id no es de OpenRouter, se usa Anthropic —
o sea el comportamiento actual no cambia hasta que se configure la key y se asigne un modelo de
OpenRouter a un agente. Fallback: si piden un modelo de OpenRouter pero no hay key, se cae al
ANTHROPIC_FALLBACK (por defecto Haiku) para no romper el flujo.

La respuesta se devuelve con la MISMA forma que usa el código: .content[0].text y
.usage.input_tokens / .usage.output_tokens (para la instrumentación de tokens).
"""

import os
import logging

import httpx
import anthropic

log = logging.getLogger("llm")

_anthropic = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_FALLBACK = os.environ.get("ANTHROPIC_FALLBACK", "claude-haiku-4-5-20251001")


def is_openrouter(model_id: str) -> bool:
    """Los modelos de OpenRouter llevan 'proveedor/modelo' (deepseek/…, openai/…, google/…)."""
    return "/" in (model_id or "")


class _Usage:
    def __init__(self, i: int, o: int):
        self.input_tokens = i
        self.output_tokens = o


class _Block:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _Resp:
    """Shim con la misma forma que una respuesta de Anthropic (para el código que lee
    response.content[0].text y response.usage)."""
    def __init__(self, text: str, usage: _Usage):
        self.content = [_Block(text)]
        self.usage = usage
        self.stop_reason = "end_turn"


def _openrouter(model_id, messages, system, max_tokens, temperature, timeout) -> _Resp:
    msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
    r = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
        json={"model": model_id, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature},
        timeout=timeout,
    )
    r.raise_for_status()
    d = r.json()
    text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    u = d.get("usage", {}) or {}
    return _Resp(text, _Usage(int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0)))


def complete(model_id: str, messages: list, system: str = "", max_tokens: int = 1024,
             temperature: float = 0.3, timeout: int = 40):
    """Llamada de TEXTO enrutada por model_id. Devuelve un objeto con .content[0].text y .usage.
    OpenRouter si el modelo es 'prov/modelo' Y hay key; si no, Anthropic (comportamiento actual)."""
    if is_openrouter(model_id):
        if OPENROUTER_KEY:
            return _openrouter(model_id, messages, system, max_tokens, temperature, timeout)
        # Pidieron OpenRouter pero no hay key → fallback a Anthropic para no romper.
        log.warning(f"Modelo OpenRouter '{model_id}' pedido sin OPENROUTER_API_KEY → fallback {ANTHROPIC_FALLBACK}")
        model_id = ANTHROPIC_FALLBACK
    kwargs: dict = {"model": model_id, "max_tokens": max_tokens, "temperature": temperature, "messages": messages}
    if system:
        kwargs["system"] = system
    if timeout:
        kwargs["timeout"] = timeout
    return _anthropic.messages.create(**kwargs)
