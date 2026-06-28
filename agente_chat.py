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
    """Busca productos por modelo o marca en Supabase, luego verifica su existencia real en 1CRM
    usando el crm_product_id. Así confirma que el producto está tanto en Supabase como en el CRM."""
    q = query.strip()
    candidatos = []
    try:
        for field in ("modelo", "marca"):
            resp = supabase.table("rfqs").select(
                "marca,modelo,estado,crm_url,crm_product_id"
            ).eq("estado", "publicado").ilike(field, f"%{q}%").limit(limite).execute()
            for r in (resp.data or []):
                pid = r.get("crm_product_id")
                if pid and not any(x["crm_product_id"] == pid for x in candidatos):
                    candidatos.append(r)
    except Exception as e:
        return {"error": str(e), "total": 0, "resultados": []}

    # Verificar en 1CRM por ID y traer detalles completos del producto
    resultados = []
    for r in candidatos:
        pid = r.get("crm_product_id")
        try:
            crm_data = _onecrm_get(f"data/Product/{pid}")
            rec = crm_data.get("record", {})
            en_crm = bool(rec.get("id"))
        except Exception:
            rec = {}
            en_crm = False
        resultados.append({
            "marca":        r.get("marca", ""),
            "modelo":       r.get("modelo", ""),
            "en_crm":       en_crm,
            "crm_url":      r.get("crm_url", "") if en_crm else None,
            "nombre_crm":   rec.get("name", ""),
            "descripcion":  (rec.get("description") or "")[:300],
            "precio":       rec.get("list_price"),
            "moneda":       rec.get("currency_id", "USD"),
            "disponible":   rec.get("is_available"),
            "tiene_imagen": bool(rec.get("image_url")),
            "imagen_url":   rec.get("image_url") or rec.get("thumbnail_url"),
        })

    return {"total": len(resultados), "resultados": resultados}


def tool_ver_producto_crm(producto_id: str) -> dict:
    """Obtiene el detalle completo de un producto del catálogo 1CRM por su ID."""
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    data = _onecrm_get(f"data/Product/{producto_id}")
    record = data.get("record", data)
    if "error" in data and not record:
        return data
    url_crm = f"{ONECRM_BASE}/index.php?module=ProductCatalog&action=DetailView&record={producto_id}"
    return {
        "id":           record.get("id", producto_id),
        "nombre":       record.get("name", ""),
        "num_parte":    record.get("manufacturers_part_no", ""),
        "descripcion":  record.get("description", ""),
        "precio":       record.get("unit_price"),
        "moneda":       record.get("currency_id", "USD"),
        "categoria":    record.get("category", ""),
        "marca":        record.get("mft_suggested_retail_price", ""),
        "imagen_url":   record.get("picture", ""),
        "url_crm":      url_crm,
        "raw":          {k: v for k, v in record.items() if v and k not in ("id",)},
    }


def tool_listar_productos_crm(categoria: str = "", limite: int = 15) -> dict:
    """Lista productos del catálogo 1CRM con filtro opcional de categoría."""
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    params: dict = {
        "fields": "id,name,manufacturers_part_no,unit_price,currency_id,category,picture",
        "limit":  min(limite, 50),
        "order_by": "date_modified desc",
    }
    if categoria:
        params["filter_text"] = categoria
    data = _onecrm_get("data/Product", params)
    records = data.get("records", [])
    return {
        "total": data.get("total_results", len(records)),
        "productos": [
            {
                "id":        r.get("id"),
                "nombre":    r.get("name", ""),
                "num_parte": r.get("manufacturers_part_no", ""),
                "precio":    r.get("list_price"),
                "moneda":    r.get("currency_id", "USD"),
                "categoria": r.get("category", ""),
                "tiene_imagen": bool(r.get("image_url")),
                "url_crm": f"{ONECRM_BASE}/index.php?module=ProductCatalog&action=DetailView&record={r.get('id')}",
            }
            for r in records
        ],
    }


def tool_buscar_clientes_crm(query: str = "", limite: int = 10) -> dict:
    """Busca cuentas/clientes en 1CRM."""
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    params: dict = {
        "fields":   "id,name,phone_office,website,billing_address_country,account_type,industry",
        "max_num":  min(limite, 100),
    }
    if query:
        params["filter_text"] = query
    data = _onecrm_get("data/Account", params)
    records = data.get("records", [])
    return {
        "total": data.get("total_count", len(records)),
        "cuentas": [
            {
                "id":       r.get("id"),
                "nombre":   r.get("name", ""),
                "tipo":     r.get("account_type", ""),
                "industria": r.get("industry", ""),
                "telefono": r.get("phone_office", ""),
                "web":      r.get("website", ""),
                "pais":     r.get("billing_address_country", ""),
                "url_crm":  f"{ONECRM_BASE}/index.php?module=Accounts&record={r.get('id')}",
            }
            for r in records
        ],
    }


def tool_ver_cliente_crm(cliente_id: str) -> dict:
    """Obtiene el detalle completo de una cuenta/cliente en 1CRM."""
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    data = _onecrm_get(f"data/Account/{cliente_id}")
    record = data.get("record", data)
    return {
        "id":        record.get("id", cliente_id),
        "nombre":    record.get("name", ""),
        "tipo":      record.get("account_type", ""),
        "industria": record.get("industry", ""),
        "telefono":  record.get("phone_office", ""),
        "email":     record.get("email1", ""),
        "web":       record.get("website", ""),
        "direccion": {
            "calle":   record.get("billing_address_street", ""),
            "ciudad":  record.get("billing_address_city", ""),
            "estado":  record.get("billing_address_state", ""),
            "pais":    record.get("billing_address_country", ""),
        },
        "descripcion": record.get("description", ""),
        "url_crm":  f"{ONECRM_BASE}/index.php?module=Accounts&record={cliente_id}",
    }


def tool_listar_cotizaciones_crm(estado: str = "", cliente_id: str = "", limite: int = 10) -> dict:
    """Lista cotizaciones/quotes de 1CRM con filtros opcionales."""
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    params: dict = {
        "fields": "id,name,quote_stage,grand_total,currency_id,date_quote_expected_closed,billing_account_name",
        "limit":  min(limite, 30),
        "order_by": "date_modified desc",
    }
    if estado:
        params["filters[quote_stage]"] = estado
    if cliente_id:
        params["filters[billing_account_id]"] = cliente_id
    data = _onecrm_get("data/Quotes", params)
    records = data.get("records", [])
    return {
        "total": data.get("total_count", len(records)),
        "cotizaciones": [
            {
                "id":       r.get("id"),
                "nombre":   r.get("name", ""),
                "estado":   r.get("quote_stage", ""),
                "total":    r.get("grand_total"),
                "moneda":   r.get("currency_id", "USD"),
                "cliente":  r.get("billing_account_name", ""),
                "cierre":   r.get("date_quote_expected_closed", ""),
                "url_crm":  f"{ONECRM_BASE}/index.php?module=Quotes&record={r.get('id')}",
            }
            for r in records
        ],
    }


def tool_buscar_proveedores_crm(nombre: str = "", categoria: str = "") -> dict:
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    params: dict = {
        "filters[account_type]": "Supplier",
        "fields":  "id,name,phone_office,website,billing_address_country",
        "max_num": 30,
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


def _get_all_contacts_with_detail() -> list:
    """Obtiene todos los contactos del CRM con detalle completo. El API de 1CRM no filtra bien
    por primary_account_id via search_params, así que traemos todo y filtramos localmente."""
    records = _onecrm_get("data/Contact", {"max_num": 200}).get("records", [])
    result = []
    for r in records:
        det = _onecrm_get(f"data/Contact/{r['id']}").get("record", {})
        result.append({
            "id":        r["id"],
            "nombre":    f"{det.get('first_name', '')} {det.get('last_name', '')}".strip() or r.get("name", ""),
            "email":     det.get("email1", "") or det.get("email2", ""),
            "telefono":  det.get("phone_work", "") or det.get("phone_mobile", ""),
            "cargo":     det.get("title", ""),
            "ciudad":    det.get("primary_address_city", ""),
            "cuenta_id": det.get("primary_account_id", ""),
            "url_crm":   f"{ONECRM_BASE}/index.php?module=Contacts&action=DetailView&record={r['id']}",
        })
    return result


def tool_buscar_contactos_crm(nombre: str = "", cuenta_id: str = "") -> dict:
    """Busca contactos en 1CRM por nombre o por cuenta."""
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    todos = _get_all_contacts_with_detail()
    if cuenta_id:
        todos = [c for c in todos if c["cuenta_id"] == cuenta_id]
    if nombre:
        q = nombre.lower()
        todos = [c for c in todos if q in c["nombre"].lower() or q in c["email"].lower()]
    return {"total": len(todos), "contactos": todos}


def tool_ver_contactos_cuenta_crm(cuenta_id: str) -> dict:
    """Devuelve todos los contactos de una cuenta/cliente específica en 1CRM."""
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    cuenta = _onecrm_get(f"data/Account/{cuenta_id}").get("record", {})
    nombre_cuenta = cuenta.get("name", cuenta_id)
    todos = _get_all_contacts_with_detail()
    contactos = [c for c in todos if c["cuenta_id"] == cuenta_id]
    return {
        "cuenta":    nombre_cuenta,
        "cuenta_id": cuenta_id,
        "total":     len(contactos),
        "contactos": contactos,
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


def tool_obtener_opciones_rfq(rfq_id: str) -> dict:
    """Lee las opciones de proveedor de la tabla opciones para un RFQ dado."""
    try:
        resp = supabase.table("opciones").select(
            "id,rank,proveedor,precio_orig,moneda,disponibilidad,score_ranking,fuente"
        ).eq("rfq_id", rfq_id).order("rank").execute()
        rfq = supabase.table("rfqs").select("marca,modelo,estado").eq("id", rfq_id).single().execute().data
        return {
            "rfq_id": rfq_id,
            "marca":  rfq.get("marca", ""),
            "modelo": rfq.get("modelo", ""),
            "estado": rfq.get("estado", ""),
            "opciones": resp.data or [],
        }
    except Exception as e:
        return {"error": str(e), "opciones": []}


def tool_seleccionar_proveedor(rfq_id: str, opcion_id: str) -> dict:
    """Guarda el proveedor seleccionado y crea job de imagen para el RFQ."""
    try:
        opcion = supabase.table("opciones").select("*").eq("id", opcion_id).single().execute().data
        supabase.table("rfqs").update({
            "estado":     "procesando_imagen",
            "proveedor":  opcion.get("proveedor", ""),
        }).eq("id", rfq_id).execute()
        job = supabase.table("jobs").insert({
            "rfq_id":     rfq_id,
            "agente":     "imagen",
            "estado":     "pendiente",
            "created_at": datetime.utcnow().isoformat(),
        }).execute()
        job_id = (job.data or [{}])[0].get("id", "?")
        log.info(f"Job imagen creado (HITL): {job_id} para rfq {rfq_id}")
        return {"ok": True, "job_imagen": job_id, "proveedor": opcion.get("proveedor")}
    except Exception as e:
        return {"error": str(e)}


def tool_obtener_foto_rfq(rfq_id: str) -> dict:
    """Obtiene la URL de la foto del RFQ (para mostrar preview al usuario)."""
    try:
        rfq = supabase.table("rfqs").select("foto_url,estado,marca,modelo").eq("id", rfq_id).single().execute().data
        return {
            "rfq_id":   rfq_id,
            "foto_url": rfq.get("foto_url"),
            "estado":   rfq.get("estado"),
            "marca":    rfq.get("marca"),
            "modelo":   rfq.get("modelo"),
        }
    except Exception as e:
        return {"error": str(e)}


def tool_publicar_rfq(rfq_id: str) -> dict:
    """Aprueba la publicación del RFQ: crea job de publicador en 1CRM."""
    try:
        supabase.table("rfqs").update({"estado": "publicando"}).eq("id", rfq_id).execute()
        job = supabase.table("jobs").insert({
            "rfq_id":     rfq_id,
            "agente":     "publicador",
            "estado":     "pendiente",
            "created_at": datetime.utcnow().isoformat(),
        }).execute()
        job_id = (job.data or [{}])[0].get("id", "?")
        log.info(f"Job publicador creado (HITL aprobado): {job_id} para rfq {rfq_id}")
        return {"ok": True, "job_publicador": job_id}
    except Exception as e:
        return {"error": str(e)}


def tool_publicar_sin_imagen_rfq(rfq_id: str) -> dict:
    """Publica el RFQ sin imagen cuando el usuario lo aprueba así."""
    try:
        supabase.table("rfqs").update({
            "estado":   "publicando",
            "foto_url": None,
        }).eq("id", rfq_id).execute()
        job = supabase.table("jobs").insert({
            "rfq_id":     rfq_id,
            "agente":     "publicador",
            "estado":     "pendiente",
            "created_at": datetime.utcnow().isoformat(),
        }).execute()
        job_id = (job.data or [{}])[0].get("id", "?")
        return {"ok": True, "job_publicador": job_id}
    except Exception as e:
        return {"error": str(e)}


def tool_verificar_lista_productos(modelos: list) -> dict:
    """Verifica si una lista de modelos/partes está publicada en el CRM."""
    resultados = []
    for modelo in modelos:
        r = tool_buscar_productos_crm(str(modelo), limite=1)
        if r.get("total", 0) > 0:
            item = r["resultados"][0]
            resultados.append({
                "modelo":       modelo,
                "encontrado":   item.get("en_crm", False),
                "nombre_crm":   item.get("nombre_crm", ""),
                "precio":       item.get("precio"),
                "moneda":       item.get("moneda", "USD"),
                "crm_url":      item.get("crm_url"),
                "tiene_imagen": item.get("tiene_imagen", False),
            })
        else:
            resultados.append({"modelo": modelo, "encontrado": False, "nombre_crm": "", "precio": None, "crm_url": None})
    publicados = sum(1 for r in resultados if r["encontrado"])
    return {"total": len(resultados), "publicados": publicados, "no_encontrados": len(resultados) - publicados, "resultados": resultados}


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

    if not stream_id or stream_id in ("None", ""):
        log.error(f"stream_id inválido en crear_rfqs_desde_texto: {stream_id!r}")
        return {"error": f"stream_id inválido: {stream_id!r}. El sistema no pudo inyectarlo correctamente."}

    bulk_id  = str(uuid.uuid4())
    rfq_ids  = []
    creados  = []
    errores  = []

    for p in productos:
        modelo      = p.get("modelo",      "").strip()
        marca       = p.get("marca",       "").strip()
        descripcion = p.get("descripcion", "").strip()
        if not modelo:
            continue
        try:
            now    = datetime.now(timezone.utc)
            suffix = str(uuid.uuid4())[:6].upper()
            rfq_id_str = f"RFQ-{now.year}-{now.month:02d}{now.day:02d}-{suffix}"
            rfq_row: dict = {
                "stream_id": stream_id,
                "rfq_id":    rfq_id_str,
                "modelo":    modelo,
                "marca":     marca,
                "estado":    "recibido",
                "urgente":   urgente,
                "bulk_id":   bulk_id,
            }
            rfq_resp = supabase.table("rfqs").insert(rfq_row).execute()
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
            errores.append({"modelo": modelo, "error": str(e)})

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

    result: dict = {
        "bulk_id":  bulk_id,
        "creados":  len(creados),
        "rfq_ids":  rfq_ids,
        "productos": creados,
    }
    if errores:
        result["errores"] = errores
    return result


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
        "name": "ver_producto_crm",
        "description": "Obtiene el detalle completo de un producto del catálogo 1CRM: descripción, precio, imagen, link directo al CRM.",
        "input_schema": {
            "type": "object",
            "properties": {
                "producto_id": {"type": "string", "description": "ID del producto en 1CRM"},
            },
            "required": ["producto_id"],
        },
    },
    {
        "name": "listar_productos_crm",
        "description": "Lista los productos más recientes del catálogo 1CRM con filtro opcional de categoría. Útil para explorar el catálogo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "categoria": {"type": "string", "description": "Filtro de categoría (opcional)"},
                "limite":    {"type": "integer", "default": 15},
            },
        },
    },
    {
        "name": "buscar_clientes_crm",
        "description": "Busca cuentas o clientes en 1CRM por nombre, empresa o industria.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":  {"type": "string", "description": "Nombre o término a buscar"},
                "limite": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "ver_cliente_crm",
        "description": "Obtiene el detalle completo de una cuenta/cliente en 1CRM: contacto, dirección, industria.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cliente_id": {"type": "string", "description": "ID de la cuenta en 1CRM"},
            },
            "required": ["cliente_id"],
        },
    },
    {
        "name": "listar_cotizaciones_crm",
        "description": "Lista cotizaciones (quotes) de 1CRM. Puede filtrar por estado o cliente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "estado":     {"type": "string", "description": "Estado: Draft, Delivered, On Hold, etc."},
                "cliente_id": {"type": "string", "description": "ID de cuenta para filtrar por cliente"},
                "limite":     {"type": "integer", "default": 10},
            },
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
        "name": "buscar_contactos_crm",
        "description": "Busca contactos en 1CRM por nombre. Si tienes el ID de una cuenta, pásalo para filtrar solo sus contactos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre":    {"type": "string", "description": "Nombre del contacto a buscar"},
                "cuenta_id": {"type": "string", "description": "ID de la cuenta para filtrar contactos de ese cliente"},
            },
        },
    },
    {
        "name": "ver_contactos_cuenta_crm",
        "description": "Devuelve todos los contactos de una cuenta/cliente específica en 1CRM. Usa el cuenta_id obtenido de buscar_clientes_crm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cuenta_id": {"type": "string", "description": "ID de la cuenta en 1CRM"},
            },
            "required": ["cuenta_id"],
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
        "name": "obtener_opciones_rfq",
        "description": "Obtiene las opciones de proveedores encontradas por el buscador para un RFQ. Úsalo cuando el sistema avise que la búsqueda completó (trigger busqueda_completa).",
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_id": {"type": "string", "description": "UUID del RFQ"},
            },
            "required": ["rfq_id"],
        },
    },
    {
        "name": "seleccionar_proveedor",
        "description": "Guarda el proveedor seleccionado por el usuario y lanza la búsqueda de imagen. Llama este tool después de que el usuario elija un proveedor de la tabla de opciones.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_id":   {"type": "string", "description": "UUID del RFQ"},
                "opcion_id": {"type": "string", "description": "UUID de la opción seleccionada"},
            },
            "required": ["rfq_id", "opcion_id"],
        },
    },
    {
        "name": "obtener_foto_rfq",
        "description": "Obtiene la URL de la imagen procesada del RFQ para mostrar preview al usuario. Úsalo cuando el sistema avise que la imagen está lista (trigger imagen_lista).",
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_id": {"type": "string", "description": "UUID del RFQ"},
            },
            "required": ["rfq_id"],
        },
    },
    {
        "name": "publicar_rfq",
        "description": "Aprueba y publica el producto en 1CRM. Úsalo cuando el usuario apruebe la imagen.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_id": {"type": "string", "description": "UUID del RFQ"},
            },
            "required": ["rfq_id"],
        },
    },
    {
        "name": "publicar_sin_imagen_rfq",
        "description": "Publica el producto en 1CRM sin imagen cuando el usuario lo aprueba así (imagen no encontrada o rechazada).",
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_id": {"type": "string", "description": "UUID del RFQ"},
            },
            "required": ["rfq_id"],
        },
    },
    {
        "name": "verificar_lista_productos",
        "description": (
            "Verifica si una lista de modelos/números de parte está publicada en el catálogo 1CRM. "
            "Úsala cuando el usuario suba un archivo (XLS, documento) y quiera saber cuáles están publicados, "
            "o cuando pida 'verificar', 'cotejar', '¿cuáles están en el CRM?' sobre una lista de productos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "modelos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista de modelos o números de parte a verificar",
                },
            },
            "required": ["modelos"],
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
                            "modelo":      {"type": "string", "description": "Número de parte / modelo exacto"},
                            "marca":       {"type": "string", "description": "Fabricante / marca (vacío si no está claro)"},
                            "descripcion": {"type": "string", "description": "Descripción breve del producto (ej: 'Outer Piston', 'Circuit Breaker 20A'). Extrae del texto del usuario si está disponible."},
                        },
                        "required": ["modelo"],
                    },
                },
                "stream_id": {
                    "type": "string",
                    "description": "NO incluyas este campo en tu tool call. El sistema lo inyecta solo.",
                },
                "urgente": {
                    "type": "boolean",
                    "description": "True si el usuario indica urgencia",
                    "default": False,
                },
            },
            "required": ["productos"],
        },
    },
]

TOOL_FUNCTIONS = {
    "buscar_productos_crm":      tool_buscar_productos_crm,
    "ver_producto_crm":          tool_ver_producto_crm,
    "listar_productos_crm":      tool_listar_productos_crm,
    "buscar_proveedores_crm":    tool_buscar_proveedores_crm,
    "buscar_clientes_crm":       tool_buscar_clientes_crm,
    "buscar_contactos_crm":      tool_buscar_contactos_crm,
    "ver_contactos_cuenta_crm":  tool_ver_contactos_cuenta_crm,
    "ver_cliente_crm":           tool_ver_cliente_crm,
    "listar_cotizaciones_crm":   tool_listar_cotizaciones_crm,
    "consultar_rfqs":            tool_consultar_rfqs,
    "consultar_metricas":        tool_consultar_metricas,
    "buscar_internet":           tool_buscar_internet,
    "verificar_lista_productos":  tool_verificar_lista_productos,
    "crear_rfqs_desde_texto":    tool_crear_rfqs_desde_texto,
    "obtener_opciones_rfq":      tool_obtener_opciones_rfq,
    "seleccionar_proveedor":     tool_seleccionar_proveedor,
    "obtener_foto_rfq":          tool_obtener_foto_rfq,
    "publicar_rfq":              tool_publicar_rfq,
    "publicar_sin_imagen_rfq":   tool_publicar_sin_imagen_rfq,
}

SYSTEM_PROMPT = """\
Eres el asistente de Brain MRO Master Pro. Ayudas al equipo a gestionar \
el catálogo de productos industriales, proveedores y solicitudes de cotización (RFQs).

Tienes cuatro modos de operación que detectas automáticamente:

MODO 1 — EXTRACCIÓN DE PARTE NUMBERS:
Si el usuario escribe o pega una lista de números de parte / modelos industriales \
(ej: "XA2EVB4LC", "1756-L61", "3RT2028-1AK60"), extrae TODOS los productos y \
llama a `crear_rfqs_desde_texto` con la lista completa. \
CRÍTICO: extrae ÚNICAMENTE los productos del ÚLTIMO mensaje del usuario. \
Ignora completamente los productos mencionados en mensajes anteriores del historial — \
esos ya fueron procesados. Nunca combines productos de mensajes distintos en un mismo lote. \
NO respondas nada después — el widget del sistema confirma automáticamente. \
Devuelve exactamente una cadena vacía como respuesta final.

MODO 2 — TRIGGER busqueda_completa:
Si recibes un mensaje que empieza con "[SISTEMA:busqueda_completa]", extrae el rfq_id \
y llama a `obtener_opciones_rfq`. Responde SOLO con la tabla markdown de opciones, \
sin texto introductorio ni recomendación. Formato exacto:

| # | Proveedor | Precio | Moneda | Disponibilidad |
|---|-----------|--------|--------|----------------|
| 1 | ... | ... | ... | ... |

Nada más. No agregues párrafos antes ni después de la tabla. El usuario ya ve el widget.

MODO 3 — TRIGGER imagen_lista:
Si recibes un mensaje que empieza con "[SISTEMA:imagen_lista]", extrae el rfq_id y la foto_url. \
Muestra el link de la imagen al usuario y pregunta: \
"Imagen lista para MARCA MODELO. ¿Apruebas? Responde 'sí' para publicar en 1CRM o 'no' para publicar sin imagen."

MODO 4 — TRIGGER imagen_no_encontrada:
Si recibes un mensaje que empieza con "[SISTEMA:imagen_no_encontrada]", extrae el rfq_id y el motivo. \
Informa al usuario que no se encontró imagen y pregunta si desea publicar sin imagen. \
Responde con el rfq_id para que pueda decidir.

MODO 5 — SELECCIÓN DE PROVEEDOR:
Si el usuario indica que quiere un proveedor específico (ej: "quiero el de Grainger", "el primero", \
"opción 2"), identifica el opcion_id del historial y llama a `seleccionar_proveedor`. \
Confirma: "Seleccioné [proveedor]. Buscando imagen, espera un momento..."

MODO 6 — APROBACIÓN DE IMAGEN:
Si el usuario responde "sí", "apruebo", "ok", "publicar" después de ver la imagen, \
llama a `publicar_rfq` con el rfq_id del contexto. \
Si responde "no", "sin imagen", llama a `publicar_sin_imagen_rfq`. \
Confirma: "Publicando en 1CRM... te aviso cuando esté listo."

MODO 8 — VERIFICACIÓN DE LISTA (cuando el usuario sube un archivo con productos):
Si el usuario manda un mensaje que incluye "[X productos extraídos de" o pide "verificar", "cotejar", \
"¿cuáles están en el CRM?" sobre una lista de productos, extrae los modelos y llama a \
`verificar_lista_productos` con todos ellos. Presenta el resultado como tabla:

| Modelo | Estado | Precio | Link |
|--------|--------|--------|------|
| modelo1 | ✅ Publicado | $X USD | [Ver CRM](...) |
| modelo2 | ❌ No encontrado | — | — |

Termina con: "X de Y productos están publicados en el CRM."

MODO 7 — CHAT CONVERSACIONAL:
Para preguntas o solicitudes de información, usa las herramientas disponibles \
(1CRM, RFQs, métricas, internet) para responder con datos reales.

Reglas:
- Responde siempre en español
- Sé conciso y directo
- Nunca inventes precios o disponibilidad — usa siempre las herramientas
- CRÍTICO: Si una búsqueda no devuelve resultados, di "no encontré resultados para X" — NUNCA afirmes que un producto "no existe" o "no está publicado" basándote solo en que la búsqueda no lo encontró. La ausencia de resultados NO es prueba de ausencia del producto.
- Para listas de productos, SIEMPRE usa crear_rfqs_desde_texto aunque sean 1 o 2 items
- Los mensajes [SISTEMA:...] son triggers automáticos del sistema, no del usuario. Procésalos silenciosamente y responde al usuario con el resultado.\
"""


# ─────────────────────────────────────────────────────────────
# LOOP DE CLAUDE CON TOOL_USE
# ─────────────────────────────────────────────────────────────
def run_chat(messages: list[dict], stream_id: str) -> tuple[str, list[str], bool, dict]:
    tools_used: list[str] = []
    rfqs_created = False
    current_messages = list(messages)
    total_input_tokens  = 0
    total_output_tokens = 0

    for _ in range(10):
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=current_messages,
        )

        if hasattr(response, "usage") and response.usage:
            total_input_tokens  += getattr(response.usage, "input_tokens",  0)
            total_output_tokens += getattr(response.usage, "output_tokens", 0)

        if response.stop_reason == "end_turn":
            text = next(
                (b.text for b in response.content if hasattr(b, "text")), ""
            )
            token_counts = {"tokens_input": total_input_tokens, "tokens_output": total_output_tokens}
            return text, tools_used, rfqs_created, token_counts

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
                if tool_name == "crear_rfqs_desde_texto" and not tool_input.get("stream_id"):
                    tool_input["stream_id"] = stream_id

                fn = TOOL_FUNCTIONS.get(tool_name)
                try:
                    result = fn(**tool_input) if fn else {"error": f"Tool '{tool_name}' no existe"}
                    if tool_name == "crear_rfqs_desde_texto" and result.get("creados", 0) > 0:
                        rfqs_created = True
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

    token_counts = {"tokens_input": total_input_tokens, "tokens_output": total_output_tokens}
    return "No pude completar la respuesta.", tools_used, rfqs_created, token_counts


# ─────────────────────────────────────────────────────────────
# PROCESADOR DE MENSAJES
# ─────────────────────────────────────────────────────────────
def procesar_mensaje(msg: dict) -> None:
    msg_id    = msg["id"]
    stream_id = msg.get("stream_id", "")
    contenido = msg.get("content", "")

    log.info(f"Chat msg {msg_id[:8]} | stream={stream_id!r} | '{contenido[:60]}...'")

    # Marcar como procesado
    supabase.table("mensajes").update({"procesado": True}).eq("id", msg_id).execute()

    # Los triggers [SISTEMA:...] los maneja el widget del frontend — no necesitan respuesta de Claude
    if contenido.startswith("[SISTEMA:"):
        log.info(f"Trigger sistema ignorado (widget lo maneja): {contenido[:60]}")
        return

    # Obtener historial del stream (últimos 10 mensajes, más recientes primero → revertir)
    historial = []
    try:
        hist_resp = (
            supabase.table("mensajes")
            .select("role, content, procesado")
            .eq("stream_id", stream_id)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        for r in reversed(hist_resp.data or []):
            if r["role"] not in ("user", "assistant"):
                continue
            if r["content"].startswith("[SISTEMA:"):
                continue
            # Reemplazar contenido de mensajes de usuario ya procesados para que
            # Claude no re-extraiga productos de turnos anteriores
            content = r["content"]
            if r["role"] == "user" and r.get("procesado") and content != contenido:
                content = "[mensaje anterior — ya procesado]"
            historial.append({"role": r["role"], "content": content})
    except Exception as e:
        log.warning(f"No se pudo cargar historial: {e}")
        historial = [{"role": "user", "content": contenido}]

    # Garantizar que el historial termina con el mensaje actual del usuario
    if not historial or historial[-1].get("content") != contenido:
        historial.append({"role": "user", "content": contenido})

    # Llamar a Claude
    token_counts = {"tokens_input": 0, "tokens_output": 0}
    try:
        respuesta, tools_used, rfqs_created, token_counts = run_chat(historial, stream_id=str(stream_id))
    except Exception as e:
        log.error(f"Error en Claude: {e}")
        respuesta    = f"Error procesando tu mensaje. Intenta de nuevo. ({str(e)[:80]})"
        tools_used   = []
        rfqs_created = False

    # Registrar job de chat con tokens para que agente_monitor los sume
    try:
        supabase.table("jobs").insert({
            "agente": "chat",
            "estado": "completado",
            "output": {
                **token_counts,
                "tokens_total": token_counts["tokens_input"] + token_counts["tokens_output"],
                "tools_used":   tools_used,
            },
        }).execute()
    except Exception as e:
        log.warning(f"No se pudo registrar job de chat: {e}")

    # Si se crearon RFQs exitosamente, el widget confirma visualmente — no insertar texto
    if rfqs_created:
        log.info("RFQs creados exitosamente — widget maneja UI, omitiendo respuesta")
        return

    # No insertar respuesta vacía
    if not respuesta.strip():
        log.info("Respuesta vacía — omitiendo inserción")
        return

    # Guardar respuesta del asistente
    log.info(f"Insertando respuesta | stream_id={str(stream_id)!r} | len={len(respuesta)}")
    insert_resp = supabase.table("mensajes").insert({
        "stream_id": stream_id if stream_id else None,
        "role":      "assistant",
        "content":   respuesta,
        "procesado": True,
        "metadata":  {"tools_used": tools_used},
    }).execute()
    if hasattr(insert_resp, 'error') and insert_resp.error:
        log.error(f"Error insertando respuesta: {insert_resp.error}")
    else:
        log.info(f"Respuesta enviada | tools={tools_used} | tokens={token_counts}")


# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────
def main() -> None:
    log.info("Agente Chat iniciado — escuchando tabla `mensajes`...")

    # Recuperar mensajes huérfanos (procesado=false de sesiones anteriores)
    try:
        supabase.table("mensajes").update({"procesado": True}).filter(
            "procesado", "is", "false"
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
                .filter("procesado", "is", "false")
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
