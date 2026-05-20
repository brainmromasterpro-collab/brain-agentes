"""
BRAIN MRO Master Pro — Worker Principal
========================================
Corre ambos agentes en paralelo en un solo proceso de Railway.
  - agente_buscador: procesa jobs de búsqueda y ranking
  - agente_imagen:   procesa jobs de fotos y optimización

Start command en Railway: python main.py
"""

import threading
import logging

log = logging.getLogger("main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

if __name__ == "__main__":
    import agente_buscador
    import agente_imagen
    import agente_publicador
    import agente_monitor

    log.info("Iniciando Brain MRO Master Pro workers...")

    t1 = threading.Thread(target=agente_buscador.main,   name="buscador",   daemon=True)
    t2 = threading.Thread(target=agente_imagen.main,     name="imagen",     daemon=True)
    t3 = threading.Thread(target=agente_publicador.main, name="publicador", daemon=True)
    t4 = threading.Thread(target=agente_monitor.main,    name="monitor",    daemon=True)

    t1.start()
    t2.start()
    t3.start()
    t4.start()

    log.info("4 agentes corriendo: buscador, imagen, publicador, monitor. Esperando...")
    t1.join()
    t2.join()
    t3.join()
    t4.join()
