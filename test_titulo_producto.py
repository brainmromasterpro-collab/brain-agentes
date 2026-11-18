"""Título del producto publicado en 1CRM: «MODELO / Marca / descripción corta» sin repetir el modelo ni la marca."""
import agente_chat as ac

_fallos = []
def check(nombre, ok, detalle=""):
    print(("  ✅ " if ok else "  ❌ ") + nombre + ("" if ok else f" — {detalle}"))
    if not ok: _fallos.append(nombre)

c = ac._construir_titulo_producto

t = c("GS-51PC", "KEYENCE", "GS-51PC, Signal Release Type Standard PNP M12 Connector Type, GS Series, KEYENCE, Mexico.")
check("caso real GS-51PC: el modelo ya no sale dos veces", t.count("GS-51PC") == 1, t)
check("caso real GS-51PC: la marca tampoco se repite", t.lower().count("keyence") == 1, t)
check("caso real GS-51PC: queda «modelo / marca / descripción»",
      t == "GS-51PC / KEYENCE / Signal Release Type Standard PNP M12 Connector Type, GS Series, Mexico.", t)

t = c("I12H003", "wenglor", "I12H003 / wenglor / Robust design inductive sensor for harsh environments.")
check("descripción que ya trae «modelo / marca /»", t == "I12H003 / wenglor / Robust design inductive sensor for harsh environments.", t)

t = c("1763-L16BWA", "Rockwell Automation", "MicroLogix 1100 16 Point Controller with 10 inputs and 6 outputs.")
check("descripción normal no se toca", t == "1763-L16BWA / Rockwell Automation / MicroLogix 1100 16 Point Controller with 10 inputs and 6 outputs.", t)

t = c("ABC-1", "Acme", "Acme ABC-1 Sensor inductivo de 12 mm")
check("prefijo «marca modelo» pegado", t == "ABC-1 / Acme / Sensor inductivo de 12 mm", t)

check("descripción igual al modelo → solo modelo y marca", c("ABC-1", "Acme", "ABC-1") == "ABC-1 / Acme", c("ABC-1", "Acme", "ABC-1"))
check("sin descripción", c("ABC-1", "Acme", "") == "ABC-1 / Acme")
check("sin marca", c("ABC-1", "", "ABC-1, Sensor de 12 mm") == "ABC-1 / Sensor de 12 mm", c("ABC-1", "", "ABC-1, Sensor de 12 mm"))
check("un modelo dentro del texto (no al inicio) no se rompe", "compatible con ABC-12" in c("ABC-1", "Acme", "Sensor compatible con ABC-12"), c("ABC-1", "Acme", "Sensor compatible con ABC-12"))

if _fallos:
    print(f"\n❌ {len(_fallos)} fallo(s): {_fallos}"); raise SystemExit(1)
print("\n✅ Todo bien")
