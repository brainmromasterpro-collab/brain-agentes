"""Pruebas del guardado de documentos en 1CRM. Offline por defecto; `python3 test_crm_documentos.py --live` además sube un
PDF de prueba a 1CRM real (lo liga a una Sales Order existente, verifica archivo+relación+duplicado y LO BORRA)."""
import sys, json
import agente_chat as ac
import crm_documentos as cd

_fallos = []
def check(nombre, ok, detalle=""):
    print(("  ✅ " if ok else "  ❌ ") + nombre + ("" if ok else f" — {detalle}"))
    if not ok: _fallos.append(nombre)

print("Nombres de archivo y de documento")
check("quita el timestamp del frontend", cd.nombre_archivo("https://x/y/1731012345678-PO 8992.pdf") == "PO 8992.pdf")
check("limpia caracteres raros", "/" not in cd.nombre_archivo("https://x/a%2Fb.pdf"))
check("nombre del documento lleva etiqueta, referencia y archivo",
      cd.nombre_documento("orden_compra_cliente", "PO 8992 · SO2026-1", "oc.pdf") == "Orden de compra del cliente · PO 8992 · SO2026-1 · oc")
check("todos los tipos apuntan a un módulo válido", all(m in cd.PARENTS_VALIDOS for _, m in cd.TIPOS.values()))

print("Sin registro destino no se guarda nada")
r = cd.guardar_documento(tipo="guia_envio", contenido=b"x")
check("falta parent_id → error claro, sin tocar 1CRM", r.get("ok") is False and "falta el registro" in r.get("error", ""), str(r))

print("Cada acción del chat dispara el guardado ligado al registro correcto")
llamadas = []
orig = ac._guardar_doc_crm
ac._guardar_doc_crm = lambda stream_id, tipo, file_url, parent_type, parent_id, referencia="", detalle="", nombre_sugerido="": \
    llamadas.append((tipo, file_url, parent_type, parent_id, referencia))

class _Res:
    def __init__(self, data): self.data = data
class _Q:
    def __init__(self, bd, t): self.bd, self.t, self.op = bd, t, "select"
    def select(self, *a, **k): self.op = "select"; return self
    def insert(self, p): self.op = "insert"; self.bd.insertados.append((self.t, p)); return self
    def update(self, p): self.op = "update"; return self
    def __getattr__(self, n): return lambda *a, **k: self
    def execute(self):
        if self.op == "select" and self.t == "mensajes": return _Res(self.bd.mensajes)
        return _Res([])
class _BD:
    def __init__(self): self.insertados, self.mensajes = [], []
    def table(self, t): return _Q(self, t)
orig_sb = ac.supabase
try:
    bd = _BD(); ac.supabase = bd
    import sales_order, pagos, sales_shipping, compra_proveedor
    # PO del cliente → SalesOrders
    o1 = sales_order.crear_sales_order; sales_order.crear_sales_order = lambda d: {"ok": True, "so_id": "SO-ID", "so_numero": "SO2026-1"}
    ac._crear_so_confirmada("S1", {"file_url": "https://x/po.pdf", "po_number": "8992", "cliente_nombre": "Acme"})
    sales_order.crear_sales_order = o1
    check("PO del cliente → Sales Order", llamadas and llamadas[-1][:4] == ("orden_compra_cliente", "https://x/po.pdf", "SalesOrders", "SO-ID") and "8992" in llamadas[-1][4], str(llamadas))
    # comprobante de pago → Payments
    n = len(llamadas); o2 = pagos.registrar_pago; pagos.registrar_pago = lambda *a, **k: {"ok": True, "payment_id": "PAY-ID"}
    ac._confirmar_pago("S1", "bill", "BILL1", 150.0, {"archivo_url": "https://x/comp.jpg", "referencia": "R123"})
    pagos.registrar_pago = o2
    check("comprobante de pago → Payment", len(llamadas) == n + 1 and llamadas[-1][:4] == ("comprobante_pago", "https://x/comp.jpg", "Payments", "PAY-ID"), str(llamadas[-1:]))
    n = len(llamadas); pagos.registrar_pago = lambda *a, **k: {"ok": True, "payment_id": "PAY-ID"}
    ac._confirmar_pago("S1", "bill", "BILL1", 150.0, {"referencia": "R123"}); pagos.registrar_pago = o2
    check("pago sin archivo → no guarda nada", len(llamadas) == n)
    n = len(llamadas); pagos.registrar_pago = lambda *a, **k: {"ok": False, "error": "x"}
    ac._confirmar_pago("S1", "bill", "BILL1", 150.0, {"archivo_url": "https://x/c.jpg"}); pagos.registrar_pago = o2
    check("pago que falló → no guarda el comprobante", len(llamadas) == n)
    # guía de envío → Shipping (el archivo viaja en el widget SHIPPING_COTEJO)
    from datetime import datetime, timezone
    bd.mensajes = [{"content": "[SHIPPING_COTEJO]" + json.dumps({"modo": "elegir_so", "archivo_url": "https://x/guia.jpg"}), "created_at": datetime.now(timezone.utc).isoformat()}]
    n = len(llamadas); o3 = sales_shipping.crear_shipping; sales_shipping.crear_shipping = lambda *a, **k: {"ok": True, "shipping_id": "SHIP-ID", "so_id": "SO-ID"}
    ac._crear_shipping_confirmado("S1", "SO-ID", [{"x": 1}], {"tracking": "1Z999"}); sales_shipping.crear_shipping = o3
    check("guía de envío → Shipping", len(llamadas) == n + 1 and llamadas[-1][:4] == ("guia_envio", "https://x/guia.jpg", "Shipping", "SHIP-ID"), str(llamadas[-1:]))
    # captura de compra → PurchaseOrders
    bd.mensajes = [{"content": "[COTEJO_PROVEEDOR]" + json.dumps({"captura": {"archivo_url": "https://x/cap.png"}}), "created_at": datetime.now(timezone.utc).isoformat()}]
    n = len(llamadas); o4 = compra_proveedor.crear_po_y_ap; compra_proveedor.crear_po_y_ap = lambda d: {"ok": True, "po_id": "PO-ID", "po_numero": "PO-1"}
    ac._crear_po_confirmado("S1", [{"proveedor_nombre": "eBay"}]); compra_proveedor.crear_po_y_ap = o4
    check("captura de compra → Purchase Order", len(llamadas) == n + 1 and llamadas[-1][:4] == ("captura_compra", "https://x/cap.png", "PurchaseOrders", "PO-ID"), str(llamadas[-1:]))
    # PO creado desde LINKS (la última cotejo no trae archivo) → no se guarda nada
    bd.mensajes = [{"content": "[COTEJO_PROVEEDOR]" + json.dumps({"links": []}), "created_at": datetime.now(timezone.utc).isoformat()}]
    n = len(llamadas); compra_proveedor.crear_po_y_ap = lambda d: {"ok": True, "po_id": "PO-ID2"}
    ac._crear_po_confirmado("S1", [{"proveedor_nombre": "eBay"}]); compra_proveedor.crear_po_y_ap = o4
    check("PO por link (sin captura) → no guarda nada", len(llamadas) == n)
    # widget viejo (>12 h) no se reutiliza
    bd.mensajes = [{"content": "[COTEJO_PROVEEDOR]" + json.dumps({"captura": {"archivo_url": "https://x/vieja.png"}}), "created_at": "2020-01-01T00:00:00+00:00"}]
    check("un archivo de hace más de 12 h no se reutiliza", ac._archivo_reciente("S1", "[COTEJO_PROVEEDOR]", lambda p: (p.get("captura") or {}).get("archivo_url")) == "")
finally:
    ac.supabase = orig_sb; ac._guardar_doc_crm = orig

if "--live" in sys.argv:
    import httpx, sales_order as so
    print("EN VIVO contra 1CRM (crea y borra un Documento de prueba)")
    SO = so._crm_get("data/SalesOrder", {"limit": 1})
    SO = (SO if isinstance(SO, list) else SO.get("records"))[0]["id"]
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    kw = dict(tipo="orden_compra_cliente", contenido=pdf, nombre_archivo_sugerido="1731012345678-TEST_borrar.pdf", parent_id=SO, referencia="PO TEST")
    r1 = cd.guardar_documento(**kw)
    try:
        check("sube el archivo real y lo liga", r1.get("ok") and r1.get("archivo", "").endswith("TEST_borrar.pdf"), str(r1))
        r2 = cd.guardar_documento(**kw)
        check("reintento → duplicado, no crea otro", r2.get("ok") and r2.get("duplicado") and r2.get("doc_id") == r1.get("doc_id"), str(r2))
    finally:
        if r1.get("doc_id"):
            d = so._crm_get(f"data/Document/{r1['doc_id']}"); d = d.get("record", d)
            httpx.delete(f"{so.CRM_BASE}/api.php/data/Document/{r1['doc_id']}", auth=so.CRM_AUTH, timeout=30)
            if d.get("document_revision_id"):
                httpx.delete(f"{so.CRM_BASE}/api.php/data/DocumentRevision/{d['document_revision_id']}", auth=so.CRM_AUTH, timeout=30)

if _fallos:
    print(f"\n❌ {len(_fallos)} fallo(s): {_fallos}"); raise SystemExit(1)
print("\n✅ Todo bien")
