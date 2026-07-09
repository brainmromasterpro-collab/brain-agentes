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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

import httpx
import anthropic
from supabase import create_client, Client
from config_agentes import get_config

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
    """Obtiene tipo de cambio USD/MXN.
    Fuentes en orden de prioridad:
    1. frankfurter.app (BCE, gratuito, sin API key)
    2. Fixer.io (si hay FX_API_KEY configurada)
    3. Hardcoded 17.50 como último recurso
    """
    # 1. frankfurter.app — gratuito, sin API key, datos del Banco Central Europeo
    try:
        resp = httpx.get(
            "https://api.frankfurter.dev/v1/latest",
            params={"from": "USD", "to": "MXN"},
            timeout=10,
            follow_redirects=True,
        )
        resp.raise_for_status()
        rate = resp.json()["rates"]["MXN"]
        log.info(f"FX USD/MXN via frankfurter.dev: {rate}")
        return float(rate)
    except Exception as e:
        log.warning(f"frankfurter.dev falló: {e} — intentando siguiente fuente")

    # 2. Fixer.io (requiere API key, base EUR en plan gratis)
    try:
        fx_key = os.environ.get("FX_API_KEY")
        if fx_key:
            resp = httpx.get(
                "http://data.fixer.io/api/latest",
                params={"access_key": fx_key, "base": "EUR", "symbols": "USD,MXN"},
                timeout=10,
            )
            data = resp.json()
            usd = data["rates"]["USD"]
            mxn = data["rates"]["MXN"]
            rate = mxn / usd  # convertir EUR base a USD base
            log.info(f"FX USD/MXN via fixer.io: {rate}")
            return float(rate)
    except Exception as e:
        log.warning(f"Fixer.io falló: {e}")

    # 3. Hardcoded como último recurso
    log.warning("Todas las fuentes FX fallaron — usando tipo de cambio aproximado 17.50")
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
    Solo acepta si el modelo buscado aparece DENTRO del texto CRM (no al revés),
    y requiere mínimo 5 caracteres normalizados para evitar falsos positivos.
    """
    norm = lambda s: re.sub(r'[\s\-\./]', '', s).lower()
    m = norm(modelo_buscado)
    t = norm(texto_crm)
    # Requerir mínimo 5 chars y solo dirección m→t (modelo en texto CRM)
    if len(m) < 5:
        return False
    return m in t


def buscar_en_crm_productos(marca: str, modelo: str) -> list[dict]:
    log.info(f"Buscando en 1CRM productos: {marca} {modelo}")
    try:
        variantes = _variantes_modelo(modelo)
        log.info(f"Variantes de búsqueda: {variantes}")

        # Estrategias: variantes del modelo + búsqueda por marca (filtrado client-side)
        # NO pasar "fields" — la API de 1CRM devuelve cero resultados cuando se especifica
        busquedas = [{"filter_text": v, "limit": 20} for v in variantes]
        if marca:
            busquedas.append({"filter_text": marca, "limit": 50})

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

                if not (_coincide_modelo(modelo, nombre) or _coincide_modelo(modelo, codigo)):
                    log.debug(f"Descartado por _coincide_modelo: '{nombre}' / '{codigo}' vs '{modelo}'")
                    continue

                vistos.add(rid)

                # Obtener detalle completo para precio e imagen
                precio_raw = 0
                img_url    = None
                try:
                    det = onecrm_get(f"data/Product/{rid}")
                    rec = det.get("record", {})
                    precio_raw = rec.get("list_price") or rec.get("price") or rec.get("unit_price") or 0
                    # image_url = URL de Supabase (fallback) o vacío
                    img_url = rec.get("image_url") or None
                    # image_filename = subido vía Playwright → servido por entryPoint
                    if not img_url and rec.get("image_filename"):
                        img_url = f"{ONECRM_BASE}/index.php?entryPoint=download&id={rid}&type=AOS_Products_Quotes&field=picture"
                    # picture como último recurso si es una URL completa
                    if not img_url:
                        pic = rec.get("picture") or ""
                        if pic.startswith("http"):
                            img_url = pic
                    log.info(f"1CRM detalle {rid[:8]}: precio={precio_raw} img={'sí' if img_url else 'no'}")
                except Exception as e:
                    log.warning(f"No se pudo obtener detalle de 1CRM {rid}: {e}")

                resultados.append({
                    "proveedor":       "1CRM Catálogo",
                    "nombre_producto": nombre,
                    "precio_orig":     float(precio_raw) if float(precio_raw) > 0 else None,
                    "moneda":          "USD",
                    "disponibilidad":  "en_stock",
                    "tiempo_entrega":  "Inmediato",
                    "condicion":       "nuevo",
                    "fuente":          "1crm_productos",
                    "url":             f"{ONECRM_BASE}/index.php?module=ProductCatalog&action=DetailView&record={rid}",
                    "dist_autorizado": True,
                    "notas":           nombre,
                    "imagen_url":      img_url,
                })

        log.info(f"1CRM productos: {len(resultados)} resultados (variantes probadas: {len(busquedas)})")
        return resultados
    except Exception as e:
        log.error(f"Error búsqueda 1CRM productos: {e}")
        return []


# ─────────────────────────────────────────
# BÚSQUEDA EN 1CRM — PROVEEDORES
# ─────────────────────────────────────────
# Proveedores que NUNCA deben aparecer como opción:
# - la cuenta del propio sistema 1CRM (viene por defecto, no es un proveedor real)
# - cuentas sin catálogo / genéricas que el usuario pidió excluir
PROVEEDORES_EXCLUIDOS = [
    "1crm systems",   # 1CRM Systems Corp — cuenta del propio CRM
    "2d2",            # 2D2, S.A. de C.V. — sin catálogo
    "2 d2",
]


def buscar_en_crm_proveedores(marca: str, modelo: str) -> list[dict]:
    log.info(f"Buscando en 1CRM proveedores para: {marca}")
    try:
        # Sin marca real no se pueden relacionar proveedores → no devolver genéricos
        palabras_marca = [p.lower() for p in marca.split() if len(p) >= 3]
        if not palabras_marca:
            log.info("1CRM proveedores: sin marca, se omite (evita devolver proveedores no relacionados)")
            return []

        # Usar filter_text (no filters[name] — ese parámetro puede ser ignorado por la API)
        data = onecrm_get("data/Account", {
            "filters[account_type]": "Supplier",
            "filter_text": marca,
            "limit": 20,
        })
        records = data.get("records", [])

        resultados = []
        for r in records:
            nombre_proveedor = (r.get("name") or "").lower()

            # Excluir cuentas del sistema / sin catálogo
            if any(excl in nombre_proveedor for excl in PROVEEDORES_EXCLUIDOS):
                log.info(f"Proveedor excluido (lista negra): {r.get('name')}")
                continue

            # Descartar si ninguna palabra de la marca aparece en el nombre del proveedor
            if not any(p in nombre_proveedor for p in palabras_marca):
                log.debug(f"Proveedor descartado (sin relación con '{marca}'): {r.get('name')}")
                continue

            resultados.append({
                "proveedor": r.get("name"),
                "nombre_producto": f"{marca} {modelo}",
                "precio_orig": None,
                "moneda": "USD",
                "disponibilidad": "bajo_pedido",
                "tiempo_entrega": "Consultar",
                "condicion": "nuevo",
                "fuente": "1crm_proveedores",
                "url": r.get("website") or f"{ONECRM_BASE}/index.php?module=Accounts&record={r.get('id')}",
                "dist_autorizado": False,
                "notas": f"Tel: {r.get('phone_office', '')}",
            })

        log.info(f"1CRM proveedores: {len(resultados)} válidos de {len(records)} registros")
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
        data = resp.json()
        if "error" in data:
            log.error(f"SerpAPI sitio_propio error: {data['error']}")
            return []
        items = data.get("organic_results", [])

        resultados = []
        for item in items:
            url = item.get("link", "")
            resultados.append({
                "proveedor":       f"Catálogo {DOMINIO_PROPIO}",
                "nombre_producto": item.get("title", f"{marca} {modelo}"),
                "precio_orig":     None,
                "moneda":          "USD",
                "disponibilidad":  "consultar",        # ← aparece en web pero NO confirma stock ni 1CRM
                "tiempo_entrega":  "Verificar en sitio",
                "condicion":       "nuevo",
                "fuente":          "sitio_propio",
                "url":             url,
                "dist_autorizado": False,              # ← no confirmado hasta verificar en 1CRM
                "notas":           f"[VERIFICAR en 1CRM] {item.get('snippet', '')}",
            })
        if resultados:
            log.info(f"Sitio propio: {len(resultados)} resultado(s) en website (verificar si está en 1CRM)")
        else:
            log.info(f"Sitio propio: sin resultados en website")
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

        # Buscar sin filtro de región — los catálogos industriales son en inglés/global
        # "precio distribuidor México" + gl=mx devuelve sitios genéricos sin catálogo
        query = f"{marca} {modelo}".strip() if marca else modelo
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": api_key, "engine": "google", "num": 10},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            log.error(f"SerpAPI google_web error: {data['error']}")
            return []
        items = data.get("organic_results", [])

        resultados = []
        for item in items:
            url = item.get("link", "")
            hostname = url.split("/")[2] if url.startswith("http") else url
            es_propio = _es_dominio_propio(url)
            snippet = item.get("snippet", "")

            # Intentar extraer precio del snippet (a veces muestra "$227.68")
            precio_snippet, moneda_snippet = _extract_price_from_text(snippet)

            thumbnail = (
                item.get("thumbnail")
                or (item.get("pagemap", {}).get("cse_image") or [{}])[0].get("src")
                or (item.get("pagemap", {}).get("cse_thumbnail") or [{}])[0].get("src")
                or None
            )
            resultados.append({
                "proveedor":       f"Catálogo {DOMINIO_PROPIO}" if es_propio else hostname,
                "nombre_producto": item.get("title", f"{marca} {modelo}"),
                "precio_orig":     precio_snippet,
                "moneda":          moneda_snippet,
                "disponibilidad":  "en_stock" if es_propio else "consultar",
                "tiempo_entrega":  "Inmediato" if es_propio else "Ver sitio",
                "condicion":       "nuevo",
                "fuente":          "sitio_propio" if es_propio else "web",
                "url":             url,
                "dist_autorizado": es_propio,
                "notas":           snippet,
                "imagen_url":      thumbnail,
            })
            if es_propio:
                log.info(f"  ★ Resultado propio detectado: {url[:80]}")
            if precio_snippet:
                log.info(f"  $ Precio en snippet [{hostname}]: {precio_snippet} {moneda_snippet}")
        log.info(f"SerpAPI: {len(resultados)} resultados")
        return resultados
    except Exception as e:
        log.error(f"Error búsqueda SerpAPI: {e}")
        return []


# ─────────────────────────────────────────
# BÚSQUEDA EN GOOGLE SHOPPING (precios reales)
# ─────────────────────────────────────────
def buscar_en_google_shopping(marca: str, modelo: str) -> list[dict]:
    """
    Usa SerpAPI Google Shopping para obtener precios reales de vendedores.
    A diferencia de la búsqueda web orgánica, Shopping devuelve
    extracted_price (precio estructurado, no estimado).
    """
    log.info(f"Buscando en Google Shopping: {marca} {modelo}")
    try:
        api_key = os.environ.get("SERPAPI_KEY", "").strip()
        if not api_key:
            return []

        # Sin restricción de región — distribuidores industriales son globales (Cadeco, Radwell, RS, etc.)
        # gl=mx limita resultados a vendedores mexicanos que raramente tienen catálogo en Shopping
        query = f"{marca} {modelo}".strip() if marca else modelo
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={
                "engine":  "google_shopping",
                "q":       query,
                "api_key": api_key,
                "num":     10,
            },
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            log.error(f"SerpAPI google_shopping error: {data['error']}")
            return []
        items = data.get("shopping_results", [])

        resultados = []
        for item in items:
            precio_raw = item.get("extracted_price")
            if precio_raw is None:
                # Intentar parsear el string de precio como fallback
                price_str = item.get("price", "")
                try:
                    precio_raw = float(
                        price_str.replace("$", "").replace(",", "").replace("MXN", "").strip()
                    )
                except (ValueError, AttributeError):
                    precio_raw = None

            # Detectar moneda: MX$ o MXN → MXN, resto → USD
            currency_raw = item.get("currency", "")
            price_str_raw = item.get("price", "")
            if "MXN" in currency_raw or "MX$" in price_str_raw or "MXN" in price_str_raw:
                moneda = "MXN"
            else:
                moneda = "USD"

            proveedor = (
                item.get("source")
                or item.get("merchant", {}).get("name", "")
                or (item.get("link", "").split("/")[2] if item.get("link", "").startswith("http") else "")
            )

            resultados.append({
                "proveedor":       proveedor,
                "nombre_producto": item.get("title", f"{marca} {modelo}"),
                "precio_orig":     float(precio_raw) if precio_raw and float(precio_raw) > 0 else None,
                "moneda":          moneda,
                "disponibilidad":  "consultar",
                "tiempo_entrega":  "Ver sitio",
                "condicion":       "reacondicionado" if item.get("second_hand_condition") else "nuevo",
                "fuente":          "google_shopping",
                "url":             item.get("link", ""),
                "dist_autorizado": False,
                "notas":           item.get("snippet", ""),
            })

        con_precio = sum(1 for r in resultados if r["precio_orig"] is not None)
        log.info(f"Google Shopping: {len(resultados)} resultados, {con_precio} con precio real")
        return resultados

    except Exception as e:
        log.error(f"Error Google Shopping: {e}")
        return []


# ─────────────────────────────────────────
# EXTRACCIÓN DE PRECIOS — SNIPPETS Y PÁGINAS
# ─────────────────────────────────────────
def _extract_price_from_text(text: str) -> tuple:
    """
    Extrae precio de un fragmento de texto usando regex.
    Retorna (precio_float, moneda_str) o (None, 'USD').
    """
    if not text:
        return None, "USD"

    is_mxn = bool(re.search(r'MXN|MX\$|pesos', text, re.IGNORECASE))

    # Patrones en orden de confiabilidad (más específico primero)
    patterns = [
        (r'MX\$\s*(\d{1,6}(?:,\d{3})*(?:\.\d{2})?)',   'MXN'),
        (r'MXN\s*(\d{1,6}(?:,\d{3})*(?:\.\d{2})?)',     'MXN'),
        (r'USD\s*(\d{1,6}(?:,\d{3})*(?:\.\d{2})?)',     'USD'),
        (r'\$\s*(\d{1,6}(?:,\d{3})*\.\d{2})',           'USD'),  # $227.68 (con centavos)
        (r'\$\s*(\d{3,6}(?:,\d{3})*)',                  'USD'),  # $1,234 (sin centavos, mín 3 dígitos)
    ]

    for pattern, default_currency in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                price = float(match.group(1).replace(',', ''))
                if 0.01 < price < 1_000_000:
                    currency = 'MXN' if (is_mxn or default_currency == 'MXN') else 'USD'
                    return price, currency
            except ValueError:
                continue

    return None, "USD"


def _extraer_precio_de_url(url: str) -> tuple:
    """
    Visita la URL del resultado y extrae el precio estructurado (JSON-LD / meta tags).
    NO hace scraping frágil de texto — solo usa datos estructurados.
    Timeout 8s para no bloquear el pipeline.
    Retorna (precio_float, moneda_str) o (None, 'USD').
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = httpx.get(url, headers=headers, timeout=8, follow_redirects=True)
        if resp.status_code != 200:
            return None, "USD"

        html = resp.text[:150000]  # Primeros 150 KB (suficiente para head + LD)

        # 1. JSON-LD structured data — más confiable (Radwell, RS Components, Arrow, etc.)
        ld_blocks = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE
        )
        for block in ld_blocks:
            try:
                data = json.loads(block.strip())
                if isinstance(data, list):
                    data = data[0]
                offers = data.get("offers") or data.get("Offers") or {}
                if isinstance(offers, list):
                    offers = offers[0]
                price_val = offers.get("price") or offers.get("lowPrice")
                if price_val:
                    price = float(str(price_val).replace(",", "").strip())
                    if 0.01 < price < 1_000_000:
                        currency = (offers.get("priceCurrency") or "USD").upper()
                        return price, currency
            except Exception:
                pass

        # 2. Open Graph / meta tags (Shopify, WooCommerce, Parmex, etc.)
        meta_price = re.search(
            r'<meta[^>]+(?:property|name)=["\'](?:product:price:amount|og:price:amount)["\'][^>]+content=["\']([0-9.,]+)["\']',
            html, re.IGNORECASE
        )
        if meta_price:
            try:
                price = float(meta_price.group(1).replace(",", ""))
                if 0.01 < price < 1_000_000:
                    meta_cur = re.search(
                        r'<meta[^>]+(?:property|name)=["\'](?:product:price:currency|og:price:currency)["\'][^>]+content=["\']([A-Z]{3})["\']',
                        html, re.IGNORECASE
                    )
                    currency = meta_cur.group(1).upper() if meta_cur else "USD"
                    return price, currency
            except Exception:
                pass

        # 3. data-price / itemprop="price" (schema.org microdata)
        itemprop_price = re.search(
            r'itemprop=["\']price["\'][^>]*content=["\']([0-9.,]+)["\']',
            html, re.IGNORECASE
        )
        if itemprop_price:
            try:
                price = float(itemprop_price.group(1).replace(",", ""))
                if 0.01 < price < 1_000_000:
                    return price, "USD"
            except Exception:
                pass

        return None, "USD"
    except Exception as e:
        log.debug(f"_extraer_precio_de_url({url[:60]}): {e}")
        return None, "USD"



def enriquecer_precios_web(resultados: list, max_urls: int = 4) -> list:
    """
    Para resultados web sin precio, visita las páginas en paralelo y extrae
    el precio estructurado (JSON-LD / meta tags).
    Solo procesa resultados de fuente 'web' sin precio_orig.
    Agrega máx 12s al pipeline total (4 URLs en paralelo con timeout 8s cada una).
    """
    sin_precio = [
        r for r in resultados
        if r.get("fuente") == "web" and r.get("precio_orig") is None and r.get("url")
    ]
    if not sin_precio:
        return resultados

    a_enriquecer = sin_precio[:max_urls]
    log.info(f"Enriqueciendo precios de {len(a_enriquecer)} URL(s) en paralelo...")

    def _fetch(r):
        precio, moneda = _extraer_precio_de_url(r["url"])
        return r, precio, moneda

    with ThreadPoolExecutor(max_workers=len(a_enriquecer)) as executor:
        futures = {executor.submit(_fetch, r): r for r in a_enriquecer}
        for future in as_completed(futures, timeout=13):
            try:
                r, precio, moneda = future.result()
                if precio:
                    r["precio_orig"] = precio
                    r["moneda"] = moneda
                    log.info(f"  ✓ Precio extraído [{r['proveedor']}]: {precio} {moneda}")
                else:
                    log.debug(f"  - Sin precio estructurado: {r['url'][:70]}")
            except Exception as e:
                log.debug(f"  Error enriqueciendo URL: {e}")

    return resultados


# ─────────────────────────────────────────
# FILTRO DE CALIDAD — RESULTADOS WEB
# ─────────────────────────────────────────
_SNIPPETS_BASURA = [
    "página", "pagina", "page", "resultados de búsqueda",
    "search results", "inicio", "home", "categoría", "categoria",
    "directorio", "listado de", "todos los productos",
]

_DOMINIOS_BASURA = [
    "facebook.com", "instagram.com", "twitter.com", "youtube.com",
    "linkedin.com", "pinterest.com", "reddit.com", "wikipedia.org",
    "slideshare.net", "scribd.com",
]


def _filtrar_resultados_web(resultados: list[dict], modelo: str) -> list[dict]:
    """
    Descarta resultados web orgánicos de baja calidad antes de rankear.
    Nunca descarta fuentes confiables (1crm_productos, 1crm_proveedores, google_shopping).
    """
    norm_modelo = re.sub(r'[\s\-\./]', '', modelo).lower()
    limpios = []

    for r in resultados:
        fuente = r.get("fuente", "")

        # Fuentes confiables: pasan siempre sin filtro
        if fuente in ("1crm_productos", "1crm_proveedores", "google_shopping"):
            limpios.append(r)
            continue

        url     = (r.get("url") or "").lower()
        snippet = (r.get("notas") or r.get("nombre_producto") or "").lower()
        nombre  = (r.get("nombre_producto") or "").lower()

        # Descartar dominios sociales / sin catálogo
        if any(dom in url for dom in _DOMINIOS_BASURA):
            log.debug(f"Descartado (dominio basura): {url[:80]}")
            continue

        # Descartar páginas de paginación / categoría genérica
        if any(palabra in snippet for palabra in _SNIPPETS_BASURA):
            log.debug(f"Descartado (snippet basura): {snippet[:80]}")
            continue

        # Para fuentes web y sitio_propio: exigir que el número de parte
        # aparezca en el título o snippet (mínimo 5 chars normalizados)
        if fuente in ("web", "sitio_propio") and len(norm_modelo) >= 5:
            norm_nombre  = re.sub(r'[\s\-\./]', '', nombre).lower()
            norm_snippet = re.sub(r'[\s\-\./]', '', snippet).lower()
            if norm_modelo not in norm_nombre and norm_modelo not in norm_snippet:
                log.debug(f"Descartado (modelo ausente en título/snippet): '{nombre[:60]}'")
                continue

        limpios.append(r)

    return limpios


# ─────────────────────────────────────────
# CLAUDE — ANALIZA Y RANKEA TOP 5
# ─────────────────────────────────────────
def corregir_marca_modelo(marca: str, modelo: str, opciones: list[dict]) -> tuple[str, str]:
    """Corrige typos de la marca/modelo del prospecto usando los resultados de búsqueda como fuente
    de verdad (p.ej. Lipper→Clipper, Feston→Festo). Si no hay evidencia clara, deja los originales."""
    import re as _re
    try:
        muestras = [{
            "proveedor": o.get("proveedor", ""),
            "nombre": o.get("nombre_producto") or o.get("nombre") or o.get("titulo") or "",
            "notas": (o.get("notas") or "")[:120],
        } for o in (opciones or [])[:5]]
        prompt = (
            "La marca y el modelo/part number los escribió un prospecto y PUEDEN tener errores de dedo.\n"
            f"Marca escrita: {marca}\nModelo / part number escrito: {modelo}\n\n"
            f"Resultados de la búsqueda de ese part number:\n{json.dumps(muestras, ensure_ascii=False)}\n\n"
            "Con los resultados como fuente de verdad, devuelve la marca y el modelo/part number CORRECTOS "
            "(ej: 'Lipper'→'Clipper', 'Feston'→'Festo'). Si los resultados NO dan evidencia clara, deja los "
            "valores escritos tal cual — NO inventes. Responde SOLO JSON: {\"marca\":\"...\",\"modelo\":\"...\"}"
        )
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=150, timeout=20,
            messages=[{"role": "user", "content": prompt}],
        )
        txt = resp.content[0].text if resp.content else ""
        m = _re.search(r'\{[\s\S]*\}', txt)
        if m:
            d = json.loads(m.group(0))
            return (str(d.get("marca") or marca).strip(), str(d.get("modelo") or modelo).strip())
    except Exception as e:
        log.warning(f"corregir_marca_modelo falló: {e}")
    return marca, modelo


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

    # Separar fuentes para el prompt
    crm_productos   = [r for r in resultados_raw if r.get("fuente") == "1crm_productos"]
    crm_proveedores = [r for r in resultados_raw if r.get("fuente") == "1crm_proveedores"][:2]  # máx 2
    shopping        = [r for r in resultados_raw if r.get("fuente") == "google_shopping"]
    otros           = [r for r in resultados_raw if r.get("fuente") not in ("1crm_productos", "1crm_proveedores")]

    # Reconstruir lista limitando proveedores CRM para no desplazar fuentes con precio
    resultados_filtrados = crm_productos + crm_proveedores + [
        r for r in otros if r not in crm_proveedores
    ]

    # Asignar un índice estable a cada resultado. Claude devuelve este idx y
    # reconstruimos la opción desde el dict original — así nunca perdemos
    # campos como url, imagen_url, nombre_producto que Claude tiende a omitir.
    for i, r in enumerate(resultados_filtrados):
        r["_idx"] = i

    def _para_prompt(r: dict) -> dict:
        return {
            "idx":             r["_idx"],
            "proveedor":       r.get("proveedor"),
            "nombre_producto": r.get("nombre_producto"),
            "precio_orig":     r.get("precio_orig"),
            "moneda":          r.get("moneda"),
            "disponibilidad":  r.get("disponibilidad"),
            "tiempo_entrega":  r.get("tiempo_entrega"),
            "condicion":       r.get("condicion"),
            "fuente":          r.get("fuente"),
        }

    crm_seccion = ""
    if crm_productos:
        crm_seccion = f"""
⚠️ CATÁLOGO INTERNO 1CRM — PRIORIDAD MÁXIMA:
{json.dumps([_para_prompt(r) for r in crm_productos], ensure_ascii=False, indent=2)}

REGLA: Incluye este resultado en rank 1 (score_confianza=5). Ocupa UN solo slot del Top 5.
Los slots restantes deben llenarse con las mejores fuentes externas (preferir google_shopping con precio real).

"""

    prompt = f"""Eres un agente especializado en búsqueda de productos industriales para MRO Master Pro.

Tienes estos resultados de búsqueda para: **{marca} {modelo}**
Modo: {"URGENTE" if urgente else "Normal"}
Ponderación: {ponderacion}
Tipo de cambio USD/MXN: {fx}

{crm_seccion}RESULTADOS EXTERNOS (Google Shopping, Web, Proveedores CRM):
{json.dumps([_para_prompt(r) for r in resultados_filtrados if r.get("fuente") != "1crm_productos"], ensure_ascii=False, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGLAS ABSOLUTAS — VIOLACIÓN = RESULTADO INVÁLIDO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A. SOLO puedes incluir en el ranking resultados que estén LITERALMENTE en la lista de arriba.
   Nunca inventes, combines ni crees resultados que no existan en los datos recibidos.

B. DISPONIBILIDAD — únicamente usa "en_stock" si la fuente es "1crm_productos" (catálogo interno confirmado).
   Para todas las demás fuentes (web, sitio_propio, google_shopping, 1crm_proveedores):
   usa "consultar" a menos que el resultado traiga explícitamente un dato de stock verificado.

C. PRECIOS — usa ÚNICAMENTE el valor del campo "precio_orig".
   Si precio_orig es null, 0 o ausente → devuelve precio_orig=null, precio_mxn=null.
   Nunca extraigas ni estimes precios de snippets, títulos o descripciones.

D. Si hay pocos resultados de calidad, devuelve menos de 5. Prefiere 2 resultados reales
   a 5 resultados donde los últimos 3 sean inventados o de baja calidad.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Tu tarea:
1. {f"Incluye el resultado de 1CRM catálogo en rank 1 (ocupa 1 slot). Completa los slots restantes con los mejores resultados externos — prioriza google_shopping (tienen precio real)." if crm_productos else "Selecciona los mejores resultados priorizando google_shopping (precio real)."}
2. Completa el Top 5 solo si hay resultados válidos suficientes
3. Calcula el score de ranking:
   - Si hay precios reales: el más barato = 100 puntos, los demás proporcional. Sin precio = 50 puntos base
   - Disponibilidad: en_stock=100, 1-5días=75, 1-2semanas=50, bajo_pedido=25, importación=10, consultar=30
   - Score final = (precio_pts * {0.3 if urgente else 0.6}) + (disponibilidad_pts * {0.7 if urgente else 0.4})
4. Score de confianza (1-5):
   - 5: producto en catálogo interno 1CRM (fuente=1crm_productos) — API directa, dato real
   - 4: proveedor en 1CRM con historial (fuente=1crm_proveedores)
   - 3: Google Shopping con precio real (fuente=google_shopping)
   - 2: encontrado en website propio (fuente=sitio_propio) — visible en web, NO confirmado en 1CRM
   - 2: resultado Google web con datos claros (fuente=web)
   - 1: fuente no verificada o datos incompletos

Responde SOLO con un JSON array con máximo 5 objetos, ordenados de mayor a menor score_ranking.
Cada objeto referencia un resultado original por su "idx" — NO copies proveedor, url ni nombres,
solo el idx y los campos que calculas tú:
[
  {{
    "idx": 0,
    "rank": 1,
    "dist_autorizado": true/false,
    "precio_orig": null,
    "moneda": "USD",
    "precio_mxn": null,
    "disponibilidad": "en_stock|consultar|bajo_pedido|importacion|dias_N",
    "score_confianza": 1,
    "score_ranking": 0.00,
    "notas": "si sin precio → 'Precio no disponible, consultar sitio'. si sitio_propio → 'Verificar disponibilidad real en 1CRM'"
  }}
]

El "idx" DEBE ser uno de los índices listados arriba. No incluyas ningún texto fuera del JSON."""

    cfg = get_config("buscador")
    extra = {"system": cfg["system_prompt"]} if cfg["system_prompt"] else {}
    response = claude.messages.create(
        model=cfg["model_id"],
        max_tokens=cfg["max_tokens"],
        temperature=cfg["temperature"],
        messages=[{"role": "user", "content": prompt}],
        **extra,
    )

    text = response.content[0].text.strip()
    # Limpiar posibles backticks
    text = text.replace("```json", "").replace("```", "").strip()

    try:
        ranked = json.loads(text)
    except json.JSONDecodeError as e:
        log.error(f"Error parseando JSON de Claude: {e}\nRespuesta: {text}")
        return []

    # Reconstruir cada opción: dict original (con url, imagen_url, nombre_producto)
    # + campos calculados por Claude (rank, scores, precio, disponibilidad, notas).
    por_idx = {r["_idx"]: r for r in resultados_filtrados}
    campos_claude = (
        "rank", "dist_autorizado", "precio_orig", "moneda", "precio_mxn",
        "disponibilidad", "score_confianza", "score_ranking", "notas",
    )
    final = []
    for item in ranked:
        idx = item.get("idx")
        orig = por_idx.get(idx)
        if orig is None:
            log.warning(f"Claude devolvió idx inválido {idx} — omitiendo")
            continue
        opcion = {k: v for k, v in orig.items() if k != "_idx"}
        for campo in campos_claude:
            if campo in item:
                opcion[campo] = item[campo]
        final.append(opcion)

    log.info(f"Claude rankeo: {len(final)} opciones en Top 5")
    return final


# ─────────────────────────────────────────
# GUARDAR RESULTADOS EN SUPABASE
# ─────────────────────────────────────────
def guardar_opciones(rfq_uuid: str, opciones: list[dict], fx: float) -> None:
    log.info(f"Guardando {len(opciones)} opciones en Supabase")

    # Borrar opciones previas por si es reintento
    supabase.table("opciones").delete().eq("rfq_id", rfq_uuid).execute()

    for op in opciones:
        precio_orig_raw = op.get("precio_orig")
        # Solo usar precio si es un número real > 0; null/0/None → None
        precio_orig = float(precio_orig_raw) if precio_orig_raw and float(precio_orig_raw) > 0 else None
        moneda = op.get("moneda", "USD")
        if precio_orig is not None:
            precio_mxn = round(precio_orig * fx, 2) if moneda == "USD" else round(precio_orig, 2)
        else:
            precio_mxn = None

        supabase.table("opciones").insert({
            "rfq_id": rfq_uuid,
            "rank": op.get("rank"),
            "proveedor": op.get("proveedor"),
            "dist_autorizado": op.get("dist_autorizado", False),
            "precio_orig": precio_orig,
            "moneda": moneda,
            "precio_mxn": precio_mxn,
            "disponibilidad": op.get("disponibilidad"),
            "tiempo_entrega": op.get("tiempo_entrega"),
            "condicion": op.get("condicion", "nuevo"),
            "fuente": op.get("fuente"),
            "url": op.get("url"),
            "score_confianza": op.get("score_confianza"),
            "score_ranking": op.get("score_ranking"),
            "notas": op.get("notas"),
            "imagen_url": op.get("imagen_url") or None,
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
        marca_raw = (rfq["marca"] or "").strip()
        modelo = rfq["modelo"].strip()
        urgente = rfq.get("urgente", False)

        # Normalizar placeholders de marca — "(Detectar)", "(Archivo Adjunto)", "N/A", etc.
        # Cualquier valor entre paréntesis o en la lista explícita se trata como placeholder
        _MARCAS_PLACEHOLDER = {"detectar", "auto", "n/a", "na", "desconocido", "unknown", ""}
        marca_norm = re.sub(r'[^\w]', '', marca_raw).lower()
        es_parentesis = bool(re.match(r'^\(.*\)$', marca_raw.strip()))
        if marca_norm in _MARCAS_PLACEHOLDER or es_parentesis:
            marca = ""
            log.info(f"Marca placeholder '{marca_raw}' — buscando solo por modelo: {modelo}")
        else:
            marca = marca_raw.title()

        agregar_log_job(job_id, "inicio", f"Buscando: '{marca}' '{modelo}' | urgente={urgente}")

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

        agregar_log_job(job_id, "busqueda_google", "Iniciando búsqueda en Google Web")
        res_google = buscar_en_google(marca, modelo)
        # Enriquecer precios de resultados web visitando las páginas (JSON-LD / meta tags)
        if res_google:
            sin_precio_antes = sum(1 for r in res_google if r.get("precio_orig") is None and r.get("fuente") == "web")
            if sin_precio_antes > 0:
                agregar_log_job(job_id, "enriquecimiento_precios", f"Extrayendo precios de {min(sin_precio_antes, 4)} páginas web...")
                res_google = enriquecer_precios_web(res_google, max_urls=4)
                con_precio_web = sum(1 for r in res_google if r.get("precio_orig") and r.get("fuente") == "web")
                agregar_log_job(job_id, "enriquecimiento_precios", f"{con_precio_web} precios extraídos de páginas web")
        resultados.extend(res_google)
        agregar_log_job(job_id, "busqueda_google", f"{len(res_google)} resultados")

        agregar_log_job(job_id, "busqueda_shopping", "Iniciando búsqueda en Google Shopping")
        res_shopping = buscar_en_google_shopping(marca, modelo)
        resultados.extend(res_shopping)
        con_precio_shopping = sum(1 for r in res_shopping if r.get("precio_orig"))
        agregar_log_job(job_id, "busqueda_shopping", f"{len(res_shopping)} resultados, {con_precio_shopping} con precio real")

        if not resultados:
            log.warning(f"Sin resultados en ninguna fuente para '{marca} {modelo}' — marcando sin_resultado")
            agregar_log_job(job_id, "sin_resultado", "Ninguna fuente devolvió resultados")
            supabase.table("rfqs").update({"estado": "sin_resultado"}).eq("id", rfq_uuid).execute()
            supabase.table("jobs").update({
                "estado": "completado",
                "finished_at": datetime.utcnow().isoformat(),
                "output": {"opciones_encontradas": 0, "razon": "sin_resultados"},
            }).eq("id", job_id).execute()
            # Crear job notificador para que el frontend reciba rfq_listo
            supabase.table("jobs").insert({
                "rfq_id": rfq_uuid,
                "agente": "notificador",
                "estado": "pendiente",
            }).execute()
            log.info(f"Job {job_id} cerrado como sin_resultado — notificador encolado")
            return

        # Filtrar resultados web basura antes de rankear
        resultados_limpios = _filtrar_resultados_web(resultados, modelo)
        descartados = len(resultados) - len(resultados_limpios)
        if descartados:
            log.info(f"Filtro web: {descartados} resultado(s) descartado(s) por baja calidad")
            agregar_log_job(job_id, "filtro_web", f"{descartados} resultados descartados, {len(resultados_limpios)} pasan al ranking")


        # Claude rankea
        agregar_log_job(job_id, "ranking", f"Claude rankeando {len(resultados_limpios)} resultados")
        top5 = rankear_con_claude(marca, modelo, urgente, resultados_limpios, fx)

        # Recuperar imagen_url desde los resultados originales (Claude no la incluye en su output)
        # Indexar por URL exacta y por fuente+proveedor como fallback
        raw_by_url  = {r.get("url", ""): r for r in resultados_limpios if r.get("url")}
        raw_by_key  = {(r.get("fuente", ""), r.get("proveedor", "")): r for r in resultados_limpios}
        for item in top5:
            if item.get("imagen_url"):
                continue
            orig = raw_by_url.get(item.get("url", "")) or raw_by_key.get((item.get("fuente", ""), item.get("proveedor", "")))
            if orig and orig.get("imagen_url"):
                item["imagen_url"] = orig["imagen_url"]

        if not top5:
            log.warning(f"Claude no generó ranking para '{marca} {modelo}' — marcando sin_resultado")
            agregar_log_job(job_id, "sin_resultado", "Claude no pudo generar ranking")
            supabase.table("rfqs").update({"estado": "sin_resultado"}).eq("id", rfq_uuid).execute()
            supabase.table("jobs").update({
                "estado": "completado",
                "finished_at": datetime.utcnow().isoformat(),
                "output": {"opciones_encontradas": 0, "razon": "ranking_vacio"},
            }).eq("id", job_id).execute()
            supabase.table("jobs").insert({
                "rfq_id": rfq_uuid,
                "agente": "notificador",
                "estado": "pendiente",
            }).execute()
            log.info(f"Job {job_id} cerrado como sin_resultado (ranking vacío) — notificador encolado")
            return

        # ── Corregir typos de marca/modelo con base en los resultados (Lipper→Clipper) ──
        # Se hace apenas termina la búsqueda para que el widget muestre los datos correctos
        # ANTES de publicar. Actualiza el rfq (marca/modelo).
        marca_c, modelo_c = corregir_marca_modelo(marca, modelo, top5)
        if (marca_c and marca_c.lower() != (marca or "").lower()) or \
           (modelo_c and modelo_c.lower() != (modelo or "").lower()):
            log.info(f"Corrección por resultados: '{marca} {modelo}' → '{marca_c} {modelo_c}'")
            try:
                supabase.table("rfqs").update({"marca": marca_c, "modelo": modelo_c}).eq("id", rfq_uuid).execute()
            except Exception as _e:
                log.warning(f"No se pudo actualizar marca/modelo corregidos: {_e}")
            marca, modelo = marca_c, modelo_c

        # ── Garantizar que 1CRM catálogo siempre aparezca en el Top 5 ──
        # Si Claude no incluyó ningún resultado del catálogo, los inyectamos
        tiene_crm_producto = any(r.get("fuente") == "1crm_productos" for r in top5)
        if not tiene_crm_producto and res_productos:
            log.info(f"Claude omitió {len(res_productos)} producto(s) 1CRM — inyectando al Top 5")
            insertar = []
            for prod in res_productos[:2]:  # máximo 2 del catálogo
                precio_raw = prod.get("precio_orig")
                precio = float(precio_raw) if precio_raw and float(precio_raw) > 0 else None
                precio_mxn_iny = round(precio * fx, 2) if precio else None
                insertar.append({
                    "rank": 1,
                    "proveedor": prod["proveedor"],
                    "dist_autorizado": True,
                    "precio_orig": precio,
                    "moneda": prod.get("moneda", "USD"),
                    "precio_mxn": precio_mxn_iny,
                    "disponibilidad": prod.get("disponibilidad", "en_stock"),
                    "tiempo_entrega": prod.get("tiempo_entrega", "Inmediato"),
                    "condicion": prod.get("condicion", "nuevo"),
                    "fuente": "1crm_productos",
                    "url": prod.get("url", ""),
                    "score_confianza": 5,
                    "score_ranking": 95.0,
                    "notas": prod.get("notas", ""),
                    "imagen_url": prod.get("imagen_url"),
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

        # HITL: si el RFQ viene de Bolt (tiene stream_id), notificar al chat
        stream_id = rfq.get("stream_id")
        if stream_id:
            marca_disp = rfq.get("marca", "").strip() or "?"
            modelo_disp = rfq.get("modelo", "").strip()
            supabase.table("mensajes").insert({
                "stream_id": stream_id,
                "role":      "user",
                "content":   (
                    f"[SISTEMA:busqueda_completa] rfq_id={rfq_uuid} "
                    f"marca={marca_disp} modelo={modelo_disp}"
                ),
                "procesado": False,
                "metadata":  {"trigger": "busqueda_completa", "rfq_id": rfq_uuid},
            }).execute()
            log.info(f"Trigger de busqueda_completa enviado al stream {str(stream_id)[:8]}")

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
def resetear_jobs_huerfanos():
    """
    Al arrancar, atiende jobs de buscador/notificador que quedaron en
    'corriendo' por un crash/redeploy anterior (jobs zombie):
      - recientes (< 1h): probablemente los mató este redeploy → reintentar (pendiente)
      - viejos: abandonados → marcar fallido (no resucitar)
    Evita que un agente se vea "encendido" para siempre por un job colgado.
    """
    try:
        huerfanos = (
            supabase.table("jobs")
            .select("id, agente, created_at")
            .in_("agente", ["buscador", "notificador"])
            .eq("estado", "corriendo")
            .execute()
            .data or []
        )
        if not huerfanos:
            log.info("Sin jobs huérfanos del buscador al arrancar")
            return

        reintentados = 0
        fallidos = 0
        for job in huerfanos:
            try:
                age_min = (time.time() - datetime.fromisoformat(job["created_at"]).timestamp()) / 60
            except Exception:
                age_min = 9999

            if age_min < 60:
                supabase.table("jobs").update({
                    "estado":     "pendiente",
                    "started_at": None,
                    "error":      "Reseteado por reinicio del agente (redeploy)",
                }).eq("id", job["id"]).execute()
                reintentados += 1
            else:
                supabase.table("jobs").update({
                    "estado":      "fallido",
                    "finished_at": datetime.utcnow().isoformat(),
                    "error":       "Job zombie: quedó en 'corriendo' tras un crash, sin terminar",
                }).eq("id", job["id"]).execute()
                fallidos += 1

        log.warning(f"⚠ Jobs huérfanos buscador: {reintentados} reintentados, {fallidos} marcados fallido")
    except Exception as e:
        log.error(f"Error reseteando jobs huérfanos del buscador: {e}")


def main():
    log.info("Agente Buscador iniciado — escuchando jobs...")
    log.info(f"1CRM: {ONECRM_BASE}")
    log.info(f"Supabase: {os.environ['SUPABASE_URL']}")
    log.info(f"Poll interval: {POLL_INTERVAL}s")

    # Limpiar jobs que quedaron colgados por un redeploy/crash anterior
    resetear_jobs_huerfanos()

    # Diagnóstico de variables de entorno opcionales
    serpapi_key = os.environ.get("SERPAPI_KEY", "")
    if serpapi_key:
        log.info("SerpAPI: OK")
    else:
        log.warning("SERPAPI_KEY no configurada — búsqueda web desactivada")

    log.info("FX: frankfurter.dev/v1 (gratuito, sin API key) con fallback a 17.50")

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
