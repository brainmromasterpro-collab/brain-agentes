"""Archivos del stream de compras: se identifican por EVIDENCIA y, si no hay, se PREGUNTA (nunca un default silencioso).
Casos reales: «Revised PI.pdf» (proforma de ELECTRAPOTHIKI → debe ir a cotejo con SO, no a pago) y un pedido de eBay (tornillos
Kmac, sin coincidencia con ningún PO ni SO → debe preguntar, no abrir «tracking» con POs al azar)."""
import agente_chat as ac
import compra_proveedor as cp
import sales_order as so

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

ITEM_PI = {"nombre": "Servo Motor 2kW", "marca": "", "part_number": "CSMD-20BT3ATT3", "cantidad": 1, "precio_unitario": 1335.0}
ITEMS2 = [{"nombre": "Sensor A", "part_number": "AAA-111", "cantidad": 2, "precio_unitario": 10.0},
          {"nombre": "Sensor B", "part_number": "BBB-222", "cantidad": 1, "precio_unitario": 20.0}]
KMAC = {"nombre": "Kmac Alloy Steel Set Screws, Brass Tip, 5/16-18 x 1/2 Thread Length, 20 Pcs", "part_number": "111730686870",
        "cantidad": 4, "precio_unitario": 20.0}

POS = []          # POs en tránsito (con sus líneas)
GRUPOS = []       # resultado del cotejo contra SO abiertas
cp.pos_esperando_recepcion = lambda *a, **k: list(POS)
cp.buscar_sales_orders_candidatas = lambda links: {"ok": True, "grupos": list(GRUPOS), "sin_match": [], "resumen": "ok"}


def destino(tipo, items, tienda="Acme", **extra):
    return ac._destino_archivo_compras({"tipo": tipo, "tienda": tienda, "items": items, **extra})


# ── 1) Cada tipo a su flujo ──────────────────────────────────────────────────────────────────────
assert destino("cotizacion_proveedor", [ITEM_PI])[0] == "cotejo", "la proforma va a cotejo (caso real Revised PI.pdf)"
assert destino("compra_online", ITEMS2)[0] == "cotejo"
assert destino("comprobante_pago", [])[0] == "pago"
assert destino("packing_tracking", [])[0] == "recepcion"
assert ac._destino_archivo_compras({"error": "x"}) == ("preguntar", {"motivo": "sin_evidencia"})
assert destino("otro", []) == ("preguntar", {"motivo": "sin_evidencia"}), "lo dudoso y sin productos se pregunta"

# otro con productos+precio y coincidencia en una SO → cotejo; con 2 productos se mandan los 2 al cotejo
GRUPOS[:] = [{"so_numero": "309", "ya_comprado": None}]
assert destino("otro", ITEMS2)[0] == "cotejo"

# ── 2) Caso de hoy: pedido de eBay (order_confirmation) sin coincidencia con ningún PO ni SO → PREGUNTA ─────
GRUPOS[:] = []
POS[:] = [{"id": "PO1", "nombre": "CSMD — Compra", "lineas": [{"name": "Servo Motor 2kW, 220VAC", "mfr_part_no": "CSMD-20BT3ATT3"}]},
          {"id": "PO2", "nombre": "TEST BRAIN MOCK", "lineas": [{"name": "1N4001 Rectifier Diode", "mfr_part_no": "1N4001"}]}]
d, info = destino("order_confirmation", [KMAC], tienda="eBay")
assert d == "preguntar" and info["motivo"] == "ninguno", (d, info)

# ── 3) Coincide con un PO en tránsito (y sus SO ya tienen compra) → recepción ──────────────────────────
POS[:] = [{"id": "PO1", "nombre": "x", "lineas": [{"name": "Kmac Alloy Steel Set Screws, Brass Tip, 5/16-18 x 1/2 Thread Length, 20 Pcs",
                                                    "mfr_part_no": ""}]}]
GRUPOS[:] = [{"so_numero": "300", "ya_comprado": {"id": "PO1"}}]
assert destino("order_confirmation", [KMAC], tienda="eBay")[0] == "recepcion"
# ... pero si además hay una venta sin comprar con ese producto → hay dos lecturas posibles → pregunta
GRUPOS[:] = [{"so_numero": "300", "ya_comprado": {"id": "PO1"}}, {"so_numero": "311", "ya_comprado": None}]
d, info = destino("order_confirmation", [KMAC], tienda="eBay")
assert d == "preguntar" and info["motivo"] == "ambos", (d, info)
# una confirmación que coincide con una SO sin compra y con ningún PO → cotejo para crear el PO
POS[:] = []
GRUPOS[:] = [{"so_numero": "311", "ya_comprado": None}]
assert destino("order_confirmation", [KMAC], tienda="eBay")[0] == "cotejo"

# ── 4) El widget de cotejo se emite con proveedor, moneda, términos y todos los productos ────────────────
VISTO = {}
cp.buscar_sales_orders_candidatas = lambda links: VISTO.setdefault("links", links) and {"ok": True, "grupos": [], "resumen": "ok"}
DATOS = {"tipo": "cotizacion_proveedor", "tienda": "ELECTRAPOTHIKI S.A.", "orden_numero": "PO2026-0828-235", "moneda": "EUR",
         "total": 1335.0, "terminos_pago": "100% in advance (T/T in advance)", "items": [ITEM_PI]}
assert ac._procesar_captura_compra("S1", "http://x/pi.pdf", "Revised PI.pdf", "application/pdf", datos=DATOS) is True
assert INSERTS[-1]["content"].startswith("[COTEJO_PROVEEDOR]")
l0 = VISTO["links"][0]
assert l0["proveedor"] == "ELECTRAPOTHIKI S.A." and l0["part_number"] == "CSMD-20BT3ATT3" and l0["moneda"] == "EUR", l0
assert l0["terminos_documento"] == "100% in advance (T/T in advance)", l0
VISTO.clear()
assert ac._procesar_captura_compra("S1", "u", "x.pdf", "application/pdf", datos={"tipo": "otro", "tienda": "Acme", "moneda": "USD", "items": ITEMS2}) is True
assert len(VISTO["links"]) == 2, "con 2 productos se cotejan los 2"
INSERTS.clear()
assert ac._procesar_captura_compra("S1", "u", "x.pdf", "application/pdf", datos={"tipo": "otro", "items": []}) is True
assert "No encontré productos" in INSERTS[-1]["content"]

# ── 5) Router del stream: cada destino llama a su flujo; lo dudoso pregunta y NO cae a pago ─────────────
_REAL_PREGUNTAR = ac._preguntar_tipo_archivo
LLAMADAS = []
for nombre_fn in ("_procesar_comprobante_pago", "_procesar_evidencia_receiving", "_preguntar_tipo_archivo", "_procesar_link_proveedor"):
    setattr(ac, nombre_fn, (lambda n: (lambda *a, **k: LLAMADAS.append(n)))(nombre_fn))


def enviar(destino_fijo, contenido="Archivo subido: doc.pdf", archivo=True):
    LLAMADAS.clear()
    ac._leer_archivo_compras = lambda *a, **k: {"tipo": "x", "items": []}
    ac._destino_archivo_compras = lambda datos: (destino_fijo, {"motivo": "ninguno"} if destino_fijo == "preguntar" else None)
    md = {"file_url": "http://x/doc.pdf", "file_name": "doc.pdf", "file_mime": "application/pdf"} if archivo else {}
    ac._manejar_compras({}, "S1", contenido, md)
    return list(LLAMADAS)


assert enviar("pago") == ["_procesar_comprobante_pago"]
assert enviar("recepcion") == ["_procesar_evidencia_receiving"]
assert enviar("preguntar") == ["_preguntar_tipo_archivo"], "lo dudoso se pregunta, no se asume pago"
# respuesta del botón → usa el último archivo subido
ac._ultimo_archivo_stream = lambda sid: {"file_url": "http://x/doc.pdf", "file_name": "doc.pdf", "file_mime": "application/pdf"}
assert enviar(None, contenido=ac._OPC_PAGO, archivo=False) == ["_procesar_comprobante_pago"]
assert enviar(None, contenido=ac._OPC_RECEP, archivo=False) == ["_procesar_evidencia_receiving"]
INSERTS.clear()
ac._leer_archivo_compras = lambda *a, **k: dict(DATOS)
LLAMADAS.clear(); ac._manejar_compras({}, "S1", ac._OPC_COTIZ, {})
assert INSERTS and INSERTS[-1]["content"].startswith("[COTEJO_PROVEEDOR]") and not LLAMADAS, "«es una compra» → cotejo del último archivo"

# la pregunta lleva botones y dice por qué
import json as _j
INSERTS.clear()
_REAL_PREGUNTAR("S1", {"tipo": "order_confirmation", "tienda": "eBay", "items": [KMAC]}, "image.png", "ninguno")
c = INSERTS[-1]["content"]
assert "ninguna Sales Order abierta" in c and "1 producto(s)" in c, c
assert set(_j.loads(c.split("[OPCIONES]")[1])["opciones"]) == {ac._OPC_COTIZ, ac._OPC_RECEP, ac._OPC_PAGO}, c
INSERTS.clear()
_REAL_PREGUNTAR("S1", None, "doc.pdf", "ambos")
assert "ya está en tránsito" in INSERTS[-1]["content"] and "[OPCIONES]" in INSERTS[-1]["content"]

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
