"""Validación fiscal (SAT) de las Invoices que se capturan A MANO en 1CRM.

Las facturas que pasan por el flujo de Alegra ya se validan en alegra.py; esto cubre las Invoices que
alguien mete directo en 1CRM. Corre como agente en el servicio de chat (auto-añadido junto al chat) y
revisa cada VALIDADOR_SEGUNDOS (default 300) las Invoices recién capturadas (VALIDADOR_DIAS, default 2):
no se tocan las históricas, porque la mayoría de las cuentas antiguas aún no tiene sus datos fiscales.

El resultado se deja en dos lugares:
  • bloque [VALIDACIÓN SAT] en la Descripción de la Invoice (visible donde se captura; se reescribe solo
    si el resultado cambia, así que no hay avisos duplicados ni escrituras en cada pasada);
  • aviso en el stream de Pagos cuando aparecen problemas.
"""
import datetime
import logging
import os
import re
import time

import alegra
import sales_order
import validador_sat

log = logging.getLogger("validar_facturas")

INTERVALO = int(os.environ.get("VALIDADOR_SEGUNDOS", "300"))
VENTANA_DIAS = int(os.environ.get("VALIDADOR_DIAS", "2"))
INI, FIN = "[VALIDACIÓN SAT]", "[/VALIDACIÓN SAT]"
_BLOQUE = re.compile(re.escape(INI) + r".*?" + re.escape(FIN), re.S)


def validar_invoice(inv: dict, acc: dict) -> dict:
    """{'errores': [...], 'avisos': [...]} para una Invoice de 1CRM con los datos fiscales de su cuenta."""
    if not acc.get("id"):
        return {"errores": ["La Invoice no tiene cuenta de facturación."], "avisos": []}
    regimen_1crm = acc.get("fiscal_regime") or ""
    regimen = alegra._REGIMEN_SAT.get(regimen_1crm, "")
    metodo = (acc.get("MetodoDpago") or "").upper()
    # Domicilio fiscal: el de la cuenta; la Invoice puede traer una copia distinta.
    val = validador_sat.validar(
        rfc=acc.get("rfc") or "", regimen=regimen, cp=acc.get("billing_address_postalcode") or "",
        estado=acc.get("billing_address_state") or "", uso=acc.get("USOCFDI") or "", metodo=metodo,
        forma=acc.get("FormadePago") or "", pais=acc.get("billing_address_country") or "Mexico",
        nombre=acc.get("razon_social") or "")
    errores, avisos = list(val["errores"]), list(val["avisos"])
    if not regimen:
        avisos.append("La cuenta no tiene un régimen fiscal con código del SAT en 1CRM"
                      + (f" («{regimen_1crm}»)." if regimen_1crm else "."))
    dias = alegra._dias_credito(inv.get("terms") or "")
    if metodo == "PUE" and dias > 0:
        avisos.append(f"La Invoice tiene condiciones a crédito ({inv.get('terms')}) pero la cuenta factura con PUE (una sola exhibición).")
    if metodo == "PPD" and dias == 0 and inv.get("terms"):
        avisos.append(f"La Invoice es de contado ({inv.get('terms')}) pero la cuenta factura con PPD (diferido).")
    cp_inv = (inv.get("billing_address_postalcode") or "").strip()
    cp_acc = (acc.get("billing_address_postalcode") or "").strip()
    if cp_inv and cp_acc and cp_inv != cp_acc:
        avisos.append(f"El C.P. de la Invoice ({cp_inv}) no coincide con el fiscal de la cuenta ({cp_acc}).")
    return {"errores": errores, "avisos": avisos}


def _bloque(val: dict) -> str:
    if not val["errores"] and not val["avisos"]:
        return f"{INI}\n✅ Datos fiscales válidos para facturar (CFDI 4.0).\n{FIN}"
    l = [INI]
    l += [f"⛔ {e}" for e in val["errores"]] + [f"⚠ {a}" for a in val["avisos"]]
    return "\n".join(l + [FIN])


def _stream_pagos() -> str:
    from agente_chat import supabase
    r = supabase.table("streams").select("id").eq("tipo", "pagos").limit(1).execute().data or []
    return r[0]["id"] if r else ""


def revisar(limite: int = 40) -> dict:
    """Una pasada. Devuelve conteos (útil para logs y pruebas)."""
    res = {"revisadas": 0, "con_problemas": 0, "escritas": 0}
    desde = (datetime.datetime.utcnow() - datetime.timedelta(days=VENTANA_DIAS)).strftime("%Y-%m-%d %H:%M:%S")
    recs = sales_order._crm_get("data/Invoice", {"order_by": "date_entered desc", "limit": limite}).get("records", [])
    for r in recs:
        inv = sales_order._crm_get(f"data/Invoice/{r.get('id')}").get("record", {})
        if not inv.get("id"):
            continue
        if (inv.get("date_entered") or "") < desde:
            break  # ordenadas por fecha de alta: lo que sigue es más viejo
        desc = inv.get("description") or ""
        if str(inv.get("cancelled") or "0") in ("1", "True", "true") or "Alegra ID:" in desc:
            continue  # canceladas, y espejos de Alegra (ya se validaron en el flujo)
        acc = sales_order._crm_get(f"data/Account/{inv['billing_account_id']}").get("record", {}) \
            if inv.get("billing_account_id") else {}
        val = validar_invoice(inv, acc)
        res["revisadas"] += 1
        bloque = _bloque(val)
        actual = _BLOQUE.search(desc)
        if actual and actual.group(0) == bloque:
            continue
        nueva = _BLOQUE.sub(lambda m: bloque, desc) if actual else ((desc.rstrip() + "\n\n") if desc.strip() else "") + bloque
        p = sales_order._crm_patch("Invoice", inv["id"], {"description": nueva})
        if "error" in p:
            log.warning(f"validación SAT {inv.get('name')}: no se pudo escribir en la Invoice: {p.get('error')}")
            continue
        res["escritas"] += 1
        if val["errores"] or val["avisos"]:
            res["con_problemas"] += 1
            _avisar(inv, acc, val)
    return res


def _avisar(inv: dict, acc: dict, val: dict) -> None:
    sid = _stream_pagos()
    if not sid:
        return
    from agente_chat import supabase
    url = f"{sales_order.CRM_BASE}/index.php?module=Invoice&action=DetailView&record={inv['id']}"
    l = [f"**Validación fiscal (SAT):** la Invoice [{inv.get('name') or inv.get('full_number') or inv['id']}]({url}) "
         f"de {acc.get('name') or 'sin cuenta'} tiene problemas para facturarse:"]
    l += [f"- ⛔ {e}" for e in val["errores"]] + [f"- ⚠ {a}" for a in val["avisos"]]
    supabase.table("mensajes").insert({"stream_id": sid, "role": "assistant", "content": "\n".join(l),
                                       "procesado": True, "metadata": {"validacion_sat": True}}).execute()


def main() -> None:
    log.info(f"Validador fiscal de Invoices activo, cada {INTERVALO}s (ventana {VENTANA_DIAS} día(s))")
    while True:
        try:
            r = revisar()
            if r["escritas"]:
                log.info(f"validar_facturas: {r}")
        except Exception as e:
            log.error(f"validar_facturas: {e}")
        time.sleep(INTERVALO)
