"""Alegra (facturación electrónica México) — cliente REST + puente desde 1CRM.

POR QUÉ REST Y NO EL MCP: mcp.alegra.com solo deja autorizar OAuth desde claude.ai (rechaza cualquier
otro redirect_uri, verificado en vivo), así que un backend en Railway no puede usarlo. La API REST
(https://api.alegra.com/api/v1, Basic auth) sí. Credenciales: ALEGRA_EMAIL + ALEGRA_TOKEN, donde
ALEGRA_TOKEN es el `api_key` de 20 caracteres (NO el JWT largo, que la API rechaza con 401).

Flujo: Sales Order de 1CRM -> BORRADOR (borrador_desde_so, no escribe nada) -> con el sí del usuario
-> crear_factura (en Alegra queda como borrador; timbrar es un paso aparte y explícito, porque un
CFDI timbrado solo se corrige cancelándolo ante el SAT).

Hallazgos verificados en vivo (cuenta de pruebas "MRO ONLINE 4U", 2026-10-27):
  - GET company/contacts/items/taxes/invoices/payments/number-templates: 200.
  - bills y bank-accounts: 402 "no se puede realizar en tu plan actual" (plan sin compras/bancos).
  - La empresa no tenía RFC/régimen/domicilio, y la numeración principal no es electrónica -> aún
    no se puede timbrar. `estado_cuenta()` lo reporta para que el widget avise.
"""
import base64
import datetime
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import sales_order

log = logging.getLogger("alegra")

BASE = "https://api.alegra.com/api/v1"
APP_URL = "https://app.alegra.com"

# 1CRM (fiscal_regime) -> clave SAT que Alegra pide en `regimeClient` (CFDI 4.0).
_REGIMEN_SAT = {
    "Persona Moral - General": "601",
    "Persona Moral - RESICO": "626",
    "Persona Moral - No Lucrativa": "603",
    "Persona Fisica - RESICO": "626",
    "Persona Fisica - Actividad Empresarial": "612",
    "Persona Fisica - Arrendamiento": "606",
    "Extranjero / Internacional": "616",
}
# 1CRM (fiscal_regime) -> clave de régimen que Alegra pide EN EL CONTACTO (`regime` + `regimeObject`).
# Lista de la doc de Alegra; el RFC de 12 caracteres (moral) y de 13 (física) admiten claves distintas.
_REGIMEN_ALEGRA = {
    "Persona Moral - General": "GENERAL_REGIME_OF_MORAL_PEOPLE_LAW",
    "Persona Moral - RESICO": "SIMPLIFIED_REGIME",
    "Persona Moral - No Lucrativa": "REGIME_OF_MORAL_PEOPLE_NOT_PROFIT",
    "Persona Fisica - RESICO": "SIMPLIFIED_REGIME",
    "Persona Fisica - Actividad Empresarial": "BUSINESS_ACTIVITIES_REGIME",
    "Persona Fisica - Arrendamiento": "LEASEHOLD_REGIME",
    "Extranjero / Internacional": "SIMPLIFIED_REGIME",
}
RFC_EXTRANJERO = "XEXX010101000"
RFC_PUBLICO_GENERAL = "XAXX010101000"

_cache: dict = {}


def configurado() -> bool:
    return bool(os.environ.get("ALEGRA_EMAIL") and os.environ.get("ALEGRA_TOKEN"))


def _auth() -> str:
    cred = f"{os.environ.get('ALEGRA_EMAIL', '')}:{os.environ.get('ALEGRA_TOKEN', '')}"
    return "Basic " + base64.b64encode(cred.encode()).decode()


def _req(method: str, path: str, body: dict | None = None, params: dict | None = None,
         timeout: int = 40) -> tuple[int, object]:
    """Llamada a la API de Alegra. Devuelve (status, json|texto). Nunca lanza por HTTP != 2xx."""
    if not configurado():
        return 0, {"message": "Alegra no configurado (faltan ALEGRA_EMAIL / ALEGRA_TOKEN)"}
    url = f"{BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": _auth(), "accept": "application/json", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw[:300].decode(errors="replace")}
    except Exception as e:
        return 0, {"message": f"sin conexión con Alegra: {e}"}


def _msg(data: object) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data)[:400]
    return str(data)[:400]


# ─────────────────────────────────────────────────────────────
# 1. ESTADO DE LA CUENTA — ¿ya se puede timbrar?
# ─────────────────────────────────────────────────────────────
def estado_cuenta() -> dict:
    """Diagnóstico de la cuenta de Alegra para avisar al usuario qué falta antes de timbrar."""
    st, emp = _req("GET", "company")
    if st != 200 or not isinstance(emp, dict):
        return {"ok": False, "error": _msg(emp)}
    st2, plantillas = _req("GET", "number-templates")
    electronica = any(t.get("isElectronic") and t.get("documentType") == "invoice"
                      for t in (plantillas if isinstance(plantillas, list) else []))
    faltan = []
    if not emp.get("identification"):
        faltan.append("RFC de la empresa")
    if (emp.get("regimeKey") or "NO_REGIME") == "NO_REGIME":
        faltan.append("régimen fiscal de la empresa")
    if not (emp.get("address") or {}).get("zipCode"):
        faltan.append("domicilio fiscal (código postal)")
    if not electronica:
        faltan.append("numeración electrónica de facturas (certificado CSD)")
    return {"ok": True, "empresa": emp.get("name"), "rfc": emp.get("identification"),
            "regimen": emp.get("regime"), "timbrado_listo": not faltan, "faltan_para_timbrar": faltan}


# ─────────────────────────────────────────────────────────────
# 2. CLIENTES E ÍTEMS
# ─────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", sales_order._norm(s or "").upper())


def buscar_cliente(rfc: str = "", nombre: str = "") -> dict | None:
    """Busca un contacto tipo cliente por RFC (prioridad) o por nombre normalizado."""
    for q in [x for x in (rfc, nombre) if x]:
        st, data = _req("GET", "contacts", params={"query": q, "type": "client", "limit": 30})
        if st != 200 or not isinstance(data, list):
            continue
        if rfc:
            for c in data:
                if _norm(c.get("identification") or "") == _norm(rfc):
                    return c
        if nombre:
            for c in data:
                if _norm(c.get("name") or "") == _norm(nombre):
                    return c
    return None


def crear_cliente(c: dict) -> dict:
    """Da de alta un cliente en Alegra desde los datos de la cuenta de 1CRM."""
    body = {
        "name": c["nombre"], "type": ["client"], "identification": c.get("rfc") or "",
        "email": c.get("email") or "",
        "address": {"zipCode": c.get("cp") or "", "street": c.get("calle") or "",
                    "municipality": c.get("ciudad") or "", "state": c.get("estado") or "",
                    "country": "MEX"},
    }
    if c.get("regimeAlegra"):
        body["regime"] = c["regimeAlegra"]
        body["regimeObject"] = [c["regimeAlegra"]]
    body = {k: v for k, v in body.items() if v not in ("", None)}
    st, data = _req("POST", "contacts", body)
    if st in (200, 201) and isinstance(data, dict) and data.get("id"):
        return {"ok": True, "id": str(data["id"]), "data": data}
    return {"ok": False, "error": _msg(data), "status": st}


def _iva16_id() -> str:
    if "iva16" not in _cache:
        st, taxes = _req("GET", "taxes")
        _cache["iva16"] = next((str(t["id"]) for t in (taxes if isinstance(taxes, list) else [])
                                if t.get("type") == "IVA" and str(t.get("percentage")) in ("16", "16.00")
                                and "no acreditable" not in (t.get("name") or "").lower()), "")
    return _cache["iva16"]


def _tax_id_por_pct(pct: float) -> str:
    if abs(pct - 16) < 0.01:
        return _iva16_id()
    st, taxes = _req("GET", "taxes")
    for t in (taxes if isinstance(taxes, list) else []):
        if t.get("type") == "IVA" and abs(float(t.get("percentage") or -1) - pct) < 0.01 \
                and "no acreditable" not in (t.get("name") or "").lower():
            return str(t["id"])
    return ""


CLAVE_SAT_GENERICA = os.environ.get("ALEGRA_CLAVE_SAT_DEFAULT", "01010101")  # "No existe en el catálogo"


def buscar_o_crear_item(nombre: str, referencia: str, precio: float, tax_id: str,
                        clave_sat: str = "") -> dict:
    """Ítem de Alegra para una línea de la SO: se reusa por referencia (no. de parte) o nombre
    exacto; si no existe se crea. (La clave SAT del producto se completa en Alegra para timbrar.)"""
    for q in [x for x in (referencia, nombre) if x]:
        st, data = _req("GET", "items", params={"query": q[:60], "limit": 30})
        if st == 200 and isinstance(data, list):
            for it in data:
                if (referencia and _norm(it.get("reference") or "") == _norm(referencia)) or \
                        _norm(it.get("name") or "") == _norm(nombre):
                    return {"ok": True, "id": str(it["id"]), "nuevo": False}
    body = {"name": nombre[:150], "reference": (referencia or "")[:50], "price": precio,
            "productKey": clave_sat or CLAVE_SAT_GENERICA, "inventory": {"unit": "H87"}}
    if tax_id:
        body["tax"] = [{"id": tax_id}]
    body = {k: v for k, v in body.items() if v not in ("", None)}
    st, data = _req("POST", "items", body)
    if st in (200, 201) and isinstance(data, dict) and data.get("id"):
        return {"ok": True, "id": str(data["id"]), "nuevo": True}
    return {"ok": False, "error": _msg(data), "status": st}


# ─────────────────────────────────────────────────────────────
# 3. BORRADOR DESDE UNA SALES ORDER DE 1CRM (no escribe nada)
# ─────────────────────────────────────────────────────────────
def _codigo_moneda(currency_id: str) -> str:
    try:
        import compra_proveedor
        for cod, m in compra_proveedor._monedas_crm().items():
            if m["id"] == currency_id:
                return cod
    except Exception:
        pass
    return "MXN"


def _dias_credito(terminos: str) -> int:
    m = re.search(r"(\d+)", terminos or "")
    return int(m.group(1)) if m and "advance" not in (terminos or "").lower() else 0


# Datos de facturación del cliente (Excel "Clientes-Factura"): 1CRM no tiene campos para uso de CFDI /
# forma / método de pago, así que viven en un bloque legible dentro de la Descripción de la cuenta
# ([DATOS DE FACTURACIÓN] ... [/DATOS DE FACTURACIÓN]). Aquí se leen para armar la factura.
_USO_CFDI = {"adquisicion de mercancias": "G01", "gastos en general": "G03"}
_FORMA_PAGO = {"transferencia electronica de fondos": "transfer", "por definir": "other",
               "tarjeta de credito": "credit-card", "tarjeta de debito": "debit-card", "efectivo": "cash",
               "cheque nominativo": "check"}


def datos_facturacion(descripcion: str) -> dict:
    m = re.search(r"\[DATOS DE FACTURACI[ÓO]N\](.*?)\[/DATOS DE FACTURACI[ÓO]N\]", descripcion or "", re.S)
    if not m:
        return {}
    d = {}
    for ln in m.group(1).splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            d[sales_order._norm(k).strip().lower()] = v.strip()
    out = {"razon_social": d.get("razon social", ""), "metodo": (d.get("metodo de pago") or "").upper()}
    out["cfdiUse"] = _USO_CFDI.get(sales_order._norm(d.get("uso de cfdi", "")).strip().lower(), "")
    out["paymentMethod"] = _FORMA_PAGO.get(sales_order._norm(d.get("forma de pago", "")).strip().lower(), "")
    return out


def borrador_desde_so(so_id: str) -> dict:
    """Arma el borrador de la factura de venta de Alegra a partir de la SO de 1CRM, con avisos de
    todo lo que falte (RFC, régimen, etc.). NO escribe en Alegra ni en 1CRM."""
    so = sales_order._crm_get(f"data/SalesOrder/{so_id}").get("record", {})
    if not so.get("id"):
        return {"error": f"no encontré la Sales Order {so_id} en 1CRM"}
    acc = sales_order._crm_get(f"data/Account/{so.get('billing_account_id')}").get("record", {}) \
        if so.get("billing_account_id") else {}
    avisos: list[str] = []

    rfc = (acc.get("rfc") or "").strip().upper()
    regimen_1crm = acc.get("fiscal_regime") or ""
    regime_sat = _REGIMEN_SAT.get(regimen_1crm, "")
    if regimen_1crm == "Extranjero / Internacional" and not rfc:
        rfc = RFC_EXTRANJERO
    if not rfc:
        avisos.append("La cuenta del cliente no tiene RFC en 1CRM (campo RFC) — se necesita para timbrar.")
    if not regime_sat:
        avisos.append("La cuenta del cliente no tiene Régimen fiscal en 1CRM — se necesita para timbrar (CFDI 4.0).")
    cp = (acc.get("billing_address_postalcode") or "").strip()
    if not cp:
        avisos.append("La cuenta del cliente no tiene código postal fiscal en 1CRM — se necesita para timbrar.")

    lineas = []
    for li in so.get("line_items") or []:
        qty = sales_order._num(li.get("quantity")) or 0
        precio = sales_order._num(li.get("unit_price")) or 0
        if qty <= 0:
            continue
        lineas.append({
            "nombre": (li.get("name") or "Producto").strip(),
            "referencia": (li.get("mfr_part_no") or "").strip(),
            "cantidad": qty, "precio": precio,
            "iva_pct": sales_order._num(li.get("line_tax_perc")) or 0,
            "descuento_pct": sales_order._num(li.get("line_discount_perc")) or 0,
        })
    if not lineas:
        avisos.append("La Sales Order no tiene líneas.")
    sub = round(sum(l["cantidad"] * l["precio"] * (1 - l["descuento_pct"] / 100) for l in lineas), 2)
    iva = round(sum(l["cantidad"] * l["precio"] * (1 - l["descuento_pct"] / 100) * l["iva_pct"] / 100 for l in lineas), 2)

    moneda = _codigo_moneda(so.get("currency_id") or "")
    if moneda != "MXN":
        avisos.append(f"La Sales Order está en {moneda}; verifica que la cuenta de Alegra tenga multimoneda activa.")
    dias = _dias_credito(so.get("terms") or "")
    hoy = datetime.date.today()
    # Datos con los que SE LE FACTURA a este cliente (de su ficha); si no hay, se deduce de las condiciones.
    fact = datos_facturacion(acc.get("description") or "")
    pay_type = fact.get("metodo") if fact.get("metodo") in ("PUE", "PPD") else ("PUE" if dias == 0 else "PPD")
    pay_method = fact.get("paymentMethod") or ("transfer" if pay_type == "PUE" else "other")
    if pay_type == "PPD":
        pay_method = "other"          # Alegra: con PPD solo se admite "Por definir"
    elif pay_method == "other":
        pay_method = "transfer"       # PUE no puede ir "Por definir"
    cfdi_use = fact.get("cfdiUse") or "G03"
    if not fact:
        avisos.append("La ficha del cliente no trae sus datos de facturación (uso de CFDI, forma y método de pago); se usan valores por defecto.")
    cuenta = estado_cuenta()
    existente = buscar_cliente(rfc, acc.get("name") or "") if configurado() else None
    return {
        "so_id": so_id, "so_nombre": so.get("name"), "so_numero": so.get("so_number"),
        "so_url": f"{sales_order.CRM_BASE}/index.php?module=SalesOrders&action=DetailView&record={so_id}",
        "po_cliente": so.get("purchase_order_num") or "",
        "cliente": {"nombre": acc.get("name") or "", "rfc": rfc, "regimeClient": regime_sat,
                    "regimeAlegra": _REGIMEN_ALEGRA.get(regimen_1crm, ""),
                    "regimen_1crm": regimen_1crm, "email": acc.get("email1") or "", "cp": cp,
                    "calle": acc.get("billing_address_street") or "", "ciudad": acc.get("billing_address_city") or "",
                    "estado": acc.get("billing_address_state") or "",
                    "alegra_id": str(existente["id"]) if existente else ""},
        "moneda": moneda, "fecha": hoy.isoformat(),
        "vence": (hoy + datetime.timedelta(days=dias)).isoformat(), "terminos": so.get("terms") or "",
        # PUE = pago en una sola exhibición (contado); PPD = a crédito/diferido.
        "paymentType": pay_type, "paymentMethod": pay_method, "cfdiUse": cfdi_use,
        "facturacion_de_ficha": bool(fact),
        "lineas": lineas, "subtotal": sub, "iva": iva, "total": round(sub + iva, 2),
        "avisos": avisos, "timbrable": bool(not avisos and cuenta.get("timbrado_listo")),
        "cuenta_alegra": cuenta,
    }


# ─────────────────────────────────────────────────────────────
# 4. ESCRITURA EN ALEGRA (tras aprobación)
# ─────────────────────────────────────────────────────────────
def crear_factura(draft: dict, timbrar: bool = False) -> dict:
    """Crea la factura de venta en Alegra: cliente (se reusa o se da de alta), ítems y factura.
    Sin `timbrar` queda como BORRADOR (reversible: se puede eliminar). Con `timbrar`, Alegra la
    timbra con CFDI 4.0; si el timbrado falla, queda en borrador y se devuelve el error."""
    if not configurado():
        return {"ok": False, "error": "Alegra no está configurado (ALEGRA_EMAIL / ALEGRA_TOKEN)."}
    cli = draft.get("cliente") or {}
    cliente_id = cli.get("alegra_id") or ""
    cliente_nuevo = False
    if not cliente_id:
        ex = buscar_cliente(cli.get("rfc", ""), cli.get("nombre", ""))
        if ex:
            cliente_id = str(ex["id"])
        else:
            r = crear_cliente(cli)
            if not r.get("ok"):
                return {"ok": False, "error": f"No pude dar de alta al cliente en Alegra: {r.get('error')}"}
            cliente_id, cliente_nuevo = r["id"], True

    items = []
    for ln in draft.get("lineas") or []:
        tax_id = _tax_id_por_pct(float(ln.get("iva_pct") or 0)) if ln.get("iva_pct") else ""
        it = buscar_o_crear_item(ln["nombre"], ln.get("referencia", ""), float(ln["precio"]), tax_id,
                                 ln.get("clave_sat", ""))
        if not it.get("ok"):
            return {"ok": False, "error": f"No pude crear el producto «{ln['nombre'][:50]}» en Alegra: {it.get('error')}"}
        row = {"id": it["id"], "name": ln["nombre"][:150], "price": float(ln["precio"]),
               "quantity": float(ln["cantidad"])}
        if ln.get("referencia"):
            row["reference"] = ln["referencia"][:50]
        if tax_id:
            row["tax"] = [{"id": tax_id}]
        if ln.get("descuento_pct"):
            row["discount"] = float(ln["descuento_pct"])
        items.append(row)

    notas = f"Sales Order {draft.get('so_numero') or ''}".strip()
    if draft.get("po_cliente"):
        notas += f" · PO cliente {draft['po_cliente']}"
    body = {
        "date": draft.get("fecha") or datetime.date.today().isoformat(),
        "dueDate": draft.get("vence") or draft.get("fecha") or datetime.date.today().isoformat(),
        "client": {"id": cliente_id}, "items": items,
        "paymentType": draft.get("paymentType", "PUE"), "paymentMethod": draft.get("paymentMethod", "transfer"),
        "cfdiUse": draft.get("cfdiUse", "G03"),
        "observations": notas, "anotation": notas,
    }
    if cli.get("regimeClient"):
        body["regimeClient"] = cli["regimeClient"]
    if timbrar:
        body["status"] = "open"
        body["stamp"] = {"generateStamp": True, "version": "4.0"}
    else:
        body["status"] = "draft"
    st, data = _req("POST", "invoices", body)
    inv = data.get("invoice") if isinstance(data, dict) and isinstance(data.get("invoice"), dict) else data
    if st in (200, 201) and isinstance(inv, dict) and inv.get("id"):
        return {"ok": True, **_resumen_factura(inv), "timbrada": bool(timbrar),
                "cliente_nuevo": cliente_nuevo, "so_id": draft.get("so_id")}
    if isinstance(inv, dict) and inv.get("id"):  # timbrado falló: la factura quedó en borrador
        return {"ok": True, **_resumen_factura(inv), "timbrada": False, "cliente_nuevo": cliente_nuevo,
                "so_id": draft.get("so_id"), "aviso": f"No se pudo timbrar, quedó en borrador: {_msg(data.get('error') if isinstance(data, dict) else data)}"}
    return {"ok": False, "error": _msg(data), "status": st}


def _resumen_factura(inv: dict) -> dict:
    nt = inv.get("numberTemplate")
    nt = nt[0] if isinstance(nt, list) and nt else (nt if isinstance(nt, dict) else {})
    return {"invoice_id": str(inv.get("id")), "estado": inv.get("status"),
            "numero": nt.get("fullNumber") or nt.get("number") or "", "total": inv.get("total"),
            "url": f"{APP_URL}/invoice/view/id/{inv.get('id')}",
            "cliente": (inv.get("client") or {}).get("name", "")}


def eliminar_borrador(invoice_id: str) -> dict:
    """Deshacer: elimina una factura que sigue en borrador. Una timbrada NO se borra (se cancela
    en Alegra siguiendo el proceso del SAT)."""
    st, inv = _req("GET", f"invoices/{invoice_id}")
    if st != 200 or not isinstance(inv, dict):
        return {"ok": False, "error": _msg(inv)}
    if inv.get("status") != "draft":
        return {"ok": False, "error": f"La factura está «{inv.get('status')}»; solo se eliminan borradores. Si ya está timbrada, cancélala en Alegra."}
    st, data = _req("DELETE", f"invoices/{invoice_id}")
    return {"ok": st in (200, 204), "error": "" if st in (200, 204) else _msg(data)}


# ─────────────────────────────────────────────────────────────
# 5. ESTATUS Y PAGOS ← ALEGRA
# ─────────────────────────────────────────────────────────────
def estado_factura(invoice_id: str) -> dict:
    st, inv = _req("GET", f"invoices/{invoice_id}")
    if st != 200 or not isinstance(inv, dict):
        return {"ok": False, "error": _msg(inv)}
    total, pagado = float(inv.get("total") or 0), float(inv.get("totalPaid") or 0)
    return {"ok": True, "invoice_id": str(inv.get("id")), "estado": inv.get("status"),
            "total": total, "pagado": pagado, "saldo": round(total - pagado, 2),
            "uuid": ((inv.get("stamp") or {}).get("uuid") if isinstance(inv.get("stamp"), dict) else None),
            "url": f"{APP_URL}/invoice/view/id/{inv.get('id')}"}


def facturas_con_movimiento(limite: int = 30) -> list[dict]:
    """Facturas recientes de Alegra con su estatus y lo cobrado (para reflejar pagos en 1CRM)."""
    st, data = _req("GET", "invoices", params={"limit": limite, "order_direction": "DESC", "order_field": "id"})
    if st != 200 or not isinstance(data, list):
        return []
    out = []
    for inv in data:
        total, pagado = float(inv.get("total") or 0), float(inv.get("totalPaid") or 0)
        out.append({"invoice_id": str(inv.get("id")), "estado": inv.get("status"), "total": total,
                    "pagado": pagado, "saldo": round(total - pagado, 2),
                    "cliente": (inv.get("client") or {}).get("name", ""),
                    "observaciones": inv.get("observations") or ""})
    return out

# ─────────────────────────────────────────────────────────────
# 6. ESPEJO EN 1CRM — la Invoice (cuenta por cobrar) ligada a la Sales Order
# Verificado en vivo (2026-10-28): POST data/Invoice con from_so_id AUTO-COPIA las líneas y los montos
# de la SO (igual que related_quote_id en SalesOrder). DELETE no existe para Invoice (404): deshacer
# = PATCH cancelled=true. OJO: Invoice.amount_due no se recalcula solo (ver pagos.py).
# ─────────────────────────────────────────────────────────────
_TERMS_1CRM = {"net 7": "Net 7", "net 15": "Net 15", "net 30": "Net 30", "net 45": "Net 45", "net 60": "Net 60",
               "net 90": "Net 90", "cod": "COD", "due on receipt": "Due on Receipt"}


def crear_espejo_1crm(draft: dict, res: dict) -> dict:
    """Crea en 1CRM la Invoice ligada a la SO con la referencia de la factura de Alegra."""
    so_id = draft.get("so_id")
    if not so_id:
        return {"ok": False, "error": "sin so_id"}
    so = sales_order._crm_get(f"data/SalesOrder/{so_id}").get("record", {})
    if not so.get("id"):
        return {"ok": False, "error": "no encontré la Sales Order"}
    terms_so = (so.get("terms") or "").replace("_", " ").replace(" Days", "").strip().lower()
    terms = _TERMS_1CRM.get(terms_so, "Net 30" if _dias_credito(terms_so) else "Due on Receipt")
    desc = (f"Alegra ID: {res.get('invoice_id')}\nAlegra folio: {res.get('numero') or '(borrador)'}\n"
            f"Alegra: {res.get('url')}\nEstado en Alegra: {res.get('estado')}")
    payload = {"name": so.get("name"), "billing_account_id": so.get("billing_account_id"),
               "currency_id": so.get("currency_id"), "from_so_id": so_id,
               "invoice_date": draft.get("fecha") or datetime.date.today().isoformat(),
               "due_date": draft.get("vence") or datetime.date.today().isoformat(),
               "terms": terms, "purchase_order_num": so.get("purchase_order_num") or "", "description": desc}
    r = sales_order._crm_post("Invoice", payload)
    iid = r.get("id")
    if not iid:
        return {"ok": False, "error": f"no se pudo crear la Invoice en 1CRM: {str(r)[:200]}"}
    g = sales_order._crm_get(f"data/Invoice/{iid}").get("record", {})
    return {"ok": True, "id": iid, "numero": g.get("invoice_number"), "total": sales_order._num(g.get("amount")),
            "lineas": len(g.get("line_items") or []),
            "url": f"{sales_order.CRM_BASE}/index.php?module=Invoice&action=DetailView&record={iid}"}


def cancelar_espejo_1crm(invoice_id: str) -> dict:
    """Deshacer del espejo: 1CRM no deja borrar Invoices por API, se marca cancelada."""
    r = sales_order._crm_patch("Invoice", invoice_id, {"cancelled": True})
    return {"ok": "error" not in r and r.get("result", True) is not False}


def anotar_espejo_1crm(invoice_id: str, linea: str) -> None:
    """Agrega una línea al historial (descripción) de la Invoice espejo."""
    try:
        d = sales_order._crm_get(f"data/Invoice/{invoice_id}").get("record", {})
        sales_order._crm_patch("Invoice", invoice_id, {"description": ((d.get("description") or "").rstrip() + "\n" + linea).strip()})
    except Exception as e:
        log.warning(f"anotar_espejo_1crm: {e}")
