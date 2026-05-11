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

    log.info("Iniciando Brain MRO Master Pro workers...")

    t1 = threading.Thread(target=agente_buscador.main, name="buscador", daemon=True)
    t2 = threading.Thread(target=agente_imagen.main, name="imagen", daemon=True)

    t1.start()
    t2.start()

    log.info("Ambos agentes corriendo. Esperando...")
    t1.join()
    t2.join()
