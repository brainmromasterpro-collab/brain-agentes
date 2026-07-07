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
# Silenciar el flood de "HTTP Request: GET ..." (cada poll de los 5 agentes) para que los
# logs sean legibles y se puedan ver los [PERF] y demás mensajes importantes.
for _noisy in ("httpx", "httpcore", "anthropic", "hpack"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"],
)
claude = anthropic.Anthropic(
    api_key=os.environ["ANTHROPIC_API_KEY"],
    timeout=60.0,     # corta cualquier llamada colgada a los 60s (evita "procesando" perpetuo)
    max_retries=1,    # a lo más 1 reintento → peor caso ~120s y responde, no cuelga para siempre
)
# Modelo del chat. Configurable por env var para poder cambiar/revertir sin re-deploy.
CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-sonnet-5")

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


_gmail_token_cache: dict = {"token": None, "exp": 0.0}


def _gmail_access_token() -> str:
    """Obtiene un access token de Google. Lo cachea en memoria hasta ~5 min antes de expirar,
    para no hacer un round-trip OAuth (~300ms) en cada llamada dentro de un mismo request."""
    import time
    if _gmail_token_cache["token"] and time.time() < _gmail_token_cache["exp"]:
        return _gmail_token_cache["token"]
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
    _gmail_token_cache["token"] = data["access_token"]
    _gmail_token_cache["exp"]   = time.time() + data.get("expires_in", 3600) - 300
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


def _gmail_ultimo_mensaje_nuestro(thread_id: str, token: str) -> bool:
    """True si el ÚLTIMO mensaje del hilo lo enviamos NOSOTROS (label SENT) — o sea, ya
    respondimos y estamos esperando al cliente, por lo que la oportunidad NO es nueva.
    False si el último mensaje es del cliente (hay algo nuevo que atender) o si no se puede
    determinar (ante la duda, se trata como accionable)."""
    if not thread_id:
        return False
    try:
        th = httpx.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "minimal"}, timeout=15,
        ).json()
        msgs = th.get("messages", [])
        if not msgs:
            return False
        # Gmail devuelve los mensajes en orden ascendente; el último es el más reciente.
        return "SENT" in msgs[-1].get("labelIds", [])
    except Exception:
        return False


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
                "id":        m["id"],
                "thread_id": detail.get("threadId", ""),
                "de":        hdrs.get("From", ""),
                "para":      hdrs.get("To", ""),
                "asunto":    hdrs.get("Subject", ""),
                "fecha":     hdrs.get("Date", ""),
                "snippet":   detail.get("snippet", ""),
                "cuerpo":    body[:1500] if body else detail.get("snippet", ""),
                "leido":     "UNREAD" not in detail.get("labelIds", []),
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

    # Token de Gmail para revisar hilos (se toma una sola vez; _gmail_access_token no cachea).
    _gmail_token = None
    omitidas_ya_atendidas = 0

    oportunidades = []
    for email in emails_raw.get("emails", []):
        de_raw = email.get("de", "")
        match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', de_raw)
        email_addr = match.group(0).lower() if match else ""

        texto_completo = (email.get("asunto", "") + " " + email.get("cuerpo", "")).lower()
        es_rfq = any(k in texto_completo for k in keywords_rfq)

        # Una oportunidad YA no es "nueva" si ya la atendimos: es decir, si el último mensaje del
        # hilo lo enviamos nosotros (ya respondimos y esperamos al cliente). Si el cliente contestó
        # después (último mensaje suyo), sigue siendo accionable.
        if es_rfq:
            try:
                if _gmail_token is None:
                    _gmail_token = _gmail_access_token()
                if _gmail_ultimo_mensaje_nuestro(email.get("thread_id", ""), _gmail_token):
                    omitidas_ya_atendidas += 1
                    continue  # ya atendida — no aparece como nueva
            except Exception:
                pass  # ante la duda, se trata como accionable

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
            "thread_id":             email.get("thread_id", ""),
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
        "total_emails":          len(oportunidades),
        "posibles_rfqs":         len([o for o in oportunidades if o["es_rfq"]]),
        "clientes_conocidos":    len([o for o in oportunidades if o["es_cliente_conocido"]]),
        "omitidas_ya_atendidas": omitidas_ya_atendidas,
        "oportunidades":         oportunidades,
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
    fact_calle: str = "",
    fact_cp: str = "",
    razon_social: str = "",
    rfc: str = "",
    regimen_fiscal: str = "",
    condiciones_pago: str = "",
    industria: str = "",
) -> dict:
    """Crea una nueva cuenta/empresa en 1CRM.
    tipo: Customer | Supplier | Partner | Competitor | Press | Analyst | Other
    Los campos envio_* llenan la dirección de envío (shipping) y fact_* la de facturación.
    Datos fiscales para alta formal de cliente: razon_social, rfc, regimen_fiscal
    (ej. 'Persona Moral - General', 'Persona Física - General'), condiciones_pago
    (ej. 'advance_100%', 'Net_15', 'Net_30').
    """
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    payload: dict = {"name": nombre, "account_type": tipo}
    if email:         payload["email1"] = email
    if telefono:      payload["phone_office"] = telefono
    if tel_alternativo: payload["phone_alternate"] = tel_alternativo
    if web:           payload["website"] = web
    if fact_calle:    payload["billing_address_street"] = fact_calle
    if ciudad:        payload["billing_address_city"] = ciudad
    if estado:        payload["billing_address_state"] = estado
    if fact_cp:       payload["billing_address_postalcode"] = fact_cp
    if pais:          payload["billing_address_country"] = pais
    if descripcion:   payload["description"] = descripcion
    # Dirección de envío (shipping) — donde vive la "dirección de envío" del RFQ
    if envio_calle:   payload["shipping_address_street"] = envio_calle
    if envio_ciudad:  payload["shipping_address_city"] = envio_ciudad
    if envio_estado:  payload["shipping_address_state"] = envio_estado
    if envio_cp:      payload["shipping_address_postalcode"] = envio_cp
    if envio_pais:    payload["shipping_address_country"] = envio_pais
    # Datos fiscales (alta formal de cliente)
    if razon_social:     payload["razon_social"] = razon_social
    if rfc:              payload["rfc"] = rfc
    if regimen_fiscal:   payload["fiscal_regime"] = regimen_fiscal
    if condiciones_pago: payload["payment_terms"] = condiciones_pago
    if industria:        payload["industry"] = industria
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


def _extraer_producto_link(url: str) -> dict:
    """Extrae datos de producto de la página (og tags + JSON-LD). Funciona en sitios estándar
    (Shopify/e-commerce). Sitios con protección anti-bots (ej. Festo) devuelven error 403 →
    requieren navegador headless (fase 2, aún no disponible)."""
    import re as _re
    from urllib.parse import quote_plus
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }

    def _parse(html: str):
        """Extrae los datos del producto del HTML (og tags + JSON-LD). None si no hay producto."""
        def meta(prop):
            m = _re.search(rf'<meta[^>]+(?:property|name)=["\']{_re.escape(prop)}["\'][^>]+content=["\']([^"\']+)', html, _re.I) \
                or _re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{_re.escape(prop)}["\']', html, _re.I)
            return m.group(1) if m else None
        ld: dict = {}
        for m in _re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, _re.S | _re.I):
            try:
                data = json.loads(m.group(1))
                for obj in (data if isinstance(data, list) else [data]):
                    if isinstance(obj, dict) and obj.get("@type") == "Product":
                        ld = obj
                        break
            except Exception:
                pass
            if ld:
                break
        offers = ld.get("offers", {}) or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        brand = ld.get("brand")
        brand = brand.get("name") if isinstance(brand, dict) else brand
        img = ld.get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        nombre = ld.get("name") or meta("og:title")
        if not nombre:
            return None
        # Ficha técnica: JSON-LD additionalProperty (PropertyValue) → "nombre: valor unidad"
        caracteristicas = []
        for prop in (ld.get("additionalProperty") or []):
            if isinstance(prop, dict) and prop.get("name"):
                val = prop.get("value", "")
                unit = prop.get("unitText") or ""
                caracteristicas.append(f"{prop['name']}: {val}{(' ' + unit) if unit else ''}".strip())
        return {
            "ok": True, "url": url, "nombre": nombre, "marca": brand or "",
            "part_number":  ld.get("sku") or ld.get("mpn") or "",
            "precio_costo": meta("product:price:amount") or offers.get("price") or "",
            "moneda":       meta("product:price:currency") or offers.get("priceCurrency") or "",
            "descripcion":  (ld.get("description") or meta("og:description") or "")[:600],
            "caracteristicas": caracteristicas[:14],
            "imagen_url":   img or meta("og:image") or "",
        }

    def _fetch(fetch_url, timeout, headers=None):
        try:
            return httpx.get(fetch_url, headers=headers, timeout=timeout, follow_redirects=True)
        except Exception:
            return None

    # Muchos sitios (Vercel/Akamai) bloquean IPs de datacenter con 403/429. SCRAPER_API_URL
    # (ScrapingAnt/ScrapingBee/...) devuelve el HTML vía proxy residencial. El render de JS
    # (browser=true / render_js=true) es LENTO (~35-56s); por eso se intenta PRIMERO sin render
    # (rápido, ~4s, sirve para la mayoría) y solo se cae al render si no trae datos (sitios JS
    # como Festo). Sin la env var → fetch directo (sitios abiertos).
    scraper_tmpl = os.environ.get("SCRAPER_API_URL", "").strip()

    if scraper_tmpl:
        # Intento 1 — RÁPIDO (sin render): solo si el template traía render activado
        fast_tmpl = scraper_tmpl.replace("browser=true", "browser=false").replace("render_js=true", "render_js=false")
        if fast_tmpl != scraper_tmpl:
            r = _fetch(fast_tmpl.replace("{url}", quote_plus(url)), 30)
            if r is not None and r.status_code == 200:
                parsed = _parse(r.text)
                if parsed:
                    return parsed
        # Intento 2 — CON RENDER (lento pero pasa sitios JS/anti-bot como Festo)
        r = _fetch(scraper_tmpl.replace("{url}", quote_plus(url)), 90)
        if r is None:
            return {"error": "El scraper no respondió a tiempo. Reintenta o pega los datos manualmente."}
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code} — el sitio bloquea el acceso. Pega los datos manualmente."}
        return _parse(r.text) or {"error": "No encontré datos de producto en el link (¿es una página de producto?)."}

    # Sin scraper: fetch directo
    r = _fetch(url, 25, headers=hdrs)
    if r is None:
        return {"error": "No se pudo acceder al link."}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} — el sitio bloquea el acceso automático "
                         f"(sin SCRAPER_API_URL configurado). Pega los datos manualmente."}
    return _parse(r.text) or {"error": "No encontré datos de producto en el link (¿es una página de producto?)."}


def tool_extraer_producto_de_link(url: str) -> dict:
    """Extrae nombre, marca, part number, precio del proveedor, descripción e imagen de la
    página de un producto (para publicarlo en 1CRM desde un link)."""
    return _extraer_producto_link(url)


def tool_publicar_producto_link(
    nombre: str,
    part_number: str,
    marca: str = "",
    descripcion: str = "",
    caracteristicas: list | None = None,
    precio_costo: float = 0,
    imagen_url: str = "",
    url_origen: str = "",
    stream_id: str = "",
) -> dict:
    """Publica en 1CRM un producto extraído de un link. El precio_costo (del proveedor) va al
    campo INTERNO 'cost' (no se expone al público); el precio de venta (list_price) queda en 0
    para definirlo después. Reutiliza el pipeline del publicador y su widget producto_publicado."""
    try:
        # Descripción completa para 1CRM = descripción + ficha técnica (características)
        desc_full = descripcion or nombre
        if caracteristicas:
            desc_full += "\n\nFicha técnica:\n" + "\n".join(f"• {c}" for c in caracteristicas)
        clean_stream = stream_id if (stream_id and stream_id not in ("None", "")) else None
        now = datetime.now(timezone.utc)
        bulk_id = str(uuid.uuid4())  # bulk de 1 producto → dispara el BulkWidget (widget "Ver en CRM")
        rfq_id_str = f"LINK-{now.year}-{now.month:02d}{now.day:02d}-{str(uuid.uuid4())[:6].upper()}"
        rfq_row: dict = {
            "stream_id": clean_stream,
            "rfq_id":    rfq_id_str,
            "modelo":    part_number or nombre,
            "marca":     marca or "",
            "estado":    "publicando",
            "foto_url":  imagen_url or None,
            "bulk_id":   bulk_id,
        }
        try:
            rfq_resp = supabase.table("rfqs").insert(rfq_row).execute()
        except Exception:
            rfq_row["stream_id"] = None
            rfq_resp = supabase.table("rfqs").insert(rfq_row).execute()
        rfq_id = rfq_resp.data[0]["id"]
        job = supabase.table("jobs").insert({
            "rfq_id":     rfq_id,
            "agente":     "publicador",
            "estado":     "pendiente",
            "created_at": now.isoformat(),
            "input": {
                "origen": "link",
                "url":    url_origen,
                "ficha": {
                    "nombre":      nombre,
                    "descripcion": desc_full,
                    "cost":        float(precio_costo or 0),
                    "list_price":  0,
                },
            },
        }).execute()
        job_id = (job.data or [{}])[0].get("id", "?")
        # Notificación tipo='bulk' → el frontend renderiza el BulkWidget (tarjeta negra con
        # "Ver en CRM"). Se usa el stream real del chat (no el del rfq, que puede caer a None por FK).
        try:
            supabase.table("notificaciones").insert({
                "tipo":      "bulk",
                "titulo":    f"📦 Publicando {nombre}",
                "mensaje":   json.dumps({"bulk_id": bulk_id, "lista": f"• {nombre}", "total": 1}),
                "rfq_id":    rfq_id,
                "stream_id": clean_stream,
                "leida":     False,
            }).execute()
        except Exception as e:
            log.warning(f"No se pudo crear notificación bulk del link: {e}")
        log.info(f"Job publicador (link) creado: {job_id} para '{nombre}'")
        return {"ok": True, "rfq_id": rfq_id, "job_publicador": job_id, "nombre": nombre}
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
        "name": "extraer_producto_de_link",
        "description": "Extrae los datos de un producto (nombre, marca, part number, precio del proveedor, descripción, imagen) desde la URL de una página de producto. Usar cuando el usuario pega un link de producto para publicarlo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL de la página del producto"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "publicar_producto_link",
        "description": "Publica en 1CRM un producto extraído de un link (tras aprobación del usuario). El precio del proveedor va al campo interno 'cost' (no público). Reutiliza el pipeline del publicador; el widget producto_publicado muestra el resultado. El stream_id se inyecta automáticamente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre":       {"type": "string", "description": "Nombre del producto"},
                "part_number":  {"type": "string", "description": "Número de parte / SKU / modelo"},
                "marca":        {"type": "string", "description": "Marca/fabricante"},
                "descripcion":  {"type": "string", "description": "Descripción del producto"},
                "caracteristicas": {"type": "array", "items": {"type": "string"}, "description": "Ficha técnica: lista de 'nombre: valor unidad' extraída del link"},
                "precio_costo": {"type": "number", "description": "Precio del proveedor (va a 'cost', interno, no público)"},
                "imagen_url":   {"type": "string", "description": "URL de la imagen del producto"},
                "url_origen":   {"type": "string", "description": "URL original del link"},
            },
            "required": ["nombre", "part_number"],
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
                "fact_calle":      {"type": "string", "description": "Dirección de facturación: calle y número"},
                "fact_cp":         {"type": "string", "description": "Dirección de facturación: código postal"},
                "razon_social":    {"type": "string", "description": "Razón social (alta fiscal)"},
                "rfc":             {"type": "string", "description": "RFC del cliente"},
                "regimen_fiscal":  {"type": "string", "description": "Régimen fiscal: 'Persona Moral - General', 'Persona Moral - RESICO', 'Persona Física - General', etc."},
                "condiciones_pago":{"type": "string", "description": "Condiciones de pago: advance_100%, advance_50%, Net_15, Net_30, Net_45, Net_60"},
                "industria":       {"type": "string", "description": "Industria (opcional)"},
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
    "extraer_producto_de_link":  tool_extraer_producto_de_link,
    "publicar_producto_link":    tool_publicar_producto_link,
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

2. Verifica los 5 DATOS OBLIGATORIOS para crear la oportunidad:
   - Contacto: nombre de la persona.
   - Empresa: nombre de la empresa/cuenta.
   - RFQ + Qty: al menos un part-number/modelo Y su cantidad de unidades.
   - Correo: email de contacto.
   - Dirección de envío.
   NOTA: el WhatsApp NO es obligatorio — si viene se guarda (phone_mobile), pero su ausencia NO bloquea la creación.

3. SI FALTA CUALQUIER BLOQUE → NO crees nada. Haz estas 3 cosas, en orden:

   (a) AVISA al usuario del chat que la oportunidad está INCOMPLETA y enumera exactamente qué bloque(s) faltan. \
       Ejemplo: "⚠️ La oportunidad de <remitente/cuenta> está incompleta. Faltan: cantidad (Qty) y dirección de envío."

   (b) REDACTA un correo de respuesta (mismo hilo, thread_id del email) pidiendo ÚNICAMENTE los datos faltantes, \
       y muéstraselo al usuario del chat como borrador. Reglas de redacción OBLIGATORIAS: \
       - TRATO: SIEMPRE de USTED (formal, profesional). NUNCA tutees al prospecto ("tú", "tu", "contáctanos"). \
       - TONO: positivo y cordial, que transmita que ya estamos avanzando. \
       - SIEMPRE, antes de pedir datos, confirma que YA estamos trabajando en su requerimiento y buscando su RFQ. \
       - CRÍTICO — NO CONDICIONAR: el avance del requerimiento NUNCA depende de que el prospecto envíe sus datos. \
         Ya estamos procesando su solicitud pase lo que pase. PROHIBIDAS las frases que condicionen o presionen, como \
         "para poder avanzar", "para continuar", "para procesar necesitamos", "una vez que nos envíe", "en cuanto \
         recibamos podremos". Los datos se piden de forma SUAVE y OPCIONAL (ej. "cuando le sea posible", "para \
         tenerlo todo listo al momento de la entrega"), nunca como requisito para atenderlo. \
       - Si el remitente NO es cliente en el CRM, menciónalo de forma positiva y ligera: que notamos que aún no \
         cuenta con un registro con nosotros y que con gusto lo damos de alta — sin condicionar el avance a ello. \
       - PROHIBIDO: NO menciones la palabra "cotización" ni prometas cotizar todavía, y NO des precios. \
       IDIOMA: detecta el idioma del correo ORIGINAL del cliente y redacta la respuesta en ESE mismo idioma \
       (si el RFQ llegó en inglés, contesta en inglés; si en portugués, en portugués; etc.). \
       La plantilla de abajo está en español (trato de usted) SOLO como modelo — tradúcela al idioma del cliente \
       conservando el trato formal, el tono positivo y el carácter NO condicionante:

       "Estimado/a [nombre]:
       Gracias por contactarnos. Le confirmamos que ya estamos trabajando en su requerimiento y buscando su RFQ; \
       muy pronto le compartimos el avance.
       Notamos además que aún no cuenta con un registro con nosotros y con gusto lo damos de alta. Cuando le sea \
       posible, le agradeceríamos compartirnos los siguientes datos para tenerlo todo listo:
       - [dato faltante 1]
       - [dato faltante 2]
       Seguimos avanzando con su requerimiento mientras tanto. Quedamos a sus órdenes. Saludos cordiales."

       Si el remitente SÍ es cliente existente en el CRM, omite la frase de registro/alta y solo pide de forma suave \
       (trato de usted, no condicionante) el dato faltante, manteniendo el tono positivo y la confirmación de que ya \
       se trabaja en su requerimiento.

   (c) Termina con [DECISION: ¿Envío esta solicitud de información a <remitente>?]. \
       El correo queda SUJETO A LA APROBACIÓN del usuario: solo tras el "Sí" llamas a enviar_email_gmail. \
       Nunca envíes sin aprobación, y nunca crees la oportunidad mientras falte información.

   (d) DATOS APORTADOS POR EL USUARIO DEL CHAT (doble fuente): si el usuario interno del chat te DA directamente \
       los datos que faltaban (porque los consiguió con el prospecto por otro canal — teléfono, WhatsApp, etc.), \
       tómalos como válidos SIN necesidad de enviar el correo al prospecto. Con esos datos la oportunidad queda \
       completa: pasa directo al paso 4 (cotejar y crear, con su [DECISION]). Es decir, los faltantes se pueden \
       resolver por DOS vías indistintas: (1) respuesta del prospecto por correo/WhatsApp, o (2) el usuario del chat \
       te los proporciona. Cualquiera de las dos completa la oportunidad.

4. SI ESTÁN LOS 4 BLOQUES COMPLETOS (por el RFQ, o completados por el prospecto o por el usuario del chat) → \
   coteja con el CRM (escanear_emails_ventas o buscar_clientes_crm por el correo/dominio del remitente):

   4a. YA ES CLIENTE (la cuenta existe en CRM): termina con [DECISION: ¿Creo la oportunidad para <cuenta>?]. \
       Tras el "Sí": crea la oportunidad con crear_oportunidad_crm — pon en descripcion el detalle "RFQ: <part-numbers> | Qty: <cantidades>" \
       y usa el cuenta_id del CRM. Confirma con el link de la oportunidad creada.

   4b. NO ES CLIENTE (no hay cuenta en CRM): hay que DAR DE ALTA al cliente primero. Sigue el MODO 12 (alta \
       inicial, baja fricción): basta con empresa, contacto, correo y dirección de envío — SIN datos fiscales. \
       Créalo con [DECISION], y en cuanto la cuenta exista crea la oportunidad ligada con crear_oportunidad_crm \
       (cuenta_id de la cuenta recién creada, descripcion "RFQ: <part-numbers> | Qty: <cantidades>").

CRÍTICO: en este modo nunca creas nada en el CRM ni envías correos sin el [DECISION] aprobado por el usuario. \
Si faltan datos, primero se piden; solo con los 4 bloques completos se procede a cotejar y crear.

MODO 11 — REVISAR OPORTUNIDADES DEL CORREO (lote):
Cuando el usuario pida "lee los correos", "revisa el correo y detecta oportunidades", \
"escanea oportunidades", "qué oportunidades hay", o similar (varios correos a la vez):

1. Llama a escanear_emails_ventas. Toma solo los que tengan es_rfq = true; ésas son las oportunidades. \
   El escaneo YA excluye las oportunidades atendidas (hilos donde nuestro último correo espera respuesta del cliente); \
   NUNCA vuelvas a listar como "nueva" una que ya se procesó. Si "omitidas_ya_atendidas" > 0, \
   menciónalo al final en una línea (ej: "Omití N que ya están en proceso, esperando respuesta del cliente").

2. Para CADA oportunidad, evalúa los mismos 5 DATOS OBLIGATORIOS del MODO 10 \
   (Contacto, Empresa, RFQ+Qty, Correo, Dirección de envío; el whatsapp NO es obligatorio) usando el contenido \
   del correo y el cotejo con CRM (es_cliente_conocido / cliente_crm). Anota qué falta en cada una.

3. Si hay al menos una oportunidad, avisa con notificar_sistema (titulo "🔔 N oportunidades detectadas en el correo").

4. Presenta TODAS las oportunidades emitiendo EXACTAMENTE este marcador, en UNA sola línea y con JSON VÁLIDO \
   (el frontend lo convierte en un widget visual — NO escribas tabla, tarjetas ni texto con los datos): \
   [OPORTUNIDADES]{"total":N,"resumen":"X completas, Y incompletas","omitidas":M,"oportunidades":[{"remitente":"Nombre","correo":"a@b.com","empresa":"Empresa","es_cliente":true,"productos":["Q0120 · 2 pz"],"faltan":["Dirección de envío"],"completa":false}],"correos_no_rfq":[]} \
   Reglas del JSON: "faltan" = lista de datos obligatorios que faltan (vacío [] si está completa); "completa" = true si no falta nada; \
   "omitidas" = valor de omitidas_ya_atendidas. Si NO hay oportunidades (total 0), emite igual el marcador con \
   "total":0, "oportunidades":[] y "correos_no_rfq":["asunto/resumen corto de cada correo revisado"], y NO notifiques. \
   NUNCA repitas los datos en texto aparte del marcador: el widget ya los muestra.

5. DESPUÉS del marcador, procesa las oportunidades UNA por una (solo las que tengan faltan/creación pendiente), \
   empezando por la #1, aplicando el flujo del MODO 10: si está completa → [DECISION: ¿Creo la oportunidad para <empresa>?] \
   (crea con alta de cuenta/contacto si es cliente nuevo, MODO 12); si le falta info → borrador de correo pidiendo SOLO \
   lo faltante (reglas del MODO 10 paso 3) + [DECISION: ¿Envío la solicitud a <remitente>?]. Tras resolver una, sigue con \
   la siguiente. Nunca crees ni envíes sin el [DECISION] aprobado.

MODO 12 — ALTA DE CLIENTE NUEVO (alta inicial, baja fricción):
Se dispara cuando un prospecto con RFQ NO es cliente en el CRM (desde el MODO 10/11), o cuando el usuario \
pide "da de alta a <cliente>". Objetivo: registrar la cuenta con lo MÍNIMO para poder cotizar. \
Aplica igual a clientes nacionales e INTERNACIONALES.

IMPORTANTE — NO pedir datos fiscales aquí (RFC, razón social, régimen fiscal). Esos son para la FACTURACIÓN y \
producen fricción innecesaria solo para cotizar. Además, con clientes internacionales muchos ni aplican. \
El onboarding fiscal formal ocurre DESPUÉS, cuando el cliente manda la ORDEN DE COMPRA (flujo aparte).

1. DATOS DEL ALTA INICIAL — solo estos 4, nada más:
   - Empresa: nombre de la empresa.
   - Contacto: nombre de la persona.
   - Correo.
   - Dirección de envío.

2. FALTANTES — DOBLE FUENTE (igual que en oportunidades): lo que falte se obtiene por (1) pedírselo al prospecto \
   por correo/WhatsApp (borrador cortés, trato de usted, no condicionante, con [DECISION] antes de enviar — reglas \
   del MODO 10 paso 3), o (2) que el usuario del chat te lo dé directamente. Avisa en el chat qué falta.

3. CREAR (solo con [DECISION] aprobado): [DECISION: ¿Doy de alta a <empresa> como cliente?]. Tras el "Sí":
   (i)  crear_cuenta_crm con tipo="Customer": nombre, email, teléfono y envio_* (dirección de envío). SIN datos fiscales.
   (ii) crear_contacto_crm ligado con cuenta_id (el id de i): nombre, apellido, email, whatsapp.
   Confirma el alta con los links de cuenta y contacto.

4. ENCADENAR CON LA OPORTUNIDAD: si el alta vino de un RFQ (MODO 10/11), en cuanto quede creada la cuenta, \
   crea la oportunidad ligada (crear_oportunidad_crm con ese cuenta_id) — con su propio [DECISION] si no se aprobó ya.

NOTA: los campos fiscales (razon_social, rfc, regimen_fiscal, condiciones_pago) EXISTEN en crear_cuenta_crm pero \
solo se llenan en el onboarding formal posterior (orden de compra), NO en esta alta inicial.

MODO 13 — PUBLICAR PRODUCTO DESDE UN LINK:
Cuando el usuario pega una URL de una página de producto (http/https) para publicarla: primero EXTRAES y \
muestras la tarjeta, y con UNA sola aprobación publicas.

IDIOMA DEL PRODUCTO — REGLA FIJA: los productos del catálogo SIEMPRE van en INGLÉS. Si los datos extraídos \
vienen en otro idioma (alemán, español, etc.), TRADUCE al inglés el nombre, la descripción y las características \
(tanto las etiquetas como los valores de texto: ej. "Anschlussart: Plattenaufbau" → "Connection type: Plate mounting", \
"Betriebsdruck, max.: 420 bar" → "Operating pressure, max.: 420 bar") ANTES de emitir el marcador [PRODUCTO_PREVIEW] \
y ANTES de llamar a publicar_producto_link. NO traduzcas: part numbers, códigos, números ni unidades \
(R900938249, 420 bar, 18.58 kg, ISO 7368, NBR se quedan igual). La tarjeta y el producto en 1CRM quedan en inglés.

1. Al recibir el link, EXTRAE DE INMEDIATO con extraer_producto_de_link (una línea breve antes está bien, \
   ej. "Reviso el link, dame un momento…"). NO preguntes antes de extraer — el usuario quiere ver los datos \
   para decidir con la información a la vista. \
   - Si devuelve "error" (sitio protegido / sin datos): dilo claramente y ofrece que el usuario pegue los datos \
     manualmente (nombre, part number, marca, precio, imagen). NO publiques con datos inventados. \
   - Si extrae bien: NO listes los datos como texto. En su lugar, emite EXACTAMENTE este marcador (el frontend lo \
     convierte en una tarjeta visual del producto), en una sola línea y con JSON válido: \
     [PRODUCTO_PREVIEW]{"nombre":"...","marca":"...","part_number":"...","precio_costo":"...","moneda":"...","descripcion":"...","caracteristicas":["...","..."],"imagen_url":"..."} \
     usando los valores tal cual los devolvió extraer_producto_de_link (copia el arreglo "caracteristicas" \
     completo tal como vino; si un campo viene vacío, pon "" o []). \
     Y DESPUÉS de la tarjeta termina con [DECISION: ¿Publico este producto en 1CRM?]. \
     No repitas los datos en texto: la tarjeta ya los muestra.

2. Tras el "Sí", llama a publicar_producto_link con TODOS los datos extraídos (incluye descripcion y el \
   arreglo caracteristicas para que queden en la ficha del producto en 1CRM) (precio_costo = precio del \
   proveedor, que va al campo interno 'cost'; el precio de venta público queda en 0). La publicación es ASÍNCRONA \
   (el worker publica en unos segundos y el widget producto_publicado — el que trae el link "Ver en CRM" — aparece \
   solo). Responde breve, tipo "Publicando en 1CRM… en un momento te muestro el producto." NO afirmes "Publicado ✅" \
   ni describas el resultado con texto largo: el widget lo maneja.

CRÍTICO: nunca publiques sin el [DECISION] aprobado. El precio del proveedor es interno (cost), no público.

MODO 7 — CHAT CONVERSACIONAL:
Para preguntas o solicitudes de información, usa las herramientas disponibles \
(1CRM, RFQs, métricas, internet) para responder con datos reales.

Reglas:
- Responde siempre en español
- Sé conciso y directo
- Nunca inventes precios o disponibilidad — usa siempre las herramientas
- CRÍTICO: Si una búsqueda no devuelve resultados, di "no encontré resultados para X" — NUNCA afirmes que un producto "no existe" o "no está publicado" basándote solo en que la búsqueda no lo encontró. La ausencia de resultados NO es prueba de ausencia del producto.
- OPORTUNIDADES — REGLA ABSOLUTA: NUNCA crees una oportunidad (crear_oportunidad_crm) si falta \
cualquiera de los 5 datos obligatorios (Contacto, Empresa, RFQ+Qty, Correo, Dirección de envío). \
El WhatsApp NO es obligatorio y no bloquea la creación. \
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
# LOG DEL STREAM (alimenta los logs del UI: global y por-stream)
# ─────────────────────────────────────────────────────────────
def _log_stream(stream_id: str, msg: str, tipo: str = "ok") -> None:
    """Escribe una entrada en stream_logs. El dashboard lee de aquí tanto el
    log global como el log por-stream. Best-effort: nunca rompe el flujo."""
    if not stream_id or not msg:
        return
    try:
        supabase.table("stream_logs").insert({
            "stream_id": str(stream_id),
            "msg":       str(msg)[:500],
            "type":      tipo,
        }).execute()
    except Exception as e:
        log.warning(f"No se pudo escribir en stream_logs: {e}")


def _mensaje_log_tool(tool_name: str, tool_input: dict, result) -> tuple[str, str]:
    """Convierte una llamada de tool + su resultado en (mensaje, tipo) legible para el log del UI."""
    err = isinstance(result, dict) and (result.get("error") or result.get("faltantes"))
    tipo = "error" if err else "ok"
    ti = tool_input or {}
    if tool_name == "enviar_email_gmail":
        msg = f"Correo enviado a {ti.get('para','')}" if not err else \
              f"Falló envío de correo: {(result.get('error') or result.get('detalle',''))[:120]}"
    elif tool_name == "crear_oportunidad_crm":
        msg = f"Oportunidad creada: {ti.get('nombre','')}" if not err else \
              f"No se creó oportunidad — falta info: {result.get('faltantes') or result.get('mensaje','')}"
    elif tool_name == "crear_cuenta_crm":
        msg = f"Cuenta creada en CRM: {ti.get('nombre','')}" if not err else f"No se creó la cuenta: {result.get('error','')}"
    elif tool_name == "crear_contacto_crm":
        msg = f"Contacto creado: {(ti.get('nombre','')+' '+ti.get('apellido','')).strip()}" if not err else \
              f"No se creó el contacto: {result.get('error','')}"
    elif tool_name == "escanear_emails_ventas":
        msg = f"Correos escaneados: {result.get('posibles_rfqs','?')} oportunidad(es) nueva(s)" if not err else \
              f"Error escaneando correos: {result.get('error','')}"
    elif tool_name == "crear_rfqs_desde_texto":
        msg = f"RFQs creados: {result.get('creados','?')}"
    elif tool_name == "notificar_sistema":
        msg = f"Notificación: {ti.get('titulo','')}"
    else:
        msg = tool_name.replace("_", " ").capitalize()
    return msg, tipo


# Mensaje que se muestra en la burbuja "procesando" ANTES de ejecutar una tarea larga,
# con el estimado de tiempo, para que el usuario sepa que hay que esperar y que se le avisará.
_LOG_INICIO_TOOL = {
    "escanear_emails_ventas":   "🔎 Reviso tu correo en busca de oportunidades… (~1 min). Te aviso al terminar.",
    "publicar_producto_link":   "📦 Publicando en 1CRM… (~30s). Te aviso cuando el producto esté listo.",
    "crear_rfqs_desde_texto":   "🔍 Buscando proveedores (~1-2 min por producto)… Te aviso al terminar.",
    "publicar_rfq":             "📦 Publicando en 1CRM… (~30s). Te aviso cuando esté listo.",
    "publicar_sin_imagen_rfq":  "📦 Publicando en 1CRM… (~30s). Te aviso cuando esté listo.",
    "extraer_producto_de_link": "🔎 Reviso el link del producto…",
    "buscar_internet":          "🌐 Buscando en internet…",
}


# ─────────────────────────────────────────────────────────────
# LOOP DE CLAUDE CON TOOL_USE
# ─────────────────────────────────────────────────────────────
def run_chat(messages: list[dict], stream_id: str) -> tuple[str, list[str], bool, dict]:
    tools_used: list[str] = []
    rfqs_created = False
    current_messages = list(messages)
    total_input_tokens  = 0
    total_output_tokens = 0

    import time as _time
    _t_start = _time.time()
    for _ronda in range(10):
        _t_llm = _time.time()
        response = claude.messages.create(
            model=CHAT_MODEL,
            max_tokens=4096,
            # (timeout y max_retries se definen a nivel de cliente — ver anthropic.Anthropic arriba)
            # cache_control cachea el prefijo estático (tools + system prompt). En el loop de
            # tools (hasta 10 vueltas) las llamadas siguientes leen de caché en vez de
            # reprocesar ~30 tools + 11 modos cada vez → mucha menos latencia y costo.
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=current_messages,
        )
        _dt_llm = _time.time() - _t_llm

        if hasattr(response, "usage") and response.usage:
            total_input_tokens  += getattr(response.usage, "input_tokens",  0)
            total_output_tokens += getattr(response.usage, "output_tokens", 0)
            _u = response.usage
            log.info(f"[PERF] ronda={_ronda} llm={_dt_llm:.1f}s stop={response.stop_reason} "
                     f"in={getattr(_u,'input_tokens',0)} out={getattr(_u,'output_tokens',0)} "
                     f"cache_read={getattr(_u,'cache_read_input_tokens',0)} "
                     f"cache_write={getattr(_u,'cache_creation_input_tokens',0)}")

        if response.stop_reason == "end_turn":
            text = next(
                (b.text for b in response.content if hasattr(b, "text")), ""
            )
            log.info(f"[PERF] TOTAL run_chat={_time.time()-_t_start:.1f}s rondas={_ronda+1} "
                     f"tools={tools_used}")
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
                if tool_name in ("crear_rfqs_desde_texto", "notificar_sistema", "publicar_producto_link") and not tool_input.get("stream_id"):
                    tool_input["stream_id"] = stream_id

                # Aviso de inicio con estimado (la burbuja "procesando" lo muestra en vivo)
                _ini = _LOG_INICIO_TOOL.get(tool_name)
                if _ini:
                    _log_stream(stream_id, _ini, "info")

                fn = TOOL_FUNCTIONS.get(tool_name)
                _t_tool = _time.time()
                try:
                    result = fn(**tool_input) if fn else {"error": f"Tool '{tool_name}' no existe"}
                    if tool_name == "crear_rfqs_desde_texto" and result.get("creados", 0) > 0:
                        rfqs_created = True
                except Exception as e:
                    result = {"error": str(e)}
                log.info(f"[PERF] tool={tool_name} dt={_time.time()-_t_tool:.1f}s")

                # Registrar la acción en el log del stream (UI global + por-stream)
                _msg_log, _tipo_log = _mensaje_log_tool(tool_name, tool_input, result)
                _log_stream(stream_id, _msg_log, _tipo_log)

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

    # Log del stream: registrar la solicitud entrante (alimenta el log del UI)
    _log_stream(stream_id, f'Solicitud recibida: "{contenido[:80]}"', "info")

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
        _log_stream(stream_id, f"Error procesando el mensaje: {str(e)[:150]}", "error")
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

    # Respuesta vacía: NO dejar al usuario sin nada (antes se hacía return y el mensaje
    # quedaba "procesado" sin respuesta → parecía que el chat no contestaba). Insertar un
    # fallback salvo que se hayan creado RFQs (ese caso ya se manejó arriba).
    if not respuesta.strip():
        log.warning("Respuesta vacía del modelo — insertando fallback")
        log.warning(f"[PERF] respuesta VACÍA tras tools={tools_used}")
        respuesta = ("Procesé tu solicitud pero no generé un mensaje de respuesta. "
                     "¿Puedes reformular o intentar de nuevo?")

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

    # Al arrancar, descartar SOLO el backlog realmente viejo (>5 min) para no responder a
    # mensajes de sesiones pasadas. Los mensajes recientes (<5 min) NO se descartan: así, si
    # el worker reinicia justo después de que el usuario envió algo, ese mensaje SÍ se procesa
    # en lugar de quedar "procesado" sin respuesta (antes se marcaban TODOS → se perdían).
    try:
        from datetime import timedelta
        corte = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        supabase.table("mensajes").update({"procesado": True}).filter(
            "procesado", "is", "false"
        ).eq("role", "user").lt("created_at", corte).execute()
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
