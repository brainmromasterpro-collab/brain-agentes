"""
COMPRA A PROVEEDOR — stream "compras", tarea `compra_proveedor`
=================================================================
El cliente sube uno o varios links de producto (eBay, etc.) al chat de compras. Este módulo:

  1. COTEJA (no escribe): busca en qué Sales Order(s) abiertas aparece cada producto —
     "si vamos a comprar algo es porque ya se lo vendimos a alguien". Agrupa los links por la
     Sales Order que machearon (una compra puede cubrir productos de varias ventas a la vez).
  2. ESCRIBE (solo tras aprobación, un grupo a la vez): crea el PurchaseOrder ligado a la Sales
     Order (`from_so_id`) y la Cuenta por Pagar (`Bill.related_purchase_order_id`) ligada al PO.

Mecánica de escritura 1CRM — VERIFICADA en vivo con prueba controlada create+delete:
  - PurchaseOrder: requiere `currency_id` y `amount` explícitos (sin default en BD). El array
    `line_items` en el POST de creación se IGNORA — las líneas se crean aparte:
    primero un `PurchaseOrderLineGroup` (`parent_id`=po_id), luego cada `PurchaseOrderLine`
    (`purchase_orders_id`=po_id, `line_group_id`=group_id).
  - Bill: requiere `currency_id`, `amount`, `bill_date`, `due_date`. A diferencia del PO, SÍ
    acepta `line_items` inline en el POST de creación (crea su propio BillLineGroup solo).
  - `from_so_id` (PurchaseOrder) y `related_purchase_order_id` (Bill) SÍ se guardan al mandarlos
    en el POST, pese a venir `editable:false` en la metadata — ese flag es solo convención de
    formularios UI, no bloquea la API (mismo hallazgo que `related_quote_id` en sales_order.py).
  - No existe un estado "ya comprado" en SalesOrder: se representa por la EXISTENCIA de un
    PurchaseOrder con `from_so_id` apuntando a esa SO (relación como fuente de verdad).

Regla de oro (igual que en sales_order.py): NO inventar. Si un link no machea ninguna Sales
Order, se reporta aparte (candidato a "gasto general"), nunca se fuerza un match dudoso.
"""

import os
import re
import logging
import datetime
from urllib.parse import urlparse

import sales_order  # reusa _crm_get/_crm_post/_crm_delete, _compact, _norm, _num, _match_item

log = logging.getLogger("compra_proveedor")

CRM_BASE = sales_order.CRM_BASE

# SO stages que ya no cuentan como "abiertas" para buscar candidatas de compra.
SO_STAGE_CERRADAS = {"Closed - Shipped and Invoiced"}

# Mapeo de dominios conocidos a nombre de proveedor "para nosotros" (a quién se le paga).
_DOMINIOS_PROVEEDOR = {
    "ebay.com": "eBay", "ebay.com.mx": "eBay",
    "amazon.com": "Amazon", "amazon.com.mx": "Amazon",
    "mercadolibre.com": "Mercado Libre", "mercadolibre.com.mx": "Mercado Libre",
    "aliexpress.com": "AliExpress",
}


def _proveedor_de_url(url: str) -> str:
    """Nombre del proveedor/vendor (a quién se le paga) a partir del dominio del link.
    OJO: esto NO es la marca/fabricante del producto (ese es otro dato, `marca`)."""
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""
    if host in _DOMINIOS_PROVEEDOR:
        return _DOMINIOS_PROVEEDOR[host]
    partes = host.split(".")
    base = partes[-2] if len(partes) >= 2 else host
    return base.capitalize()


# ─────────────────────────────────────────────────────────────
# 1. SALES ORDERS ABIERTAS (índice de líneas, mismo patrón que sales_order.cotizaciones_cliente)
# ─────────────────────────────────────────────────────────────
def sales_orders_abiertas(limite: int = 150) -> list[dict]:
    """Índice de líneas de Sales Orders recientes, para el match por part number.

    OJO: el endpoint de LISTA de 1CRM viene "delgado" — trae `line_items` completos pero NO
    `so_stage`/`billing_account`/`currency_id` a nivel del registro (mismo hallazgo ya documentado
    para Quote en sales_order._vigencia). Por eso aquí NO se filtra por stage ni se arma `cliente`/
    `currency_id` — eso se resuelve con un GET de detalle SOLO de los candidatos que ya matchearon
    (ver `_so_detalle`), no de los 150."""
    data = sales_order._crm_get("data/SalesOrder", {
        "order_by": "date_modified desc",
        "limit": limite,
    })
    out: list[dict] = []
    for so in data.get("records", []):
        lines = []
        for li in (so.get("line_items") or []):
            pn = li.get("mfr_part_no") or ""
            lines.append({
                "part_number": pn,
                "part_compact": sales_order._compact(pn),
                "unit_price": sales_order._num(li.get("unit_price")),
                "quantity": sales_order._num(li.get("quantity")),
                "descripcion": li.get("name", ""),
            })
        out.append({"id": so.get("id"), "nombre": so.get("name", ""), "lines": lines})
    return out


def _so_detalle(so_id: str) -> dict:
    """Detalle completo de UNA Sales Order candidata (so_stage, cliente, currency_id, so_number)
    — se llama solo para las pocas que ya matchearon, no para las 150 del índice.
    `billing_account` viene vacío en la API (verificado en vivo) — el nombre del cliente hay que
    resolverlo aparte con `billing_account_id` (reusa sales_order.cuenta_por_id)."""
    d = sales_order._crm_get(f"data/SalesOrder/{so_id}")
    rec = d.get("record", d)
    cuenta = sales_order.cuenta_por_id(rec.get("billing_account_id", ""))
    return {
        "so_stage": rec.get("so_stage", ""),
        "so_number": rec.get("so_number"),
        "cliente": (cuenta or {}).get("nombre", ""),
        "currency_id": rec.get("currency_id") or "",
    }


def _mapa_ya_comprado(limite: int = 30) -> dict:
    """Mapa {so_id: {id, nombre, url}} de PurchaseOrders recientes que ya están ligados a una
    Sales Order, para marcar "ya comprado". LIMITACIÓN CONOCIDA: `filters[from_so_id]` de la API
    de 1CRM NO filtra de verdad (verificado en vivo — devuelve el mismo set sin importar el valor,
    probablemente porque el campo viene `reportable:false` en su metadata). Y la lista de
    PurchaseOrder viene "delgada" (sin from_so_id) — por eso se pide detalle de los N más
    recientes UNA sola vez por corrida, no por candidato. Solo cubre compras recientes; una compra
    vieja para esa SO podría no aparecer aquí — está bien, es un cross-check adicional, no la
    única fuente de verdad (el usuario igual ve el link/producto y puede confirmar si ya compró)."""
    data = sales_order._crm_get("data/PurchaseOrder", {"order_by": "date_modified desc", "limit": limite})
    mapa: dict = {}
    for po in data.get("records", []):
        po_id = po.get("id")
        if not po_id:
            continue
        d = sales_order._crm_get(f"data/PurchaseOrder/{po_id}")
        rec = d.get("record", d)
        so_id = rec.get("from_so_id")
        if so_id and so_id not in mapa:
            mapa[so_id] = {"id": po_id, "nombre": rec.get("name", ""),
                           "url": f"{CRM_BASE}/index.php?module=PurchaseOrders&record={po_id}"}
    return mapa


# ─────────────────────────────────────────────────────────────
# 2. COTEJO — agrupa los links por la Sales Order que machearon
# ─────────────────────────────────────────────────────────────
def buscar_sales_orders_candidatas(links_data: list[dict]) -> dict:
    """Recibe una lista de productos ya leídos (ver _extraer_producto_link en agente_chat.py):
    [{ok, url, nombre, marca, part_number, precio_costo, moneda, descripcion, imagen_url}, ...]

    Devuelve {ok, grupos: [{so, ya_comprado, links: [...]}], sin_match: [...], resumen}.
    NO escribe nada al CRM."""
    if not CRM_BASE:
        return {"error": "1CRM no configurado"}

    validos = [l for l in (links_data or []) if l and l.get("ok")]
    if not validos:
        return {"error": "no se pudo leer ningún producto de los links dados"}

    sos = sales_orders_abiertas()
    indice: dict[str, list[tuple[dict, dict]]] = {}
    for so in sos:
        for ln in so["lines"]:
            if ln["part_compact"]:
                indice.setdefault(ln["part_compact"], []).append((so, ln))

    grupos: dict[str, dict] = {}   # so_id -> {so, links: [...]}
    sin_match: list[dict] = []

    for link in validos:
        pn = link.get("part_number", "")
        pc = sales_order._compact(pn)
        texto_busqueda = f"{link.get('nombre','')} {link.get('descripcion','')}"
        candidatos, tipo_match = sales_order._match_item(pc, texto_busqueda, indice)

        if not candidatos:
            sin_match.append({**link, "motivo": "ningún producto de una Sales Order abierta coincide"})
            continue

        # Elegir el mejor candidato: 1 por SO (la primera línea que machee de esa SO).
        vistos_so: set = set()
        for so, ln in candidatos:
            if so["id"] in vistos_so:
                continue
            vistos_so.add(so["id"])
            g = grupos.setdefault(so["id"], {"so": so, "links": []})
            g["links"].append({
                **link,
                "part_number_so": ln["part_number"],
                "descripcion_so": ln["descripcion"],
                "tipo_match": tipo_match,
                "match_parcial": tipo_match in ("parcial", "descripcion"),
            })

    mapa_comprado = _mapa_ya_comprado()
    grupos_out = []
    for so_id, g in grupos.items():
        detalle = _so_detalle(so_id)
        if detalle["so_stage"] in SO_STAGE_CERRADAS:
            continue  # la SO ya se cerró (entregada y facturada) — no tiene caso comprar para ella
        yc = mapa_comprado.get(so_id)
        links = g["links"]
        # Proveedor sugerido = dominio del primer link (normalmente todos vienen del mismo
        # marketplace en una sola compra); el usuario lo puede corregir en el widget.
        proveedor_sugerido = _proveedor_de_url(links[0]["url"]) if links else ""
        lineas_sugeridas = [{
            "name": l.get("nombre") or l.get("descripcion_so") or "Producto",
            "mfr_part_no": l.get("part_number") or l.get("part_number_so") or "",
            "quantity": 1,
            "unit_price": sales_order._num(l.get("precio_costo")) or 0,
        } for l in links]
        grupos_out.append({
            "so_id": so_id,
            "so_nombre": g["so"]["nombre"],
            "so_numero": detalle["so_number"],
            "so_url": f"{CRM_BASE}/index.php?module=SalesOrders&record={so_id}",
            "cliente": detalle["cliente"],
            "currency_id": detalle["currency_id"],
            "ya_comprado": yc,
            "proveedor_nombre": proveedor_sugerido,
            "lineas": lineas_sugeridas,
            "links": links,
        })
    # Orden: los que no tienen compra previa primero (los que sí, se muestran pero al final).
    grupos_out.sort(key=lambda x: x["ya_comprado"] is not None)

    return {
        "ok": True,
        "grupos": grupos_out,
        "sin_match": sin_match,
        "resumen": f"{len(validos)} producto(s) leído(s); {len(grupos_out)} Sales Order(s) candidata(s); "
                   f"{len(sin_match)} sin match (candidato a gasto general).",
    }


# ─────────────────────────────────────────────────────────────
# 3. ESCRITURA — crear el PurchaseOrder + Bill de UN grupo (tras aprobación)
# ─────────────────────────────────────────────────────────────
def _crear_lineas_po(po_id: str, currency_id: str, lineas: list[dict]) -> int:
    """PurchaseOrder no auto-crea líneas desde el POST de creación (verificado en vivo):
    hace falta un PurchaseOrderLineGroup y luego cada PurchaseOrderLine apuntando a él."""
    grp = sales_order._crm_post("PurchaseOrderLineGroup", {"parent_id": po_id})
    grp_id = grp.get("id")
    if not grp_id:
        log.error(f"No se pudo crear PurchaseOrderLineGroup para PO {po_id}: {grp}")
        return 0
    creadas = 0
    for ln in lineas:
        payload = {
            "purchase_orders_id": po_id,
            "line_group_id": grp_id,
            "name": ln.get("name") or ln.get("nombre") or "Producto",
            "mfr_part_no": ln.get("mfr_part_no") or ln.get("part_number") or "",
            "quantity": ln.get("quantity", 1),
            "unit_price": ln.get("unit_price") or ln.get("precio_costo") or 0,
        }
        r = sales_order._crm_post("PurchaseOrderLine", payload)
        if r.get("id"):
            creadas += 1
        else:
            log.error(f"No se pudo crear PurchaseOrderLine en PO {po_id}: {r}")
    return creadas


def _buscar_o_crear_proveedor(nombre: str) -> str | None:
    """Busca una Account tipo Supplier por nombre; si no existe, la da de alta (mismo patrón
    de alta de cliente del MODO 12, pero con account_type=Supplier)."""
    nombre = (nombre or "").strip()
    if not nombre:
        return None
    data = sales_order._crm_get("data/Account", {
        "filters[account_type]": "Supplier", "filter_text": nombre, "limit": 10,
    })
    n = sales_order._norm(nombre)
    for r in data.get("records", []):
        if sales_order._norm(r.get("name", "")) == n:
            return r.get("id")
    r = sales_order._crm_post("Account", {"name": nombre, "account_type": "Supplier"})
    return r.get("id")


def crear_po_y_ap(draft: dict) -> dict:
    """Crea el PurchaseOrder (ligado a la Sales Order si `so_id` viene en el draft) y la Bill
    (cuenta por pagar) ligada al PO. Se llama UNA VEZ POR GRUPO ya aprobado por el usuario.

    draft = {
      so_id: str | None,      # None => tarea gasto_general (compra sin venta asociada)
      proveedor_nombre: str,  # ej. "eBay" — a quién se le paga, no la marca del producto
      currency_id: str,
      lineas: [{name, mfr_part_no, quantity, unit_price}],
    }
    """
    if not CRM_BASE:
        return {"error": "1CRM no configurado"}
    lineas = draft.get("lineas") or []
    if not lineas:
        return {"error": "faltan líneas para crear la orden de compra"}

    supplier_id = _buscar_o_crear_proveedor(draft.get("proveedor_nombre", ""))
    if not supplier_id:
        return {"error": "no se pudo resolver ni dar de alta el proveedor"}

    currency_id = draft.get("currency_id") or ""
    total = sum(float(ln.get("unit_price") or 0) * float(ln.get("quantity") or 1) for ln in lineas)
    hoy = datetime.date.today().isoformat()
    so_id = draft.get("so_id") or ""

    po_payload = {
        "name": draft.get("nombre") or (f"Compra {draft['proveedor_nombre']}"),
        "supplier_id": supplier_id,
        "shipping_stage": "Ordered",
        "currency_id": currency_id,
        "amount": total,
    }
    if so_id:
        po_payload["from_so_id"] = so_id
    r = sales_order._crm_post("PurchaseOrder", po_payload)
    po_id = r.get("id")
    if not po_id:
        return {"error": f"no se pudo crear el Purchase Order: {r}"}

    _crear_lineas_po(po_id, currency_id, lineas)

    bill_payload = {
        "name": f"AP — {draft.get('proveedor_nombre','')} — {draft.get('nombre','')}".strip(" —"),
        "supplier_id": supplier_id,
        "related_purchase_order_id": po_id,
        "currency_id": currency_id,
        "amount": total,
        "bill_date": hoy,
        "due_date": hoy,
        "line_items": [{
            "name": ln.get("name") or ln.get("nombre") or "Producto",
            "quantity": ln.get("quantity", 1),
            "unit_price": ln.get("unit_price") or ln.get("precio_costo") or 0,
        } for ln in lineas],
    }
    rb = sales_order._crm_post("Bill", bill_payload)
    bill_id = rb.get("id")
    if not bill_id:
        return {"error": f"Purchase Order creado ({po_id}) pero no se pudo crear la cuenta por pagar: {rb}",
                "po_id": po_id}

    return {
        "ok": True,
        "po_id": po_id,
        "po_url": f"{CRM_BASE}/index.php?module=PurchaseOrders&record={po_id}",
        "bill_id": bill_id,
        "bill_url": f"{CRM_BASE}/index.php?module=Bills&record={bill_id}",
        "so_id": so_id or None,
        "so_url": f"{CRM_BASE}/index.php?module=SalesOrders&record={so_id}" if so_id else None,
        "proveedor": draft.get("proveedor_nombre", ""),
        "total": total,
    }
