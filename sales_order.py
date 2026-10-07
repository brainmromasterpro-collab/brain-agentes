"""
COTEJO DE ORDEN DE COMPRA vs COTIZACIONES — Fase 1 del stream Sales Order
=========================================================================
Recibe el PO ya estructurado (de orden_compra.leer_po) y lo cruza contra las cotizaciones del
cliente en 1CRM, SIN escribir nada. Devuelve: coincidencias por producto, discrepancias
(precio distinto, producto ausente, cotización vencida) y las cotizaciones CANDIDATAS a ser la
referencia (cuando el PO abarca varias, el usuario elige).

Hechos de la API 1CRM en que se apoya (verificados en vivo):
- data/Account con filter_text SÍ filtra por nombre.
- data/Quote?filters[billing_account_id]=<id> devuelve las cotizaciones del cliente CON line_items
  embebidos (name, mfr_part_no, unit_price, quantity) → índice en una sola llamada.
- valid_until y quote_stage NO vienen en el listado → se sacan con un GET de detalle solo de las
  cotizaciones candidatas (pocas).
"""

import os
import re
import logging
import unicodedata
from datetime import date, datetime

import httpx

log = logging.getLogger("sales_order")

CRM_BASE = (os.environ.get("ONECRM_URL", "") or "").rstrip("/")
CRM_AUTH = (os.environ.get("ONECRM_USERNAME", ""), os.environ.get("ONECRM_PASSWORD", ""))

# Tolerancia de precio por defecto: 1% o 1 centavo (lo mayor). Se afina con casos reales.
TOL_REL = 0.01
TOL_ABS = 0.01

# Etapas de cotización que ya NO son válidas como origen de una orden.
STAGE_MUERTAS = {"Closed Lost", "Closed Dead"}

# Cómo nos llamamos (para confirmar que la PO es para NOSOTROS). Acepta varios alias separados por
# coma en NUESTRO_NOMBRE (p.ej. "MRO Master Pro,MRO Online 4U,MRO MasterPro").
_NUESTROS_TOKENS = [re.sub(r"[^A-Z0-9]", "", n.upper())
                    for n in os.environ.get("NUESTRO_NOMBRE",
                        "MRO Master Pro,MRO MasterPro,MRO Online 4U").split(",") if n.strip()]


def _crm_get(path: str, params: dict | None = None) -> dict:
    r = httpx.get(f"{CRM_BASE}/api.php/{path}", params=params or {}, auth=CRM_AUTH, timeout=40)
    try:
        return r.json()
    except Exception:
        return {}


def _crm_post(model: str, data: dict) -> dict:
    r = httpx.post(f"{CRM_BASE}/api.php/data/{model}", json={"data": data}, auth=CRM_AUTH, timeout=45)
    try:
        return r.json()
    except Exception:
        return {"error": r.text[:200]}


def _crm_patch(model: str, rec_id: str, data: dict) -> dict:
    r = httpx.patch(f"{CRM_BASE}/api.php/data/{model}/{rec_id}", json={"data": data}, auth=CRM_AUTH, timeout=45)
    try:
        return r.json()
    except Exception:
        return {"error": r.text[:200]}


def _crm_delete(model: str, rec_id: str) -> dict:
    r = httpx.delete(f"{CRM_BASE}/api.php/data/{model}/{rec_id}", auth=CRM_AUTH, timeout=45)
    try:
        return r.json()
    except Exception:
        return {"error": r.text[:120]}


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def _norm(s: str) -> str:
    """Normaliza para comparar nombres/partes: sin acentos, mayúsculas, espacios colapsados
    (así 'México' == 'Mexico')."""
    return re.sub(r"\s+", " ", _sin_acentos(s).strip()).upper()


def _compact(s: str) -> str:
    """Versión sin separadores para tolerar '48625 RO-222' vs '48625RO222'."""
    return re.sub(r"[^A-Z0-9]", "", _norm(s))


def _clave(pn: str, desc: str = "") -> str:
    """Clave de comparación de una línea: el número de parte compacto, o —si la cotización/SO no
    tiene número de parte (líneas libres tipo "Lamina de acero inoxidable")— la descripción compacta.
    Sin esto esas líneas no se indexaban y NUNCA podían coincidir con un PO (bug real, PO Weidmann
    4501054140)."""
    return (_compact(pn) or _compact(desc))[:40]


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 1. CUENTA
# ─────────────────────────────────────────────────────────────
def buscar_cuenta(nombre: str) -> dict | None:
    """Encuentra la cuenta del cliente por nombre. Devuelve {id, nombre, url} o None.

    BUG REAL confirmado en vivo (PO de Weidmann): el PO llega con la razón social LOCAL de la
    subsidiaria ("WEIDMANN TECNOLOGIA ELECTRICA DE MEXICO") mientras la cuenta en 1CRM está
    registrada con el nombre en inglés ("Weidmann Electrical Technology") — la MISMA empresa, dos
    nombres distintos que solo comparten la marca/razón social raíz ("Weidmann"). El filter_text de
    1CRM con el nombre COMPLETO del PO devolvía 0 registros (no es un problema de esta función — la
    búsqueda de 1CRM ya no encontraba nada que comparar), así que nunca llegaba a intentar el match
    local. Fallback: si el nombre completo no da nada, reintenta filter_text con SOLO la primera
    PALABRA (normalmente la marca/razón social distintiva en nombres de empresa).

    OJO — primer intento de este fallback tenía un falso positivo real: comparar por PREFIJO de
    texto compacto hacía que "EMPRESA..." (razón social inventada de prueba) matcheara "EMPRESARIAL
    SANTINOX" (cuenta real, nada que ver) porque "empresarial" empieza con las letras "empresa". Se
    corrigió comparando la PRIMERA PALABRA COMPLETA (no un prefijo parcial) y exigiendo que sea la
    ÚNICA cuenta candidata con esa palabra — ante varias cuentas que comparten una primera palabra
    genérica ("Industrias X" vs "Industrias Y"), no se adivina, se deja sin encontrar."""
    nombre = (nombre or "").strip()
    if not nombre or not CRM_BASE:
        return None

    def _mejor_de(data: dict) -> dict | None:
        n = _norm(nombre)
        nc = _compact(nombre)
        mejor = None
        for r in data.get("records", []):
            rn = _norm(r.get("name", ""))
            rc = _compact(r.get("name", ""))
            # match exacto, o uno contenido en el otro con largo razonable (evita falsos por 1-2 letras)
            if rn == n or (len(min(nc, rc, key=len)) >= 6 and (nc in rc or rc in nc)):
                mejor = r
                if rn == n:
                    break
        return mejor

    data = _crm_get("data/Account", {"filter_text": nombre, "limit": 20})
    mejor = _mejor_de(data)

    if not mejor:
        primera = _norm(nombre).split(" ")[0] if nombre else ""
        if len(primera) >= 5:
            data2 = _crm_get("data/Account", {"filter_text": primera, "limit": 20})
            recs2 = data2.get("records", [])
            candidatos = [r for r in recs2 if (_norm(r.get("name", "")).split(" ") or [""])[0] == primera]
            if len(candidatos) == 1:
                mejor = candidatos[0]

    if not mejor:
        return None
    return {
        "id":     mejor.get("id"),
        "nombre": mejor.get("name", ""),
        "url":    f"{CRM_BASE}/index.php?module=Accounts&action=DetailView&record={mejor.get('id')}",
    }


def cuenta_por_id(cuenta_id: str) -> dict | None:
    """Trae una cuenta por id (para derivar el cliente desde la cotización referenciada)."""
    if not cuenta_id:
        return None
    d = _crm_get(f"data/Account/{cuenta_id}")
    rec = d.get("record", d)
    if not rec.get("id"):
        return None
    return {"id": rec["id"], "nombre": rec.get("name", ""),
            "url": f"{CRM_BASE}/index.php?module=Accounts&action=DetailView&record={rec['id']}"}


def _terminos_y_moneda(cuenta_id: str) -> dict:
    """Trae los TÉRMINOS DE PAGO (default_terms) y la MONEDA de la cuenta — específicos del cliente.
    Van en la Sales Order: terms y currency_id."""
    d = _crm_get(f"data/Account/{cuenta_id}")
    rec = d.get("record", d)
    return {
        "terminos_pago": rec.get("default_terms") or "",
        "currency_id":   rec.get("currency_id") or "",
        "moneda":        rec.get("currency") or "",
    }


def _es_para_nosotros(proveedor: str) -> bool | None:
    """¿La orden va dirigida a NOSOTROS? True/False, o None si el PO no dice el proveedor."""
    prov = re.sub(r"[^A-Z0-9]", "", _norm(proveedor))
    if not prov:
        return None
    return any(t and (t in prov or prov in t) for t in _NUESTROS_TOKENS)


# ─────────────────────────────────────────────────────────────
# 2. COTIZACIONES DEL CLIENTE (índice de líneas)
# ─────────────────────────────────────────────────────────────
def cotizaciones_cliente(cuenta_id: str, limite: int = 300) -> list[dict]:
    """Cotizaciones recientes del cliente CON sus líneas. Cada una:
    {id, nombre, lines:[{part_number, part_compact, unit_price, quantity, descripcion}]}."""
    # La API devuelve páginas cortas (~20): se pagina hasta `limite`. Con 40 se perdían cotizaciones
    # viejas del cliente (bug real: la cotización de 5999833 de Weidmann quedaba fuera, 161 en total).
    registros: list = []
    offset = 0
    while len(registros) < limite:
        data = _crm_get("data/Quote", {
            "filters[billing_account_id]": cuenta_id,
            "order_by": "date_modified desc",
            "limit": 20, "offset": offset,
        })
        page = data.get("records", [])
        if not page:
            break
        registros += page
        offset += 20
        if len(page) < 20:
            break
    out: list[dict] = []
    for q in registros[:limite]:
        lines = []
        for li in (q.get("line_items") or []):
            pn = li.get("mfr_part_no") or ""
            lines.append({
                "part_number": pn,
                "part_compact": _clave(pn, li.get("name", "")),
                "unit_price": _num(li.get("unit_price")),
                "quantity":   _num(li.get("quantity")),
                "descripcion": li.get("name", ""),
            })
        out.append({"id": q.get("id"), "nombre": q.get("name", ""), "lines": lines})

    # El listado de cotizaciones de la API NO devuelve las CERRADAS/aceptadas (las ya convertidas a
    # Sales Order — de 980 solo salen 974). Pero se pueden reutilizar (pedido de Gabriel: aunque estén
    # cerradas o vencidas deben salir como opción y que el usuario escoja), así que se recuperan por el
    # related_quote_id de las SO del cliente (el GET por id sí las devuelve).
    ya = {q["id"] for q in out}
    so_ids: dict = {}
    # SO abiertas (listado) + SO CERRADAS, que tampoco salen en el listado pero sí cuelgan de las
    # facturas del cliente (Invoice.from_so_id).
    for mod, campo in (("SalesOrder", "id"), ("Invoice", "from_so_id")):
        off = 0
        while off < 400:
            page = _crm_get(f"data/{mod}", {"filters[billing_account_id]": cuenta_id, "limit": 20, "offset": off}).get("records", [])
            if not page:
                break
            for r in page:
                sid = r.get("id") if campo == "id" else (_crm_get(f"data/Invoice/{r['id']}").get("record", {}) or {}).get("from_so_id")
                if sid:
                    so_ids[sid] = True
            if len(page) < 20:
                break
            off += 20
    for sid in so_ids:
        qid = (_crm_get(f"data/SalesOrder/{sid}").get("record", {}) or {}).get("related_quote_id")
        if qid and qid not in ya:
            cq = cotizacion_por_id(qid)
            if cq:
                cq["cerrada"] = True
                out.append(cq)
                ya.add(qid)
    return out


def cotizacion_por_ref(ref: str) -> dict | None:
    """Busca una cotización por su número/folio (p.ej. 'Q2026-0608-2042') citado en el PO.
    Devuelve {id, nombre, lines[...], referenciada:True} o None. filter_text SÍ encuentra el folio."""
    ref = (ref or "").strip()
    if not ref or not CRM_BASE:
        return None
    data = _crm_get("data/Quote", {"filter_text": ref, "limit": 3})
    recs = data.get("records", [])
    if not recs:
        return None
    qid = recs[0].get("id")
    full = _crm_get(f"data/Quote/{qid}")
    rec = full.get("record", full)
    lines = []
    for li in (rec.get("line_items") or []):
        pn = li.get("mfr_part_no") or ""
        lines.append({
            "part_number": pn, "part_compact": _clave(pn, li.get("name", "")),
            "unit_price": _num(li.get("unit_price")), "quantity": _num(li.get("quantity")),
            "descripcion": li.get("name", ""),
        })
    return {"id": qid, "nombre": rec.get("name", ""), "lines": lines, "referenciada": True,
            "cuenta_id": rec.get("billing_account_id") or ""}


def cotizacion_por_id(quote_id: str) -> dict | None:
    """Igual que cotizacion_por_ref pero buscando directo por id — se usa cuando el usuario elige
    a mano cuál de varias cotizaciones candidatas AMBIGUAS es la correcta (ver cotejar(...,
    forzar_quote_id=...)). Devuelve {id, nombre, lines[...], referenciada:True, cuenta_id} o None."""
    if not quote_id or not CRM_BASE:
        return None
    full = _crm_get(f"data/Quote/{quote_id}")
    rec = full.get("record", full)
    if not rec.get("id"):
        return None
    lines = []
    for li in (rec.get("line_items") or []):
        pn = li.get("mfr_part_no") or ""
        lines.append({
            "part_number": pn, "part_compact": _clave(pn, li.get("name", "")),
            "unit_price": _num(li.get("unit_price")), "quantity": _num(li.get("quantity")),
            "descripcion": li.get("name", ""),
        })
    return {"id": rec["id"], "nombre": rec.get("name", ""), "lines": lines, "referenciada": True,
            "cuenta_id": rec.get("billing_account_id") or ""}


def _vigencia(quote_id: str) -> dict:
    """Detalle mínimo de una cotización candidata: valid_until, quote_stage, vigente."""
    d = _crm_get(f"data/Quote/{quote_id}")
    rec = d.get("record", d)
    vu = rec.get("valid_until") or ""
    stage = rec.get("quote_stage") or ""
    vigente = True
    motivo = ""
    if vu:
        try:
            if datetime.strptime(vu[:10], "%Y-%m-%d").date() < date.today():
                vigente, motivo = False, f"vencida el {vu[:10]}"
        except Exception:
            pass
    if stage in STAGE_MUERTAS:
        vigente, motivo = False, f"etapa {stage}"
    return {"valid_until": vu, "quote_stage": stage, "vigente": vigente, "motivo": motivo}


# ─────────────────────────────────────────────────────────────
# 3. COTEJO
# ─────────────────────────────────────────────────────────────
def _match_item(pc: str, descripcion: str, indice: dict) -> tuple[list, str]:
    """Cascada de match para UN renglón del PO. Devuelve (líneas, tipo):
      'exacto'      → número de parte idéntico al de una cotización.
      'parcial'     → por prefijo (>=5 chars): tolera truncados/variantes ('48625' vs '48625RO222').
      'descripcion' → el cliente usó SU código interno (p.ej. PIN.140242) y el número real del
                      fabricante (CSMD-20BT3ATT3) aparece dentro de la DESCRIPCIÓN del renglón.
      ''            → no encontrado.
    Parcial y descripción se marcan para que el humano confirme — nunca se dan por buenos en silencio."""
    if pc in indice:
        return indice[pc], "exacto"
    if len(pc) >= 5:
        hits: list = []
        for k, v in indice.items():
            if len(k) >= 5 and (k.startswith(pc) or pc.startswith(k)):
                hits.extend(v)
        if hits:
            return hits, "parcial"
    # scan de descripción: ¿algún número de parte de las cotizaciones está DENTRO del texto del renglón?
    dc = _compact(descripcion)
    if len(dc) >= 6:
        hits = []
        for k, v in indice.items():
            if len(k) >= 6 and k in dc:
                hits.extend(v)
        if hits:
            return hits, "descripcion"
    return [], ""


_STOP = {"para", "con", "sin", "del", "los", "las", "una", "uno", "por", "mas", "que", "de", "en", "el", "la", "y", "x"}


def _tokens(txt: str) -> set:
    """Palabras significativas (>=3 letras/dígitos, sin acentos ni palabras vacías) de un texto."""
    return {w for w in re.findall(r"[A-Z0-9]+", _norm(txt)) if len(w) >= 3 and w.lower() not in _STOP}


def _pts_precio(po_precio, cot_precio) -> int:
    """Puntos por cercanía de precio PO vs cotizado: idéntico (al centavo/0.02%) 12, <=1% 8, <=5% 5, <=15% 2."""
    if po_precio is None or not cot_precio:
        return 0
    d = abs(po_precio - cot_precio)
    if d <= 0.05 or d / max(abs(cot_precio), 0.01) <= 0.0002:
        return 12
    r = d / max(abs(cot_precio), 0.01)
    return 8 if r <= 0.01 else (5 if r <= 0.05 else (2 if r <= 0.15 else 0))


def _match_texto_precio(descripcion: str, po_precio, indice: dict, max_quotes: int = 6) -> tuple[list, str]:
    """Respaldo cuando el número de parte del PO no aparece en ninguna cotización (el cliente usa SU
    código interno / la cotización no trae número de parte): puntúa TODAS las líneas de las
    cotizaciones del cliente por (a) palabras de la descripción en común y (b) cercanía de PRECIO
    (idéntico +12, <=5% +6, <=15% +2; el precio es el indicador principal) y devuelve las mejores
    cotizaciones (hasta `max_quotes`) de más a menos cercana, para que el usuario elija. Siempre es
    un match por confirmar. Cada línea trae '_txt' (proporción de palabras del PO que comparte)."""
    toks = _tokens(descripcion)
    mejores: dict = {}   # quote_id -> (score, q, ln)
    vistos = set()
    for lst in indice.values():
        for (q, ln) in lst:
            k = (q["id"], ln["part_compact"], ln["unit_price"])
            if k in vistos:
                continue
            vistos.add(k)
            comunes = len(toks & _tokens(ln.get("descripcion", ""))) if toks else 0
            ratio = comunes / len(toks) if toks else 0
            score_txt = 8 * ratio
            score_pr = _pts_precio(po_precio, ln["unit_price"])
            if comunes < 1 and score_pr < 12:
                score_pr = score_pr / 2   # precio parecido pero producto sin relación: vale la mitad
            score = score_txt + score_pr
            # Congruente con el CRM: solo se sugiere si hay relación REAL de producto o precio idéntico —
            # >=50% de las palabras del PO, o precio idéntico, o precio <=5% con alguna palabra en común.
            # (Antes entraban productos sin relación solo por tener un precio parecido: básculas,
            # hidrolavadoras, cafeteras... que confundían al usuario.)
            if not (ratio >= 0.5 or score_pr >= 12 or (comunes >= 1 and _pts_precio(po_precio, ln["unit_price"]) >= 5)):
                continue   # sin palabras en común y sin precio IDÉNTICO: ruido (precios parecidos de productos distintos)
            if q["id"] not in mejores or score > mejores[q["id"]][0]:
                ln2 = dict(ln); ln2["_txt"] = ratio
                mejores[q["id"]] = (score, q, ln2)
    ordenadas = sorted(mejores.values(), key=lambda t: t[0], reverse=True)[:max_quotes]
    if not ordenadas:
        return [], ""
    return [(q, ln) for (_s, q, ln) in ordenadas], "similar"


def _match_por_precio(descripcion: str, po_precio, indice: dict) -> list:
    """Líneas de cotización cuyo PRECIO coincide con el del PO (el precio es lo más importante, pedido
    de Gabriel), aunque el número de parte/descripción no coincidan. Cuenta si comparte al menos una
    palabra significativa con la descripción del PO, o si es la ÚNICA línea con ese precio."""
    if po_precio is None:
        return []
    toks = _tokens(descripcion)
    hits, vistos = [], set()
    for lst in indice.values():
        for (q, ln) in lst:
            k = (q["id"], ln["part_compact"], ln["unit_price"])
            if k in vistos or ln["unit_price"] is None or not _precio_coincide(po_precio, ln["unit_price"]):
                continue
            vistos.add(k)
            hits.append((q, ln, bool(toks & _tokens(ln.get("descripcion", "")))))
    con_texto = [(q, ln) for (q, ln, t) in hits if t]
    if con_texto:
        return con_texto
    return [(q, ln) for (q, ln, _t) in hits] if len(hits) == 1 else []


def _etiqueta_coincidencia(puntaje: float, n_items: int) -> str:
    """exacta / casi exacta / cercana según el puntaje contra el máximo posible (parte exacta 6 +
    precio idéntico 12 = 18 por renglón del PO)."""
    r = puntaje / (18 * max(n_items, 1))
    return "exacta" if r >= 0.9 else ("casi exacta" if r >= 0.55 else "cercana")


def _armar_draft(cuenta: dict, tm: dict, po: dict, items_out: list,
                 candidatas: list, quotes: list, para_nosotros) -> dict | None:
    """Arma el borrador de la Sales Order para el PREVIO: qué se va a mandar y DE DÓNDE sale cada dato,
    con las líneas de la cotización de referencia marcadas 'pedido/no pedido' por el PO. No escribe."""
    if not candidatas:
        return None
    ref = candidatas[0]                                   # referencia = citada o de mayor cobertura
    ref_quote = next((q for q in quotes if q["id"] == ref["id"]), None)
    if not ref_quote:
        return None

    # po_qty por (cotización, número de parte del renglón de la cotización)
    po_qty_map: dict = {}
    for it in items_out:
        cid = (it.get("cotizacion") or {}).get("id")
        pcn = _clave(it.get("part_number_cotizacion") or "", it.get("descripcion_cotizacion") or "")
        if cid and pcn:
            po_qty_map[(cid, pcn)] = it.get("cantidad_po")

    po_precio_map: dict = {}
    for it in items_out:
        cid = (it.get("cotizacion") or {}).get("id")
        pcn = _clave(it.get("part_number_cotizacion") or "", it.get("descripcion_cotizacion") or "")
        if cid and pcn:
            po_precio_map[(cid, pcn)] = it.get("precio_po")

    lineas = []
    for ln in ref_quote["lines"]:
        po_precio = po_precio_map.get((ref["id"], ln["part_compact"]))
        po_qty = po_qty_map.get((ref["id"], ln["part_compact"]))
        pedido = po_qty is not None
        lineas.append({
            "part_number": ln["part_number"] or ln["descripcion"], "descripcion": ln["descripcion"],
            "unit_price": ln["unit_price"], "quote_qty": ln["quantity"],
            "po_qty": po_qty, "pedido": pedido, "incluir_default": pedido,
            "precio_po": po_precio,
            "precio": po_precio if (pedido and po_precio is not None) else ln["unit_price"],  # el PO manda; editable
            "cantidad": po_qty if pedido else ln["quantity"],   # cantidad final propuesta
        })

    return {
        "puede_crear": para_nosotros is not False and ref.get("vigente", True),
        "cuenta_id": cuenta["id"], "cuenta_nombre": cuenta["nombre"],
        "quote_id": ref["id"], "quote_nombre": ref["nombre"],
        "quote_vigente": ref.get("vigente"), "quote_stage": ref.get("quote_stage"),
        "quote_referenciada": ref.get("referenciada", False),
        "currency_id": tm["currency_id"], "moneda": po.get("moneda", "") or tm["moneda"],
        "terms": tm["terminos_pago"],
        "po_number": po.get("po_number", ""),
        "file_url": po.get("file_url", ""),  # para subir el PO a Documentos/Notas al crear la SO
        "para_nosotros": para_nosotros,
        "lineas": lineas,
        # Trazabilidad: de dónde sale cada dato (el usuario lo ve en el previo).
        "origen": {
            "cuenta": "cliente del CRM" + (" (derivado de la cotización citada)" if ref.get("referenciada") else ""),
            "cotizacion": ref["nombre"] + (" · citada en el PO" if ref.get("referenciada") else " · mayor cobertura"),
            "terminos": "términos de pago de la cuenta" if tm["terminos_pago"] else "⚠ la cuenta no tiene términos configurados",
            "moneda": "moneda de la cuenta",
            "po_number": "número de la orden de compra del cliente",
            "cantidades": "cantidades del PO (lo que ordenó el cliente)",
            "precios": "precios de la cotización de referencia",
        },
    }


def _precio_coincide(po_precio: float | None, cot_precio: float | None) -> bool:
    if po_precio is None or cot_precio is None:
        return True  # sin precio en el PO → no se marca discrepancia de precio
    return abs(po_precio - cot_precio) <= max(TOL_ABS, TOL_REL * cot_precio)


def so_existente_por_po(po_number: str, cuenta_id: str = "") -> dict | None:
    """¿YA existe una Sales Order para este número de PO? Evita duplicados — si ya está creada,
    el flujo debe solo confirmarlo en vez de volver a cotejar/crear otra. Verificado en vivo:
    filters[purchase_order_num] SÍ filtra de verdad en el módulo SalesOrder (a diferencia de otros
    campos relate ya documentados en reference_1crm_api_quirks — probado con PO real e inexistente)."""
    po_number = (po_number or "").strip()
    if not po_number:
        return None
    objetivo = _compact(po_number)

    def _como_resultado(sid: str, rec: dict) -> dict:
        numero = f"{rec.get('prefix', '')}{rec.get('so_number', '')}".strip() or rec.get("name", "")
        return {
            "id": sid, "numero": numero, "so_stage": rec.get("so_stage"),
            "url": f"{CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={sid}",
        }

    # 1) Rápido: filtro nativo por "Núm. Pedido de Compra" (purchase_order_num).
    data = _crm_get("data/SalesOrder", {"filters[purchase_order_num]": po_number, "limit": 5})
    for r in data.get("records", []):
        sid = r.get("id")
        if not sid:
            continue
        rec = _crm_get(f"data/SalesOrder/{sid}").get("record", {})
        if cuenta_id and rec.get("billing_account_id") != cuenta_id:
            continue  # mismo número de PO pero de OTRO cliente — coincidencia, no duplicado
        return _como_resultado(sid, rec)

    # 2) Respaldo (pedido de Gabriel): buscar entre las SO DEL CLIENTE si el número coincide en
    # "Núm. Pedido de Compra" O en el campo "SalesOrder" (el documento del PO que se ligó a la SO,
    # cuyo nombre es el número de PO), comparando sin espacios/guiones/mayúsculas — cubre PO
    # capturados con formato distinto (espacios, sufijos de renglón "/10", etc.).
    if cuenta_id:
        data = _crm_get("data/SalesOrder", {"filters[billing_account_id]": cuenta_id, "limit": 100})
        for r in data.get("records", []):
            sid = r.get("id")
            if not sid:
                continue
            rec = _crm_get(f"data/SalesOrder/{sid}").get("record", {})
            if rec.get("billing_account_id") != cuenta_id:
                continue
            for campo in (rec.get("purchase_order_num"), rec.get("SalesOrder")):
                c = _compact(str(campo or ""))
                if c and (c == objetivo or (len(c) >= 8 and (c.startswith(objetivo) or objetivo.startswith(c)))):
                    return _como_resultado(sid, rec)
    return None


def ventas_previas(cuenta_id: str, po: dict, max_hits: int = 5) -> list[dict]:
    """Ventas YA HECHAS al cliente (Facturas y Sales Orders) cuyas líneas coinciden con los renglones
    del PO por descripción y/o precio. Las cotizaciones aceptadas/cerradas NO salen en el listado de
    cotizaciones de la API, y una línea agregada directo a la SO/Factura (como la lámina de Weidmann,
    FAC243 a $2,239.38) tampoco está en ninguna cotización — pero es la mejor prueba de que ese producto
    ya se vendió a ese precio. Devuelve [{tipo, numero, fecha, descripcion, precio, cantidad,
    coincide_precio, coincide_descripcion, url}] de mejor a menor coincidencia."""
    hits = []
    for it in po.get("items", []):
        toks = _tokens(it.get("descripcion", ""))
        pc = _compact(it.get("part_number", ""))
        po_precio = _num(it.get("precio_unitario"))
        for mod, url_mod, pref in (("Invoice", "Invoices", "INV"), ("SalesOrder", "SalesOrders", "SO")):
            offset = 0
            while offset < 400:
                data = _crm_get(f"data/{mod}", {"filters[billing_account_id]": cuenta_id, "limit": 20, "offset": offset})
                page = data.get("records", [])
                if not page:
                    break
                for rec in page:
                    for ln in (rec.get("line_items") or []):
                        ratio = (len(toks & _tokens(ln.get("name", ""))) / len(toks)) if toks else 0
                        pr = _pts_precio(po_precio, _num(ln.get("unit_price")))
                        mismo_pn = bool(pc) and _compact(ln.get("mfr_part_no", "")) == pc
                        c_desc = ratio >= 0.5 or mismo_pn
                        c_prec = pr >= 12
                        if not (c_desc or (c_prec and ratio >= 0.2)):
                            continue
                        hits.append({
                            "score": (12 if c_prec else pr) + 8 * ratio + (6 if mismo_pn else 0),
                            "tipo": "Factura" if mod == "Invoice" else "Sales Order",
                            "numero": (rec.get("_display") or rec.get("name") or "").split(":")[0],
                            "fecha": (rec.get("invoice_date") or rec.get("date_entered") or "")[:10],
                            "descripcion": ln.get("name", ""), "precio": _num(ln.get("unit_price")),
                            "cantidad": _num(ln.get("quantity")),
                            "coincide_precio": c_prec, "coincide_descripcion": c_desc,
                            "url": f"{CRM_BASE}/index.php?module={url_mod}&action=DetailView&record={rec.get('id')}",
                            "_id": rec.get("id"), "_mod": mod,
                        })
                if len(page) < 20:
                    break
                offset += 20
    hits.sort(key=lambda h: h["score"], reverse=True)
    top = hits[:max_hits]
    for h in top:
        h.pop("score", None)
        det = _crm_get(f"data/{h.pop('_mod')}/{h.pop('_id')}").get("record", {})  # el listado no trae fecha
        h["fecha"] = (det.get("invoice_date") or det.get("date_entered") or "")[:10]
    return top


def cotejar(po: dict, forzar_quote_id: str = "") -> dict:
    """Cruza el PO contra las cotizaciones del cliente. NO escribe nada.
    Devuelve un diagnóstico completo para armar el previo y elegir la cotización de referencia.

    `forzar_quote_id`: cuando el cotejo salió AMBIGUO (ver 'ambiguo' en el resultado — varias
    cotizaciones candidatas sin que ninguna sea un match claro) y el usuario YA eligió a mano cuál
    es la correcta, se vuelve a cotejar tratando esa cotización como la referenciada — mismo
    mecanismo que cuando el PO SÍ trae el folio citado."""
    if not CRM_BASE:
        return {"error": "1CRM no configurado"}
    if po.get("error"):
        return {"error": f"el PO no se pudo leer: {po['error']}"}

    # Cotización REFERENCIADA en el propio PO (cita/adjunta nuestro presupuesto Q2026-…): mejor pista
    # de origen. La buscamos primero porque además define la cuenta cuando el nombre no matchea.
    ref_q = cotizacion_por_id(forzar_quote_id) if forzar_quote_id else cotizacion_por_ref(po.get("cotizacion_ref", ""))

    cuenta = buscar_cuenta(po.get("cliente", ""))
    if not cuenta and ref_q and ref_q.get("cuenta_id"):
        cuenta = cuenta_por_id(ref_q["cuenta_id"])  # la cotización citada define el cliente
    if not cuenta:
        return {
            "ok": False,
            "avisos": [f"No encontré en el CRM la cuenta del cliente «{po.get('cliente','?')}». "
                       f"Verifica el nombre o si el cliente ya existe."],
            "cuenta": None, "items": [], "cotizaciones_candidatas": [],
        }

    # ¿Ya existe una SO para este PO? Si sí, no hace falta cotejar contra cotizaciones — solo
    # confirmar que ya está creada (pedido explícito de Gabriel: evitar duplicar Sales Orders).
    so_ya = so_existente_por_po(po.get("po_number", ""), cuenta["id"])
    if so_ya:
        return {
            "ok": True, "ya_existe": True, "so": so_ya, "cuenta": cuenta,
            "avisos": [f"Ya existe la Sales Order {so_ya['numero']} para el PO «{po.get('po_number', '')}» "
                       f"— no hace falta crear otra."],
            "items": [], "cotizaciones_candidatas": [],
        }

    # Términos de pago + moneda de ESTE cliente (van a la Sales Order).
    tm = _terminos_y_moneda(cuenta["id"])

    # ¿La orden es para NOSOTROS? (si el PO nombra a otro proveedor, hay que confirmar antes de crear).
    para_nosotros = _es_para_nosotros(po.get("proveedor", ""))

    quotes = cotizaciones_cliente(cuenta["id"])

    # Priorizamos la referenciada, pero seguimos el proceso completo (verificamos productos/precios/
    # vigencia igual). Si no está en la lista reciente del cliente, la agregamos.
    ref_id = None
    if ref_q:
        ref_id = ref_q["id"]
        if ref_id not in {q["id"] for q in quotes}:
            quotes.insert(0, ref_q)

    # índice part_compact → lista de (quote, line)
    indice: dict[str, list[tuple[dict, dict]]] = {}
    for q in quotes:
        for ln in q["lines"]:
            if ln["part_compact"]:
                indice.setdefault(ln["part_compact"], []).append((q, ln))

    items_out: list[dict] = []
    cobertura: dict[str, int] = {}     # quote_id → nº de items del PO que cubre
    puntaje: dict[str, float] = {}     # quote_id → puntaje de coincidencia (parte/descripción/precio)
    precio_ok_n: dict[str, int] = {}   # quote_id → nº de items cuyo precio coincide
    discrepancias: list[str] = []

    for it in po.get("items", []):
        pn = it.get("part_number", "")
        pc = _compact(pn)
        po_precio = _num(it.get("precio_unitario"))
        po_qty = _num(it.get("cantidad"))
        candidatos, tipo_match = _match_item(pc, it.get("descripcion", ""), indice)
        if not candidatos:
            candidatos, tipo_match = _match_texto_precio(it.get("descripcion", ""), po_precio, indice)
        # Si ningún candidato por parte/descripción tiene el precio del PO, agregar las líneas de
        # cualquier cotización que SÍ tengan ese precio (el precio manda).
        extras: set = set()
        if po_precio is not None and not any(_precio_coincide(po_precio, ln["unit_price"]) for (_q, ln) in candidatos):
            for (q, ln) in _match_por_precio(it.get("descripcion", ""), po_precio, indice):
                if not any(q["id"] == q2["id"] and ln is ln2 for (q2, ln2) in candidatos):
                    candidatos = list(candidatos) + [(q, ln)]
                    extras.add((q["id"], id(ln)))
            if extras and not tipo_match:
                tipo_match = "precio"
        parcial = tipo_match in ("parcial", "descripcion", "similar", "precio")

        if not candidatos:
            items_out.append({
                "part_number": pn, "cantidad_po": po_qty, "precio_po": po_precio,
                "estado": "no_encontrado", "cotizacion": None, "precio_cotizacion": None,
            })
            discrepancias.append(f"«{pn}» no está en ninguna cotización reciente del cliente.")
            continue

        # elegir la línea de mejor match de precio; registrar cobertura por cotización
        mejor_q, mejor_ln, precio_ok = None, None, False
        for (q, ln) in candidatos:
            ok = _precio_coincide(po_precio, ln["unit_price"])
            if mejor_q is None or (ok and not precio_ok):
                mejor_q, mejor_ln, precio_ok = q, ln, ok
        if (mejor_q["id"], id(mejor_ln)) in extras:
            tipo_match, parcial = "precio", True
        base = {"exacto": 6, "parcial": 4, "descripcion": 4, "similar": 0, "precio": 2}.get(tipo_match, 1)
        for (q, _ln) in candidatos:
            cobertura[q["id"]] = cobertura.get(q["id"], 0) + 1
            p_ok = _precio_coincide(po_precio, _ln["unit_price"])
            _bono_precio = _pts_precio(po_precio, _ln["unit_price"])
            if tipo_match == "similar" and _ln.get("_txt", 0) == 0 and _bono_precio < 12:
                _bono_precio = _bono_precio / 2   # precio parecido de un producto sin relación vale la mitad
            puntaje[q["id"]] = puntaje.get(q["id"], 0) + (base if (q["id"], id(_ln)) not in extras else 2) + _bono_precio + 10 * _ln.get("_txt", 0)
            precio_ok_n[q["id"]] = precio_ok_n.get(q["id"], 0) + (1 if p_ok else 0)

        estado = "ok" if precio_ok else "precio_distinto"
        items_out.append({
            "part_number": pn,
            "part_number_cotizacion": mejor_ln["part_number"],  # el número tal cual está en la cotización
            "descripcion_cotizacion": mejor_ln["descripcion"],
            "cantidad_po": po_qty,                        # la cantidad del PO manda
            "cantidad_cotizacion": mejor_ln["quantity"],
            "precio_po": po_precio,
            "precio_cotizacion": mejor_ln["unit_price"],
            "estado": estado,
            "match_parcial": parcial,
            "tipo_match": tipo_match,
            "cotizacion": {"id": mejor_q["id"], "nombre": mejor_q["nombre"]},
            "en_varias": len({q["id"] for q, _ in candidatos}) > 1,
        })
        if tipo_match == "descripcion":
            discrepancias.append(
                f"«{pn}» parece ser el código interno del cliente; el número real «{mejor_ln['part_number']}» "
                f"aparece en la descripción (cot. {mejor_q['nombre'][:30]}) — confirma que es el mismo producto."
            )
        elif tipo_match in ("similar", "precio"):
            motivo = "descripción parecida" if tipo_match == "similar" else "mismo precio"
            discrepancias.append(
                f"«{pn}» ({it.get('descripcion', '')[:40]}) no tiene número de parte en la cotización; "
                f"la coincidencia es solo por {motivo} con «{(mejor_ln['descripcion'] or mejor_ln['part_number'])[:40]}» "
                f"(cot. {mejor_q['nombre'][:30]}) — confirma que es la misma cotización."
            )
        elif tipo_match == "parcial":
            discrepancias.append(
                f"«{pn}»: coincidencia PARCIAL de número de parte con «{mejor_ln['part_number']}» "
                f"(cot. {mejor_q['nombre'][:30]}) — verifica que sea el mismo producto."
            )
        if estado == "precio_distinto":
            discrepancias.append(
                f"«{pn}»: PO ${po_precio} vs cotización ${mejor_ln['unit_price']} "
                f"(cot. {mejor_q['nombre'][:30]})."
            )

    # cotizaciones candidatas: las que cubren ≥1 item (+ la referenciada aunque cubra 0),
    # ordenadas: referenciada primero, luego por cobertura y vigencia.
    candidatas = []
    for q in quotes:
        cov = cobertura.get(q["id"], 0)
        es_ref = (q["id"] == ref_id)
        if cov <= 0 and not es_ref:
            continue
        vig = _vigencia(q["id"])
        candidatas.append({
            "id": q["id"], "nombre": q["nombre"],
            "items_cubiertos": cov, "total_items_po": len(po.get("items", [])),
            "cerrada": bool(q.get("cerrada")),
            "items_precio_ok": precio_ok_n.get(q["id"], 0), "puntaje": puntaje.get(q["id"], 0),
            "coincidencia": _etiqueta_coincidencia(puntaje.get(q["id"], 0), len(po.get("items", []))),
            "referenciada": es_ref,
            **vig,
            "url": f"{CRM_BASE}/index.php?module=Quotes&action=DetailView&record={q['id']}",
        })
        if not vig["vigente"]:
            discrepancias.append(f"La cotización «{q['nombre'][:30]}» está {vig['motivo']}.")
    candidatas.sort(key=lambda c: (c["referenciada"], c["puntaje"], c["items_cubiertos"], c["vigente"]), reverse=True)

    encontrados = sum(1 for i in items_out if i["estado"] != "no_encontrado")
    todo_ok = (encontrados == len(items_out) and len(items_out) > 0
               and all(i["estado"] == "ok" for i in items_out)
               and not any(i.get("match_parcial") for i in items_out)
               and any(c["vigente"] for c in candidatas))

    # Aviso duro si la orden parece ser para OTRO proveedor.
    if para_nosotros is False:
        discrepancias.insert(0, f"⚠ La orden va dirigida a «{po.get('proveedor','otro')}», no a nosotros. "
                                f"CONFIRMA que esta orden de compra es para nosotros antes de crear la Sales Order.")

    # AMBIGÜEDAD: ninguna cotización fue citada en el PO y hay MÁS de una candidata con cobertura
    # real, sin que el match sea exacto (todo_ok) — pedido explícito de Gabriel: si solo hay UNA
    # coincidencia clara (precio y/o producto exactos) se asume esa; si hay varias sin ganador
    # claro, hay que PREGUNTAR en vez de adivinar con candidatas[0]. No se arma so_draft todavía —
    # el usuario elige primero (ver cotejar(..., forzar_quote_id=...) para la segunda vuelta).
    candidata_citada = any(c["referenciada"] for c in candidatas)
    con_cobertura = [c for c in candidatas if c["items_cubiertos"] > 0]
    ambiguo = (not candidata_citada) and len(con_cobertura) >= 1 and not todo_ok

    # DRAFT de la Sales Order (para el PREVIO). Referencia = candidata top (citada o mayor cobertura).
    so_draft = None if ambiguo else _armar_draft(cuenta, tm, po, items_out, candidatas, quotes, para_nosotros)
    if so_draft and forzar_quote_id:
        # El usuario eligió esta cotización a mano: una cotización vencida/draft solo ADVIERTE, no bloquea
        # (él decide; el previo y la confirmación siguen siendo obligatorios).
        so_draft["puede_crear"] = para_nosotros is not False
        so_draft["aviso_vigencia"] = bool(so_draft.get("quote_vigente") is False)

    return {
        "ok": True,
        "cuenta": cuenta,
        "po_number": po.get("po_number", ""),
        "proveedor": po.get("proveedor", ""),
        "para_nosotros": para_nosotros,
        "ambiguo": ambiguo,
        "forzada": bool(forzar_quote_id),
        "ventas_previas": ventas_previas(cuenta["id"], po),
        "so_draft": so_draft,               # True / False / None (no lo dice)
        "terminos_pago": tm["terminos_pago"],         # default_terms del cliente
        "moneda": po.get("moneda", "") or tm["moneda"],
        "currency_id": tm["currency_id"],
        "items": items_out,
        "cotizaciones_candidatas": candidatas,
        "discrepancias": discrepancias,
        "todo_ok": todo_ok and para_nosotros is not False,
        "resumen": (f"{encontrados}/{len(items_out)} productos ubicados en cotizaciones; "
                    f"{len(candidatas)} cotización(es) candidata(s); "
                    f"{len(discrepancias)} discrepancia(s)."
                    + (" Ninguna cotización coincide exacto (parte y precio) — elige cuál es, ordenadas de mayor a menor coincidencia." if ambiguo else "")),
    }


# ─────────────────────────────────────────────────────────────
# 4. ESCRITURA (Fase 2) — crear la Sales Order convirtiendo la cotización
# ─────────────────────────────────────────────────────────────
def crear_sales_order(draft: dict) -> dict:
    """Crea la Sales Order a partir del draft aprobado. NO se llama sin aprobación del usuario.

    draft = {
      cuenta_id, currency_id, terms, po_number, quote_id,
      lineas: [{part_number, cantidad, incluir}]   # incluir=False → se quita de la SO
    }

    Mecánica (verificada en 1CRM): al crear la SO con related_quote_id, 1CRM AUTO-COPIA las líneas
    de la cotización y la marca 'Closed Accepted'. Luego ajustamos cantidades al PO (PATCH) y
    quitamos las líneas que el usuario no seleccionó (DELETE). Devuelve {ok, so_id, url, ...}.
    """
    if not CRM_BASE:
        return {"error": "1CRM no configurado"}
    cuenta_id = draft.get("cuenta_id")
    quote_id  = draft.get("quote_id")
    if not cuenta_id or not quote_id:
        return {"error": "faltan cuenta_id o quote_id para crear la Sales Order"}

    # 1) Crear la SO ligada a la cotización → auto-copia líneas + auto-acepta la cotización.
    payload = {
        "billing_account_id": cuenta_id,
        "related_quote_id":   quote_id,
        "so_stage":           "In Manufacturing",   # al crear la SO entra en preparación/fabricación
        "purchase_order_num": draft.get("po_number", "") or "",
    }
    if draft.get("currency_id"):
        payload["currency_id"] = draft["currency_id"]
    if draft.get("terms"):
        payload["terms"] = draft["terms"]
    r = _crm_post("SalesOrder", payload)
    so_id = r.get("id")
    if not so_id:
        return {"error": f"no se pudo crear la Sales Order: {r}"}

    # 2) Ajustar líneas: mapear por número de parte (compacto). PATCH cantidad, DELETE no-seleccionadas.
    sel = {}
    for ln in draft.get("lineas", []):
        pc = _clave(ln.get("part_number", ""))
        if pc:
            sel[pc] = ln
    full = _crm_get(f"data/SalesOrder/{so_id}")
    lineas_so = (full.get("record", full).get("line_items") or [])
    ajustadas, quitadas = 0, 0
    for li in lineas_so:
        pc = _clave(li.get("mfr_part_no", ""), li.get("name", ""))
        d = sel.get(pc)
        if d is None or d.get("incluir") is False:
            _crm_delete("SalesOrderLine", li["id"]); quitadas += 1
            continue
        qty = d.get("cantidad")
        patch: dict = {}
        if qty is not None and _num(li.get("quantity")) != _num(qty):
            patch["quantity"] = qty
        # Correcciones del usuario en el previo (precio / descripción / nº de parte) — cuando el PO
        # no coincide exacto con la cotización elegida, el usuario aclara los datos finales.
        precio = _num(d.get("precio"))
        if precio is not None and precio != _num(li.get("unit_price")):
            patch["unit_price"] = f"{precio:.2f}"
        if d.get("descripcion") and d["descripcion"] != li.get("name"):
            patch["name"] = d["descripcion"]
        if d.get("mfr_part_no") is not None and d["mfr_part_no"] != (li.get("mfr_part_no") or ""):
            patch["mfr_part_no"] = d["mfr_part_no"]
        if patch:
            q_final = _num(patch.get("quantity", li.get("quantity"))) or 0
            u_final = _num(patch.get("unit_price", li.get("unit_price"))) or 0
            if "quantity" in patch or "unit_price" in patch:
                patch["ext_price"] = f"{q_final * u_final:.2f}"  # la API no recalcula la línea sola
            _crm_patch("SalesOrderLine", li["id"], patch); ajustadas += 1

    # 2b) La API no recalcula los totales de la SO al editar líneas: se recalculan (subtotal + IVA
    # por línea) y se parchan amount/subtotal (verificado en vivo que son editables).
    if ajustadas or quitadas:
        fresh = (_crm_get(f"data/SalesOrder/{so_id}").get("record", {}).get("line_items") or [])
        sub = sum(_num(l.get("ext_price")) or 0 for l in fresh)
        iva = sum((_num(l.get("ext_price")) or 0) * ((_num(l.get("line_tax_perc")) or 0) / 100) for l in fresh)
        _crm_patch("SalesOrder", so_id, {"subtotal": f"{sub:.2f}", "amount": f"{sub + iva:.2f}"})

    # 3) Reafirmar cotización aceptada (normalmente ya quedó 'Closed Accepted' sola; si no, enforce).
    q = _crm_get(f"data/Quote/{quote_id}").get("record", {})
    if (q.get("quote_stage") or "") != "Closed Accepted":
        _crm_patch("Quote", quote_id, {"quote_stage": "Closed Accepted"})
    cotizacion_ya_aceptada = (q.get("quote_stage") == "Closed Accepted")

    fin = _crm_get(f"data/SalesOrder/{so_id}").get("record", {})
    return {
        "ok": True,
        "so_id": so_id,
        "so_numero": fin.get("so_number"),
        "nombre": fin.get("name", ""),
        "lineas_finales": len(fin.get("line_items") or []),
        "ajustadas": ajustadas,
        "quitadas": quitadas,
        "cotizacion_ya_estaba_aceptada": cotizacion_ya_aceptada,
        "url": f"{CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={so_id}",
    }


# ─────────────────────────────────────────────────────────────
# 5. SUBIR EL PO ORIGINAL A LA SALES ORDER (sección Documentos/Notas)
# ─────────────────────────────────────────────────────────────
def subir_po_a_so(so_id: str, file_url: str, po_number: str = "") -> dict:
    """Sube el archivo original del PO como Nota/Archivo adjunto de la Sales Order recién creada —
    pedido explícito de Gabriel: el documento debe quedar guardado y ligado a la SO, no solo leído
    desde donde se subió.

    1CRM Cloud no acepta subir archivos vía la API REST (el campo 'filename' de Note es tipo
    'file_ref', igual que en Documents — bloquea upload.php a clientes HTTP externos, mismo patrón
    ya usado en agente_publicador.subir_imagen_a_crm). Único mecanismo que funciona: un browser real
    con sesión (Playwright) — login → abrir la SO → click 'Nueva Nota o Archivo Adjunto' (ya trae
    'Relativo a' pre-rellenado con ESTA SalesOrder, confirmado en vivo) → nombre + archivo → guardar.

    Verificado en vivo (create+GET+delete controlado): la Nota queda con parent_type='SalesOrders',
    parent_id=<so_id> y filename con el archivo real — sin esto, probar a ciegas: el listado por
    filter_text NO trae esos campos (hay que leer el detalle), que es como se confirmó.

    Fallback silencioso: si Playwright falla por cualquier motivo, NO bloquea la creación de la SO
    (que ya quedó hecha) — solo se loguea el error."""
    if not file_url or not so_id:
        return {"ok": False, "error": "falta file_url o so_id"}
    import tempfile
    import pathlib
    from urllib.parse import urlparse

    crm_url = CRM_BASE
    crm_user = os.environ.get("ONECRM_USERNAME", "")
    crm_pass = os.environ.get("ONECRM_PASSWORD", "")
    nombre_nota = f"PO {po_number}".strip() if po_number else "Orden de compra"

    # Descargar el PO a un archivo temporal con un nombre limpio (el que ve el usuario en 1CRM).
    try:
        r = httpx.get(file_url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        ext = pathlib.Path(urlparse(file_url).path).suffix or ".pdf"
        safe = (po_number or "orden_compra").replace("/", "-").replace(" ", "_")
        tmp_path = str(pathlib.Path(tempfile.gettempdir()) / f"PO_{safe}{ext}")
        pathlib.Path(tmp_path).write_bytes(r.content)
    except Exception as e:
        log.warning(f"No se pudo descargar el PO para subirlo a 1CRM: {e}")
        return {"ok": False, "error": str(e)}

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_context(ignore_https_errors=True, viewport={"width": 1400, "height": 1000}).new_page()

            page.goto(f"{crm_url}/index.php?module=Users&action=Login", timeout=30000)
            page.fill('input[name="user_name"]', crm_user)
            page.fill('input[name="user_password"]', crm_pass)
            page.click('input[type="submit"], button[type="submit"]')
            page.wait_for_url(f"{crm_url}/index.php*", timeout=20000)

            page.goto(f"{crm_url}/index.php?module=SalesOrders&action=DetailView&record={so_id}", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=30000)

            page.locator("text=Nueva Nota o Archivo Adjunto").first.click(timeout=10000)
            page.wait_for_timeout(1000)
            page.fill('input[name="name"]', nombre_nota)
            page.set_input_files('input[name="filename"]', tmp_path)
            page.wait_for_timeout(500)
            page.click("#QuickCreateForm_0_save")
            page.wait_for_timeout(3000)  # upload.php + async.php de guardado, sin respuesta fácil de interceptar

            browser.close()
        log.info(f"PO subido a la SO {so_id} como Nota '{nombre_nota}'")
        return {"ok": True}
    except Exception as e:
        log.warning(f"No se pudo subir el PO a 1CRM (SO ya quedó creada, esto no la afecta): {e}")
        return {"ok": False, "error": str(e)}
