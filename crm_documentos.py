"""
DOCUMENTOS EN 1CRM — guarda cada archivo que llega a un stream (PO del cliente, guía de envío, comprobante de pago,
captura de compra, packing list, evidencia de recepción) en el módulo Documentos de 1CRM y lo RELACIONA con el
registro de la acción (Sales Order, Shipping, Purchase Order, Payment).

POR QUÉ ASÍ (verificado en vivo el 2026-11-10 contra mromasterpro.1crmcloud.com):
  · La API REST de 1CRM NO guarda los bytes del archivo: POST /data/Document (y DocumentRevision) solo crea el registro y
    guarda el nombre como texto; `filename` es un campo `file_ref` y no hay endpoint de subida. El `subir_po_a_so` anterior
    creaba Documentos VACÍOS (sin revisión, sin archivo) y aun así se logueaba "guardado ✓".
  · El archivo SÍ se guarda cuando se sube por la pantalla de Documentos (Playwright, igual que el publicador con las
    imágenes de producto): crea el Document + su DocumentRevision con el archivo real.
  · La pantalla no tiene campo "Relativo a", pero Document tiene `parent_type`/`parent_id` (módulos permitidos: Accounts,
    Bills, Cases, CreditNotes, ExpenseReports, Invoice, Payments, PurchaseOrders, Quotes, SalesOrders, Shipping,
    Timesheets) y la API sí acepta PATCH de esos campos → la relación se fija por API después de subir el archivo.
  · `filter_text` de Documentos solo busca por NOMBRE (no por palabras clave ni descripción): el nombre lleva la
    referencia y el nombre del archivo, y de ahí sale la detección de duplicados.

Corre en una cola con UN solo hilo (un Chromium a la vez) para no frenar el chat ni gastar memoria; el resultado se
avisa por el callback `on_done`. Es best-effort: si falla, la acción de negocio ya quedó hecha y solo se avisa.
"""
import os
import re
import queue
import logging
import mimetypes
import pathlib
import threading
import time
from urllib.parse import urlparse, unquote

import httpx

log = logging.getLogger("crm_documentos")

# tipo de documento → (etiqueta legible, módulo 1CRM al que se liga por defecto)
TIPOS = {
    "orden_compra_cliente": ("Orden de compra del cliente", "SalesOrders"),
    "guia_envio":           ("Guía de envío", "Shipping"),
    "captura_compra":       ("Compra a proveedor (captura)", "PurchaseOrders"),
    "packing_list":         ("Packing list / order confirmation", "PurchaseOrders"),
    "evidencia_recepcion":  ("Evidencia de recepción", "PurchaseOrders"),
    "comprobante_pago":     ("Comprobante de pago", "Payments"),
}
PARENT_LABEL = {
    "SalesOrders": "Sales Order", "Shipping": "Envío", "PurchaseOrders": "Purchase Order",
    "Payments": "Pago", "Bills": "Cuenta por pagar", "Invoice": "Factura",
}
PARENTS_VALIDOS = {"Accounts", "Bills", "Cases", "CreditNotes", "ExpenseReports", "Invoice", "Payments",
                   "PurchaseOrders", "Quotes", "SalesOrders", "Shipping", "Timesheets"}
MAX_BYTES = 40 * 1024 * 1024


def _cfg():
    base = (os.environ.get("ONECRM_URL", "") or "").rstrip("/")
    return base, os.environ.get("ONECRM_USERNAME", ""), os.environ.get("ONECRM_PASSWORD", "")


def habilitado() -> bool:
    base, u, p = _cfg()
    return bool(base and u and p) and os.environ.get("DOCUMENTOS_CRM", "on").lower() not in ("off", "0", "false", "no")


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────
def nombre_archivo(file_url: str, sugerido: str = "") -> str:
    """Nombre limpio del archivo: el de la URL sin el prefijo de timestamp que le pone el frontend al subirlo."""
    crudo = sugerido or unquote(pathlib.PurePosixPath(urlparse(file_url).path).name) or "archivo"
    crudo = re.sub(r"^\d{10,}[-_]", "", crudo)            # 1731012345678-nombre.pdf → nombre.pdf
    crudo = re.sub(r"[^\w.\- ()]+", "_", crudo).strip(" ._") or "archivo"
    return crudo[:120]


def nombre_documento(tipo: str, referencia: str, archivo: str) -> str:
    etiqueta = TIPOS.get(tipo, (tipo, ""))[0]
    partes = [etiqueta] + ([referencia.strip()] if (referencia or "").strip() else []) + [pathlib.Path(archivo).stem[:60]]
    return " · ".join(partes)[:200]


def url_documento(doc_id: str) -> str:
    base = _cfg()[0]
    return f"{base}/index.php?module=Documents&action=DetailView&record={doc_id}"


def _crm_auth():
    _, u, p = _cfg()
    return (u, p)


def _buscar_duplicado(nombre: str, parent_id: str) -> str:
    """id del Documento con ESE nombre ya ligado a ESE registro (reintento / doble clic), o ''."""
    base = _cfg()[0]
    try:
        r = httpx.get(f"{base}/api.php/data/Document", params={"filter_text": nombre, "limit": 20},
                      auth=_crm_auth(), timeout=40).json()
        filas = r if isinstance(r, list) else (r.get("records") or [])
        for f in filas:
            d = httpx.get(f"{base}/api.php/data/Document/{f['id']}", auth=_crm_auth(), timeout=40).json()
            d = d.get("record", d)
            if (d.get("document_name") or "") == nombre and (d.get("parent_id") or "") == parent_id:
                return d["id"]
    except Exception as e:
        log.warning(f"búsqueda de duplicado falló (se continúa sin deduplicar): {e}")
    return ""


def _subir_por_pantalla(contenido: bytes, filename: str, mime: str, nombre: str, descripcion: str, keywords: str) -> str:
    """Crea el Document + DocumentRevision con el archivo REAL por la pantalla de Documentos. Devuelve el id."""
    from playwright.sync_api import sync_playwright
    base, user, pwd = _cfg()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_context(ignore_https_errors=True, viewport={"width": 1300, "height": 900}).new_page()
            page.goto(f"{base}/index.php?module=Users&action=Login", timeout=40000)
            page.fill('input[name="user_name"]', user)
            page.fill('input[name="user_password"]', pwd)
            page.click('input[type="submit"], button[type="submit"]')
            page.wait_for_url(f"{base}/index.php*", timeout=30000)

            page.goto(f"{base}/index.php?module=Documents&action=EditView", timeout=40000)
            page.wait_for_load_state("networkidle", timeout=30000)
            page.fill('input[name="document_name"]', nombre)
            if descripcion:
                page.fill('textarea[name="description"]', descripcion)
            if keywords:
                page.fill('input[name="keywords"]', keywords)
            page.set_input_files('input[name="revision_filename"]',
                                 {"name": filename, "mimeType": mime, "buffer": contenido})
            botones = page.locator("button.input-button").filter(has_text="Guardar")
            visibles = [i for i in range(botones.count()) if botones.nth(i).is_visible()]
            if not visibles:
                raise RuntimeError("no encontré el botón Guardar de Documentos")
            botones.nth(visibles[0]).click()
            # Al guardar, 1CRM sube el archivo (upload.php) y redirige a la DetailView con record=<id>.
            page.wait_for_url(re.compile(r"module=Documents&action=DetailView&record=[0-9a-f-]{36}"), timeout=60000)
            m = re.search(r"record=([0-9a-f-]{36})", page.url)
            if not m:
                raise RuntimeError(f"no pude leer el id del documento creado ({page.url[:120]})")
            return m.group(1)
        finally:
            browser.close()


def _ligar(doc_id: str, parent_type: str, parent_id: str) -> None:
    base = _cfg()[0]
    r = httpx.patch(f"{base}/api.php/data/Document/{doc_id}",
                    json={"data": {"parent_type": parent_type, "parent_id": parent_id}},
                    auth=_crm_auth(), timeout=40)
    if r.status_code >= 300:
        raise RuntimeError(f"no se pudo ligar el documento a {parent_type}: {r.status_code} {r.text[:160]}")


def _verificar(doc_id: str, parent_type: str, parent_id: str) -> dict:
    """Verifica en 1CRM que el Documento quedó con ARCHIVO real y con la relación puesta (no se da por hecho)."""
    base = _cfg()[0]
    d = httpx.get(f"{base}/api.php/data/Document/{doc_id}", auth=_crm_auth(), timeout=40).json()
    d = d.get("record", d)
    rev_id = d.get("document_revision_id") or ""
    rev = {}
    if rev_id:
        rev = httpx.get(f"{base}/api.php/data/DocumentRevision/{rev_id}", auth=_crm_auth(), timeout=40).json()
        rev = rev.get("record", rev)
    ok_archivo = bool(rev.get("filename"))
    ok_rel = d.get("parent_type") == parent_type and d.get("parent_id") == parent_id
    return {"ok": ok_archivo and ok_rel, "archivo": rev.get("filename") or "", "relacion": ok_rel}


# ─────────────────────────────────────────────────────────────
# API pública
# ─────────────────────────────────────────────────────────────
def guardar_documento(*, tipo: str, file_url: str = "", contenido: bytes | None = None, nombre_archivo_sugerido: str = "",
                      parent_type: str = "", parent_id: str = "", referencia: str = "", detalle: str = "",
                      mime: str = "") -> dict:
    """Guarda el archivo en Documentos de 1CRM y lo liga al registro. Síncrono (tarda ~10 s). Devuelve
    {ok, doc_id, nombre, url, duplicado?, error?}."""
    if not habilitado():
        return {"ok": False, "error": "documentos de 1CRM deshabilitado o sin credenciales"}
    etiqueta, parent_defecto = TIPOS.get(tipo, (tipo, ""))
    parent_type = parent_type or parent_defecto
    if parent_type not in PARENTS_VALIDOS or not parent_id:
        return {"ok": False, "error": f"falta el registro al que ligarlo (parent_type={parent_type!r}, parent_id={parent_id!r})"}
    try:
        if contenido is None:
            if not file_url:
                return {"ok": False, "error": "no hay archivo (file_url vacío)"}
            r = httpx.get(file_url, timeout=60, follow_redirects=True)
            r.raise_for_status()
            contenido = r.content
            mime = mime or r.headers.get("content-type", "").split(";")[0].strip()
        if not contenido:
            return {"ok": False, "error": "el archivo está vacío"}
        if len(contenido) > MAX_BYTES:
            return {"ok": False, "error": f"el archivo pesa {len(contenido) // (1024 * 1024)} MB (máximo 40 MB)"}
        filename = nombre_archivo(file_url, nombre_archivo_sugerido)
        if not pathlib.Path(filename).suffix:
            ext = mimetypes.guess_extension(mime or "") or (".pdf" if contenido[:4] == b"%PDF" else ".bin")
            filename += ext
        mime = mime if mime and mime != "application/octet-stream" else (mimetypes.guess_type(filename)[0] or "application/octet-stream")
        nombre = nombre_documento(tipo, referencia, filename)

        dup = _buscar_duplicado(nombre, parent_id)
        if dup:
            log.info(f"Documento ya existía ({nombre}) → {dup}")
            return {"ok": True, "doc_id": dup, "nombre": nombre, "url": url_documento(dup), "duplicado": True}

        desc = f"{etiqueta}. Ligado a {PARENT_LABEL.get(parent_type, parent_type)}" + (f" {referencia}" if referencia else "") + "."
        if detalle:
            desc += f" {detalle}"
        desc += " Guardado automáticamente por Brain."
        doc_id = _subir_por_pantalla(contenido, filename, mime, nombre, desc, referencia)
        _ligar(doc_id, parent_type, parent_id)
        v = _verificar(doc_id, parent_type, parent_id)
        if not v["ok"]:
            return {"ok": False, "doc_id": doc_id, "nombre": nombre, "url": url_documento(doc_id),
                    "error": f"el documento se creó pero no quedó completo en 1CRM (archivo={v['archivo']!r}, relación={v['relacion']})"}
        log.info(f"Documento guardado en 1CRM: {nombre} → {parent_type}/{parent_id} ({doc_id})")
        return {"ok": True, "doc_id": doc_id, "nombre": nombre, "url": url_documento(doc_id), "archivo": v["archivo"]}
    except Exception as e:
        log.warning(f"guardar_documento falló ({tipo}): {e}")
        return {"ok": False, "error": str(e)[:300]}


# ── Cola en segundo plano (un solo Chromium a la vez) ────────────────────────────────────────────
_cola: "queue.Queue[tuple]" = queue.Queue()
_hilo = None
_lock = threading.Lock()


def _trabajador():
    while True:
        kwargs, on_done = _cola.get()
        res = {}
        try:
            for intento in (1, 2):
                res = guardar_documento(**kwargs)
                if res.get("ok") or "falta" in (res.get("error") or "") or "vacío" in (res.get("error") or ""):
                    break
                time.sleep(3)
        except Exception as e:  # nunca matar el hilo
            res = {"ok": False, "error": str(e)[:300]}
        try:
            if on_done:
                on_done(res)
        except Exception as e:
            log.warning(f"on_done falló: {e}")
        finally:
            _cola.task_done()


def encolar(on_done=None, **kwargs) -> bool:
    """Encola el guardado y regresa de inmediato (el chat no espera). `on_done(resultado)` corre al terminar."""
    global _hilo
    if not habilitado():
        return False
    with _lock:
        if _hilo is None or not _hilo.is_alive():
            _hilo = threading.Thread(target=_trabajador, name="crm_documentos", daemon=True)
            _hilo.start()
    _cola.put((kwargs, on_done))
    return True
