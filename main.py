"""
BRAIN MRO Master Pro — Worker Principal
========================================
Corre los agentes en Railway. QUÉ agentes corren se controla con la env var AGENTS,
para poder AISLAR el chat en su propio proceso/servicio (evita que el GIL y los
picos del publicador/Chromium lo congelen o lo tumben).

  AGENTS no seteada  → corre los 5 (comportamiento actual, sin cambios)
  AGENTS=chat        → corre SOLO el chat (para un servicio dedicado y liviano)
  AGENTS=buscador,imagen,publicador,monitor → corre los workers pesados (sin chat)

Recomendado: dos servicios en Railway (mismo repo/imagen):
  - Servicio "chat"     → AGENTS=chat
  - Servicio "workers"  → AGENTS=buscador,imagen,publicador,monitor

Start command en Railway: python main.py
"""

import os
import threading
import logging

log = logging.getLogger("main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def _latido(activos: list):
    """LATIDO del servicio de workers → resource_status(workers/heartbeat) cada 60s.

    Por qué existe: Railway DUERME el servicio cuando no se usa (p.ej. varios días sin actividad).
    Dormido, los jobs se quedan en la cola sin procesarse y la falla es SILENCIOSA. El monitor no
    sirve para avisarlo porque vive en este mismo servicio y se duerme también. Con este latido, el
    chat y el frontend (que sí están despiertos cuando el usuario trabaja) detectan que los workers
    no están y avisan ANTES de encolar trabajo.
    """
    import time
    import agente_monitor
    while True:
        try:
            agente_monitor.upsert("workers", "heartbeat", valor_texto=",".join(activos), estado="ok")
        except Exception as e:
            log.warning(f"latido: {e}")
        time.sleep(60)


if __name__ == "__main__":
    import agente_buscador
    import agente_imagen
    import agente_publicador
    import agente_chat
    import agente_monitor
    import agente_lector

    disponibles = {
        "buscador":   agente_buscador.main,
        "imagen":     agente_imagen.main,
        "publicador": agente_publicador.main,
        "chat":       agente_chat.main,
        "monitor":    agente_monitor.main,
        "lector":     agente_lector.main,
    }

    agents_env = os.environ.get("AGENTS", "").strip().lower()
    if not agents_env or agents_env == "all":
        seleccionados = list(disponibles.keys())
    else:
        seleccionados = [a.strip() for a in agents_env.split(",") if a.strip() in disponibles]
        if not seleccionados:
            log.warning(f"AGENTS='{agents_env}' no coincide con ninguno; corriendo TODOS por defecto")
            seleccionados = list(disponibles.keys())

    # El lector de Gmail necesita las creds de Gmail (servicio chat) → corre junto al chat aunque
    # AGENTS=chat no lo liste explícitamente.
    if "chat" in seleccionados and "lector" not in seleccionados:
        seleccionados.append("lector")

    log.info(f"Iniciando Brain MRO Master Pro — agentes: {seleccionados}")

    # Solo el servicio que corre workers reales late (el servicio de chat no debe reportar workers vivos).
    if any(a in seleccionados for a in ("buscador", "imagen", "publicador")):
        threading.Thread(target=_latido, args=(seleccionados,), name="latido", daemon=True).start()
        log.info("Latido de workers activo → resource_status(workers/heartbeat) cada 60s")

    hilos = []
    for nombre in seleccionados:
        t = threading.Thread(target=disponibles[nombre], name=nombre, daemon=True)
        t.start()
        hilos.append(t)

    log.info(f"{len(hilos)} agente(s) corriendo: {seleccionados}. Esperando...")
    for t in hilos:
        t.join()
