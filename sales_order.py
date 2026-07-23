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


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def _norm(s: str) -> str:
    """Normaliza para comparar nombres/partes: sin acentos, mayúsculas, espacios colapsados
    (así 'México' == 'Mexico')."""
    return re.sub(r"\s+", " ", _sin_acentos(s).strip()).upper()


def _compact(s: str) -> str:
    """Versión sin separadores para tolerar '48625 RO-222' vs '48625RO222'."""
    return re.sub(r"[^A-Z0-9]", "", _norm(s))


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 1. CUENTA
# ─────────────────────────────────────────────────────────────
def buscar_cuenta(nombre: str) -> dict | None:
    """Encuentra la cuenta del cliente por nombre. Devuelve {id, nombre, url} o None."""
    nombre = (nombre or "").strip()
    if not nombre or not CRM_BASE:
        return None
    data = _crm_get("data/Account", {"filter_text": nombre, "limit": 20})
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
    if not mejor:
        return None
    return {
        "id":     mejor.get("id"),
        "nombre": mejor.get("name", ""),
        "url":    f"{CRM_BASE}/index.php?module=Accounts&record={mejor.get('id')}",
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
            "url": f"{CRM_BASE}/index.php?module=Accounts&record={rec['id']}"}


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
def cotizaciones_cliente(cuenta_id: str, limite: int = 40) -> list[dict]:
    """Cotizaciones recientes del cliente CON sus líneas. Cada una:
    {id, nombre, lines:[{part_number, part_compact, unit_price, quantity, descripcion}]}."""
    data = _crm_get("data/Quote", {
        "filters[billing_account_id]": cuenta_id,
        "order_by": "date_modified desc",
        "limit": limite,
    })
    out: list[dict] = []
    for q in data.get("records", []):
        lines = []
        for li in (q.get("line_items") or []):
            pn = li.get("mfr_part_no") or ""
            lines.append({
                "part_number": pn,
                "part_compact": _compact(pn),
                "unit_price": _num(li.get("unit_price")),
                "quantity":   _num(li.get("quantity")),
                "descripcion": li.get("name", ""),
            })
        out.append({"id": q.get("id"), "nombre": q.get("name", ""), "lines": lines})
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
            "part_number": pn, "part_compact": _compact(pn),
            "unit_price": _num(li.get("unit_price")), "quantity": _num(li.get("quantity")),
            "descripcion": li.get("name", ""),
        })
    return {"id": qid, "nombre": rec.get("name", ""), "lines": lines, "referenciada": True,
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


def _precio_coincide(po_precio: float | None, cot_precio: float | None) -> bool:
    if po_precio is None or cot_precio is None:
        return True  # sin precio en el PO → no se marca discrepancia de precio
    return abs(po_precio - cot_precio) <= max(TOL_ABS, TOL_REL * cot_precio)


def cotejar(po: dict) -> dict:
    """Cruza el PO contra las cotizaciones del cliente. NO escribe nada.
    Devuelve un diagnóstico completo para armar el previo y elegir la cotización de referencia."""
    if not CRM_BASE:
        return {"error": "1CRM no configurado"}
    if po.get("error"):
        return {"error": f"el PO no se pudo leer: {po['error']}"}

    # Cotización REFERENCIADA en el propio PO (cita/adjunta nuestro presupuesto Q2026-…): mejor pista
    # de origen. La buscamos primero porque además define la cuenta cuando el nombre no matchea.
    ref_q = cotizacion_por_ref(po.get("cotizacion_ref", ""))

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
    discrepancias: list[str] = []

    for it in po.get("items", []):
        pn = it.get("part_number", "")
        pc = _compact(pn)
        po_precio = _num(it.get("precio_unitario"))
        po_qty = _num(it.get("cantidad"))
        candidatos, tipo_match = _match_item(pc, it.get("descripcion", ""), indice)
        parcial = tipo_match in ("parcial", "descripcion")

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
        for (q, _ln) in candidatos:
            cobertura[q["id"]] = cobertura.get(q["id"], 0) + 1

        estado = "ok" if precio_ok else "precio_distinto"
        items_out.append({
            "part_number": pn,
            "part_number_cotizacion": mejor_ln["part_number"],  # el número tal cual está en la cotización
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
            "referenciada": es_ref,
            **vig,
            "url": f"{CRM_BASE}/index.php?module=Quotes&record={q['id']}",
        })
        if not vig["vigente"]:
            discrepancias.append(f"La cotización «{q['nombre'][:30]}» está {vig['motivo']}.")
    candidatas.sort(key=lambda c: (c["referenciada"], c["items_cubiertos"], c["vigente"]), reverse=True)

    encontrados = sum(1 for i in items_out if i["estado"] != "no_encontrado")
    todo_ok = (encontrados == len(items_out) and len(items_out) > 0
               and all(i["estado"] == "ok" for i in items_out)
               and not any(i.get("match_parcial") for i in items_out)
               and any(c["vigente"] for c in candidatas))

    # Aviso duro si la orden parece ser para OTRO proveedor.
    if para_nosotros is False:
        discrepancias.insert(0, f"⚠ La orden va dirigida a «{po.get('proveedor','otro')}», no a nosotros. "
                                f"CONFIRMA que esta orden de compra es para nosotros antes de crear la Sales Order.")

    return {
        "ok": True,
        "cuenta": cuenta,
        "po_number": po.get("po_number", ""),
        "proveedor": po.get("proveedor", ""),
        "para_nosotros": para_nosotros,               # True / False / None (no lo dice)
        "terminos_pago": tm["terminos_pago"],         # default_terms del cliente
        "moneda": po.get("moneda", "") or tm["moneda"],
        "currency_id": tm["currency_id"],
        "items": items_out,
        "cotizaciones_candidatas": candidatas,
        "discrepancias": discrepancias,
        "todo_ok": todo_ok and para_nosotros is not False,
        "resumen": f"{encontrados}/{len(items_out)} productos ubicados en cotizaciones; "
                   f"{len(candidatas)} cotización(es) candidata(s); "
                   f"{len(discrepancias)} discrepancia(s).",
    }
