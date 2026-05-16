"""
AGENTE EXTRACTOR — Brain · MRO Master Pro
==========================================
Worker que corre en Railway.

Recibe un job con tipo 'extractor'. El job lleva en el campo `input` (jsonb):
  {
    "imagen_url": "https://...supabase.../storage/v1/object/public/...",
    "stream_id": "uuid-del-stream"
  }

1. Descarga la imagen desde Supabase Storage
2. Llama a Claude Vision para extraer la tabla de modelos + marcas
3. Crea un rfq + job buscador por cada producto encontrado
4. Publica una notificación con el resumen en el stream

Dependencias (ya en requirements.txt del proyecto):
  pip install anthropic supabase httpx python-dotenv

Variables de entorno:
  ANTHROPIC_API_KEY=
  SUPABASE_URL=
  SUPABASE_SERVICE_KEY=
"""

import os
import re
import time
import json
import base64
import logging
from datetime import datetime

import httpx
import anthropic
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("agente_extractor")

# ─────────────────────────────────────────
# CLIENTES
# ─────────────────────────────────────────
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"],
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

POLL_INTERVAL = 10  # segundos


# ─────────────────────────────────────────
# EXTRACCIÓN CON CLAUDE VISION
# ─────────────────────────────────────────
EXTRACTION_PROMPT = """\
Analiza esta imagen. Contiene una tabla o lista de productos industriales/MRO.
Extrae TODOS los productos que aparecen y devuelve ÚNICAMENTE un JSON array con este formato:

[
  {"modelo": "XA2EVB4LC", "marca": "Schneider"},
  {"modelo": "1756-PA72", "marca": "Allen Bradley"},
  {"modelo": "BTL5-E10-M0460-K-SR32", "marca": "Balluff"}
]

Reglas estrictas:
- Copia el número de modelo/parte EXACTAMENTE como aparece en la imagen (mayúsculas, guiones, espacios)
- Si hay columna de marca/fabricante, inclúyela; si no es visible, usa cadena vacía ""
- Ignora encabezados de columna (p. ej. "Modelo", "Marca", "Cantidad")
- Ignora totales, fechas, nombres de empresa y texto que NO sea un código de parte
- Los colores de fila (rojo/amarillo/verde) son indicadores de urgencia, no afectan la extracción
- Si el mismo modelo aparece varias veces, inclúyelo solo una vez
- Devuelve SOLO el JSON array, sin texto previo ni bloques markdown (sin ``` ni explicaciones)
"""


def descargar_imagen(url: str) -> tuple[bytes, str]:
    """Descarga imagen y retorna (bytes, media_type)."""
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    ct = r.headers.get("content-type", "image/png").split(";")[0].strip()
    # Normalizar tipos comunes
    if ct in ("image/jpg",):
        ct = "image/jpeg"
    return r.content, ct


def extraer_productos_con_claude(imagen_bytes: bytes, media_type: str) -> list[dict]:
    """Usa Claude Vision para extraer lista de productos de una imagen."""
    imagen_b64 = base64.standard_b64encode(imagen_bytes).decode("utf-8")

    resp = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": imagen_b64,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    )

    raw = resp.content[0].text.strip()

    # Quitar bloque markdown si Claude lo añade pese a las instrucciones
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?```\s*$", "", raw)
    raw = raw.strip()

    productos = json.loads(raw)

    # Validar estructura y filtrar vacíos
    resultado = []
    for p in productos:
        if isinstance(p, dict) and p.get("modelo", "").strip():
            resultado.append({
                "modelo": p["modelo"].strip(),
                "marca":  p.get("marca", "").strip(),
            })
    return resultado


# ─────────────────────────────────────────
# CREACIÓN DE RFQs EN SUPABASE
# ─────────────────────────────────────────
def crear_rfq_y_job(stream_id: str, modelo: str, marca: str) -> str:
    """Inserta un rfq + job buscador. Retorna el rfq_id creado."""
    rfq_resp = supabase.table("rfqs").insert({
        "stream_id": stream_id,
        "modelo":    modelo,
        "marca":     marca,
        "estado":    "recibido",
        "urgente":   False,
    }).execute()

    rfq_id = rfq_resp.data[0]["id"]

    supabase.table("jobs").insert({
        "rfq_id": rfq_id,
        "agente": "buscador",
        "estado": "pendiente",
    }).execute()

    return rfq_id


# ─────────────────────────────────────────
# PROCESADOR PRINCIPAL
# ─────────────────────────────────────────
def procesar_job_extractor(job: dict) -> None:
    job_id   = job["id"]
    inp      = job.get("input") or {}
    imagen_url = inp.get("imagen_url", "")
    stream_id  = inp.get("stream_id", "")

    log.info(f"Extractor job {job_id} | stream={stream_id} | imagen={imagen_url[:70]}...")

    # Marcar como corriendo
    supabase.table("jobs").update({
        "estado":     "corriendo",
        "started_at": datetime.utcnow().isoformat(),
    }).eq("id", job_id).execute()

    rfq_ids: list[str] = []
    productos: list[dict] = []

    try:
        if not imagen_url:
            raise ValueError("El job no tiene imagen_url en el campo input")
        if not stream_id:
            raise ValueError("El job no tiene stream_id en el campo input")

        # 1 — Descargar imagen
        log.info("Descargando imagen...")
        imagen_bytes, media_type = descargar_imagen(imagen_url)
        log.info(f"Imagen: {len(imagen_bytes):,} bytes | tipo: {media_type}")

        # 2 — Claude Vision: extraer productos
        log.info("Extrayendo productos con Claude Vision...")
        productos = extraer_productos_con_claude(imagen_bytes, media_type)
        log.info(f"Claude encontró {len(productos)} producto(s)")

        if not productos:
            raise ValueError("Claude no encontró ningún producto en la imagen")

        # 3 — Crear rfq + job buscador por cada producto
        for p in productos:
            rfq_id = crear_rfq_y_job(stream_id, p["modelo"], p["marca"])
            rfq_ids.append(rfq_id)
            log.info(f"  → RFQ {rfq_id}: {p['modelo']} | {p['marca']}")

        # 4 — Notificación resumen en el stream
        lista_txt = "\n".join(
            f"• {p['modelo']}" + (f"  [{p['marca']}]" if p["marca"] else "")
            for p in productos
        )
        supabase.table("notificaciones").insert({
            "rfq_id":  rfq_ids[0],   # primer rfq para que quede en el stream
            "tipo":    "info",
            "titulo":  f"📋 {len(productos)} productos extraídos del screenshot",
            "mensaje": f"Iniciando búsquedas automáticas:\n{lista_txt}",
            "leida":   False,
        }).execute()

        # 5 — Cerrar job
        supabase.table("jobs").update({
            "estado":      "completado",
            "finished_at": datetime.utcnow().isoformat(),
            "output": {
                "productos_extraidos": len(productos),
                "rfq_ids":  rfq_ids,
                "productos": productos,
            },
        }).eq("id", job_id).execute()

        log.info(f"Job extractor {job_id} completado — {len(productos)} búsquedas lanzadas")

    except Exception as e:
        log.error(f"Job extractor {job_id} falló: {e}", exc_info=True)

        supabase.table("jobs").update({
            "estado":      "fallido",
            "finished_at": datetime.utcnow().isoformat(),
            "error":       str(e),
        }).eq("id", job_id).execute()

        # Notificación de error
        if stream_id:
            try:
                # Crear un rfq placeholder solo para poder anclar la notificación
                rfq_resp = supabase.table("rfqs").insert({
                    "stream_id": stream_id,
                    "modelo":    "[error-extractor]",
                    "marca":     "",
                    "estado":    "recibido",
                    "urgente":   False,
                }).execute()
                error_rfq_id = rfq_resp.data[0]["id"]

                supabase.table("notificaciones").insert({
                    "rfq_id":  error_rfq_id,
                    "tipo":    "error",
                    "titulo":  "❌ Error al procesar screenshot",
                    "mensaje": f"No se pudieron extraer productos: {str(e)}",
                    "leida":   False,
                }).execute()
            except Exception as ne:
                log.error(f"No se pudo enviar notificación de error: {ne}")


# ─────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────
def main() -> None:
    log.info("Agente Extractor iniciado — escuchando jobs tipo 'extractor'...")
    log.info(f"Supabase: {os.environ['SUPABASE_URL']}")
    log.info(f"Poll interval: {POLL_INTERVAL}s")

    # Recuperar jobs huérfanos (quedaron 'corriendo' por un crash anterior)
    try:
        huerfanos = (
            supabase.table("jobs")
            .select("id")
            .eq("agente", "extractor")
            .eq("estado", "corriendo")
            .execute()
            .data
        )
        if huerfanos:
            ids = [j["id"] for j in huerfanos]
            supabase.table("jobs").update({"estado": "pendiente"}).in_("id", ids).execute()
            log.info(f"Recuperados {len(ids)} jobs huérfanos → pendiente")
    except Exception as e:
        log.warning(f"No se pudieron recuperar jobs huérfanos: {e}")

    while True:
        try:
            resp = (
                supabase.table("jobs")
                .select("*")
                .eq("agente", "extractor")
                .eq("estado", "pendiente")
                .order("created_at")
                .limit(1)
                .execute()
            )
            jobs = resp.data
            if jobs:
                procesar_job_extractor(jobs[0])
            else:
                log.debug("Sin jobs extractor pendientes, esperando...")

        except Exception as e:
            log.error(f"Error en loop principal: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
