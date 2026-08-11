"""
Test end-to-end del RECEIVING v2 (sub-proceso anidado con ciclo de vida) — corre las funciones
REALES de agente_chat.py contra 1CRM real y un Purchase Order de prueba (TEST BRAIN MOCK), en un
stream de Supabase temporal que se borra al final.

Ciclo cubierto:
  1. Activar (order confirmation / packing list) -> [RECEIVING_ESTADO] esperando, guarda packing
     list de referencia.
  2. Tracking -> [RECEIVING_ESTADO] en_transito.
  3. Iniciar recepción -> [RECEIVING_RECEPCION] checklist (con cantidad_packing/sugerida).
  4. Confirmar entrega PARCIAL -> [RECEIVING_CONFIRMADO] parcial. REGLA CLAVE: el shipping_stage
     nativo del PO NO cambia (se queda Ordered); el stock SÍ sube.
  5. Segunda entrega -> completa -> el PO SÍ pasa a Received.
  6. Deshacer -> revierte el stock.

Uso: python3 test_receiving.py
"""
import json
import sys
import uuid

from dotenv import load_dotenv
load_dotenv()

import agente_chat as ac
import compra_proveedor as cp
import sales_order as so


def ok(cond: bool, msg: str) -> None:
    if not cond:
        print(f"❌ FALLÓ: {msg}")
        sys.exit(1)
    print(f"✅ {msg}")


def ultimo_marker(stream_id: str, marker: str) -> dict:
    rows = (ac.supabase.table("mensajes").select("content")
            .eq("stream_id", stream_id).order("created_at", desc=True).limit(30).execute())
    for r in rows.data or []:
        content = r.get("content") or ""
        if content.startswith(marker):
            return json.loads(content[len(marker):])
    raise AssertionError(f"no se encontró ningún mensaje con el marcador {marker}")


def stage_po(po_id: str) -> str:
    return so._crm_get(f"data/PurchaseOrder/{po_id}").get("record", {}).get("shipping_stage", "")


def main():
    print("=== 1. PO de prueba (TEST BRAIN MOCK) ===")
    draft = {
        "so_id": "58b250c5-4652-fc3d-ab8a-6a73a0407fa2",
        "proveedor_nombre": "TEST BRAIN MOCK Supplier — v2",
        "currency_id": "-99", "terminos_pago": "Net 15",
        "nombre": f"TEST BRAIN MOCK v2 — receiving {uuid.uuid4().hex[:6]}",
        "lineas": [
            {"name": "1N4001 Rectifier Diode 1A 50V", "mfr_part_no": "1N4001", "quantity": 10, "unit_price": 0.05},
            {"name": "LM7805 Voltage Regulator TO-220", "mfr_part_no": "LM7805", "quantity": 4, "unit_price": 0.40},
        ],
    }
    creado = cp.crear_po_y_ap(draft)
    ok(creado.get("ok"), f"PO+Bill creados: {creado.get('po_url')}")
    po_id = creado["po_id"]
    ok(stage_po(po_id) == "Ordered", "PO arranca en Ordered")

    print("\n=== 2. Stream temporal ===")
    uid = ac.supabase.table("streams").select("user_id").eq("tipo", "compras").limit(1).execute().data[0]["user_id"]
    stream_id = ac.supabase.table("streams").insert({
        "nombre": "TEST BRAIN MOCK — Receiving v2 (borrar)", "tipo": "compras",
        "user_id": uid, "auto_detectar": False,
    }).execute().data[0]["id"]
    print("stream_id:", stream_id)

    try:
        # Candidato tal como lo entrega pos_esperando_recepcion (lo que el widget reenvía al activar)
        cand = next(p for p in cp.pos_esperando_recepcion() if p["id"] == po_id)

        print("\n=== 3. Activar con packing list (order confirmation/packing list) ===")
        doc = {"tipo": "packing_list", "items": [
            {"nombre": "1N4001 Rectifier Diode", "mfr_part_no": "1N4001", "cantidad": 10},
            {"nombre": "LM7805 Voltage Regulator", "mfr_part_no": "LM7805", "cantidad": 4},
        ], "tracking": "", "notas": ""}
        ac._activar_receiving(stream_id, po_id, cand, doc)
        est = ultimo_marker(stream_id, "[RECEIVING_ESTADO]")
        ok(est.get("estatus") == "esperando", f"estatus tras activar: {est.get('estatus')}")
        ok((est.get("packing_list") or {}).get("items"), "guardó el packing list de referencia")
        ok(est.get("so_nombre") == "TEST BRAIN MOCK 4 — SO para probar compras", f"trae la SO vinculada: {est.get('so_nombre')}")
        activos = ac._receivings_activos(stream_id)
        ok(len(activos) == 1 and activos[0]["po_id"] == po_id, "aparece como receiving activo")

        print("\n=== 4. Tracking -> en tránsito ===")
        ac._marcar_en_transito(stream_id, po_id, "1Z999TESTV2")
        est2 = ultimo_marker(stream_id, "[RECEIVING_ESTADO]")
        ok(est2.get("estatus") == "en_transito", f"estatus tras tracking: {est2.get('estatus')}")
        ok(est2.get("tracking") == "1Z999TESTV2", "guardó el tracking")
        ok((est2.get("packing_list") or {}).get("items"), "conservó el packing list de referencia al pasar a tránsito")

        print("\n=== 5. Iniciar recepción -> checklist ===")
        ac._iniciar_recepcion(stream_id, po_id, guia="1Z999TESTV2")
        recl = ultimo_marker(stream_id, "[RECEIVING_RECEPCION]")
        lineas = {l["mfr_part_no"]: l for l in recl.get("lineas") or []}
        ok(lineas["1N4001"]["cantidad_packing"] == 10, f"checklist trae cantidad del packing list: {lineas['1N4001']}")
        ok(lineas["1N4001"]["cantidad_sugerida"] == 10 and lineas["1N4001"]["recibido"] is True, "precheck sugerido correcto")
        estado_anterior = recl.get("estado_anterior")
        ok(estado_anterior == "Ordered", f"estado_anterior = {estado_anterior}")

        print("\n=== 6. Confirmar entrega PARCIAL (6/10 diodos, 4/4 reguladores) ===")
        ac._confirmar_recepcion_checklist(stream_id, po_id, {"1N4001": 6, "LM7805": 4}, estado_anterior)
        conf1 = ultimo_marker(stream_id, "[RECEIVING_CONFIRMADO]")
        ok(conf1.get("ok") and conf1.get("completo") is False, "parcial confirmada, completo=False")
        ok(conf1.get("estatus_receiving") == "parcial", f"estatus_receiving: {conf1.get('estatus_receiving')}")
        ok(stage_po(po_id) == "Ordered", "REGLA CLAVE: tras parcial el PO SIGUE en Ordered (no Partially Received)")

        print("\n=== 7. Segunda entrega (4 diodos restantes) -> completa -> PO a Received ===")
        ac._iniciar_recepcion(stream_id, po_id, guia="1Z999TESTV2-B")
        recl2 = ultimo_marker(stream_id, "[RECEIVING_RECEPCION]")
        l2 = {l["mfr_part_no"]: l for l in recl2.get("lineas") or []}
        ok(l2["1N4001"]["cantidad_restante"] == 4 and l2["1N4001"]["cantidad_recibida_previo"] == 6,
           f"la 2a vez descuenta lo recibido: {l2['1N4001']}")
        ac._confirmar_recepcion_checklist(stream_id, po_id, {"1N4001": 4}, recl2.get("estado_anterior"))
        conf2 = ultimo_marker(stream_id, "[RECEIVING_CONFIRMADO]")
        ok(conf2.get("completo") is True, "segunda entrega completa=True")
        ok(stage_po(po_id) == "Received", "ciclo completo -> el PO SÍ pasa a Received")
        ok(len(ac._receivings_activos(stream_id)) == 0, "ya no queda ningún receiving activo")

        print("\n=== 8. Deshacer la 2a entrega -> revierte stock ===")
        ac._deshacer_receiving(stream_id, po_id, conf2.get("cantidades") or {}, conf2.get("estado_anterior", ""))
        desh = ultimo_marker(stream_id, "[ACCION_DESHECHA]")
        ok(desh.get("ok"), "deshacer OK")

        print("\n=== 9. Router texto: tracking suelto abre cotejo de tracking ===")
        ac._procesar_evidencia_receiving(stream_id, "tracking 1Z111ABC del envío")
        cot = ultimo_marker(stream_id, "[RECEIVING_COTEJO]")
        ok(cot.get("modo") == "tracking", f"modo del cotejo por texto: {cot.get('modo')}")

        print("\n🎉 TODOS LOS PASOS DEL RECEIVING v2 PASARON")
        print(f"   PO de prueba (queda en 1CRM): {creado.get('po_url')}")

    finally:
        print("\n=== Limpieza ===")
        ac.supabase.table("mensajes").delete().eq("stream_id", stream_id).execute()
        ac.supabase.table("streams").delete().eq("id", stream_id).execute()
        print("stream y mensajes de prueba borrados.")


if __name__ == "__main__":
    main()
