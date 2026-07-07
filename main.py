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

if __name__ == "__main__":
    import agente_buscador
    import agente_imagen
    import agente_publicador
    import agente_chat
    import agente_monitor

    disponibles = {
        "buscador":   agente_buscador.main,
        "imagen":     agente_imagen.main,
        "publicador": agente_publicador.main,
        "chat":       agente_chat.main,
        "monitor":    agente_monitor.main,
    }

    agents_env = os.environ.get("AGENTS", "").strip().lower()
    if not agents_env or agents_env == "all":
        seleccionados = list(disponibles.keys())
    else:
        seleccionados = [a.strip() for a in agents_env.split(",") if a.strip() in disponibles]
        if not seleccionados:
            log.warning(f"AGENTS='{agents_env}' no coincide con ninguno; corriendo TODOS por defecto")
            seleccionados = list(disponibles.keys())

    log.info(f"Iniciando Brain MRO Master Pro — agentes: {seleccionados}")

    hilos = []
    for nombre in seleccionados:
        t = threading.Thread(target=disponibles[nombre], name=nombre, daemon=True)
        t.start()
        hilos.append(t)

    log.info(f"{len(hilos)} agente(s) corriendo: {seleccionados}. Esperando...")
    for t in hilos:
        t.join()
