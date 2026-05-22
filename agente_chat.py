"""
AGENTE CHAT — Brain · MRO Master Pro
======================================
Endpoint conversacional: Claude con herramientas reales.
Corre en Railway como servicio independiente en el puerto 8000.

POST /chat
  Body: { "messages": [...], "stream_id": "uuid-opcional" }
  Response: { "response": "texto", "tools_used": [...] }

Herramientas disponibles para Claude:
  - buscar_productos_crm      → catálogo interno 1CRM
  - buscar_proveedores_crm    → proveedores en 1CRM
  - consultar_rfqs            → RFQs activos en Supabase
  - consultar_metricas        → estado de APIs y recursos
  - buscar_en_internet        → Google CSE / SerpAPI

Variables de entorno (las mismas del proyecto):
  ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
  ONECRM_URL, ONECRM_USERNAME, ONECRM_PASSWORD
  SERPAPI_KEY, GOOGLE_API_KEY, GOOGLE_CX
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agente_chat")

# ── Clientes ───────────────────────────────────────────────────────────────
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"],
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ONECRM_BASE = os.environ.get("ONECRM_URL", "").rstrip("/")

app = FastAPI(title="Brain Chat API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Modelos ────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str      # "user" | "assistant"
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    stream_id: str | None = None

class ChatResponse(BaseModel):
    response: str
    tools_used: list[str] = []


# ── Implementaciones de herramientas ───────────────────────────────────────

def _onecrm_get(endpoint: str, params: dict = {}) -> dict:
    user = os.environ.get("ONECRM_USERNAME", "")
    pwd  = os.environ.get("ONECRM_PASSWORD", "")
    try:
        resp = httpx.get(
            f"{ONECRM_BASE}/api.php/{endpoint}",
            auth=(user, pwd),
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e), "records": [], "total_count": 0}


def tool_buscar_productos_crm(query: str, limite: int = 10) -> dict:
    """Busca productos en el catálogo interno 1CRM."""
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado", "resultados": []}
    data = _onecrm_get("data/Product", {
        "search[name]": query,
        "fields": "id,name,description,unit_price,currency_id,manufacturers_part_no",
        "limit": min(limite, 20),
    })
    records = data.get("records", [])
    resultados = [
        {
            "id":          r.get("id"),
            "nombre":      r.get("name", ""),
            "num_parte":   r.get("manufacturers_part_no", ""),
            "descripcion": r.get("description", "")[:200],
            "precio":      r.get("unit_price"),
        }
        for r in records
    ]
    return {"total": data.get("total_count", len(records)), "resultados": resultados}


def tool_buscar_proveedores_crm(nombre: str = "", categoria: str = "") -> dict:
    """Busca proveedores (cuentas tipo Supplier) en 1CRM."""
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado", "resultados": []}
    params: dict = {
        "filters[account_type]": "Supplier",
        "fields": "id,name,phone_office,website,billing_address_country,description",
        "limit": 15,
    }
    if nombre:
        params["search[name]"] = nombre
    data = _onecrm_get("data/Account", params)
    records = data.get("records", [])
    resultados = [
        {
            "id":       r.get("id"),
            "nombre":   r.get("name", ""),
            "telefono": r.get("phone_office", ""),
            "web":      r.get("website", ""),
            "pais":     r.get("billing_address_country", ""),
        }
        for r in records
    ]
    return {"total": data.get("total_count", len(records)), "resultados": resultados}


def tool_consultar_rfqs(estado: str = "", limite: int = 10) -> dict:
    """Consulta RFQs activos en Supabase. Estado puede ser: pendiente, buscando, foto_lista, publicado, etc."""
    try:
        q = supabase.table("rfqs").select(
            "id, rfq_id, marca, modelo, qty, estado, urgente, created_at"
        ).order("created_at", desc=True).limit(min(limite, 30))
        if estado:
            q = q.eq("estado", estado)
        resp = q.execute()
        return {"total": len(resp.data), "rfqs": resp.data or []}
    except Exception as e:
        return {"error": str(e), "rfqs": []}


def tool_consultar_metricas() -> dict:
    """Devuelve el estado actual de todas las APIs y recursos del sistema."""
    try:
        resp = supabase.table("resource_status").select(
            "servicio, metrica, valor, valor_texto, unidad, limite, estado, actualizado_en"
        ).execute()
        por_servicio: dict[str, Any] = {}
        for row in (resp.data or []):
            srv = row["servicio"]
            if srv not in por_servicio:
                por_servicio[srv] = {}
            por_servicio[srv][row["metrica"]] = {
                "valor":   row.get("valor_texto") or row.get("valor"),
                "unidad":  row.get("unidad"),
                "limite":  row.get("limite"),
                "estado":  row.get("estado"),
            }
        return por_servicio
    except Exception as e:
        return {"error": str(e)}


def tool_buscar_internet(query: str) -> dict:
    """Busca información en internet usando SerpAPI o Google CSE."""
    serpapi_key = os.environ.get("SERPAPI_KEY", "").strip()
    google_key  = os.environ.get("GOOGLE_API_KEY", "").strip()
    google_cx   = os.environ.get("GOOGLE_CX", "").strip()

    # Intentar SerpAPI primero
    if serpapi_key:
        try:
            resp = httpx.get(
                "https://serpapi.com/search",
                params={"q": query, "api_key": serpapi_key, "num": 5},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            resultados = [
                {"titulo": r.get("title"), "url": r.get("link"), "snippet": r.get("snippet", "")[:200]}
                for r in data.get("organic_results", [])[:5]
            ]
            return {"fuente": "serpapi", "resultados": resultados}
        except Exception as e:
            log.warning(f"SerpAPI falló: {e}")

    # Fallback a Google CSE
    if google_key and google_cx:
        try:
            resp = httpx.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": google_key, "cx": google_cx, "q": query, "num": 5},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            resultados = [
                {"titulo": r.get("title"), "url": r.get("link"), "snippet": r.get("snippet", "")[:200]}
                for r in data.get("items", [])[:5]
            ]
            return {"fuente": "google_cse", "resultados": resultados}
        except Exception as e:
            log.warning(f"Google CSE falló: {e}")

    return {"error": "Sin APIs de búsqueda disponibles o sin créditos"}


# ── Definición de tools para Claude ───────────────────────────────────────
TOOLS: list[dict] = [
    {
        "name": "buscar_productos_crm",
        "description": "Busca productos en el catálogo interno de 1CRM por nombre, número de parte o descripción.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Término de búsqueda (nombre, número de parte, marca)"},
                "limite": {"type": "integer", "description": "Máximo de resultados (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "buscar_proveedores_crm",
        "description": "Busca proveedores registrados en 1CRM. Puede filtrar por nombre o categoría.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre":    {"type": "string", "description": "Nombre o parte del nombre del proveedor"},
                "categoria": {"type": "string", "description": "Categoría o rubro del proveedor"},
            },
        },
    },
    {
        "name": "consultar_rfqs",
        "description": "Consulta las solicitudes de cotización (RFQs) activas en el sistema. Puede filtrar por estado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "estado": {
                    "type": "string",
                    "description": "Estado del RFQ: pendiente, buscando, foto_lista, foto_pendiente, publicado, etc. Dejar vacío para todos.",
                },
                "limite": {"type": "integer", "description": "Máximo de resultados (default 10)", "default": 10},
            },
        },
    },
    {
        "name": "consultar_metricas",
        "description": "Muestra el estado actual de todas las APIs y recursos del sistema (SerpAPI, Remove.bg, Anthropic, Railway, etc.).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "buscar_internet",
        "description": "Busca información en internet sobre productos, precios, proveedores o cualquier tema relevante.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Término de búsqueda"},
            },
            "required": ["query"],
        },
    },
]

TOOL_FUNCTIONS = {
    "buscar_productos_crm":   tool_buscar_productos_crm,
    "buscar_proveedores_crm": tool_buscar_proveedores_crm,
    "consultar_rfqs":         tool_consultar_rfqs,
    "consultar_metricas":     tool_consultar_metricas,
    "buscar_internet":        tool_buscar_internet,
}

SYSTEM_PROMPT = """Eres el asistente de Brain MRO Master Pro. Ayudas al equipo a gestionar el catálogo de productos industriales, proveedores y solicitudes de cotización (RFQs).

Tienes acceso a:
- El catálogo interno de productos en 1CRM
- La base de datos de proveedores en 1CRM
- Las solicitudes de cotización activas en el sistema
- Las métricas y estado de las APIs del sistema
- Búsqueda en internet para información externa

Responde siempre en español. Sé conciso y directo. Cuando busques información, usa las herramientas disponibles antes de responder."""


# ── Loop de tool_use ───────────────────────────────────────────────────────
def run_chat(messages: list[dict]) -> tuple[str, list[str]]:
    """Ejecuta el loop de Claude con tool_use. Devuelve (respuesta_final, tools_usadas)."""
    tools_used: list[str] = []
    current_messages = list(messages)

    for _ in range(10):  # máximo 10 iteraciones para evitar loops infinitos
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=current_messages,
        )

        if response.stop_reason == "end_turn":
            text = next(
                (b.text for b in response.content if hasattr(b, "text")),
                "",
            )
            return text, tools_used

        if response.stop_reason == "tool_use":
            # Agregar respuesta de Claude al historial
            current_messages.append({
                "role": "assistant",
                "content": response.content,
            })

            # Ejecutar cada tool y recopilar resultados
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_name = block.name
                tool_input = block.input
                tools_used.append(tool_name)

                log.info(f"Tool call: {tool_name}({json.dumps(tool_input)[:100]})")

                fn = TOOL_FUNCTIONS.get(tool_name)
                if fn:
                    try:
                        result = fn(**tool_input)
                    except Exception as e:
                        result = {"error": str(e)}
                else:
                    result = {"error": f"Tool '{tool_name}' no implementada"}

                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     json.dumps(result, ensure_ascii=False),
                })

            current_messages.append({"role": "user", "content": tool_results})

        else:
            # stop_reason inesperado
            break

    return "No pude completar la respuesta.", tools_used


# ── Endpoints ──────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages no puede estar vacío")

    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    log.info(f"Chat request: {len(messages)} mensajes | stream_id={req.stream_id}")

    response_text, tools_used = run_chat(messages)

    # Guardar en stream_logs si hay stream_id
    if req.stream_id:
        try:
            last_user = next(
                (m["content"] for m in reversed(messages) if m["role"] == "user"),
                "",
            )
            supabase.table("stream_logs").insert([
                {
                    "stream_id": req.stream_id,
                    "role":      "user",
                    "content":   last_user,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                {
                    "stream_id": req.stream_id,
                    "role":      "assistant",
                    "content":   response_text,
                    "metadata":  {"tools_used": tools_used},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            ]).execute()
        except Exception as e:
            log.warning(f"No se pudo guardar en stream_logs: {e}")

    return ChatResponse(response=response_text, tools_used=tools_used)


# ── Entry point ────────────────────────────────────────────────────────────
def main():
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    log.info(f"Agente Chat iniciado en puerto {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
