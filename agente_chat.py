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

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
GMAIL_USER           = "brain.mromasterpro@gmail.com"


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


def _gmail_access_token() -> str:
    """Obtiene un access token fresco usando el refresh token de Google."""
    resp = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id":     os.environ.get("GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID),
        "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET),
        "refresh_token": os.environ.get("GOOGLE_REFRESH_TOKEN", GOOGLE_REFRESH_TOKEN),
        "grant_type":    "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise ValueError(f"Google OAuth error: {data}")
    return data["access_token"]


def _gmail_decode_body(payload: dict) -> str:
    """Extrae texto plano del payload de un mensaje de Gmail."""
    import base64

    def extract(part: dict) -> str:
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        for sub in part.get("parts", []):
            result = extract(sub)
            if result:
                return result
        return ""

    return extract(payload).strip()


def tool_leer_emails_gmail(max_emails: int = 10, query: str = "newer_than:1d") -> dict:
    """Lee emails de brain.mromasterpro@gmail.com.
    query soporta filtros de Gmail: newer_than:1d, from:alguien@mail.com, subject:tema, is:unread, etc.
    """
    refresh = os.environ.get("GOOGLE_REFRESH_TOKEN", GOOGLE_REFRESH_TOKEN)
    if not refresh:
        return {"error": "GOOGLE_REFRESH_TOKEN no configurado en Railway"}
    try:
        token = _gmail_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        base = "https://gmail.googleapis.com/gmail/v1"

        # Listar IDs
        params = {"userId": "me", "maxResults": min(max_emails, 20), "q": query}
        lista = httpx.get(f"{base}/users/me/messages", headers=headers, params=params, timeout=15).json()
        msgs = lista.get("messages", [])

        emails = []
        for m in msgs:
            detail = httpx.get(f"{base}/users/me/messages/{m['id']}",
                headers=headers, params={"userId": "me", "format": "full"}, timeout=15).json()
            hdrs = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            body = _gmail_decode_body(detail.get("payload", {}))
            emails.append({
                "id":      m["id"],
                "de":      hdrs.get("From", ""),
                "para":    hdrs.get("To", ""),
                "asunto":  hdrs.get("Subject", ""),
                "fecha":   hdrs.get("Date", ""),
                "snippet": detail.get("snippet", ""),
                "cuerpo":  body[:1500] if body else detail.get("snippet", ""),
                "leido":   "UNREAD" not in detail.get("labelIds", []),
            })
        return {"total": len(emails), "query": query, "emails": emails}
    except Exception as e:
        return {"error": str(e)}


def tool_buscar_email_gmail(query: str, max_emails: int = 5) -> dict:
    """Busca emails en Gmail con cualquier query: from:, subject:, has:attachment, etc."""
    return tool_leer_emails_gmail(max_emails=max_emails, query=query)


def tool_enviar_email_gmail(para: str, asunto: str, cuerpo: str, thread_id: str = "", message_id: str = "") -> dict:
    """Envía o responde un email desde brain.mromasterpro@gmail.com.
    Si se pasa thread_id y message_id, responde en el hilo existente (Reply).
    Si no, envía un email nuevo.
    """
    import base64
    from email.mime.text import MIMEText

    refresh = os.environ.get("GOOGLE_REFRESH_TOKEN", GOOGLE_REFRESH_TOKEN)
    if not refresh:
        return {"error": "GOOGLE_REFRESH_TOKEN no configurado en Railway"}
    try:
        token = _gmail_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        msg = MIMEText(cuerpo, "plain", "utf-8")
        msg["To"]      = para
        msg["From"]    = GMAIL_USER
        msg["Subject"] = asunto
        if message_id:
            msg["In-Reply-To"] = message_id
            msg["References"]  = message_id

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body: dict = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id

        resp = httpx.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers=headers,  # incluye Content-Type: application/json (sin esto Gmail da 400 INVALID_ARGUMENT)
            content=json.dumps(body).encode(),
            timeout=15,
        )
        if not resp.is_success:
            return {
                "error":        f"Gmail API {resp.status_code}",
                "detalle":      resp.text[:500],
                "token_prefix": token[:20],
            }
        data = resp.json()
        return {
            "ok":         True,
            "message_id": data.get("id"),
            "thread_id":  data.get("threadId"),
            "enviado_a":  para,
            "asunto":     asunto,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_diagnostico_gmail() -> dict:
    """Diagnóstica el estado de la conexión Gmail — muestra si las variables están configuradas."""
    client_id  = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_sec = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh    = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    status = {
        "GOOGLE_CLIENT_ID":     "✅ configurado" if client_id  else "❌ falta",
        "GOOGLE_CLIENT_SECRET": "✅ configurado" if client_sec else "❌ falta",
        "GOOGLE_REFRESH_TOKEN": "✅ configurado" if refresh    else "❌ falta",
    }
    if client_id and client_sec and refresh:
        try:
            _gmail_access_token()
            status["conexion"] = "✅ token de acceso obtenido correctamente"
        except Exception as e:
            status["conexion"] = f"❌ error al obtener token: {e}"
    else:
        status["conexion"] = "❌ variables incompletas"
    return status


def _lookup_contact_by_email(email_addr: str) -> dict:
    """Busca si un email pertenece a un contacto o cuenta en 1CRM. Máximo 3 llamadas HTTP."""
    result: dict = {"contacto": None, "cuenta": None}
    if not email_addr or not ONECRM_BASE:
        return result

    # 1 llamada: buscar contacto por email con fields necesarios
    params = [
        ("max_num", 3), ("filter_text", email_addr),
        ("fields[]", "id"), ("fields[]", "first_name"), ("fields[]", "last_name"),
        ("fields[]", "title"), ("fields[]", "primary_account_id"),
    ]
    contactos = _onecrm_get("data/Contact", dict(params)).get("records", [])
    if contactos:
        c = contactos[0]
        result["contacto"] = {
            "id":        c.get("id"),
            "nombre":    f"{c.get('first_name','')} {c.get('last_name','')}".strip(),
            "cargo":     c.get("title", ""),
            "cuenta_id": c.get("primary_account_id", ""),
        }
        if c.get("primary_account_id"):
            # 1 llamada: obtener nombre de la cuenta
            acct = _onecrm_get(f"data/Account/{c['primary_account_id']}").get("record", {})
            result["cuenta"] = {"id": acct.get("id"), "nombre": acct.get("name", "")}

    # Si no encontramos contacto, buscar por dominio (1 llamada)
    if not result["cuenta"] and "@" in email_addr:
        domain = email_addr.split("@")[-1]
        if domain not in ("gmail.com", "hotmail.com", "yahoo.com", "outlook.com", "icloud.com"):
            keyword = domain.split(".")[0]
            cuentas = _onecrm_get("data/Account", {"max_num": 3, "filter_text": keyword}).get("records", [])
            if cuentas:
                result["cuenta"] = {"id": cuentas[0].get("id"), "nombre": cuentas[0].get("name", "")}
    return result


def _check_products_in_crm(texto: str) -> list:
    """Extrae posibles part-numbers del texto y verifica si están en el catálogo CRM."""
    import re
    patrones = re.findall(
        r'\b([A-Z0-9]{3,}[-/][A-Z0-9]{2,}(?:[-/][A-Z0-9]+)*|[A-Z]{1,4}\d{4,}[A-Z0-9-]*)\b',
        texto.upper()
    )
    resultados = []
    vistos: set = set()
    for pn in patrones[:5]:
        if pn in vistos:
            continue
        vistos.add(pn)
        data = _onecrm_get("data/AOS_Products_Quotes", {"max_num": 1, "filter_text": pn})
        en_crm = len(data.get("records", [])) > 0
        resultados.append({"part_number": pn, "en_catalogo_crm": en_crm})
    return resultados


def tool_escanear_emails_ventas(query: str = "newer_than:3d", max_emails: int = 10) -> dict:
    """Escanea emails de Gmail buscando oportunidades de venta (RFQs, solicitudes de cotización).
    Para cada email detecta: si el remitente es cliente en CRM, a qué cuenta pertenece,
    qué productos/part-numbers menciona y si están en el catálogo.
    """
    import re
    emails_raw = tool_leer_emails_gmail(max_emails=max_emails, query=query)
    if "error" in emails_raw:
        return emails_raw

    keywords_rfq = ["rfq", "cotización", "cotizacion", "precio", "presupuesto", "quote",
                    "disponibilidad", "solicitud", "necesitamos", "requerimos", "cuánto cuesta",
                    "availability", "request for", "lead time"]

    oportunidades = []
    for email in emails_raw.get("emails", []):
        de_raw = email.get("de", "")
        match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', de_raw)
        email_addr = match.group(0).lower() if match else ""

        texto_completo = (email.get("asunto", "") + " " + email.get("cuerpo", "")).lower()
        es_rfq = any(k in texto_completo for k in keywords_rfq)

        # Los lookups a 1CRM son caros (~0.5s c/u). Solo se corren en correos que SÍ son RFQ;
        # así el escaneo de una bandeja normal no dispara ~90 llamadas secuenciales al CRM.
        if es_rfq:
            crm_info = _lookup_contact_by_email(email_addr) if email_addr else {"contacto": None, "cuenta": None}
            texto_upper = email.get("asunto", "") + " " + email.get("cuerpo", "")
            productos = _check_products_in_crm(texto_upper)
        else:
            crm_info = {"contacto": None, "cuenta": None}
            productos = []

        cantidades = re.findall(r'\b(\d+)\s*(?:pz|pzas?|piezas?|unidades?|units?|qty|pcs?)\b', texto_completo)

        oportunidades.append({
            "email_id":              email.get("id"),
            "asunto":                email.get("asunto"),
            "de":                    de_raw,
            "email_remitente":       email_addr,
            "fecha":                 email.get("fecha"),
            "snippet":               email.get("snippet"),
            "es_rfq":                es_rfq,
            "cliente_crm":           crm_info["cuenta"],
            "contacto_crm":          crm_info["contacto"],
            "es_cliente_conocido":   crm_info["cuenta"] is not None,
            "productos_detectados":  productos,
            "cantidades_mencionadas": cantidades,
            "cuerpo_resumido":       email.get("cuerpo", "")[:800],
        })

    return {
        "total_emails":       len(oportunidades),
        "posibles_rfqs":      len([o for o in oportunidades if o["es_rfq"]]),
        "clientes_conocidos": len([o for o in oportunidades if o["es_cliente_conocido"]]),
        "oportunidades":      oportunidades,
    }


def tool_notificar_sistema(titulo: str, mensaje: str = "", tipo: str = "oportunidad",
                           stream_id: str = "") -> dict:
    """Publica una notificación en el sistema (campana/toast del dashboard).
    Úsala para avisar que hay oportunidades detectadas en el correo.
    """
    try:
        payload: dict = {"tipo": tipo, "titulo": titulo, "mensaje": mensaje or "", "leida": False}
        if stream_id:
            payload["stream_id"] = stream_id
        supabase.table("notificaciones").insert(payload).execute()
        return {"ok": True, "titulo": titulo}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _onecrm_post(endpoint: str, payload: dict) -> dict:
    user = os.environ.get("ONECRM_USERNAME", "")
    pwd  = os.environ.get("ONECRM_PASSWORD",  "")
    try:
        resp = httpx.post(
            f"{ONECRM_BASE}/api.php/{endpoint}",
            auth=(user, pwd), json={"data": payload}, timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


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
    params: dict = {"max_num": min(limite, 100)}
    if query:
        params["filter_text"] = query
    data = _onecrm_get("data/Account", params)
    records = data.get("records", [])
    return {
        "total": data.get("total_count", len(records)),
        "cuentas": [
            {
                "id":        r.get("id"),
                "nombre":    r.get("name", ""),
                "tipo":      r.get("account_type", ""),
                "industria": r.get("industry", ""),
                "email":     r.get("email1", "") or r.get("email2", ""),
                "telefono":  r.get("phone_office", "") or r.get("phone_alternate", ""),
                "tel_alt":   r.get("phone_alternate", "") if r.get("phone_office") else "",
                "web":       r.get("website", ""),
                "ciudad":    r.get("billing_address_city", ""),
                "pais":      r.get("billing_address_country", ""),
                "url_crm":   f"{ONECRM_BASE}/index.php?module=Accounts&record={r.get('id')}",
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
        "email":     record.get("email1", "") or record.get("email2", ""),
        "email2":    record.get("email2", "") if record.get("email1") else "",
        "telefono":  record.get("phone_office", "") or record.get("phone_alternate", ""),
        "tel_alt":   record.get("phone_alternate", ""),
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


def tool_listar_oportunidades_crm(estado: str = "", cuenta_id: str = "", limite: int = 10) -> dict:
    """Lista oportunidades/deals en 1CRM. estado puede ser: Prospecting, Qualification,
    Proposal/Price Quote, Negotiation, Closed Won, Closed Lost."""
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    params: dict = {"max_num": min(limite, 50), "order_by": "date_modified desc"}
    if estado:
        params["search_params[sales_stage][0]"] = estado
    if cuenta_id:
        params["search_params[account_id][0]"] = cuenta_id
    data = _onecrm_get("data/Opportunity", params)
    records = data.get("records", [])
    return {
        "total": len(records),
        "oportunidades": [
            {
                "id":        r.get("id"),
                "nombre":    r.get("name", ""),
                "etapa":     r.get("sales_stage", ""),
                "monto":     r.get("amount", ""),
                "moneda":    r.get("currency_id", ""),
                "cierre":    r.get("date_closed", ""),
                "cuenta":    r.get("account_name", ""),
                "probabilidad": r.get("probability", ""),
                "url_crm":   f"{ONECRM_BASE}/index.php?module=Opportunities&record={r.get('id')}",
            }
            for r in records
        ],
    }


def tool_crear_oportunidad_crm(
    nombre: str,
    cuenta_id: str,
    monto: float = 0,
    fecha_cierre: str = "",
    etapa: str = "Prospecting",
    descripcion: str = "",
) -> dict:
    """Crea una nueva oportunidad en 1CRM.
    etapa: Prospecting | Qualification | Proposal/Price Quote | Negotiation | Closed Won | Closed Lost
    fecha_cierre: formato YYYY-MM-DD
    """
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    # CANDADO DE INFO OBLIGATORIA: no se crea la oportunidad sin cuenta ni sin RFQ+cantidad.
    # La cantidad debe venir en la descripción (formato "RFQ: <parts> | Qty: <n>"). Si falta,
    # se rechaza para forzar al agente a pedir la información faltante antes de crear.
    import re as _re
    faltantes = []
    if not cuenta_id or not str(cuenta_id).strip():
        faltantes.append("cuenta")
    # Cantidad real: un token de cantidad (qty/cant/unidad/pieza/pz/pcs) seguido de número.
    # No basta con "hay un dígito" porque los part-numbers (ej. Q0120) ya traen dígitos.
    _tiene_qty = bool(descripcion) and _re.search(
        r'(?:(?:qty|cant)\w*\W*\d|\d\s*(?:unidad|pieza|\bpz|pcs|units?))',
        descripcion, _re.I)
    if not _tiene_qty:
        faltantes.append("RFQ + cantidad (Qty)")
    if faltantes:
        return {
            "error":     "INFO_INCOMPLETA",
            "faltantes": faltantes,
            "mensaje":   "No se puede crear la oportunidad: falta información obligatoria. "
                         "Pide al cliente los datos faltantes (con [DECISION] para enviar el correo) "
                         "en lugar de crear la oportunidad.",
        }
    import datetime
    if not fecha_cierre:
        fecha_cierre = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    payload = {
        "name":        nombre,
        "account_id":  cuenta_id,
        "amount":      monto,
        "date_closed": fecha_cierre,
        "sales_stage": etapa,
        "description": descripcion,
    }
    resp = _onecrm_post("data/Opportunity", payload)
    opp_id = resp.get("id", "")
    return {
        "ok":      bool(opp_id),
        "id":      opp_id,
        "nombre":  nombre,
        "url_crm": f"{ONECRM_BASE}/index.php?module=Opportunities&record={opp_id}" if opp_id else "",
    }


def tool_crear_cuenta_crm(
    nombre: str,
    tipo: str = "Customer",
    email: str = "",
    telefono: str = "",
    tel_alternativo: str = "",
    web: str = "",
    ciudad: str = "",
    estado: str = "",
    pais: str = "Mexico",
    descripcion: str = "",
    envio_calle: str = "",
    envio_ciudad: str = "",
    envio_estado: str = "",
    envio_cp: str = "",
    envio_pais: str = "",
) -> dict:
    """Crea una nueva cuenta/empresa en 1CRM.
    tipo: Customer | Supplier | Partner | Competitor | Press | Analyst | Other
    Los campos envio_* llenan la dirección de envío (shipping) de la cuenta.
    """
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    payload: dict = {"name": nombre, "account_type": tipo}
    if email:         payload["email1"] = email
    if telefono:      payload["phone_office"] = telefono
    if tel_alternativo: payload["phone_alternate"] = tel_alternativo
    if web:           payload["website"] = web
    if ciudad:        payload["billing_address_city"] = ciudad
    if estado:        payload["billing_address_state"] = estado
    if pais:          payload["billing_address_country"] = pais
    if descripcion:   payload["description"] = descripcion
    # Dirección de envío (shipping) — donde vive la "dirección de envío" del RFQ
    if envio_calle:   payload["shipping_address_street"] = envio_calle
    if envio_ciudad:  payload["shipping_address_city"] = envio_ciudad
    if envio_estado:  payload["shipping_address_state"] = envio_estado
    if envio_cp:      payload["shipping_address_postalcode"] = envio_cp
    if envio_pais:    payload["shipping_address_country"] = envio_pais
    resp = _onecrm_post("data/Account", payload)
    acct_id = resp.get("id", "")
    return {
        "ok":      bool(acct_id),
        "id":      acct_id,
        "nombre":  nombre,
        "url_crm": f"{ONECRM_BASE}/index.php?module=Accounts&record={acct_id}" if acct_id else "",
        "error":   resp.get("error", ""),
    }


def tool_crear_contacto_crm(
    nombre: str,
    apellido: str = "",
    cuenta_id: str = "",
    email: str = "",
    whatsapp: str = "",
    telefono: str = "",
    cargo: str = "",
    descripcion: str = "",
) -> dict:
    """Crea un contacto (persona) en 1CRM, opcionalmente ligado a una cuenta/empresa.
    whatsapp se guarda en phone_mobile (1CRM no tiene campo de WhatsApp dedicado).
    cuenta_id liga el contacto a su empresa mediante primary_account_id.
    """
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    payload: dict = {"first_name": nombre, "last_name": apellido or nombre}
    if cuenta_id:    payload["primary_account_id"] = cuenta_id
    if email:        payload["email1"] = email
    if whatsapp:     payload["phone_mobile"] = whatsapp
    if telefono:     payload["phone_work"] = telefono
    if cargo:        payload["title"] = cargo
    if descripcion:  payload["description"] = descripcion
    resp = _onecrm_post("data/Contact", payload)
    contacto_id = resp.get("id", "")
    return {
        "ok":         bool(contacto_id),
        "id":         contacto_id,
        "nombre":     f"{nombre} {apellido}".strip(),
        "cuenta_id":  cuenta_id,
        "url_crm":    f"{ONECRM_BASE}/index.php?module=Contacts&record={contacto_id}" if contacto_id else "",
        "error":      resp.get("error", ""),
    }


def tool_buscar_proveedores_crm(nombre: str = "", categoria: str = "") -> dict:
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    params: dict = {"max_num": 30}
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
    """Obtiene todos los contactos en UNA sola llamada usando fields[] para evitar N+1 queries."""
    params = [
        ("max_num", 200),
        ("fields[]", "id"), ("fields[]", "first_name"), ("fields[]", "last_name"),
        ("fields[]", "email1"), ("fields[]", "email2"), ("fields[]", "phone_work"),
        ("fields[]", "phone_mobile"), ("fields[]", "title"),
        ("fields[]", "primary_account_id"), ("fields[]", "primary_address_city"),
    ]
    records = _onecrm_get("data/Contact", dict(params)).get("records", [])
    return [
        {
            "id":        r["id"],
            "nombre":    f"{r.get('first_name', '')} {r.get('last_name', '')}".strip() or r.get("name", ""),
            "email":     r.get("email1", "") or r.get("email2", ""),
            "telefono":  r.get("phone_work", "") or r.get("phone_mobile", ""),
            "cargo":     r.get("title", ""),
            "ciudad":    r.get("primary_address_city", ""),
            "cuenta_id": r.get("primary_account_id", ""),
            "url_crm":   f"{ONECRM_BASE}/index.php?module=Contacts&action=DetailView&record={r['id']}",
        }
        for r in records
    ]


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
    # Incluir email/teléfono del Account si no hay Contact records separados
    info_cuenta: dict = {}
    acct_email = cuenta.get("email1", "") or cuenta.get("email2", "")
    acct_tel   = cuenta.get("phone_office", "") or cuenta.get("phone_alternate", "")
    acct_tel2  = cuenta.get("phone_alternate", "") if cuenta.get("phone_office") else ""
    if acct_email or acct_tel:
        info_cuenta = {
            "email":    acct_email,
            "telefono": acct_tel,
            "tel_alt":  acct_tel2,
            "web":      cuenta.get("website", ""),
            "nota":     "Datos de contacto registrados en la cuenta (sin contacto individual separado)" if not contactos else "",
        }
    return {
        "cuenta":      nombre_cuenta,
        "cuenta_id":   cuenta_id,
        "info_cuenta": info_cuenta,
        "total":       len(contactos),
        "contactos":   contactos,
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

    # stream_id puede ser None cuando el RFQ viene de un email — se guarda sin stream
    clean_stream_id: str | None = stream_id if (stream_id and stream_id not in ("None", "")) else None

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
                "stream_id": clean_stream_id,
                "rfq_id":    rfq_id_str,
                "modelo":    modelo,
                "marca":     marca,
                "estado":    "recibido",
                "urgente":   urgente,
                "bulk_id":   bulk_id,
            }
            try:
                rfq_resp = supabase.table("rfqs").insert(rfq_row).execute()
            except Exception:
                # stream_id no existe en tabla streams — reintenta sin stream
                rfq_row["stream_id"] = None
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
                "stream_id": clean_stream_id,
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
        "name": "enviar_email_gmail",
        "description": "Envía un email o responde en un hilo existente desde brain.mromasterpro@gmail.com. Usar cuando el usuario pide contestar, responder o enviar un correo a alguien.",
        "input_schema": {
            "type": "object",
            "properties": {
                "para":       {"type": "string", "description": "Email del destinatario"},
                "asunto":     {"type": "string", "description": "Asunto del email"},
                "cuerpo":     {"type": "string", "description": "Cuerpo del email en texto plano"},
                "thread_id":  {"type": "string", "description": "threadId del email original para responder en el mismo hilo (opcional)"},
                "message_id": {"type": "string", "description": "Message-ID del email original para el header In-Reply-To (opcional)"},
            },
            "required": ["para", "asunto", "cuerpo"],
        },
    },
    {
        "name": "diagnostico_gmail",
        "description": "Diagnostica si las variables de Gmail están configuradas correctamente en Railway. Usar cuando Gmail no responde o hay errores de conexión.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "leer_emails_gmail",
        "description": "Lee emails de brain.mromasterpro@gmail.com. Por defecto muestra los del día de hoy. Soporta filtros Gmail: newer_than:1d, from:alguien@mail.com, subject:tema, is:unread, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_emails": {"type": "integer", "description": "Número máximo de emails a traer (default 10, max 20)"},
                "query":      {"type": "string",  "description": "Filtro Gmail. Default: newer_than:1d (correos de hoy)"},
            },
        },
    },
    {
        "name": "buscar_email_gmail",
        "description": "Busca emails específicos en Gmail por remitente, asunto, adjuntos, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":      {"type": "string",  "description": "Query de búsqueda Gmail: from:, subject:, has:attachment, etc."},
                "max_emails": {"type": "integer", "description": "Máximo de resultados (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "escanear_emails_ventas",
        "description": "Escanea emails de Gmail de los últimos días buscando oportunidades de venta: RFQs, cotizaciones, solicitudes. Detecta si el remitente es cliente existente en CRM, qué productos menciona y si están en catálogo. Usar cuando el usuario pide revisar correos en busca de oportunidades o leads.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":      {"type": "string",  "description": "Filtro Gmail (default: newer_than:3d). Ejemplos: newer_than:7d, is:unread, from:cliente.com"},
                "max_emails": {"type": "integer", "description": "Máximo de emails a analizar (default 10, max 20)"},
            },
        },
    },
    {
        "name": "notificar_sistema",
        "description": "Publica una notificación en el sistema (campana/toast del dashboard) para avisar al equipo. Usar para avisar que se detectaron oportunidades en el correo. El stream_id se inyecta automáticamente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo":  {"type": "string", "description": "Título corto de la notificación"},
                "mensaje": {"type": "string", "description": "Detalle (opcional): resumen de las oportunidades detectadas"},
            },
            "required": ["titulo"],
        },
    },
    {
        "name": "crear_cuenta_crm",
        "description": "Crea una nueva cuenta/empresa en 1CRM (cliente, proveedor, socio, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre":          {"type": "string", "description": "Nombre de la empresa"},
                "tipo":            {"type": "string", "description": "Customer | Supplier | Partner | Other (default: Customer)"},
                "email":           {"type": "string"},
                "telefono":        {"type": "string"},
                "tel_alternativo": {"type": "string"},
                "web":             {"type": "string"},
                "ciudad":          {"type": "string"},
                "estado":          {"type": "string", "description": "Estado/provincia"},
                "pais":            {"type": "string", "description": "Default: Mexico"},
                "descripcion":     {"type": "string"},
                "envio_calle":     {"type": "string", "description": "Dirección de envío: calle y número"},
                "envio_ciudad":    {"type": "string", "description": "Dirección de envío: ciudad"},
                "envio_estado":    {"type": "string", "description": "Dirección de envío: estado/provincia"},
                "envio_cp":        {"type": "string", "description": "Dirección de envío: código postal"},
                "envio_pais":      {"type": "string", "description": "Dirección de envío: país"},
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "crear_contacto_crm",
        "description": "Crea un contacto (persona) en 1CRM ligado a una cuenta. Usar tras crear_cuenta_crm para registrar a la persona (nombre, correo, whatsapp) de la empresa.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre":      {"type": "string", "description": "Nombre(s) de la persona"},
                "apellido":    {"type": "string", "description": "Apellido(s)"},
                "cuenta_id":   {"type": "string", "description": "ID de la cuenta/empresa a la que se liga el contacto"},
                "email":       {"type": "string"},
                "whatsapp":    {"type": "string", "description": "Número de WhatsApp (se guarda en phone_mobile)"},
                "telefono":    {"type": "string", "description": "Teléfono de trabajo"},
                "cargo":       {"type": "string", "description": "Puesto/cargo"},
                "descripcion": {"type": "string"},
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "listar_oportunidades_crm",
        "description": "Lista oportunidades/deals en 1CRM. Puede filtrar por etapa o por cuenta.",
        "input_schema": {
            "type": "object",
            "properties": {
                "estado":    {"type": "string", "description": "Etapa: Prospecting, Qualification, Proposal/Price Quote, Negotiation, Closed Won, Closed Lost"},
                "cuenta_id": {"type": "string", "description": "ID de la cuenta para filtrar sus oportunidades"},
                "limite":    {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "crear_oportunidad_crm",
        "description": "Crea una nueva oportunidad/deal en 1CRM para un cliente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre":        {"type": "string",  "description": "Nombre descriptivo de la oportunidad"},
                "cuenta_id":     {"type": "string",  "description": "ID de la cuenta/cliente en 1CRM"},
                "monto":         {"type": "number",  "description": "Monto estimado de la oportunidad"},
                "fecha_cierre":  {"type": "string",  "description": "Fecha estimada de cierre YYYY-MM-DD"},
                "etapa":         {"type": "string",  "description": "Etapa inicial: Prospecting (default), Qualification, Proposal/Price Quote, etc."},
                "descripcion":   {"type": "string",  "description": "Descripción o contexto de la oportunidad"},
            },
            "required": ["nombre", "cuenta_id"],
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
    "enviar_email_gmail":        tool_enviar_email_gmail,
    "diagnostico_gmail":         tool_diagnostico_gmail,
    "leer_emails_gmail":         tool_leer_emails_gmail,
    "buscar_email_gmail":        tool_buscar_email_gmail,
    "escanear_emails_ventas":    tool_escanear_emails_ventas,
    "notificar_sistema":         tool_notificar_sistema,
    "crear_cuenta_crm":          tool_crear_cuenta_crm,
    "crear_contacto_crm":        tool_crear_contacto_crm,
    "listar_oportunidades_crm":  tool_listar_oportunidades_crm,
    "crear_oportunidad_crm":     tool_crear_oportunidad_crm,
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

MODO 9 — RFQ DESDE EMAIL:
Cuando el usuario pide "busca el RFQ del email", "procesa los RFQs del correo", o similar:
1. Usa leer_emails_gmail o buscar_email_gmail para encontrar el email con la solicitud de cotización.
2. Extrae TODOS los part-numbers / modelos del cuerpo del email.
3. Llama a crear_rfqs_desde_texto con esos productos — exactamente igual que si el usuario los hubiera pegado en el chat.
4. Si el usuario también pidió responderle al remitente, usa enviar_email_gmail para contestar en el mismo hilo (thread_id del email encontrado).
CRÍTICO: No preguntes si deben crearse los RFQs — créalos directamente. El usuario ya autorizó al pedir "busca el RFQ".

MODO 10 — GENERAR OPORTUNIDAD DESDE RFQ (correo):
Cuando el usuario pida "genera la oportunidad del correo", "arma la oportunidad de este RFQ", \
"da de alta esta solicitud", o similar sobre un email de cotización:

1. Lee el email con leer_emails_gmail / buscar_email_gmail (o usa escanear_emails_ventas para el cotejo con CRM).

2. Verifica los 4 BLOQUES OBLIGATORIOS para crear la oportunidad:
   - RFQ + Qty: al menos un part-number/modelo Y su cantidad de unidades.
   - Cuenta: nombre del cliente/empresa.
   - Contacto: nombre, correo y whatsapp de la persona.
   - Dirección de envío.

3. SI FALTA CUALQUIER BLOQUE → NO crees nada. Haz estas 3 cosas, en orden:

   (a) AVISA al usuario del chat que la oportunidad está INCOMPLETA y enumera exactamente qué bloque(s) faltan. \
       Ejemplo: "⚠️ La oportunidad de <remitente/cuenta> está incompleta. Faltan: cantidad (Qty) y dirección de envío."

   (b) REDACTA un correo de respuesta cortés (mismo hilo, thread_id del email) pidiendo ÚNICAMENTE los datos faltantes, \
       y muéstraselo al usuario del chat como borrador. \
       OBLIGATORIO — NUNCA lo omitas ni lo abrevies, aunque reformules el resto del correo: \
       el mensaje SIEMPRE debe incluir, ANTES de pedir los datos, una frase que confirme que YA se está \
       trabajando en su requerimiento y que muy pronto se le avisa. Es crítico para mantener el interés del prospecto. \
       IDIOMA: detecta el idioma del correo ORIGINAL del cliente y redacta la respuesta en ESE mismo idioma \
       (si el RFQ llegó en inglés, contesta en inglés; si en portugués, en portugués; etc.). \
       La plantilla de abajo está en español SOLO como modelo — tradúcela al idioma del cliente y conserva SIEMPRE \
       la frase de "ya estamos procesando su requerimiento y muy pronto le avisamos":

       "Estimado/a [nombre]:
       Gracias por su solicitud. Le confirmamos que ya estamos procesando su requerimiento y muy pronto le avisamos. \
       Mientras tanto, para poder completar su cotización, le agradeceríamos nos comparta la siguiente información:
       - [dato faltante 1]
       - [dato faltante 2]
       Quedamos atentos y con gusto avanzamos en cuanto la recibamos. Saludos."

   (c) Termina con [DECISION: ¿Envío esta solicitud de información a <remitente>?]. \
       El correo queda SUJETO A LA APROBACIÓN del usuario: solo tras el "Sí" llamas a enviar_email_gmail. \
       Nunca envíes sin aprobación, y nunca crees la oportunidad mientras falte información.

4. SI ESTÁN LOS 4 BLOQUES COMPLETOS → coteja con el CRM (escanear_emails_ventas o buscar_clientes_crm por el correo/dominio del remitente):

   4a. YA ES CLIENTE (la cuenta existe en CRM): termina con [DECISION: ¿Creo la oportunidad para <cuenta>?]. \
       Tras el "Sí": crea la oportunidad con crear_oportunidad_crm — pon en descripcion el detalle "RFQ: <part-numbers> | Qty: <cantidades>" \
       y usa el cuenta_id del CRM. Confirma con el link de la oportunidad creada.

   4b. NO ES CLIENTE (no hay cuenta en CRM): termina con [DECISION: ¿Doy de alta la cuenta + contacto y creo la oportunidad?]. \
       Tras el "Sí", en orden:
       (i)   crear_cuenta_crm con nombre, email, teléfono y los campos envio_* de la dirección de envío.
       (ii)  crear_contacto_crm ligado con cuenta_id (el id devuelto en i), con nombre, apellido, email y whatsapp.
       (iii) crear_oportunidad_crm con ese cuenta_id y descripcion "RFQ: <part-numbers> | Qty: <cantidades>".
       Confirma con los links de cuenta, contacto y oportunidad creados.

CRÍTICO: en este modo nunca creas nada en el CRM ni envías correos sin el [DECISION] aprobado por el usuario. \
Si faltan datos, primero se piden; solo con los 4 bloques completos se procede a cotejar y crear.

MODO 11 — REVISAR OPORTUNIDADES DEL CORREO (lote):
Cuando el usuario pida "lee los correos", "revisa el correo y detecta oportunidades", \
"escanea oportunidades", "qué oportunidades hay", o similar (varios correos a la vez):

1. Llama a escanear_emails_ventas. Toma solo los que tengan es_rfq = true; ésas son las oportunidades.

2. Para CADA oportunidad, evalúa los mismos 4 BLOQUES OBLIGATORIOS del MODO 10 \
   (RFQ+Qty, Cuenta, Contacto con nombre/correo/whatsapp, Dirección de envío) usando el contenido del correo \
   y el cotejo con CRM (es_cliente_conocido / cliente_crm). Anota qué falta en cada una.

3. Avisa en el sistema con notificar_sistema: titulo tipo "🔔 N oportunidades detectadas en el correo", \
   mensaje con un resumen breve (cuántas completas y cuántas incompletas).

4. Presenta al usuario un TRIAGE en tabla con TODAS las oportunidades:

   | # | Remitente | Cuenta CRM | Producto(s) | Qty | Falta para crear | Acción sugerida |
   |---|-----------|------------|-------------|-----|------------------|-----------------|
   | 1 | juan@x.com | ✅ Aceros del Norte | Q0120 | 20 | — (completa) | Crear oportunidad |
   | 2 | maria@y.com | ❌ No es cliente | ABC123 | ? | Contacto, Dirección, Qty | Pedir datos al cliente |

   Debajo resume: "X completas listas para crear, Y incompletas que requieren pedir datos."

5. Luego procesa UNA por una, empezando por la #1, aplicando el flujo del MODO 10:
   - Si está completa → [DECISION: ¿Creo la oportunidad para <cuenta>?] y al aprobar, créala (con contacto/cuenta si es cliente nuevo).
   - Si le falta info → muestra el borrador de correo pidiendo SOLO lo faltante y [DECISION: ¿Envío la solicitud a <remitente>?].
   Tras resolver una, continúa con la siguiente. Nunca crees ni envíes sin el [DECISION] aprobado.

Si no hay oportunidades (ningún es_rfq), dilo claramente y no notifiques nada.

MODO 7 — CHAT CONVERSACIONAL:
Para preguntas o solicitudes de información, usa las herramientas disponibles \
(1CRM, RFQs, métricas, internet) para responder con datos reales.

Reglas:
- Responde siempre en español
- Sé conciso y directo
- Nunca inventes precios o disponibilidad — usa siempre las herramientas
- CRÍTICO: Si una búsqueda no devuelve resultados, di "no encontré resultados para X" — NUNCA afirmes que un producto "no existe" o "no está publicado" basándote solo en que la búsqueda no lo encontró. La ausencia de resultados NO es prueba de ausencia del producto.
- OPORTUNIDADES — REGLA ABSOLUTA: NUNCA crees una oportunidad (crear_oportunidad_crm) si falta \
cualquiera de los 4 bloques obligatorios (RFQ+Qty, Cuenta, Contacto con whatsapp, Dirección de envío). \
Si falta algo, PRIMERO pides la información al cliente (borrador de correo + [DECISION]); solo con todo \
completo creas. Si la tool devuelve "INFO_INCOMPLETA", NO reintentes crear: pide lo faltante.
- Para listas de productos, SIEMPRE usa crear_rfqs_desde_texto aunque sean 1 o 2 items
- Los mensajes [SISTEMA:...] son triggers automáticos del sistema, no del usuario. Procésalos silenciosamente y responde al usuario con el resultado.
- CONTACTOS CRM: Cuando el usuario pregunte por el contacto de un cliente, primero usa buscar_clientes_crm para obtener el cuenta_id, luego usa ver_contactos_cuenta_crm. La respuesta incluye "info_cuenta" con el email y teléfono registrados en la cuenta — SIEMPRE muestra esos datos aunque no haya Contact records separados. "info_cuenta" con email o teléfono ES información de contacto válida.
- DECISIONES CON BOTONES: Cuando necesites aprobación del usuario para una acción importante (enviar un email, crear algo en CRM, etc.), termina tu mensaje con el tag [DECISION: pregunta corta aquí]. Ejemplo: "Le contestaré a Alejandro que tenemos el producto disponible. [DECISION: ¿Confirmas que lo enviamos?]". El sistema mostrará botones Sí/No automáticamente.
- IDIOMA DE CORREOS: la regla "responde en español" aplica al chat con el usuario, NO a los correos a clientes. \
Todo correo saliente (borrador o envío) debe ir en el MISMO idioma del correo original del cliente.
- EMAILS Y ACCIÓN: Cuando el usuario te pide explícitamente enviar o responder un email (ej: "contéstale", "dile que sí", "mándale cotización"), ACTÚA DIRECTAMENTE con enviar_email_gmail sin pedir confirmación adicional. El usuario ya dio la instrucción. Solo pide confirmación si hay ambigüedad sobre a QUIÉN enviar o si el contenido puede causar un compromiso comercial incorrecto que el usuario no mencionó.\
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

                # Inyectar stream_id automáticamente en tools que lo necesitan
                if tool_name in ("crear_rfqs_desde_texto", "notificar_sistema") and not tool_input.get("stream_id"):
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
            raw_content = r.get("content") or ""
            if not raw_content or not isinstance(raw_content, str):
                continue
            if raw_content.startswith("[SISTEMA:"):
                continue
            content = raw_content
            if r["role"] == "user" and r.get("procesado") and content != contenido:
                content = "[mensaje anterior — ya procesado]"
            # Evitar mensajes consecutivos del mismo rol (causa 400 en API de Claude)
            if historial and historial[-1]["role"] == r["role"]:
                continue
            historial.append({"role": r["role"], "content": content})
    except Exception as e:
        log.warning(f"No se pudo cargar historial: {e}")
        historial = [{"role": "user", "content": contenido}]

    # Garantizar que el historial empieza con user y termina con el mensaje actual
    while historial and historial[0]["role"] != "user":
        historial.pop(0)
    if not historial or historial[-1].get("content") != contenido:
        historial.append({"role": "user", "content": contenido})

    # Llamar a Claude
    token_counts = {"tokens_input": 0, "tokens_output": 0}
    try:
        roles = [m['role'] for m in historial]
        print(f"[BRAIN] historial roles={roles} n={len(historial)}", flush=True)
        respuesta, tools_used, rfqs_created, token_counts = run_chat(historial, stream_id=str(stream_id))
    except Exception as e:
        import traceback as _tb
        print(f"[BRAIN ERROR] {e}", flush=True)
        _tb.print_exc()
        log.error(f"Error en Claude (completo): {e}")
        respuesta    = f"Error procesando tu mensaje. Intenta de nuevo. ({str(e)[:400]})"
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
