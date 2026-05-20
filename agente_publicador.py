"""
AGENTE PUBLICADOR — Brain · MRO Master Pro
==========================================
Worker que crea el producto en 1CRM cuando el gerente aprueba la imagen.

Trigger: Job con agente='publicador' y estado='pendiente', creado desde
         Bolt cuando el gerente aprueba la imagen procesada.

Flujo:
  1. Obtener datos del RFQ (marca, modelo, foto_url)
  2. Obtener Top 5 opciones de Supabase (precio, descripción, proveedor)
  3. Claude genera nombre comercial y descripción técnica del producto
  4. POST a 1CRM data/Product → crear producto
  5. Subir imagen PNG al producto creado en 1CRM
  6. Actualizar rfqs: crm_product_id + estado='publicado'
  7. Notificación al gerente

Variables de entorno (mismas que agente_buscador):
  ANTHROPIC_API_KEY=
  SUPABASE_URL=
  SUPABASE_SERVICE_KEY=
  ONECRM_URL=
  ONECRM_USERNAME=
  ONECRM_PASSWORD=
"""

import os
import time
import json
import logging
from datetime import datetime
from dotenv import load_dotenv

import httpx
import anthropic
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agente_publicador")

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"],
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ONECRM_BASE = os.environ["ONECRM_URL"].rstrip("/")
POLL_INTERVAL = 10


# ─────────────────────────────────────────
# 1CRM — HTTP BASIC AUTH
# ─────────────────────────────────────────
def onecrm_get(endpoint: str, params: dict = {}) -> dict:
    resp = httpx.get(
        f"{ONECRM_BASE}/api.php/{endpoint}",
        auth=(os.environ["ONECRM_USERNAME"], os.environ["ONECRM_PASSWORD"]),
        params=params,
        timeout=20,
    )
    if resp.status_code != 200:
        log.error(f"1CRM GET error {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()
    return resp.json()


def onecrm_post(endpoint: str, data: dict) -> dict:
    resp = httpx.post(
        f"{ONECRM_BASE}/api.php/{endpoint}",
        auth=(os.environ["ONECRM_USERNAME"], os.environ["ONECRM_PASSWORD"]),
        json={"data": data},
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        body = resp.text[:2000]
        log.error(f"1CRM POST error {resp.status_code}: {body}")
        # Raise with body so it propagates to Supabase error column
        raise RuntimeError(f"1CRM POST {resp.status_code} on {endpoint}: {body}")
    return resp.json()


_crm_currency_id: str | None = None  # module-level cache

# Nombre del módulo de productos en 1CRM — se descubre en runtime
_crm_product_module: str | None = None
_PRODUCT_MODULE_CANDIDATES = [
    "AOS_Products",      # ← módulo estándar 1CRM para ProductCatalog UI
    "Product",           # ← confirmado HTTP 200, pero puede ser módulo diferente
    "ProductCatalog",
    "AOS_Products_Quotes",
    "Products",
]


def discover_product_module() -> str:
    """
    Prueba cada candidato de módulo de productos en 1CRM y devuelve el primero
    que responda con 200 OK. Guarda el resultado en _crm_product_module.
    """
    global _crm_product_module
    if _crm_product_module:
        return _crm_product_module

    log.info("Descubriendo módulo de productos en 1CRM API...")

    # Intentar obtener lista completa de módulos vía metadata
    try:
        meta = onecrm_get("meta/modules", {})
        mods = meta.get("modules") or {}
        product_mods = [m for m in mods if any(x in m.lower() for x in ["product", "catalog", "aos_p"])]
        log.info(f"Módulos de 1CRM totales: {len(mods)} | Relacionados con producto: {product_mods}")
    except Exception as e:
        log.warning(f"Metadata endpoint no disponible: {e}")
    for mod in _PRODUCT_MODULE_CANDIDATES:
        ep = f"data/{mod}"
        try:
            resp = httpx.get(
                f"{ONECRM_BASE}/api.php/{ep}",
                auth=(os.environ["ONECRM_USERNAME"], os.environ["ONECRM_PASSWORD"]),
                params={"max_num": 1},
                timeout=10,
            )
            log.info(f"  {ep} → HTTP {resp.status_code} | {resp.text[:120]}")
            if resp.status_code == 200:
                _crm_product_module = mod
                log.info(f"✓ Módulo de productos 1CRM: {mod}")
                return mod
        except Exception as e:
            log.warning(f"  {ep} → ERROR: {e}")

    # Fallback — usar Product (confirmado como correcto en esta instancia)
    _crm_product_module = "Product"
    log.error(f"No se pudo descubrir módulo de productos. Usando fallback: {_crm_product_module}")
    return _crm_product_module


def get_crm_currency_id() -> str:
    """
    Devuelve el ID de moneda USD en 1CRM.
    En esta instancia, USD usa el id especial "-99" (moneda base del sistema).
    Si no se puede confirmar, usa "-99" como fallback seguro.
    """
    global _crm_currency_id
    if _crm_currency_id is not None:
        return _crm_currency_id
    # "-99" es el ID reservado para la moneda base (USD) en 1CRM/SugarCRM
    # Los productos del catálogo usan este ID — usando cualquier otro (ej. AUD)
    # el ProductCatalog UI no los muestra correctamente.
    try:
        result = onecrm_get("data/Currency", {"max_num": 50})
        records = result.get("records") or []
        # Buscar USD explícitamente primero
        for rec in records:
            name = (rec.get("name") or "").lower()
            rid = rec.get("id", "")
            if "us dollar" in name or "usd" in name or rid == "-99":
                log.info(f"Currency USD encontrado: id={rid} name={rec.get('name')}")
                _crm_currency_id = rid
                return rid
    except Exception as e:
        log.warning(f"No se pudo buscar currency USD: {e}")
    # Fallback: -99 es siempre USD en 1CRM
    log.info("Usando currency_id=-99 (USD sistema 1CRM)")
    _crm_currency_id = "-99"
    return "-99"


_crm_category_id: str | None = None  # module-level cache
_crm_product_type_id: str | None = None  # module-level cache


def get_crm_category_id() -> str | None:
    """
    Fetch the first available product category UUID from 1CRM.
    Required: this 1CRM instance has product_category_id NOT NULL with no default.
    Caches the result for the process lifetime.
    """
    global _crm_category_id
    if _crm_category_id is not None:
        return _crm_category_id
    for ep in ("data/ProductCategory", "data/ProductCategories", "data/AOS_Product_Categories"):
        try:
            result = onecrm_get(ep, {"max_num": 5})
            records = result.get("records") or []
            for rec in records:
                cid = rec.get("id")
                if cid:
                    log.info(f"Category ID obtenido de {ep}: {cid} ({rec.get('name', '?')})")
                    _crm_category_id = cid
                    return cid
        except Exception as e:
            log.warning(f"No se pudo obtener category de {ep}: {e}")
    log.error("product_category_id no disponible — el POST fallará (MySQL 1364)")
    return None


def get_crm_product_type_id() -> str | None:
    """
    Fetch the first available product type UUID from 1CRM.
    CRITICAL: product_type_id is required to route POST data/Product to the
    aos_products (ProductCatalog) table rather than the products (line items) table.
    Without it, products are created in the wrong SQL table and never appear in
    the ProductCatalog UI. Caches the result for the process lifetime.
    """
    global _crm_product_type_id
    if _crm_product_type_id is not None:
        return _crm_product_type_id

    # Primary: fetch from ProductTypes endpoint
    for ep in ("data/ProductType", "data/ProductTypes"):
        try:
            result = onecrm_get(ep, {"max_num": 5})
            records = result.get("records") or []
            for rec in records:
                tid = rec.get("id")
                if tid:
                    log.info(f"ProductType ID obtenido de {ep}: {tid} ({rec.get('name', '?')})")
                    _crm_product_type_id = tid
                    return tid
        except Exception as e:
            log.warning(f"No se pudo obtener ProductType de {ep}: {e}")

    # Fallback: read product_type_id from an existing catalog product
    try:
        result = onecrm_get("data/Product", {"max_num": 5})
        records = result.get("records") or []
        for rec in records:
            pid = rec.get("id")
            if pid:
                detail = onecrm_get(f"data/Product/{pid}")
                tid = (detail.get("record") or {}).get("product_type_id")
                if tid:
                    log.info(f"ProductType ID extraído de producto existente: {tid}")
                    _crm_product_type_id = tid
                    return tid
    except Exception as e:
        log.warning(f"No se pudo leer product_type_id de producto existente: {e}")

    log.error("product_type_id no disponible — los productos NO aparecerán en ProductCatalog")
    return None


def onecrm_patch(endpoint: str, data: dict) -> dict:
    resp = httpx.patch(
        f"{ONECRM_BASE}/api.php/{endpoint}",
        auth=(os.environ["ONECRM_USERNAME"], os.environ["ONECRM_PASSWORD"]),
        json={"data": data},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────
# CLAUDE — GENERA NOMBRE Y DESCRIPCIÓN
# ─────────────────────────────────────────
def generar_ficha_producto(marca: str, modelo: str, opciones: list[dict]) -> dict:
    """
    Claude genera el nombre comercial y descripción técnica del producto
    basándose en los datos del Top 5.
    Devuelve: {nombre, descripcion, precio_referencia, moneda}
    """
    log.info(f"Claude generando ficha para: {marca} {modelo}")

    # Extraer la mejor opción con precio
    mejor_precio = None
    mejor_moneda = "USD"
    for op in opciones:
        p = op.get("precio_orig") or op.get("precio_mxn")
        if p and float(p) > 0:
            mejor_precio = float(op.get("precio_orig") or 0) or None
            mejor_moneda = op.get("moneda", "USD")
            break

    prompt = f"""Eres un especialista en catálogos de productos industriales MRO.

Genera la ficha de producto para el catálogo de 1CRM con estos datos:

Marca: {marca}
Modelo/Número de parte: {modelo}

Resultados de búsqueda encontrados:
{json.dumps(opciones, ensure_ascii=False, indent=2)}

Devuelve SOLO este JSON (sin texto extra):
{{
  "nombre": "Nombre comercial completo del producto (marca + modelo + descripción corta, máx 80 chars)",
  "descripcion": "Descripción técnica en español, 2-4 oraciones. Incluye aplicación, características principales y compatibilidad si se conoce.",
  "precio_referencia": {mejor_precio or 0},
  "moneda": "{mejor_moneda}",
  "unidad": "PZA"
}}"""

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        ficha = json.loads(text)
        log.info(f"Ficha generada: {ficha.get('nombre', '')[:60]}")
        return ficha
    except Exception as e:
        log.error(f"Error generando ficha con Claude: {e}")
        return {
            "nombre": f"{marca} {modelo}",
            "descripcion": f"Producto {marca} número de parte {modelo}.",
            "precio_referencia": mejor_precio or 0,
            "moneda": mejor_moneda,
            "unidad": "PZA",
        }


# ─────────────────────────────────────────
# CREAR PRODUCTO EN 1CRM
# ─────────────────────────────────────────
def buscar_producto_por_codigo(modelo: str) -> tuple[str | None, list[str]]:
    """
    Busca un producto existente en 1CRM por manufacturers_part_no.
    Nota: el API REST de esta instancia NO filtra correctamente por search[]
    (ignora los parámetros y devuelve todos los productos paginados).
    Por eso buscamos por nombre que sí incluye el número de parte.

    Devuelve (id_o_None, lista_de_diagnósticos).
    """
    diags: list[str] = []
    mod = discover_product_module()
    ep = f"data/{mod}"
    target = modelo.strip().upper()

    # Intento A: buscar por nombre que contenga el número de parte
    # (1CRM no filtra por product_code pero sí puede encontrar por nombre parcial)
    try:
        result = onecrm_get(ep, {
            "search[manufacturers_part_no]": modelo,
            "max_num": 20,
        })
        records = result.get("records") or []
        diags.append(f"GET {ep} search manufacturers_part_no → {len(records)} registros")
        for rec in records:
            stored = (rec.get("manufacturers_part_no") or "").strip().upper()
            # También revisar si el nombre contiene el número de parte
            name_has = target in (rec.get("name") or "").upper()
            if stored == target or name_has:
                pid = rec.get("id")
                if pid:
                    log.info(f"Producto encontrado por manufacturers_part_no: id={pid}")
                    return pid, diags
    except Exception as e:
        diags.append(f"GET {ep} search ERR: {str(e)[:100]}")

    # Intento B: buscar por nombre
    try:
        result = onecrm_get(ep, {
            "search[name]": modelo,
            "max_num": 20,
        })
        records = result.get("records") or []
        diags.append(f"GET {ep} search name → {len(records)} registros")
        for rec in records:
            name_has = target in (rec.get("name") or "").upper()
            mfr      = (rec.get("manufacturers_part_no") or "").strip().upper()
            if name_has or mfr == target:
                pid = rec.get("id")
                if pid:
                    log.info(f"Producto encontrado por nombre: id={pid}")
                    return pid, diags
    except Exception as e:
        diags.append(f"GET {ep} search name ERR: {str(e)[:100]}")

    log.warning(f"Producto '{modelo}' no encontrado en 1CRM. Diags: {diags}")
    return None, diags


def crear_producto_en_crm(ficha: dict, modelo: str) -> str:
    """
    Crea el producto en 1CRM vía POST data/Product → escribe en aos_products (ProductCatalog).

    CRITICAL: los campos del payload deben coincidir con columnas de aos_products, NO
    de la tabla products (line items). Diferencias clave:
      - list_price (aos_products) en vez de price / unit_price (products)
      - product_type_id DEBE incluirse — es lo que enruta el POST a aos_products
      - eshop="1" requerido para que aparezca en el catálogo
      - currency_id="-99" (USD base del sistema)

    Primero busca si ya existe por product_code para evitar duplicados.
    Devuelve el ID del producto.
    Raises RuntimeError con el response body si falla definitivamente.
    """
    log.info(f"Creando producto en 1CRM: {ficha['nombre']}")
    precio = round(float(ficha.get("precio_referencia") or 0), 2)
    precio_str = f"{precio:.2f}"

    # Campos que enrutan a aos_products (ProductCatalog) — NO usar price/unit_price
    # que son columnas de la tabla products (line items de cotizaciones)
    payload: dict = {
        "name":                  ficha["nombre"],
        "manufacturers_part_no": modelo,
        "description":           ficha["descripcion"],
        # Precios según schema de aos_products
        "list_price":            precio_str,
        "list_usdollar":         precio_str,
        "purchase_price":        "0.00",
        "purchase_usdollar":     "0.00",
        "cost":                  "0.00",
        "cost_usdollar":         "0.00",
        "support_cost":          "0.00",
        "support_cost_usdollar": "0.00",
        "support_list_price":    "0.00",
        "support_list_usdollar": "0.00",
        "support_selling_price": "0.00",
        "support_selling_usdollar": "0.00",
        # Campos de catálogo
        "status":            "Active",
        "is_available":      "yes",
        "track_inventory":   "semiauto",
        "eshop":             "1",          # requerido para aparecer en catálogo
        "pricing_formula":   "Fixed Price",
        "support_price_formula": "Fixed Price",
        "currency_id":       "-99",        # USD base del sistema
        "exchange_rate":     "1",
    }

    # product_type_id: CRÍTICO — enruta el POST a aos_products en vez de products
    type_id = get_crm_product_type_id()
    if type_id:
        payload["product_type_id"] = type_id
        log.info(f"product_type_id={type_id}")
    else:
        log.error("⚠ Sin product_type_id — el producto podría ir a tabla incorrecta")

    # product_category_id: NOT NULL sin default en esta instancia
    category_id = get_crm_category_id()
    if category_id:
        payload["product_category_id"] = category_id
        log.info(f"product_category_id={category_id}")

    log.info(f"POST payload: type_id={type_id or '(omitido)'} cat_id={category_id or '(omitido)'}")

    # Check if product already exists before POST (avoids 500 duplicate constraint)
    existing_id, pre_diags = buscar_producto_por_codigo(modelo)
    if existing_id:
        log.info(f"Producto ya existe en 1CRM (id={existing_id}) — reutilizando")
        return existing_id

    try:
        mod = discover_product_module()
        result = onecrm_post(f"data/{mod}", payload)

        # Log full response for debugging — 1CRM can return ID under different keys
        log.info(f"1CRM POST data/{mod} → full response: {str(result)[:1000]}")

        # Try multiple possible response formats
        product_id = (
            result.get("id")
            or result.get("record_id")
            or (result.get("record") or {}).get("id")
            or ((result.get("data") or {}).get("id") if isinstance(result.get("data"), dict) else None)
        )

        if product_id:
            log.info(f"Producto creado en 1CRM: id={product_id}")
            return product_id

        # POST succeeded (2xx) but no ID in response — try GET fallback
        log.warning("POST exitoso pero sin ID en respuesta — buscando producto por código")
        existing_id, post_diags = buscar_producto_por_codigo(modelo)
        if existing_id:
            return existing_id

        resp_snippet = str(result)[:400]
        raise RuntimeError(f"POST OK sin ID. Resp={resp_snippet}. GET_diags={post_diags}")

    except RuntimeError:
        raise
    except Exception as post_err:
        # Could be 500 (duplicate product_code), network error, etc.
        log.warning(f"POST falló ({post_err}) — buscando producto existente por código")
        existing_id, err_diags = buscar_producto_por_codigo(modelo)
        if existing_id:
            log.info(f"Reutilizando producto existente id={existing_id}")
            return existing_id
        # Include full diagnostics in error for Supabase visibility
        raise RuntimeError(
            f"POST={str(post_err)[:200]} | "
            f"PRE_GET={pre_diags} | "
            f"ERR_GET={err_diags}"
        )


# ─────────────────────────────────────────
# SUBIR IMAGEN AL PRODUCTO EN 1CRM
# ─────────────────────────────────────────
def subir_imagen_a_crm(product_id: str, foto_url: str) -> bool:
    """
    Descarga la imagen de Supabase Storage y la sube a 1CRM
    como adjunto del producto. Intenta dos estrategias:
    1. Campo picture en PATCH del producto (URL directa)
    2. Endpoint /files con multipart
    """
    log.info(f"Subiendo imagen a 1CRM para producto {product_id}")

    # Estrategia 1: PATCH con campo picture (URL)
    try:
        mod = discover_product_module()
        onecrm_patch(f"data/{mod}/{product_id}", {"picture": foto_url})
        log.info("Imagen vinculada via PATCH picture OK")
        return True
    except Exception as e:
        log.warning(f"PATCH picture falló: {e} — intentando upload multipart")

    # Estrategia 2: Upload multipart via /files
    try:
        img_resp = httpx.get(foto_url, timeout=20)
        img_resp.raise_for_status()
        img_bytes = img_resp.content

        mod = discover_product_module()
        resp = httpx.post(
            f"{ONECRM_BASE}/api.php/files",
            auth=(os.environ["ONECRM_USERNAME"], os.environ["ONECRM_PASSWORD"]),
            files={"file": ("product.png", img_bytes, "image/png")},
            data={
                "parent_type": mod,
                "parent_id":   product_id,
                "field":       "picture",
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            log.info("Imagen subida via /files OK")
            return True
        log.warning(f"Upload /files devolvió {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log.error(f"Upload multipart falló: {e}")
        return False


# ─────────────────────────────────────────
# PROCESADOR PRINCIPAL
# ─────────────────────────────────────────
def procesar_job_publicador(job: dict) -> None:
    job_id   = job["id"]
    rfq_uuid = job["rfq_id"]
    log.info(f"=== Job publicador {job_id} | rfq {rfq_uuid} ===")

    supabase.table("jobs").update({
        "estado":      "corriendo",
        "started_at":  datetime.utcnow().isoformat(),
    }).eq("id", job_id).execute()

    try:
        # ── Obtener RFQ ─────────────────────────────────────────────────
        rfq    = supabase.table("rfqs").select("*").eq("id", rfq_uuid).single().execute().data
        marca  = rfq["marca"].strip().title()
        modelo = rfq["modelo"].strip()
        foto_url = rfq.get("foto_url")

        log.info(f"Publicando: {marca} {modelo} | foto={'sí' if foto_url else 'no'}")

        # ── Obtener opciones (Top 5) ─────────────────────────────────────
        opts = supabase.table("opciones").select("*").eq("rfq_id", rfq_uuid)\
            .order("rank").execute().data or []
        log.info(f"{len(opts)} opciones encontradas")

        # ── Claude genera ficha ──────────────────────────────────────────
        ficha = generar_ficha_producto(marca, modelo, opts)

        # ── Crear producto en 1CRM ───────────────────────────────────────
        # crear_producto_en_crm raises RuntimeError with full response if 1CRM fails
        product_id = crear_producto_en_crm(ficha, modelo)

        # ── Subir imagen (si existe) ─────────────────────────────────────
        imagen_subida = False
        if foto_url:
            imagen_subida = subir_imagen_a_crm(product_id, foto_url)
        else:
            log.warning("Sin foto_url — producto creado sin imagen")

        # ── Actualizar RFQ ───────────────────────────────────────────────
        supabase.table("rfqs").update({
            "crm_product_id": product_id,
            "estado":         "publicado",
        }).eq("id", rfq_uuid).execute()

        # ── Notificar ────────────────────────────────────────────────────
        crm_url = f"{ONECRM_BASE}/index.php?module=ProductCatalog&record={product_id}"
        supabase.table("notificaciones").insert({
            "tipo":      "producto_publicado",
            "titulo":    f"Publicado en 1CRM — {marca} {modelo}",
            "mensaje":   (
                f"Producto creado exitosamente en el catálogo de 1CRM.\n"
                f"Imagen: {'subida ✓' if imagen_subida else 'pendiente ⚠'}\n"
                f"Ver en 1CRM: {crm_url}"
            ),
            "rfq_id":    rfq_uuid,
            "stream_id": rfq.get("stream_id"),
            "leida":     False,
        }).execute()

        # ── Cerrar job ───────────────────────────────────────────────────
        supabase.table("jobs").update({
            "estado":      "completado",
            "finished_at": datetime.utcnow().isoformat(),
            "output": {
                "crm_product_id": product_id,
                "crm_url":        crm_url,
                "imagen_subida":  imagen_subida,
                "nombre":         ficha["nombre"],
            },
        }).eq("id", job_id).execute()

        log.info(f"Job publicador {job_id} completado ✓ — producto {product_id} en 1CRM")

    except Exception as e:
        log.error(f"Job publicador {job_id} falló: {e}")
        supabase.table("jobs").update({
            "estado":      "fallido",
            "finished_at": datetime.utcnow().isoformat(),
            "error":       str(e),
        }).eq("id", job_id).execute()

        # Marcar como fallido con mensaje — NO volver a foto_lista (causa loop infinito)
        try:
            supabase.table("rfqs").update({
                "estado": "publicacion_fallida",
            }).eq("id", rfq_uuid).execute()
            log.info(f"RFQ {rfq_uuid} → publicacion_fallida tras error del publicador")
        except Exception as reset_err:
            log.error(f"No se pudo marcar rfq {rfq_uuid} como fallido: {reset_err}")


# ─────────────────────────────────────────
# LOOP PRINCIPAL — POLLING
# ─────────────────────────────────────────
def resetear_rfqs_publicando():
    """
    Detecta rfqs atascados en estado 'publicando' sin job activo y los marca como
    'publicacion_fallida'. Ocurre cuando el worker crashea o se redeploya mid-job.
    NO resetea a 'foto_lista' para evitar el loop infinito de publicación.
    """
    try:
        rfqs = (
            supabase.table("rfqs")
            .select("id")
            .eq("estado", "publicando")
            .execute()
            .data or []
        )
        for rfq in rfqs:
            rfq_id = rfq["id"]
            activos = (
                supabase.table("jobs")
                .select("id")
                .eq("rfq_id", rfq_id)
                .eq("agente", "publicador")
                .in_("estado", ["pendiente", "corriendo"])
                .execute()
                .data or []
            )
            if not activos:
                log.warning(f"RFQ {rfq_id} atascado en 'publicando' sin job activo — marcando publicacion_fallida")
                supabase.table("rfqs").update({
                    "estado": "publicacion_fallida",
                }).eq("id", rfq_id).execute()
    except Exception as e:
        log.error(f"Error reseteando rfqs publicando: {e}")


def resetear_jobs_huerfanos():
    """Resetea jobs de publicador que quedaron en 'corriendo' por redeploy."""
    try:
        huerfanos = (
            supabase.table("jobs")
            .select("id, rfq_id")
            .eq("agente", "publicador")
            .eq("estado", "corriendo")
            .execute()
            .data or []
        )
        if huerfanos:
            log.warning(f"⚠ {len(huerfanos)} job(s) de publicador huérfanos — reseteando")
            for job in huerfanos:
                supabase.table("jobs").update({
                    "estado":     "pendiente",
                    "started_at": None,
                    "error":      "Reseteado por reinicio del agente (redeploy)",
                }).eq("id", job["id"]).execute()
                # NO tocar el rfq — queda en 'publicando' para que el job
                # recién reseteado lo retome sin mostrar el botón Publicar otra vez
                log.info(f"  Job publicador {job['id']} reseteado a pendiente (rfq sin cambios)")
        else:
            log.info("Sin jobs huérfanos de publicador al arrancar")
    except Exception as e:
        log.error(f"Error reseteando jobs huérfanos publicador: {e}")


def main():
    log.info("Agente Publicador iniciado — escuchando jobs...")
    log.info(f"1CRM: {ONECRM_BASE}")
    log.info(f"Supabase: {os.environ['SUPABASE_URL']}")

    # Descubrir módulo de productos al arrancar (log visible en Railway)
    discover_product_module()
    get_crm_product_type_id()   # precarga — crítico para enrutar POST a aos_products
    get_crm_category_id()       # precarga — NOT NULL en esta instancia

    resetear_jobs_huerfanos()
    resetear_rfqs_publicando()

    while True:
        try:
            resp = (
                supabase.table("jobs")
                .select("*")
                .eq("agente", "publicador")
                .eq("estado", "pendiente")
                .order("created_at")
                .limit(1)
                .execute()
            )
            if resp.data:
                procesar_job_publicador(resp.data[0])
            else:
                log.debug("Sin jobs de publicador pendientes")
        except Exception as e:
            log.error(f"Error en loop principal: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
