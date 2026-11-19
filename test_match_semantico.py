"""Match semántico del cotejo de compras: el mismo producto en otro idioma / sin número de parte (caso real: pedido de eBay
«Kmac Alloy Steel Set Screws, Brass Tip, 5/16-18 x 1/2"» ↔ SO «Tornillo opresor 5/16-18 x 1/2" Acero con punta de latón»).
La IA se simula; lo que se prueba es la barrera de especificaciones y el uso de su respuesta."""
import agente_chat  # carga el entorno
import compra_proveedor as cp
import sales_order as so

PROMPTS = []
RESPUESTA = {}


def _llm(prompt, max_tokens=400):
    PROMPTS.append(prompt)
    return dict(RESPUESTA)


cp._llm_json = _llm
SOS = [
    {"id": "SO1", "nombre": "Value Collection BT-51618-500-AS", "lines": [
        {"part_number": "BT-51618-500-AS", "part_compact": "BT51618500AS", "unit_price": 54.17, "quantity": 58,
         "descripcion": 'Tornillo opresor 5/16-18 x 1/2" Acero con punta de laton'}]},
    {"id": "SO2", "nombre": "Otra", "lines": [
        {"part_number": "", "part_compact": "", "unit_price": 10, "quantity": 1, "descripcion": "Pintura blanca 4 litros"},
        {"part_number": "ZZ-1", "part_compact": "ZZ1", "unit_price": 99, "quantity": 2, "descripcion": 'Tuerca 5/16-18 hexagonal'}]},
]
KMAC = {"ok": True, "nombre": 'Kmac Alloy Steel Set Screws, Brass Tip, 5/16-18 x 1/2" Thread Length, 20 Pcs', "marca": "",
        "part_number": "111730686870", "descripcion": ""}

# especificaciones literales
e = cp._especificaciones('Set Screws 5/16-18 x 1/2" 24T 220VAC')
assert {"5/16-18", "1/2", "24t", "220vac"} <= e, e

# 1) la IA dice que la línea 0 es el mismo producto → se devuelve esa línea (y solo las que pasaron la barrera)
RESPUESTA.update({"coincidencias": [{"i": 0, "evidencia": "5/16-18 x 1/2, punta de latón"}]})
r = cp.match_semantico(KMAC, SOS)
assert len(r) == 1 and r[0][0]["id"] in ("SO1", "SO2"), r
prompt = PROMPTS[-1]
assert "Pintura blanca" not in prompt, "una línea sin especificaciones en común no se le muestra a la IA"
assert "Tornillo opresor" in prompt and "Tuerca" in prompt   # ambas comparten 5/16-18

# 2) la IA no puede devolver una línea fuera de las candidatas, ni inventar
RESPUESTA.clear(); RESPUESTA.update({"coincidencias": [{"i": 99}, {"i": "x"}, {}]})
assert cp.match_semantico(KMAC, SOS) == []
# 3) la IA falla o no está segura → []
RESPUESTA.clear()
assert cp.match_semantico(KMAC, SOS) == []

# 4) sin especificaciones en común NI se llama a la IA (no hay nada que comparar)
PROMPTS.clear(); RESPUESTA.update({"coincidencias": [{"i": 0}]})
assert cp.match_semantico({"nombre": "Cafetera automática", "part_number": "", "marca": ""}, SOS) == [] and not PROMPTS
# producto con especificaciones que no aparecen en ninguna línea → tampoco
assert cp.match_semantico({"nombre": 'Sierra 10-1/4" 24T', "part_number": "A-94910"}, SOS) == [] and not PROMPTS

# 5) integrado en el cotejo: sin parte ni palabras en común, sale como coincidencia «≈ verificar» (tipo ia)
cp.CRM_BASE = "https://x"
cp.sales_orders_abiertas = lambda *a, **k: SOS
cp._so_detalle = lambda sid: {"so_number": "309", "so_stage": "In Manufacturing", "cliente": "C", "currency_id": "MXN", "tax_code_id": ""}
cp._mapa_ya_comprado = lambda: {}
cp._terminos_proveedor = lambda n: ""
cp.proveedor_en_crm = lambda n: n
cp._monedas_crm = lambda: {"USD": {"id": "U", "codigo": "USD", "nombre": "US Dollar"}}
RESPUESTA.clear(); RESPUESTA.update({"coincidencias": [{"i": 0, "evidencia": "x"}]})
co = cp.buscar_sales_orders_candidatas([{**KMAC, "precio_costo": 18.63, "moneda": "USD", "cantidad": 4, "proveedor": "eBay"}])
assert len(co["grupos"]) == 1 and not co["sin_match"], co
lk = co["grupos"][0]["links"][0]
assert lk["tipo_match"] == "ia" and lk["match_parcial"] is True, lk
# y si la IA no confirma, queda sin match (no se inventa)
RESPUESTA.clear()
co = cp.buscar_sales_orders_candidatas([{**KMAC, "precio_costo": 18.63, "moneda": "USD", "cantidad": 4, "proveedor": "eBay"}])
assert not co["grupos"] and len(co["sin_match"]) == 1, co
print("OK test_match_semantico")
