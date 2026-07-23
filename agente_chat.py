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
from datetime import datetime, timezone, timedelta
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
VENTAS_USER          = os.environ.get("GMAIL_DELEGATED_USER", "ventas@mromasterpro.com")


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


_gmail_token_cache: dict  = {"token": None, "exp": 0.0}
_ventas_token_cache: dict = {"token": None, "exp": 0.0}


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


def _gmail_access_token_ventas() -> str:
    """Token de acceso para ventas@mromasterpro.com vía Service Account + DWD.
    Requiere GOOGLE_SERVICE_ACCOUNT_JSON en env."""
    import time
    if _ventas_token_cache["token"] and time.time() < _ventas_token_cache["exp"]:
        return _ventas_token_cache["token"]
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON no configurado")
    from google.oauth2 import service_account
    import google.auth.transport.requests
    import base64 as _b64
    # Acepta tanto JSON directo como JSON codificado en base64 (recomendado para Railway)
    try:
        info = json.loads(sa_json)
    except json.JSONDecodeError:
        info = json.loads(_b64.b64decode(sa_json).decode())
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    ).with_subject(VENTAS_USER)
    creds.refresh(google.auth.transport.requests.Request())
    _ventas_token_cache["token"] = creds.token
    _ventas_token_cache["exp"]   = time.time() + 3300  # ~55 min
    return creds.token


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


def _leer_emails_con_token(token: str, max_emails: int, query: str) -> list[dict]:
    """Lee emails de Gmail usando un access token ya obtenido."""
    headers = {"Authorization": f"Bearer {token}"}
    base = "https://gmail.googleapis.com/gmail/v1"
    params = {"userId": "me", "maxResults": min(max_emails, 20), "q": query}
    lista = httpx.get(f"{base}/users/me/messages", headers=headers, params=params, timeout=15).json()
    emails = []
    for m in lista.get("messages", []):
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
    return emails


def tool_leer_emails_gmail(max_emails: int = 10, query: str = "newer_than:1d",
                           cuenta: str = "ventas") -> dict:
    """Lee emails de Gmail.
    cuenta: 'ventas' (ventas@mromasterpro.com, default), 'personal' (brain.mromasterpro@gmail.com),
            'ambas' (combina los dos buzones).
    query soporta filtros de Gmail: newer_than:1d, from:alguien@mail.com, subject:tema, is:unread, etc.
    """
    emails: list[dict] = []
    errors: list[str] = []

    if cuenta in ("ventas", "ambas"):
        try:
            token = _gmail_access_token_ventas()
            emails += _leer_emails_con_token(token, max_emails, query)
        except Exception as e:
            errors.append(f"ventas@: {e}")

    if cuenta in ("personal", "ambas"):
        refresh = os.environ.get("GOOGLE_REFRESH_TOKEN", GOOGLE_REFRESH_TOKEN)
        if refresh:
            try:
                token = _gmail_access_token()
                emails += _leer_emails_con_token(token, max_emails, query)
            except Exception as e:
                errors.append(f"personal: {e}")

    if not emails and errors:
        return {"error": "; ".join(errors)}

    # Deduplicar por id (puede haber overlap si el mismo correo está en ambas cuentas)
    seen: set[str] = set()
    unique = [e for e in emails if not (e["id"] in seen or seen.add(e["id"]))]  # type: ignore[func-returns-value]
    return {"total": len(unique), "query": query, "cuenta": cuenta, "emails": unique}


def tool_buscar_email_gmail(query: str, max_emails: int = 5) -> dict:
    """Busca emails en Gmail con cualquier query: from:, subject:, has:attachment, etc."""
    return tool_leer_emails_gmail(max_emails=max_emails, query=query)


def tool_enviar_email_gmail(para: str, asunto: str, cuerpo: str, thread_id: str = "",
                            message_id: str = "", cuenta: str = "ventas") -> dict:
    """Envía o responde un email desde la cuenta de la empresa.
    cuenta: 'ventas' (ventas@mromasterpro.com, default) o 'personal' (brain.mromasterpro@gmail.com).
    Si se pasa thread_id y message_id, responde en el hilo existente (Reply).
    """
    import base64
    from email.mime.text import MIMEText

    try:
        if cuenta == "ventas":
            token   = _gmail_access_token_ventas()
            from_addr = VENTAS_USER
        else:
            refresh = os.environ.get("GOOGLE_REFRESH_TOKEN", GOOGLE_REFRESH_TOKEN)
            if not refresh:
                return {"error": "GOOGLE_REFRESH_TOKEN no configurado"}
            token   = _gmail_access_token()
            from_addr = GMAIL_USER

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        msg = MIMEText(cuerpo, "plain", "utf-8")
        msg["To"]      = para
        msg["From"]    = from_addr
        msg["Subject"] = asunto
        if message_id:
            msg["In-Reply-To"] = message_id
            msg["References"]  = message_id

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body_payload: dict = {"raw": raw}
        if thread_id:
            body_payload["threadId"] = thread_id

        resp = httpx.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers=headers,
            content=json.dumps(body_payload).encode(),
            timeout=15,
        )
        if not resp.is_success:
            return {"error": f"Gmail API {resp.status_code}", "detalle": resp.text[:500]}
        data = resp.json()
        return {
            "ok":         True,
            "message_id": data.get("id"),
            "thread_id":  data.get("threadId"),
            "enviado_a":  para,
            "desde":      from_addr,
            "asunto":     asunto,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_diagnostico_gmail() -> dict:
    """Diagnóstica el estado de la conexión Gmail — muestra si las variables están configuradas."""
    client_id  = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_sec = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh    = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    sa_json    = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    status = {
        "GOOGLE_CLIENT_ID":            "✅ configurado" if client_id  else "❌ falta",
        "GOOGLE_CLIENT_SECRET":        "✅ configurado" if client_sec else "❌ falta",
        "GOOGLE_REFRESH_TOKEN":        "✅ configurado" if refresh    else "❌ falta",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "✅ configurado" if sa_json   else "❌ falta",
        "GMAIL_DELEGATED_USER":        VENTAS_USER,
    }
    if client_id and client_sec and refresh:
        try:
            _gmail_access_token()
            status["conexion_personal"] = f"✅ token OK para {GMAIL_USER}"
        except Exception as e:
            status["conexion_personal"] = f"❌ error: {e}"
    else:
        status["conexion_personal"] = "❌ variables incompletas"
    if sa_json:
        try:
            _gmail_access_token_ventas()
            status["conexion_ventas"] = f"✅ token OK para {VENTAS_USER}"
        except Exception as e:
            status["conexion_ventas"] = f"❌ error: {e}"
    else:
        status["conexion_ventas"] = "❌ GOOGLE_SERVICE_ACCOUNT_JSON no configurado"
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


def _notif(stream_id: str, titulo: str, mensaje: str = "", tipo: str = "oportunidad") -> None:
    """Notificación DETERMINISTA (la emite la tool al tener éxito, no el modelo → nunca se salta)."""
    try:
        payload: dict = {"tipo": tipo, "titulo": titulo, "mensaje": mensaje or "", "leida": False}
        if stream_id and stream_id not in ("None", ""):
            payload["stream_id"] = stream_id
        supabase.table("notificaciones").insert(payload).execute()
    except Exception as e:
        log.warning(f"notif determinista falló: {e}")


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
                "url_crm":   f"{ONECRM_BASE}/index.php?module=Accounts&action=DetailView&record={r.get('id')}",
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
        "url_crm":  f"{ONECRM_BASE}/index.php?module=Accounts&action=DetailView&record={cliente_id}",
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
                "url_crm":   f"{ONECRM_BASE}/index.php?module=Opportunities&action=DetailView&record={r.get('id')}",
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
    stream_id: str = "",
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
    if opp_id:
        _notif(stream_id, f"✅ Oportunidad creada — {nombre}", descripcion[:120])
    return {
        "ok":      bool(opp_id),
        "id":      opp_id,
        "nombre":  nombre,
        "url_crm": f"{ONECRM_BASE}/index.php?module=Opportunities&action=DetailView&record={opp_id}" if opp_id else "",
    }


def _norm_nombre(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _buscar_cuenta_existente(nombre: str, email: str = "") -> dict | None:
    """Busca una cuenta ya existente en 1CRM por CORREO (identificador confiable) o por NOMBRE de
    empresa. Devuelve {id, nombre, url_crm, por} o None. Evita crear clientes duplicados."""
    if not ONECRM_BASE:
        return None
    def _url(rid): return f"{ONECRM_BASE}/index.php?module=Accounts&action=DetailView&record={rid}"
    try:
        # 1) por email exacto (email1 o email2)
        if email:
            data = _onecrm_get("data/Account", {"filter_text": email, "max_num": 20})
            for r in (data.get("records", []) or []):
                if email.lower() in ((r.get("email1", "") or "").lower(), (r.get("email2", "") or "").lower()):
                    return {"id": r.get("id"), "nombre": r.get("name", ""), "url_crm": _url(r.get("id")), "por": "correo"}
        # 2) por nombre de empresa (normalizado; uno contenido en el otro, evita variaciones SA/SA de CV)
        if nombre:
            n = _norm_nombre(nombre)
            if len(n) >= 6:
                data = _onecrm_get("data/Account", {"filter_text": nombre, "max_num": 20})
                for r in (data.get("records", []) or []):
                    cn = _norm_nombre(r.get("name", ""))
                    if cn and (cn == n or (len(min(cn, n, key=len)) >= 8 and (n in cn or cn in n))):
                        return {"id": r.get("id"), "nombre": r.get("name", ""), "url_crm": _url(r.get("id")), "por": "nombre"}
    except Exception as e:
        log.warning(f"_buscar_cuenta_existente falló: {e}")
    return None


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
    stream_id: str = "",
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
    # DEDUPE: si la cuenta ya existe (por correo o nombre), NO crear duplicado — devolver la existente.
    _ex = _buscar_cuenta_existente(nombre, email)
    if _ex:
        return {
            "ok": True, "ya_existe": True, "id": _ex["id"], "nombre": _ex["nombre"],
            "url_crm": _ex["url_crm"],
            "mensaje": f"La cuenta '{_ex['nombre']}' YA EXISTE en 1CRM (coincidencia por {_ex['por']}). "
                       f"No la dupliqué. Usa este cuenta_id para el contacto/oportunidad.",
        }
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
    if acct_id:
        _notif(stream_id, f"✅ Alta de cuenta — {nombre}")
    return {
        "ok":      bool(acct_id),
        "id":      acct_id,
        "nombre":  nombre,
        "url_crm": f"{ONECRM_BASE}/index.php?module=Accounts&action=DetailView&record={acct_id}" if acct_id else "",
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
    stream_id: str = "",
) -> dict:
    """Crea un contacto (persona) en 1CRM, opcionalmente ligado a una cuenta/empresa.
    whatsapp se guarda en phone_mobile (1CRM no tiene campo de WhatsApp dedicado).
    cuenta_id liga el contacto a su empresa mediante primary_account_id.
    """
    if not ONECRM_BASE:
        return {"error": "1CRM no configurado"}
    # DEDUPE: si ya existe un contacto con ese correo, no duplicar — devolver el existente.
    if email:
        try:
            data = _onecrm_get("data/Contact", {"filter_text": email, "max_num": 20})
            for r in (data.get("records", []) or []):
                if email.lower() in ((r.get("email1", "") or "").lower(), (r.get("email2", "") or "").lower()):
                    rid = r.get("id")
                    return {
                        "ok": True, "ya_existe": True, "id": rid,
                        "nombre": f"{r.get('first_name','')} {r.get('last_name','')}".strip(),
                        "cuenta_id": r.get("primary_account_id", "") or cuenta_id,
                        "url_crm": f"{ONECRM_BASE}/index.php?module=Contacts&action=DetailView&record={rid}",
                        "mensaje": "El contacto con ese correo YA EXISTE — no lo dupliqué.",
                    }
        except Exception as e:
            log.warning(f"dedupe contacto falló: {e}")
    payload: dict = {"first_name": nombre, "last_name": apellido or nombre}
    if cuenta_id:    payload["primary_account_id"] = cuenta_id
    if email:        payload["email1"] = email
    if whatsapp:     payload["phone_mobile"] = whatsapp
    if telefono:     payload["phone_work"] = telefono
    if cargo:        payload["title"] = cargo
    if descripcion:  payload["description"] = descripcion
    resp = _onecrm_post("data/Contact", payload)
    contacto_id = resp.get("id", "")
    if contacto_id:
        _notif(stream_id, f"✅ Contacto creado — {f'{nombre} {apellido}'.strip()}")
    return {
        "ok":         bool(contacto_id),
        "id":         contacto_id,
        "nombre":     f"{nombre} {apellido}".strip(),
        "cuenta_id":  cuenta_id,
        "url_crm":    f"{ONECRM_BASE}/index.php?module=Contacts&action=DetailView&record={contacto_id}" if contacto_id else "",
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
    def _traducir(prod):
        """Traduce descripción + características al INGLÉS con Haiku (rápido), para NO cargar la
        llamada principal del chat (que tiene el system prompt gigante). Fallback: datos originales."""
        if not prod or not prod.get("ok"):
            return prod
        prod["caracteristicas"] = (prod.get("caracteristicas") or [])[:8]  # menos specs = más rápido
        nombre = prod.get("nombre") or ""
        desc = prod.get("descripcion") or ""
        carac = prod.get("caracteristicas") or []
        if not nombre and not desc and not carac:
            return prod
        try:
            payload = json.dumps({"nombre": nombre, "descripcion": desc, "caracteristicas": carac}, ensure_ascii=False)
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1600,
                timeout=25,
                system=(
                    "Recibes un JSON de producto (nombre, descripcion, caracteristicas). Haz DOS cosas:\n"
                    "1) Traduce TODO al INGLÉS. Conserva intactos part numbers, códigos, números y unidades "
                    "(420 bar, 18.58 kg, R900938249, ISO 7368, NBR, M12×1, IP67, PNP).\n"
                    "2) Si la 'descripcion' trae specs técnicas embebidas como pares 'Etiqueta: valor' "
                    "(p.ej. 'Dimension: 32 x 20 x 8 mm, Range: 5 mm, Switching output: PNP NO, "
                    "Housing material: Stainless steel, Connection: Cable with connector, M12×1-Male, 4-pin'), "
                    "SEPÁRALAS: mételas en 'caracteristicas' como arreglo de strings 'Label: value' (una por spec, "
                    "respetando valores que llevan comas), y deja en 'descripcion' SOLO la descripción general del "
                    "producto (la parte introductoria), SIN la lista de specs y SIN líneas de precio/'list price'.\n"
                    "Combina con las caracteristicas que ya existan, sin duplicar.\n"
                    "Responde SOLO JSON válido con las claves: nombre (string), descripcion (string), "
                    "caracteristicas (array de strings)."),
                messages=[{"role": "user", "content": payload}],
            )
            txt = resp.content[0].text if resp.content else ""
            m = _re.search(r'\{[\s\S]*\}', txt)
            if m:
                data = json.loads(m.group(0))
                if data.get("nombre"):         prod["nombre"] = data["nombre"]
                if data.get("descripcion"):    prod["descripcion"] = data["descripcion"]
                if isinstance(data.get("caracteristicas"), list) and data["caracteristicas"]:
                    prod["caracteristicas"] = [str(c) for c in data["caracteristicas"]][:14]
        except Exception as e:
            log.warning(f"Traducción de producto falló, se usa original: {e}")
        return prod

    def _parse_llm(html: str):
        """Fallback cuando el sitio NO expone datos estructurados (JSON-LD/og), como Futek: extrae
        los campos del producto con Haiku a partir del título, h1, meta description, texto visible y
        candidatos de imagen. Devuelve el mismo shape que _parse, o None."""
        try:
            from urllib.parse import urljoin
            _clean = _re.sub(r'(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>', ' ', html)
            _t = _re.search(r'<title[^>]*>(.*?)</title>', html, _re.S | _re.I)
            title = _re.sub(r'<[^>]+>', ' ', _t.group(1)).strip() if _t else ""
            _h = _re.search(r'<h1[^>]*>(.*?)</h1>', html, _re.S | _re.I)
            h1 = _re.sub(r'<[^>]+>', ' ', _h.group(1)).strip() if _h else ""
            _md = _re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', html, _re.I)
            mdesc = _md.group(1) if _md else ""
            cands = []
            _og = _re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, _re.I)
            if _og:
                cands.append(urljoin(url, _og.group(1)))
            for m in _re.finditer(r'<(?:img|source)[^>]+(?:src|data-src|data-original|data-lazy|srcset)=["\']([^"\']+)', html, _re.I):
                u = m.group(1).split()[0]
                if not u or _re.search(r'(icon|logo|sprite|placeholder|avatar|flag|\.svg|1x1|blank)', u, _re.I):
                    continue
                cands.append(urljoin(url, u))
            _seen: set = set()
            cands = [c for c in cands if not (c in _seen or _seen.add(c))][:20]
            visible = _re.sub(r'\s+', ' ', _re.sub(r'<[^>]+>', ' ', _clean)).strip()[:12000]
            payload = json.dumps({"url": url, "title": title, "h1": h1, "meta_description": mdesc,
                                  "image_candidates": cands, "page_text": visible}, ensure_ascii=False)
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=1500, timeout=30,
                system=(
                    "Extrae los datos de UN producto industrial de la página. Responde SOLO JSON con las claves: "
                    "nombre, part_number, marca, precio_costo (''si no hay precio visible), moneda, descripcion "
                    "(general), caracteristicas (array 'Etiqueta: valor'), imagen_url. REGLAS: part_number del "
                    "título o de la URL; marca = fabricante (p.ej. Futek); NO inventes precio si la página no lo "
                    "muestra (deja ''); imagen_url = la FOTO principal del producto elegida de image_candidates "
                    "(NUNCA iconos ni logos); si ninguna candidata es la foto del producto, deja imagen_url ''. "
                    "Conserva part numbers, códigos, números y unidades intactos."),
                messages=[{"role": "user", "content": payload}],
            )
            m = _re.search(r'\{[\s\S]*\}', resp.content[0].text if resp.content else "")
            if not m:
                return None
            d = json.loads(m.group(0))
            if not d.get("nombre"):
                return None
            log.info(f"_parse_llm extrajo: {d.get('nombre','')[:50]} | img={'sí' if d.get('imagen_url') else 'no'}")
            return {
                "ok": True, "url": url, "nombre": d.get("nombre", ""), "marca": d.get("marca", ""),
                "part_number": d.get("part_number", ""), "precio_costo": d.get("precio_costo", "") or "",
                "moneda": d.get("moneda", "") or "", "descripcion": (d.get("descripcion", "") or "")[:600],
                "caracteristicas": [str(c) for c in (d.get("caracteristicas") or [])][:14],
                "imagen_url": d.get("imagen_url", "") or "",
            }
        except Exception as e:
            log.warning(f"_parse_llm falló: {e}")
            return None

    scraper_tmpl = os.environ.get("SCRAPER_API_URL", "").strip()

    if scraper_tmpl:
        # Intento 1 — RÁPIDO (sin render): solo si el template traía render activado
        fast_tmpl = scraper_tmpl.replace("browser=true", "browser=false").replace("render_js=true", "render_js=false")
        if fast_tmpl != scraper_tmpl:
            r = _fetch(fast_tmpl.replace("{url}", quote_plus(url)), 30)
            if r is not None and r.status_code == 200:
                parsed = _parse(r.text) or _parse_llm(r.text)  # fallback LLM (Futek y sitios sin schema)
                if parsed:
                    return _traducir(parsed)
        # Intento 2 — CON RENDER (lento pero pasa sitios JS/anti-bot como Festo)
        r = _fetch(scraper_tmpl.replace("{url}", quote_plus(url)), 90)
        if r is None:
            return {"error": "El scraper no respondió a tiempo. Reintenta o pega los datos manualmente."}
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code} — el sitio bloquea el acceso. Pega los datos manualmente."}
        parsed = _parse(r.text) or _parse_llm(r.text)  # fallback LLM para sitios sin schema (Futek)
        return _traducir(parsed) if parsed else {"error": "No encontré datos de producto en el link (¿es una página de producto?)."}

    # Sin scraper: fetch directo
    r = _fetch(url, 25, headers=hdrs)
    if r is None:
        return {"error": "No se pudo acceder al link."}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} — el sitio bloquea el acceso automático "
                         f"(sin SCRAPER_API_URL configurado). Pega los datos manualmente."}
    parsed = _parse(r.text) or _parse_llm(r.text)  # fallback LLM para sitios sin schema (Futek)
    return _traducir(parsed) if parsed else {"error": "No encontré datos de producto en el link (¿es una página de producto?)."}


def tool_extraer_producto_de_link(url: str) -> dict:
    """Extrae nombre, marca, part number, precio del proveedor, descripción e imagen de la
    página de un producto (para publicarlo en 1CRM desde un link)."""
    return _extraer_producto_link(url)


def _ajustar_imagen_500(content: bytes) -> bytes:
    """Ajusta cualquier imagen a un lienzo cuadrado de 500x500 px sobre fondo blanco (sin deformar
    el aspect ratio) — tamaño estándar para las fotos de producto que se suben a 1CRM. Devuelve
    siempre PNG."""
    from PIL import Image
    import io as _io
    im = Image.open(_io.BytesIO(content))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im).convert("RGB")
    else:
        im = im.convert("RGB")
    im.thumbnail((500, 500), Image.LANCZOS)
    lienzo = Image.new("RGB", (500, 500), (255, 255, 255))
    lienzo.paste(im, ((500 - im.width) // 2, (500 - im.height) // 2))
    buf = _io.BytesIO()
    lienzo.save(buf, format="PNG")
    return buf.getvalue()


def tool_extraer_ficha_pdf(url: str) -> dict:
    """Extrae el/los producto(s) de una ficha técnica (PDF/Excel/Word) por URL: nombre, marca,
    part number, descripción y características técnicas de cada variante/SKU encontrado. Si el PDF
    trae una foto embebida (p.ej. la portada), se re-hospeda en Supabase Storage y se asigna como
    imagen_url a todos los productos devueltos. Import perezoso para no romper el chat si falta
    alguna dependencia de parsing en el entorno."""
    try:
        import ficha_tecnica
    except Exception as e:
        return {"error": f"módulo de ficha técnica no disponible: {e}"}

    datos = ficha_tecnica.leer_ficha(url=url)
    b64  = datos.pop("imagen_b64", "")
    datos.pop("imagen_ext", "")
    if b64:
        try:
            import base64 as _b64mod
            content = _ajustar_imagen_500(_b64mod.b64decode(b64))
            path = f"ficha/{uuid.uuid4().hex[:10]}.png"
            supabase.storage.from_("product-images").upload(
                path=path, file=content, file_options={"content-type": "image/png", "upsert": "true"},
            )
            img_url = supabase.storage.from_("product-images").get_public_url(path)
            for p in datos.get("productos", []):
                p["imagen_url"] = img_url
            log.info(f"Imagen extraída del PDF, ajustada a 500x500 y re-hospedada: {img_url[:70]}")
        except Exception as e:
            log.warning(f"No se pudo subir la imagen extraída del PDF: {e}")
    return datos


def _rehost_imagen(imagen_url: str, part_number: str = "") -> str:
    """Descarga la imagen y la re-hospeda en Supabase Storage, para que el publicador pueda
    subirla a 1CRM. Sitios como Festo bloquean la descarga directa desde IPs de datacenter
    (Railway) → si el directo falla, se reintenta vía ScrapingAnt (proxy residencial). Si todo
    falla, devuelve la URL original (el publicador hará su propio fallback)."""
    if not imagen_url:
        return imagen_url
    from urllib.parse import quote_plus

    def _dl(via_scraper: bool):
        try:
            if via_scraper:
                sc = os.environ.get("SCRAPER_API_URL", "").strip()
                if not sc:
                    return None
                fetch = sc.replace("browser=true", "browser=false").replace("render_js=true", "render_js=false")
                return httpx.get(fetch.replace("{url}", quote_plus(imagen_url)), timeout=40)
            return httpx.get(imagen_url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        except Exception:
            return None

    def _ok(r):
        return r is not None and r.status_code == 200 and "image" in r.headers.get("content-type", "").lower()

    try:
        r = _dl(False)
        if not _ok(r):
            r = _dl(True)  # bloqueado → proxy residencial
        if not _ok(r):
            return imagen_url
        content_type = r.headers.get("content-type", "image/jpeg").lower()
        try:
            # Ajusta a 500x500 (tamaño estándar de foto de producto) y normaliza a PNG de paso
            # (1CRM no acepta WebP/AVIF/etc y el publicador los subiría con extensión errónea).
            content = _ajustar_imagen_500(r.content)
            ct, ext = "image/png", "png"
        except Exception as _e_conv:
            log.warning(f"No se pudo ajustar la imagen a 500x500 ({content_type}): {_e_conv}")
            content, ct = r.content, content_type
            ext = "png" if "png" in content_type else "jpg"
        safe = (part_number or "producto").replace("/", "-").replace(" ", "_")
        path = f"link/{safe}_{str(uuid.uuid4())[:6]}.{ext}"
        supabase.storage.from_("product-images").upload(
            path=path, file=content, file_options={"content-type": ct, "upsert": "true"},
        )
        url = supabase.storage.from_("product-images").get_public_url(path)
        log.info(f"Imagen re-hospedada en Supabase: {url[:70]}")
        return url
    except Exception as e:
        log.warning(f"No se pudo re-hospedar la imagen, se usa la original: {e}")
        return imagen_url


_HEARTBEAT_MAX_MIN = 3  # el servicio de workers late cada 60s; 3 min sin latido = dormido/caído


def _workers_despiertos() -> tuple[bool, str]:
    """(despiertos, cuánto llevan dormidos). Lee el latido que escribe el servicio de workers.

    Railway duerme el servicio cuando no se usa (días sin actividad). Dormido, los jobs se encolan
    pero NADIE los procesa: falla silenciosa. Esto permite avisarle al usuario ANTES de trabajar.
    Ante cualquier duda devuelve True (no estorbar el flujo por un problema de lectura).
    """
    try:
        r = (supabase.table("resource_status").select("actualizado_en")
             .eq("servicio", "workers").eq("metrica", "heartbeat").limit(1).execute())
        if not r.data:
            return True, ""  # nunca ha latido (función nueva / tabla vacía) → NO alarmar en falso
        ts = datetime.fromisoformat(str(r.data[0]["actualizado_en"]).replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        if delta < timedelta(minutes=_HEARTBEAT_MAX_MIN):
            return True, ""
        mins = int(delta.total_seconds() // 60)
        if mins < 120:
            return False, f"{mins} min"
        horas = mins // 60
        return False, (f"{horas} h" if horas < 48 else f"{horas // 24} días")
    except Exception:
        return True, ""


def _aviso_workers_dormidos(detalle: str, clean_stream=None) -> str:
    """Aviso determinista (no depende de que el modelo se acuerde) de que la cola no se va a mover."""
    txt = (f"Los workers llevan {detalle} sin dar señales — Railway los duerme cuando el servicio no se usa. "
           f"El trabajo QUEDA ENCOLADO pero NO se va a procesar hasta que reactives el servicio en Railway.")
    try:
        tool_notificar_sistema("⚠️ Workers dormidos — la cola no se va a procesar", txt,
                               tipo="sistema", stream_id=str(clean_stream or ""))
    except Exception:
        pass
    return txt


_EN_CURSO_MIN = 5  # una publicación real tarda <1-2 min; más allá = job atorado, no "publicado"


def _estado_publicacion_reciente(part_number: str, clean_stream, minutos: int = 30) -> str:
    """Guardia anti-doble-publicación, PERO sin mentir. Devuelve:
      'publicado' → ya se publicó DE VERDAD en este stream hace poco (bloquea).
      'en_curso'  → se está publicando AHORITA (job vivo, < _EN_CURSO_MIN) (bloquea).
      ''          → libre. Incluye el caso de un rfq ATORADO en 'publicando' (el worker nunca
                    tomó el job): eso NO se reporta como publicado y SÍ se deja reintentar.
    """
    if not part_number or not clean_stream:
        return ""
    try:
        corte = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
        r = (supabase.table("rfqs").select("id,estado,created_at")
             .eq("stream_id", clean_stream).eq("modelo", part_number)
             .in_("estado", ["publicando", "publicado"])
             .gte("created_at", corte).order("created_at", desc=True).limit(1).execute())
        if not r.data:
            return ""
        row = r.data[0]
        if (row.get("estado") or "") == "publicado":
            return "publicado"
        # 'publicando': solo cuenta si es reciente; si lleva rato, el job se atoró → permitir reintento.
        try:
            creado = datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - creado) < timedelta(minutes=_EN_CURSO_MIN):
                return "en_curso"
        except Exception:
            pass
        return ""
    except Exception:
        return ""


def _publicar_producto_uno(
    bulk_id: str, clean_stream, nombre: str, part_number: str, marca: str = "",
    descripcion: str = "", caracteristicas: list | None = None, precio_costo: float = 0,
    imagen_url: str = "", url_origen: str = "",
) -> dict:
    """Crea el rfq + job del publicador para UN producto extraído de link, bajo el bulk_id dado.
    Lo comparten el flujo de 1 link y el de N links (bulk). Devuelve {rfq_id, job_id, nombre_crm}."""
    # Nombre para 1CRM en el ORDEN: modelo (part number) / marca / descripción (nombre).
    # (se omiten partes vacías o repetidas — p.ej. si el nombre extraído == part number).
    _seen: set = set()
    _partes: list = []
    for _p in (part_number, marca, nombre):
        _p = (_p or "").strip()
        if _p and _p.lower() not in _seen:
            _seen.add(_p.lower())
            _partes.append(_p)
    nombre_crm = " / ".join(_partes) if _partes else (nombre or part_number or "Producto")

    # Descripción completa para 1CRM = descripción + ficha técnica (características)
    desc_full = descripcion or nombre
    if caracteristicas:
        desc_full += "\n\nFicha técnica:\n" + "\n".join(f"• {c}" for c in caracteristicas)
    now = datetime.now(timezone.utc)
    rfq_id_str = f"LINK-{now.year}-{now.month:02d}{now.day:02d}-{str(uuid.uuid4())[:6].upper()}"
    # Re-hospedar la imagen en Supabase (sitios como Festo bloquean la descarga directa del
    # publicador) para que sí se suba a 1CRM.
    foto_final = _rehost_imagen(imagen_url, part_number) if imagen_url else None
    rfq_row: dict = {
        "stream_id": clean_stream, "rfq_id": rfq_id_str,
        "modelo": part_number or nombre, "marca": marca or "",
        "estado": "publicando", "foto_url": foto_final, "bulk_id": bulk_id,
    }
    try:
        rfq_resp = supabase.table("rfqs").insert(rfq_row).execute()
    except Exception:
        rfq_row["stream_id"] = None
        rfq_resp = supabase.table("rfqs").insert(rfq_row).execute()
    rfq_id = rfq_resp.data[0]["id"]
    job = supabase.table("jobs").insert({
        "rfq_id": rfq_id, "agente": "publicador", "estado": "pendiente",
        "created_at": now.isoformat(),
        "input": {"origen": "link", "url": url_origen,
                  "ficha": {"nombre": nombre_crm, "descripcion": desc_full,
                            "cost": float(precio_costo or 0), "list_price": 0}},
    }).execute()
    job_id = (job.data or [{}])[0].get("id", "?")
    return {"rfq_id": rfq_id, "job_id": job_id, "nombre_crm": nombre_crm}


def _crear_notificacion_bulk(clean_stream, bulk_id: str, nombres: list, rfq_id) -> None:
    """Notificación tipo='bulk' → el frontend renderiza el BulkWidget (tarjeta negra con "Ver en
    CRM"). Sirve para 1 o N productos (el widget lee todos los rfqs con ese bulk_id)."""
    try:
        total = len(nombres)
        titulo = f"📦 Publicando {nombres[0]}" if total == 1 else f"📦 Publicando {total} productos"
        lista = "\n".join(f"• {n}" for n in nombres)
        supabase.table("notificaciones").insert({
            "tipo": "bulk", "titulo": titulo,
            "mensaje": json.dumps({"bulk_id": bulk_id, "lista": lista, "total": total}),
            "rfq_id": rfq_id, "stream_id": clean_stream, "leida": False,
        }).execute()
    except Exception as e:
        log.warning(f"No se pudo crear notificación bulk del link: {e}")


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
    """Publica en 1CRM UN producto extraído de un link. El precio_costo (del proveedor) va al
    campo INTERNO 'cost' (no se expone al público); el precio de venta (list_price) queda en 0
    para definirlo después. Reutiliza el pipeline del publicador y su widget producto_publicado."""
    try:
        clean_stream = stream_id if (stream_id and stream_id not in ("None", "")) else None
        _est = _estado_publicacion_reciente(part_number, clean_stream)
        if _est == "publicado":
            return {"error": f"El producto {part_number or nombre} YA está publicado en este stream (verificado en la base). "
                             f"No lo republico. Si de verdad quieres otra copia, dímelo explícitamente."}
        if _est == "en_curso":
            return {"error": f"El producto {part_number or nombre} se está publicando en este momento. "
                             f"Espera a que termine — todavía NO está en el CRM."}
        bulk_id = str(uuid.uuid4())  # bulk de 1 producto → dispara el BulkWidget (widget "Ver en CRM")
        res = _publicar_producto_uno(bulk_id, clean_stream, nombre, part_number, marca,
                                     descripcion, caracteristicas, precio_costo, imagen_url, url_origen)
        _crear_notificacion_bulk(clean_stream, bulk_id, [nombre], res["rfq_id"])
        log.info(f"Job publicador (link) creado: {res['job_id']} para '{nombre}'")
        out = {"ok": True, "rfq_id": res["rfq_id"], "job_publicador": res["job_id"], "nombre": nombre}
        _vivos, _det = _workers_despiertos()
        if not _vivos:
            out["AVISO_CRITICO"] = (f"{_aviso_workers_dormidos(_det, clean_stream)} "
                                    f"DÍSELO AL USUARIO de forma clara y visible: el producto NO está publicado todavía.")
        return out
    except Exception as e:
        return {"error": str(e)}


def tool_publicar_productos_desde_links(productos: list | None = None, stream_id: str = "") -> dict:
    """Publica en 1CRM VARIOS productos extraídos de links, en UN SOLO BulkWidget (bulk_id
    compartido). `productos` es una lista de objetos con las MISMAS claves que publicar_producto_link
    (nombre, part_number, marca, descripcion, caracteristicas, precio_costo, imagen_url, url_origen)."""
    try:
        if not productos or not isinstance(productos, list):
            return {"error": "productos vacío o no es una lista"}
        clean_stream = stream_id if (stream_id and stream_id not in ("None", "")) else None
        bulk_id = str(uuid.uuid4())
        nombres: list = []
        first_rfq = None
        ok = 0
        errores: list = []
        for p in productos:
            if not isinstance(p, dict):
                continue
            _est = _estado_publicacion_reciente(p.get("part_number", ""), clean_stream)
            if _est:
                _que = "ya está publicado" if _est == "publicado" else "se está publicando en este momento"
                errores.append(f"{p.get('part_number') or p.get('nombre') or '?'}: {_que}, omitido")
                continue
            try:
                res = _publicar_producto_uno(
                    bulk_id, clean_stream,
                    p.get("nombre", ""), p.get("part_number", ""), p.get("marca", ""),
                    p.get("descripcion", ""), p.get("caracteristicas"),
                    p.get("precio_costo", 0), p.get("imagen_url", ""), p.get("url_origen", ""),
                )
                nombres.append(p.get("nombre") or p.get("part_number") or "Producto")
                first_rfq = first_rfq or res["rfq_id"]
                ok += 1
            except Exception as e:
                errores.append(f"{p.get('part_number') or p.get('nombre') or '?'}: {e}")
        if ok == 0:
            return {"error": "no se pudo publicar ninguno", "detalles": errores}
        _crear_notificacion_bulk(clean_stream, bulk_id, nombres, first_rfq)
        _vivos, _det = _workers_despiertos()
        if not _vivos:
            errores.append(f"AVISO_CRITICO: {_aviso_workers_dormidos(_det, clean_stream)} "
                           f"Ninguno de los {ok} productos está publicado todavía — DÍSELO AL USUARIO.")
        log.info(f"Bulk link: {ok}/{len(productos)} publicados, bulk_id={bulk_id}, errores={len(errores)}")
        return {"ok": True, "publicados": ok, "total": len(productos), "bulk_id": bulk_id, "errores": errores}
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
    _vivos, _det = _workers_despiertos()
    if not _vivos and creados:
        result["AVISO_CRITICO"] = (f"{_aviso_workers_dormidos(_det, clean_stream_id)} "
                                   f"Las búsquedas NO van a arrancar — DÍSELO AL USUARIO antes de que espere en vano.")
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
        "name": "extraer_ficha_pdf",
        "description": "Extrae el/los producto(s) (nombre, marca, part number, descripción, características técnicas) de una ficha técnica en PDF/Excel/Word por URL. Si el documento describe varias variantes/SKU del mismo producto (o accesorios con su propio Item No.), devuelve UNA entrada por cada uno. Usar cuando el usuario comparte el link de un datasheet para publicarlo en 1CRM.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL del PDF/Excel/Word de la ficha técnica"},
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
        "name": "publicar_productos_desde_links",
        "description": "Publica en 1CRM VARIOS productos (extraídos de links) de una sola vez, en UN SOLO widget/lote compartido. Úsalo cuando el usuario pega MÚLTIPLES links (o vienen de un .txt/screenshot) y ya aprobó publicarlos. Cada item usa las mismas claves que publicar_producto_link. El stream_id se inyecta automáticamente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "productos": {
                    "type": "array",
                    "description": "Lista de productos ya extraídos a publicar juntos",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nombre":          {"type": "string"},
                            "part_number":     {"type": "string"},
                            "marca":           {"type": "string"},
                            "descripcion":     {"type": "string"},
                            "caracteristicas": {"type": "array", "items": {"type": "string"}},
                            "precio_costo":    {"type": "number"},
                            "imagen_url":      {"type": "string"},
                            "url_origen":      {"type": "string"},
                        },
                        "required": ["nombre", "part_number"],
                    },
                },
            },
            "required": ["productos"],
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
    "extraer_ficha_pdf":         tool_extraer_ficha_pdf,
    "publicar_producto_link":    tool_publicar_producto_link,
    "publicar_productos_desde_links": tool_publicar_productos_desde_links,
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

ENFOQUE DEL STREAM DE CORREO — tu ÚNICO trabajo aquí es llevar la oportunidad del correo hasta crearla en \
el CRM: (a) detectar la oportunidad, (b) CONSEGUIR los datos faltantes, (c) dar de alta cuenta/contacto si \
hace falta, y (d) crear la oportunidad. No te desvíes a otros temas ni des consejos generales; si el usuario \
pregunta algo fuera de este flujo, respóndele en una línea y regresa al objetivo.

AVISOS AL USUARIO (usa notificar_sistema — breve, en CADA transición, para que sepa qué está pasando):
- Oportunidad detectada pero INCOMPLETA → avisa de quién es, qué dato(s) falta(n) y que ya la estás \
  consiguiendo. Ej: notificar_sistema(titulo "🔔 Oportunidad — Aceros del Norte", mensaje "Falta dirección de \
  envío; enviando solicitud al cliente").
- Cuando YA conseguiste el dato faltante (por respuesta del prospecto o porque el usuario del chat te lo dio) \
  → avisa que ya está completa. Ej: "✅ Ya tengo los datos de Aceros del Norte; lista para crear".
- Al dar de alta CUENTA/CONTACTO → avisa. Ej: "✅ Alta de Aceros del Norte (cuenta + contacto)".
- Al crear la OPORTUNIDAD → avisa. Ej: "✅ Oportunidad creada — Aceros del Norte".
- Si el RFQ venía COMPLETO desde el correo → crea directo (con su [DECISION]) y avisa que se creó.

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

2.5. FIRMA DEL CORREO (obligatorio revisarla): los correos suelen traer una FIRMA al final con datos valiosos: \
   nombre completo, cargo, EMPRESA, TELÉFONO, DIRECCIÓN, sitio web, correo corporativo. EXTRÁELOS SIEMPRE y úsalos: \
   - Para COTEJAR con el CRM: busca con buscar_clientes_crm por el NOMBRE DE LA EMPRESA y por el TELÉFONO / correo \
     corporativo de la firma — NO solo por el dominio del remitente (clave cuando el prospecto escribe desde un \
     gmail/hotmail genérico: la firma revela la empresa real y ahí sí puede coincidir con una cuenta existente). \
     Si coincide con una cuenta/contacto del CRM, ES cliente conocido → usa ese cuenta_id. \
   - Para COMPLETAR los 5 datos obligatorios: la empresa, el nombre del contacto, la dirección de envío y el \
     teléfono a menudo están EN LA FIRMA. Toma esos datos como válidos y complétalos con ellos antes de pedir nada.

3. SI FALTA CUALQUIER BLOQUE (tras revisar cuerpo + firma + cotejo CRM) → NO crees nada. Haz estas 3 cosas, en orden:

   (a) AVISA al usuario del chat que la oportunidad está INCOMPLETA y enumera exactamente qué bloque(s) faltan. \
       Ejemplo: "⚠️ La oportunidad de <remitente/cuenta> está incompleta. Faltan: cantidad (Qty) y dirección de envío."

   (b) REDACTA un correo de respuesta (mismo hilo, thread_id del email) pidiendo ÚNICAMENTE los datos faltantes, \
       y muéstraselo al usuario del chat como borrador. Reglas de redacción OBLIGATORIAS: \
       - TRATO: de TÚ (cercano pero profesional). NUNCA uses "usted", "le", "les". \
       - PERSONA: primera persona SINGULAR — "yo estoy buscando", "te notifico", "te agradezco". \
         NUNCA "estamos", "les confirmamos", "quedamos a sus órdenes", "nosotros". \
       - TONO: directo y cálido, que transmita que ya se está avanzando. \
       - SIEMPRE confirma que YA estás buscando su RFQ antes de pedir datos. \
       - CRÍTICO — NO CONDICIONAR: el avance NUNCA depende de que el prospecto envíe sus datos. \
         PROHIBIDAS las frases que condicionen o presionen como "para poder avanzar", "para continuar", \
         "para procesar necesitamos", "una vez que me envíes", "en cuanto reciba podré". \
         Los datos se piden de forma SUAVE (ej. "para tener tu cotización cuanto antes"), nunca como requisito. \
       - Si el remitente NO es cliente en el CRM, menciónalo positivo y ligero: que aún no tienes su registro \
         y con gusto lo das de alta — sin condicionar el avance a ello. \
       - PROHIBIDO: NO menciones la palabra "cotización" ni prometas cotizar todavía, y NO des precios. \
       IDIOMA: detecta el idioma del correo ORIGINAL del cliente y redacta la respuesta en ESE mismo idioma \
       (si el RFQ llegó en inglés, contesta en inglés; si en portugués, en portugués; etc.). \
       La plantilla de abajo está en español SOLO como modelo — tradúcela al idioma del cliente \
       conservando el trato de tú, primera persona singular, tono positivo y carácter NO condicionante:

       "Estimado/a [Nombre]

       Gracias por contactarnos, ya estoy buscando tu RFQ. Te notifico en cuanto tenga precio y disponibilidad.

       Para tener tu cotización cuanto antes, te agradezco me compartas los siguientes datos:

       [dato faltante 1]: ______________________________
       [dato faltante 2]: ______________________________

       Quedo pendiente de la información.
       Saludos cordiales"

       FORMATO CRÍTICO DE LOS DATOS FALTANTES: SIEMPRE usa "Nombre del campo: ______________________________" \
       (una línea por campo con guiones bajos al final). NUNCA uses viñetas, bullets, guiones (-) ni listas. \
       Los guiones bajos son la señal visual de que hay que escribir ahí. Esto aplica en CUALQUIER idioma: \
       en inglés sería "Quantity needed: ______________________________" (no "- Quantity needed"). \

       Si el remitente SÍ es cliente existente en el CRM, omite cualquier mención de registro/alta y solo pide \
       de forma suave (tú, primera persona, no condicionante) el dato faltante, manteniendo tono positivo.

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
   COTEJA SIEMPRE con el CRM antes de decidir, para NO duplicar clientes: usa buscar_clientes_crm buscando \
   por el CORREO del contacto Y por el NOMBRE DE LA EMPRESA (no solo el dominio). \
   - Si encuentras la cuenta (por correo o por nombre) → YA ES CLIENTE: usa ESE cuenta_id (no crees otra). \
   - Si hay una coincidencia PARCIAL o dudosa (nombre parecido pero no idéntico) → NO asumas: confírmalo con el \
     usuario ("¿<empresa> es la misma que <cuenta encontrada> en el CRM?") antes de crear. \
   - Nota: las tools crear_cuenta_crm/crear_contacto_crm ya tienen protección — si el cliente/contacto ya existe \
     devuelven "ya_existe": true con su id; eso NO es error, usa ese id para ligar la oportunidad.

   4a. YA ES CLIENTE Y EL CONTACTO YA EXISTE (cuenta + contacto en CRM, y no falta ningún dato obligatorio): \
       termina con [DECISION: ¿Creo la oportunidad para <cuenta>?]. Tras el "Sí": crea la oportunidad con \
       crear_oportunidad_crm — descripcion "RFQ: <part-numbers> | Qty: <cantidades>", cuenta_id del CRM. \
       Confirma con el link de la oportunidad creada.

   4a-bis. LA EMPRESA/CUENTA EXISTE PERO EL CONTACTO ES NUEVO (o falta algún dato de la oportunidad): la cuenta \
       ya está en el CRM (coincide por dominio del correo, nombre de empresa o firma), pero esta PERSONA no está \
       como contacto de esa cuenta (verifícalo con ver_contactos_cuenta_crm usando el cuenta_id). Entonces: \
       - Redacta un correo BREVE y cordial al remitente (trato de TÚ, primera persona singular, mismo idioma, \
         positivo, NO condicionante) confirmando su alta como CONTACTO ADICIONAL de su empresa y/o pidiendo \
         SOLO el dato que falte. Ej: "Estimado/a <nombre>: ya estoy buscando tu RFQ. Ya tenemos registrada a \
         <empresa>; con gusto te doy de alta como contacto adicional. Para tener todo listo, te agradezco me \
         confirmes <dato faltante, p.ej. dirección de envío>: ______. Quedo pendiente. Saludos cordiales." \
       - Termina con [DECISION: ¿Envío la confirmación a <remitente>?]. (Si el usuario del chat ya te dio lo que \
         faltaba, en su lugar pide: [DECISION: ¿Doy de alta a <nombre> como contacto de <empresa> y creo la \
         oportunidad?].) \
       - Tras el "Sí": crea el contacto con crear_contacto_crm ligado al cuenta_id EXISTENTE (NUNCA crees otra \
         cuenta), y luego la oportunidad ligada a esa misma cuenta. Avisa con notificar_sistema del alta del \
         contacto y de la oportunidad creada.

   4b. NO ES CLIENTE (no hay cuenta en CRM): hay que DAR DE ALTA al cliente primero. Sigue el MODO 12 (alta \
       inicial, baja fricción): basta con empresa, contacto, correo y dirección de envío — SIN datos fiscales. \
       Créalo con [DECISION], y en cuanto la cuenta exista crea la oportunidad ligada con crear_oportunidad_crm \
       (cuenta_id de la cuenta recién creada, descripcion "RFQ: <part-numbers> | Qty: <cantidades>").

FORMATO DE SALIDA (OBLIGATORIO): NO escribas el diagnóstico ni el borrador como prosa/texto. En su lugar \
emite EXACTAMENTE este marcador en UNA línea y con JSON válido (el frontend lo vuelve una tarjeta), y \
DESPUÉS el [DECISION] que corresponda: \
[OPORTUNIDAD]{"es_oportunidad":true,"resumen":"una línea","remitente":"Nombre","correo":"correo@x.com","empresa":"","campos":[{"nombre":"Contacto","ok":true,"valor":"Jesús G"},{"nombre":"Empresa","ok":false,"valor":"no la menciona"},{"nombre":"RFQ + Qty","ok":false,"valor":"'quiero una parte' es ambiguo"},{"nombre":"Correo","ok":true,"valor":"correo@x.com"},{"nombre":"Dirección de envío","ok":false,"valor":"no la incluye"}],"completa":false,"faltan":["empresa","part-number + cantidad","dirección de envío"],"es_cliente":false,"accion":"Pedir datos faltantes","borrador":{"para":"correo@x.com","asunto":"Re: ...","cuerpo":"texto redactado según las reglas de arriba"}} \
- Los 5 campos van SIEMPRE en este orden y con estos nombres: Contacto, Empresa, RFQ + Qty, Correo, Dirección de envío. Cada uno con ok (true/false) y un "valor" corto (el dato o por qué falta). \
- Incompleta → accion "Pedir datos faltantes", incluye el borrador, y termina con [DECISION: ¿Envío esta solicitud de información a <remitente>?]. \
- Completa y ES cliente → borrador null, accion "Crear oportunidad", [DECISION: ¿Creo la oportunidad para <cuenta>?]. \
- Completa y NO es cliente → borrador null, accion "Alta de cliente + oportunidad", [DECISION: ¿Doy de alta a <empresa> y creo la oportunidad?]. \
NO repitas el diagnóstico ni el borrador en texto aparte: la tarjeta ya los muestra.

CRÍTICO: en este modo nunca creas nada en el CRM ni envías correos sin el [DECISION] aprobado por el usuario. \
Si faltan datos, primero se piden; solo con los 4 bloques completos se procede a cotejar y crear.

AL TERMINAR DE CREAR EN EL CRM (oportunidad, y cuenta+contacto si fue cliente nuevo): NO escribas los links en \
texto. Emite EXACTAMENTE este marcador, en UNA línea y con JSON válido (el frontend lo vuelve una tarjeta), usando \
las url_crm que devolvieron las tools: \
[OPORTUNIDAD_CREADA]{"empresa":"Aceros del Norte SA","oportunidad":"Square D Q0120 x20","oportunidad_url":"https://...","cuenta_url":"https://...","contacto":"Juan Pérez","contacto_url":"https://..."} \
Si era cliente existente (no creaste cuenta/contacto), omite cuenta_url/contacto/contacto_url (pon ""). No repitas \
los datos en texto: la tarjeta los muestra.

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

5. DESPUÉS del marcador, procesa las oportunidades de UNA EN UNA — REGLA ESTRICTA, una acción = un visto bueno: \
   - Toma SOLO la PRIMERA oportunidad pendiente (empezando por la #1). NO re-describas sus datos (el widget ya los \
     muestra); emite solo una línea corta identificándola (ej. "Oportunidad 1 · Aceros del Norte:") + su acción: \
     si está completa → [DECISION: ¿Doy de alta y creo la oportunidad para <empresa>?]; si le falta info → el \
     borrador de correo pidiendo SOLO lo faltante (reglas del MODO 10 paso 3) + [DECISION: ¿Envío la solicitud a <remitente>?]. \
   - Y DETENTE AHÍ. NO presentes, menciones ni actúes sobre las demás oportunidades en el mismo turno. UN solo \
     [DECISION] por turno. Espera la respuesta del usuario. \
   - Cuando el usuario apruebe (o rechace), ejecuta ESA acción y confírmala, y HASTA ENTONCES, en el siguiente \
     turno, pasa a la siguiente oportunidad con su propio [DECISION]. \
   PROHIBIDO: hacer dos acciones (crear una y enviar correo de otra) con un solo "Sí". Cada visto bueno aplica a \
   UNA sola acción, la que se mostró justo antes.

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

IDIOMA DEL PRODUCTO: la descripción y las características que devuelve extraer_producto_de_link YA vienen en \
INGLÉS (el tool las traduce). Solo COPIA esos valores tal cual al marcador y a publicar_producto_link — NO los \
vuelvas a traducir ni los reescribas. El nombre también en inglés si el tool lo trae así.

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
   y el widget del sistema (BulkWidget con "Ver en CRM") muestra el producto solo. NO agregues NINGÚN texto después \
   de llamar a publicar_producto_link — nada de "Publicando…" ni "Publicado ✅": sería redundante con el widget. \
   Devuelve exactamente una cadena vacía como respuesta final.

MÚLTIPLES LINKS (bulk): si hay VARIOS links (pegados, en un .txt, o en una imagen), EXTRAE cada uno con \
extraer_producto_de_link (puedes llamarlo varias veces en la MISMA respuesta). Luego emite UN SOLO marcador, \
en UNA sola línea y con JSON válido, con TODOS los productos: \
[PRODUCTOS_PREVIEW]{"productos":[{"nombre":"...","marca":"...","part_number":"...","precio_costo":"...","moneda":"...","descripcion":"...","caracteristicas":["..."],"imagen_url":"...","url_origen":"..."}, {"...otro producto..."}]} \
usando los valores tal cual los devolvió el extractor (copia caracteristicas completas; campo vacío = "" o []). \
Para VARIOS NO uses [PRODUCTO_PREVIEW] singular ni [DECISION]: el widget de [PRODUCTOS_PREVIEW] ya muestra cada \
producto con SU PROPIO botón Publicar. NO publiques todavía y NO agregues texto: el usuario publicará cada uno \
desde el widget. Si algún link falla al extraer, inclúyelo igual con los datos que tengas o menciónalo aparte \
(no inventes datos). \
Cuando el usuario pida publicar UNO del preview (p.ej. "Publica SOLO el producto XYZ"), llama a \
publicar_producto_link con los datos de ESE producto que YA tienes del preview — NO vuelvas a extraer, NO pidas \
confirmación, respuesta final vacía. (Si en cambio pide "publica TODOS", usa publicar_productos_desde_links con \
el arreglo completo.)

IMAGEN ADJUNTA (screenshot): si el mensaje trae una imagen, LEE las URLs de producto COMPLETAS visibles \
en ella (que empiecen con http, en enlaces, texto o tarjetas de preview). En estos screenshots de chat la \
MISMA URL suele aparecer dos veces (como enlace y dentro de la tarjeta de preview) y también hay dominios \
sueltos (p.ej. "www.futek.com"): IGNORA los dominios sueltos y NO proceses una URL repetida más de una vez. \
DEDUPE SOLO cuando la URL completa es IDÉNTICA carácter por carácter. DOS URLs con paths distintos son DOS \
productos DIFERENTES aunque compartan dominio/marca — NUNCA las juntes. Ejemplo: \
".../pressure-sensor-oem-pmp300" y ".../stick-shift-load-cell-mau300/fsh04423" son DOS productos distintos. \
Ante la duda, publica de MÁS (dos productos separados), nunca de menos. Con la lista ya única, trátalas \
EXACTAMENTE como links pegados y sigue el flujo de arriba: si es 1 → [PRODUCTO_PREVIEW] + [DECISION]; si son \
VARIOS → un solo [PRODUCTOS_PREVIEW] con el arreglo (el usuario publica cada uno desde el widget). Si en la \
imagen no hay ninguna URL completa legible, dilo claramente y pide el link en texto — NO inventes ni completes \
URLs a medias.

CRÍTICO — NO REPUBLIQUES NI ACTÚES POR MENSAJES SUELTOS: solo publicas como respuesta INMEDIATA a la \
aprobación ("Sí", "publica", "adelante") de un preview que ACABAS de mostrar y que AÚN NO se ha publicado. \
Un producto ya aprobado/publicado NO se vuelve a publicar. Si el usuario dice algo conversacional o un \
afirmativo suelto ("muy bien", "ok", "gracias", "perfecto", "excelente", "va") y NO hay un preview NUEVO \
esperando su aprobación, es SOLO conversación: responde breve y NO llames a ninguna tool de publicar. Nunca \
uses los datos de un [PRODUCTO_PREVIEW] anterior del historial para publicar de nuevo.

CRÍTICO: nunca publiques sin el [DECISION] aprobado. El precio del proveedor es interno (cost), no público.

MODO 14 — PUBLICAR PRODUCTO DESDE FICHA TÉCNICA (PDF/Excel/Word):
Cuando el usuario comparte la URL de un datasheet/ficha técnica (PDF, Excel o Word, NO una página \
web) para publicar el producto en 1CRM:

1. Extrae DE INMEDIATO con extraer_ficha_pdf (una línea breve antes está bien, ej. "Reviso la ficha, \
   dame un momento…"). NO preguntes antes de extraer.
   - Si devuelve "error" (no se pudo descargar o no se pudo leer el documento): dilo claramente y \
     ofrece que el usuario pegue los datos manualmente. NO publiques con datos inventados.
   - Si el usuario ya especificó QUÉ variante/modelo quiere (p.ej. "publica el Dynamic 2100/16"), y \
     entre los productos devueltos hay UNA sola coincidencia clara con eso: trátalo como el caso de \
     "1 producto" del punto 2, usando SOLO ese producto (ignora el resto de variantes/accesorios \
     que haya devuelto el extractor).

2. Si el resultado (o la coincidencia pedida por el usuario) es 1 SOLO producto: NO listes los datos \
   como texto. Emite EXACTAMENTE este marcador en una sola línea con JSON válido: \
   [PRODUCTO_PREVIEW]{"nombre":"...","marca":"...","part_number":"...","precio_costo":"...","moneda":"...","descripcion":"...","caracteristicas":["...","..."],"imagen_url":""} \
   y termina con [DECISION: ¿Publico este producto en 1CRM?]. Tras el "Sí", llama a \
   publicar_producto_link con esos mismos datos. NO agregues texto después de publicar (el widget ya \
   confirma) — respuesta final vacía.

3. Si el extractor devuelve VARIAS variantes/SKU (y el usuario NO pidió una en específico, o pidió \
   una pero el documento trae más de una que podría aplicar): NUNCA publiques directo — esto es \
   AMBIGUO y hay que preguntar en el stream ANTES de tocar el CRM. Emite UN SOLO marcador con TODOS \
   los productos encontrados: \
   [PRODUCTOS_PREVIEW]{"productos":[{"nombre":"...","marca":"...","part_number":"...","precio_costo":"...","moneda":"...","descripcion":"...","caracteristicas":["..."],"imagen_url":"","url_origen":"..."}, {"...otro..."}]} \
   SIN [DECISION] — el widget de PRODUCTOS_PREVIEW ya trae un botón "Publicar" por cada uno, así que \
   el usuario elige cuál(es) publicar desde ahí. Si de una vez pide "publica todas las variantes" o \
   "súbelas todas", usa publicar_productos_desde_links con el arreglo completo.

4. Igual que en MODO 13: nunca inventes precio, imagen ni specs que el documento no traiga (deja "" o \
   []); no repitas los datos en texto aparte del marcador; no vuelvas a publicar un producto ya \
   aprobado/publicado de un preview anterior.

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
- AFIRMATIVOS SUELTOS = SOLO CONVERSACIÓN: mensajes como "muy bien", "ok", "gracias", "perfecto", "va", \
"excelente", "genial" NO son instrucciones para ejecutar acciones (publicar, crear/actualizar en CRM, enviar \
correo). NUNCA llames una tool de acción por ellos. Solo ejecutas una acción con una instrucción EXPLÍCITA, o \
como aprobación DIRECTA de un [DECISION]/preview que acabas de mostrar y que sigue pendiente. Ante la duda, \
responde conversando y NO actúes.
- DECISIONES CON BOTONES: Cuando necesites aprobación del usuario para una acción importante (enviar un email, crear algo en CRM, etc.), termina tu mensaje con el tag [DECISION: pregunta corta aquí]. Ejemplo: "Le contestaré a Alejandro que tenemos el producto disponible. [DECISION: ¿Confirmas que lo enviamos?]". El sistema mostrará botones Sí/No automáticamente.
- IDIOMA DE CORREOS: la regla "responde en español" aplica al chat con el usuario, NO a los correos a clientes. \
Todo correo saliente (borrador o envío) debe ir en el MISMO idioma del correo original del cliente.
- EMAILS Y ACCIÓN: Cuando el usuario te pide explícitamente enviar o responder un email (ej: "contéstale", "dile que sí", "mándale cotización"), ACTÚA DIRECTAMENTE con enviar_email_gmail sin pedir confirmación adicional. El usuario ya dio la instrucción. Solo pide confirmación si hay ambigüedad sobre a QUIÉN enviar o si el contenido puede causar un compromiso comercial incorrecto que el usuario no mencionó.\
"""


# ─────────────────────────────────────────────────────────────
# PROMPT FOCALIZADO POR TIPO DE STREAM
# Cada stream tiene un `tipo`; según el tipo se carga SOLO los modos de ese propósito (+ el intro
# y el MODO 7, que son las reglas base). Prompt más chico = más predecible y algo más rápido.
# Tipo desconocido/None → prompt COMPLETO (comportamiento actual intacto → cero riesgo).
# ─────────────────────────────────────────────────────────────
def _partir_prompt(full: str):
    heads = list(re.finditer(r'(?m)^MODO (\d+) [—-]', full))
    if not heads:
        return full, {}
    intro = full[:heads[0].start()]
    modos: dict = {}
    for i, h in enumerate(heads):
        num = int(h.group(1))
        end = heads[i + 1].start() if i + 1 < len(heads) else len(full)
        modos[num] = full[h.start():end]
    return intro, modos


_PROMPT_INTRO, _PROMPT_MODOS = _partir_prompt(SYSTEM_PROMPT)

# Qué modos carga cada tipo (además del intro y MODO 7 = reglas base, que van siempre).
# "generico" / "compras" / "general" / desconocido / None → prompt COMPLETO (todos los modos).
TIPO_MODOS = {
    "correo":       [9, 10, 11, 12],              # correo: RFQ, oportunidades, alta cliente
    "whatsapp":     [9, 10, 11, 12],              # WhatsApp: mismos modos que correo, otro canal
    "busquedas":    [1, 2, 3, 4, 5, 6, 8, 14],     # búsqueda RFQ + selección proveedor + imagen + ficha técnica PDF
    "publicacion":  [13, 14],                      # publicar producto(s) desde link o ficha técnica PDF
    "cotizacion":   [],                            # (módulo de cotización se agrega después)
    "ordenes":      [],
    # aliases de compatibilidad
    "mensajeria":   [9, 10, 11, 12],
    "catalogo":     [1, 2, 3, 4, 5, 6, 8, 13, 14],
    "cotizaciones": [],
}


def _build_system_prompt(tipo: str) -> str:
    """Prompt focalizado según el tipo del stream. Tipo no reconocido → prompt completo."""
    if not tipo or tipo not in TIPO_MODOS or not _PROMPT_MODOS:
        return SYSTEM_PROMPT
    partes = [_PROMPT_INTRO]
    for n in TIPO_MODOS[tipo]:
        if n in _PROMPT_MODOS:
            partes.append(_PROMPT_MODOS[n])
    if 7 in _PROMPT_MODOS:
        partes.append(_PROMPT_MODOS[7])  # MODO 7 = reglas generales, siempre al final
    return "".join(partes)


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
    "extraer_ficha_pdf":        "📄 Leyendo la ficha técnica…",
    "buscar_internet":          "🌐 Buscando en internet…",
}


# ─────────────────────────────────────────────────────────────
# LOOP DE CLAUDE CON TOOL_USE
# ─────────────────────────────────────────────────────────────
def run_chat(messages: list[dict], stream_id: str, system_prompt: str = SYSTEM_PROMPT) -> tuple[str, list[str], bool, dict]:
    tools_used: list[str] = []
    rfqs_created = False
    current_messages = list(messages)
    total_input_tokens  = 0
    total_output_tokens = 0

    import time as _time
    _t_start = _time.time()
    # Desglose de tiempos que se persiste en el metadata de la respuesta (para diagnosticar
    # la lentitud sin depender de los logs de Railway): segundos en el modelo, por tool, total.
    _perf: dict = {"llm_s": 0.0, "tools": {}, "rondas": 0}
    for _ronda in range(10):
        _t_llm = _time.time()
        response = claude.messages.create(
            model=CHAT_MODEL,
            max_tokens=8192,
            # (timeout y max_retries se definen a nivel de cliente — ver anthropic.Anthropic arriba)
            # cache_control cachea el prefijo estático (tools + system prompt). En el loop de
            # tools (hasta 10 vueltas) las llamadas siguientes leen de caché en vez de
            # reprocesar ~30 tools + 11 modos cada vez → mucha menos latencia y costo.
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=current_messages,
        )
        _dt_llm = _time.time() - _t_llm
        _perf["llm_s"] = round(_perf["llm_s"] + _dt_llm, 1)
        _perf["rondas"] = _ronda + 1

        if hasattr(response, "usage") and response.usage:
            total_input_tokens  += getattr(response.usage, "input_tokens",  0)
            total_output_tokens += getattr(response.usage, "output_tokens", 0)
            _u = response.usage
            log.info(f"[PERF] ronda={_ronda} llm={_dt_llm:.1f}s stop={response.stop_reason} "
                     f"in={getattr(_u,'input_tokens',0)} out={getattr(_u,'output_tokens',0)} "
                     f"cache_read={getattr(_u,'cache_read_input_tokens',0)} "
                     f"cache_write={getattr(_u,'cache_creation_input_tokens',0)}")

        # end_turn = terminó normal. max_tokens = se truncó (respuesta grande): devolvemos lo
        # generado igual (mejor un widget/aviso parcial que "No pude completar la respuesta").
        if response.stop_reason in ("end_turn", "max_tokens"):
            text = next(
                (b.text for b in response.content if hasattr(b, "text")), ""
            )
            _perf["total_s"] = round(_time.time() - _t_start, 1)
            if response.stop_reason == "max_tokens":
                log.warning(f"[PERF] respuesta truncada por max_tokens (out={total_output_tokens})")
            log.info(f"[PERF] TOTAL run_chat={_perf['total_s']:.1f}s rondas={_ronda+1} "
                     f"tools={tools_used} stop={response.stop_reason}")
            token_counts = {"tokens_input": total_input_tokens, "tokens_output": total_output_tokens}
            return text, tools_used, rfqs_created, token_counts, _perf

        if response.stop_reason == "tool_use":
            current_messages.append({"role": "assistant", "content": response.content})

            # Si el agente escribió [DECISION] en esta misma respuesta, NO ejecutar
            # tools de escritura en el mismo turno — el [DECISION] debe esperar aprobación.
            _WRITE_TOOLS = {"crear_oportunidad_crm", "crear_cuenta_crm", "crear_contacto_crm",
                            "enviar_email_gmail", "crear_rfqs_desde_texto",
                            "publicar_producto_link", "publicar_productos_desde_links"}
            _response_text = " ".join(
                b.text for b in response.content if getattr(b, "type", "") == "text"
            )
            _has_pending_decision = "[DECISION" in _response_text

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_name  = block.name
                tool_input = block.input
                tools_used.append(tool_name)
                log.info(f"Tool: {tool_name}({json.dumps(tool_input)[:120]})")

                # Gate: bloquear tools de escritura si hay un [DECISION] pendiente en esta respuesta
                if _has_pending_decision and tool_name in _WRITE_TOOLS:
                    log.warning(f"[GATE] Tool '{tool_name}' bloqueada — hay [DECISION] pendiente en este turno")
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps({"error": "Acción bloqueada: hay una solicitud de aprobación [DECISION] pendiente en este turno. Espera la respuesta del usuario antes de ejecutar esta acción."}, ensure_ascii=False),
                    })
                    continue

                # Inyectar stream_id automáticamente en tools que lo necesitan
                if tool_name in ("crear_rfqs_desde_texto", "notificar_sistema", "publicar_producto_link", "publicar_productos_desde_links", "crear_cuenta_crm", "crear_contacto_crm", "crear_oportunidad_crm") and not tool_input.get("stream_id"):
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
                    # Publicar desde link: el widget negro (BulkWidget) ya muestra el resultado →
                    # suprimir la respuesta de texto redundante ("Publicando…").
                    if tool_name in ("publicar_producto_link", "publicar_productos_desde_links") and isinstance(result, dict) and result.get("ok"):
                        rfqs_created = True
                except Exception as e:
                    result = {"error": str(e)}
                _dt_tool = round(_time.time() - _t_tool, 1)
                _perf["tools"][tool_name] = round(_perf["tools"].get(tool_name, 0) + _dt_tool, 1)
                log.info(f"[PERF] tool={tool_name} dt={_dt_tool:.1f}s")

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

    _perf["total_s"] = round(_time.time() - _t_start, 1)
    token_counts = {"tokens_input": total_input_tokens, "tokens_output": total_output_tokens}
    return "No pude completar la respuesta.", tools_used, rfqs_created, token_counts, _perf


def _build_image_block(url: str):
    """Descarga una imagen y la devuelve como bloque de visión (base64) para la API de Claude.
    Se usa base64 (no la URL) para no depender de que Anthropic pueda alcanzar la URL de Supabase."""
    try:
        r = httpx.get(url, timeout=25, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or "image" not in r.headers.get("content-type", "").lower():
            log.warning(f"Imagen para visión no accesible: {r.status_code}")
            return None
        ct = r.headers.get("content-type", "").lower()
        media = ("image/png" if "png" in ct else "image/webp" if "webp" in ct
                 else "image/gif" if "gif" in ct else "image/jpeg")
        import base64 as _b64
        return {"type": "image", "source": {"type": "base64", "media_type": media,
                                            "data": _b64.standard_b64encode(r.content).decode()}}
    except Exception as e:
        log.warning(f"No se pudo construir bloque de imagen: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# PROCESADOR DE MENSAJES
# ─────────────────────────────────────────────────────────────
def _procesar_orden_compra(stream_id: str, file_url: str, nombre: str = "orden", mime: str = "") -> None:
    """PIPELINE DE ORDEN DE COMPRA (Fase 1, stream 'ordenes'): lee el PO, lo coteja contra las
    cotizaciones del cliente y emite el widget [COTEJO_PO]. NO escribe al CRM. Determinista
    (sin round-trip al modelo): más barato y robusto. Import perezoso para no romper el chat si
    aún falta alguna dependencia de parsing en el entorno."""
    try:
        import orden_compra
        import sales_order
    except Exception as e:
        log.error(f"orden_compra/sales_order no disponible: {e}")
        supabase.table("mensajes").insert({
            "stream_id": stream_id, "role": "assistant",
            "content": f"No pude procesar la orden de compra (módulo no disponible: {e}).",
            "procesado": True, "metadata": {},
        }).execute()
        return

    _log_stream(stream_id, f"Leyendo orden de compra: {nombre}", "info")
    po = orden_compra.leer_po(url=file_url, nombre=nombre, mime=mime)
    if po.get("error"):
        _log_stream(stream_id, f"No se pudo leer el PO: {po['error']}", "error")
        supabase.table("mensajes").insert({
            "stream_id": stream_id, "role": "assistant",
            "content": f"No pude leer la orden de compra «{nombre}»: {po['error']}. "
                       f"Verifica que el archivo sea legible (PDF/Excel/Word).",
            "procesado": True, "metadata": {},
        }).execute()
        return

    _log_stream(stream_id, f"Cotejando {len(po.get('items', []))} producto(s) contra cotizaciones…", "info")
    cotejo = sales_order.cotejar(po)
    payload = {"po": po, "cotejo": cotejo}
    supabase.table("mensajes").insert({
        "stream_id": stream_id, "role": "assistant",
        "content": "[COTEJO_PO]" + json.dumps(payload, ensure_ascii=False),
        "procesado": True, "metadata": {"cotejo_po": True},
    }).execute()
    _log_stream(stream_id, cotejo.get("resumen", "Cotejo listo"), "ok")
    log.info(f"Cotejo PO emitido | {cotejo.get('resumen','')}")


def _crear_so_confirmada(stream_id: str, draft: dict) -> None:
    """Crea la Sales Order tras la aprobación del usuario en el previo (metadata.so_action='crear').
    Escribe al CRM SOLO aquí, con el draft ya confirmado. Emite el widget [SO_CREADA]."""
    try:
        import sales_order
    except Exception as e:
        supabase.table("mensajes").insert({
            "stream_id": stream_id, "role": "assistant",
            "content": f"No pude crear la Sales Order (módulo no disponible: {e}).",
            "procesado": True, "metadata": {},
        }).execute()
        return
    _log_stream(stream_id, "Creando Sales Order en 1CRM…", "info")
    res = sales_order.crear_sales_order(draft or {})
    if not res.get("ok"):
        _log_stream(stream_id, f"No se pudo crear la Sales Order: {res.get('error','')}", "error")
        supabase.table("mensajes").insert({
            "stream_id": stream_id, "role": "assistant",
            "content": f"No pude crear la Sales Order: {res.get('error','error desconocido')}.",
            "procesado": True, "metadata": {},
        }).execute()
        return
    supabase.table("mensajes").insert({
        "stream_id": stream_id, "role": "assistant",
        "content": "[SO_CREADA]" + json.dumps(res, ensure_ascii=False),
        "procesado": True, "metadata": {"so_creada": True},
    }).execute()
    _log_stream(stream_id, f"Sales Order {res.get('so_numero','')} creada ✓", "ok")
    log.info(f"Sales Order creada: {res.get('so_id')} ({res.get('so_numero')})")


def procesar_mensaje(msg: dict) -> None:
    msg_id    = msg["id"]
    stream_id = msg.get("stream_id", "")
    contenido = msg.get("content", "")

    # Tipo del stream → prompt focalizado (base + solo los modos de ese propósito). Si no hay tipo
    # reconocido, _build_system_prompt devuelve el prompt completo (comportamiento actual).
    _tipo_stream = None
    try:
        if stream_id:
            _sres = supabase.table("streams").select("tipo").eq("id", stream_id).single().execute()
            _tipo_stream = (_sres.data or {}).get("tipo")
    except Exception as e:
        log.warning(f"No se pudo leer tipo del stream: {e}")
    _system_prompt = _build_system_prompt(_tipo_stream)

    log.info(f"Chat msg {msg_id[:8]} | stream={stream_id!r} tipo={_tipo_stream!r} "
             f"prompt={len(_system_prompt)}ch | '{contenido[:60]}...'")

    # Marcar como procesado
    supabase.table("mensajes").update({"procesado": True}).eq("id", msg_id).execute()

    # PIPELINE DE ORDEN DE COMPRA: si es un stream 'ordenes' y llega un archivo (PO), léelo y
    # coteja contra las cotizaciones del cliente — sin pasar por el modelo. Emite el widget y termina.
    _md0 = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    _po_url = _md0.get("file_url") or _md0.get("po_url")
    if _tipo_stream in ("ordenes", "sales_order") and _po_url:
        _procesar_orden_compra(stream_id, _po_url,
                               (_md0.get("file_name") or "orden"), _md0.get("file_mime", ""))
        return

    # CREAR la Sales Order: el usuario confirmó desde el previo (metadata.so_action == "crear").
    if _tipo_stream in ("ordenes", "sales_order") and _md0.get("so_action") == "crear":
        _crear_so_confirmada(stream_id, _md0.get("draft") or {})
        return

    # Los triggers [SISTEMA:...] los maneja el widget del frontend — no necesitan respuesta de Claude
    if contenido.startswith("[SISTEMA:"):
        log.info(f"Trigger sistema ignorado (widget lo maneja): {contenido[:60]}")
        return

    # Obtener historial del stream (últimos 10 mensajes, más recientes primero → revertir)
    historial = []
    try:
        hist_resp = (
            supabase.table("mensajes")
            .select("role, content, procesado, metadata")
            .eq("stream_id", stream_id)
            .order("created_at", desc=True)
            .limit(12)
            .execute()
        )
        for r in reversed(hist_resp.data or []):
            if r["role"] not in ("user", "assistant"):
                continue
            _md = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
            if _md.get("estimado") or _md.get("correo_entrante"):
                continue  # estimados y tarjetas de correo entrante no van al contexto
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

    # Visión: si el mensaje trae una imagen (screenshot), se adjunta al último turno del usuario
    # como bloque de imagen para que el modelo lea los links/datos visibles y corra el flujo de
    # publicar. El resto del pipeline (texto) queda igual.
    try:
        _md = msg.get("metadata") or {}
        _img_url = _md.get("image_url") if isinstance(_md, dict) else None
    except Exception:
        _img_url = None
    if _img_url and historial and historial[-1]["role"] == "user":
        _blk = _build_image_block(_img_url)
        if _blk:
            historial[-1]["content"] = [
                {"type": "text", "text": contenido or "Imagen adjunta."}, _blk,
            ]
            log.info("Mensaje con imagen → visión activada")

    # Log del stream: registrar la solicitud entrante (alimenta el log del UI)
    _log_stream(stream_id, f'Solicitud recibida: "{contenido[:80]}"', "info")

    # El estimado de tiempo ("procesando…") lo muestra el FRONTEND de forma inmediata como una
    # burbuja animada que se borra sola al llegar la respuesta (ver App.tsx). Aquí NO insertamos
    # un mensaje de estimado: hacerlo dejaba un texto plano pegado y duplicado.

    # Llamar a Claude
    token_counts = {"tokens_input": 0, "tokens_output": 0}
    perf: dict = {}
    _t_proc = time.time()
    try:
        roles = [m['role'] for m in historial]
        print(f"[BRAIN] historial roles={roles} n={len(historial)}", flush=True)
        respuesta, tools_used, rfqs_created, token_counts, perf = run_chat(historial, stream_id=str(stream_id), system_prompt=_system_prompt)
    except Exception as e:
        import traceback as _tb
        print(f"[BRAIN ERROR] {e}", flush=True)
        _tb.print_exc()
        log.error(f"Error en Claude (completo): {e}")
        _log_stream(stream_id, f"Error procesando el mensaje: {str(e)[:150]}", "error")
        respuesta    = f"Error procesando tu mensaje. Intenta de nuevo. ({str(e)[:400]})"
        tools_used   = []
        rfqs_created = False
    # Tiempo de cola: desde que el usuario mandó el mensaje hasta que empezó a procesarse.
    try:
        _created = msg.get("created_at")
        if _created:
            _t0 = datetime.fromisoformat(_created.replace("Z", "+00:00"))
            perf["cola_s"] = round((datetime.now(timezone.utc) - _t0).total_seconds() - (time.time() - _t_proc), 1)
    except Exception:
        pass

    # Registrar job de chat con tokens para que agente_monitor los sume
    try:
        supabase.table("jobs").insert({
            "agente": "chat",
            "estado": "completado",
            "output": {
                **token_counts,
                "tokens_total": token_counts["tokens_input"] + token_counts["tokens_output"],
                "tools_used":   tools_used,
                "perf":         perf,
                "stream_id":    str(stream_id) if stream_id else None,
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
        "metadata":  {"tools_used": tools_used, "perf": perf},
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
