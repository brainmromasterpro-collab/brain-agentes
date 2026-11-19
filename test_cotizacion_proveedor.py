"""Una cotización/proforma de proveedor subida al stream de compras entra al cotejo contra Sales Orders abiertas (y de ahí al
PO para ese proveedor), en vez de tomarse por comprobante de pago (caso real: «Revised PI.pdf», ELECTRAPOTHIKI, 1,335 EUR)."""
import agente_chat as ac
import compra_proveedor as cp
import sales_order as so

# ── 1) Enrutamiento: tipo cotizacion_proveedor → [COTEJO_PROVEEDOR] ──────────────────────────────
INSERTS = []


class _T:
    def insert(self, row):
        INSERTS.append(row)
        return self

    def execute(self):
        return self


class _SB:
    def table(self, n):
        return _T()


ac.supabase = _SB()
ac._bajar_imagenes = lambda *a, **k: [b"img"]
ac._log_stream = lambda *a, **k: None
DATOS = {"tipo": "cotizacion_proveedor", "tienda": "ELECTRAPOTHIKI S.A.", "orden_numero": "PO2026-0828-235", "moneda": "EUR",
         "total": 1335.0, "terminos_pago": "100% in advance (T/T in advance)",
         "items": [{"nombre": "Servo Motor 2kW", "marca": "", "part_number": "CSMD-20BT3ATT3", "cantidad": 1, "precio_unitario": 1335.0}]}
cp.leer_captura_compra = lambda imgs, model_id="": dict(DATOS)
VISTO = {}
cp.buscar_sales_orders_candidatas = lambda links: VISTO.setdefault("links", links) and {"ok": True, "grupos": [], "resumen": "ok"}

assert ac._procesar_captura_compra("S1", "http://x/pi.pdf", "Revised PI.pdf", "application/pdf") is True
assert INSERTS and INSERTS[-1]["content"].startswith("[COTEJO_PROVEEDOR]"), INSERTS
l0 = VISTO["links"][0]
assert l0["proveedor"] == "ELECTRAPOTHIKI S.A." and l0["part_number"] == "CSMD-20BT3ATT3" and l0["moneda"] == "EUR", l0
assert l0["terminos_documento"] == "100% in advance (T/T in advance)", l0

# un comprobante de pago sigue yendo a su flujo (el cotejo de compras NO lo atiende)
INSERTS.clear()
cp.leer_captura_compra = lambda imgs, model_id="": {"tipo": "comprobante_pago", "items": []}
assert ac._procesar_captura_compra("S1", "http://x/c.pdf", "c.pdf", "application/pdf") is False and not INSERTS

# ── 1b) Se identifica por EVIDENCIA, no por el tipo que diga el lector ───────────────────────────
ITEMS2 = [{"nombre": "Sensor A", "part_number": "AAA-111", "cantidad": 2, "precio_unitario": 10.0},
          {"nombre": "Sensor B", "part_number": "BBB-222", "cantidad": 1, "precio_unitario": 20.0}]
# lector dudoso («otro») pero con productos y precios → es algo que se compra → cotejo (con 2 productos)
INSERTS.clear(); VISTO.clear()
cp.leer_captura_compra = lambda imgs, model_id="": {"tipo": "otro", "tienda": "Acme", "moneda": "USD", "items": ITEMS2}
assert ac._procesar_captura_compra("S1", "u", "x.pdf", "application/pdf") is True and len(VISTO["links"]) == 2, VISTO
# «otro» sin productos → no lo atiende (el router pregunta)
cp.leer_captura_compra = lambda imgs, model_id="": {"tipo": "otro", "items": []}
assert ac._procesar_captura_compra("S1", "u", "x.pdf", "application/pdf") is False
# packing/tracking nunca se toma por compra aunque traiga renglones
cp.leer_captura_compra = lambda imgs, model_id="": {"tipo": "packing_tracking", "items": ITEMS2}
assert ac._procesar_captura_compra("S1", "u", "x.pdf", "application/pdf") is False
# el usuario dijo «es una cotización» → se procesa aunque el lector dudara; sin productos avisa y no se queda callado
cp.leer_captura_compra = lambda imgs, model_id="": {"tipo": "otro", "items": ITEMS2}
VISTO.clear(); assert ac._procesar_captura_compra("S1", "u", "x.pdf", "application/pdf", forzar=True) is True and VISTO.get("links")
INSERTS.clear()
cp.leer_captura_compra = lambda imgs, model_id="": {"tipo": "otro", "items": []}
assert ac._procesar_captura_compra("S1", "u", "x.pdf", "application/pdf", forzar=True) is True and "no encontré productos" in INSERTS[-1]["content"]

# Router del stream de compras: cada tipo a su flujo; lo dudoso se pregunta (ya NO cae a «comprobante de pago»)
LLAMADAS = []
for nombre_fn in ("_procesar_comprobante_pago", "_procesar_evidencia_receiving", "_preguntar_tipo_archivo", "_procesar_link_proveedor"):
    setattr(ac, nombre_fn, (lambda n: (lambda *a, **k: LLAMADAS.append(n)))(nombre_fn))


def enviar(tipo_lector, contenido="Archivo subido: doc.pdf", archivo=True, items=None):
    LLAMADAS.clear()
    ac._leer_archivo_compras = lambda *a, **k: ({"error": "x"} if tipo_lector == "error" else {"tipo": tipo_lector, "items": items or []})
    md = {"file_url": "http://x/doc.pdf", "file_name": "doc.pdf", "file_mime": "application/pdf"} if archivo else {}
    ac._manejar_compras({}, "S1", contenido, md)
    return list(LLAMADAS)


assert enviar("comprobante_pago") == ["_procesar_comprobante_pago"]
assert enviar("packing_tracking") == ["_procesar_evidencia_receiving"]
assert enviar("otro") == ["_preguntar_tipo_archivo"], "lo dudoso se pregunta, no se asume pago"
assert enviar("error") == ["_preguntar_tipo_archivo"]
INSERTS.clear()
assert enviar("otro", items=ITEMS2) == [] and INSERTS[-1]["content"].startswith("[COTEJO_PROVEEDOR]"), "con productos y precios → cotejo, sin pago/recepción/pregunta"
# respuesta del botón → usa el último archivo subido
ac._ultimo_archivo_stream = lambda sid: {"file_url": "http://x/doc.pdf", "file_name": "doc.pdf", "file_mime": "application/pdf"}
assert enviar(None, contenido=ac._OPC_PAGO, archivo=False) == ["_procesar_comprobante_pago"]
assert enviar(None, contenido=ac._OPC_RECEP, archivo=False) == ["_procesar_evidencia_receiving"]

# ── 2) Términos del documento al crear el PO ─────────────────────────────────────────────────────
REG = {}
so._crm_post = lambda m, d: REG.setdefault((m, f"{m}-1"), dict(d)) and {"id": f"{m}-1"}
so._crm_get = lambda p, params=None: ({"record": dict(REG.get((p.split('/')[1], p.split('/')[2]), {}))} if p.count('/') == 2 else {"records": []})
so._crm_patch = lambda m, i, d: REG[(m, i)].update(d) or {}
cp.CRM_BASE = "https://x"
cp._buscar_o_crear_proveedor = lambda n: "SUP-1"
cp._crear_lineas_po = lambda *a, **k: 1
cp._terminos_proveedor = lambda n: ""      # proveedor nuevo: sin términos en 1CRM


def borrador(**extra):
    return {"so_id": "SO-1", "proveedor_nombre": "ELECTRAPOTHIKI S.A.", "currency_id": "EUR",
            "lineas": [{"name": "Servo", "mfr_part_no": "CSMD-20BT3ATT3", "quantity": 1, "unit_price": 1335}], **extra}


# a) el documento trae términos y el proveedor es el del documento → se crea con «100% Advance»
r = cp.crear_po_y_ap(borrador(terminos_documento="100% in advance (T/T in advance)", proveedor_documento="ELECTRAPOTHIKI S.A."))
assert r.get("ok") and r["terms"] == "100% Advance" and REG[("PurchaseOrder", "PurchaseOrder-1")]["terms"] == "100% Advance", (r, REG)

# b) el usuario cambió el proveedor en el widget → los términos del documento NO se arrastran
REG.clear()
r = cp.crear_po_y_ap(borrador(proveedor_nombre="Otro SA", terminos_documento="100% in advance", proveedor_documento="ELECTRAPOTHIKI S.A."))
assert "error" in r and not REG, r

# c) ni 1CRM ni el documento traen términos → se sigue exigiendo (regla de Gabriel)
r = cp.crear_po_y_ap(borrador(terminos_documento="", proveedor_documento="ELECTRAPOTHIKI S.A."))
assert "error" in r and not REG, r

# d) si el proveedor SÍ tiene términos en 1CRM, mandan los de 1CRM
cp._terminos_proveedor = lambda n: "Net 30"
r = cp.crear_po_y_ap(borrador(terminos_documento="100% in advance", proveedor_documento="ELECTRAPOTHIKI S.A."))
assert r.get("ok") and r["terms"] == "Net 30", r
# ── 3) El proveedor existe en el CRM con otro nombre → se propone la cuenta existente ───────────────
CRM_PROV = ["ELEKTRAPOTHEKA OF THESSALONIKI", "eBay", "Grainger", "Rockwell Automation México", "Rockwell Automation Mexico SA"]
so._crm_get = lambda p, params=None: {"records": [{"id": f"A{i}", "name": n} for i, n in enumerate(CRM_PROV)]}
assert cp.proveedor_en_crm("ELECTRAPOTHIKI S.A. THESSALONIKI") == "ELEKTRAPOTHEKA OF THESSALONIKI"
assert cp.proveedor_en_crm("EBAY") == "eBay"                                  # exacto sin importar mayúsculas
assert cp.proveedor_en_crm("Proveedor Inventado XYZ 123") == ""               # nada parecido → no inventa
assert cp.proveedor_en_crm("Rockwell Automation") == "", "dos parecidos sin un claro ganador → no adivina"
assert cp.proveedor_en_crm("") == ""
print("OK test_cotizacion_proveedor")
