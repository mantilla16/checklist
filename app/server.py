"""Servidor local de la interfaz de validacion de pagos a proveedores.

Ejecutar:  python app/server.py     ->  http://127.0.0.1:5000

El .xlsx de la carpeta del proyecto se usa SOLO como plantilla de formato.
Los lotes, registros y validaciones los captura el usuario y se guardan en
`app/data/trabajo.json`.
"""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.utils import secure_filename

from comprobantes import (analizar_comprobante, analizar_registro_compras,
                          renglones_desde_comprobante, validar_egreso,
                          validar_registro_compras)
import autenticacion as auth
import bd
from decisor import decidir
from entrada import analizar_entrada, leer_texto, validar_entrada
from excel_io import exportar as exportar_libro
from excel_io import leer_plantilla
from extractor import analizar
from factura_electronica import analizar_factura, emparejar
from orden_compra import analizar_orden, validar_orden
from radian import analizar_radian
from validador_ia import MODELO, hay_credenciales, validar

BASE = Path(__file__).resolve().parent

# Rutas configurables: en el servidor los datos suelen ir fuera del codigo.
#   CHECKLIST_DATOS     carpeta de la base y del libro generado
#   CHECKLIST_SUBIDAS   carpeta de los PDF cargados
#   CHECKLIST_PLANTILLA archivo .xlsx que se usa como formato de salida
DATA = Path(os.environ.get("CHECKLIST_DATOS") or BASE / "data")
UPLOADS = Path(os.environ.get("CHECKLIST_SUBIDAS") or BASE / "uploads")
UPLOADS.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)

TRABAJO = DATA / "trabajo.json"      # solo para migrar el estado anterior
SALIDA = DATA / "libro-validado.xlsx"
CATEGORIAS = {"correo", "factura", "orden", "egreso", "entrada", "otro"}

app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB por request

# La cookie de sesion se firma con un secreto que vive en la carpeta de datos,
# para que sea el mismo en los tres trabajadores de gunicorn y sobreviva a los
# reinicios.
app.secret_key = auth.clave_de_sesion()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,     # el JavaScript de la pagina no la ve
    SESSION_COOKIE_SAMESITE="Lax",    # no se envia desde otros sitios
    # Solo por HTTPS. Queda apagada por omision porque con un sitio en HTTP
    # plano encenderla dejaria a nadie poder entrar: el navegador no mandaria
    # la cookie. Con certificado, poner CHECKLIST_COOKIE_SEGURA=1.
    SESSION_COOKIE_SECURE=os.environ.get("CHECKLIST_COOKIE_SEGURA") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


def plantilla() -> Path | None:
    """Plantilla de formato: la de CHECKLIST_PLANTILLA, o la de la carpeta.

    En el servidor se indica con la variable de entorno; en local basta con
    dejar el .xlsx en la carpeta del proyecto.
    """
    indicada = os.environ.get("CHECKLIST_PLANTILLA")
    if indicada:
        ruta = Path(indicada)
        return ruta if ruta.is_file() else None

    for carpeta in (BASE.parent, DATA):
        for ruta in sorted(carpeta.glob("*.xlsx")):
            if not ruta.name.startswith("~$") and ruta.name != SALIDA.name:
                return ruta
    return None


def motivo_sin_plantilla() -> str:
    """Por que no se encontro, con la ruta a la vista.

    Decir solo "no hay ningun .xlsx en la carpeta del proyecto" despista en el
    servidor, donde la ruta la fija CHECKLIST_PLANTILLA: el archivo puede estar
    subido y a un directorio de distancia.
    """
    indicada = os.environ.get("CHECKLIST_PLANTILLA")
    if not indicada:
        return (f"No hay ningun .xlsx para usar como plantilla en "
                f"{BASE.parent} ni en {DATA}.")

    esperada = Path(indicada)
    mensaje = f"No existe la plantilla {esperada}."
    if esperada.parent.is_dir():
        # dict y no set: conserva el orden y descarta el archivo repetido
        # cuando las dos carpetas son la misma o una contiene a la otra
        cerca = dict.fromkeys(
            r for c in (esperada.parent, DATA) if c.is_dir()
            for r in sorted(c.rglob("*.xlsx"))
            if not r.name.startswith("~$") and r.name != SALIDA.name)
        if cerca:
            lista = ", ".join(str(r) for r in list(cerca)[:4])
            mensaje += f" Hay .xlsx cerca: {lista}. Muevelo o renombralo."
    return mensaje


EXTENSIONES = (".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")


def nombre_seguro(nombre: str) -> str:
    """Conserva la extension: la entrada de inventario puede ser imagen."""
    limpio = secure_filename(nombre) or "documento.pdf"
    return limpio if limpio.lower().endswith(EXTENSIONES) else limpio + ".pdf"


def guardar_subida(archivo, categoria: str = "otro") -> dict:
    """Guarda el archivo una sola vez: se identifica por su contenido.

    Si el mismo PDF ya se habia cargado (mismo SHA-256) se reutiliza el que
    esta en disco en vez de dejar otra copia.
    """
    datos = archivo.read()
    huella = bd.hash_archivo(datos)
    nombre = nombre_seguro(archivo.filename or "")

    ficha = bd.documento_por_hash(huella)
    if ficha and (UPLOADS / ficha["archivo"]).exists():
        return {"ruta": ficha["archivo"], "nombre": ficha["nombre"],
                "hash": huella, "repetido": True}

    guardado = f"{huella[:16]}_{nombre}"
    (UPLOADS / guardado).write_bytes(datos)
    bd.registrar_documento(huella, nombre, guardado, categoria, len(datos))
    return {"ruta": guardado, "nombre": nombre, "hash": huella, "repetido": False}


# --------------------------------------------------------------------------- #
# Sesion
#
# Todo pide sesion menos la propia pantalla de entrada. El archivo estatico
# tambien: con static_url_path="" la interfaz queda servida en /index.html, y
# sin cubrir esa ruta el login se saltaria escribiendola a mano.
# --------------------------------------------------------------------------- #

SIN_SESION = {"inicio", "login", "sesion_actual"}


@app.before_request
def exigir_sesion():
    if request.endpoint in SIN_SESION or "usuario" in session:
        return None
    if request.endpoint == "static":
        return send_from_directory(app.static_folder, "login.html"), 401
    # Las llamadas de la interfaz son fetch: un 401 le basta para recargar y
    # mostrar la pantalla de entrada.
    return jsonify({"error": "sesion",
                    "mensaje": "Tu sesion termino. Vuelve a entrar."}), 401


@app.get("/api/sesion")
def sesion_actual():
    """Quien esta dentro. Publica: la pantalla de entrada la consulta."""
    return jsonify({
        "dentro": "usuario" in session,
        "usuario": session.get("usuario", ""),
        "nombre": session.get("nombre", ""),
        "hay_usuarios": auth.hay_usuarios(),
    })


@app.post("/api/login")
def login():
    datos = request.get_json(silent=True) or {}
    persona, motivo = auth.verificar(datos.get("usuario", ""), datos.get("clave", ""))
    if persona is None:
        return jsonify({"error": "credenciales", "mensaje": motivo}), 401

    session.clear()
    session.permanent = True
    session["usuario"] = persona["usuario"]
    session["nombre"] = persona["nombre"]
    return jsonify({"dentro": True, **persona})


@app.post("/api/salir")
def salir():
    quien = session.get("usuario", "")
    session.clear()
    if quien:
        bd.registrar_evento("salida", quien)
    return jsonify({"dentro": False})


@app.get("/")
def inicio():
    archivo = "index.html" if "usuario" in session else "login.html"
    return send_from_directory(app.static_folder, archivo)


# --------------------------------------------------------------------------- #
# Plantilla
# --------------------------------------------------------------------------- #

@app.get("/api/plantilla")
def info_plantilla():
    """Formato disponible: columnas de la tabla y campos del encabezado."""
    ruta = plantilla()
    if ruta is None:
        return jsonify({"valida": False, "error": motivo_sin_plantilla()})
    try:
        descripcion = leer_plantilla(ruta)
    except Exception as exc:
        return jsonify({"valida": False, "error": f"No se pudo leer la plantilla: {exc}"})

    descripcion["nombre"] = ruta.name
    return jsonify(descripcion)


# --------------------------------------------------------------------------- #
# Documentos PDF
# --------------------------------------------------------------------------- #

@app.post("/api/cargar")
def cargar():
    """Recibe uno o varios PDF, los guarda y devuelve el analisis de cada uno."""
    categoria = request.form.get("categoria", "otro")
    if categoria not in CATEGORIAS:
        categoria = "otro"

    archivos = request.files.getlist("archivos")
    if not archivos:
        return jsonify({"error": "No se recibio ningun archivo"}), 400

    resultados, errores = [], []
    for archivo in archivos:
        subida = guardar_subida(archivo, categoria)
        nombre, destino = subida["nombre"], UPLOADS / subida["ruta"]
        try:
            resultado = analizar(str(destino), nombre, categoria)
            resultado["ruta"] = destino.name
            resultados.append(resultado)
        except Exception as exc:   # PDF corrupto o protegido
            errores.append({"nombre": nombre, "detalle": str(exc)})

    return jsonify({"documentos": resultados, "errores": errores})


@app.get("/api/pdf/<nombre>")
def ver_pdf(nombre: str):
    return send_from_directory(UPLOADS, secure_filename(nombre))


@app.post("/api/lote-desde-egresos")
def lote_desde_egresos():
    """Arma un lote a partir de los comprobantes de egreso (columnas B a G)."""
    archivos = request.files.getlist("archivos")
    if not archivos:
        return jsonify({"error": "No se recibio ningun comprobante"}), 400

    registros, errores = [], []
    for archivo in archivos:
        subida = guardar_subida(archivo, "egreso")
        nombre, destino = subida["nombre"], UPLOADS / subida["ruta"]
        try:
            analisis = analizar(str(destino), nombre, "egreso")
        except Exception as exc:
            errores.append({"nombre": nombre, "detalle": str(exc)})
            continue

        comprobante = analizar_comprobante(analisis.get("texto") or "")
        if comprobante is None:
            errores.append({
                "nombre": nombre,
                "detalle": "No parece un comprobante de egreso (no dice 'Girado a')",
            })
            continue

        analisis["ruta"] = destino.name
        analisis["comprobante"] = comprobante
        registros.append({
            "titular": comprobante["titular"],
            "nit_cliente": comprobante["nit_cliente"],
            "documento": comprobante["documento"],
            "valor": comprobante["valor"],
            "egreso": comprobante["numero"],
            "fecha": comprobante["fecha"],
            "banco": comprobante["banco"],
            "renglones": renglones_desde_comprobante(comprobante),
            "documento_pdf": analisis,
        })

    if not registros:
        return jsonify({"error": "Ningun archivo era un comprobante de egreso",
                        "errores": errores}), 400

    # El encabezado del lote sale de los propios comprobantes
    fechas = sorted({r["fecha"] for r in registros if r["fecha"]})
    bancos = [r["banco"] for r in registros if r["banco"]]
    registros.sort(key=lambda r: r["egreso"])

    clientes = [r["nit_cliente"] for r in registros if r["nit_cliente"]]
    return jsonify({
        "info": {
            "tipo_pago": "PAGO A PROVEEDORES",
            "nit_cliente": clientes[0] if clientes else "",
            "cuenta": bancos[0] if bancos else "",
            "valor_total": sum(r["valor"] or 0 for r in registros),
            "fecha_creacion": fechas[0] if fechas else "",
            "fecha_aplicacion": fechas[-1] if fechas else "",
        },
        "registros": registros,
        "errores": errores,
    })


@app.post("/api/facturas")
def validar_facturas():
    """Valida facturas electronicas y las empareja con los renglones (M a P).

    Espera en el formulario:
      nit_cliente  - de quien paga, sale del comprobante de egreso
      nit_tercero  - documento titular de la tabla azul
      facturas     - numeros de factura del registro, separados por coma
    """
    archivos = request.files.getlist("archivos")
    if not archivos:
        return jsonify({"error": "No se recibio ninguna factura"}), 400

    nit_cliente = request.form.get("nit_cliente", "").strip()
    nit_tercero = request.form.get("nit_tercero", "").strip()
    facturas = [f.strip() for f in (request.form.get("facturas") or "").split(",") if f.strip()]

    resultados, errores = [], []
    for archivo in archivos:
        subida = guardar_subida(archivo, "factura")
        nombre, destino = subida["nombre"], UPLOADS / subida["ruta"]

        # Si el registro tiene una sola factura, se sabe cual se espera
        esperado = {"nit_cliente": nit_cliente, "nit_tercero": nit_tercero,
                    "numero": facturas[0] if len(facturas) == 1 else ""}
        try:
            analisis = analizar_factura(str(destino), nombre, esperado)
        except Exception as exc:
            errores.append({"nombre": nombre, "detalle": str(exc)})
            continue

        analisis["ruta"] = destino.name
        analisis["renglon"] = emparejar(analisis, facturas)
        resultados.append(analisis)

    return jsonify({"facturas": resultados, "errores": errores})


@app.post("/api/registro-compras")
def registro_compras():
    """Lee los documentos P (registro contable de compras) para la columna Q.

    El total del documento P es el "valor a pagar": ya trae aplicadas las
    retenciones y el IVA descontable.
    """
    archivos = request.files.getlist("archivos")
    if not archivos:
        return jsonify({"error": "No se recibio ningun documento"}), 400

    facturas = [f.strip() for f in (request.form.get("facturas") or "").split(",") if f.strip()]
    resultados, errores = [], []

    for archivo in archivos:
        subida = guardar_subida(archivo, "contable")
        nombre, destino = subida["nombre"], UPLOADS / subida["ruta"]
        try:
            analisis = analizar(str(destino), nombre, "otro")
        except Exception as exc:
            errores.append({"nombre": nombre, "detalle": str(exc)})
            continue

        texto = analisis.get("texto") or ""

        # Un egreso (G) valida la columna U; un registro de compras (P), la V
        comprobante = analizar_comprobante(texto)
        if comprobante and "GIRADO" in texto.upper():
            validacion = validar_egreso(comprobante, request.form.get("valor_tabla"))
            resultados.append({
                "tipo": "egreso", "nombre": nombre, "ruta": destino.name,
                "numero": comprobante["numero"], "total": comprobante["valor"],
                "columnas": validacion["columnas"],
                "revisiones": validacion["revisiones"],
                "valor_tabla": validacion["valor_tabla"],
            })
            continue

        documento = analizar_registro_compras(texto)
        if documento is None:
            errores.append({
                "nombre": nombre,
                "detalle": "No parece un registro contable (P) ni un egreso (G)",
            })
            continue

        par = emparejar({"columnas": {"N": documento["factura"]}}, facturas)
        validacion = validar_registro_compras(documento, {
            "orden_compra": request.form.get("orden_compra", ""),
            "factura": par.get("factura") or documento["factura"],
        })
        documento.update({
            "tipo": "compras", "nombre": nombre, "ruta": destino.name,
            "renglon": par, "columnas": validacion["columnas"],
            "revisiones": validacion["revisiones"],
        })
        resultados.append(documento)

    if not resultados:
        return jsonify({"error": "Ningun archivo era un registro contable (P) ni un egreso (G)",
                        "errores": errores}), 400

    return jsonify({"documentos": resultados, "errores": errores})


@app.post("/api/radian")
def validar_radian():
    """Valida los soportes de RADIAN (columna S).

    El PDF de RADIAN es imagen pura, asi que se pasa por OCR: tarda unos
    segundos por documento.
    """
    archivos = request.files.getlist("archivos")
    if not archivos:
        return jsonify({"error": "No se recibio ningun soporte de RADIAN"}), 400

    nit_tercero = request.form.get("nit_tercero", "").strip()
    facturas = [f.strip() for f in (request.form.get("facturas") or "").split(",") if f.strip()]

    resultados, errores = [], []
    for archivo in archivos:
        subida = guardar_subida(archivo, "radian")
        nombre, destino = subida["nombre"], UPLOADS / subida["ruta"]

        esperado = {"nit_tercero": nit_tercero,
                    "numero": facturas[0] if len(facturas) == 1 else ""}
        try:
            analisis = analizar_radian(str(destino), nombre, esperado)
        except Exception as exc:
            errores.append({"nombre": nombre, "detalle": str(exc)})
            continue

        analisis["ruta"] = destino.name
        analisis["renglon"] = emparejar(
            {"columnas": {"N": analisis["numero_documento"]}}, facturas)
        resultados.append(analisis)

    if not resultados:
        return jsonify({"error": "No se pudo leer ningun soporte de RADIAN",
                        "errores": errores}), 400

    return jsonify({"radian": resultados, "errores": errores})


@app.post("/api/orden-compra")
def orden_compra():
    """Lee las ordenes de compra (documentos Y) y las valida (columna T)."""
    archivos = request.files.getlist("archivos")
    if not archivos:
        return jsonify({"error": "No se recibio ninguna orden de compra"}), 400

    nit_tercero = request.form.get("nit_tercero", "").strip()
    facturas = [f.strip() for f in (request.form.get("facturas") or "").split(",") if f.strip()]

    # La interfaz manda la fecha y la cantidad de cada factura, si las tiene
    try:
        contexto = json.loads(request.form.get("contexto") or "{}")
    except json.JSONDecodeError:
        contexto = {}

    resultados, errores = [], []
    for archivo in archivos:
        subida = guardar_subida(archivo, "orden")
        nombre, destino = subida["nombre"], UPLOADS / subida["ruta"]
        try:
            analisis = analizar(str(destino), nombre, "orden")
        except Exception as exc:
            errores.append({"nombre": nombre, "detalle": str(exc)})
            continue

        orden = analizar_orden(analisis.get("texto") or "")
        if orden is None:
            errores.append({"nombre": nombre,
                            "detalle": "No parece una orden de compra (Y)"})
            continue

        # La orden puede cubrir varias facturas del registro (entregas parciales)
        datos = contexto.get(orden["numero"]) or contexto.get("__unico__") or {}
        validacion = validar_orden(orden, {
            "nit_tercero": nit_tercero,
            "fecha_factura": datos.get("fecha_factura", ""),
            "cantidad_factura": datos.get("cantidad_factura"),
        })
        validacion["nombre"] = nombre
        validacion["ruta"] = destino.name
        resultados.append(validacion)

    if not resultados:
        return jsonify({"error": "Ningun archivo era una orden de compra",
                        "errores": errores}), 400

    return jsonify({"ordenes": resultados, "errores": errores})


@app.post("/api/entrada")
def validar_entradas():
    """Valida las entradas de inventario (columna W). Acepta PDF e imagen."""
    archivos = request.files.getlist("archivos")
    if not archivos:
        return jsonify({"error": "No se recibio ninguna entrada de inventario"}), 400

    esperado = {
        "nit_tercero": request.form.get("nit_tercero", "").strip(),
        "nit_cliente": request.form.get("nit_cliente", "").strip(),
        "orden_compra": request.form.get("orden_compra", "").strip(),
    }
    facturas = [f.strip() for f in (request.form.get("facturas") or "").split(",") if f.strip()]

    # Cantidad facturada por numero de factura, leida en el Paso 3
    try:
        cantidades = json.loads(request.form.get("cantidades") or "{}")
    except json.JSONDecodeError:
        cantidades = {}

    resultados, errores = [], []
    for archivo in archivos:
        subida = guardar_subida(archivo, "entrada")
        nombre, destino = subida["nombre"], UPLOADS / subida["ruta"]
        try:
            texto, metodo = leer_texto(str(destino))
        except Exception as exc:
            errores.append({"nombre": nombre, "detalle": str(exc)})
            continue

        datos = analizar_entrada(texto)
        if datos is None:
            errores.append({"nombre": nombre,
                            "detalle": "No parece una entrada de inventario (E)"})
            continue

        par = emparejar({"columnas": {"N": datos["factura"]}}, facturas)
        numero = par.get("factura") or datos["factura"]
        validacion = validar_entrada(datos, {
            **esperado,
            "factura": numero,
            "cantidad_factura": cantidades.get(str(numero)),
        })
        validacion.update({"nombre": nombre, "ruta": destino.name,
                           "metodo": metodo, "renglon": par})
        resultados.append(validacion)

    if not resultados:
        return jsonify({"error": "Ningun archivo era una entrada de inventario",
                        "errores": errores}), 400

    return jsonify({"entradas": resultados, "errores": errores})


@app.post("/api/entrada/revalidar")
def revalidar_entrada():
    """Vuelve a validar una entrada ya leida, sin repetir el OCR."""
    datos = request.get_json(silent=True) or {}
    if not datos.get("entrada"):
        return jsonify({"error": "Falta la entrada"}), 400
    return jsonify(validar_entrada(datos["entrada"], datos.get("esperado") or {}))


# --------------------------------------------------------------------------- #
# Veredicto
# --------------------------------------------------------------------------- #

@app.post("/api/decidir")
def decidir_valor():
    """Veredicto del motor de reglas: que valor esta aprobado y con que soporte."""
    datos = request.get_json(silent=True) or {}
    return jsonify(decidir(datos.get("documentos") or []))


@app.post("/api/validar-ia")
def validar_ia():
    """Segunda opinion con el modelo (solo si hay credencial configurada)."""
    datos = request.get_json(silent=True) or {}
    valor_lote = datos.get("valor_lote")
    try:
        valor_lote = float(valor_lote) if valor_lote not in (None, "") else None
    except (TypeError, ValueError):
        valor_lote = None
    return jsonify(validar(datos.get("documentos") or [], valor_lote))


@app.get("/api/estado-ia")
def estado_ia():
    return jsonify({"disponible": hay_credenciales(), "modelo": MODELO})


# --------------------------------------------------------------------------- #
# Trabajo (lotes capturados)
# --------------------------------------------------------------------------- #

@app.get("/api/trabajo")
def leer_trabajo():
    return jsonify(bd.leer_trabajo())


@app.post("/api/trabajo")
def guardar_trabajo():
    datos = request.get_json(silent=True) or {}
    if "lotes" not in datos:
        return jsonify({"error": "Falta la lista de lotes"}), 400
    return jsonify(bd.guardar_trabajo(datos))


@app.post("/api/trabajo/borrar")
def borrar_trabajo():
    bd.borrar_trabajo()
    return jsonify({"ok": True})


@app.get("/api/eventos")
def eventos():
    """Bitacora: que se valido, cuando y sobre que documento."""
    return jsonify({"eventos": bd.leer_eventos(int(request.args.get("limite", 200)))})


@app.get("/api/mantenimiento")
def mantenimiento():
    """Reporta los archivos de `uploads` que ya nadie referencia.

    Los que tienen nombre con marca de tiempo (`1788838778326_x.pdf`) son del
    esquema anterior, antes de identificar los archivos por su contenido.
    """
    import re as _re

    con_hash, viejos, tamano_viejos = [], [], 0
    for ruta in UPLOADS.iterdir():
        if not ruta.is_file():
            continue
        if _re.match(r"^\d{13}_", ruta.name):
            viejos.append(ruta.name)
            tamano_viejos += ruta.stat().st_size
        else:
            con_hash.append(ruta.name)

    return jsonify({
        "con_hash": len(con_hash),
        "esquema_anterior": len(viejos),
        "mb_recuperables": round(tamano_viejos / 1_048_576, 1),
        "archivos": sorted(viejos)[:20],
    })


@app.post("/api/mantenimiento/limpiar")
def limpiar_uploads():
    """Borra los archivos del esquema anterior. Se pide explicitamente."""
    import re as _re

    borrados, liberado = 0, 0
    for ruta in list(UPLOADS.iterdir()):
        if ruta.is_file() and _re.match(r"^\d{13}_", ruta.name):
            liberado += ruta.stat().st_size
            ruta.unlink()
            borrados += 1

    bd.registrar_evento("mantenimiento",
                        f"{borrados} archivo(s) del esquema anterior, "
                        f"{round(liberado / 1_048_576, 1)} MB")
    return jsonify({"borrados": borrados, "mb": round(liberado / 1_048_576, 1)})


@app.get("/api/resumen")
def resumen():
    """Estado de todos los registros de todos los lotes."""
    return jsonify(bd.resumen())


# --------------------------------------------------------------------------- #
# Exportacion
# --------------------------------------------------------------------------- #

@app.post("/api/exportar")
def exportar():
    """Genera el libro final a partir de la plantilla y los lotes capturados."""
    ruta = plantilla()
    if ruta is None:
        return jsonify({"error": "No hay plantilla en la carpeta del proyecto"}), 400

    lotes = (request.get_json(silent=True) or {}).get("lotes") or []
    if not lotes:
        return jsonify({"error": "No hay lotes para exportar"}), 400

    try:
        resultado = exportar_libro(ruta, SALIDA, lotes)
    except Exception as exc:
        return jsonify({"error": f"No se pudo exportar: {exc}"}), 500

    for x in resultado.get("registros", []):
        bd.registrar_evento(
            "exportacion",
            f"{x['facturas']} factura(s) · G {x['valor_lote']:,.2f} · "
            f"K {x['valor_aprobado']:,.2f}",
            lote=x["hoja"], registro=x["registro"],
        )

    # Relativa: la aplicacion puede estar servida en una subruta
    resultado["url"] = "api/descargar"
    return jsonify(resultado)


@app.get("/api/descargar")
def descargar():
    if not SALIDA.exists():
        return jsonify({"error": "Todavia no se ha exportado nada"}), 404
    return send_from_directory(DATA, SALIDA.name, as_attachment=True)


bd.inicializar()
_MIGRACION = bd.migrar_desde_json(TRABAJO)

if __name__ == "__main__":
    if _MIGRACION.get("migrado"):
        print(f"Migrados {_MIGRACION['lotes']} lote(s) de trabajo.json a la base"
              f" (respaldo: {_MIGRACION['respaldo']})")
    anfitrion = os.environ.get("CHECKLIST_HOST", "127.0.0.1")
    puerto = int(os.environ.get("CHECKLIST_PUERTO", "5000"))
    recargar = os.environ.get("CHECKLIST_RECARGAR", "1") == "1"

    print(f"Base de datos: {bd.RUTA_BD}")
    print(f"Interfaz disponible en http://{anfitrion}:{puerto}")
    # use_reloader solo sirve en desarrollo; en el servidor va gunicorn
    app.run(host=anfitrion, port=puerto, debug=False, use_reloader=recargar)
