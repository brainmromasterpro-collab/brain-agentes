"""El PO y su Bill deben quedar con los términos del PROVEEDOR en 1CRM aunque 1CRM los pise con los de la SO ligada."""
import agente_chat  # carga el entorno
import compra_proveedor as cp
import sales_order as so

REG = {}          # (modelo, id) → registro guardado
PATCHES = []
SO_TERMS = "Net 30"   # lo que 1CRM copia de la SO ligada y pisa


def _post(modelo, data):
    rid = f"{modelo}-1"
    rec = dict(data)
    if modelo == "PurchaseOrder":
        rec["terms"] = SO_TERMS           # simula la herencia desde from_so_id
    REG[(modelo, rid)] = rec
    return {"id": rid}


def _get(path, params=None):
    partes = path.split("/")
    if len(partes) == 3:
        return {"record": dict(REG.get((partes[1], partes[2]), {}))}
    return {"records": []}


def _patch(modelo, rid, data):
    PATCHES.append((modelo, rid, data))
    REG[(modelo, rid)].update(data)
    return {}


so._crm_post, so._crm_get, so._crm_patch = _post, _get, _patch
cp.CRM_BASE = "https://x"
cp._buscar_o_crear_proveedor = lambda n: "SUP-1"
cp._terminos_proveedor = lambda n: "100% Advance"
cp._crear_lineas_po = lambda *a, **k: 1

# 1) 1CRM pisa los términos del PO con los de la SO → deben corregirse
r = cp.crear_po_y_ap({"so_id": "SO-1", "proveedor_nombre": "SIT", "currency_id": "MXN",
                      "lineas": [{"name": "UPS", "mfr_part_no": "SRT1000XLI", "quantity": 5, "unit_price": 100}]})
assert r.get("ok"), r
assert REG[("PurchaseOrder", "PurchaseOrder-1")]["terms"] == "100% Advance", REG
assert REG[("Bill", "Bill-1")]["terms"] == "100% Advance"
assert any(m == "PurchaseOrder" and d.get("terms") == "100% Advance" for m, _, d in PATCHES), PATCHES
assert r["terms"] == "100% Advance" and r["bill_terms"] == "100% Advance", r

# 2) Sin herencia: no hay PATCH innecesario
PATCHES.clear(); REG.clear(); SO_TERMS = "100% Advance"
r = cp.crear_po_y_ap({"so_id": "SO-1", "proveedor_nombre": "SIT", "currency_id": "MXN",
                      "lineas": [{"name": "UPS", "quantity": 1, "unit_price": 10}]})
assert r.get("ok") and not [p for p in PATCHES if p[0] == "PurchaseOrder"], PATCHES

# 3) Proveedor sin términos y con SO: error, no se crea nada
cp._terminos_proveedor = lambda n: ""
REG.clear()
r = cp.crear_po_y_ap({"so_id": "SO-1", "proveedor_nombre": "X", "currency_id": "MXN",
                      "lineas": [{"name": "a", "quantity": 1, "unit_price": 1}]})
assert "error" in r and not REG, r
print("OK test_terminos_po")
