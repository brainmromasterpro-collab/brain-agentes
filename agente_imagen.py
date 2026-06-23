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
from config_agentes import get_config

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
def _build_queries(marca: str, modelo: str) -> list[str]:
    """Genera lista de queries degradando el modelo de específico a genérico."""
    partes = modelo.split("-")
    modelo_base  = partes[0]                                             # "3RT2028"
    modelo_corto = "-".join(partes[:2]) if len(partes) > 1 else modelo  # "3RT2028-1AK60"
    queries = [
        f"{marca} {modelo} product image",
        f"{marca} {modelo_corto} product image",
        f"{marca} {modelo_base} product image white background",
        f"{marca} {modelo_base}",
        f"{marca} {modelo_base} industrial catalog",
    ]
    return list(dict.fromkeys(queries))  # eliminar duplicados


def buscar_imagenes_google(marca: str, modelo: str) -> list[str]:
    """
    Intenta Google Custom Search (hasta 5 queries degradadas).
    Devuelve URLs de imágenes o [] si no hay resultados / cuota agotada.
    """
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    cx      = os.environ.get("GOOGLE_CX",      "").strip()
    if not api_key or not cx:
        log.warning("Sin GOOGLE_API_KEY o GOOGLE_CX — Google Images desactivado")
        return []

    for query in _build_queries(marca, modelo):
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
                log.info(f"Google Images: {len(urls)} URLs con query '{query}'")
                return urls
            log.info(f"Google Images: sin resultados para '{query}', probando siguiente...")
        except Exception as e:
            log.error(f"Google Images error '{query}': {e}")

    log.warning("Google Images: sin resultados tras todos los intentos")
    return []


def buscar_imagenes_serpapi(marca: str, modelo: str) -> list[str]:
    """
    Fallback: SerpAPI Google Images. Se usa cuando Google Custom Search falla o
    agota cuota. SerpAPI tiene cuota independiente y suele tener mejores resultados
    para part-numbers industriales poco conocidos.
    """
    serpapi_key = os.environ.get("SERPAPI_KEY", "").strip()
    if not serpapi_key:
        log.warning("Sin SERPAPI_KEY — SerpAPI Images desactivado")
        return []

    for query in _build_queries(marca, modelo):
        log.info(f"SerpAPI Images: probando query → '{query}'")
        try:
            resp = httpx.get(
                "https://serpapi.com/search",
                params={
                    "engine":  "google_images",
                    "q":       query,
                    "api_key": serpapi_key,
                    "num":     5,
                },
                timeout=20,
            )
            resp.raise_for_status()
            images = resp.json().get("images_results", [])
            urls = [img["original"] for img in images[:5] if img.get("original")]
            if urls:
                log.info(f"SerpAPI Images: {len(urls)} URLs con query '{query}'")
                return urls
            log.info(f"SerpAPI Images: sin resultados para '{query}', probando siguiente...")
        except Exception as e:
            log.error(f"SerpAPI Images error '{query}': {e}")

    log.warning("SerpAPI Images: sin resultados tras todos los intentos")
    return []


def buscar_imagenes(marca: str, modelo: str) -> list[str]:
    """
    Busca imágenes con Google Custom Search; si falla o da 0 resultados,
    usa SerpAPI como fallback.
    """
    urls = buscar_imagenes_google(marca, modelo)
    if urls:
        return urls
    log.info("Google Images no devolvió resultados — intentando SerpAPI como fallback")
    return buscar_imagenes_serpapi(marca, modelo)



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
            f"Eres un especialista en catálogos de productos industriales MRO.\n\n"
            f"Evalúa estas {len(candidatas)} imágenes para el producto: **{marca} {modelo}**\n\n"
            f"ACEPTA la imagen si:\n"
            f"- Se ve claramente el producto o un producto del mismo tipo/familia\n"
            f"- No tiene texto de marca de agua grande que tape el producto\n"
            f"- No es solo un logo, icono o imagen de 'producto no disponible'\n"
            f"- El producto es reconocible (fondo blanco, catálogo, foto técnica — todos OK)\n\n"
            f"RECHAZA solo si:\n"
            f"- Es un logo, ícono o placeholder (no muestra el producto real)\n"
            f"- Tiene una filigrana/watermark que cubre más del 30% del producto\n"
            f"- Es completamente irrelevante (muestra algo totalmente distinto)\n\n"
            f"IMPORTANTE: Para piezas industriales (fittings, cables, transmisores, válvulas, etc.) "
            f"las fotos de catálogo con fondo de color, varias piezas juntas, o en contexto de instalación "
            f"son PERFECTAMENTE aceptables. Sé generoso — prefiere ACEPTA ante la duda.\n\n"
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
        cfg = get_config("imagen")
        extra = {"system": cfg["system_prompt"]} if cfg["system_prompt"] else {}
        response = claude.messages.create(
            model=cfg["model_id"],
            max_tokens=cfg["max_tokens"],
            temperature=cfg["temperature"],
            messages=[{"role": "user", "content": content}],
            **extra,
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
# AUTO-PUBLICAR SIN IMAGEN
# ─────────────────────────────────────────
def _auto_publicar_sin_imagen(rfq_uuid: str, marca: str, modelo: str, motivo: str) -> None:
    """
    Cuando no se puede obtener una imagen aceptable (ni por Vision ni por error),
    crea automáticamente un job de publicador para que el producto llegue a 1CRM
    sin imagen. El gerente recibirá una notificación específica con el enlace de
    edición para subir la foto manualmente cuando la consiga.
    """
    # 1. Crear job de publicador
    nuevo_job = supabase.table("jobs").insert({
        "agente":     "publicador",
        "rfq_id":     rfq_uuid,
        "estado":     "pendiente",
        "created_at": datetime.utcnow().isoformat(),
    }).execute()
    job_id_pub = (nuevo_job.data or [{}])[0].get("id", "?")
    log.info(f"Job publicador creado automáticamente: {job_id_pub} (sin imagen)")

    # 2. Actualizar estado del RFQ a 'publicando' (igual que si el gerente lo aprobara)
    supabase.table("rfqs").update({"estado": "publicando"}).eq("id", rfq_uuid).execute()

    # 3. Notificación específica al gerente
    supabase.table("notificaciones").insert({
        "tipo":    "publicando_sin_imagen",
        "titulo":  f"Sin imagen — publicando igual — {marca} {modelo}",
        "mensaje": (
            f"No se encontró una imagen limpia para {marca} {modelo}.\n"
            f"Motivo: {motivo}\n\n"
            f"El producto se publicará automáticamente en 1CRM sin imagen. "
            f"Cuando consigas la foto, súbela desde el EditView del producto en 1CRM."
        ),
        "rfq_id":  rfq_uuid,
        "leida":   False,
    }).execute()
    log.info(f"Notificación 'publicando_sin_imagen' enviada para rfq {rfq_uuid}")


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
        # Intenta Google Custom Search primero; si falla/cuota agotada, usa SerpAPI
        urls = buscar_imagenes(marca, modelo)
        if not urls:
            raise Exception("Sin resultados de imagen — Google Images y SerpAPI fallaron")

        # ── Paso 2: Descargar ───────────────────────────────────────────
        candidatas = descargar_candidatas(urls)
        if not candidatas:
            raise Exception("Ninguna imagen descargada superó el mínimo de resolución")

        # ── Paso 3: Claude Vision ───────────────────────────────────────
        mejor_idx = evaluar_con_claude_vision(marca, modelo, candidatas)

        if mejor_idx is None:
            log.warning("Claude Vision: ninguna imagen aceptable")
            stream_id = rfq.get("stream_id")
            if stream_id:
                # HITL: notificar al chat para que el usuario decida
                supabase.table("mensajes").insert({
                    "stream_id": stream_id,
                    "role":      "user",
                    "content":   (
                        f"[SISTEMA:imagen_no_encontrada] rfq_id={rfq_uuid} "
                        f"marca={marca} modelo={modelo} "
                        f"motivo=Claude revisó {len(candidatas)} imagen(es) y ninguna es aceptable"
                    ),
                    "procesado": False,
                    "metadata":  {"trigger": "imagen_no_encontrada", "rfq_id": rfq_uuid},
                }).execute()
                supabase.table("rfqs").update({"estado": "sin_imagen"}).eq("id", rfq_uuid).execute()
                supabase.table("notificaciones").insert({
                    "tipo":    "imagen_no_encontrada",
                    "titulo":  f"Sin imagen — {marca} {modelo}",
                    "mensaje": "No se encontró una imagen aceptable para este producto. Puedes publicarlo sin imagen o reintentar la búsqueda.",
                    "rfq_id":  rfq_uuid,
                    "leida":   False,
                }).execute()
            else:
                # Automático: publicar sin imagen
                _auto_publicar_sin_imagen(
                    rfq_uuid, marca, modelo,
                    motivo=f"Claude revisó {len(candidatas)} imagen(es) y ninguna es aceptable (logos, filigranas o baja calidad).",
                )
            supabase.table("jobs").update({
                "estado":      "sin_imagen",
                "finished_at": datetime.utcnow().isoformat(),
                "output":      {"resultado": "sin_imagen", "candidatas": len(candidatas)},
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

        # ── Guardar foto_url, luego decidir modo ────────────────────────
        stream_id = rfq.get("stream_id")
        if stream_id:
            # HITL: actualizar foto pero NO lanzar publicador — esperar aprobación del usuario
            supabase.table("rfqs").update({
                "foto_url": foto_url,
                "estado":   "foto_lista",
            }).eq("id", rfq_uuid).execute()
            supabase.table("mensajes").insert({
                "stream_id": stream_id,
                "role":      "user",
                "content":   (
                    f"[SISTEMA:imagen_lista] rfq_id={rfq_uuid} "
                    f"marca={marca} modelo={modelo} "
                    f"foto_url={foto_url}"
                ),
                "procesado": False,
                "metadata":  {"trigger": "imagen_lista", "rfq_id": rfq_uuid, "foto_url": foto_url},
            }).execute()
            log.info(f"HITL: imagen lista — esperando aprobación del usuario en stream {str(stream_id)[:8]}")
        else:
            # Automático: publicar de inmediato
            supabase.table("rfqs").update({
                "foto_url": foto_url,
                "estado":   "publicando",
            }).eq("id", rfq_uuid).execute()

            pub_job = supabase.table("jobs").insert({
                "rfq_id":     rfq_uuid,
                "agente":     "publicador",
                "estado":     "pendiente",
                "created_at": datetime.utcnow().isoformat(),
            }).execute()
            pub_job_id = (pub_job.data or [{}])[0].get("id", "?")
            log.info(f"Job publicador creado automáticamente: {pub_job_id}")

            supabase.table("notificaciones").insert({
                "tipo":    "foto_lista",
                "titulo":  f"Foto lista — publicando — {marca} {modelo}",
                "mensaje": "Imagen procesada (500×500, fondo blanco). Publicando automáticamente en 1CRM.",
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

        # Fallback: sin imagen — en HITL notificar al chat, en automático publicar directo
        try:
            rfq_fb = supabase.table("rfqs").select("stream_id,marca,modelo").eq("id", rfq_uuid).single().execute().data
            stream_id_fb = (rfq_fb or {}).get("stream_id")
            marca_fb  = (rfq_fb or {}).get("marca", marca)
            modelo_fb = (rfq_fb or {}).get("modelo", modelo)
            if stream_id_fb:
                supabase.table("mensajes").insert({
                    "stream_id": stream_id_fb,
                    "role":      "user",
                    "content":   (
                        f"[SISTEMA:imagen_no_encontrada] rfq_id={rfq_uuid} "
                        f"marca={marca_fb} modelo={modelo_fb} "
                        f"motivo=Error en agente imagen: {str(e)[:150]}"
                    ),
                    "procesado": False,
                    "metadata":  {"trigger": "imagen_no_encontrada", "rfq_id": rfq_uuid},
                }).execute()
                supabase.table("rfqs").update({"estado": "sin_imagen"}).eq("id", rfq_uuid).execute()
                supabase.table("notificaciones").insert({
                    "tipo":    "imagen_no_encontrada",
                    "titulo":  f"Sin imagen — {marca_fb} {modelo_fb}",
                    "mensaje": "No se pudo obtener una imagen para este producto. Puedes publicarlo sin imagen o reintentar la búsqueda.",
                    "rfq_id":  rfq_uuid,
                    "leida":   False,
                }).execute()
            else:
                _auto_publicar_sin_imagen(
                    rfq_uuid, marca_fb, modelo_fb,
                    motivo=f"Error en agente de imágenes: {str(e)[:200]}",
                )
            log.info(f"RFQ {rfq_uuid} → fallback sin imagen gestionado")
        except Exception as e2:
            log.error(f"Error en fallback sin imagen: {e2}")


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
            # Estados donde tiene sentido retomar el procesamiento de imagen
            ESTADOS_IMAGEN = {"busqueda_completa", "procesando_imagen"}
            for job in huerfanos:
                supabase.table("jobs").update({
                    "estado":     "pendiente",
                    "started_at": None,
                    "error":      "Reseteado por reinicio del agente (redeploy)",
                }).eq("id", job["id"]).execute()

                # Solo tocar el rfq si sigue en estado de imagen.
                # Si ya avanzó a foto_lista / publicando / publicado / etc.,
                # NO retroceder — causaría el loop publicar→foto_lista→publicar.
                rfq_data = (
                    supabase.table("rfqs")
                    .select("estado")
                    .eq("id", job["rfq_id"])
                    .single()
                    .execute()
                    .data or {}
                )
                estado_actual = rfq_data.get("estado", "")
                if estado_actual in ESTADOS_IMAGEN:
                    supabase.table("rfqs").update({
                        "estado": "procesando_imagen",
                    }).eq("id", job["rfq_id"]).execute()
                    log.info(f"  Job {job['id']} reseteado; rfq → procesando_imagen")
                else:
                    log.info(f"  Job {job['id']} reseteado; rfq queda en '{estado_actual}' (no retroceder)")
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
