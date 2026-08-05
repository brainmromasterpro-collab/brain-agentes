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
    """Detalle completo de UNA Sales Order candidata (so_stage, cliente, currency_id, so_number,
    términos de pago del cliente) — se llama solo para las pocas que ya matchearon, no para las
    150 del índice. `billing_account` viene vacío en la API (verificado en vivo) — el nombre del
    cliente hay que resolverlo aparte con `billing_account_id` (reusa sales_order.cuenta_por_id).
    Los términos de pago se piden porque el cliente pidió que el PO herede los términos que
    tenemos con ESE cliente (mismo campo `default_terms` que ya usa sales_order.crear_sales_order
    vía _terminos_y_moneda)."""
    d = sales_order._crm_get(f"data/SalesOrder/{so_id}")
    rec = d.get("record", d)
    billing_account_id = rec.get("billing_account_id", "")
    cuenta = sales_order.cuenta_por_id(billing_account_id)
    tm = sales_order._terminos_y_moneda(billing_account_id) if billing_account_id else {}
    return {
        "so_stage": rec.get("so_stage", ""),
        "so_number": rec.get("so_number"),
        "cliente": (cuenta or {}).get("nombre", ""),
        "currency_id": rec.get("currency_id") or "",
        "terminos_pago": tm.get("terminos_pago", ""),
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
                           "url": f"{CRM_BASE}/index.php?module=PurchaseOrders&action=DetailView&record={po_id}"}
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

        # Respaldo para códigos CORTOS (<6 chars, ej. "NE555"): sales_order._match_item exige
        # min 6 chars en el escaneo por descripción para evitar falsos positivos, lo cual excluye
        # códigos cortos válidos. Aquí se permite desde 4 chars pero exigiendo coincidencia de
        # PALABRA COMPLETA (no substring suelto) en el título/descripción del link — frecuente en
        # anuncios de dropshipping donde el part_number no viene limpio en ningún campo separado.
        if not candidatos:
            texto_norm = re.sub(r'[^A-Z0-9\s]', ' ', texto_busqueda.upper())
            palabras = set(texto_norm.split())
            for k, v in indice.items():
                if 4 <= len(k) < 6 and k in palabras:
                    candidatos.extend(v)
                    tipo_match = "descripcion"

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
            "so_url": f"{CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={so_id}",
            "cliente": detalle["cliente"],
            "currency_id": detalle["currency_id"],
            "terminos_pago": detalle["terminos_pago"],
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
    hace falta un PurchaseOrderLineGroup y luego cada PurchaseOrderLine apuntando a él.

    `unit_price_usd`/`ext_price_usd` de cada línea TAMBIÉN hay que mandarlos explícitos —
    verificado en vivo: sin ellos se quedan en None pese a que el PO padre sí trae bien
    amount_usdollar/subtotal_usd/pretax_usd. Sospecha fundada (a confirmar con Gabriel viendo
    la UI): si el total que se muestra en pantalla se arma sumando estos campos por línea en
    vez de leer subtotal/amount directo, una suma con null da NaN en JS — coincide con el
    síntoma reportado ("sigue saliendo con NaN")."""
    grp = sales_order._crm_post("PurchaseOrderLineGroup", {"parent_id": po_id})
    grp_id = grp.get("id")
    if not grp_id:
        log.error(f"No se pudo crear PurchaseOrderLineGroup para PO {po_id}: {grp}")
        return 0

    # Tasa de cambio real que 1CRM ya resolvió sola al crear el PO (se ve en el propio registro
    # aunque no se haya mandado explícita) — se usa para el equivalente en USD de cada línea.
    po_rec = sales_order._crm_get(f"data/PurchaseOrder/{po_id}").get("record", {})
    exchange_rate = sales_order._num(po_rec.get("exchange_rate")) or 1.0

    creadas = 0
    for ln in lineas:
        cantidad = ln.get("quantity", 1) or 1
        precio_unit = ln.get("unit_price") or ln.get("precio_costo") or 0
        ext = float(precio_unit) * float(cantidad)
        payload = {
            "purchase_orders_id": po_id,
            "line_group_id": grp_id,
            "name": ln.get("name") or ln.get("nombre") or "Producto",
            "mfr_part_no": ln.get("mfr_part_no") or ln.get("part_number") or "",
            "quantity": cantidad,
            "unit_price": precio_unit,
            # 1CRM NO calcula el extendido solo (verificado en vivo: unit_price*quantity con
            # ext_price ausente se queda en None) — hay que mandarlo explícito.
            "ext_price": ext,
            "unit_price_usd": float(precio_unit) / exchange_rate,
            "ext_price_usd": ext / exchange_rate,
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
        # 1CRM NO deriva subtotal/pretax de las líneas (verificado en vivo: se quedan en 0 aunque
        # las líneas ya traigan su ext_price) — hay que mandarlos explícitos, igual que amount.
        "subtotal": total,
        "pretax": total,
    }
    if draft.get("terminos_pago"):
        po_payload["terms"] = draft["terminos_pago"]
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
        **({"terms": draft["terminos_pago"]} if draft.get("terminos_pago") else {}),
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
        "po_url": f"{CRM_BASE}/index.php?module=PurchaseOrders&action=DetailView&record={po_id}",
        "bill_id": bill_id,
        "bill_url": f"{CRM_BASE}/index.php?module=Bills&action=DetailView&record={bill_id}",
        "so_id": so_id or None,
        "so_url": f"{CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={so_id}" if so_id else None,
        "proveedor": draft.get("proveedor_nombre", ""),
        "total": total,
    }


def _leer_con_vision(imagenes: list[bytes], instruccion: str, model_id: str = "") -> dict:
    """Helper compartido: manda imágenes + una instrucción a Claude vision y parsea el JSON de
    respuesta. Usado por leer_comprobante y leer_contenido_paquete (mismo patrón que
    orden_compra.normalizar_vision)."""
    if not imagenes:
        return {"error": "sin imágenes que leer"}
    import base64
    import json as _json
    import anthropic
    contenido: list = [{"type": "text", "text": instruccion}]
    for img in imagenes:
        contenido.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": base64.b64encode(img).decode()}})
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        resp = client.messages.create(
            model=model_id or os.environ.get("PO_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=500, temperature=0,
            messages=[{"role": "user", "content": contenido}],
        )
        raw = (resp.content[0].text if resp.content else "").strip()
        i, j = raw.find("{"), raw.rfind("}")
        if i >= 0 and j > i:
            raw = raw[i:j + 1]
        return _json.loads(raw)
    except Exception as e:
        log.error(f"_leer_con_vision: {e}")
        return {"error": f"no pude leer la imagen: {e}"}


# ─────────────────────────────────────────────────────────────
# 4. PAGOS (Etapa 2) — conciliar comprobante de pago contra Bills abiertas
# ─────────────────────────────────────────────────────────────
# HALLAZGO VERIFICADO (create+delete controlado): Payment.related_invoice_id SÍ acepta el id de
# un Bill y se guarda (pese a que su bean_name diga "Invoice" en la metadata — mismo patrón de
# "editable:false no bloquea la API" ya visto), PERO Bill.amount_due NO se recalcula solo, y
# TAMPOCO es editable por PATCH directo (campo protegido/calculado por la lógica interna de
# 1CRM, fuera de alcance de la API cruda). Por eso: el pago SÍ queda creado y trazable (Payment
# ligado al Bill), pero el estatus "pagada" que reporta ESTE sistema se basa en la EXISTENCIA de
# ese Payment — igual criterio que "ya comprado" en el PO (relación como fuente de verdad, no un
# campo). Si se necesita que 1CRM también lo muestre saldado en su propia UI, hay que aplicarlo
# ahí manualmente — no es alcanzable desde aquí.
def bills_abiertas(limite: int = 40) -> list[dict]:
    """Bills (cuentas por pagar) recientes con saldo pendiente (amount_due > 0), con el nombre
    del proveedor resuelto. La lista de 1CRM viene delgada (sin amount_due/supplier_id) — se pide
    detalle de las últimas `limite`, mismo patrón que sales_orders_abiertas/pos_esperando_recepcion."""
    data = sales_order._crm_get("data/Bill", {"order_by": "date_modified desc", "limit": limite})
    out: list[dict] = []
    for b in data.get("records", []):
        bid = b.get("id")
        if not bid:
            continue
        d = sales_order._crm_get(f"data/Bill/{bid}")
        rec = d.get("record", d)
        due = sales_order._num(rec.get("amount_due"))
        if not due or due <= 0:
            continue
        cuenta = sales_order.cuenta_por_id(rec.get("supplier_id", ""))
        out.append({
            "id": bid, "nombre": rec.get("name", ""),
            "amount_due": due, "currency_id": rec.get("currency_id", ""),
            "proveedor": (cuenta or {}).get("nombre", ""),
            "proveedor_id": rec.get("supplier_id", ""),
            "related_purchase_order_id": rec.get("related_purchase_order_id", ""),
            "url": f"{CRM_BASE}/index.php?module=Bills&action=DetailView&record={bid}",
        })
    return out


_INSTR_COMPROBANTE = (
    "Esta imagen es un COMPROBANTE DE PAGO/TRANSFERENCIA a un proveedor. Devuelve SOLO un JSON "
    "(sin ``` ni explicaciones) con el esquema exacto:\n"
    '{"monto": number o null, "moneda": "MXN"|"USD"|"", "fecha": "YYYY-MM-DD" o "", '
    '"referencia": "folio/referencia/beneficiario tal cual aparece, o \\"\\"", '
    '"notas": "cualquier dato dudoso"}\n'
    "NO inventes cifras: si el monto no es legible con claridad, usa null."
)


def leer_comprobante(imagenes: list[bytes], model_id: str = "") -> dict:
    """Lee un comprobante de pago (foto/captura/PDF ya rasterizado a imágenes) con visión de
    Claude. Extrae SOLO lo que esté claramente legible — NO inventa cifras."""
    datos = _leer_con_vision(imagenes, _INSTR_COMPROBANTE, model_id)
    datos.setdefault("monto", None)
    datos.setdefault("moneda", "")
    datos.setdefault("referencia", "")
    return datos


def buscar_bill_candidata(comprobante: dict, bills: list[dict] | None = None) -> dict:
    """Cruza el comprobante leído contra las Bills abiertas por MONTO (tolerancia 1% o 1 unidad,
    lo mayor) — el monto es el dato más confiable de un comprobante. Devuelve
    {candidatas, multiples, por_monto}. NO asume un match único en silencio si hay ambigüedad: el
    usuario confirma cuál es en el widget."""
    todas = bills if bills is not None else bills_abiertas()
    monto = sales_order._num(comprobante.get("monto"))
    if monto is None:
        return {"candidatas": todas, "multiples": len(todas) > 1, "por_monto": False}
    cands = [b for b in todas if abs(b["amount_due"] - monto) <= max(1.0, 0.01 * b["amount_due"])]
    return {"candidatas": cands or todas, "multiples": len(cands) > 1, "por_monto": bool(cands)}


def registrar_pago(bill_id: str, monto: float, datos_comprobante: dict | None = None) -> dict:
    """Crea el Payment ligado al Bill (registrado y trazable). OJO: no cierra el Bill en la UI
    nativa de 1CRM (amount_due no se recalcula vía API, verificado) — el estatus 'pagada' que
    reporta este sistema se basa en la EXISTENCIA de este Payment, no en el campo de 1CRM."""
    if not CRM_BASE:
        return {"error": "1CRM no configurado"}
    d = sales_order._crm_get(f"data/Bill/{bill_id}")
    bill = d.get("record", d)
    if not bill.get("id"):
        return {"error": f"no encontré la Bill {bill_id}"}
    extra = datos_comprobante or {}
    payload = {
        "amount": monto,
        "currency_id": bill.get("currency_id") or "",
        "payment_date": extra.get("fecha") or datetime.date.today().isoformat(),
        "direction": "outgoing",
        "payment_type": extra.get("payment_type") or "Wire Transfer",
        "account_id": bill.get("supplier_id"),
        "related_invoice_id": bill_id,
        "customer_reference": extra.get("referencia") or "",
    }
    r = sales_order._crm_post("Payment", payload)
    pay_id = r.get("id")
    if not pay_id:
        return {"error": f"no se pudo crear el Payment: {r}"}
    return {
        "ok": True,
        "payment_id": pay_id,
        "payment_url": f"{CRM_BASE}/index.php?module=Payments&action=DetailView&record={pay_id}",
        "bill_id": bill_id,
        "bill_url": f"{CRM_BASE}/index.php?module=Bills&action=DetailView&record={bill_id}",
        "monto": monto,
        "aviso": "Pago registrado y ligado al Bill. El saldo (amount_due) de 1CRM no se actualiza "
                 "solo — si necesitas que se vea saldada también en la UI nativa de 1CRM, aplícalo "
                 "ahí manualmente.",
    }


# ─────────────────────────────────────────────────────────────
# 5. RECEPCIÓN (Etapa 3) — tracking + confirmación de contenido → cierra el PurchaseOrder
# ─────────────────────────────────────────────────────────────
# PurchaseOrder no tiene un campo dedicado a tracking number — se guarda como nota en
# `description` (campo libre, siempre presente). `shipping_stage` SÍ es nativo y su PATCH está
# VERIFICADO en vivo (Draft/Ordered/Partially Received/Received) — es lo que cierra el PO.
def pos_esperando_recepcion(limite: int = 40) -> list[dict]:
    """Purchase Orders con mercancía en tránsito (shipping_stage Ordered o Partially Received)."""
    data = sales_order._crm_get("data/PurchaseOrder", {"order_by": "date_modified desc", "limit": limite})
    out: list[dict] = []
    for po in data.get("records", []):
        pid = po.get("id")
        if not pid:
            continue
        d = sales_order._crm_get(f"data/PurchaseOrder/{pid}")
        rec = d.get("record", d)
        if rec.get("shipping_stage") not in ("Ordered", "Partially Received"):
            continue
        cuenta = sales_order.cuenta_por_id(rec.get("supplier_id", ""))
        out.append({
            "id": pid, "nombre": rec.get("name", ""), "shipping_stage": rec.get("shipping_stage"),
            "proveedor": (cuenta or {}).get("nombre", ""), "description": rec.get("description", ""),
            "url": f"{CRM_BASE}/index.php?module=PurchaseOrders&action=DetailView&record={pid}",
            "lineas": [{"name": li.get("name"), "mfr_part_no": li.get("mfr_part_no"),
                        "quantity": li.get("quantity")} for li in (rec.get("line_items") or [])],
        })
    return out


def filtrar_candidatos_recepcion(pos: list[dict], texto_detectado: str = "", tracking: str = "",
                                 max_fallback: int = 8) -> list[dict]:
    """Angosta los PO's candidatos matcheando lo detectado en la foto (o el tracking) contra
    part number/nombre de sus líneas — mismo criterio de coincidencia por substring que
    sales_order._match_item. Con muchos PO's abiertos reales, mostrar los 40 sin filtrar es
    inservible: el usuario tendría que buscar a mano el correcto entre docenas.
    Si NADA matchea, se devuelven los `max_fallback` más recientes (mejor limitado y honesto
    que una lista larga) — nunca se oculta el correcto quedándose en cero candidatos."""
    texto = sales_order._compact(f"{texto_detectado} {tracking}")
    if len(texto) >= 4:
        coincidencias = []
        for po in pos:
            for ln in (po.get("lineas") or []):
                pn = sales_order._compact(ln.get("mfr_part_no") or "")
                nm = sales_order._compact(ln.get("name") or "")
                if (pn and len(pn) >= 4 and pn in texto) or (nm and len(nm) >= 6 and nm in texto):
                    coincidencias.append(po)
                    break
        if coincidencias:
            return coincidencias
    return pos[:max_fallback]


def registrar_tracking(po_id: str, tracking: str) -> dict:
    """Anota el tracking en la descripción del PO (no hay campo nativo de tracking en
    PurchaseOrder — se deja como nota legible)."""
    d = sales_order._crm_get(f"data/PurchaseOrder/{po_id}")
    rec = d.get("record", d)
    if not rec.get("id"):
        return {"error": f"no encontré el Purchase Order {po_id}"}
    desc_actual = (rec.get("description") or "").strip()
    nota = f"Tracking: {tracking}"
    nueva_desc = f"{desc_actual}\n{nota}" if desc_actual else nota
    r = sales_order._crm_patch("PurchaseOrder", po_id, {"description": nueva_desc})
    return {"ok": "error" not in r, "po_id": po_id,
            "po_url": f"{CRM_BASE}/index.php?module=PurchaseOrders&action=DetailView&record={po_id}", "tracking": tracking}


_INSTR_PAQUETE = (
    "Esta es una foto de un paquete/mercancía RECIBIDA de un proveedor. Describe QUÉ productos y "
    "CUÁNTAS piezas de cada uno se alcanzan a ver (colores, modelos, cantidades visibles). "
    "Devuelve SOLO un JSON: "
    '{"items_detectados": ["ej: 2 sensores rojos", "1 sensor verde"], "notas": ""}. '
    "No inventes cantidades que no se vean con claridad — dilo en notas."
)


def leer_contenido_paquete(imagenes: list[bytes], model_id: str = "") -> dict:
    """Describe con visión lo que se ve en la foto del paquete recibido. NUNCA decide solo si
    coincide con el PO esperado — el usuario confirma en el widget viendo ambos lados uno junto
    al otro (mismas reglas de 'no inventar' que el resto del sistema)."""
    datos = _leer_con_vision(imagenes, _INSTR_PAQUETE, model_id)
    datos.setdefault("items_detectados", [])
    return datos


def confirmar_recepcion(po_id: str) -> dict:
    """Cierra el Purchase Order (shipping_stage=Received) — solo tras confirmación del usuario de
    que el contenido coincide con lo esperado."""
    r = sales_order._crm_patch("PurchaseOrder", po_id, {"shipping_stage": "Received"})
    return {"ok": "error" not in r, "po_id": po_id,
            "po_url": f"{CRM_BASE}/index.php?module=PurchaseOrders&action=DetailView&record={po_id}"}


# ─────────────────────────────────────────────────────────────
# 6. CIERRE DE VENTA (Etapa 4) — entrega + factura firmada → cierra la Sales Order
# ─────────────────────────────────────────────────────────────
# so_stage (nativo, PATCH VERIFICADO en vivo) ya modela justo esta distinción: entregado sin
# facturar / facturado sin entregar / cerrado con ambos — no hace falta inventar un campo nuevo.
_SO_STAGE_CIERRE = {
    (True, True):   "Closed - Shipped and Invoiced",
    (True, False):  "Shipped and not Invoiced",
    (False, True):  "Invoiced NOT SHIPPED",
}


def sos_pendientes_cierre(limite: int = 40) -> list[dict]:
    """Sales Orders que aún no están cerradas (Closed - Shipped and Invoiced)."""
    data = sales_order._crm_get("data/SalesOrder", {"order_by": "date_modified desc", "limit": limite})
    out: list[dict] = []
    for so in data.get("records", []):
        sid = so.get("id")
        if not sid:
            continue
        d = sales_order._crm_get(f"data/SalesOrder/{sid}")
        rec = d.get("record", d)
        if rec.get("so_stage") in SO_STAGE_CERRADAS:
            continue
        cuenta = sales_order.cuenta_por_id(rec.get("billing_account_id", ""))
        out.append({"id": sid, "nombre": rec.get("name", ""), "so_stage": rec.get("so_stage"),
                    "cliente": (cuenta or {}).get("nombre", ""),
                    "url": f"{CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={sid}"})
    return out


def cerrar_venta(so_id: str, entregado: bool, facturado: bool) -> dict:
    """Mueve so_stage según lo que el usuario confirma que ya pasó (entrega y/o firma de
    factura) — VERIFICADO en vivo. Si ninguno de los dos ocurrió, no hay nada que mover."""
    nuevo_stage = _SO_STAGE_CIERRE.get((entregado, facturado))
    if not nuevo_stage:
        return {"error": "ni entregado ni facturado — no hay nada que actualizar"}
    r = sales_order._crm_patch("SalesOrder", so_id, {"so_stage": nuevo_stage})
    return {"ok": "error" not in r, "so_id": so_id, "so_stage": nuevo_stage,
            "so_url": f"{CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={so_id}"}


# ─────────────────────────────────────────────────────────────
# 7. DESHACER — cada confirmación puede revertirse con un clic (el usuario puede equivocarse
#    de candidata al confirmar). Un "deshacer" nunca es silencioso: siempre reporta ok/error.
# ─────────────────────────────────────────────────────────────
def deshacer_po_y_ap(po_id: str, bill_id: str = "") -> dict:
    """Revierte la Etapa 1: borra el Bill y el PurchaseOrder (con sus líneas/grupo)."""
    if po_id:
        d = sales_order._crm_get(f"data/PurchaseOrder/{po_id}")
        rec = d.get("record", d)
        grupos = set()
        for li in (rec.get("line_items") or []):
            sales_order._crm_delete("PurchaseOrderLine", li["id"])
            if li.get("line_group_id"):
                grupos.add(li["line_group_id"])
        for g in grupos:
            sales_order._crm_delete("PurchaseOrderLineGroup", g)
    if bill_id:
        sales_order._crm_delete("Bill", bill_id)
    if po_id:
        sales_order._crm_delete("PurchaseOrder", po_id)
    return {"ok": True}


def deshacer_pago(payment_id: str) -> dict:
    """Revierte la Etapa 2: borra el Payment registrado."""
    if not payment_id:
        return {"error": "falta payment_id"}
    r = sales_order._crm_delete("Payment", payment_id)
    return {"ok": "error" not in r}


def deshacer_recepcion(po_id: str, estado_anterior: str = "") -> dict:
    """Revierte la Etapa 3: regresa shipping_stage al valor que tenía antes de confirmar."""
    if not po_id:
        return {"error": "falta po_id"}
    r = sales_order._crm_patch("PurchaseOrder", po_id, {"shipping_stage": estado_anterior or "Ordered"})
    return {"ok": "error" not in r, "po_id": po_id}


def deshacer_cierre(so_id: str, estado_anterior: str = "") -> dict:
    """Revierte la Etapa 4: regresa so_stage al valor que tenía antes de confirmar."""
    if not so_id:
        return {"error": "falta so_id"}
    r = sales_order._crm_patch("SalesOrder", so_id, {"so_stage": estado_anterior or "Ordered"})
    return {"ok": "error" not in r, "so_id": so_id}


# ─────────────────────────────────────────────────────────────
# 8. ESTADO OPERATIVO — rollup Vendido→Comprado→Pagado→Recibido→Facturado de una Sales Order
# ─────────────────────────────────────────────────────────────
# HALLAZGO EN VIVO: el endpoint de LISTA de PurchaseOrder/Bill/Payment con order_by=date_modified
# desc + limit NO refleja registros recién creados de forma confiable (probado: un PO creado y
# verificado por GET directo minutos antes no aparecía entre los primeros 60 de la lista, con solo
# 26 resultados devueltos pese a limit=60 — huele a caché o a que order_by/limit no se respetan de
# verdad en este endpoint, mismo espíritu que el hallazgo de filters[x] ya documentado). Por eso
# esta función NO escanea listas: recibe los po_ids/bill_ids YA CONOCIDOS (el llamador los saca
# del historial de mensajes de Supabase, que es la fuente de verdad confiable) y solo hace GET
# de detalle por id — eso sí funciona siempre, confirmado repetidas veces en la sesión.
def estado_operativo_so(so_id: str, po_ids: list[str] | None = None, bill_ids: list[str] | None = None,
                        bill_ids_pagadas: set[str] | None = None) -> dict:
    """Arma el rollup de una Sales Order a partir de los PO's/Bills que YA SE SABE que le
    pertenecen (por el historial de la conversación) — no intenta redescubrirlos escaneando 1CRM.
    `bill_ids_pagadas` = subconjunto de bill_ids para los que ya se registró un Payment (el
    llamador lo saca del historial también — amount_due no sirve, ver nota arriba)."""
    bill_ids_pagadas = bill_ids_pagadas or set()
    d = sales_order._crm_get(f"data/SalesOrder/{so_id}")
    so = d.get("record", d)
    if not so.get("id"):
        return {"error": f"no encontré la Sales Order {so_id}"}
    cuenta = sales_order.cuenta_por_id(so.get("billing_account_id", ""))

    pos_ligados = []
    for pid in (po_ids or []):
        rec = sales_order._crm_get(f"data/PurchaseOrder/{pid}").get("record", {})
        if rec.get("id"):
            pos_ligados.append(rec)

    bills_ligadas = []
    for bid in (bill_ids or []):
        rec = sales_order._crm_get(f"data/Bill/{bid}").get("record", {})
        if rec.get("id"):
            bills_ligadas.append(rec)

    total_po = len(pos_ligados)
    recibidos = sum(1 for p in pos_ligados if p.get("shipping_stage") == "Received")
    total_bill = len(bills_ligadas)
    # amount_due no sirve para saber si está pagada (verificado: no se actualiza solo — ver
    # deshacer_pago/registrar_pago) — "pagada" se decide por si el historial ya registró un
    # Payment para esa Bill, que el llamador pasa en bill_ids_pagadas.
    pagadas = sum(1 for b in bills_ligadas if b["id"] in bill_ids_pagadas)

    return {
        "so_id": so_id, "so_nombre": so.get("name", ""), "so_numero": so.get("so_number"),
        "so_stage": so.get("so_stage", ""), "cliente": (cuenta or {}).get("nombre", ""),
        "so_url": f"{CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={so_id}",
        "comprado": {"total": total_po, "hecho": total_po,
                     "pos": [{"id": p["id"], "nombre": p.get("name", ""),
                              "url": f"{CRM_BASE}/index.php?module=PurchaseOrders&action=DetailView&record={p['id']}"}
                             for p in pos_ligados]},
        "pagado": {"total": total_bill, "hecho": pagadas},
        "recibido": {"total": total_po, "hecho": recibidos},
        "facturado": so.get("so_stage") == "Closed - Shipped and Invoiced",
    }
