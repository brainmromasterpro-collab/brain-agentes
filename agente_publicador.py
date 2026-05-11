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
        log.error(f"1CRM POST error {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()
    return resp.json()


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
def crear_producto_en_crm(ficha: dict, modelo: str) -> str | None:
    """
    Crea el producto en 1CRM vía POST data/Product.
    Devuelve el ID del producto creado, o None si falla.
    """
    log.info(f"Creando producto en 1CRM: {ficha['nombre']}")
    try:
        payload = {
            "name":         ficha["nombre"],
            "product_code": modelo,
            "description":  ficha["descripcion"],
            "price":        str(round(float(ficha.get("precio_referencia") or 0), 2)),
            "unit_price":   str(round(float(ficha.get("precio_referencia") or 0), 2)),
            "currency_id":  "-99",   # moneda del sistema (ajustar si se necesita USD específico)
        }
        result = onecrm_post("data/Product", payload)
        product_id = result.get("id")
        if product_id:
            log.info(f"Producto creado en 1CRM: id={product_id}")
        else:
            log.warning(f"1CRM no devolvió ID. Respuesta: {result}")
        return product_id
    except Exception as e:
        log.error(f"Error creando producto en 1CRM: {e}")
        return None


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
        product_id = crear_producto_en_crm(ficha, modelo)
        if not product_id:
            raise Exception("1CRM no creó el producto — sin ID devuelto")

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
def main():
    log.info("Agente Publicador iniciado — escuchando jobs...")
    log.info(f"1CRM: {ONECRM_BASE}")
    log.info(f"Supabase: {os.environ['SUPABASE_URL']}")

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
