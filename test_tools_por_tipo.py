"""
Guardia contra la CONTAMINACIÓN ENTRE STREAMS: cada tipo de stream solo debe poder usar las herramientas
de su propio proceso (agente_chat.TOOLS_POR_TIPO).

Correr:  python3 test_tools_por_tipo.py
"""
from dotenv import load_dotenv

load_dotenv()
import agente_chat as ac

_fallos: list[str] = []


def check(nombre: str, cond: bool, detalle: str = ""):
    print(f"  {'✅' if cond else '❌'} {nombre}" + (f" — {detalle}" if detalle and not cond else ""))
    if not cond:
        _fallos.append(nombre)


# Pipeline de catálogo: crea RFQs de búsqueda de producto, elige proveedor y publica productos en 1CRM.
PIPELINE_CATALOGO = {"crear_rfqs_desde_texto", "seleccionar_proveedor", "publicar_rfq", "publicar_sin_imagen_rfq",
                     "obtener_opciones_rfq", "obtener_foto_rfq", "verificar_lista_productos"}
VENTAS_CRM = {"buscar_clientes_crm", "crear_cuenta_crm", "crear_contacto_crm", "crear_oportunidad_crm"}
nombres = {t["name"] for t in ac.TOOLS}

print("Toda herramienta permitida existe")
for tipo, tools in ac.TOOLS_POR_TIPO.items():
    check(tipo, tools <= nombres, str(sorted(tools - nombres)))

print("Oportunidad (rfq): crea oportunidades, NO productos")
rfq = ac.TOOLS_POR_TIPO["rfq"]
check("tiene todo el CRM de ventas", VENTAS_CRM <= rfq, str(sorted(VENTAS_CRM - rfq)))
check("sin pipeline de catálogo", not (rfq & PIPELINE_CATALOGO), str(sorted(rfq & PIPELINE_CATALOGO)))
check("sin buscar_proveedores_crm (proveedor ≠ cliente)", "buscar_proveedores_crm" not in rfq)
check("sin Gmail (MODO 15 lee del chat)", not any("gmail" in n for n in rfq))
check("el prompt de rfq no pide herramientas que no tiene",
      not {n for n in nombres if n in ac._build_system_prompt("rfq")
           and f"`{n}`" in ac._build_system_prompt("rfq")} - rfq)

print("Streams deterministas (ordenes, compras, pagos): sin pipeline de catálogo")
for tipo in ("ordenes", "compras", "pagos"):
    t = ac.TOOLS_POR_TIPO[tipo]
    check(tipo, not (t & PIPELINE_CATALOGO), str(sorted(t & PIPELINE_CATALOGO)))

print("Catálogo/Productos: sin crear oportunidades")
for tipo in ("publicacion", "busquedas"):
    check(tipo, "crear_oportunidad_crm" not in ac.TOOLS_POR_TIPO[tipo])

print("Registro de procesos (streams_registry): ninguna mezcla nueva ni herramienta sin clasificar")
import re
import streams_registry as sr
_err, _avi = sr.verificar(ac.TOOLS_POR_TIPO, nombres)
check("sin errores", not _err, "; ".join(_err))
for a in _avi:
    print(f"  ⚠ {a}")

print("El prompt de cada stream no pide herramientas que ese stream no tiene")
_PEND = {}
for tipo, modos in ac.TIPO_MODOS.items():
    if not modos or tipo not in ac.TOOLS_POR_TIPO:
        continue
    texto = "".join(ac._PROMPT_MODOS.get(n, "") for n in modos)
    faltan = sorted(n for n in nombres if re.search(r"\b" + re.escape(n) + r"\b", texto)) 
    faltan = [n for n in faltan if n not in ac.TOOLS_POR_TIPO[tipo]]
    if faltan and tipo in _PEND:
        print(f"  ⚠ {tipo}: pide {faltan} sin tenerlas — conocido: {_PEND[tipo]}")
    else:
        check(tipo, not faltan, str(faltan))

print("Tipo de stream desconocido: falla cerrado (no hereda todas las herramientas)")
check("stream inexistente → no ok", ac._resolver_tipo_stream("00000000-0000-0000-0000-000000000000")[1] is False)
check("stream real (Oportunidad) → rfq", ac._resolver_tipo_stream("66c0d303-e633-45cc-a042-12824c40d02b") == ("rfq", True))
check("todo tipo de TIPO_MODOS tiene herramientas registradas", set(ac.TIPO_MODOS) <= set(ac.TOOLS_POR_TIPO))

print("Barrera de ejecución: una herramienta de otro proceso NO se ejecuta aunque el modelo la pida")
from types import SimpleNamespace as _NS
_llamadas = []
def _fake_tool(**kw):
    _llamadas.append(kw); return {"ok": True}
def _resp(blocks, stop):
    return _NS(content=blocks, stop_reason=stop, usage=None)
def _tool_use(name, inp):
    return _NS(type="tool_use", id="t1", name=name, input=inp)
def _correr(tipo, nombre_tool, inp):
    _llamadas.clear()
    originales = dict(ac.TOOL_FUNCTIONS)
    ac.TOOL_FUNCTIONS[nombre_tool] = _fake_tool
    seq = [_resp([_tool_use(nombre_tool, inp)], "tool_use"), _resp([_NS(type="text", text="listo")], "end_turn")]
    orig_create = ac.claude.messages.create
    ac.claude.messages.create = lambda **kw: seq.pop(0)
    try:
        ac.run_chat([{"role": "user", "content": "x"}], stream_id="STREAM-REAL", system_prompt="", tipo_stream=tipo)
    finally:
        ac.claude.messages.create = orig_create
        ac.TOOL_FUNCTIONS.clear(); ac.TOOL_FUNCTIONS.update(originales)
    return list(_llamadas)
check("rfq pide crear_rfqs_desde_texto → bloqueada", _correr("rfq", "crear_rfqs_desde_texto", {"texto": "x"}) == [])
check("rfq pide buscar_proveedores_crm → bloqueada", _correr("rfq", "buscar_proveedores_crm", {"nombre": "x"}) == [])
check("correo pide crear_rfqs_desde_texto → bloqueada", _correr("correo", "crear_rfqs_desde_texto", {"texto": "x"}) == [])
check("busquedas pide publicar_producto_link → bloqueada", _correr("busquedas", "publicar_producto_link", {"url": "x"}) == [])
check("tipo desconocido no ejecuta nada", _correr("inventado", "crear_oportunidad_crm", {"nombre": "x"}) == [])
_ok = _correr("rfq", "crear_oportunidad_crm", {"nombre": "x"})
check("rfq sí ejecuta crear_oportunidad_crm", len(_ok) == 1)
_ok = _correr("rfq", "crear_oportunidad_crm", {"nombre": "x", "stream_id": "OTRO-STREAM"})
check("el stream_id del modelo se sobrescribe con el real", len(_ok) == 1 and _ok[0].get("stream_id") == "STREAM-REAL", str(_ok))
check("_tools_para_tipo(None) y desconocido → vacío", ac._tools_para_tipo(None) == [] and ac._tools_para_tipo("zzz") == [])
check("generico (legado) conserva todo", len(ac._tools_para_tipo("generico")) == len(ac.TOOLS))

if _fallos:
    print(f"\n❌ {len(_fallos)} fallo(s): {_fallos}")
    raise SystemExit(1)
print("\n✅ Todo bien")
