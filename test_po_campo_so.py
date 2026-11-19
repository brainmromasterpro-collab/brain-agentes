"""El PO del cliente guardado en Documentos también debe quedar en el campo «SalesOrder» (SalesOrder_id) de la SO."""
import agente_chat  # carga el entorno
import crm_documentos as cd

SO = {"SalesOrder_id": ""}
PATCHES = []


class _R:
    def __init__(self, data=None, code=200):
        self._d, self.status_code, self.text = data or {}, code, ""

    def json(self):
        return self._d


def _patch(url, json=None, auth=None, timeout=0):
    PATCHES.append((url, json))
    if "/SalesOrder/" in url and SO_ACEPTA:
        SO.update(json["data"])
    return _R()


def _get(url, **k):
    return _R({"record": dict(SO)})


cd.httpx.patch, cd.httpx.get = _patch, _get
cd._cfg = lambda: ("https://x",)
cd._crm_auth = lambda: None

SO_ACEPTA = True
assert cd.referenciar_en_so("SO-1", "DOC-1") == {"ok": True} and SO["SalesOrder_id"] == "DOC-1"
assert PATCHES[0][1] == {"data": {"SalesOrder_id": "DOC-1"}}, PATCHES

# Si 1CRM no lo guarda, se dice (no se da por hecho)
SO["SalesOrder_id"] = ""; SO_ACEPTA = False
r = cd.referenciar_en_so("SO-1", "DOC-2")
assert r["ok"] is False and "no guardó" in r["error"], r

# guardar_documento: PO del cliente → llena el campo; otro tipo → no lo toca
cd.habilitado = lambda: True
cd._buscar_duplicado = lambda nombre, parent_id: "DOC-3"
SO["SalesOrder_id"] = ""; SO_ACEPTA = True; PATCHES.clear()
r = cd.guardar_documento(tipo="orden_compra_cliente", file_url="http://x/a.pdf", contenido=b"%PDF-1", parent_type="SalesOrders",
                         parent_id="SO-1", referencia="PO 1")
assert r["ok"] and r["campo_so"]["ok"] and SO["SalesOrder_id"] == "DOC-3", (r, SO)
PATCHES.clear(); SO["SalesOrder_id"] = ""
r = cd.guardar_documento(tipo="guia_envio", contenido=b"%PDF-1", parent_type="PurchaseOrders", parent_id="PO-1", referencia="x")
assert "campo_so" not in r and not PATCHES, (r, PATCHES)
print("OK test_po_campo_so")
