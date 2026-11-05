"""
CONTRATOS DE STREAM — única fuente de verdad de qué hace (y qué NO hace) cada tipo de stream.
===========================================================================================
Cada stream tiene un CONTRATO escrito:
  · manejador   → quién lo atiende: 'llm' (el modelo, con prompt y herramientas acotados) o un router
                  determinista ('ordenes', 'compras', 'pagos') que no usa herramientas de otros procesos.
  · procesos    → los procesos de negocio que puede ejecutar (ver streams_registry.PROCESO_DE_HERRAMIENTA).
  · herramientas→ las ÚNICAS herramientas que puede ejecutar el modelo en ese stream (se aplica al ejecutar,
                  no solo al mostrarlas).
  · modos_prompt→ qué partes del prompt recibe.
  · triggers    → qué marcadores [SISTEMA:...] pueden escribirse en ese stream desde otros agentes.

agente_chat.py deriva TOOLS_POR_TIPO y TIPO_MODOS de aquí; no se definen en ningún otro lado.
Para agregar un tipo de stream hay que escribir su contrato aquí y pasar test_tools_por_tipo.py.

Este módulo no importa nada del proyecto (para poder usarse desde cualquier agente sin ciclos).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Contrato:
    tipo: str
    nombre: str
    descripcion: str
    manejador: str                    # 'llm' | 'ordenes' | 'compras' | 'pagos'
    procesos: frozenset
    herramientas: frozenset
    modos_prompt: tuple = ()
    triggers: frozenset = frozenset()  # marcadores [SISTEMA:x] que otros agentes pueden dejar en este stream


# ── Conjuntos de herramientas (nombres) ─────────────────────────────────────────────────────────
_T_VENTAS = frozenset({
    "enviar_email_gmail", "diagnostico_gmail", "leer_emails_gmail", "buscar_email_gmail",
    "escanear_emails_ventas", "buscar_clientes_crm", "ver_cliente_crm", "crear_cuenta_crm",
    "crear_contacto_crm", "actualizar_cuenta_crm", "actualizar_contacto_crm",
    "buscar_contactos_crm", "ver_contactos_cuenta_crm", "listar_oportunidades_crm",
    "crear_oportunidad_crm", "actualizar_oportunidad_crm", "listar_cotizaciones_crm",
    "buscar_internet", "notificar_sistema", "consultar_metricas"})

_T_RFQ = frozenset({
    "buscar_productos_crm", "ver_producto_crm", "listar_productos_crm", "buscar_proveedores_crm",
    "consultar_rfqs", "obtener_opciones_rfq", "seleccionar_proveedor", "obtener_foto_rfq",
    "publicar_rfq", "publicar_sin_imagen_rfq", "verificar_lista_productos", "crear_rfqs_desde_texto",
    "buscar_internet", "notificar_sistema", "consultar_metricas"})

# Oportunidad (stream 'rfq'): solo CRM de ventas. SIN el pipeline de catálogo, SIN proveedores, SIN Gmail.
_T_OPORT = frozenset({
    "buscar_clientes_crm", "ver_cliente_crm", "crear_cuenta_crm", "crear_contacto_crm",
    "actualizar_cuenta_crm", "actualizar_contacto_crm", "buscar_contactos_crm",
    "ver_contactos_cuenta_crm", "listar_oportunidades_crm", "crear_oportunidad_crm",
    "actualizar_oportunidad_crm", "buscar_internet", "notificar_sistema", "consultar_metricas"})

_T_CATALOG = frozenset({
    "buscar_productos_crm", "ver_producto_crm", "listar_productos_crm", "extraer_producto_de_link",
    "extraer_ficha_pdf", "publicar_producto_link", "publicar_productos_desde_links",
    "buscar_internet", "notificar_sistema", "consultar_metricas"})

_T_OPS = frozenset({
    "buscar_productos_crm", "ver_producto_crm", "listar_productos_crm", "buscar_clientes_crm",
    "ver_cliente_crm", "buscar_proveedores_crm", "listar_cotizaciones_crm", "buscar_internet",
    "notificar_sistema", "consultar_metricas"})

_BASE = frozenset({"consulta"})
_TRIG_BUSQ = frozenset({"busqueda_completa", "imagen_lista", "imagen_no_encontrada"})

# Stream legado que conserva todo a propósito (sin uso desde 2026-10-14). No se ofrece al crear streams.
TIPOS_SIN_RESTRICCION = frozenset({"generico"})

_CORREO = dict(
    manejador="llm", procesos=_BASE | {"oportunidades", "correo"}, herramientas=_T_VENTAS,
    modos_prompt=(10, 11, 12))

CONTRATOS: dict[str, Contrato] = {c.tipo: c for c in (
    Contrato("rfq", "Oportunidad",
             "Captura de RFQ del cliente (texto/screenshot) → cuenta, contacto y oportunidad en el CRM. NUNCA catálogo.",
             "llm", _BASE | {"oportunidades"}, _T_OPORT, (15, 12)),
    Contrato("correo", "Correo", "Oportunidades que llegan por correo → completar datos → crear en el CRM.", **_CORREO),
    Contrato("whatsapp", "WhatsApp", "Igual que Correo, por WhatsApp.", **_CORREO),
    Contrato("mensajeria", "Mensajería", "Alias de compatibilidad de Correo/WhatsApp.", **_CORREO),
    Contrato("busquedas", "Buscar prods",
             "Buscar producto y proveedores para un RFQ de catálogo y elegir opción.",
             "llm", _BASE | {"proveedores", "pipeline_rfq"}, _T_RFQ, (1, 2, 3, 4, 5, 6, 8),
             triggers=_TRIG_BUSQ),
    Contrato("publicacion", "Productos", "Publicar producto(s) en el catálogo 1CRM desde link o ficha técnica.",
             "llm", _BASE | {"publicar_producto"}, _T_CATALOG, (13, 14)),
    Contrato("catalogo", "Catálogo (legado)", "Alias antiguo: búsqueda + publicación.",
             "llm", _BASE | {"proveedores", "pipeline_rfq", "publicar_producto"}, _T_CATALOG | _T_RFQ,
             (1, 2, 3, 4, 5, 6, 8, 13, 14), triggers=_TRIG_BUSQ),
    Contrato("cotizacion", "Cotización", "Cotizaciones a clientes.", "llm",
             _BASE | {"oportunidades", "correo"}, _T_VENTAS, ()),
    Contrato("cotizaciones", "Cotizaciones (alias)", "Alias de cotizacion.", "llm",
             _BASE | {"oportunidades", "correo"}, _T_VENTAS, ()),
    Contrato("ordenes", "Sales Order",
             "Orden de compra del cliente → cotejo con cotizaciones → Sales Order, envío, cierre y facturación.",
             "ordenes", _BASE | {"proveedores"}, _T_OPS, ()),
    Contrato("compras", "Purchase Order",
             "Compra a proveedor: link → PO/AP, pagos, recepción.", "compras",
             _BASE | {"proveedores"}, _T_OPS, ()),
    Contrato("pagos", "Pagos", "Comprobantes de pago → Bill/Invoice.", "pagos",
             _BASE | {"proveedores"}, _T_OPS, ()),
)}


def contrato_de(tipo):
    """Contrato del tipo, o None si no existe (el llamador debe FALLAR CERRADO)."""
    return CONTRATOS.get(tipo)


def tools_por_tipo() -> dict:
    return {t: set(c.herramientas) for t, c in CONTRATOS.items()}


def modos_por_tipo() -> dict:
    return {t: list(c.modos_prompt) for t, c in CONTRATOS.items()}


def procesos_permitidos() -> dict:
    return {t: set(c.procesos) for t, c in CONTRATOS.items()}


def trigger_permitido(tipo, trigger: str) -> bool:
    """¿Puede un agente dejar el marcador [SISTEMA:<trigger>] en un stream de este tipo?"""
    c = CONTRATOS.get(tipo)
    return bool(c and trigger in c.triggers)


def insertar_trigger(supabase, stream_id, trigger: str, fila: dict, log=None) -> bool:
    """Deja un marcador [SISTEMA:<trigger>] en un stream SOLO si el contrato de ese stream lo permite.
    Así un agente (buscador, imagen...) no puede escribir en un stream de otro proceso aunque el RFQ traiga
    un stream_id equivocado. Falla cerrado: si no se puede leer el tipo, no escribe."""
    try:
        tipo = (supabase.table("streams").select("tipo").eq("id", stream_id).single().execute().data or {}).get("tipo")
    except Exception as e:
        if log: log.error(f"trigger {trigger}: no se pudo leer el tipo del stream {stream_id!r} ({e}) — no se escribe")
        return False
    if not trigger_permitido(tipo, trigger):
        if log: log.error(f"[BARRERA] trigger {trigger} NO permitido en stream tipo={tipo!r} ({stream_id!r}) — no se escribe")
        return False
    supabase.table("mensajes").insert(fila).execute()
    return True


def stream_permite_proceso(supabase, stream_id, proceso: str, log=None) -> bool:
    """¿El contrato del stream incluye este proceso? Falla cerrado (False) si no se puede leer el tipo."""
    if not stream_id:
        return False
    try:
        tipo = (supabase.table("streams").select("tipo").eq("id", stream_id).single().execute().data or {}).get("tipo")
    except Exception as e:
        if log: log.error(f"no se pudo leer el tipo del stream {stream_id!r} ({e})")
        return False
    c = CONTRATOS.get(tipo)
    return bool(c and proceso in c.procesos)
