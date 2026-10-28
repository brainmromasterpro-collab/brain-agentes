"""Validador de datos fiscales para facturar (CFDI 4.0) contra las reglas y catálogos del SAT.

Corre SIEMPRE que se arma o se crea una factura (alegra.borrador_desde_so y alegra.crear_factura).
Devuelve {"errores": [...], "avisos": [...]}: un ERROR impide timbrar (el PAC la rechazaría); un AVISO
se muestra pero no bloquea.

Fuentes:
  • catalogos_sat/cp_estado.json.gz — c_CodigoPostal del SAT (catCFDI.xls, rev. ene-2021): C.P. → estado.
  • Regímenes / uso de CFDI / forma y método de pago: reglas del Anexo 20 (CFDI 4.0). OJO: el catCFDI.xls
    público es anterior a RESICO (626) y no trae la tabla uso↔régimen; esas reglas están escritas aquí.
"""
import datetime
import gzip
import json
import os
import re
import unicodedata

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalogos_sat")
GENERICOS = {"XAXX010101000", "XEXX010101000"}
_ALF = "0123456789ABCDEFGHIJKLMN&OPQRSTUVWXYZ Ñ"

# c_RegimenFiscal: código → aplica a (física, moral)
REGIMEN_PERSONA = {
    "601": (False, True), "603": (False, True), "605": (True, False), "606": (True, False), "607": (False, True),
    "608": (True, False), "609": (False, True), "610": (True, True), "611": (True, False), "612": (True, False),
    "614": (True, False), "615": (True, False), "616": (True, False), "620": (False, True), "621": (True, False),
    "622": (True, True), "623": (False, True), "624": (False, True), "625": (True, False), "626": (True, True),
    "628": (False, True), "629": (True, True), "630": (True, False),
}
# c_UsoCFDI → regímenes del receptor permitidos (solo los usos que maneja el sistema)
USO_REGIMENES = {
    "G01": {"601", "603", "606", "612", "620", "621", "622", "623", "624", "625", "626"},
    "G03": {"601", "603", "606", "612", "620", "621", "622", "623", "624", "625", "626"},
}
# Estado (texto de la cuenta) → clave de estado del SAT
_ESTADOS = {
    "aguascalientes": "AGU", "baja california": "BCN", "baja california sur": "BCS", "campeche": "CAM",
    "chiapas": "CHP", "chihuahua": "CHH", "coahuila": "COA", "coahuila de zaragoza": "COA", "colima": "COL",
    "ciudad de mexico": "DIF", "cdmx": "DIF", "distrito federal": "DIF", "durango": "DUR", "guanajuato": "GUA",
    "guerrero": "GRO", "hidalgo": "HID", "jalisco": "JAL", "estado de mexico": "MEX", "mexico": "MEX",
    "michoacan": "MIC", "morelos": "MOR", "nayarit": "NAY", "nuevo leon": "NLE", "oaxaca": "OAX", "puebla": "PUE",
    "queretaro": "QUE", "quintana roo": "ROO", "san luis potosi": "SLP", "sinaloa": "SIN", "sonora": "SON",
    "tabasco": "TAB", "tamaulipas": "TAM", "tlaxcala": "TLA", "veracruz": "VER", "yucatan": "YUC", "zacatecas": "ZAC",
}
_cp_cache: dict | None = None


def _cp_catalogo() -> dict:
    global _cp_cache
    if _cp_cache is None:
        try:
            with gzip.open(os.path.join(_DIR, "cp_estado.json.gz"), "rt", encoding="utf-8") as f:
                _cp_cache = json.load(f)
        except Exception:
            _cp_cache = {}
    return _cp_cache


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower()) if unicodedata.category(c) != "Mn").strip()


def digito_rfc(rfc: str) -> str:
    base = (" " + rfc[:-1]) if len(rfc) == 12 else rfc[:-1]
    s = sum(_ALF.index(c) * (13 - i) for i, c in enumerate(base))
    d = 11 - s % 11
    return "0" if d == 11 else ("A" if d == 10 else str(d))


def validar_rfc(rfc: str) -> list[str]:
    rfc = (rfc or "").strip().upper()
    if not rfc:
        return ["Falta el RFC del cliente."]
    if rfc in GENERICOS:
        return []
    if not re.fullmatch(r"[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}", rfc):
        return [f"El RFC «{rfc}» no tiene un formato válido (12 caracteres persona moral, 13 persona física)."]
    out = []
    f = rfc[-9:-3]
    try:
        datetime.date(int(f[:2]) + (2000 if int(f[:2]) <= 26 else 1900), int(f[2:4]), int(f[4:6]))
    except ValueError:
        out.append(f"El RFC «{rfc}» trae una fecha inválida ({f}).")
    if rfc[-1] != digito_rfc(rfc):
        out.append(f"El RFC «{rfc}» no pasa el dígito verificador (debería terminar en {digito_rfc(rfc)}); revisa que esté bien capturado.")
    return out


def validar(rfc: str = "", regimen: str = "", cp: str = "", estado: str = "", uso: str = "",
            metodo: str = "", forma: str = "", pais: str = "Mexico", nombre: str = "") -> dict:
    """regimen = código SAT (601…); metodo = PUE/PPD; forma = código SAT de la forma de pago (99 = Por definir)
    o su equivalente de Alegra ("other")."""
    errores, avisos = [], []
    rfc = (rfc or "").strip().upper()
    errores += validar_rfc(rfc)
    nacional = rfc not in GENERICOS or rfc == "XAXX010101000"

    if regimen and len(rfc) in (12, 13) and rfc not in GENERICOS:
        reg = REGIMEN_PERSONA.get(regimen)
        if reg is None:
            errores.append(f"El régimen fiscal «{regimen}» no existe en el catálogo del SAT.")
        elif not reg[0 if len(rfc) == 13 else 1]:
            errores.append(f"El régimen {regimen} no aplica a una persona {'física' if len(rfc) == 13 else 'moral'} (RFC de {len(rfc)} caracteres).")
    if uso:
        if regimen and rfc not in GENERICOS and uso in USO_REGIMENES and regimen not in USO_REGIMENES[uso]:
            errores.append(f"El uso de CFDI {uso} no es válido para el régimen {regimen} del cliente.")
    else:
        errores.append("Falta el uso de CFDI del cliente.")

    es_99 = forma in ("99", "other", "1")
    if metodo == "PPD" and forma and not es_99:
        errores.append("Método PPD exige forma de pago 99 «Por definir».")
    if metodo == "PUE" and forma and es_99:
        errores.append("Método PUE no admite forma de pago 99 «Por definir».")
    if not metodo:
        errores.append("Falta el método de pago (PUE o PPD).")

    if pais in ("Mexico", "México", "") and nacional:
        cp = (cp or "").strip()
        if not re.fullmatch(r"\d{5}", cp):
            errores.append("Falta el código postal fiscal del cliente (5 dígitos).")
        else:
            cat = _cp_catalogo()
            if not cat:
                avisos.append("No se pudo cargar el catálogo de códigos postales del SAT; no se validó el C.P.")
            elif cp not in cat:
                avisos.append(f"El C.P. {cp} no aparece en el catálogo del SAT (rev. 2021); confirma que sea correcto.")
            else:
                esp = _ESTADOS.get(_norm(estado))
                if estado and esp and esp != cat[cp]:
                    errores.append(f"El C.P. {cp} pertenece al estado {cat[cp]} según el SAT, pero la cuenta dice «{estado}».")
    if re.search(r"\bS\.?\s?A\.?\s?(P\.?\s?I\.?\s?)?DE\s?C\.?\s?V\.?|\bS\.?\s?DE\s?R\.?\s?L\b|\bSAPI\b|\bSAS\b", (nombre or "").upper()):
        avisos.append(f"La razón social «{nombre}» incluye el régimen societario; CFDI 4.0 la pide idéntica a la Constancia de Situación Fiscal (sin «S.A. de C.V.»).")
    return {"errores": errores, "avisos": avisos}


def validar_draft(draft: dict) -> dict:
    """Valida un borrador de factura de Alegra (el dict que arma alegra.borrador_desde_so)."""
    cli = draft.get("cliente") or {}
    return validar(rfc=cli.get("rfc", ""), regimen=cli.get("regimeClient", ""), cp=cli.get("cp", ""),
                   estado=cli.get("estado", ""), uso=draft.get("cfdiUse", ""), metodo=draft.get("paymentType", ""),
                   forma=draft.get("paymentMethod", ""), nombre=cli.get("razon_social") or cli.get("nombre", ""))
