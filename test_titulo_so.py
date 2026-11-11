"""Pruebas del asunto (nombre) de la Sales Order: números de parte de las líneas que van en la SO."""
import agente_chat  # carga el entorno
import sales_order as so

_fallos = []
def check(nombre, ok, detalle=""):
    print(("  ✅ " if ok else "  ❌ ") + nombre + ("" if ok else f" — {detalle}"))
    if not ok: _fallos.append(nombre)

L = lambda pn, name="": {"mfr_part_no": pn, "name": name}
_marca_real = so.marca_de_linea
_ia_real = so._marca_por_ia
so._marca_por_ia = lambda n, m="": ""   # sin red en las pruebas
so.marca_de_linea = lambda ln: ""   # sin marca: estas pruebas son solo de modelos
print("titulo_so")
check("1 producto", so.titulo_so([L("CSMD-20BT3ATT3")]) == "CSMD-20BT3ATT3")
check("2 productos", so.titulo_so([L("ZK22"), L("MGP806N")]) == "ZK22, MGP806N")
check("3 productos, en el orden de las líneas", so.titulo_so([L("A1"), L("B2"), L("C3")]) == "A1, B2, C3")
check("no repite el mismo número de parte (aunque cambie el formato)", so.titulo_so([L("Q0120"), L("q-0120"), L("B2")]) == "Q0120, B2")
check("línea libre sin número de parte aporta su descripción corta", so.titulo_so([L("", "Lámina de acero inoxidable  calibre 14"), L("ZK22")]) == "Lámina de acero inoxidable calibre 14, ZK22")
check("sin líneas → vacío (no se toca el nombre)", so.titulo_so([]) == "" and so.titulo_so([L("", "")]) == "")
largo = so.titulo_so([L(f"PARTE-NUMERO-{i:03d}") for i in range(40)], maximo=100)
check("muchos productos: cabe en el máximo y avisa cuántos faltan", len(largo) <= 100 and largo.endswith("más") and "+" in largo, largo)
check("nunca corta un número de parte a la mitad", all(p.startswith("PARTE-NUMERO-") and len(p) == 16 for p in largo.rsplit(" +", 1)[0].split(", ")), largo)

so.marca_de_linea = _marca_real   # (las pruebas siguientes sí usan la marca)
print("titulo_so con marca: «Marca Modelo», sin cantidades")
MARCAS = {"CSMD-20BT3ATT3": "RS Automation", "NI4U-EG08-AN6X-H1341": "Turck", "1763-L16BWA": "Rockwell Automation"}
M = lambda ln: MARCAS.get(ln.get("mfr_part_no"), "")
LQ = lambda pn, q: {"mfr_part_no": pn, "name": pn, "quantity": q}
check("1 producto: marca y modelo", so.titulo_so([LQ("CSMD-20BT3ATT3", 1)], marca=M) == "RS Automation CSMD-20BT3ATT3")
check("3 piezas del mismo producto → UNA mención y sin cantidad",
      so.titulo_so([LQ("CSMD-20BT3ATT3", 3)], marca=M) == "RS Automation CSMD-20BT3ATT3")
check("mismo modelo en dos líneas → una sola mención",
      so.titulo_so([LQ("CSMD-20BT3ATT3", 2), LQ("CSMD-20BT3ATT3", 1)], marca=M) == "RS Automation CSMD-20BT3ATT3")
check("varios productos, cada uno con su marca, sin números de cantidad",
      so.titulo_so([LQ("NI4U-EG08-AN6X-H1341", 2), LQ("1763-L16BWA", 5)], marca=M) == "Turck NI4U-EG08-AN6X-H1341, Rockwell Automation 1763-L16BWA")
check("marca desconocida → solo el modelo (no se inventa)", so.titulo_so([LQ("ZK22", 4), LQ("1763-L16BWA", 1)], marca=M) == "ZK22, Rockwell Automation 1763-L16BWA")
check("si la marca ya va dentro del modelo no se repite", so.titulo_so([LQ("TURCK-123", 1)], marca=lambda l: "Turck") == "TURCK-123")
check("una marca que truena no tumba el título", so.titulo_so([LQ("ZK22", 1)], marca=lambda l: 1/0) == "ZK22")

print("marca_de_linea y limpiar_marca")
check("razón social fuera", so.limpiar_marca("RS Automation USA LLC") == "RS Automation" and so.limpiar_marca("Turck") == "Turck"
      and so.limpiar_marca("Siemens AG") == "Siemens" and so.limpiar_marca("Weg S.A. de C.V.") == "Weg", so.limpiar_marca("RS Automation USA LLC"))
check("marca desde el nombre «MODELO / Marca / descripción»",
      so.marca_de_linea({"name": "1763-L16BWA / Rockwell Automation / MicroLogix 1100 16 Point Controller", "mfr_part_no": "1763-L16BWA"}) == "Rockwell Automation")
_o = (so._crm_get,)
def _g(path, params=None):
    if path == "data/Product": return {"records": [{"id": "P0"}, {"id": "P1"}]}
    if path == "data/Product/P0": return {"record": {"manufacturers_part_no": "OTRO-1", "manufacturer_id": "F0"}}
    if path == "data/Product/P1": return {"record": {"manufacturers_part_no": "ni4u-eg08-an6x-h1341", "manufacturer_id": "F1"}}
    if path == "data/Account/F1": return {"record": {"name": "Turck"}}
    return {}
so._crm_get = _g
try:
    check("marca desde el catálogo (producto con ese número de parte exacto)",
          so.marca_de_linea({"name": "Sensor, Inductivo, NI4U-EG08-AN6X-H1341", "mfr_part_no": ""}) == "Turck")
    check("producto del catálogo con otro número de parte NO sirve", so.marca_de_linea({"name": "Otra cosa XYZ-9988", "mfr_part_no": "XYZ-9988"}) == "")
finally:
    so._crm_get = _o[0]
    so._marca_por_ia = _ia_real

print("respaldo con IA: solo vale si la marca está escrita literalmente en el nombre")
class _R:  # respuesta simulada de Haiku
    def __init__(s, txt): s.content = [type("C", (), {"text": txt})()]
import anthropic
_cli = anthropic.Anthropic
def _falso(txt):
    class A:
        def __init__(s, **k): s.messages = type("M", (), {"create": staticmethod(lambda **kw: _R(txt))})()
    return A
try:
    anthropic.Anthropic = _falso("APC")
    check("marca escrita en el nombre → se acepta", so._marca_por_ia("APC SMART-UPS SRT1000XLI", "SRT1000XLI") == "APC")
    anthropic.Anthropic = _falso("Schneider")
    check("marca que NO está en el nombre (la inventó) → se descarta", so._marca_por_ia("APC SMART-UPS SRT1000XLI") == "")
    anthropic.Anthropic = _falso("NINGUNA")
    check("el modelo dice NINGUNA → vacío", so._marca_por_ia("Tornillo opresor 5/16-18") == "")
    anthropic.Anthropic = _falso("Wilden.")
    check("limpia puntuación", so._marca_por_ia("BOMBA NEUMATICA WILDEN T4/AAPPB") == "Wilden")
finally:
    anthropic.Anthropic = _cli
    so._marca_por_ia = lambda n, m="": ""

print("numero_parte: el modelo sale del nombre cuando el campo está vacío (casos reales de 1CRM)")
np = so.numero_parte
check("campo lleno manda", np("1763-L16BWA", "1763-L16BWA / Rockwell Automation / MicroLogix 1100") == "1763-L16BWA")
check("sensor", np("", "Sensor, Inductivo, NI4U-EG08-AN6X-H1341") == "NI4U-EG08-AN6X-H1341")
check("TV: el modelo completo, no el pedazo", np("", "TV LG de 55 pulgadas UHD AI UA7510 4K SMART TV 2025 55UA7510") == "55UA7510")
check("UPS", np("", "APC SMART-UPS SRT1000XLI") == "SRT1000XLI")
check("bomba: el modelo, no «04-10» ni «No. DE PARTE»", np("", "BOMBA NEUMATICA WILDEN T4/AAPPB/BNS/BN/BN No. DE PARTE 04-10") == "T4/AAPPB/BNS/BN/BN")
check("escalera", np("", "EFR-10 Escalera rodante de fierro, Plataf. 40 x 61.5 cm, Alt. Plataf. 100 cm") == "EFR-10")
check("medidas y años no son número de parte", np("", "Tornillo 5/16-18 x 1/2 2025 4K") == "Tornillo 5/16-18 x 1/2 2025 4K"[:40])

print("crear_sales_order deja el asunto con las líneas que quedaron (CRM simulado)")
llamadas = {"patch": []}
estado = {"lineas": [{"id": "L1", "mfr_part_no": "CSMD-10BT3AST3", "name": "x", "quantity": 1, "unit_price": "10", "ext_price": "10", "line_tax_perc": 16},
                     {"id": "L2", "mfr_part_no": "CSMD-20BT3ATT3", "name": "y", "quantity": 1, "unit_price": "20", "ext_price": "20", "line_tax_perc": 16},
                     {"id": "L3", "mfr_part_no": "CSD7-25BXF1-Y", "name": "z", "quantity": 1, "unit_price": "30", "ext_price": "30", "line_tax_perc": 16}],
          "name": "CSMD-10BT3AST3 CSMD-20BT3ATT3 CSD7-25BXF1-Y RS Automation"}
o = (so._crm_post, so._crm_get, so._crm_patch, so._crm_delete, so.CRM_BASE)
so.CRM_BASE = "https://crm.test"
so._crm_post = lambda m, d: {"id": "SO1"}
def _get(path, params=None):
    if path == "data/SalesOrder/SO1": return {"record": {"id": "SO1", "name": estado["name"], "so_number": "295", "line_items": list(estado["lineas"])}}
    if path.startswith("data/Quote/"): return {"record": {"quote_stage": "Closed Accepted"}}
    return {}
so._crm_get = _get
def _patch(m, i, d):
    llamadas["patch"].append((m, i, d))
    if m == "SalesOrder" and "name" in d: estado["name"] = d["name"]
    return {}
so._crm_patch = _patch
def _del(m, i):
    estado["lineas"] = [l for l in estado["lineas"] if l["id"] != i]; return {}
so._crm_delete = _del
try:
    r = so.crear_sales_order({"cuenta_id": "C1", "quote_id": "Q1", "po_number": "8992",
                              "lineas": [{"part_number": "CSMD-20BT3ATT3", "cantidad": 1, "incluir": True}]})
    nombres = [d["name"] for m, i, d in llamadas["patch"] if m == "SalesOrder" and "name" in d]
    check("PO pide 1 de las 3 líneas de la cotización → asunto = ese número de parte", nombres == ["CSMD-20BT3ATT3"], str(nombres))
    check("el resultado trae el asunto nuevo", r.get("nombre") == "CSMD-20BT3ATT3", str(r.get("nombre")))
finally:
    so._crm_post, so._crm_get, so._crm_patch, so._crm_delete, so.CRM_BASE = o

if _fallos:
    print(f"\n❌ {len(_fallos)} fallo(s): {_fallos}"); raise SystemExit(1)
print("\n✅ Todo bien")
