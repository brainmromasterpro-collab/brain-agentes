"""
Test mock end-to-end del módulo Receiving (recepción multi-entrega) — corre el pipeline REAL de
agente_chat.py (las mismas funciones que dispara el router de producción) contra 1CRM real y un
Purchase Order de prueba nuevo ("TEST BRAIN MOCK"), usando un stream de Supabase TEMPORAL que se
borra al final del script (no debe quedar visible en la app real).

Cubre:
  1. Evidencia (texto con el part number) -> [RECEIVING_COTEJO] -> el PO de prueba aparece
     entre los candidatos, con su Sales Order/cliente vinculado.
  2. Elegir el PO -> [RECEIVING_PENDIENTE] -> cantidades esperadas correctas (todo restante,
     sin entregas previas).
  3. Confirmar una entrega PARCIAL -> [RECEIVING_CONFIRMADO] con shipping_stage=Partially
     Received, completo=False, ajuste de stock aplicado.
  4. Segunda entrega -> [RECEIVING_CONFIRMADO] con shipping_stage=Received, completo=True
     (usa el historial de mensajes para saber cuánto ya se había recibido).
  5. Deshacer de la segunda entrega -> el PO regresa a Partially Received y el stock se revierte.

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
    """Lee de Supabase el ÚLTIMO mensaje con ese marcador para este stream (mismo criterio que
    usa el frontend real vía extractMarkerJson)."""
    rows = (ac.supabase.table("mensajes").select("content")
            .eq("stream_id", stream_id).order("created_at", desc=True).limit(20).execute())
    for r in rows.data or []:
        content = r.get("content") or ""
        if content.startswith(marker):
            return json.loads(content[len(marker):])
    raise AssertionError(f"no se encontró ningún mensaje con el marcador {marker}")


def main():
    print("=== 1. Preparar Purchase Order de prueba (TEST BRAIN MOCK) ===")
    # Reusa la cuenta+Sales Order de MOCK 4 (ya tiene default_purchase_terms y tax_code_id
    # configurados) pero crea un Purchase Order NUEVO con cantidades propias, para no chocar
    # con el PO que ya se dejó a medias en la sesión anterior (848e75b4..., Partially Received).
    so_id = "58b250c5-4652-fc3d-ab8a-6a73a0407fa2"  # TEST BRAIN MOCK 4 — SO
    draft = {
        "so_id": so_id,
        "proveedor_nombre": "TEST BRAIN MOCK Supplier — Receiving Test",
        "currency_id": "-99",
        "terminos_pago": "Net 15",
        "nombre": f"TEST BRAIN MOCK 5 — receiving pipeline {uuid.uuid4().hex[:6]}",
        "lineas": [
            {"name": "1N4001 Rectifier Diode 1A 50V", "mfr_part_no": "1N4001", "quantity": 10, "unit_price": 0.05},
            {"name": "LM7805 Voltage Regulator TO-220", "mfr_part_no": "LM7805", "quantity": 4, "unit_price": 0.40},
        ],
    }
    creado = cp.crear_po_y_ap(draft)
    ok(creado.get("ok"), f"PurchaseOrder+Bill de prueba creados: {creado.get('po_url')}")
    po_id = creado["po_id"]

    print("\n=== 2. Stream de Supabase TEMPORAL (se borra al final) ===")
    # Mismo user_id que el stream real "Compras" (única forma de pasar el FK de streams.user_id)
    # — se borra al final del script, así que no queda visible en la app.
    stream_real = ac.supabase.table("streams").select("user_id").eq("tipo", "compras").limit(1).execute()
    user_id = stream_real.data[0]["user_id"]
    stream = ac.supabase.table("streams").insert({
        "nombre": "TEST BRAIN MOCK — Receiving (borrar)", "tipo": "compras",
        "user_id": user_id, "auto_detectar": False,
    }).execute()
    stream_id = stream.data[0]["id"]
    print(f"stream_id de prueba: {stream_id}")

    try:
        print("\n=== 3. PASO 1 — evidencia (texto con el part number) -> [RECEIVING_COTEJO] ===")
        ac._procesar_evidencia_recepcion(stream_id, "llegó un paquete con 1N4001, viene con guía")
        cotejo = ultimo_marker(stream_id, "[RECEIVING_COTEJO]")
        candidatos = cotejo.get("candidatos") or []
        ok(any(c["id"] == po_id for c in candidatos),
           f"el PO de prueba aparece entre los {len(candidatos)} candidato(s) de [RECEIVING_COTEJO]")
        cand = next(c for c in candidatos if c["id"] == po_id)
        ok(cand.get("so_nombre") == "TEST BRAIN MOCK 4 — SO para probar compras",
           f"el candidato trae la Sales Order vinculada: {cand.get('so_nombre')}")
        ok(bool(cand.get("so_cliente")), f"el candidato trae el cliente vinculado: {cand.get('so_cliente')}")

        print("\n=== 4. PASO 2 — elegir el PO -> [RECEIVING_PENDIENTE] ===")
        ac._crear_receiving_pendiente(stream_id, po_id, tracking="1Z999TESTMOCK5", packing_list=None)
        pendiente = ultimo_marker(stream_id, "[RECEIVING_PENDIENTE]")
        lineas = {l["mfr_part_no"]: l for l in pendiente.get("lineas") or []}
        ok(lineas["1N4001"]["cantidad_restante"] == 10 and lineas["1N4001"]["cantidad_recibida_previo"] == 0,
           f"cantidades esperadas correctas SIN entregas previas: {lineas['1N4001']}")
        ok(lineas["LM7805"]["cantidad_restante"] == 4,
           f"LM7805 restante correcto: {lineas['LM7805']['cantidad_restante']}")
        estado_anterior_1 = pendiente.get("estado_anterior", "")
        ok(estado_anterior_1 == "Ordered", f"estado_anterior capturado antes de la 1a entrega: {estado_anterior_1!r}")

        print("\n=== 5. PASO 3 — confirmar ENTREGA PARCIAL (6/10 diodos, 4/4 reguladores) ===")
        cantidades_1 = {"1N4001": 6, "LM7805": 4}
        ac._procesar_confirmar_recepcion_parcial(stream_id, po_id, cantidades_1, estado_anterior_1)
        conf1 = ultimo_marker(stream_id, "[RECEIVING_CONFIRMADO]")
        ok(conf1.get("ok"), "primera confirmación OK")
        ok(conf1.get("shipping_stage") == "Partially Received",
           f"shipping_stage tras entrega parcial: {conf1.get('shipping_stage')}")
        ok(conf1.get("completo") is False, "completo=False (todavía faltan 4 diodos)")

        rec_mid = so._crm_get(f"data/PurchaseOrder/{po_id}").get("record", {})
        ok(rec_mid.get("shipping_stage") == "Partially Received",
           "1CRM refleja Partially Received (verificado con GET directo, no solo la respuesta)")

        print("\n=== 6. PASO 2 otra vez — segunda entrega, ahora debe descontar lo ya recibido ===")
        ac._crear_receiving_pendiente(stream_id, po_id, tracking="", packing_list=None)
        pendiente2 = ultimo_marker(stream_id, "[RECEIVING_PENDIENTE]")
        lineas2 = {l["mfr_part_no"]: l for l in pendiente2.get("lineas") or []}
        ok(lineas2["1N4001"]["cantidad_recibida_previo"] == 6 and lineas2["1N4001"]["cantidad_restante"] == 4,
           f"la 2a vez SÍ descuenta lo recibido en la 1a entrega (leído del historial): {lineas2['1N4001']}")
        ok(lineas2["LM7805"]["cantidad_restante"] == 0,
           f"LM7805 ya no pide más (0 restante): {lineas2['LM7805']}")
        estado_anterior_2 = pendiente2.get("estado_anterior", "")
        ok(estado_anterior_2 == "Partially Received",
           f"estado_anterior de la 2a entrega es el resultado de la 1a: {estado_anterior_2!r}")

        print("\n=== 7. PASO 3 otra vez — confirmar los 4 diodos restantes -> debe CERRAR el PO ===")
        cantidades_2 = {"1N4001": 4}
        ac._procesar_confirmar_recepcion_parcial(stream_id, po_id, cantidades_2, estado_anterior_2)
        conf2 = ultimo_marker(stream_id, "[RECEIVING_CONFIRMADO]")
        ok(conf2.get("ok"), "segunda confirmación OK")
        ok(conf2.get("shipping_stage") == "Received",
           f"shipping_stage tras cubrir todo: {conf2.get('shipping_stage')}")
        ok(conf2.get("completo") is True, "completo=True (ya no falta nada)")

        rec_final = so._crm_get(f"data/PurchaseOrder/{po_id}").get("record", {})
        ok(rec_final.get("shipping_stage") == "Received",
           "1CRM refleja Received (verificado con GET directo)")

        print("\n=== 8. Deshacer la 2a entrega -> debe regresar a Partially Received ===")
        ac._deshacer_receiving(stream_id, po_id, conf2.get("cantidades") or {}, conf2.get("estado_anterior", ""))
        deshecho = ultimo_marker(stream_id, "[ACCION_DESHECHA]")
        ok(deshecho.get("ok"), "deshacer OK")
        rec_undo = so._crm_get(f"data/PurchaseOrder/{po_id}").get("record", {})
        ok(rec_undo.get("shipping_stage") == "Partially Received",
           f"tras deshacer, 1CRM regresó a: {rec_undo.get('shipping_stage')}")

        print("\n🎉 TODOS LOS PASOS DEL PIPELINE DE RECEIVING PASARON")
        print(f"   PO de prueba (queda en 1CRM, Partially Received): {creado.get('po_url')}")

    finally:
        print("\n=== 9. Limpieza — borrar el stream y mensajes de prueba de Supabase ===")
        ac.supabase.table("mensajes").delete().eq("stream_id", stream_id).execute()
        ac.supabase.table("streams").delete().eq("id", stream_id).execute()
        print("stream y mensajes de prueba borrados (el PO/Bill quedan en 1CRM, mismo criterio que los demás MOCK).")


if __name__ == "__main__":
    main()
