"""
LECTOR DE FICHAS TÉCNICAS DE PRODUCTO — stream 'busquedas'
===========================================================
Lee la ficha técnica de un producto (PDF / Excel / Word) que comparte el usuario, saca el TEXTO de
forma determinista (reutiliza el parsing de orden_compra.py, sin costo de visión) y un LLM chico lo
estructura a una LISTA de productos con el MISMO esquema que usa extraer_producto_de_link (link de
página web), para poder publicarlos con las tools publicar_producto_link / publicar_productos_desde_links
ya existentes sin tocar el pipeline de publicación.

Regla de oro (igual que en orden_compra): NO inventar. Si un dato no está claro, se deja vacío.

Si el documento describe UN producto base con VARIAS variantes/SKU (tabla de Item No. con distintas
medidas/capacidades), o trae accesorios/repuestos con su propio Item No., se genera UN producto POR
CADA Item No. — el chat decide si publica uno o varios (vía el mismo widget de MODO 13).
"""

import os
import json
import logging

import orden_compra
import llm

log = logging.getLogger("ficha_tecnica")

FICHA_MODEL = os.environ.get("FICHA_MODEL", "claude-haiku-4-5-20251001")

_NORM_SYS = (
    "Eres un extractor de datos de FICHAS TÉCNICAS de productos industriales (datasheets de "
    "fabricantes, folletos de producto). Recibes el TEXTO crudo del documento y devuelves SOLO un "
    "JSON válido, sin explicaciones ni ```.\n\n"
    "Esquema exacto:\n"
    "{\n"
    '  "productos": [\n'
    "    {\n"
    '      "nombre": "nombre comercial del producto (modelo base + descripción corta)",\n'
    '      "part_number": "número de parte / modelo / Item No. EXACTO tal como aparece",\n'
    '      "marca": "marca/fabricante",\n'
    '      "descripcion": "descripción general del producto (uso, tipo, funcionamiento), SIN la lista de specs",\n'
    '      "caracteristicas": ["Etiqueta: valor unidad", "..."],\n'
    '      "precio_costo": "",\n'
    '      "moneda": ""\n'
    "    }\n"
    "  ],\n"
    '  "notas": "cualquier ambigüedad o dato dudoso, en una línea"\n'
    "}\n\n"
    "REGLAS:\n"
    "- NO inventes. Si un dato no está claro o no aparece, déjalo vacío (\"\" o []).\n"
    "- Si el documento describe UN producto base con VARIAS variantes/SKU (tabla de Item No. con "
    "distintas medidas/capacidades), genera UN producto POR CADA Item No./SKU de esa tabla, heredando "
    "las características generales del datasheet MÁS su spec distintiva (p.ej. ancho/grosor propio de "
    "esa fila).\n"
    "- Si además aparecen accesorios, repuestos o partes relacionadas (baterías, cargadores, soportes) "
    "CON su propio Item No., inclúyelos TAMBIÉN como productos separados (son código de parte "
    "independiente).\n"
    "- part_number es el identificador (Item No./SKU/modelo), NUNCA la descripción.\n"
    "- Copia part numbers, códigos y unidades EXACTAMENTE como aparecen (mayúsculas, guiones, decimales).\n"
    "- precio_costo NO suele aparecer en un datasheet: déjalo \"\" salvo que el documento SÍ muestre un "
    "precio explícito.\n"
    "- Si el texto NO parece una ficha técnica de producto, devuelve productos:[] y explica en notas."
)


def normalizar_ficha(texto: str, model_id: str = "") -> dict:
    """Convierte el texto crudo del datasheet en la lista de productos estructurados.
    Devuelve {"productos": [...], "notas": ...} (con 'error' si algo falla)."""
    texto = (texto or "").strip()
    if not texto:
        return {"error": "documento vacío o ilegible", "productos": []}
    recorte = texto[:15000]
    raw = ""
    try:
        resp = llm.complete(
            model_id=model_id or FICHA_MODEL,
            system=_NORM_SYS,
            messages=[{"role": "user", "content": recorte}],
            max_tokens=3000,
            temperature=0,
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1].lstrip("json").strip() if "```" in raw[3:] else raw.strip("`")
        datos = json.loads(raw)
        datos.setdefault("productos", [])
        datos.setdefault("notas", "")
        return datos
    except json.JSONDecodeError as e:
        log.error(f"normalizar_ficha: JSON inválido: {e} | raw={raw[:200]}")
        return {"error": "no pude estructurar la ficha (JSON inválido del extractor)", "productos": []}
    except Exception as e:
        log.error(f"normalizar_ficha: {e}")
        return {"error": f"error al normalizar: {e}", "productos": []}


def leer_ficha(url: str = "", data: bytes = b"", nombre: str = "", mime: str = "",
                model_id: str = "") -> dict:
    """Punto de entrada: baja el archivo (o usa data), extrae texto y lo normaliza a productos.
    Devuelve {"productos": [...], "notas": ..., "formato": ...} o {"error": ...}."""
    try:
        if not data:
            if not url:
                return {"error": "sin archivo ni URL", "productos": []}
            data = orden_compra.descargar(url)
    except Exception as e:
        return {"error": f"no pude descargar el archivo: {e}", "productos": []}

    if not nombre and url:
        nombre = url.rsplit("/", 1)[-1]

    texto, fmt = orden_compra.extraer_texto(data, nombre, mime)
    if not texto.strip():
        return {"error": f"no pude extraer texto del archivo ({fmt})", "productos": [], "formato": fmt}

    datos = normalizar_ficha(texto, model_id=model_id)
    datos["formato"] = fmt
    for p in datos.get("productos", []):
        p.setdefault("url_origen", url)
        p.setdefault("imagen_url", "")
        p.setdefault("moneda", "")
        p.setdefault("precio_costo", "")
    return datos
