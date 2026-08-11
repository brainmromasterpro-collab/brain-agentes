"""
SALES SHIPPING — Brain · MRO Master Pro
=======================================
Lado SALIENTE (envío al cliente) del ciclo O2C, espejo del receiving (compra_proveedor).
Vive dentro del stream de compras (es el siguiente paso operativo del mismo pedido: se compró →
se pagó → se recibió → SE ENVÍA → se factura).

Modelo nativo de 1CRM (verificado en vivo, ver reference_1crm_api_quirks):
  Shipping (so_id, invoice_id, shipping_stage: "In Preparation" -> "Shipped", tracking_number,
  date_shipped, weight_1/weight_2, num_packages, shipping_cost, description, warehouse_id) +
  ShippingLineGroup (parent_id, group_type) + ShippingLine (shipping_id, line_group_id, name,
  mfr_part_no, quantity).

Reglas de negocio (definidas con Gabriel):
  - Un SO puede salir en VARIOS envíos parciales.
  - El stock se RESTA al ENVIAR (marcar Shipped), no al facturar (la factura quedó solo financiera).
  - so_stage se deriva de dos dimensiones: enviado × facturado.
  - Marcar Shipped por REST NO dispara el inventario semiauto nativo -> el descuento es manual.
"""
import datetime

import sales_order
import compra_proveedor  # reusa _ajustar_stock (mismo campo all_stock)

CRM_BASE = sales_order.CRM_BASE

# Bodega por defecto (una real del CRM); si el SO/tenant tuviera otra, se puede parametrizar.
WAREHOUSE_DEFAULT = "3777faab-6531-fb55-12cc-64fa32a513dc"

SO_STAGE_CERRADAS = {"Closed - Shipped and Invoiced"}
# Estados de so_stage que implican que YA se facturó (para derivar el estado combinado al enviar).
_SO_STAGE_FACTURADO = {
    "Invoiced NOT SHIPPED", "Partially Shipped and Invoiced", "Closed - Shipped and Invoiced",
}


def sos_por_enviar(limite: int = 40) -> list[dict]:
    """Sales Orders con líneas pendientes de enviar (no cerradas). Trae cliente y líneas para el
    checklist de qué se envía."""
    data = sales_order._crm_get("data/SalesOrder", {"order_by": "date_modified desc", "limit": limite})
    out: list[dict] = []
    for so in data.get("records", []):
        sid = so.get("id")
        if not sid:
            continue
        rec = sales_order._crm_get(f"data/SalesOrder/{sid}").get("record", {})
        if rec.get("so_stage") in SO_STAGE_CERRADAS:
            continue
        cuenta = sales_order.cuenta_por_id(rec.get("billing_account_id", ""))
        out.append({
            "id": sid, "nombre": rec.get("name", ""), "so_stage": rec.get("so_stage"),
            "cliente": (cuenta or {}).get("nombre", ""),
            "url": f"{CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={sid}",
            "lineas": [{"name": li.get("name"), "mfr_part_no": li.get("mfr_part_no"),
                        "quantity": li.get("quantity")} for li in (rec.get("line_items") or [])],
        })
    return out


def cantidades_por_enviar(so_id: str, ya_enviado: dict[str, float] | None = None) -> list[dict]:
    """Por cada línea del SO: cuánto se pidió, cuánto ya se envió antes (`ya_enviado`, claves =
    part number compactado, lo calcula el llamador desde el historial) y cuánto falta. Base del
    checklist de envío."""
    ya_enviado = ya_enviado or {}
    rec = sales_order._crm_get(f"data/SalesOrder/{so_id}").get("record", {})
    out = []
    for li in (rec.get("line_items") or []):
        pn = li.get("mfr_part_no") or ""
        pc = sales_order._compact(pn)
        pedido = sales_order._num(li.get("quantity")) or 0
        enviado_previo = ya_enviado.get(pc, 0) or 0
        out.append({
            "name": li.get("name", ""), "mfr_part_no": pn,
            "cantidad_pedida": pedido, "cantidad_enviada_previo": enviado_previo,
            "cantidad_restante": max(0, pedido - enviado_previo),
        })
    return out


def crear_shipping(so_id: str, lineas_enviadas: list[dict], datos: dict | None = None) -> dict:
    """Crea el Shipping en 'In Preparation' con las líneas de ESTE envío (puede ser parcial) y los
    datos logísticos que capturó el usuario (peso, dimensiones, costo, tracking, paquetes). NO
    toca el stock todavía — eso pasa al marcar 'Shipped'.

    lineas_enviadas = [{name, mfr_part_no, quantity}]  (solo lo que va en este envío)
    datos = {tracking, peso_lbs, peso_oz, dimensiones, costo_envio, num_packages, carrier_id}
    """
    if not CRM_BASE:
        return {"error": "1CRM no configurado"}
    datos = datos or {}
    lineas = [l for l in (lineas_enviadas or []) if (sales_order._num(l.get("quantity")) or 0) > 0]
    if not lineas:
        return {"error": "no hay líneas con cantidad para enviar"}

    so_rec = sales_order._crm_get(f"data/SalesOrder/{so_id}").get("record", {})
    if not so_rec.get("id"):
        return {"error": f"no encontré el Sales Order {so_id}"}
    cuenta_id = so_rec.get("billing_account_id", "")

    descripcion = ""
    if datos.get("dimensiones"):
        descripcion = f"Dimensiones: {datos['dimensiones']}"

    costo = sales_order._num(datos.get("costo_envio")) or 0
    payload = {
        "name": so_rec.get("name", "") or f"Envío {so_id}",
        "so_id": so_id,
        "shipping_stage": "In Preparation",
        "warehouse_id": datos.get("warehouse_id") or WAREHOUSE_DEFAULT,
        "shipping_cost": costo, "shipping_cost_usd": costo, "total_shipping": costo,
        "weight_1": int(sales_order._num(datos.get("peso_lbs")) or 0),
        "weight_2": int(sales_order._num(datos.get("peso_oz")) or 0),
        "num_packages": int(sales_order._num(datos.get("num_packages")) or 1),
        "tracking_number": datos.get("tracking", "") or "",
        "description": descripcion,
    }
    if cuenta_id:
        payload["shipping_account_id"] = cuenta_id
    if datos.get("carrier_id"):
        payload["shipping_provider_id"] = datos["carrier_id"]

    r = sales_order._crm_post("Shipping", payload)
    ship_id = r.get("id")
    if not ship_id:
        return {"error": f"no se pudo crear el Shipping: {r.get('message') or r}"}

    lineas_creadas = _crear_lineas_shipping(ship_id, lineas)

    return {"ok": True, "shipping_id": ship_id, "so_id": so_id,
            "shipping_stage": "In Preparation", "tracking": payload["tracking_number"],
            # las líneas viajan en el resultado (y de ahí al marker) para que marcar_enviado/deshacer
            # no dependan de releerlas de 1CRM (el agregado/lista de ShippingLine es inestable).
            "lineas": [{"name": l.get("name"), "mfr_part_no": l.get("mfr_part_no"),
                        "quantity": sales_order._num(l.get("quantity")) or 0, "line_id": l.get("line_id")}
                       for l in lineas_creadas],
            "shipping_url": f"{CRM_BASE}/index.php?module=Shipping&action=DetailView&record={ship_id}",
            "so_url": f"{CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={so_id}"}


def _crear_lineas_shipping(ship_id: str, lineas: list[dict]) -> list[dict]:
    """ShippingLineGroup (parent_id=shipping) + una ShippingLine por línea. Devuelve las líneas con
    su line_id creado. Se guardan y ligan aunque el line_items del Shipping no las refleje al
    instante (quirk documentado)."""
    grp = sales_order._crm_post("ShippingLineGroup", {"parent_id": ship_id, "group_type": "standard"})
    gid = grp.get("id")
    if not gid:
        return []
    creadas = []
    for ln in lineas:
        cantidad = sales_order._num(ln.get("quantity")) or 0
        if cantidad <= 0:
            continue
        res = sales_order._crm_post("ShippingLine", {
            "shipping_id": ship_id, "line_group_id": gid,
            "name": ln.get("name") or ln.get("mfr_part_no") or "Producto",
            "mfr_part_no": ln.get("mfr_part_no") or "",
            "quantity": cantidad, "ext_quantity": cantidad,
        })
        creadas.append({**ln, "line_id": res.get("id")})
    return creadas


def marcar_enviado(ship_id: str, lineas_enviadas: list[dict], so_id: str,
                   tracking: str = "", enviado_total: dict[str, float] | None = None) -> dict:
    """Pasa el Shipping a 'Shipped' (con date_shipped), RESTA del stock lo enviado en ESTE envío
    (`lineas_enviadas`, del marker — no se releen de 1CRM) y recalcula el so_stage combinando lo
    enviado acumulado (`enviado_total`, claves = part number compactado, lo arma el llamador desde
    el historial) × facturado. El stock físico sale al ENVIAR (regla de Gabriel), no al facturar."""
    patch = {"shipping_stage": "Shipped", "date_shipped": datetime.date.today().isoformat()}
    if tracking:
        patch["tracking_number"] = tracking
    r = sales_order._crm_patch("Shipping", ship_id, patch)
    ok = "error" not in r

    ajustes: list = []
    for ln in (lineas_enviadas or []):
        pn = ln.get("mfr_part_no") or ""
        cantidad = sales_order._num(ln.get("quantity")) or 0
        if pn and cantidad:
            ajustes.append(compra_proveedor._ajustar_stock(pn, -cantidad))

    nuevo_stage = _recalcular_so_stage(so_id, enviado_total or {})
    return {"ok": ok, "shipping_id": ship_id, "so_id": so_id, "shipping_stage": "Shipped",
            "so_stage": nuevo_stage, "ajustes_stock": ajustes,
            "shipping_url": f"{CRM_BASE}/index.php?module=Shipping&action=DetailView&record={ship_id}",
            "so_url": f"{CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={so_id}"}


def _recalcular_so_stage(so_id: str, enviado_total: dict[str, float]) -> str:
    """Deriva y escribe so_stage de dos dimensiones: ENVIADO (`enviado_total`, acumulado por part
    number compactado que arma el llamador desde el historial de envíos) vs lo pedido en el SO ×
    FACTURADO (se conserva del estado actual, que lo maneja la factura)."""
    so_rec = sales_order._crm_get(f"data/SalesOrder/{so_id}").get("record", {})
    facturado = so_rec.get("so_stage") in _SO_STAGE_FACTURADO

    enviado_completo = True
    algo_enviado = False
    for li in (so_rec.get("line_items") or []):
        pc = sales_order._compact(li.get("mfr_part_no") or "")
        pedido = sales_order._num(li.get("quantity")) or 0
        env = (enviado_total or {}).get(pc, 0) or 0
        if env > 0:
            algo_enviado = True
        if env < pedido:
            enviado_completo = False

    if enviado_completo and facturado:
        stage = "Closed - Shipped and Invoiced"
    elif enviado_completo:
        stage = "Shipped and not Invoiced"
    elif algo_enviado and facturado:
        stage = "Partially Shipped and Invoiced"
    elif algo_enviado:
        stage = "Partially Shipped and not Invoiced"
    else:
        stage = so_rec.get("so_stage") or "Ordered"

    sales_order._crm_patch("SalesOrder", so_id, {"so_stage": stage})
    return stage


def deshacer_shipping(ship_id: str, lineas: list[dict], so_id: str = "",
                      estaba_enviado: bool = False, so_stage_anterior: str = "") -> dict:
    """Deshace un envío: si ya estaba 'Shipped' regresa al stock lo que se había restado (`lineas`,
    del marker); borra las ShippingLine (por line_id del marker) y el Shipping, y restaura el
    so_stage anterior."""
    ajustes: list = []
    if estaba_enviado:
        for ln in (lineas or []):
            pn = ln.get("mfr_part_no") or ""
            cantidad = sales_order._num(ln.get("quantity")) or 0
            if pn and cantidad:
                ajustes.append(compra_proveedor._ajustar_stock(pn, cantidad))  # regresa lo restado

    for ln in (lineas or []):
        if ln.get("line_id"):
            sales_order._crm_delete("ShippingLine", ln["line_id"])
    sales_order._crm_delete("Shipping", ship_id)

    if so_id and so_stage_anterior:
        sales_order._crm_patch("SalesOrder", so_id, {"so_stage": so_stage_anterior})
    return {"ok": True, "shipping_id": ship_id, "so_id": so_id, "ajustes_stock": ajustes}
