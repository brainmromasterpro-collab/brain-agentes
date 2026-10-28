"""
Pruebas del VALIDADOR FISCAL (validador_sat.py): reglas del SAT para facturar con CFDI 4.0.

Correr:  python3 test_validador_sat.py
"""
import validador_sat as v

_fallos: list[str] = []


def check(nombre: str, cond: bool, detalle: str = ""):
    print(f"  {'✅' if cond else '❌'} {nombre}" + (f" — {detalle}" if detalle and not cond else ""))
    if not cond:
        _fallos.append(nombre)


def ok(**kw):
    base = dict(rfc="CGU100127CZ4", regimen="601", cp="66266", estado="Nuevo León", uso="G01", metodo="PPD", forma="other")
    base.update(kw)
    return v.validar(**base)


print("RFC")
for rfc in ("MOU2308019E6", "SAT970701NN3", "CGU100127CZ4", "EAD110822RX9", "XAXX010101000"):
    check(f"RFC válido {rfc}", not v.validar_rfc(rfc))
check("dígito verificador malo (Weidmann)", bool(v.validar_rfc("WTE970520QT2")))
check("formato inválido", bool(v.validar_rfc("ABC123")))
check("fecha inválida", bool(v.validar_rfc("ABC991399AB1")))
check("RFC vacío", bool(v.validar_rfc("")))

print("Régimen / uso / pago")
check("caso correcto sin errores", ok()["errores"] == [])
check("régimen moral con RFC de física", bool(ok(rfc="PIHJ901001II9", regimen="601")["errores"]))
check("régimen de física con RFC moral", bool(ok(regimen="612")["errores"]))
check("626 aplica a moral y física", not ok(regimen="626")["errores"] and not ok(rfc="PIHJ901001II9", regimen="626")["errores"])
check("uso G01 inválido con 616", bool(ok(rfc="PIHJ901001II9", regimen="616")["errores"]))
check("sin uso de CFDI", bool(ok(uso="")["errores"]))
check("PPD con transferencia", bool(ok(forma="transfer")["errores"]))
check("PUE con 'por definir'", bool(ok(metodo="PUE", forma="other")["errores"]))
check("PUE con transferencia es válido", not ok(metodo="PUE", forma="transfer")["errores"])
check("sin método de pago", bool(ok(metodo="")["errores"]))

print("Código postal")
check("C.P. y estado coinciden", not ok()["errores"])
check("C.P. de otro estado", bool(ok(estado="Coahuila")["errores"]))
check("sin C.P.", bool(ok(cp="")["errores"]))
check("C.P. con 4 dígitos", bool(ok(cp="6626")["errores"]))
check("CDMX = DIF", not ok(rfc="CGU100127CZ4", cp="06600", estado="Ciudad de México")["errores"])
check("RFC genérico extranjero no exige C.P.", not v.validar(rfc="XEXX010101000", uso="G03", metodo="PUE", forma="transfer", pais="Estados Unidos")["errores"])

print("Nombre")
check("razón social con S.A. de C.V. avisa", bool(ok(nombre="ACME S.A. DE C.V.")["avisos"]))
check("razón social limpia no avisa", not ok(nombre="INDUSTRIAS GSL")["avisos"])

print("Borrador completo")
draft = {"cliente": {"rfc": "WTE970520QT2", "regimeClient": "601", "cp": "25294", "estado": "Coahuila", "nombre": "Weidmann"},
         "cfdiUse": "G03", "paymentType": "PPD", "paymentMethod": "other"}
check("borrador con RFC malo trae error", bool(v.validar_draft(draft)["errores"]))

if _fallos:
    print(f"\n❌ {len(_fallos)} fallo(s): {_fallos}")
    raise SystemExit(1)
print("\n✅ Todo bien")
