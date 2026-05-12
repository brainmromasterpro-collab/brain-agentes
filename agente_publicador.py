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


def get_crm_currency_id() -> str | None:
    """
    Fetch the default/first active currency UUID from 1CRM.
    Caches the result for the process lifetime.
    Returns None if the endpoint is unavailable (1CRM will use its own default).
    """
    global _crm_currency_id
    if _crm_currency_id is not None:
        return _crm_currency_id
    for ep in ("data/Currency", "data/Currencies"):
        try:
            result = onecrm_get(ep, {"max_num": 5})
            records = result.get("records") or []
            for rec in records:
                cid = rec.get("id")
                if cid:
                    log.info(f"Currency ID obtenido de {ep}: {cid} ({rec.get('name', '?')})")
                    _crm_currency_id = cid
                    return cid
        except Exception as e:
            log.warning(f"No se pudo obtener currency de {ep}: {e}")
    log.warning("Currency ID no disponible — se omitirá del payload")
    return None


_crm_category_id: str | None = None  # module-level cache


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
    Busca un producto existente en 1CRM por product_code.
    Devuelve (id_o_None, lista_de_diagnósticos).
    Intenta múltiples endpoints y estrategias para máxima cobertura.
    """
    diags: list[str] = []

    # Candidatos de endpoints a probar
    endpoints = ["data/Product", "data/Products"]

    for ep in endpoints:
        # Intento A: search por product_code — verificamos que el código devuelto
        # coincida (case-insensitive) para evitar falsos positivos cuando 1CRM
        # no filtra correctamente y devuelve todos los productos.
        try:
            result = onecrm_get(ep, {
                "search[product_code]": modelo,
                "max_num": 5,
            })
            diags.append(f"GET {ep} search → {str(result)[:200]}")
            records = result.get("records") or []
            target = modelo.strip().upper()
            for rec in records:
                stored = (rec.get("product_code") or "").strip().upper()
                if stored == target:
                    pid = rec.get("id")
                    if pid:
                        log.info(f"Producto encontrado ({ep} search, code match): id={pid}")
                        return pid, diags
            if records:
                diags.append(
                    f"GET {ep} search: {len(records)} registros pero ninguno con "
                    f"product_code={modelo} (1CRM no filtra correctamente)"
                )
        except Exception as e:
            diags.append(f"GET {ep} search ERR: {str(e)[:100]}")

        # Intento B: lista todos y filtra client-side (case-insensitive)
        try:
            result = onecrm_get(ep, {"max_num": 100})
            diags.append(f"GET {ep} list → {str(result)[:200]}")
            records = result.get("records") or []
            for rec in records:
                stored_code = (rec.get("product_code") or "").strip().upper()
                if stored_code == modelo.strip().upper():
                    pid = rec.get("id")
                    if pid:
                        log.info(f"Producto encontrado ({ep} list): id={pid}")
                        return pid, diags
        except Exception as e:
            diags.append(f"GET {ep} list ERR: {str(e)[:100]}")

    log.warning(f"Producto no encontrado. Diagnóstico: {diags}")
    return None, diags


def crear_producto_en_crm(ficha: dict, modelo: str) -> str:
    """
    Crea el producto en 1CRM vía POST data/Product.
    Primero busca si ya existe por product_code para evitar duplicados (500).
    Si ya existe, reutiliza el ID existente.
    Devuelve el ID del producto.
    Raises RuntimeError con el response body si falla definitivamente.
    """
    log.info(f"Creando producto en 1CRM: {ficha['nombre']}")
    precio = round(float(ficha.get("precio_referencia") or 0), 2)
    payload: dict = {
        "name":         ficha["nombre"],
        "product_code": modelo,
        "description":  ficha["descripcion"],
        "price":        str(precio),
        "unit_price":   str(precio),
        "cost":         "0.00",
    }
    # Attach real currency UUID if available (avoid "-99" which fails DB INSERT)
    currency_id = get_crm_currency_id()
    if currency_id:
        payload["currency_id"] = currency_id

    # product_category_id is NOT NULL with no default in this 1CRM instance (MySQL 1364)
    category_id = get_crm_category_id()
    if category_id:
        payload["product_category_id"] = category_id

    log.info(f"POST payload currency_id={currency_id or '(omitted)'} category_id={category_id or '(omitted)'}")

    # Check if product already exists before POST (avoids 500 duplicate constraint)
    existing_id, pre_diags = buscar_producto_por_codigo(modelo)
    if existing_id:
        log.info(f"Producto ya existe en 1CRM (id={existing_id}) — reutilizando")
        return existing_id

    try:
        result = onecrm_post("data/Product", payload)

        # Log full response for debugging — 1CRM can return ID under different keys
        log.info(f"1CRM POST data/Product → full response: {str(result)[:1000]}")

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
        onecrm_patch(f"data/Product/{product_id}", {"picture": foto_url})
        log.info("Imagen vinculada via PATCH picture OK")
        return True
    except Exception as e:
        log.warning(f"PATCH picture falló: {e} — intentando upload multipart")

    # Estrategia 2: Upload multipart via /files
    try:
        img_resp = httpx.get(foto_url, timeout=20)
        img_resp.raise_for_status()
        img_bytes = img_resp.content

        resp = httpx.post(
            f"{ONECRM_BASE}/api.php/files",
            auth=(os.environ["ONECRM_USERNAME"], os.environ["ONECRM_PASSWORD"]),
            files={"file": ("product.png", img_bytes, "image/png")},
            data={
                "parent_type": "Product",
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
        crm_url = f"{ONECRM_BASE}/index.php?module=Products&record={product_id}"
        supabase.table("notificaciones").insert({
            "tipo":    "producto_publicado",
            "titulo":  f"Publicado en 1CRM — {marca} {modelo}",
            "mensaje": (
                f"Producto creado exitosamente en el catálogo de 1CRM.\n"
                f"Imagen: {'subida ✓' if imagen_subida else 'pendiente ⚠'}\n"
                f"Ver en 1CRM: {crm_url}"
            ),
            "rfq_id":  rfq_uuid,
            "leida":   False,
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


# ─────────────────────────────────────────
# LOOP PRINCIPAL — POLLING
# ─────────────────────────────────────────
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
                supabase.table("rfqs").update({
                    "estado": "foto_lista",
                }).eq("id", job["rfq_id"]).execute()
                log.info(f"  Job publicador {job['id']} reseteado")
        else:
            log.info("Sin jobs huérfanos de publicador al arrancar")
    except Exception as e:
        log.error(f"Error reseteando jobs huérfanos publicador: {e}")


def main():
    log.info("Agente Publicador iniciado — escuchando jobs...")
    log.info(f"1CRM: {ONECRM_BASE}")
    log.info(f"Supabase: {os.environ['SUPABASE_URL']}")
    resetear_jobs_huerfanos()

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
