"""Al quitar líneas de la SO copiada de la cotización, los totales del grupo y de la SO deben ser los de las líneas que quedan
(caso real SO 307: quedaba 1 línea de 57,966.55 pero la pantalla mostraba el subtotal de toda la cotización)."""
import agente_chat  # carga el entorno
import sales_order as so

REG = {
    ("SalesOrder", "SO-1"): {
        "exchange_rate": "20", "total_shipping": 0, "total_discount": 0,
        "subtotal": "148959.06", "amount": "172792.51",
        "line_items": [{"id": "L1", "line_group_id": "G1", "ext_price": "57966.55", "line_tax_perc": "16",
                        "cost_price": "28569", "quantity": "1"}],
    },
    ("SalesOrderLineGroup", "G1"): {"subtotal": "148959.06", "total": "172792.51", "cost": "73081"},
}
PATCHES = []


def _get(path, params=None):
    _, modelo, rid = path.split("/")
    return {"record": dict(REG[(modelo, rid)])}


def _patch(modelo, rid, data):
    PATCHES.append((modelo, rid, data))
    REG[(modelo, rid)].update(data)
    return {"result": True}


so._crm_get, so._crm_patch = _get, _patch

r = so.recalcular_totales_so("SO-1")
assert r == {"subtotal": 57966.55, "iva": 9274.65, "total": 67241.2, "grupos": 1}, r
g = REG[("SalesOrderLineGroup", "G1")]
assert g["subtotal"] == "57966.55" and g["total"] == "67241.20" and g["cost"] == "28569.00", g
s = REG[("SalesOrder", "SO-1")]
assert s["subtotal"] == "57966.55" and s["amount"] == "67241.20" and s["pretax"] == "57966.55", s

# Dos grupos: cada uno con lo suyo
REG[("SalesOrder", "SO-1")]["line_items"] = [
    {"id": "A", "line_group_id": "G1", "ext_price": "100", "line_tax_perc": "16", "cost_price": "50", "quantity": "2"},
    {"id": "B", "line_group_id": "G2", "ext_price": "200", "line_tax_perc": "0", "cost_price": "10", "quantity": "1"},
]
REG[("SalesOrderLineGroup", "G2")] = {}
r = so.recalcular_totales_so("SO-1")
assert r["subtotal"] == 300 and r["iva"] == 16 and r["total"] == 316 and r["grupos"] == 2, r
assert REG[("SalesOrderLineGroup", "G1")]["total"] == "116.00" and REG[("SalesOrderLineGroup", "G2")]["total"] == "200.00"
print("OK test_totales_so")
