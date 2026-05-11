"""
AGENTE BUSCADOR — Brain · MRO Master Pro
=========================================
Worker que corre en Railway. Escucha jobs pendientes en Supabase,
hace búsqueda en 1CRM (productos + proveedores) y Google,
rankea Top 5 y escribe resultados de vuelta en Supabase.

Dependencias:
  pip install anthropic supabase httpx python-dotenv

Variables de entorno (.env):
  ANTHROPIC_API_KEY=
  SUPABASE_URL=
  SUPABASE_SERVICE_KEY=
  ONECRM_URL=https://mromasterpro.1crmcloud.com
  ONECRM_CLIENT_ID=
  ONECRM_CLIENT_SECRET=
  ONECRM_USERNAME=
  ONECRM_PASSWORD=
  FX_API_KEY=        # opcional — fixer.io o similar
"""

import os
import re
import time
import json
import logging
from datetime import datetime, date
from dotenv import load_dotenv

import httpx
import anthropic
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agente_buscador")

# ─────────────────────────────────────────
# CLIENTES
# ─────────────────────────────────────────
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"]
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ONECRM_BASE = os.environ["ONECRM_URL"].rstrip("/")
POLL_INTERVAL = 10  # segundos entre polls


# ─────────────────────────────────────────
# 1CRM — HTTP BASIC AUTH
# ─────────────────────────────────────────
def onecrm_get(endpoint: str, params: dict = {}) -> dict:
    user = os.environ["ONECRM_USERNAME"]
    pwd = os.environ["ONECRM_PASSWORD"]
    resp = httpx.get(
        f"{ONECRM_BASE}/api.php/{endpoint}",
        auth=(user, pwd),
        params=params,
        timeout=20,
    )
    if resp.status_code != 200:
        log.error(f"1CRM error {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────
# TIPO DE CAMBIO USD/MXN
# ─────────────────────────────────────────
def get_fx_usd_mxn() -> float:
    """Obtiene tipo de cambio USD/MXN. Usa fixer.io si hay API key, si no usa Banxico."""
    try:
        fx_key = os.environ.get("FX_API_KEY")
        if fx_key:
            resp = httpx.get(
                f"http://data.fixer.io/api/latest",
                params={"access_key": fx_key, "base": "USD", "symbols": "MXN"},
                timeout=10,
            )
            data = resp.json()
            return data["rates"]["MXN"]
        else:
            # Fallback: tipo de cambio aproximado hardcoded si no hay API
            # TODO: conectar Banxico cuando se tenga acceso
            log.warning("Sin FX_API_KEY, usando tipo de cambio aproximado 17.50")
            return 17.50
    except Exception as e:
        log.warning(f"Error obteniendo FX: {e}, usando 17.50")
        return 17.50


# ─────────────────────────────────────────
# BÚSQUEDA EN 1CRM — PRODUCTOS
# ─────────────────────────────────────────
def _variantes_modelo(modelo: str) -> list[str]:
    """
    Genera variantes del número de parte para cubrir diferencias de formato.
    Ej: '3RH2911-1HA11' → ['3RH2911-1HA11', '3RH29111HA11', '3RH2911 1HA11', '3RH2911']
    """
    variantes = [modelo]
    sin_sep   = re.sub(r'[\s\-\./]', '', modelo)          # sin separadores
    con_esp   = re.sub(r'[\-\./]',   ' ', modelo)         # con espacios
    con_guion = re.sub(r'[\s\./]',   '-', modelo)         # con guiones
    primera   = re.split(r'[\s\-\./]', modelo)[0]         # primer bloque
    for v in [sin_sep, con_esp, con_guion, primera]:
        if v and v not in variantes and len(v) >= 3:
            variantes.append(v)
    return variantes


def _coincide_modelo(modelo_buscado: str, texto_crm: str) -> bool:
    """
    Compara sin importar separadores ni mayúsculas.
    '3RH2911-1HA11' coincide con '3RH29111HA11', '3rh2911 1ha11', etc.
    """
    norm = lambda s: re.sub(r'[\s\-\./]', '', s).lower()
    m = norm(modelo_buscado)
    t = norm(texto_crm)
    return m in t or t in m


def buscar_en_crm_productos(marca: str, modelo: str) -> list[dict]:
    log.info(f"Buscando en 1CRM productos: {marca} {modelo}")
    try:
        variantes = _variantes_modelo(modelo)
        log.info(f"Variantes de búsqueda: {variantes}")

        # Estrategias: variantes del modelo + búsqueda por marca (filtrado client-side)
        busquedas = [{"filter_text": v, "limit": 20} for v in variantes]
        busquedas.append({"filter_text": marca, "limit": 50})  # todos los productos de la marca

        vistos = set()
        resultados = []

        for params in busquedas:
            try:
                data    = onecrm_get("data/Product", params)
                records = data.get("records", [])
                total   = data.get("total_count", len(records))
                log.info(f"1CRM filter_text='{params['filter_text']}': total={total} names={[r.get('name','?')[:40] for r in records[:3]]}")
            except Exception as e:
                log.warning(f"1CRM falló con {params}: {e}")
                continue

            for r in records:
                rid = r.get("id")
                if rid in vistos:
                    continue

                nombre = r.get("name") or ""
                codigo = r.get("product_code") or ""
                desc   = r.get("description") or ""

                # Para búsqueda por marca (limit=50), filtrar client-side por modelo
                if params["filter_text"].lower() == marca.lower():
                    if not (_coincide_modelo(modelo, nombre) or _coincide_modelo(modelo, codigo)):
                        continue

                vistos.add(rid)
                resultados.append({
                    "proveedor":        "1CRM Catálogo",
                    "nombre_producto":  nombre,
                    "precio_orig":      float(r.get("price") or 0) or None,
                    "moneda":           "USD",
                    "disponibilidad":   "en_stock",
                    "tiempo_entrega":   "Inmediato",
                    "condicion":        "nuevo",
                    "fuente":           "1crm_productos",
                    "url":              f"{ONECRM_BASE}/index.php?module=Products&record={rid}",
                    "dist_autorizado":  True,
                    "notas":            desc,
                })

        log.info(f"1CRM productos: {len(resultados)} resultados (variantes probadas: {len(busquedas)})")
        return resultados
    except Exception as e:
        log.error(f"Error búsqueda 1CRM productos: {e}")
        return []


# ─────────────────────────────────────────
# BÚSQUEDA EN 1CRM — PROVEEDORES
# ─────────────────────────────────────────
def buscar_en_crm_proveedores(marca: str, modelo: str) -> list[dict]:
    log.info(f"Buscando en 1CRM proveedores para: {marca}")
    try:
        data = onecrm_get("data/Account", {
            "filters[account_type]": "Supplier",
            "filters[name]": marca,
            "limit": 10,
        })
        records = data.get("records", [])
        resultados = []
        for r in records:
            resultados.append({
                "proveedor": r.get("name"),
                "nombre_producto": f"{marca} {modelo}",
                "precio_orig": None,  # proveedores no tienen precio directo
                "moneda": "USD",
                "disponibilidad": "bajo_pedido",
                "tiempo_entrega": "Consultar",
                "condicion": "nuevo",
                "fuente": "1crm_proveedores",
                "url": r.get("website") or f"{ONECRM_BASE}/index.php?module=Accounts&record={r.get('id')}",
                "dist_autorizado": False,
                "notas": f"Tel: {r.get('phone_office', '')}",
            })
        log.info(f"1CRM proveedores: {len(resultados)} resultados")
        return resultados
    except Exception as e:
        log.error(f"Error búsqueda 1CRM proveedores: {e}")
        return []


# Dominio propio del cliente — resultados de este dominio se tratan
# como productos ya publicados en el catálogo (equivalente a 1CRM)
DOMINIO_PROPIO = os.environ.get("DOMINIO_PROPIO", "mromasterpro.com")


def _es_dominio_propio(url: str) -> bool:
    return DOMINIO_PROPIO in url


# ─────────────────────────────────────────
# BÚSQUEDA EN SITIO PROPIO (site:mromasterpro.com)
# ─────────────────────────────────────────
def buscar_en_sitio_propio(marca: str, modelo: str) -> list[dict]:
    """
    Busca el producto específicamente en el sitio propio usando SerpAPI.
    Si aparece → el producto YA está publicado (equivale a estar en 1CRM).
    """
    log.info(f"Buscando en sitio propio ({DOMINIO_PROPIO}): {modelo}")
    try:
        api_key = os.environ.get("SERPAPI_KEY", "").strip()
        if not api_key:
            return []

        query = f"site:{DOMINIO_PROPIO} {modelo}"
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": api_key, "engine": "google", "num": 5},
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json().get("organic_results", [])

        resultados = []
        for item in items:
            url = item.get("link", "")
            resultados.append({
                "proveedor":       f"Catálogo {DOMINIO_PROPIO}",
                "nombre_producto": item.get("title", f"{marca} {modelo}"),
                "precio_orig":     None,
                "moneda":          "USD",
                "disponibilidad":  "en_stock",
                "tiempo_entrega":  "Inmediato",
                "condicion":       "nuevo",
                "fuente":          "1crm_productos",   # ← ya publicado en nuestro sistema
                "url":             url,
                "dist_autorizado": True,
                "notas":           item.get("snippet", ""),
            })
        if resultados:
            log.info(f"Sitio propio: {len(resultados)} resultado(s) — producto YA publicado")
        else:
            log.info(f"Sitio propio: sin resultados — producto no publicado aún")
        return resultados
    except Exception as e:
        log.error(f"Error buscando en sitio propio: {e}")
        return []


# ─────────────────────────────────────────
# BÚSQUEDA WEB (SerpAPI — Google Search)
# ─────────────────────────────────────────
def buscar_en_google(marca: str, modelo: str) -> list[dict]:
    log.info(f"Buscando en SerpAPI: {marca} {modelo}")
    try:
        api_key = os.environ.get("SERPAPI_KEY", "").strip()
        if not api_key:
            log.warning("Sin SERPAPI_KEY, saltando búsqueda web")
            return []

        query = f"{marca} {modelo} precio distribuidor México"
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": api_key, "engine": "google", "num": 5, "gl": "mx", "hl": "es"},
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json().get("organic_results", [])

        resultados = []
        for item in items:
            url = item.get("link", "")
            hostname = url.split("/")[2] if url.startswith("http") else url
            es_propio = _es_dominio_propio(url)
            resultados.append({
                "proveedor":       f"Catálogo {DOMINIO_PROPIO}" if es_propio else hostname,
                "nombre_producto": item.get("title", f"{marca} {modelo}"),
                "precio_orig":     None,
                "moneda":          "USD",
                "disponibilidad":  "en_stock" if es_propio else "consultar",
                "tiempo_entrega":  "Inmediato" if es_propio else "Ver sitio",
                "condicion":       "nuevo",
                "fuente":          "1crm_productos" if es_propio else "web",
                "url":             url,
                "dist_autorizado": es_propio,
                "notas":           item.get("snippet", ""),
            })
            if es_propio:
                log.info(f"  ★ Resultado propio detectado: {url[:80]}")
        log.info(f"SerpAPI: {len(resultados)} resultados")
        return resultados
    except Exception as e:
        log.error(f"Error búsqueda SerpAPI: {e}")
        return []


# ─────────────────────────────────────────
# BÚSQUEDA EN BRAVE
# ─────────────────────────────────────────
def buscar_en_brave(marca: str, modelo: str) -> list[dict]:
    log.info(f"Buscando en Brave: {marca} {modelo}")
    try:
        api_key = os.environ.get("BRAVE_API_KEY", "").strip()
        if not api_key:
            log.warning("Sin BRAVE_API_KEY, saltando búsqueda Brave")
            return []

        query = f"{marca} {modelo} precio distribuidor México"
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            params={"q": query, "count": 5, "country": "mx", "search_lang": "es"},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("web", {}).get("results", [])

        resultados = []
        for item in items:
            resultados.append({
                "proveedor": item.get("meta_url", {}).get("hostname", ""),
                "nombre_producto": item.get("title", f"{marca} {modelo}"),
                "precio_orig": None,
                "moneda": "USD",
                "disponibilidad": "consultar",
                "tiempo_entrega": "Ver sitio",
                "condicion": "nuevo",
                "fuente": "brave",
                "url": item.get("url", ""),
                "dist_autorizado": False,
                "notas": item.get("description", ""),
            })
        log.info(f"Brave: {len(resultados)} resultados")
        return resultados
    except Exception as e:
        log.error(f"Error búsqueda Brave: {e}")
        return []


# ─────────────────────────────────────────
# CLAUDE — ANALIZA Y RANKEA TOP 5
# ─────────────────────────────────────────
def rankear_con_claude(
    marca: str,
    modelo: str,
    urgente: bool,
    resultados_raw: list[dict],
    fx: float,
) -> list[dict]:
    log.info(f"Claude rankeando {len(resultados_raw)} resultados (urgente={urgente})")

    ponderacion = "30% precio / 70% disponibilidad" if urgente else "60% precio / 40% disponibilidad"

    disponibilidad_puntos = {
        "en_stock": 100,
        "inmediato": 100,
        "dias_1_5": 75,
        "bajo_pedido": 25,
        "importacion": 10,
        "consultar": 30,
        "ver_sitio": 20,
    }

    # Separar productos del catálogo 1CRM para destacarlos en el prompt
    crm_productos = [r for r in resultados_raw if r.get("fuente") == "1crm_productos"]
    otros = [r for r in resultados_raw if r.get("fuente") != "1crm_productos"]

    crm_seccion = ""
    if crm_productos:
        crm_seccion = f"""
⚠️ CATÁLOGO INTERNO 1CRM — PRIORIDAD MÁXIMA:
{json.dumps(crm_productos, ensure_ascii=False, indent=2)}

REGLA OBLIGATORIA: Los resultados anteriores son del catálogo propio del cliente.
DEBES incluir AL MENOS UNO en el Top 5, en el rank 1, con score_confianza=5.
Aunque no tengan precio, su presencia en el catálogo interno es la señal más fuerte.

"""

    prompt = f"""Eres un agente especializado en búsqueda de productos industriales para MRO Master Pro.

Tienes estos resultados de búsqueda para: **{marca} {modelo}**
Modo: {"URGENTE" if urgente else "Normal"}
Ponderación: {ponderacion}
Tipo de cambio USD/MXN: {fx}

{crm_seccion}OTROS RESULTADOS:
{json.dumps(otros, ensure_ascii=False, indent=2)}

Tu tarea:
1. {f"OBLIGATORIO: incluye primero los {len(crm_productos)} resultado(s) del catálogo 1CRM (fuente=1crm_productos) en rank 1." if crm_productos else "Selecciona los mejores 5 resultados."}
2. Completa el Top 5 con los mejores resultados restantes
3. Para cada uno infiere o estima el precio si no está explícito (basado en snippet/notas)
4. Verifica si es distribuidor autorizado de {marca} (indicios en nombre o URL)
5. Calcula el score de ranking:
   - Normaliza precios: el más barato = 100 puntos, los demás proporcional
   - Disponibilidad: en_stock=100, 1-5días=75, 1-2semanas=50, bajo_pedido=25, importación=10
   - Score final = (precio_pts * {0.3 if urgente else 0.6}) + (disponibilidad_pts * {0.7 if urgente else 0.4})
6. Score de confianza (1-5):
   - 5: producto en catálogo 1CRM (fuente=1crm_productos)
   - 4: proveedor en 1CRM con historial
   - 3: resultado Google con precio y datos claros
   - 2: datos incompletos o precio estimado
   - 1: fuente no verificada

Responde SOLO con un JSON array con máximo 5 objetos, ordenados de mayor a menor score_ranking:
[
  {{
    "rank": 1,
    "proveedor": "nombre",
    "dist_autorizado": true/false,
    "precio_orig": 0.00,
    "moneda": "USD",
    "precio_mxn": 0.00,
    "disponibilidad": "en_stock|dias_N|bajo_pedido|importacion",
    "tiempo_entrega": "texto",
    "condicion": "nuevo|reacondicionado|usado",
    "fuente": "1crm_productos|1crm_proveedores|web",
    "url": "https://...",
    "score_confianza": 1-5,
    "score_ranking": 0.00,
    "notas": "observaciones"
  }}
]

No incluyas ningún texto fuera del JSON."""

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Limpiar posibles backticks
    text = text.replace("```json", "").replace("```", "").strip()

    try:
        ranked = json.loads(text)
        log.info(f"Claude rankeo: {len(ranked)} opciones en Top 5")
        return ranked
    except json.JSONDecodeError as e:
        log.error(f"Error parseando JSON de Claude: {e}\nRespuesta: {text}")
        return []


# ─────────────────────────────────────────
# GUARDAR RESULTADOS EN SUPABASE
# ─────────────────────────────────────────
def guardar_opciones(rfq_uuid: str, opciones: list[dict], fx: float) -> None:
    log.info(f"Guardando {len(opciones)} opciones en Supabase")

    # Borrar opciones previas por si es reintento
    supabase.table("opciones").delete().eq("rfq_id", rfq_uuid).execute()

    for op in opciones:
        precio_orig = op.get("precio_orig") or 0
        moneda = op.get("moneda", "USD")
        precio_mxn = (precio_orig * fx) if moneda == "USD" else precio_orig

        supabase.table("opciones").insert({
            "rfq_id": rfq_uuid,
            "rank": op.get("rank"),
            "proveedor": op.get("proveedor"),
            "dist_autorizado": op.get("dist_autorizado", False),
            "precio_orig": precio_orig,
            "moneda": moneda,
            "precio_mxn": round(precio_mxn, 2),
            "disponibilidad": op.get("disponibilidad"),
            "tiempo_entrega": op.get("tiempo_entrega"),
            "condicion": op.get("condicion", "nuevo"),
            "fuente": op.get("fuente"),
            "url": op.get("url"),
            "score_confianza": op.get("score_confianza"),
            "score_ranking": op.get("score_ranking"),
            "notas": op.get("notas"),
        }).execute()

    log.info("Opciones guardadas OK")


def agregar_log_job(job_id: str, paso: str, msg: str) -> None:
    try:
        job = supabase.table("jobs").select("log").eq("id", job_id).single().execute()
        logs = job.data.get("log") or []
        logs.append({
            "paso": paso,
            "timestamp": datetime.utcnow().isoformat(),
            "msg": msg,
        })
        supabase.table("jobs").update({"log": logs}).eq("id", job_id).execute()
    except Exception as e:
        log.warning(f"Error actualizando log del job: {e}")


# ─────────────────────────────────────────
# PROCESADOR PRINCIPAL DEL JOB
# ─────────────────────────────────────────
def procesar_job(job: dict) -> None:
    job_id = job["id"]
    rfq_uuid = job["rfq_id"]

    log.info(f"Procesando job {job_id} para rfq {rfq_uuid}")

    # Marcar job como corriendo
    supabase.table("jobs").update({
        "estado": "corriendo",
        "started_at": datetime.utcnow().isoformat(),
    }).eq("id", job_id).execute()

    # Marcar RFQ como buscando
    supabase.table("rfqs").update({"estado": "buscando"}).eq("id", rfq_uuid).execute()

    try:
        # Obtener datos del RFQ
        rfq_resp = supabase.table("rfqs").select("*").eq("id", rfq_uuid).single().execute()
        rfq = rfq_resp.data
        marca = rfq["marca"].strip().title()
        modelo = rfq["modelo"].strip()
        urgente = rfq.get("urgente", False)

        agregar_log_job(job_id, "inicio", f"Buscando: {marca} {modelo} | urgente={urgente}")

        # Obtener tipo de cambio
        fx = get_fx_usd_mxn()
        supabase.table("rfqs").update({
            "fx_usd_mxn": fx,
            "fx_fecha": date.today().isoformat(),
        }).eq("id", rfq_uuid).execute()

        agregar_log_job(job_id, "fx", f"Tipo de cambio USD/MXN: {fx}")

        # 3 búsquedas
        resultados = []

        # Primero buscar en sitio propio (detecta si ya está publicado)
        agregar_log_job(job_id, "busqueda_sitio_propio", f"Buscando en {DOMINIO_PROPIO}")
        res_sitio = buscar_en_sitio_propio(marca, modelo)
        resultados.extend(res_sitio)
        agregar_log_job(job_id, "busqueda_sitio_propio", f"{len(res_sitio)} resultados")

        agregar_log_job(job_id, "busqueda_1crm_productos", "Iniciando búsqueda en 1CRM productos")
        res_productos = buscar_en_crm_productos(marca, modelo)
        resultados.extend(res_productos)
        agregar_log_job(job_id, "busqueda_1crm_productos", f"{len(res_productos)} resultados")

        agregar_log_job(job_id, "busqueda_1crm_proveedores", "Iniciando búsqueda en 1CRM proveedores")
        res_proveedores = buscar_en_crm_proveedores(marca, modelo)
        resultados.extend(res_proveedores)
        agregar_log_job(job_id, "busqueda_1crm_proveedores", f"{len(res_proveedores)} resultados")

        agregar_log_job(job_id, "busqueda_google", "Iniciando búsqueda en Google")
        res_google = buscar_en_google(marca, modelo)
        resultados.extend(res_google)
        agregar_log_job(job_id, "busqueda_google", f"{len(res_google)} resultados")

        agregar_log_job(job_id, "busqueda_brave", "Iniciando búsqueda en Brave")
        res_brave = buscar_en_brave(marca, modelo)
        resultados.extend(res_brave)
        agregar_log_job(job_id, "busqueda_brave", f"{len(res_brave)} resultados")

        if not resultados:
            raise Exception("No se encontraron resultados en ninguna fuente")

        # Claude rankea
        agregar_log_job(job_id, "ranking", f"Claude rankeando {len(resultados)} resultados")
        top5 = rankear_con_claude(marca, modelo, urgente, resultados, fx)

        if not top5:
            raise Exception("Claude no pudo generar el ranking")

        # ── Garantizar que 1CRM catálogo siempre aparezca en el Top 5 ──
        # Si Claude no incluyó ningún resultado del catálogo, los inyectamos
        tiene_crm_producto = any(r.get("fuente") == "1crm_productos" for r in top5)
        if not tiene_crm_producto and res_productos:
            log.info(f"Claude omitió {len(res_productos)} producto(s) 1CRM — inyectando al Top 5")
            insertar = []
            for prod in res_productos[:2]:  # máximo 2 del catálogo
                precio = float(prod.get("precio_orig") or 0) or None
                insertar.append({
                    "rank": 1,
                    "proveedor": prod["proveedor"],
                    "dist_autorizado": True,
                    "precio_orig": precio,
                    "moneda": prod.get("moneda", "USD"),
                    "precio_mxn": round((precio or 0) * fx, 2),
                    "disponibilidad": prod.get("disponibilidad", "en_stock"),
                    "tiempo_entrega": prod.get("tiempo_entrega", "Inmediato"),
                    "condicion": prod.get("condicion", "nuevo"),
                    "fuente": "1crm_productos",
                    "url": prod.get("url", ""),
                    "score_confianza": 5,
                    "score_ranking": 95.0,
                    "notas": prod.get("notas", ""),
                })
            # Renumerar: insertar al inicio, desplazar los últimos
            for i, item in enumerate(insertar):
                item["rank"] = i + 1
            restantes = top5[:5 - len(insertar)]
            for i, item in enumerate(restantes):
                item["rank"] = len(insertar) + i + 1
            top5 = insertar + restantes
            log.info(f"Top 5 actualizado con productos 1CRM: {len(top5)} opciones")
        else:
            log.info(f"1CRM catálogo presente en Top 5: {tiene_crm_producto}")

        # Guardar en Supabase
        guardar_opciones(rfq_uuid, top5, fx)

        # Actualizar RFQ a busqueda_completa
        supabase.table("rfqs").update({
            "estado": "busqueda_completa",
        }).eq("id", rfq_uuid).execute()

        # Crear job para notificación al gerente
        supabase.table("jobs").insert({
            "rfq_id": rfq_uuid,
            "agente": "notificador",
            "estado": "pendiente",
        }).execute()

        # Cerrar job exitosamente
        supabase.table("jobs").update({
            "estado": "completado",
            "finished_at": datetime.utcnow().isoformat(),
            "output": {"opciones_encontradas": len(top5)},
        }).eq("id", job_id).execute()

        log.info(f"Job {job_id} completado — Top {len(top5)} generado")

    except Exception as e:
        log.error(f"Job {job_id} falló: {e}")
        agregar_log_job(job_id, "error", str(e))

        # Reintentar hasta 3 veces
        intento = job.get("intento", 1)
        if intento < 3:
            supabase.table("jobs").update({
                "estado": "pendiente",
                "intento": intento + 1,
            }).eq("id", job_id).execute()
            supabase.table("rfqs").update({"estado": "recibido"}).eq("id", rfq_uuid).execute()
            log.info(f"Job reintentará (intento {intento + 1}/3)")
        else:
            supabase.table("jobs").update({
                "estado": "fallido",
                "finished_at": datetime.utcnow().isoformat(),
                "error": str(e),
            }).eq("id", job_id).execute()
            supabase.table("rfqs").update({"estado": "recibido"}).eq("id", rfq_uuid).execute()
            log.error(f"Job {job_id} falló definitivamente después de 3 intentos")


# ─────────────────────────────────────────
# AGENTE NOTIFICADOR
# ─────────────────────────────────────────
def procesar_job_notificador(job: dict):
    job_id = job["id"]
    rfq_uuid = job["rfq_id"]
    log.info(f"Notificando job {job_id} para rfq {rfq_uuid}")

    try:
        supabase.table("jobs").update({
            "estado": "en_proceso",
            "started_at": datetime.utcnow().isoformat(),
        }).eq("id", job_id).execute()

        # Obtener datos del RFQ
        rfq_resp = supabase.table("rfqs").select("*").eq("id", rfq_uuid).single().execute()
        rfq = rfq_resp.data
        marca = rfq.get("marca", "")
        modelo = rfq.get("modelo", "")

        # Contar opciones guardadas
        opts_resp = supabase.table("opciones").select("id").eq("rfq_id", rfq_uuid).execute()
        n_opciones = len(opts_resp.data)

        # Escribir notificación
        supabase.table("notificaciones").insert({
            "tipo": "rfq_listo",
            "titulo": f"RFQ listo — {marca} {modelo}",
            "mensaje": f"Se encontraron {n_opciones} opciones. Revisa el RFQ para aprobar.",
            "rfq_id": rfq_uuid,
            "leida": False,
        }).execute()

        log.info(f"Notificación creada para rfq {rfq_uuid}")

        supabase.table("jobs").update({
            "estado": "completado",
            "finished_at": datetime.utcnow().isoformat(),
            "output": {"notificacion": "enviada"},
        }).eq("id", job_id).execute()

    except Exception as e:
        log.error(f"Job notificador {job_id} falló: {e}")
        supabase.table("jobs").update({
            "estado": "fallido",
            "finished_at": datetime.utcnow().isoformat(),
            "error": str(e),
        }).eq("id", job_id).execute()


# LOOP PRINCIPAL — POLLING
# ─────────────────────────────────────────
def main():
    log.info("Agente Buscador iniciado — escuchando jobs...")
    log.info(f"1CRM: {ONECRM_BASE}")
    log.info(f"Supabase: {os.environ['SUPABASE_URL']}")
    log.info(f"Poll interval: {POLL_INTERVAL}s")

    # Diagnóstico de variables de entorno opcionales
    serpapi_key = os.environ.get("SERPAPI_KEY", "")
    if serpapi_key:
        log.info("SerpAPI: OK")
    else:
        log.warning("SERPAPI_KEY no configurada — búsqueda web desactivada")

    brave_key = os.environ.get("BRAVE_API_KEY", "")
    if brave_key:
        log.info("Brave Search: OK")
    else:
        log.warning("BRAVE_API_KEY no configurada — búsqueda Brave desactivada")

    fx_key = os.environ.get("FX_API_KEY", "")
    if not fx_key:
        log.warning("FX_API_KEY no configurada — se usará tipo de cambio aproximado 17.50")

    while True:
        try:
            resp = supabase.table("jobs")\
                .select("*")\
                .in_("agente", ["buscador", "notificador"])\
                .eq("estado", "pendiente")\
                .order("created_at")\
                .limit(1)\
                .execute()

            jobs = resp.data
            if jobs:
                job = jobs[0]
                if job["agente"] == "buscador":
                    procesar_job(job)
                elif job["agente"] == "notificador":
                    procesar_job_notificador(job)
            else:
                log.debug("Sin jobs pendientes, esperando...")

        except Exception as e:
            log.error(f"Error en loop principal: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
