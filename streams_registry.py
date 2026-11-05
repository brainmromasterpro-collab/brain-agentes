"""Registro ÚNICO de qué proceso es dueño de cada herramienta y qué procesos puede usar cada stream.

Existe para que los streams NO se mezclen: agente_chat.TOOLS_POR_TIPO se valida contra esta tabla (en
test_tools_por_tipo.py y al arrancar el chat). Una herramienta nueva sin clasificar, o un stream que reciba
herramientas de un proceso que no le toca, es un ERROR — no se puede olvidar ni "heredar" por accidente.

Procesos:
  consulta          lecturas inocuas del CRM (producto, cliente, contacto, cotización) y utilidades
  proveedores       buscar_proveedores_crm (proveedor ≠ cliente: solo donde se compra)
  oportunidades     cuenta + contacto + oportunidad en 1CRM (ventas)
  correo            Gmail
  pipeline_rfq      RFQ de catálogo: buscar proveedor/precio de un producto, foto y publicación
  publicar_producto publicar productos en 1CRM desde link o ficha técnica
"""

PROCESO_DE_HERRAMIENTA = {
    **{t: "consulta" for t in (
        "buscar_productos_crm", "ver_producto_crm", "listar_productos_crm", "buscar_clientes_crm",
        "ver_cliente_crm", "listar_cotizaciones_crm", "buscar_contactos_crm", "ver_contactos_cuenta_crm",
        "notificar_sistema", "consultar_metricas", "buscar_internet")},
    "buscar_proveedores_crm": "proveedores",
    **{t: "oportunidades" for t in (
        "crear_cuenta_crm", "crear_contacto_crm", "actualizar_cuenta_crm", "actualizar_contacto_crm",
        "listar_oportunidades_crm", "crear_oportunidad_crm", "actualizar_oportunidad_crm")},
    **{t: "correo" for t in (
        "enviar_email_gmail", "diagnostico_gmail", "leer_emails_gmail", "buscar_email_gmail",
        "escanear_emails_ventas")},
    **{t: "pipeline_rfq" for t in (
        "crear_rfqs_desde_texto", "consultar_rfqs", "obtener_opciones_rfq", "seleccionar_proveedor",
        "obtener_foto_rfq", "publicar_rfq", "publicar_sin_imagen_rfq", "verificar_lista_productos")},
    **{t: "publicar_producto" for t in (
        "extraer_producto_de_link", "extraer_ficha_pdf", "publicar_producto_link",
        "publicar_productos_desde_links")},
}

_BASE = {"consulta"}
PROCESOS_PERMITIDOS = {
    "rfq":          _BASE | {"oportunidades"},                                   # Oportunidad: NUNCA catálogo
    "correo":       _BASE | {"oportunidades", "correo"},
    "whatsapp":     _BASE | {"oportunidades", "correo"},
    "mensajeria":   _BASE | {"oportunidades", "correo"},
    "busquedas":    _BASE | {"proveedores", "pipeline_rfq"},
    "publicacion":  _BASE | {"publicar_producto"},
    "catalogo":     _BASE | {"proveedores", "pipeline_rfq", "publicar_producto"},
    "cotizacion":   _BASE | {"oportunidades", "correo"},
    "cotizaciones": _BASE | {"oportunidades", "correo"},
    "ordenes":      _BASE | {"proveedores"},
    "compras":      _BASE | {"proveedores"},
    "pagos":        _BASE | {"proveedores"},
}

# Mezclas que HOY existen a propósito o están pendientes de decidir. Aparecen como advertencia en cada
# verificación hasta que se resuelvan; no se pueden agregar nuevas sin escribirlas aquí con su motivo.
EXCEPCIONES_CONOCIDAS: dict = {}  # vacío: ya no hay mezclas conocidas


def verificar(tools_por_tipo: dict, nombres_tools: set) -> tuple[list[str], list[str]]:
    """Devuelve (errores, advertencias). Error = mezcla de procesos o herramienta sin clasificar."""
    errores, avisos = [], []
    for n in sorted(nombres_tools - PROCESO_DE_HERRAMIENTA.keys()):
        errores.append(f"herramienta «{n}» sin clasificar en streams_registry.PROCESO_DE_HERRAMIENTA")
    for tipo, tools in tools_por_tipo.items():
        permitidos = PROCESOS_PERMITIDOS.get(tipo)
        if permitidos is None:
            errores.append(f"stream «{tipo}» sin procesos permitidos en streams_registry.PROCESOS_PERMITIDOS")
            continue
        for n in sorted(tools):
            proc = PROCESO_DE_HERRAMIENTA.get(n)
            if proc and proc not in permitidos:
                errores.append(f"stream «{tipo}» recibe «{n}» del proceso «{proc}», que no le corresponde")
        for (t, proc), motivo in EXCEPCIONES_CONOCIDAS.items():
            if t == tipo and any(PROCESO_DE_HERRAMIENTA.get(n) == proc for n in tools):
                avisos.append(f"{tipo}: mezcla conocida con «{proc}» — {motivo}")
    return errores, avisos
