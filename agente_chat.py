"""
AGENTE CHAT — Brain · MRO Master Pro
======================================
Worker que corre en Railway (dentro de main.py).

Lee mensajes de la tabla `mensajes` donde procesado=false y role='user',
los procesa con Claude (tool_use) y escribe la respuesta de vuelta.

Capacidades:
  A. Chat conversacional: responde preguntas sobre 1CRM, RFQs, métricas, internet
  B. Extracción de números de parte: detecta cuando el usuario pega una lista
     de part-numbers y crea RFQs automáticamente (como el extractor de imagen)

Tabla Supabase requerida (correr una vez):
  CREATE TABLE IF NOT EXISTS mensajes (
      id          uuid DEFAULT gen_random_uuid() PRIMARY KEY,
      stream_id   uuid,
      role        text NOT NULL CHECK (role IN ('user','assistant','system')),
      content     text NOT NULL,
      metadata    jsonb DEFAULT '{}',
      procesado   boolean DEFAULT false,
      created_at  timestamptz DEFAULT now()
  );
  ALTER TABLE mensajes ENABLE ROW LEVEL SECURITY;
  CREATE POLICY "lectura publica mensajes"
      ON mensajes FOR SELECT USING (true);
  CREATE POLICY "insert anon mensajes"
      ON mensajes FOR INSERT WITH CHECK (true);
  CREATE POLICY "update service_role mensajes"
      ON mensajes FOR UPDATE USING (auth.role() = 'service_role');

Variables de entorno (las mismas del proyecto):
  ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
  ONECRM_URL, ONECRM_USERNAME, ONECRM_PASSWORD
  SERPAPI_KEY, GOOGLE_API_KEY, GOOGLE_CX
"""

import os
import re
import json
import time
import uuid
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
import anthropic
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agente_chat")

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"],
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ONECRM_BASE   = os.environ.get("ONECRM_URL", "").rstrip("/")
POLL_INTERVAL = 5   # segundos — respuesta rápida en el chat


# ─────────────────────────────────────────────────────────────
# HERRAMIENTAS — IMPLEMENTACIONES
# ─────────────────────────────────────────────────────────────

def _onecrm_get(endpoint: str, params: dict = {}) -> dict:
    user = os.environ.get("ONECRM_USERNAME", "")
    pwd  = os.environ.get("ONECRM_PASSWORD",  "")
    try:
        resp = httpx.get(
            f"{ONECRM_BASE}/api.php/{endpoint}",
            auth=(user, pwd), params=params, timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e), "records": [], "total_count": 0}


def tool_buscar_productos_crm(query: str, limite: int = 10) -> dict:
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    data = _onecrm_get("data/Product", {
        "filter_text": query,
        "fields": "id,name,description,unit_price,currency_id,manufacturers_part_no",
        "limit": min(limite, 20),
    })
    records = data.get("records", [])
    return {
        "total": data.get("total_count", len(records)),
        "resultados": [
            {
                "nombre":      r.get("name", ""),
                "num_parte":   r.get("manufacturers_part_no", ""),
                "descripcion": (r.get("description") or "")[:200],
                "precio":      r.get("unit_price"),
            }
            for r in records
        ],
    }


def tool_buscar_proveedores_crm(nombre: str = "", categoria: str = "") -> dict:
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    params: dict = {
        "filters[account_type]": "Supplier",
        "fields": "id,name,phone_office,website,billing_address_country",
        "limit": 15,
    }
    if nombre:
        params["filter_text"] = nombre
    data = _onecrm_get("data/Account", params)
    records = data.get("records", [])
    return {
        "total": data.get("total_count", len(records)),
        "resultados": [
            {
                "nombre":   r.get("name", ""),
                "telefono": r.get("phone_office", ""),
                "web":      r.get("website", ""),
                "pais":     r.get("billing_address_country", ""),
            }
            for r in records
        ],
    }


def tool_consultar_rfqs(estado: str = "", limite: int = 10) -> dict:
    try:
        q = supabase.table("rfqs").select(
            "id, rfq_id, marca, modelo, estado, urgente, created_at"
        ).order("created_at", desc=True).limit(min(limite, 30))
        if estado:
            q = q.eq("estado", estado)
        resp = q.execute()
        return {"total": len(resp.data), "rfqs": resp.data or []}
    except Exception as e:
        return {"error": str(e), "rfqs": []}


def tool_consultar_metricas() -> dict:
    try:
        resp = supabase.table("resource_status").select(
            "servicio,metrica,valor,valor_texto,unidad,limite,estado"
        ).execute()
        resultado: dict[str, Any] = {}
        for row in (resp.data or []):
            srv = row["servicio"]
            if srv not in resultado:
                resultado[srv] = {}
            resultado[srv][row["metrica"]] = {
                "valor":  row.get("valor_texto") or row.get("valor"),
                "unidad": row.get("unidad"),
                "limite": row.get("limite"),
                "estado": row.get("estado"),
            }
        return resultado
    except Exception as e:
        return {"error": str(e)}


def tool_buscar_internet(query: str) -> dict:
    serpapi_key = os.environ.get("SERPAPI_KEY", "").strip()
    google_key  = os.environ.get("GOOGLE_API_KEY", "").strip()
    google_cx   = os.environ.get("GOOGLE_CX", "").strip()

    if serpapi_key:
        try:
            resp = httpx.get(
                "https://serpapi.com/search",
                params={"q": query, "api_key": serpapi_key, "num": 5},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("organic_results", [])[:5]
            return {
                "fuente": "serpapi",
                "resultados": [
                    {"titulo": r.get("title"), "url": r.get("link"),
                     "snippet": (r.get("snippet") or "")[:200]}
                    for r in items
                ],
            }
        except Exception as e:
            log.warning(f"SerpAPI falló: {e}")

    if google_key and google_cx:
        try:
            resp = httpx.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": google_key, "cx": google_cx, "q": query, "num": 5},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])[:5]
            return {
                "fuente": "google_cse",
                "resultados": [
                    {"titulo": r.get("title"), "url": r.get("link"),
                     "snippet": (r.get("snippet") or "")[:200]}
                    for r in items
                ],
            }
        except Exception as e:
            log.warning(f"Google CSE falló: {e}")

    return {"error": "Sin APIs de búsqueda disponibles"}


def tool_crear_rfqs_desde_texto(
    productos: list[dict],
    stream_id: str,
    urgente: bool = False,
) -> dict:
    """
    Recibe una lista de productos extraídos del texto del usuario y crea
    un RFQ + job buscador por cada uno. Agrupa con bulk_id.

    productos: [{"modelo": "XA2EVB4LC", "marca": "Schneider"}, ...]
    """
    if not productos:
        return {"error": "Lista de productos vacía"}

    bulk_id  = str(uuid.uuid4())
    rfq_ids  = []
    creados  = []

    for p in productos:
        modelo = p.get("modelo", "").strip()
        marca  = p.get("marca",  "").strip()
        if not modelo:
            continue
        try:
            rfq_resp = supabase.table("rfqs").insert({
                "stream_id": stream_id,
                "modelo":    modelo,
                "marca":     marca,
                "estado":    "recibido",
                "urgente":   urgente,
                "bulk_id":   bulk_id,
            }).execute()
            rfq_id = rfq_resp.data[0]["id"]
            rfq_ids.append(rfq_id)

            supabase.table("jobs").insert({
                "rfq_id": rfq_id,
                "agente": "buscador",
                "estado": "pendiente",
            }).execute()

            creados.append({"rfq_id": rfq_id, "modelo": modelo, "marca": marca})
            log.info(f"  → RFQ {rfq_id}: {modelo} | {marca}")
        except Exception as e:
            log.error(f"Error creando RFQ para {modelo}: {e}")

    if rfq_ids:
        lista_txt = "\n".join(
            f"• {p['modelo']}" + (f"  [{p['marca']}]" if p.get("marca") else "")
            for p in creados
        )
        try:
            supabase.table("notificaciones").insert({
                "rfq_id":    rfq_ids[0],
                "stream_id": stream_id,
                "tipo":      "bulk",
                "titulo":    f"📋 {len(creados)} productos desde chat — búsqueda iniciada",
                "mensaje":   json.dumps({
                    "bulk_id": bulk_id,
                    "lista":   lista_txt,
                    "total":   len(creados),
                }),
                "leida": False,
            }).execute()
        except Exception as e:
            log.warning(f"No se pudo enviar notificación bulk: {e}")

    return {
        "bulk_id":  bulk_id,
        "creados":  len(creados),
        "rfq_ids":  rfq_ids,
        "productos": creados,
    }


# ─────────────────────────────────────────────────────────────
# DEFINICIÓN DE TOOLS PARA CLAUDE
# ─────────────────────────────────────────────────────────────
TOOLS: list[dict] = [
    {
        "name": "buscar_productos_crm",
        "description": "Busca productos en el catálogo interno de 1CRM por nombre, número de parte o descripción.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":  {"type": "string", "description": "Término de búsqueda"},
                "limite": {"type": "integer", "description": "Máximo resultados (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "buscar_proveedores_crm",
        "description": "Busca proveedores registrados en 1CRM.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre":    {"type": "string", "description": "Nombre del proveedor"},
                "categoria": {"type": "string", "description": "Categoría o rubro"},
            },
        },
    },
    {
        "name": "consultar_rfqs",
        "description": "Consulta RFQs activos. Estado puede ser: recibido, buscando, busqueda_completa, foto_lista, publicado, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "estado": {"type": "string", "description": "Filtro de estado (vacío = todos)"},
                "limite": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "consultar_metricas",
        "description": "Devuelve el estado actual de todas las APIs y recursos (SerpAPI, Remove.bg, Anthropic, Railway, 1CRM, etc.).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "buscar_internet",
        "description": "Busca información en internet sobre productos, precios o proveedores.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Término de búsqueda"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "crear_rfqs_desde_texto",
        "description": (
            "Crea RFQs a partir de una lista de productos extraídos del texto del usuario. "
            "Usa esta herramienta cuando el usuario pegue o escriba una lista de números de parte / modelos. "
            "Extrae modelo y marca de cada línea y llama a esta herramienta con la lista completa."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "productos": {
                    "type": "array",
                    "description": "Lista de productos extraídos",
                    "items": {
                        "type": "object",
                        "properties": {
                            "modelo": {"type": "string", "description": "Número de parte / modelo exacto"},
                            "marca":  {"type": "string", "description": "Fabricante / marca (vacío si no está claro)"},
                        },
                        "required": ["modelo"],
                    },
                },
                "stream_id": {
                    "type": "string",
                    "description": "UUID del stream donde se crearán los RFQs",
                },
                "urgente": {
                    "type": "boolean",
                    "description": "True si el usuario indica urgencia",
                    "default": False,
                },
            },
            "required": ["productos", "stream_id"],
        },
    },
]

TOOL_FUNCTIONS = {
    "buscar_productos_crm":   tool_buscar_productos_crm,
    "buscar_proveedores_crm": tool_buscar_proveedores_crm,
    "consultar_rfqs":         tool_consultar_rfqs,
    "consultar_metricas":     tool_consultar_metricas,
    "buscar_internet":        tool_buscar_internet,
    "crear_rfqs_desde_texto": tool_crear_rfqs_desde_texto,
}

SYSTEM_PROMPT = """\
Eres el asistente de Brain MRO Master Pro. Ayudas al equipo a gestionar \
el catálogo de productos industriales, proveedores y solicitudes de cotización (RFQs).

Tienes dos modos de operación que detectas automáticamente:

MODO 1 — EXTRACCIÓN DE PARTE NUMBERS:
Si el usuario escribe o pega una lista de números de parte / modelos industriales \
(ej: "XA2EVB4LC", "1756-L61", "3RT2028-1AK60"), extrae TODOS los productos y \
llama a `crear_rfqs_desde_texto` con la lista completa. \
Después confirma cuántos RFQs se crearon y que la búsqueda de proveedores está en curso.

MODO 2 — CHAT CONVERSACIONAL:
Si el usuario hace una pregunta o solicita información, usa las herramientas \
disponibles (1CRM, RFQs, métricas, internet) para responder con datos reales.

Reglas:
- Responde siempre en español
- Sé conciso y directo
- Si ves números de parte mezclados con una pregunta, extrae los part numbers Y responde la pregunta
- Nunca inventes precios o disponibilidad — usa siempre las herramientas
- Para listas de productos, SIEMPRE usa crear_rfqs_desde_texto aunque sean 1 o 2 items\
"""


# ─────────────────────────────────────────────────────────────
# LOOP DE CLAUDE CON TOOL_USE
# ─────────────────────────────────────────────────────────────
def run_chat(messages: list[dict], stream_id: str) -> tuple[str, list[str]]:
    tools_used: list[str] = []
    current_messages = list(messages)

    for _ in range(10):
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=current_messages,
        )

        if response.stop_reason == "end_turn":
            text = next(
                (b.text for b in response.content if hasattr(b, "text")), ""
            )
            return text, tools_used

        if response.stop_reason == "tool_use":
            current_messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_name  = block.name
                tool_input = block.input
                tools_used.append(tool_name)
                log.info(f"Tool: {tool_name}({json.dumps(tool_input)[:120]})")

                # Inyectar stream_id automáticamente en crear_rfqs_desde_texto
                if tool_name == "crear_rfqs_desde_texto" and "stream_id" not in tool_input:
                    tool_input["stream_id"] = stream_id

                fn = TOOL_FUNCTIONS.get(tool_name)
                try:
                    result = fn(**tool_input) if fn else {"error": f"Tool '{tool_name}' no existe"}
                except Exception as e:
                    result = {"error": str(e)}

                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     json.dumps(result, ensure_ascii=False),
                })

            current_messages.append({"role": "user", "content": tool_results})
        else:
            break

    return "No pude completar la respuesta.", tools_used


# ─────────────────────────────────────────────────────────────
# PROCESADOR DE MENSAJES
# ─────────────────────────────────────────────────────────────
def procesar_mensaje(msg: dict) -> None:
    msg_id    = msg["id"]
    stream_id = msg.get("stream_id", "")
    contenido = msg.get("content", "")

    log.info(f"Chat msg {msg_id[:8]} | stream={str(stream_id)[:8]} | '{contenido[:60]}...'")

    # Marcar como procesando
    supabase.table("mensajes").update({"procesado": True}).eq("id", msg_id).execute()

    # Obtener historial del stream (últimos 10 mensajes)
    historial = []
    try:
        hist_resp = (
            supabase.table("mensajes")
            .select("role, content")
            .eq("stream_id", stream_id)
            .order("created_at", desc=False)
            .limit(10)
            .execute()
        )
        historial = [
            {"role": r["role"], "content": r["content"]}
            for r in (hist_resp.data or [])
            if r["role"] in ("user", "assistant")
        ]
    except Exception as e:
        log.warning(f"No se pudo cargar historial: {e}")
        historial = [{"role": "user", "content": contenido}]

    # Llamar a Claude
    try:
        respuesta, tools_used = run_chat(historial, stream_id=str(stream_id))
    except Exception as e:
        log.error(f"Error en Claude: {e}")
        respuesta  = f"Error procesando tu mensaje. Intenta de nuevo. ({str(e)[:80]})"
        tools_used = []

    # Guardar respuesta del asistente
    supabase.table("mensajes").insert({
        "stream_id": stream_id,
        "role":      "assistant",
        "content":   respuesta,
        "procesado": True,
        "metadata":  {"tools_used": tools_used},
    }).execute()

    log.info(f"Respuesta enviada | tools={tools_used}")


# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────
def main() -> None:
    log.info("Agente Chat iniciado — escuchando tabla `mensajes`...")

    # Recuperar mensajes huérfanos (procesado=false de sesiones anteriores)
    try:
        supabase.table("mensajes").update({"procesado": True}).eq(
            "procesado", False
        ).eq("role", "user").lt(
            "created_at",
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ).execute()
    except Exception:
        pass

    while True:
        try:
            resp = (
                supabase.table("mensajes")
                .select("*")
                .eq("role", "user")
                .eq("procesado", False)
                .order("created_at")
                .limit(1)
                .execute()
            )
            if resp.data:
                procesar_mensaje(resp.data[0])
        except Exception as e:
            log.error(f"Error en loop chat: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
