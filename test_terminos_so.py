"""Pruebas de los términos de pago y vencimiento de la Sales Order (los de la CUENTA del cliente)."""
import agente_chat  # carga el entorno
import sales_order as so
from datetime import date

_fallos = []
def check(nombre, ok, detalle=""):
    print(("  ✅ " if ok else "  ❌ ") + nombre + ("" if ok else f" — {detalle}"))
    if not ok: _fallos.append(nombre)

print("Traducción cuenta → Sales Order")
for entra, sale in [("Net_30", "Net 30"), ("Net_15", "Net 15"), ("Net_45", "Net 45"), ("Net_60", "Net 60"), ("Net_90", "Net 90"),
                    ("advance_100%", "100% Advance"), ("advance_50%", "Advance 50% / 50%"),
                    ("Net 30", "Net 30"), ("Net 15", "Net 15"), ("Net 30 Days", "Net 30"), ("COD", "COD"),
                    ("", ""), (None, ""), ("raro", ""), ("Net_20", "")]:
    check(f"{entra!r} → {sale!r}", so.terminos_so(entra) == sale, so.terminos_so(entra))
check("todo valor traducido es válido en SalesOrder.terms", all(
    so.terminos_so(v) in {"100% Advance", "Advance 50% / 50%", "Net 7", "Net 15", "Net 30", "Net 45", "Net 60", "Net 90", "COD", "Due on Receipt", ""}
    for v in ("Net_15", "Net_30", "Net_45", "Net_60", "Net_90", "advance_100%", "advance_50%", "Net 15", "Net 30")))

print("Vencimiento = creación + días de crédito")
d0 = date(2026, 11, 10)
check("Net 30 → +30 días", so.fecha_vencimiento("Net 30", d0) == "2026-12-10")
check("Net 45 → +45 días", so.fecha_vencimiento("Net 45", d0) == "2026-12-25")
check("anticipo → el mismo día", so.fecha_vencimiento("100% Advance", d0) == "2026-11-10")
check("COD → el mismo día", so.fecha_vencimiento("COD", d0) == "2026-11-10")

print("crear_sales_order escribe los términos de la CUENTA (CRM simulado)")
def correr(payment_terms, terms_que_deja_1crm, due_que_deja_1crm, draft_terms="Net 30 Days", default_terms="Net 30"):
    posts, patches = [], []
    estado = {"terms": terms_que_deja_1crm, "due_date": due_que_deja_1crm}
    o = (so._crm_post, so._crm_get, so._crm_patch, so._crm_delete, so.CRM_BASE)
    so.CRM_BASE = "https://crm.test"
    def _post(m, d): posts.append((m, d)); return {"id": "SO1"}
    def _get(path, params=None):
        if path.startswith("data/Account/"): return {"record": {"payment_terms": payment_terms, "default_terms": default_terms, "currency_id": "C", "currency": "MXN"}}
        if path == "data/SalesOrder/SO1": return {"record": {"id": "SO1", "name": "x", "so_number": "300", "terms": estado["terms"], "due_date": estado["due_date"],
                                                           "line_items": [{"id": "L1", "mfr_part_no": "ZK22", "name": "a", "quantity": 1, "unit_price": "10", "ext_price": "10", "line_tax_perc": 16}]}}
        if path.startswith("data/Quote/"): return {"record": {"quote_stage": "Closed Accepted"}}
        return {}
    def _patch(m, i, d):
        patches.append((m, d)); estado.update({k: v for k, v in d.items() if k in ("terms", "due_date")}); return {}
    so._crm_post, so._crm_get, so._crm_patch, so._crm_delete = _post, _get, _patch, (lambda m, i: {})
    try:
        so.crear_sales_order({"cuenta_id": "A1", "quote_id": "Q1", "po_number": "1", "terms": draft_terms,
                              "lineas": [{"part_number": "ZK22", "cantidad": 1, "incluir": True}]})
    finally:
        so._crm_post, so._crm_get, so._crm_patch, so._crm_delete, so.CRM_BASE = o
    return posts[0][1], patches, estado

p, parches, est = correr("advance_100%", "Net 30", "2026-11-10")
check("cuenta con anticipo 100% → la SO sale con '100% Advance' (no el 'Net 30' de la cotización)", p.get("terms") == "100% Advance" and est["terms"] == "100% Advance", str((p.get("terms"), est)))
check("anticipo → vence el mismo día", p.get("due_date") == so.fecha_vencimiento("100% Advance"), str(p.get("due_date")))
p, parches, est = correr("Net_45", "Net 30", "2026-11-10")
check("cuenta Net_45 → 'Net 45' y vence a 45 días", p.get("terms") == "Net 45" and p.get("due_date") == so.fecha_vencimiento("Net 45"), str(p))
check("si 1CRM deja los de la cotización, se corrigen con PATCH", est["terms"] == "Net 45" and est["due_date"] == so.fecha_vencimiento("Net 45") and any(m == "SalesOrder" and "terms" in d for m, d in parches), str((est, parches)))
p, parches, est = correr("", "Net 30", "2026-11-10", draft_terms="", default_terms="")
check("cuenta sin términos → no se pisan los de la cotización", "terms" not in p and "due_date" not in p and not any("terms" in d for m, d in parches), str((p, parches)))
p, parches, est = correr("", "Net 30", "2026-11-10", draft_terms="", default_terms="Net 45 Days")
check("sin payment_terms se usa default_terms de la cuenta", p.get("terms") == "Net 45", str(p.get("terms")))
p, parches, est = correr("Net_30", "Net 30", so.fecha_vencimiento("Net 30"))
check("si ya quedó bien no se hace PATCH de más", not any(m == "SalesOrder" and ("terms" in d or "due_date" in d) for m, d in parches), str(parches))

if _fallos:
    print(f"\n❌ {len(_fallos)} fallo(s): {_fallos}"); raise SystemExit(1)
print("\n✅ Todo bien")
