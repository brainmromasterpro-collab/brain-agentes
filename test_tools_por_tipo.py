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

print("Contratos y despachador: cada stream tiene contrato y lo atiende SU manejador")
import stream_contracts as sc
check("los 4 manejadores deterministas existen", all(c.manejador == "llm" or c.manejador in ac._MANEJADORES for c in sc.CONTRATOS.values()))
check("TOOLS_POR_TIPO sale de los contratos", ac.TOOLS_POR_TIPO == sc.tools_por_tipo())
check("TIPO_MODOS sale de los contratos", ac.TIPO_MODOS == sc.modos_por_tipo())
check("solo los streams llm tienen modos de prompt", all((c.manejador == "llm") or not c.modos_prompt for c in sc.CONTRATOS.values()))
check("triggers: solo busquedas/catalogo reciben busqueda_completa",
      {t for t in sc.CONTRATOS if sc.trigger_permitido(t, "busqueda_completa")} == {"busquedas", "catalogo"})
try:
    _reales = ac.supabase.table("streams").select("id,tipo,nombre").execute().data or []
    _sin = [f'{r["nombre"]}:{r["tipo"]}' for r in _reales if r.get("tipo") not in sc.CONTRATOS and r.get("tipo") not in sc.TIPOS_SIN_RESTRICCION]
    check("todo stream real de la BD tiene contrato", not _sin, str(_sin))
except Exception as _e:
    print(f"  (omitido, sin BD: {_e})")

class _Res:  # respuesta falsa de Supabase
    def __init__(self, data): self.data = data
class _Q:
    def __init__(self, bd, tabla): self.bd, self.tabla, self.op, self.payload = bd, tabla, "select", None
    def select(self, *a, **k): self.op = "select"; return self
    def insert(self, p): self.op, self.payload = "insert", p; return self
    def update(self, p): self.op, self.payload = "update", p; return self
    def __getattr__(self, n): return lambda *a, **k: self
    def execute(self):
        if self.op == "insert": self.bd.insertados.append((self.tabla, self.payload)); return _Res([self.payload])
        if self.op == "update": return _Res([])
        if self.tabla == "streams": return _Res({"tipo": self.bd.tipo})
        return _Res([])
class _BD:
    def __init__(self, tipo): self.tipo, self.insertados = tipo, []
    def table(self, t): return _Q(self, t)

def _despachar(tipo, msg_extra):
    bd = _BD(tipo)
    orig_sb, orig_run = ac.supabase, ac.run_chat
    llamadas = []
    ac.supabase = bd
    ac.run_chat = lambda *a, **k: (llamadas.append(k.get("tipo_stream")) or ("ok", [], False, {"tokens_input": 0, "tokens_output": 0}, {}))
    try:
        ac.procesar_mensaje({"id": "m1" * 4, "stream_id": "S1", "content": "hola", "metadata": {}, **msg_extra})
    finally:
        ac.supabase, ac.run_chat = orig_sb, orig_run
    return llamadas, bd.insertados

_ll, _ins = _despachar("compras", {"metadata": {"cancelar_accion": True}})
check("compras: lo atiende su router, el modelo NO se usa", _ll == [] and len(_ins) >= 1)
_ll, _ins = _despachar("pagos", {"content": "hola"})
check("pagos: lo atiende su router, el modelo NO se usa", _ll == [])
_ll, _ins = _despachar("ordenes", {"metadata": {"cancelar_accion": True}})
check("ordenes: botón cancelar lo atiende su router", _ll == [])
_ll, _ins = _despachar("rfq", {"content": "necesito 2 pzas del modelo X"})
check("Oportunidad (rfq) → modelo con tipo rfq", _ll == ["rfq"], str(_ll))
_ll, _ins = _despachar("correo", {})
check("correo → modelo con tipo correo", _ll == ["correo"], str(_ll))
_ll, _ins = _despachar("tipo_inventado", {})
check("tipo sin contrato → no se ejecuta nada", _ll == [])

print("Triggers de otros agentes: solo se escriben en streams cuyo contrato los permite")
import logging as _lg
def _trig(tipo):
    bd = _BD(tipo)
    ok = sc.insertar_trigger(bd, "S1", "busqueda_completa", {"stream_id": "S1", "content": "[SISTEMA:busqueda_completa]"}, _lg.getLogger("t"))
    return ok, bd.insertados
check("busquedas recibe busqueda_completa", _trig("busquedas")[0] is True and len(_trig("busquedas")[1]) == 1)
for _t in ("rfq", "correo", "publicacion", "pagos", "compras", "ordenes", "tipo_inventado"):
    _ok, _ins = _trig(_t)
    check(f"{_t} NO recibe busqueda_completa", _ok is False and _ins == [])

print("Contrato espejo del frontend coincide con el backend")
import os as _os, re as _re
_ts = "/Users/gabrielfuentes/Documents/mrojinni-app/src/lib/streamContracts.ts"
if _os.path.exists(_ts):
    _txt = open(_ts).read()
    _tipos_ts = set(_re.findall(r"'([a-z_]+)'", _re.search(r"TIPOS_PIPELINE_RFQ = \[(.*?)\]", _txt, _re.S).group(1)))
    _tipos_be = {t for t, c in sc.CONTRATOS.items() if "pipeline_rfq" in c.procesos} | set(sc.TIPOS_SIN_RESTRICCION)
    check("tipos con pipeline_rfq: frontend == backend", _tipos_ts == _tipos_be, f"{_tipos_ts} vs {_tipos_be}")
    _sql = "/Users/gabrielfuentes/Documents/mrojinni-app/sql/2026-11-05_rfqs_stream_contrato.sql"
    _sqltxt = open(_sql).read()
    _bus_sql = set(_re.findall(r"'([a-z_]+)'", _re.search(r"v_busqueda\s+text\[\] := ARRAY\[(.*?)\]", _sqltxt, _re.S).group(1)))
    _pub_sql = set(_re.findall(r"'([a-z_]+)'", _re.search(r"v_publicacion\s+text\[\] := ARRAY\[(.*?)\]", _sqltxt, _re.S).group(1)))
    check("SQL: tipos para RFQ de búsqueda == backend", _bus_sql == _tipos_be, f"{_bus_sql} vs {_tipos_be}")
    _pub_be = {t for t, c in sc.CONTRATOS.items() if "publicar_producto" in c.procesos} | set(sc.TIPOS_SIN_RESTRICCION)
    check("SQL: tipos para publicación desde link == backend", _pub_sql == _pub_be, f"{_pub_sql} vs {_pub_be}")
else:
    print("  (omitido: no está el repo del frontend)")

print("El PROMPT de cada stream no trae instrucciones de procesos que no son suyos")
def _p(t): return ac._build_system_prompt(t)
check("Oportunidad: el prompt no manda a usar crear_rfqs_desde_texto", "crear_rfqs_desde_texto" not in _p("rfq"))
check("Oportunidad: el prompt no menciona publicar_producto_link / extraer_ficha_pdf", not any(x in _p("rfq") for x in ("publicar_producto_link", "extraer_ficha_pdf")))
check("Buscar prods: sin reglas de oportunidades", "REGLA ABSOLUTA" not in _p("busquedas") and "crear_oportunidad_crm" not in _p("busquedas"))
check("Productos: sin reglas de oportunidades ni pipeline de búsqueda", "REGLA ABSOLUTA" not in _p("publicacion") and "crear_rfqs_desde_texto" not in _p("publicacion"))
check("Correo: sin pipeline de catálogo en el prompt", "crear_rfqs_desde_texto" not in _p("correo"))
check("todos los prompts focalizados traen su límite", all("LÍMITE DE ESTE STREAM" in _p(t) for t in ("rfq", "correo", "busquedas", "publicacion", "whatsapp")))
check("generico (legado) conserva el prompt completo", _p("generico") == ac.SYSTEM_PROMPT)

if _fallos:
    print(f"\n❌ {len(_fallos)} fallo(s): {_fallos}")
    raise SystemExit(1)
print("\n✅ Todo bien")
