"""Conciliador de pagos: cotejo por monto Y moneda, validación de que el archivo sea un pago, saldo ajustado por pagos previos,
aviso de sobrepago y defensa al registrar. (Sin tocar 1CRM ni Supabase: todo simulado.)"""
import agente_chat as ac
import pagos
import sales_order as so

# ── 1) Cotejo: monto Y moneda ────────────────────────────────────────────────────────────────────
CUENTAS = [
    {"id": "B1", "tipo": "bill", "nombre": "AP — ELEKTRAPOTHEKA", "amount_due": 1335.0, "moneda": "EUR", "cuenta": "ELEKTRAPOTHEKA"},
    {"id": "I1", "tipo": "invoice", "nombre": "Factura Markdom", "amount_due": 1335.0, "moneda": "MXN", "cuenta": "Markdom"},
    {"id": "B2", "tipo": "bill", "nombre": "AP — eBay", "amount_due": 740.99, "moneda": "USD", "cuenta": "eBay"},
]
r = pagos.buscar_candidata({"monto": 1335.0, "moneda": "EUR"}, CUENTAS)
assert r["por_monto"] and [c["id"] for c in r["candidatas"]] == ["B1"], r          # 1,335 EUR no es 1,335 MXN
r = pagos.buscar_candidata({"monto": 1335.0, "moneda": "MXN"}, CUENTAS)
assert r["por_monto"] and [c["id"] for c in r["candidatas"]] == ["I1"], r
r = pagos.buscar_candidata({"monto": 1335.0, "moneda": ""}, CUENTAS)
assert {c["id"] for c in r["candidatas"]} == {"B1", "I1"}, "sin moneda en el comprobante no hay con qué comparar"
# monto igual pero SOLO en otra moneda → no hay match automático y la cuenta sale marcada
r = pagos.buscar_candidata({"monto": 740.99, "moneda": "MXN"}, CUENTAS)
assert r["por_monto"] is False and next(c for c in r["candidatas"] if c["id"] == "B2")["moneda_coincide"] is False, r
# tolerancia de 1% / 1 unidad
assert pagos.buscar_candidata({"monto": 1334.5, "moneda": "EUR"}, CUENTAS)["por_monto"] is True
# parcial y sobrepago
r = pagos.buscar_candidata({"monto": 500.0, "moneda": "EUR"}, [CUENTAS[0]])
assert r["candidatas"][0]["completo"] is False and r["candidatas"][0]["excede"] == 0
r = pagos.buscar_candidata({"monto": 1500.0, "moneda": "EUR"}, [CUENTAS[0]])
assert r["candidatas"][0]["excede"] == 165.0, r

# ── 2) Saldo ajustado por pagos ya registrados (el amount_due de 1CRM no baja solo) ───────────────
pagos.bills_abiertas = lambda limite=40: [dict(CUENTAS[0])]
pagos.invoices_abiertas = lambda limite=40: [dict(CUENTAS[1])]
cs = {c["id"]: c for c in pagos.cuentas_abiertas({"B1": 335.0})}
assert cs["B1"]["amount_due"] == 1000.0, cs
assert "B1" not in {c["id"] for c in pagos.cuentas_abiertas({"B1": 1335.0})}, "pagada del todo → ya no es candidata"

# ── 3) Registrar: moneda distinta → error y no se crea el Payment ────────────────────────────────
POSTS = []
so._crm_get = lambda p, params=None: {"record": {"id": "B1", "name": "AP — ELEKTRAPOTHEKA", "currency_id": "CUR-EUR", "supplier_id": "S1"}}
so._crm_post = lambda m, d: POSTS.append((m, d)) or {"id": "PAY1"}
pagos.CRM_BASE = "https://x"
pagos.codigo_moneda = lambda cid: {"CUR-EUR": "EUR"}.get(cid, "")
res = pagos.registrar_pago("bill", "B1", 1335.0, {"moneda": "MXN", "referencia": "R"})
assert "error" in res and not POSTS, res
res = pagos.registrar_pago("bill", "B1", 1335.0, {"moneda": "EUR", "referencia": "PAP-1-03607", "fecha": "2026-11-20"})
assert res.get("ok") and POSTS[0][1]["related_invoice_id"] == "B1" and POSTS[0][1]["direction"] == "outgoing" \
    and POSTS[0][1]["amount"] == 1335.0 and POSTS[0][1]["customer_reference"] == "PAP-1-03607", (res, POSTS)
POSTS.clear()
assert pagos.registrar_pago("bill", "B1", 10.0, {})["ok"], "sin moneda en el comprobante no se bloquea"

# ── 4) ¿Es un pago? Lo que no lo es se pregunta, no se coteja ────────────────────────────────────
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
ac._log_stream = lambda *a, **k: None
ac._bajar_imagenes = lambda *a, **k: [b"img"]
ac._pagado_previo_map = lambda: {}
pagos.cuentas_abiertas = lambda prev=None, limite=40: [dict(CUENTAS[0])]
LEIDO = {}
pagos.leer_comprobante = lambda imgs, model_id="": dict(LEIDO)
TIPO = {"v": "compras"}
ac._resolver_tipo_stream = lambda sid: (TIPO["v"], True)

LEIDO.clear(); LEIDO.update({"es_pago": False, "monto": 173.7, "moneda": "USD", "notas": "Página de pedido de eBay"})
ac._procesar_comprobante_pago("S1", "u", "image.png", "image/png")
assert "[OPCIONES]" in INSERTS[-1]["content"] and not INSERTS[-1]["content"].startswith("[COTEJO_PAGO]"), INSERTS[-1]
assert "no demuestra un pago" in INSERTS[-1]["content"] and "Página de pedido de eBay" in INSERTS[-1]["content"]
assert ac._OPC_COTIZ in INSERTS[-1]["content"], "en compras ofrece las 3 opciones"
TIPO["v"] = "pagos"
ac._procesar_comprobante_pago("S1", "u", "image.png", "image/png")
assert ac._OPC_ES_PAGO in INSERTS[-1]["content"] and ac._OPC_NO_PAGO in INSERTS[-1]["content"], INSERTS[-1]
# si el usuario afirma que sí es un pago, se coteja
INSERTS.clear()
ac._procesar_comprobante_pago("S1", "u", "image.png", "image/png", forzar=True)
assert INSERTS[-1]["content"].startswith("[COTEJO_PAGO]"), INSERTS
# un pago real se coteja normal, y el comprobante viaja con su archivo
INSERTS.clear()
LEIDO.clear(); LEIDO.update({"es_pago": True, "monto": 1335.0, "moneda": "EUR", "referencia": "PAP-1-03607"})
ac._procesar_comprobante_pago("S1", "http://x/c.pdf", "c.pdf", "application/pdf")
import json as _j
p = _j.loads(INSERTS[-1]["content"][len("[COTEJO_PAGO]"):])
assert p["comprobante"]["archivo_url"] == "http://x/c.pdf" and p["cotejo"]["por_monto"] is True \
    and p["cotejo"]["candidatas"][0]["id"] == "B1", p
# el lector que no dice es_pago se comporta como antes
assert pagos.leer_comprobante.__name__  # (stub) — el default real se prueba abajo
import compra_proveedor as cp
cp._leer_con_vision = lambda imgs, instr, model_id="", max_tokens=500: {"monto": 5}
pagos2 = __import__("importlib").reload(pagos)
assert pagos2.leer_comprobante([b"x"])["es_pago"] is True

# ── 5) Respuestas de los botones en el stream «Pagos» ─────────────────────────────────────────────
LL = []
ac._procesar_comprobante_pago = lambda *a, **k: LL.append(("pago", k.get("forzar")))
ac._ultimo_archivo_stream = lambda sid: {"file_url": "http://x/c.pdf", "file_name": "c.pdf", "file_mime": "application/pdf"}
ac._manejar_pagos({}, "S1", ac._OPC_ES_PAGO, {})
assert LL == [("pago", True)], LL
INSERTS.clear(); LL.clear()
ac._manejar_pagos({}, "S1", ac._OPC_NO_PAGO, {})
assert not LL and "no hice nada" in INSERTS[-1]["content"]
ac._manejar_pagos({}, "S1", "Archivo subido: c.pdf", {"file_url": "http://x/c.pdf", "file_name": "c.pdf", "file_mime": "application/pdf"})
assert LL == [("pago", None)], LL   # subir un archivo no fuerza nada (None = sin forzar)
print("OK test_pagos_conciliador")
