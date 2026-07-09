"""
AGENTE LECTOR — watcher de Gmail
=================================
Revisa la bandeja de brain.mromasterpro@gmail.com cada LECTOR_POLL segundos y, al detectar un
correo NUEVO (no visto antes), lo empuja a los streams de tipo correo/mensajeria: escribe un log
en vivo (verde) y una notificación tipo='correo_entrante'. Así el "Lector" vigila de verdad.

Baseline: en la PRIMERA vuelta marca los correos ya existentes como vistos SIN notificar (para no
spamear con la bandeja actual); solo notifica los que llegan después.

Corre junto al chat (necesita las credenciales de Gmail, GOOGLE_REFRESH_TOKEN, del servicio chat).
"""

import os
import json
import time
import logging

from agente_chat import tool_leer_emails_gmail, supabase

log = logging.getLogger("agente_lector")

POLL = int(os.environ.get("LECTOR_POLL", "60"))          # segundos entre revisiones
QUERY = os.environ.get("LECTOR_QUERY", "is:unread in:inbox newer_than:2d")


def _streams_correo() -> list[str]:
    """IDs de los streams de tipo correo/mensajeria (a los que se empuja el aviso)."""
    try:
        rows = supabase.table("streams").select("id,tipo").execute().data or []
        return [r["id"] for r in rows if (r.get("tipo") or "") in ("correo", "mensajeria")]
    except Exception as e:
        log.warning(f"No se pudieron leer streams de correo: {e}")
        return []


def _notificar(stream_id: str, email: dict) -> None:
    de = (email.get("de") or "")[:60]
    asunto = (email.get("asunto") or "(sin asunto)")[:80]
    try:
        supabase.table("stream_logs").insert({
            "stream_id": str(stream_id),
            "msg": f"Nuevo correo — {de}: {asunto}",
            "type": "ok",
        }).execute()
    except Exception as e:
        log.warning(f"stream_log correo falló: {e}")
    try:
        supabase.table("notificaciones").insert({
            "tipo": "correo_entrante",
            "titulo": f"Nuevo correo — {asunto}",
            "mensaje": json.dumps({
                "gmail_id": email.get("id"), "de": email.get("de", ""),
                "asunto": email.get("asunto", ""), "snippet": email.get("snippet", ""),
            }),
            "stream_id": str(stream_id),
            "leida": False,
        }).execute()
    except Exception as e:
        log.warning(f"notificación correo falló: {e}")
    # Tarjeta clickable EN el stream (mensaje del asistente con marcador). El frontend la renderiza
    # con un botón para procesar el correo. metadata.correo_entrante → el historial del chat la ignora.
    try:
        payload = json.dumps({
            "de": email.get("de", ""), "asunto": email.get("asunto", ""),
            "snippet": email.get("snippet", ""), "gmail_id": email.get("id", ""),
        }, ensure_ascii=False)
        supabase.table("mensajes").insert({
            "stream_id": str(stream_id), "role": "assistant",
            "content": f"[CORREO_ENTRANTE]{payload}",
            "procesado": True, "metadata": {"correo_entrante": True},
        }).execute()
    except Exception as e:
        log.warning(f"tarjeta de correo en stream falló: {e}")


def main() -> None:
    if not os.environ.get("GOOGLE_REFRESH_TOKEN"):
        log.warning("GOOGLE_REFRESH_TOKEN no configurado — el lector de Gmail NO arranca.")
        return
    log.info(f"Lector Gmail arrancando (poll={POLL}s, query={QUERY!r})")
    seen: set = set()
    first = True
    while True:
        try:
            res = tool_leer_emails_gmail(max_emails=15, query=QUERY)
            emails = res.get("emails", []) if isinstance(res, dict) else []
            nuevos = [e for e in emails if e.get("id") and e["id"] not in seen]
            streams = _streams_correo() if (nuevos and not first) else []
            for e in nuevos:
                seen.add(e["id"])
                if first:
                    continue  # baseline: no notificar la bandeja ya existente
                for sid in streams:
                    _notificar(sid, e)
                if streams:
                    log.info(f"Correo nuevo → {len(streams)} stream(s): {(e.get('asunto') or '')[:50]}")
            if first:
                log.info(f"Baseline: {len(seen)} correos existentes marcados como vistos.")
            first = False
        except Exception as e:
            log.error(f"Error en loop del lector: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
