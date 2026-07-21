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


def _crm_get(path: str, params: dict | None = None) -> dict:
    r = httpx.get(f"{CRM_BASE}/api.php/{path}", params=params or {}, auth=CRM_AUTH, timeout=40)
    try:
        return r.json()
    except Exception:
        return {}


def _norm(s: str) -> str:
    """Normaliza para comparar nombres/partes: mayúsculas, espacios colapsados."""
    return re.sub(r"\s+", " ", (s or "").strip()).upper()


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

    cuenta = buscar_cuenta(po.get("cliente", ""))
    if not cuenta:
        return {
            "ok": False,
            "avisos": [f"No encontré en el CRM la cuenta del cliente «{po.get('cliente','?')}». "
                       f"Verifica el nombre o si el cliente ya existe."],
            "cuenta": None, "items": [], "cotizaciones_candidatas": [],
        }

    quotes = cotizaciones_cliente(cuenta["id"])
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
        candidatos = indice.get(pc, [])

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
            "cantidad_po": po_qty,                        # la cantidad del PO manda
            "cantidad_cotizacion": mejor_ln["quantity"],
            "precio_po": po_precio,
            "precio_cotizacion": mejor_ln["unit_price"],
            "estado": estado,
            "cotizacion": {"id": mejor_q["id"], "nombre": mejor_q["nombre"]},
            "en_varias": len({q["id"] for q, _ in candidatos}) > 1,
        })
        if estado == "precio_distinto":
            discrepancias.append(
                f"«{pn}»: PO ${po_precio} vs cotización ${mejor_ln['unit_price']} "
                f"(cot. {mejor_q['nombre'][:30]})."
            )

    # cotizaciones candidatas: las que cubren ≥1 item, ordenadas por cobertura desc
    candidatas = []
    for q in quotes:
        cov = cobertura.get(q["id"], 0)
        if cov <= 0:
            continue
        vig = _vigencia(q["id"])
        candidatas.append({
            "id": q["id"], "nombre": q["nombre"],
            "items_cubiertos": cov, "total_items_po": len(po.get("items", [])),
            **vig,
            "url": f"{CRM_BASE}/index.php?module=Quotes&record={q['id']}",
        })
        if not vig["vigente"]:
            discrepancias.append(f"La cotización «{q['nombre'][:30]}» está {vig['motivo']}.")
    candidatas.sort(key=lambda c: (c["items_cubiertos"], c["vigente"]), reverse=True)

    encontrados = sum(1 for i in items_out if i["estado"] != "no_encontrado")
    todo_ok = (encontrados == len(items_out) and len(items_out) > 0
               and all(i["estado"] == "ok" for i in items_out)
               and any(c["vigente"] for c in candidatas))

    return {
        "ok": True,
        "cuenta": cuenta,
        "po_number": po.get("po_number", ""),
        "moneda": po.get("moneda", ""),
        "items": items_out,
        "cotizaciones_candidatas": candidatas,
        "discrepancias": discrepancias,
        "todo_ok": todo_ok,
        "resumen": f"{encontrados}/{len(items_out)} productos ubicados en cotizaciones; "
                   f"{len(candidatas)} cotización(es) candidata(s); "
                   f"{len(discrepancias)} discrepancia(s).",
    }
