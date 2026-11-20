"""
PAGOS — stream "pagos" (y también disponible dentro de "compras", mismo backend)
=================================================================================
Lee un comprobante de pago (foto/captura/PDF) y lo coteja contra las cuentas ABIERTAS de 1CRM en
AMBAS direcciones:
  - Bill   (cuenta por pagar — nosotros le pagamos a un proveedor, direction=outgoing)
  - Invoice (cuenta por cobrar — un cliente nos paga a nosotros, direction=incoming)

Mecánica de escritura 1CRM — VERIFICADA en vivo con prueba controlada create+delete:
  - `Payment.related_invoice_id` acepta el id de un Bill O de una Invoice y se guarda en ambos
    casos (pese a que su bean_name declarado en la metadata diga "Invoice" — mismo patrón de
    "editable:false/bean_name no es la verdad" ya visto en el resto del proyecto). Confirmado con
    ambos: un Bill real (ya en producción desde antes) y una Invoice real (sesión 2026-08-19,
    create+GET+delete controlado, sin dejar rastro).
  - NI `Bill.amount_due` NI `Invoice.amount_due` se recalculan solos ni son editables por PATCH
    directo — son campos protegidos/calculados internamente por 1CRM, fuera de alcance de la API
    REST. El pago SÍ queda creado y trazable (Payment ligado), pero el saldo pendiente que reporta
    ESTE sistema se calcula aparte (ver `saldo_real` más abajo) — igual criterio que "ya comprado"
    en compra_proveedor.py (relación/historial como fuente de verdad, no un campo de 1CRM).
  - `filters[related_invoice_id]` en `GET data/Payment` NO filtra de verdad (devuelve la lista
    completa sin importar el valor) — no se puede usar para sumar pagos previos de un Bill/Invoice.
    Por eso `saldo_real` recibe `pagado_previo` ya calculado por el caller (agente_chat.py) desde
    el historial de mensajes `[PAGO_REGISTRADO]` — mismo patrón ya establecido en el proyecto para
    este tipo de problema (`_recibido_previo_po`, `_enviado_previo_so`).
"""

import os
import logging
import datetime

import sales_order  # reusa _crm_get/_crm_post/_crm_delete, _num, cuenta_por_id
import compra_proveedor  # reusa _leer_con_vision (helper de visión compartido)

log = logging.getLogger("pagos")

CRM_BASE = sales_order.CRM_BASE


def codigo_moneda(currency_id: str) -> str:
    """Código ISO ('MXN', 'USD', 'EUR'…) de un currency_id de 1CRM; '' si no se conoce."""
    try:
        for codigo, m in compra_proveedor._monedas_crm().items():
            if m.get("id") == currency_id:
                return codigo
    except Exception as e:
        log.warning(f"codigo_moneda({currency_id}): {e}")
    return ""


# ─────────────────────────────────────────────────────────────
# 1. CUENTAS ABIERTAS (Bill + Invoice combinadas)
# ─────────────────────────────────────────────────────────────
def bills_abiertas(limite: int = 40) -> list[dict]:
    """Bills (cuentas por pagar) recientes con saldo pendiente (amount_due > 0 en 1CRM), con el
    nombre del proveedor resuelto. La lista de 1CRM viene delgada — se pide detalle por id."""
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
            "id": bid, "tipo": "bill", "direccion": "outgoing",
            "nombre": rec.get("name", ""),
            "amount_due": due, "currency_id": rec.get("currency_id", ""), "moneda": codigo_moneda(rec.get("currency_id", "")),
            "cuenta": (cuenta or {}).get("nombre", ""), "cuenta_id": rec.get("supplier_id", ""),
            "url": f"{CRM_BASE}/index.php?module=Bills&action=DetailView&record={bid}",
        })
    return out


def invoices_abiertas(limite: int = 40) -> list[dict]:
    """Invoices (cuentas por cobrar — un cliente nos debe) recientes con saldo pendiente, con el
    nombre del cliente resuelto. Mismo patrón que bills_abiertas (lista delgada → detalle por id)."""
    data = sales_order._crm_get("data/Invoice", {"order_by": "date_modified desc", "limit": limite})
    out: list[dict] = []
    for inv in data.get("records", []):
        iid = inv.get("id")
        if not iid:
            continue
        d = sales_order._crm_get(f"data/Invoice/{iid}")
        rec = d.get("record", d)
        if str(rec.get("cancelled") or "0") in ("1", "True", "true"):
            continue  # Invoice cancelada (p.ej. espejo de una factura de Alegra deshecha)
        due = sales_order._num(rec.get("amount_due"))
        if not due or due <= 0:
            continue
        cuenta = sales_order.cuenta_por_id(rec.get("billing_account_id", ""))
        out.append({
            "id": iid, "tipo": "invoice", "direccion": "incoming",
            "nombre": rec.get("name", ""),
            "amount_due": due, "currency_id": rec.get("currency_id", ""), "moneda": codigo_moneda(rec.get("currency_id", "")),
            "cuenta": (cuenta or {}).get("nombre", ""), "cuenta_id": rec.get("billing_account_id", ""),
            "url": f"{CRM_BASE}/index.php?module=Invoice&action=DetailView&record={iid}",
        })
    return out


def cuentas_abiertas(pagado_previo: dict | None = None, limite: int = 40) -> list[dict]:
    """Combina Bills + Invoices abiertas, y AJUSTA `amount_due` restando lo que este sistema ya
    tiene registrado como pagado (`pagado_previo`, dict {id: monto_acumulado} calculado por el
    caller desde el historial de [PAGO_REGISTRADO] — ver docstring del módulo). Sin este ajuste,
    un segundo pago parcial sobre el mismo Bill/Invoice seguiría viendo el saldo bruto de 1CRM, que
    nunca baja solo. Se descartan las que ya quedaron en $0 o menos según nuestro propio cálculo."""
    pagado_previo = pagado_previo or {}
    todas = bills_abiertas(limite) + invoices_abiertas(limite)
    out = []
    for c in todas:
        ya_pagado = pagado_previo.get(c["id"], 0) or 0
        saldo = round(c["amount_due"] - ya_pagado, 2)
        if saldo <= 0:
            continue
        c = {**c, "amount_due": saldo}
        out.append(c)
    return out


# ─────────────────────────────────────────────────────────────
# 2. LEER COMPROBANTE (visión) + COTEJAR contra cuentas abiertas
# ─────────────────────────────────────────────────────────────
_INSTR_COMPROBANTE = (
    "Esta imagen debería ser un COMPROBANTE DE PAGO/TRANSFERENCIA. Devuelve SOLO un JSON "
    "(sin ``` ni explicaciones) con el esquema exacto:\n"
    '{"es_pago": true|false, "monto": number o null, "moneda": "MXN"|"USD"|"EUR"|"", "fecha": "YYYY-MM-DD" o "", '
    '"referencia": "folio/referencia/beneficiario tal cual aparece, o \\"\\"", '
    '"notas": "cualquier dato dudoso"}\n'
    "es_pago = true SOLO si es evidencia de un pago ya hecho (transferencia, recibo de pago, ficha de depósito, "
    "confirmación de pago). Es false si es un pedido/orden, carrito, cotización, proforma, factura por pagar, guía de envío o "
    "cualquier documento que NO demuestre que se pagó algo; en ese caso explica en notas qué es. "
    "NO inventes cifras: si el monto no es legible con claridad, usa null."
)


def leer_comprobante(imagenes: list[bytes], model_id: str = "") -> dict:
    """Lee un comprobante de pago (foto/captura/PDF ya rasterizado a imágenes) con visión de
    Claude. Extrae SOLO lo que esté claramente legible — NO inventa cifras."""
    datos = compra_proveedor._leer_con_vision(imagenes, _INSTR_COMPROBANTE, model_id)
    datos.setdefault("monto", None)
    datos.setdefault("moneda", "")
    datos.setdefault("referencia", "")
    datos.setdefault("es_pago", True)   # si el lector no lo dijo, se comporta como antes
    return datos


def buscar_candidata(comprobante: dict, cuentas: list[dict] | None = None,
                      pagado_previo: dict | None = None) -> dict:
    """Cruza el comprobante leído contra las cuentas abiertas (Bill+Invoice) por MONTO (tolerancia
    1% o 1 unidad, lo mayor — el monto es el dato más confiable de un comprobante). Si nada
    matchea por monto (puede ser un pago PARCIAL, o simplemente no corresponder a nada que ya
    tengamos abierto), cae a mostrar TODAS las cuentas abiertas para que el usuario elija a mano —
    nunca asume en silencio. `por_monto=False` es la señal de "no hubo match automático": ahí es
    donde agente_chat.py ofrece el Agente 2 (crear PO/SO), SIN dejar de mostrar la lista completa
    como alternativa manual. Cada candidata trae "completo" (el monto cubre el saldo real) o
    parcial, calculado contra su `amount_due` YA AJUSTADO por `cuentas_abiertas`."""
    todas = cuentas if cuentas is not None else cuentas_abiertas(pagado_previo)
    monto = sales_order._num(comprobante.get("monto"))
    mon_comp = (comprobante.get("moneda") or "").upper()

    def _misma_moneda(c: dict) -> bool:
        # Sin moneda en el comprobante o en la cuenta no hay con qué comparar: no se bloquea.
        return not mon_comp or not c.get("moneda") or c["moneda"] == mon_comp

    todas = [{**c, "moneda_coincide": _misma_moneda(c)} for c in todas]
    if monto is None:
        return {"candidatas": todas, "multiples": len(todas) > 1, "por_monto": False}
    # Coincidencia automática = monto Y moneda (1,335 EUR no es 1,335 MXN).
    cands = [c for c in todas if c["moneda_coincide"] and abs(c["amount_due"] - monto) <= max(1.0, 0.01 * c["amount_due"])]
    finales = cands or todas
    finales = [{**c, "completo": monto >= c["amount_due"] * 0.99,
                "excede": round(monto - c["amount_due"], 2) if monto > c["amount_due"] * 1.01 else 0} for c in finales]
    return {"candidatas": finales, "multiples": len(finales) > 1, "por_monto": bool(cands)}


# ─────────────────────────────────────────────────────────────
# 3. REGISTRAR / DESHACER PAGO
# ─────────────────────────────────────────────────────────────
def registrar_pago(tipo: str, registro_id: str, monto: float, datos_comprobante: dict | None = None) -> dict:
    """Crea el Payment ligado al Bill o Invoice (registrado y trazable). tipo: "bill" (pagamos a
    proveedor, direction=outgoing) o "invoice" (cliente nos paga, direction=incoming). OJO: no
    cierra el Bill/Invoice en la UI nativa de 1CRM — ver docstring del módulo."""
    if not CRM_BASE:
        return {"error": "1CRM no configurado"}
    if tipo not in ("bill", "invoice"):
        return {"error": f"tipo inválido: {tipo!r} (debe ser 'bill' o 'invoice')"}
    modulo = "Bill" if tipo == "bill" else "Invoice"
    d = sales_order._crm_get(f"data/{modulo}/{registro_id}")
    rec = d.get("record", d)
    if not rec.get("id"):
        return {"error": f"no encontré {modulo} {registro_id}"}
    cuenta_id = rec.get("supplier_id") if tipo == "bill" else rec.get("billing_account_id")
    extra = datos_comprobante or {}
    # Defensa en el servidor (aunque el widget ya lo bloquea): el Payment se guarda en la moneda de la cuenta, así que un
    # comprobante en otra moneda registraría un monto sin sentido.
    mon_comp, mon_cta = (extra.get("moneda") or "").upper(), codigo_moneda(rec.get("currency_id", ""))
    if mon_comp and mon_cta and mon_comp != mon_cta:
        return {"error": f"el comprobante está en {mon_comp} y {modulo} {rec.get('name', registro_id)!s} está en {mon_cta}: "
                         f"no registro el pago (el monto no es comparable)."}
    payload = {
        "amount": monto,
        "currency_id": rec.get("currency_id") or "",
        "payment_date": extra.get("fecha") or datetime.date.today().isoformat(),
        "direction": "outgoing" if tipo == "bill" else "incoming",
        "payment_type": extra.get("payment_type") or "Wire Transfer",
        "account_id": cuenta_id,
        "related_invoice_id": registro_id,
        "customer_reference": extra.get("referencia") or "",
    }
    r = sales_order._crm_post("Payment", payload)
    pay_id = r.get("id")
    if not pay_id:
        return {"error": f"no se pudo crear el Payment: {r}"}
    modulo_url = "Bills" if tipo == "bill" else "Invoice"
    return {
        "ok": True,
        "payment_id": pay_id,
        "payment_url": f"{CRM_BASE}/index.php?module=Payments&action=DetailView&record={pay_id}",
        "tipo": tipo,
        "registro_id": registro_id,
        "registro_url": f"{CRM_BASE}/index.php?module={modulo_url}&action=DetailView&record={registro_id}",
        "monto": monto,
        "aviso": "Pago registrado y ligado — el saldo pendiente de 1CRM no se actualiza solo; "
                 "si necesitas que se vea saldado también en la UI nativa de 1CRM, aplícalo ahí manualmente.",
    }


def deshacer_pago(payment_id: str) -> dict:
    """Borra el Payment registrado."""
    if not payment_id:
        return {"error": "falta payment_id"}
    r = sales_order._crm_delete("Payment", payment_id)
    return {"ok": "error" not in r}
