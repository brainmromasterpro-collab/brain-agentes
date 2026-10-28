"""Sincronización Alegra → 1CRM (cobros y estatus de las facturas que se crearon desde un stream).

Corre como agente en el servicio de chat (AGENTS incluye "alegra" automáticamente junto al chat) y
revisa Alegra cada ALEGRA_SYNC_SEGUNDOS (default 300). Sin URL pública ni webhooks.

La FUENTE DE VERDAD de qué facturas vigilar es el historial de mensajes ([ALEGRA_CREADA]); lo ya
sincronizado se lee de [ALEGRA_COBRO] / [ALEGRA_ESTATUS] (mismo patrón "el historial es la verdad"
que el resto del sistema), así que no hace falta una tabla nueva y un reinicio no duplica nada.

Por cada factura vigilada:
  • cobro nuevo en Alegra (totalPaid sube)  → Payment en 1CRM ligado a la Invoice espejo
    (pagos.registrar_pago, direction=incoming) + aviso [ALEGRA_COBRO] en el stream.
  • factura timbrada/abierta (ya no es borrador) → se anota folio/UUID en la Invoice de 1CRM y se
    marca la SO como facturada.
  • factura anulada en Alegra (void) → se cancela la Invoice espejo y se avisa.
"""
import json
import logging
import os
import time

import alegra
import pagos
from agente_chat import supabase, _confirmar_cierre_venta, _facturado_so

log = logging.getLogger("alegra_sync")

INTERVALO = int(os.environ.get("ALEGRA_SYNC_SEGUNDOS", "300"))


def _mensajes(marker: str) -> list[dict]:
    """Payloads JSON (con stream_id) de los mensajes del historial que empiezan con `marker`."""
    out = []
    try:
        rows = (supabase.table("mensajes").select("stream_id,content,created_at")
                .ilike("content", f"%{marker}%").order("created_at").execute().data or [])
    except Exception as e:
        log.warning(f"historial {marker}: {e}")
        return out
    for r in rows:
        c = r.get("content") or ""
        if not c.startswith(marker):
            continue
        try:
            p = json.loads(c[len(marker):])
        except Exception:
            continue
        p["_stream_id"] = r["stream_id"]
        out.append(p)
    return out


def facturas_vigiladas() -> dict:
    """{alegra_invoice_id: {stream_id, so_id, crm_invoice_id, numero, cliente}} desde [ALEGRA_CREADA]."""
    out = {}
    for p in _mensajes("[ALEGRA_CREADA]"):
        if p.get("ok") and p.get("invoice_id"):
            out[str(p["invoice_id"])] = {
                "stream_id": p["_stream_id"], "so_id": p.get("so_id"),
                "crm_invoice_id": (p.get("crm_invoice") or {}).get("id", ""),
                "numero": p.get("numero", ""), "cliente": p.get("cliente", ""),
            }
    return out


def _emitir(stream_id: str, marker: str, payload: dict) -> None:
    supabase.table("mensajes").insert({
        "stream_id": stream_id, "role": "assistant",
        "content": marker + json.dumps(payload, ensure_ascii=False),
        "procesado": True, "metadata": {"alegra_sync": True},
    }).execute()


def sincronizar() -> dict:
    """Una pasada de sincronización. Devuelve conteos (útil para logs/pruebas)."""
    res = {"revisadas": 0, "cobros": 0, "timbradas": 0, "anuladas": 0}
    vigiladas = facturas_vigiladas()
    if not vigiladas:
        return res
    cobrado = {}
    for p in _mensajes("[ALEGRA_COBRO]"):
        cobrado[str(p.get("alegra_id"))] = max(cobrado.get(str(p.get("alegra_id")), 0), float(p.get("pagado") or 0))
    estatus = {(str(p.get("alegra_id")), p.get("evento")) for p in _mensajes("[ALEGRA_ESTATUS]")}

    for aid, info in vigiladas.items():
        est = alegra.estado_factura(aid)
        if not est.get("ok"):
            continue  # borrador eliminado o error transitorio
        res["revisadas"] += 1
        stream_id, crm_id = info["stream_id"], info["crm_invoice_id"]

        # Anulada en Alegra → cancelar espejo.
        if est["estado"] == "void" and (aid, "anulada") not in estatus:
            if crm_id:
                alegra.cancelar_espejo_1crm(crm_id)
            _emitir(stream_id, "[ALEGRA_ESTATUS]", {"alegra_id": aid, "evento": "anulada", "numero": info["numero"],
                                                    "cliente": info["cliente"], "url": est["url"]})
            res["anuladas"] += 1
            continue

        # Ya no es borrador (abierta / cobrada) → folio y UUID al espejo; SO facturada.
        if est["estado"] in ("open", "closed") and (aid, "timbrada") not in estatus:
            if crm_id:
                alegra.anotar_espejo_1crm(crm_id, f"Alegra: factura emitida ({est['estado']}) UUID {est.get('uuid') or '—'}")
            _emitir(stream_id, "[ALEGRA_ESTATUS]", {"alegra_id": aid, "evento": "timbrada", "numero": info["numero"],
                                                    "cliente": info["cliente"], "uuid": est.get("uuid"), "url": est["url"]})
            try:
                if info["so_id"] and not _facturado_so(stream_id, info["so_id"]):
                    _confirmar_cierre_venta(stream_id, info["so_id"], True, "")
            except Exception as e:
                log.warning(f"marcar facturada {info['so_id']}: {e}")
            res["timbradas"] += 1

        # Cobros nuevos → Payment en 1CRM.
        previo = cobrado.get(aid, 0.0)
        delta = round(est["pagado"] - previo, 2)
        if delta > 0.005 and crm_id:
            pago = pagos.registrar_pago("invoice", crm_id, delta, {"referencia": f"Alegra {info['numero'] or aid}"})
            if pago.get("ok"):
                _emitir(stream_id, "[ALEGRA_COBRO]", {
                    "alegra_id": aid, "numero": info["numero"], "cliente": info["cliente"], "monto": delta,
                    "pagado": est["pagado"], "total": est["total"], "saldo": est["saldo"],
                    "payment_url": pago.get("payment_url"), "invoice_url": pago.get("registro_url"), "url": est["url"]})
                res["cobros"] += 1
            else:
                log.warning(f"cobro Alegra {aid}: no se pudo registrar el Payment: {pago.get('error')}")
    return res


def main() -> None:
    log.info(f"Sincronización Alegra→1CRM activa, cada {INTERVALO}s")
    while True:
        try:
            if alegra.configurado():
                r = sincronizar()
                if r["cobros"] or r["timbradas"] or r["anuladas"]:
                    log.info(f"alegra_sync: {r}")
        except Exception as e:
            log.error(f"alegra_sync: {e}")
        time.sleep(INTERVALO)
