"""Cotejo PO ↔ cotización cuando el cliente usa SU código interno (PIN.140242) y el modelo real (CSMD-20BT3ATT3) viene en
la descripción del PO: con el precio igual cuenta como coincidencia de parte y precio — no es «ambiguo» ni «Verificar»."""
import agente_chat  # carga el entorno
import sales_order as so


def linea(parte, desc, precio, qty=1):
    return {"part_number": parte, "part_compact": so._compact(parte), "descripcion": desc, "quantity": qty, "unit_price": precio}


def cot(id_, nombre, lineas, cerrada=True):
    return {"id": id_, "nombre": nombre, "lines": lineas, "cerrada": cerrada, "amount": 0, "cuenta_id": "ACC"}


QUOTES = []
so.CRM_BASE = "https://x"
so.buscar_cuenta = lambda n: {"id": "ACC", "nombre": "Markdom"}
so.cotizacion_por_ref = lambda r: None
so.so_existente_por_po = lambda *a, **k: None
so._terminos_y_moneda = lambda c: {"terminos_pago": "Net 30", "terminos_so": "Net 30", "moneda": "MXN", "currency_id": "C"}
so._es_para_nosotros = lambda p: True
so.cotizaciones_cliente = lambda c: list(QUOTES)
so.cotizaciones_ocultas_ui = lambda *a, **k: []
so.ventas_previas = lambda *a, **k: []
VIG = {"vigente": False, "motivo": "vencida el 2026-06-08", "quote_stage": "Closed Accepted", "valid_until": "2026-06-08"}
so._vigencia = lambda qid: dict(VIG)

PO = {"po_number": "8992", "cliente": "Markdom", "proveedor": "MRO", "moneda": "MXN", "items": [
    {"part_number": "PIN.140242", "descripcion": "CSMD-20BT3ATT3 RS Automation 2kW Servo Motor 220VAC", "cantidad": 1, "precio_unitario": 57966.55}]}

# 1) Caso real: una sola cotización, modelo real en la descripción, precio igual, cotización vencida
QUOTES[:] = [cot("Q1", "CSMD-10BT3AST3 CSMD-20BT3ATT3", [
    linea("CSMD-10BT3AST3", "RS Automation 1kW Servo", 30000), linea("CSMD-20BT3ATT3", "RS Automation 2kW Servo Motor 220VAC", 57966.55),
    linea("CSD7-25BXF1-Y", "Drive", 61000)])]
r = so.cotejar(PO)
it = r["items"][0]
assert it["estado"] == "ok" and it["tipo_match"] == "descripcion" and not it["match_parcial"], it
assert r["ambiguo"] is False, "una sola cotización que coincide en parte y precio no es ambigua"
assert "Ninguna cotización coincide" not in r["resumen"], r["resumen"]
assert not any("código interno" in d for d in r["discrepancias"]), r["discrepancias"]
d = r["so_draft"]
assert d and d["quote_id"] == "Q1", d
assert d["puede_crear"] is True and d["aviso_vigencia"] is True, "vencida: solo advierte (el previo y la confirmación siguen)"
assert [l["pedido"] for l in d["lineas"]] == [False, True, False], d["lineas"]
assert r["cotizaciones_candidatas"][0]["coincidencia"] == "exacta", r["cotizaciones_candidatas"][0]

# 2) Mismo caso pero el precio del PO NO coincide → no es firme: se pide confirmar y no se arma borrador solo
PO2 = {**PO, "items": [{**PO["items"][0], "precio_unitario": 50000}]}
r = so.cotejar(PO2)
assert r["ambiguo"] is True and r["so_draft"] is None, (r["ambiguo"], r["so_draft"])

# 3) Dos cotizaciones con la misma parte y precio → sí hay que elegir
QUOTES.append(cot("Q2", "Otra cotización", [linea("CSMD-20BT3ATT3", "RS Automation 2kW Servo Motor 220VAC", 57966.55)]))
r = so.cotejar(PO)
assert r["ambiguo"] is True and "más de una cotización" in r["resumen"], r["resumen"]

# 4) Parte exacta + precio igual en una sola cotización vigente → todo_ok como siempre
VIG.update(vigente=True, motivo="")
QUOTES[:] = [cot("Q3", "X", [linea("ABC-123", "Sensor", 100)], cerrada=False)]
r = so.cotejar({**PO, "items": [{"part_number": "ABC-123", "descripcion": "Sensor", "cantidad": 2, "precio_unitario": 100}]})
assert r["todo_ok"] and not r["ambiguo"] and r["so_draft"]["puede_crear"] is True, r
print("OK test_cotejo_codigo_interno")
