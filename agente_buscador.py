"""
AGENTE BUSCADOR — Brain · MRO Master Pro
=========================================
Worker que corre en Railway. Escucha jobs pendientes en Supabase,
hace búsqueda en 1CRM (productos + proveedores) y Google,
rankea Top 5 y escribe resultados de vuelta en Supabase.

Dependencias:
  pip install anthropic supabase httpx python-dotenv

Variables de entorno (.env):
  ANTHROPIC_API_KEY=
  SUPABASE_URL=
  SUPABASE_SERVICE_KEY=
  ONECRM_URL=https://mromasterpro.1crmcloud.com
  ONECRM_CLIENT_ID=
  ONECRM_CLIENT_SECRET=
  ONECRM_USERNAME=
  ONECRM_PASSWORD=
  GOOGLE_API_KEY=
  GOOGLE_CX=
  FX_API_KEY=        # opcional — fixer.io o similar
"""

import os
import time
import json
import logging
from datetime import datetime, date
from typing import Optional
from dotenv import load_dotenv

import httpx
import anthropic
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agente_buscador")

# ─────────────────────────────────────────
# CLIENTES
# ─────────────────────────────────────────
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"]
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ONECRM_BASE = os.environ["ONECRM_URL"].rstrip("/")
POLL_INTERVAL = 10  # segundos entre polls


# ─────────────────────────────────────────
# 1CRM — AUTENTICACIÓN OAuth 2.0
# ─────────────────────────────────────────
_onecrm_token: Optional[str] = None
_onecrm_token_expiry: float = 0

def get_onecrm_token() -> str:
    global _onecrm_token, _onecrm_token_expiry
    if _onecrm_token and time.time() < _onecrm_token_expiry:
        return _onecrm_token

    log.info("Obteniendo token 1CRM...")
   resp = httpx.post(
        f"{ONECRM_BASE}/api.php/auth/user/access_token",
        json={
            "grant_type": "password",
            "client_id": os.environ["ONECRM_CLIENT_ID"],
            "client_secret": os.environ["ONECRM_CLIENT_SECRET"],
            "username": os.environ["ONECRM_USERNAME"],
            "password": os.environ["ONECRM_PASSWORD"],
        },
        timeout=15,
    ),
    )
    resp.raise_for_status()
    data = resp.json()
    _onecrm_token = data["access_token"]
    _onecrm_token_expiry = time.time() + data.get("expires_in", 3600) - 60
    log.info("Token 1CRM obtenido OK")
    return _onecrm_token


def onecrm_get(endpoint: str, params: dict = {}) -> dict:
    token = get_onecrm_token()
    resp = httpx.get(
        f"{ONECRM_BASE}/api.php/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────
# TIPO DE CAMBIO USD/MXN
# ─────────────────────────────────────────
def get_fx_usd_mxn() -> float:
    """Obtiene tipo de cambio USD/MXN. Usa fixer.io si hay API key, si no usa Banxico."""
    try:
        fx_key = os.environ.get("FX_API_KEY")
        if fx_key:
            resp = httpx.get(
                f"http://data.fixer.io/api/latest",
                params={"access_key": fx_key, "base": "USD", "symbols": "MXN"},
                timeout=10,
            )
            data = resp.json()
            return data["rates"]["MXN"]
        else:
            # Fallback: tipo de cambio aproximado hardcoded si no hay API
            # TODO: conectar Banxico cuando se tenga acceso
            log.warning("Sin FX_API_KEY, usando tipo de cambio aproximado 17.50")
            return 17.50
    except Exception as e:
        log.warning(f"Error obteniendo FX: {e}, usando 17.50")
        return 17.50


# ─────────────────────────────────────────
# BÚSQUEDA EN 1CRM — PRODUCTOS
# ─────────────────────────────────────────
def buscar_en_crm_productos(marca: str, modelo: str) -> list[dict]:
    log.info(f"Buscando en 1CRM productos: {marca} {modelo}")
    try:
        data = onecrm_get("data/Product", {
            "search_fields": json.dumps({
                "name": modelo,
                "mfr_part_no": modelo,
            }),
            "fields": "id,name,mfr_part_no,price,currency_id,description,category_id",
            "max_num": 10,
        })
        records = data.get("records", [])
        resultados = []
        for r in records:
            # Filtrar por marca si viene en el nombre o descripción
            nombre = (r.get("name") or "").lower()
            desc = (r.get("description") or "").lower()
            if marca.lower() in nombre or marca.lower() in desc or modelo.lower() in nombre:
                resultados.append({
                    "proveedor": "1CRM Catálogo",
                    "nombre_producto": r.get("name"),
                    "precio_orig": float(r.get("price") or 0),
                    "moneda": "USD",
                    "disponibilidad": "en_stock",
                    "tiempo_entrega": "Inmediato",
                    "condicion": "nuevo",
                    "fuente": "1crm_productos",
                    "url": f"{ONECRM_BASE}/index.php?module=Products&record={r.get('id')}",
                    "dist_autorizado": True,
                    "notas": r.get("description", ""),
                })
        log.info(f"1CRM productos: {len(resultados)} resultados")
        return resultados
    except Exception as e:
        log.error(f"Error búsqueda 1CRM productos: {e}")
        return []


# ─────────────────────────────────────────
# BÚSQUEDA EN 1CRM — PROVEEDORES
# ─────────────────────────────────────────
def buscar_en_crm_proveedores(marca: str, modelo: str) -> list[dict]:
    log.info(f"Buscando en 1CRM proveedores para: {marca}")
    try:
        data = onecrm_get("data/Account", {
            "search_fields": json.dumps({
                "account_type": "Supplier",
                "name": marca,
            }),
            "fields": "id,name,account_type,website,phone_office",
            "max_num": 10,
        })
        records = data.get("records", [])
        resultados = []
        for r in records:
            resultados.append({
                "proveedor": r.get("name"),
                "nombre_producto": f"{marca} {modelo}",
                "precio_orig": None,  # proveedores no tienen precio directo
                "moneda": "USD",
                "disponibilidad": "bajo_pedido",
                "tiempo_entrega": "Consultar",
                "condicion": "nuevo",
                "fuente": "1crm_proveedores",
                "url": r.get("website") or f"{ONECRM_BASE}/index.php?module=Accounts&record={r.get('id')}",
                "dist_autorizado": False,
                "notas": f"Tel: {r.get('phone_office', '')}",
            })
        log.info(f"1CRM proveedores: {len(resultados)} resultados")
        return resultados
    except Exception as e:
        log.error(f"Error búsqueda 1CRM proveedores: {e}")
        return []


# ─────────────────────────────────────────
# BÚSQUEDA EN GOOGLE
# ─────────────────────────────────────────
def buscar_en_google(marca: str, modelo: str) -> list[dict]:
    log.info(f"Buscando en Google: {marca} {modelo}")
    try:
        api_key = os.environ.get("GOOGLE_API_KEY")
        cx = os.environ.get("GOOGLE_CX")
        if not api_key or not cx:
            log.warning("Sin GOOGLE_API_KEY o GOOGLE_CX, saltando búsqueda Google")
            return []

        query = f"{marca} {modelo} precio distribuidor México"
        resp = httpx.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": api_key, "cx": cx, "q": query, "num": 5},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])

        resultados = []
        for item in items:
            resultados.append({
                "proveedor": item.get("displayLink", ""),
                "nombre_producto": item.get("title", f"{marca} {modelo}"),
                "precio_orig": None,  # Claude infiere después
                "moneda": "USD",
                "disponibilidad": "consultar",
                "tiempo_entrega": "Ver sitio",
                "condicion": "nuevo",
                "fuente": "google",
                "url": item.get("link", ""),
                "dist_autorizado": False,
                "notas": item.get("snippet", ""),
            })
        log.info(f"Google: {len(resultados)} resultados")
        return resultados
    except Exception as e:
        log.error(f"Error búsqueda Google: {e}")
        return []


# ─────────────────────────────────────────
# CLAUDE — ANALIZA Y RANKEA TOP 5
# ─────────────────────────────────────────
def rankear_con_claude(
    marca: str,
    modelo: str,
    urgente: bool,
    resultados_raw: list[dict],
    fx: float,
) -> list[dict]:
    log.info(f"Claude rankeando {len(resultados_raw)} resultados (urgente={urgente})")

    ponderacion = "30% precio / 70% disponibilidad" if urgente else "60% precio / 40% disponibilidad"

    disponibilidad_puntos = {
        "en_stock": 100,
        "inmediato": 100,
        "dias_1_5": 75,
        "bajo_pedido": 25,
        "importacion": 10,
        "consultar": 30,
        "ver_sitio": 20,
    }

    prompt = f"""Eres un agente especializado en búsqueda de productos industriales para MRO Master Pro.

Tienes estos resultados de búsqueda para: **{marca} {modelo}**
Modo: {"URGENTE" if urgente else "Normal"}
Ponderación: {ponderacion}
Tipo de cambio USD/MXN: {fx}

RESULTADOS ENCONTRADOS:
{json.dumps(resultados_raw, ensure_ascii=False, indent=2)}

Tu tarea:
1. Selecciona los mejores 5 resultados (puede ser menos si no hay suficientes)
2. Para cada uno infiere o estima el precio si no está explícito (basado en el snippet/notas)
3. Verifica si es distribuidor autorizado de {marca} (busca indicios en el nombre o URL)
4. Calcula el score de ranking con esta fórmula:
   - Normaliza precios: el más barato = 100 puntos, los demás proporcional
   - Disponibilidad en puntos: en_stock=100, 1-5días=75, 1-2semanas=50, bajo_pedido=25, importación=10
   - Score final = (precio_pts * {0.3 if urgente else 0.6}) + (disponibilidad_pts * {0.7 if urgente else 0.4})
5. Asigna score de confianza (1-5):
   - 5: distribuidor oficial verificado
   - 4: proveedor en 1CRM con historial
   - 3: resultado Google con precio y datos claros
   - 2: datos incompletos o precio estimado
   - 1: fuente no verificada

Responde SOLO con un JSON array con máximo 5 objetos, ordenados de mayor a menor score_ranking:
[
  {{
    "rank": 1,
    "proveedor": "nombre",
    "dist_autorizado": true/false,
    "precio_orig": 0.00,
    "moneda": "USD",
    "precio_mxn": 0.00,
    "disponibilidad": "en_stock|dias_N|bajo_pedido|importacion",
    "tiempo_entrega": "texto",
    "condicion": "nuevo|reacondicionado|usado",
    "fuente": "1crm_productos|1crm_proveedores|google",
    "url": "https://...",
    "score_confianza": 1-5,
    "score_ranking": 0.00,
    "notas": "observaciones"
  }}
]

No incluyas ningún texto fuera del JSON."""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Limpiar posibles backticks
    text = text.replace("```json", "").replace("```", "").strip()

    try:
        ranked = json.loads(text)
        log.info(f"Claude rankeo: {len(ranked)} opciones en Top 5")
        return ranked
    except json.JSONDecodeError as e:
        log.error(f"Error parseando JSON de Claude: {e}\nRespuesta: {text}")
        return []


# ─────────────────────────────────────────
# GUARDAR RESULTADOS EN SUPABASE
# ─────────────────────────────────────────
def guardar_opciones(rfq_uuid: str, opciones: list[dict], fx: float) -> None:
    log.info(f"Guardando {len(opciones)} opciones en Supabase")

    # Borrar opciones previas por si es reintento
    supabase.table("opciones").delete().eq("rfq_id", rfq_uuid).execute()

    for op in opciones:
        precio_orig = op.get("precio_orig") or 0
        moneda = op.get("moneda", "USD")
        precio_mxn = (precio_orig * fx) if moneda == "USD" else precio_orig

        supabase.table("opciones").insert({
            "rfq_id": rfq_uuid,
            "rank": op.get("rank"),
            "proveedor": op.get("proveedor"),
            "dist_autorizado": op.get("dist_autorizado", False),
            "precio_orig": precio_orig,
            "moneda": moneda,
            "precio_mxn": round(precio_mxn, 2),
            "disponibilidad": op.get("disponibilidad"),
            "tiempo_entrega": op.get("tiempo_entrega"),
            "condicion": op.get("condicion", "nuevo"),
            "fuente": op.get("fuente"),
            "url": op.get("url"),
            "score_confianza": op.get("score_confianza"),
            "score_ranking": op.get("score_ranking"),
            "notas": op.get("notas"),
        }).execute()

    log.info("Opciones guardadas OK")


def agregar_log_job(job_id: str, paso: str, msg: str) -> None:
    try:
        job = supabase.table("jobs").select("log").eq("id", job_id).single().execute()
        logs = job.data.get("log") or []
        logs.append({
            "paso": paso,
            "timestamp": datetime.utcnow().isoformat(),
            "msg": msg,
        })
        supabase.table("jobs").update({"log": logs}).eq("id", job_id).execute()
    except Exception as e:
        log.warning(f"Error actualizando log del job: {e}")


# ─────────────────────────────────────────
# PROCESADOR PRINCIPAL DEL JOB
# ─────────────────────────────────────────
def procesar_job(job: dict) -> None:
    job_id = job["id"]
    rfq_uuid = job["rfq_id"]

    log.info(f"Procesando job {job_id} para rfq {rfq_uuid}")

    # Marcar job como corriendo
    supabase.table("jobs").update({
        "estado": "corriendo",
        "started_at": datetime.utcnow().isoformat(),
    }).eq("id", job_id).execute()

    # Marcar RFQ como buscando
    supabase.table("rfqs").update({"estado": "buscando"}).eq("id", rfq_uuid).execute()

    try:
        # Obtener datos del RFQ
        rfq_resp = supabase.table("rfqs").select("*").eq("id", rfq_uuid).single().execute()
        rfq = rfq_resp.data
        marca = rfq["marca"]
        modelo = rfq["modelo"]
        urgente = rfq.get("urgente", False)

        agregar_log_job(job_id, "inicio", f"Buscando: {marca} {modelo} | urgente={urgente}")

        # Obtener tipo de cambio
        fx = get_fx_usd_mxn()
        supabase.table("rfqs").update({
            "fx_usd_mxn": fx,
            "fx_fecha": date.today().isoformat(),
        }).eq("id", rfq_uuid).execute()

        agregar_log_job(job_id, "fx", f"Tipo de cambio USD/MXN: {fx}")

        # 3 búsquedas
        resultados = []

        agregar_log_job(job_id, "busqueda_1crm_productos", "Iniciando búsqueda en 1CRM productos")
        res_productos = buscar_en_crm_productos(marca, modelo)
        resultados.extend(res_productos)
        agregar_log_job(job_id, "busqueda_1crm_productos", f"{len(res_productos)} resultados")

        agregar_log_job(job_id, "busqueda_1crm_proveedores", "Iniciando búsqueda en 1CRM proveedores")
        res_proveedores = buscar_en_crm_proveedores(marca, modelo)
        resultados.extend(res_proveedores)
        agregar_log_job(job_id, "busqueda_1crm_proveedores", f"{len(res_proveedores)} resultados")

        agregar_log_job(job_id, "busqueda_google", "Iniciando búsqueda en Google")
        res_google = buscar_en_google(marca, modelo)
        resultados.extend(res_google)
        agregar_log_job(job_id, "busqueda_google", f"{len(res_google)} resultados")

        if not resultados:
            raise Exception("No se encontraron resultados en ninguna fuente")

        # Claude rankea
        agregar_log_job(job_id, "ranking", f"Claude rankeando {len(resultados)} resultados")
        top5 = rankear_con_claude(marca, modelo, urgente, resultados, fx)

        if not top5:
            raise Exception("Claude no pudo generar el ranking")

        # Guardar en Supabase
        guardar_opciones(rfq_uuid, top5, fx)

        # Actualizar RFQ a busqueda_completa
        supabase.table("rfqs").update({
            "estado": "busqueda_completa",
        }).eq("id", rfq_uuid).execute()

        # Crear job para notificación al gerente
        supabase.table("jobs").insert({
            "rfq_id": rfq_uuid,
            "agente": "notificador",
            "estado": "pendiente",
        }).execute()

        # Cerrar job exitosamente
        supabase.table("jobs").update({
            "estado": "completado",
            "finished_at": datetime.utcnow().isoformat(),
            "output": {"opciones_encontradas": len(top5)},
        }).eq("id", job_id).execute()

        log.info(f"Job {job_id} completado — Top {len(top5)} generado")

    except Exception as e:
        log.error(f"Job {job_id} falló: {e}")
        agregar_log_job(job_id, "error", str(e))

        # Reintentar hasta 3 veces
        intento = job.get("intento", 1)
        if intento < 3:
            supabase.table("jobs").update({
                "estado": "pendiente",
                "intento": intento + 1,
            }).eq("id", job_id).execute()
            supabase.table("rfqs").update({"estado": "recibido"}).eq("id", rfq_uuid).execute()
            log.info(f"Job reintentará (intento {intento + 1}/3)")
        else:
            supabase.table("jobs").update({
                "estado": "fallido",
                "finished_at": datetime.utcnow().isoformat(),
                "error": str(e),
            }).eq("id", job_id).execute()
            supabase.table("rfqs").update({"estado": "recibido"}).eq("id", rfq_uuid).execute()
            log.error(f"Job {job_id} falló definitivamente después de 3 intentos")


# ─────────────────────────────────────────
# LOOP PRINCIPAL — POLLING
# ─────────────────────────────────────────
def main():
    log.info("Agente Buscador iniciado — escuchando jobs...")
    log.info(f"1CRM: {ONECRM_BASE}")
    log.info(f"Supabase: {os.environ['SUPABASE_URL']}")
    log.info(f"Poll interval: {POLL_INTERVAL}s")

    while True:
        try:
            # Buscar jobs pendientes para el agente buscador
            resp = supabase.table("jobs")\
                .select("*")\
                .eq("agente", "buscador")\
                .eq("estado", "pendiente")\
                .order("created_at")\
                .limit(1)\
                .execute()

            jobs = resp.data
            if jobs:
                procesar_job(jobs[0])
            else:
                log.debug("Sin jobs pendientes, esperando...")

        except Exception as e:
            log.error(f"Error en loop principal: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
