"""
AGENTE MONITOR — Brain · MRO Master Pro
========================================
Corre en Railway (dentro de main.py). Cada hora llama a las APIs
de cada servicio externo y guarda el estado en la tabla `resource_status`
de Supabase. El dashboard de Bolt lee de ahí para mostrar consumo en tiempo real.

Servicios monitoreados:
  - SerpAPI       → búsquedas restantes del mes
  - Remove.bg     → créditos restantes
  - Anthropic     → tokens usados hoy (rastreados en jobs de Supabase)
  - Google CSE    → llamadas hechas hoy (contador interno en Supabase)
  - Supabase      → storage usado, filas en tablas clave
  - Railway       → estado de servicios (requiere RAILWAY_TOKEN, opcional)
  - GitHub        → rate limit restante de la API

Tabla Supabase requerida (correr una vez):
  CREATE TABLE IF NOT EXISTS resource_status (
      id            uuid DEFAULT gen_random_uuid() PRIMARY KEY,
      servicio      text NOT NULL,
      metrica       text NOT NULL,
      valor         numeric,
      valor_texto   text,
      unidad        text,
      limite        numeric,
      estado        text DEFAULT 'ok',   -- 'ok' | 'warning' | 'critical'
      actualizado_en timestamptz DEFAULT now(),
      UNIQUE(servicio, metrica)
  );

Variables de entorno (todas opcionales — el agente ignora lo que no tenga):
  SERPAPI_KEY
  REMOVEBG_API_KEY
  GOOGLE_API_KEY / GOOGLE_CX
  RAILWAY_TOKEN       # Settings → Tokens en railway.app
  GITHUB_TOKEN        # PAT con permisos read:org (el mismo del repo)
  SUPABASE_URL / SUPABASE_SERVICE_KEY  (ya configuradas)
"""

import os
import time
import logging
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agente_monitor")

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"],
)

MONITOR_INTERVAL = 3600   # 1 hora entre actualizaciones
WARN_THRESHOLD   = 0.20   # warning cuando queda < 20% del límite
CRIT_THRESHOLD   = 0.05   # critical cuando queda < 5%


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def _estado(usado: float | None, limite: float | None) -> str:
    """Calcula estado: ok / warning / critical basándose en % restante."""
    if usado is None or limite is None or limite == 0:
        return "ok"
    restante = (limite - usado) / limite
    if restante <= CRIT_THRESHOLD:
        return "critical"
    if restante <= WARN_THRESHOLD:
        return "warning"
    return "ok"


def _estado_restante(restante: float | None, limite: float | None) -> str:
    """Igual pero recibe directamente el restante en lugar del usado."""
    if restante is None or limite is None or limite == 0:
        return "ok"
    ratio = restante / limite
    if ratio <= CRIT_THRESHOLD:
        return "critical"
    if ratio <= WARN_THRESHOLD:
        return "warning"
    return "ok"


def upsert(servicio: str, metrica: str, valor: float | None = None,
           valor_texto: str | None = None, unidad: str | None = None,
           limite: float | None = None, estado: str = "ok") -> None:
    """Guarda o actualiza una métrica en resource_status."""
    try:
        supabase.table("resource_status").upsert({
            "servicio":       servicio,
            "metrica":        metrica,
            "valor":          valor,
            "valor_texto":    valor_texto,
            "unidad":         unidad,
            "limite":         limite,
            "estado":         estado,
            "actualizado_en": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="servicio,metrica").execute()
    except Exception as e:
        log.error(f"upsert {servicio}/{metrica}: {e}")


# ─────────────────────────────────────────────────────────────
# 1. SERPAPI
# ─────────────────────────────────────────────────────────────
def check_serpapi() -> None:
    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key:
        log.debug("SerpAPI: sin key, omitiendo")
        return
    try:
        resp = httpx.get(
            "https://serpapi.com/account.json",
            params={"api_key": key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        plan        = data.get("plan_name", "unknown")
        restante    = float(data.get("plan_searches_left", 0))
        limite_mes  = float(data.get("plan_searches_per_month", 100))
        usado_mes   = float(data.get("this_month_usage", 0))

        estado = _estado_restante(restante, limite_mes)

        upsert("serpapi", "busquedas_restantes",
               valor=restante, unidad="búsquedas",
               limite=limite_mes, estado=estado)
        upsert("serpapi", "busquedas_usadas_mes",
               valor=usado_mes, unidad="búsquedas", limite=limite_mes)
        upsert("serpapi", "plan",
               valor_texto=plan, estado=estado)

        log.info(f"SerpAPI: {restante:.0f}/{limite_mes:.0f} restantes | plan={plan} | {estado}")
    except Exception as e:
        log.error(f"SerpAPI check falló: {e}")
        upsert("serpapi", "busquedas_restantes", estado="critical",
               valor_texto=f"Error: {str(e)[:80]}")


# ─────────────────────────────────────────────────────────────
# 2. REMOVE.BG
# ─────────────────────────────────────────────────────────────
def check_removebg() -> None:
    key = os.environ.get("REMOVEBG_API_KEY", "").strip()
    if not key:
        log.debug("Remove.bg: sin key, omitiendo")
        return
    try:
        resp = httpx.get(
            "https://api.remove.bg/v1.0/account",
            headers={"X-Api-Key": key},
            timeout=10,
        )
        resp.raise_for_status()
        # La API devuelve los créditos bajo data.attributes.credits (no data.credits)
        attrs   = resp.json().get("data", {}).get("attributes", {})
        credits = attrs.get("credits", {})

        total_creditos    = float(credits.get("total", 0))
        sub_creditos      = float(credits.get("subscription", 0))
        payg_creditos     = float(credits.get("payg", 0))
        enterprise        = float(credits.get("enterprise", 0))

        # Cuenta de pago (cuota anual): no hay límite mensual fijo.
        # Marcamos critical solo si quedan muy pocos créditos en términos absolutos.
        if total_creditos <= 10:
            estado = "critical"
        elif total_creditos <= 50:
            estado = "warning"
        else:
            estado = "ok"

        upsert("removebg", "creditos_restantes",
               valor=total_creditos, unidad="créditos", estado=estado)
        upsert("removebg", "creditos_suscripcion",
               valor=sub_creditos, unidad="créditos")
        upsert("removebg", "creditos_payg",
               valor=payg_creditos, unidad="créditos")
        upsert("removebg", "creditos_enterprise",
               valor=enterprise, unidad="créditos")

        log.info(f"Remove.bg: {total_creditos:.0f} créditos totales "
                 f"(sub={sub_creditos:.0f}, payg={payg_creditos:.0f}, ent={enterprise:.0f}) | {estado}")
    except Exception as e:
        log.error(f"Remove.bg check falló: {e}")
        upsert("removebg", "creditos_restantes", estado="critical",
               valor_texto=f"Error: {str(e)[:80]}")


# ─────────────────────────────────────────────────────────────
# 3. ANTHROPIC — tokens acumulados desde jobs de Supabase
# ─────────────────────────────────────────────────────────────
def check_anthropic() -> None:
    """
    Suma los tokens registrados en jobs.output->>'tokens_total' para hoy.
    Si los agentes no guardan este campo aún, devuelve 0 (se puede mejorar).
    """
    try:
        hoy = datetime.now(timezone.utc).date().isoformat()
        resp = supabase.table("jobs") \
            .select("output") \
            .gte("created_at", hoy) \
            .eq("estado", "completado") \
            .execute()

        tokens_input  = 0
        tokens_output = 0
        jobs_hoy      = 0

        for job in (resp.data or []):
            out = job.get("output") or {}
            jobs_hoy      += 1
            tokens_input  += int(out.get("tokens_input",  out.get("tokens_total", 0)))
            tokens_output += int(out.get("tokens_output", 0))

        tokens_total = tokens_input + tokens_output

        # Anthropic no expone límites vía API — usamos referencia informativa
        upsert("anthropic", "tokens_hoy",
               valor=tokens_total, unidad="tokens",
               estado="ok")
        upsert("anthropic", "tokens_input_hoy",
               valor=tokens_input, unidad="tokens")
        upsert("anthropic", "tokens_output_hoy",
               valor=tokens_output, unidad="tokens")
        upsert("anthropic", "jobs_completados_hoy",
               valor=jobs_hoy, unidad="jobs")

        log.info(f"Anthropic: {tokens_total:,} tokens hoy "
                 f"(in={tokens_input:,}, out={tokens_output:,}) | {jobs_hoy} jobs")
    except Exception as e:
        log.error(f"Anthropic check falló: {e}")


# ─────────────────────────────────────────────────────────────
# 4. GOOGLE CUSTOM SEARCH — contador interno en Supabase
# ─────────────────────────────────────────────────────────────
def check_google_cse() -> None:
    """
    Google no expone cuota vía API. Contamos las llamadas hechas hoy
    usando el campo registrado en los jobs de imagen.
    Límite gratis: 100 queries/día.
    """
    try:
        configured = bool(
            os.environ.get("GOOGLE_API_KEY", "").strip() and
            os.environ.get("GOOGLE_CX", "").strip()
        )
        if not configured:
            upsert("google_cse", "estado_config",
                   valor_texto="No configurado", estado="warning")
            return

        hoy = datetime.now(timezone.utc).date().isoformat()
        resp = supabase.table("jobs") \
            .select("output") \
            .gte("created_at", hoy) \
            .eq("agente", "imagen") \
            .in_("estado", ["completado", "fallido", "foto_pendiente"]) \
            .execute()

        llamadas_hoy = 0
        for job in (resp.data or []):
            out = job.get("output") or {}
            llamadas_hoy += int(out.get("google_cse_calls", 0))

        # Estimado: si no hay registro exacto, contamos jobs de imagen
        if llamadas_hoy == 0:
            llamadas_hoy = len(resp.data or []) * 3   # ~3 queries por job en promedio

        limite_dia = 100
        estado = _estado(llamadas_hoy, limite_dia)

        upsert("google_cse", "llamadas_hoy",
               valor=llamadas_hoy, unidad="queries",
               limite=limite_dia, estado=estado)
        upsert("google_cse", "limite_diario",
               valor=limite_dia, unidad="queries")

        log.info(f"Google CSE: ~{llamadas_hoy}/{limite_dia} queries hoy | {estado}")
    except Exception as e:
        log.error(f"Google CSE check falló: {e}")


# ─────────────────────────────────────────────────────────────
# 5. SUPABASE — storage y conteo de filas
# ─────────────────────────────────────────────────────────────
def check_supabase() -> None:
    try:
        # Conteo de tablas clave
        tablas = ["rfqs", "jobs", "opciones", "notificaciones"]
        for tabla in tablas:
            try:
                resp = supabase.table(tabla).select("id", count="exact").execute()
                total = resp.count or len(resp.data or [])
                upsert("supabase", f"filas_{tabla}",
                       valor=total, unidad="filas")
            except Exception:
                pass

        # Storage: listar buckets y sumar tamaño (recursivo en subcarpetas)
        try:
            buckets_resp = supabase.storage.list_buckets()
            total_bytes  = 0

            def _sumar_carpeta(bucket_id: str, prefijo: str = "", profundidad: int = 0) -> int:
                """Suma bytes de una carpeta y desciende en subcarpetas (máx 6 niveles)."""
                if profundidad > 6:
                    return 0
                acumulado = 0
                try:
                    items = supabase.storage.from_(bucket_id).list(
                        prefijo, {"limit": 1000}
                    )
                except Exception:
                    return 0
                for it in (items or []):
                    nombre = it.get("name", "")
                    meta   = it.get("metadata") or {}
                    if meta.get("size") is not None:
                        # Es un archivo
                        acumulado += int(meta.get("size", 0))
                    else:
                        # Es una subcarpeta — descender
                        sub = f"{prefijo}/{nombre}" if prefijo else nombre
                        acumulado += _sumar_carpeta(bucket_id, sub, profundidad + 1)
                return acumulado

            for bucket in (buckets_resp or []):
                bucket_id = bucket.id if hasattr(bucket, "id") else bucket.get("id", "")
                if not bucket_id:
                    continue
                total_bytes += _sumar_carpeta(bucket_id)

            storage_gb = round(total_bytes / (1024 ** 3), 3)
            # Plan free de Supabase: 1 GB storage
            upsert("supabase", "storage_gb",
                   valor=storage_gb, unidad="GB",
                   limite=1.0,
                   estado=_estado(storage_gb, 1.0))
            log.info(f"Supabase storage: {storage_gb:.3f} GB")
        except Exception as e:
            log.warning(f"Supabase storage check: {e}")

        # Jobs por estado (resumen de actividad)
        for estado_job in ["pendiente", "corriendo", "fallido"]:
            try:
                resp = supabase.table("jobs") \
                    .select("id", count="exact") \
                    .eq("estado", estado_job) \
                    .execute()
                upsert("supabase", f"jobs_{estado_job}",
                       valor=resp.count or 0, unidad="jobs")
            except Exception:
                pass

        log.info("Supabase: métricas actualizadas")
    except Exception as e:
        log.error(f"Supabase check falló: {e}")


# ─────────────────────────────────────────────────────────────
# 6. RAILWAY — estado de servicios via GraphQL
# ─────────────────────────────────────────────────────────────
def check_railway() -> None:
    token = os.environ.get("RAILWAY_TOKEN", "").strip()
    if not token:
        log.debug("Railway: sin RAILWAY_TOKEN, omitiendo")
        return
    try:
        query = """
        query {
          me {
            usage {
              estimatedUsage
              currentUsage
            }
            projects {
              edges {
                node {
                  name
                  services {
                    edges {
                      node {
                        name
                        deployments(last: 1) {
                          edges {
                            node {
                              status
                              createdAt
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        resp = httpx.post(
            "https://backboard.railway.app/graphql/v2",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            json={"query": query},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("me", {})

        # Uso de créditos
        uso = data.get("usage", {})
        estimado = uso.get("estimatedUsage", 0)
        actual   = uso.get("currentUsage",   0)
        upsert("railway", "uso_estimado_usd",
               valor=round(float(estimado or 0), 4), unidad="USD")
        upsert("railway", "uso_actual_usd",
               valor=round(float(actual   or 0), 4), unidad="USD")

        # Estado de deployments
        servicios_ok      = 0
        servicios_error   = 0
        servicios_nombres = []
        for project_edge in data.get("projects", {}).get("edges", []):
            project = project_edge.get("node", {})
            for svc_edge in project.get("services", {}).get("edges", []):
                svc = svc_edge.get("node", {})
                svc_nombre = svc.get("name", "")
                deploys = svc.get("deployments", {}).get("edges", [])
                if deploys:
                    status = deploys[0].get("node", {}).get("status", "UNKNOWN")
                    if status in ("SUCCESS", "ACTIVE"):
                        servicios_ok += 1
                    else:
                        servicios_error += 1
                    servicios_nombres.append(f"{svc_nombre}:{status}")

        estado_gral = "critical" if servicios_error > 0 else "ok"
        upsert("railway", "servicios_activos",
               valor=servicios_ok, unidad="servicios", estado=estado_gral)
        upsert("railway", "servicios_error",
               valor=servicios_error, unidad="servicios")
        upsert("railway", "detalle_servicios",
               valor_texto=", ".join(servicios_nombres), estado=estado_gral)

        log.info(f"Railway: {servicios_ok} OK / {servicios_error} error | "
                 f"uso={actual} USD | {estado_gral}")
    except Exception as e:
        # No marcamos falla crítica: el heartbeat ya cubre "¿está vivo el backend?".
        # El API de costos de Railway cambia de esquema; si falla, solo lo registramos.
        log.warning(f"Railway cost check falló (no crítico): {e}")


# ─────────────────────────────────────────────────────────────
# 7. 1CRM — productos y proveedores
# ─────────────────────────────────────────────────────────────
def check_1crm() -> None:
    base = os.environ.get("ONECRM_URL", "").rstrip("/")
    user = os.environ.get("ONECRM_USERNAME", "").strip()
    pwd  = os.environ.get("ONECRM_PASSWORD",  "").strip()
    if not base or not user or not pwd:
        log.debug("1CRM: credenciales no configuradas, omitiendo")
        return
    try:
        # Total de productos en catálogo
        resp_prod = httpx.get(
            f"{base}/api.php/data/Product",
            auth=(user, pwd),
            params={"limit": 1},
            timeout=15,
        )
        resp_prod.raise_for_status()
        # La API de 1CRM devuelve "total_results" (no "total_count")
        total_productos = int(resp_prod.json().get("total_results", 0))

        # Total de proveedores (cuentas tipo Supplier)
        resp_prov = httpx.get(
            f"{base}/api.php/data/Account",
            auth=(user, pwd),
            params={"filters[account_type]": "Supplier", "limit": 1},
            timeout=15,
        )
        resp_prov.raise_for_status()
        total_proveedores = int(resp_prov.json().get("total_results", 0))

        upsert("1crm", "productos_total",
               valor=total_productos, unidad="productos", estado="ok")
        upsert("1crm", "proveedores_total",
               valor=total_proveedores, unidad="proveedores", estado="ok")

        log.info(f"1CRM: {total_productos} productos | {total_proveedores} proveedores")
    except Exception as e:
        log.error(f"1CRM check falló: {e}")
        upsert("1crm", "productos_total", estado="critical",
               valor_texto=f"Error: {str(e)[:80]}")


# ─────────────────────────────────────────────────────────────
# LOOP PRINCIPAL
# ─────────────────────────────────────────────────────────────
def check_heartbeat() -> None:
    """Latido del backend. Como el monitor corre DENTRO del worker de Railway,
    el solo hecho de escribir este timestamp prueba que el backend está vivo.
    El dashboard calcula 'hace X min' — si es reciente, Railway está corriendo."""
    upsert("sistema", "backend_activo",
           valor_texto=datetime.now(timezone.utc).isoformat(),
           estado="ok")


def run_all_checks() -> None:
    log.info("▶ Monitor: ejecutando chequeo de todos los servicios...")
    check_serpapi()
    check_removebg()
    check_anthropic()
    check_google_cse()
    check_supabase()
    check_railway()
    check_1crm()
    check_heartbeat()
    log.info("✓ Monitor: chequeo completo")


def main() -> None:
    log.info("Agente Monitor iniciado — primer chequeo en 120s, luego cada %ds", MONITOR_INTERVAL)
    # Esperar 2 minutos antes del primer chequeo para no saturar Railway al arrancar
    time.sleep(120)
    while True:
        try:
            run_all_checks()
        except Exception as e:
            log.error(f"Error en monitor loop: {e}")
        time.sleep(MONITOR_INTERVAL)


if __name__ == "__main__":
    main()
