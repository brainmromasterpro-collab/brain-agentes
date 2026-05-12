"""
AGENTE IMAGEN — Brain · MRO Master Pro
========================================
Worker que corre en Railway. Escucha jobs tipo 'imagen' en Supabase,
busca fotos del producto en Google Images, las evalúa con Claude Vision,
remueve el fondo con Remove.bg, optimiza a 500x500px y sube a Supabase Storage.

Trigger: Job con agente='imagen' y estado='pendiente', creado desde Bolt
         cuando el gerente aprueba publicar un producto nuevo en 1CRM.

Flujo:
  1. Google Custom Search (searchType=image) → 5 URLs candidatas
  2. Claude Vision evalúa y elige la mejor
  3. Remove.bg quita el fondo (PNG transparente)
  4. Pillow → canvas blanco 500x500px centrado
  5. Supabase Storage (bucket: product-images) → URL pública
  6. Actualiza rfqs.foto_url y crea notificación

Variables de entorno:
  ANTHROPIC_API_KEY=
  SUPABASE_URL=
  SUPABASE_SERVICE_KEY=
  GOOGLE_API_KEY=        # misma que el buscador
  GOOGLE_CX=             # misma que el buscador
  REMOVEBG_API_KEY=      # https://www.remove.bg/dashboard#api-key
"""

import os
import io
import time
import json
import base64
import logging
from datetime import datetime
from dotenv import load_dotenv

import httpx
import anthropic
from PIL import Image
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agente_imagen")

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"],
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

POLL_INTERVAL = 10
BUCKET = "product-images"


# ─────────────────────────────────────────
# PASO 1 — BUSCAR IMÁGENES EN GOOGLE
# ─────────────────────────────────────────
def buscar_imagenes_google(marca: str, modelo: str) -> list[str]:
    """
    Devuelve hasta 5 URLs de imágenes del producto.
    Intenta múltiples queries en orden de especificidad si la primera no devuelve resultados.
    """
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    cx = os.environ.get("GOOGLE_CX", "").strip()
    if not api_key or not cx:
        log.warning("Sin GOOGLE_API_KEY o GOOGLE_CX — búsqueda de imágenes desactivada")
        return []

    # Degradar el modelo para queries más genéricas:
    # Los part numbers industriales tienen sufijos de variante (ej. "3RT2028-1AK60-0XB0")
    # Separar por guion y tomar partes progresivamente más cortas
    partes = modelo.split("-")
    modelo_base = partes[0]                        # "3RT2028"
    modelo_corto = "-".join(partes[:2]) if len(partes) > 1 else modelo  # "3RT2028-1AK60"

    # Queries en orden: más específico → más genérico
    queries = [
        f"{marca} {modelo} product image",                    # full + context
        f"{marca} {modelo_corto} product image",              # sin último sufijo
        f"{marca} {modelo_base} product image white background",  # solo base model
        f"{marca} {modelo_base}",                             # base model sin contexto
        f"{marca} {modelo_base} industrial catalog",          # base model + categoría
    ]
    # Eliminar duplicados si el modelo no tiene guiones (ya son iguales)
    queries = list(dict.fromkeys(queries))

    for query in queries:
        log.info(f"Google Images: probando query → '{query}'")
        try:
            resp = httpx.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": api_key,
                    "cx": cx,
                    "q": query,
                    "searchType": "image",
                    "num": 5,
                    "imgSize": "medium",
                    "safe": "active",
                },
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            urls = [item["link"] for item in items if "link" in item]
            if urls:
                log.info(f"Google Images: {len(urls)} URLs encontradas con query '{query}'")
                return urls
            log.info(f"Google Images: sin resultados para '{query}', intentando siguiente...")
        except Exception as e:
            log.error(f"Error buscando con query '{query}': {e}")

    log.warning(f"Google Images: sin resultados tras {len(queries)} intentos")
    return []


# ─────────────────────────────────────────
# PASO 2 — DESCARGAR CANDIDATAS
# ─────────────────────────────────────────
def descargar_imagen(url: str) -> bytes | None:
    """Descarga una imagen y valida que sea realmente una imagen."""
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            log.debug(f"No es imagen ({content_type}): {url}")
            return None
        return resp.content
    except Exception as e:
        log.debug(f"No se pudo descargar {url}: {e}")
        return None


def descargar_candidatas(urls: list[str]) -> list[dict]:
    """Descarga todas las URLs y filtra las que tienen resolución mínima 200x200."""
    candidatas = []
    for url in urls:
        img_bytes = descargar_imagen(url)
        if not img_bytes:
            continue
        try:
            img = Image.open(io.BytesIO(img_bytes))
            w, h = img.size
            if w >= 200 and h >= 200:
                candidatas.append({
                    "url": url,
                    "bytes": img_bytes,
                    "width": w,
                    "height": h,
                    "formato": img.format or "JPEG",
                })
                log.info(f"  ✓ {w}x{h} — {url[:80]}")
            else:
                log.debug(f"  ✗ resolución insuficiente {w}x{h}: {url[:60]}")
        except Exception as e:
            log.debug(f"  ✗ imagen inválida: {e}")
    log.info(f"{len(candidatas)} candidatas descargadas y válidas")
    return candidatas


# ─────────────────────────────────────────
# PASO 3 — CLAUDE VISION EVALÚA
# ─────────────────────────────────────────
def evaluar_con_claude_vision(marca: str, modelo: str, candidatas: list[dict]) -> int | None:
    """
    Envía las imágenes candidatas a Claude Vision.
    Devuelve el índice de la mejor, o None si ninguna es aceptable.
    """
    log.info(f"Claude Vision evaluando {len(candidatas)} imágenes")

    media_map = {
        "JPEG": "image/jpeg",
        "PNG":  "image/png",
        "WEBP": "image/webp",
        "GIF":  "image/gif",
    }

    content = []
    content.append({
        "type": "text",
        "text": (
            f"Eres un especialista en catálogos de productos industriales.\n\n"
            f"Evalúa estas {len(candidatas)} imágenes para el producto: **{marca} {modelo}**\n\n"
            f"Criterios de aceptación:\n"
            f"1. ¿Muestra el producto correcto ({marca} {modelo})?\n"
            f"2. ¿Solo el producto, sin personas ni contexto de ambiente?\n"
            f"3. ¿Resolución suficiente (ya filtrado ≥200px, pero ¿se ve nítida)?\n"
            f"4. ¿Ángulo frontal o 3/4 (no trasero ni lateral extremo)?\n\n"
            f"Responde SOLO con este JSON:\n"
            f'{{"evaluaciones":[{{"indice":0,"decision":"ACEPTA|RECHAZA","razon":"breve"}},...], '
            f'"mejor_indice":0,"resumen":"texto breve"}}\n'
            f'Si ninguna es aceptable: {{"evaluaciones":[...],"mejor_indice":null,"resumen":"..."}}'
        ),
    })

    for i, cand in enumerate(candidatas):
        media_type = media_map.get(cand["formato"], "image/jpeg")
        try:
            b64 = base64.standard_b64encode(cand["bytes"]).decode("utf-8")
            content.append({
                "type": "text",
                "text": f"\n**Imagen {i}** ({cand['width']}x{cand['height']}px):",
            })
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
        except Exception as e:
            log.warning(f"Error preparando imagen {i} para Claude: {e}")
            content.append({"type": "text", "text": f"\n**Imagen {i}**: no se pudo incluir"})

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": content}],
        )
        text = response.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        mejor = result.get("mejor_indice")
        log.info(f"Claude Vision: mejor_indice={mejor} | {result.get('resumen', '')}")
        return mejor
    except Exception as e:
        log.error(f"Error en Claude Vision: {e}")
        # Fallback: devolver índice 0 si hubo error al parsear
        return 0 if candidatas else None


# ─────────────────────────────────────────
# PASO 4 — REMOVE.BG
# ─────────────────────────────────────────
def remover_fondo(imagen_bytes: bytes, imagen_url: str) -> bytes | None:
    """
    Llama a remove.bg con la URL primero (gratis y más rápido).
    Si falla, intenta con los bytes directamente.
    Devuelve PNG sin fondo, o None si no hay API key o falla.
    """
    api_key = os.environ.get("REMOVEBG_API_KEY", "").strip()
    if not api_key:
        log.warning("Sin REMOVEBG_API_KEY — se omite remoción de fondo")
        return None

    log.info("Remove.bg: removiendo fondo")
    # Intento 1: URL (consume menos cuota)
    try:
        resp = httpx.post(
            "https://api.remove.bg/v1.0/removebg",
            headers={"X-Api-Key": api_key},
            data={"image_url": imagen_url, "size": "auto"},
            timeout=30,
        )
        if resp.status_code == 200:
            log.info(f"Remove.bg OK via URL — {len(resp.content)} bytes")
            return resp.content
        log.warning(f"Remove.bg URL falló ({resp.status_code}), intentando upload directo")
    except Exception as e:
        log.warning(f"Remove.bg URL excepción: {e}, intentando upload directo")

    # Intento 2: bytes directos
    try:
        resp = httpx.post(
            "https://api.remove.bg/v1.0/removebg",
            headers={"X-Api-Key": api_key},
            files={"image_file": ("image.jpg", imagen_bytes, "image/jpeg")},
            data={"size": "auto"},
            timeout=30,
        )
        resp.raise_for_status()
        log.info(f"Remove.bg OK via upload — {len(resp.content)} bytes")
        return resp.content
    except Exception as e:
        log.error(f"Remove.bg falló en ambos intentos: {e}")
        return None


# ─────────────────────────────────────────
# PASO 5 — OPTIMIZAR 500×500
# ─────────────────────────────────────────
def optimizar_500x500(imagen_bytes: bytes) -> bytes:
    """
    Abre la imagen (con o sin fondo), crea un canvas blanco 500x500,
    centra el producto y devuelve PNG optimizado.
    """
    log.info("Pillow: optimizando a 500x500px")
    img = Image.open(io.BytesIO(imagen_bytes)).convert("RGBA")

    # Canvas blanco RGBA
    canvas = Image.new("RGBA", (500, 500), (255, 255, 255, 255))

    # Escalar con margen (480px máx) manteniendo proporción
    img.thumbnail((480, 480), Image.LANCZOS)

    # Centrar
    x = (500 - img.width) // 2
    y = (500 - img.height) // 2

    # Pegar respetando transparencia si existe
    if img.mode == "RGBA":
        canvas.paste(img, (x, y), img)
    else:
        canvas.paste(img, (x, y))

    # Convertir a RGB con fondo blanco sólido
    fondo = Image.new("RGB", (500, 500), (255, 255, 255))
    fondo.paste(canvas, mask=canvas.split()[3])

    buf = io.BytesIO()
    fondo.save(buf, format="PNG", optimize=True)
    log.info("Pillow: imagen lista")
    return buf.getvalue()


# ─────────────────────────────────────────
# PASO 6 — SUPABASE STORAGE
# ─────────────────────────────────────────
def subir_a_storage(imagen_bytes: bytes, rfq_id: str, marca: str, modelo: str) -> str | None:
    """Sube la imagen PNG al bucket product-images y devuelve la URL pública."""
    log.info("Supabase Storage: subiendo imagen")
    try:
        nombre = f"{modelo}_{marca}_500x500.png".replace(" ", "_").replace("/", "-").replace(",", "")
        path = f"{rfq_id}/{nombre}"

        supabase.storage.from_(BUCKET).upload(
            path=path,
            file=imagen_bytes,
            file_options={"content-type": "image/png", "upsert": "true"},
        )

        url = supabase.storage.from_(BUCKET).get_public_url(path)
        log.info(f"Imagen pública: {url}")
        return url
    except Exception as e:
        log.error(f"Error subiendo a Supabase Storage: {e}")
        return None


# ─────────────────────────────────────────
# PROCESADOR PRINCIPAL
# ─────────────────────────────────────────
def procesar_job_imagen(job: dict) -> None:
    job_id  = job["id"]
    rfq_uuid = job["rfq_id"]
    log.info(f"=== Job imagen {job_id} | rfq {rfq_uuid} ===")

    # Marcar corriendo
    supabase.table("jobs").update({
        "estado": "corriendo",
        "started_at": datetime.utcnow().isoformat(),
    }).eq("id", job_id).execute()

    try:
        # Obtener RFQ
        rfq = supabase.table("rfqs").select("*").eq("id", rfq_uuid).single().execute().data
        marca  = rfq["marca"].strip().title()
        modelo = rfq["modelo"].strip()

        # ── Paso 1: Buscar ──────────────────────────────────────────────
        urls = buscar_imagenes_google(marca, modelo)
        if not urls:
            raise Exception("Google Images: sin resultados de búsqueda")

        # ── Paso 2: Descargar ───────────────────────────────────────────
        candidatas = descargar_candidatas(urls)
        if not candidatas:
            raise Exception("Ninguna imagen descargada superó el mínimo de resolución")

        # ── Paso 3: Claude Vision ───────────────────────────────────────
        mejor_idx = evaluar_con_claude_vision(marca, modelo, candidatas)

        if mejor_idx is None:
            # Ninguna imagen aceptable → estado foto_pendiente
            log.warning("Claude Vision: ninguna imagen aceptable → foto_pendiente")
            supabase.table("rfqs").update({"estado": "foto_pendiente"}).eq("id", rfq_uuid).execute()
            supabase.table("notificaciones").insert({
                "tipo": "foto_pendiente",
                "titulo": f"Foto requerida — {marca} {modelo}",
                "mensaje": (
                    f"Claude revisó {len(candidatas)} imágenes y ninguna es aceptable "
                    f"para {marca} {modelo}. Sube la foto manualmente desde Bolt."
                ),
                "rfq_id": rfq_uuid,
                "leida": False,
            }).execute()
            supabase.table("jobs").update({
                "estado": "foto_pendiente",
                "finished_at": datetime.utcnow().isoformat(),
                "output": {"resultado": "foto_pendiente", "candidatas": len(candidatas)},
            }).eq("id", job_id).execute()
            return

        ganadora = candidatas[mejor_idx]
        log.info(f"Imagen ganadora [idx={mejor_idx}]: {ganadora['width']}x{ganadora['height']} — {ganadora['url'][:80]}")

        # ── Paso 4: Remove.bg ───────────────────────────────────────────
        img_sin_fondo = remover_fondo(ganadora["bytes"], ganadora["url"])

        # ── Paso 5: Optimizar 500×500 ───────────────────────────────────
        img_final = optimizar_500x500(img_sin_fondo if img_sin_fondo else ganadora["bytes"])

        # ── Paso 6: Supabase Storage ────────────────────────────────────
        foto_url = subir_a_storage(img_final, rfq_uuid, marca, modelo)
        if not foto_url:
            raise Exception("Fallo al subir imagen a Supabase Storage")

        # ── Actualizar RFQ ──────────────────────────────────────────────
        supabase.table("rfqs").update({
            "foto_url":    foto_url,
            "estado":      "foto_lista",
        }).eq("id", rfq_uuid).execute()

        # ── Notificar ───────────────────────────────────────────────────
        supabase.table("notificaciones").insert({
            "tipo":    "foto_lista",
            "titulo":  f"Foto lista — {marca} {modelo}",
            "mensaje": "Imagen procesada (500×500, fondo blanco) y lista para publicar en 1CRM.",
            "rfq_id":  rfq_uuid,
            "leida":   False,
        }).execute()

        # ── Cerrar job ──────────────────────────────────────────────────
        supabase.table("jobs").update({
            "estado":      "completado",
            "finished_at": datetime.utcnow().isoformat(),
            "output": {
                "foto_url":      foto_url,
                "imagen_fuente": ganadora["url"],
                "removebg":      img_sin_fondo is not None,
            },
        }).eq("id", job_id).execute()

        log.info(f"Job imagen {job_id} completado ✓ — {foto_url}")

    except Exception as e:
        log.error(f"Job imagen {job_id} falló: {e}")
        supabase.table("jobs").update({
            "estado":      "fallido",
            "finished_at": datetime.utcnow().isoformat(),
            "error":       str(e),
        }).eq("id", job_id).execute()

        # ── IMPORTANTE: poner rfq.estado = 'foto_pendiente' para que el UI muestre ──
        # el widget de reintento / upload manual. NO usar 'busqueda_completa' porque
        # el polling del frontend busca 'foto_pendiente' para mostrar el widget correcto.
        try:
            supabase.table("rfqs").update({
                "estado": "foto_pendiente",   # UI detecta esto y muestra retry / upload manual
            }).eq("id", rfq_uuid).execute()

            supabase.table("notificaciones").insert({
                "tipo":    "imagen_fallida",
                "titulo":  f"Error procesando imagen — revisar",
                "mensaje": (
                    f"El agente de imágenes no pudo obtener una foto válida.\n"
                    f"Error: {str(e)}\n"
                    f"Puedes subir una foto manualmente o intentar publicar de nuevo."
                ),
                "rfq_id":  rfq_uuid,
                "leida":   False,
            }).execute()
            log.info(f"RFQ {rfq_uuid} → foto_pendiente tras fallo de imagen")
        except Exception as e2:
            log.error(f"Error reseteando rfq tras fallo: {e2}")


# ─────────────────────────────────────────
# LOOP PRINCIPAL — POLLING
# ─────────────────────────────────────────
def resetear_jobs_huerfanos():
    """
    Al arrancar, resetea jobs de imagen que quedaron en 'corriendo'
    por un redeploy anterior. Los vuelve a 'pendiente' para que
    sean reintentados, y también resetea el rfq a 'procesando_imagen'.
    """
    try:
        huerfanos = (
            supabase.table("jobs")
            .select("id, rfq_id")
            .eq("agente", "imagen")
            .eq("estado", "corriendo")
            .execute()
            .data or []
        )
        if huerfanos:
            log.warning(f"⚠ {len(huerfanos)} job(s) de imagen huérfanos — reseteando a pendiente")
            for job in huerfanos:
                supabase.table("jobs").update({
                    "estado":     "pendiente",
                    "started_at": None,
                    "error":      "Reseteado por reinicio del agente (redeploy)",
                }).eq("id", job["id"]).execute()
                # Asegurar que el rfq esté en procesando_imagen (no busqueda_completa)
                supabase.table("rfqs").update({
                    "estado": "procesando_imagen",
                }).eq("id", job["rfq_id"]).execute()
                log.info(f"  Job {job['id']} (rfq {job['rfq_id']}) reseteado a pendiente")
        else:
            log.info("Sin jobs huérfanos de imagen al arrancar")
    except Exception as e:
        log.error(f"Error reseteando jobs huérfanos: {e}")


def main():
    log.info("Agente Imagen iniciado — escuchando jobs...")
    log.info(f"Supabase: {os.environ['SUPABASE_URL']}")
    log.info(f"Bucket:   {BUCKET}")

    google_key  = os.environ.get("GOOGLE_API_KEY", "")
    google_cx   = os.environ.get("GOOGLE_CX", "")
    removebg    = os.environ.get("REMOVEBG_API_KEY", "")

    log.info(f"Google Images:  {'OK' if google_key and google_cx else '⚠ NO CONFIGURADO'}")
    log.info(f"Remove.bg:      {'OK' if removebg else '⚠ NO CONFIGURADO (se omitirá)'}")

    # Resetear jobs que quedaron colgados por redeploy anterior
    resetear_jobs_huerfanos()

    while True:
        try:
            resp = (
                supabase.table("jobs")
                .select("*")
                .eq("agente", "imagen")
                .eq("estado", "pendiente")
                .order("created_at")
                .limit(1)
                .execute()
            )
            if resp.data:
                procesar_job_imagen(resp.data[0])
            else:
                log.debug("Sin jobs de imagen pendientes")
        except Exception as e:
            log.error(f"Error en loop principal: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
