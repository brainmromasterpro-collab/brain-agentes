"""
CONFIG AGENTES — Brain · MRO Master Pro
========================================
Lee la configuración de cada agente desde la tabla `agents` de Supabase.
Cachea el resultado 5 minutos para no golpear la BD en cada job.

Uso:
    from config_agentes import get_config

    cfg = get_config("buscador")
    # cfg = {
    #   "model_id":      "claude-sonnet-4-6",
    #   "max_tokens":    4096,
    #   "temperature":   0.3,
    #   "system_prompt": "Eres un agente...",
    #   "timeout":       30,
    # }
"""

import os
import time
import logging
from typing import TypedDict

from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("config_agentes")

_supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"],
)

# ── Mapeo de nombres de modelo UI → ID real de la API ──────────────────────
_MODEL_MAP: dict[str, str] = {
    "claude sonnet 4":             "claude-sonnet-4-6",
    "claude sonnet 4.6":           "claude-sonnet-4-6",
    "claude haiku 4":              "claude-haiku-4-5-20251001",
    "claude haiku 4.5":            "claude-haiku-4-5-20251001",
    "claude opus 4":               "claude-opus-4-7",
    "claude opus 4.7":             "claude-opus-4-7",
    # IDs directos (por si la UI los guarda así)
    "claude-sonnet-4-6":           "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001":   "claude-haiku-4-5-20251001",
    "claude-opus-4-7":             "claude-opus-4-7",
}

_DEFAULTS: dict[str, object] = {
    # Haiku por defecto para los workers (rankear/elegir imagen/generar ficha): ~10x más barato
    # que Sonnet y suficiente para estas tareas estructuradas. Reversible: volver a sonnet-4-6.
    "model_id":      "claude-haiku-4-5-20251001",
    "max_tokens":    4096,
    "temperature":   0.3,
    "system_prompt": "",
    "timeout":       30,
}

# ── Caché en memoria ────────────────────────────────────────────────────────
_CACHE: dict[str, dict] = {}
_CACHE_TS: dict[str, float] = {}
_TTL = 300  # 5 minutos


class AgentConfig(TypedDict):
    model_id: str
    max_tokens: int
    temperature: float
    system_prompt: str
    timeout: int


def _resolve_model(nombre_ui: str | None) -> str:
    if not nombre_ui:
        return str(_DEFAULTS["model_id"])
    key = nombre_ui.strip().lower()
    return _MODEL_MAP.get(key, str(_DEFAULTS["model_id"]))


def get_config(nombre_agente: str) -> AgentConfig:
    """
    Devuelve la configuración del agente desde Supabase.
    Si el agente no existe en la tabla o hay error, retorna los valores por defecto.
    El resultado se cachea 5 minutos.
    """
    now = time.monotonic()
    if nombre_agente in _CACHE and (now - _CACHE_TS.get(nombre_agente, 0)) < _TTL:
        return _CACHE[nombre_agente]  # type: ignore[return-value]

    try:
        resp = _supabase.table("agents") \
            .select("nombre, modelo, max_tokens, temperatura, system_prompt, timeout") \
            .eq("nombre", nombre_agente) \
            .limit(1) \
            .execute()

        row = (resp.data or [None])[0]
        if not row:
            log.warning(f"Agente '{nombre_agente}' no encontrado en Supabase — usando defaults")
            cfg = dict(_DEFAULTS)  # type: ignore[arg-type]
        else:
            cfg = {
                "model_id":      _resolve_model(row.get("modelo")),
                "max_tokens":    int(row.get("max_tokens")  or _DEFAULTS["max_tokens"]),
                "temperature":   float(row.get("temperatura") or _DEFAULTS["temperature"]),
                "system_prompt": (row.get("system_prompt") or "").strip(),
                "timeout":       int(row.get("timeout") or _DEFAULTS["timeout"]),
            }
            log.info(
                f"Config cargada para '{nombre_agente}': "
                f"model={cfg['model_id']} max_tokens={cfg['max_tokens']} "
                f"temp={cfg['temperature']}"
            )
    except Exception as e:
        log.error(f"Error leyendo config de '{nombre_agente}': {e} — usando defaults")
        cfg = dict(_DEFAULTS)  # type: ignore[arg-type]

    _CACHE[nombre_agente] = cfg  # type: ignore[assignment]
    _CACHE_TS[nombre_agente] = now
    return cfg  # type: ignore[return-value]


def invalidar_cache(nombre_agente: str | None = None) -> None:
    """Invalida la caché de un agente específico o de todos si se omite."""
    if nombre_agente:
        _CACHE.pop(nombre_agente, None)
        _CACHE_TS.pop(nombre_agente, None)
    else:
        _CACHE.clear()
        _CACHE_TS.clear()
