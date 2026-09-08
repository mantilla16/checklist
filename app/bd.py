"""Almacenamiento en SQLite del papel de trabajo.

Un solo archivo (`app/data/validacion.db`) que se puede copiar para respaldar.
Reemplaza al `trabajo.json`, que se sobrescribía completo en cada guardado y
podía corromperse si el proceso se cortaba a mitad de la escritura.

Qué gana el proyecto:

- **Transaccional**: o se guarda todo o no se guarda nada; dos pestañas ya no
  se pisan entre sí.
- **Historial**: la tabla `eventos` deja rastro de qué se validó y cuándo.
- **Sin duplicados**: los PDF se identifican por su hash SHA-256, así que
  cargar el mismo archivo dos veces no lo guarda dos veces.
- **Consultable**: se puede preguntar por los registros pendientes o con
  diferencia sin abrir cada lote.

La estructura que ve la interfaz no cambia: `leer_trabajo()` devuelve el mismo
diccionario que antes venía del JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
CARPETA_DATOS = Path(os.environ.get("CHECKLIST_DATOS") or BASE / "data")
RUTA_BD = CARPETA_DATOS / "validacion.db"

# Listas de análisis que la interfaz guarda dentro de cada registro. Se
# almacenan como JSON porque son la respuesta cruda de cada validación.
LISTAS_REGISTRO = ("documentos", "facturas", "radian", "entradas", "ordenes",
                   "documentos_p")

ESQUEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS lotes (
    id              INTEGER PRIMARY KEY,
    hoja            TEXT    NOT NULL UNIQUE,
    tipo_pago       TEXT,
    nombre_pago     TEXT,
    cuenta          TEXT,
    valor_total     REAL,
    num_registros   INTEGER,
    nit_cliente     TEXT,
    fecha_creacion  TEXT,
    fecha_aplicacion TEXT,
    orden           INTEGER DEFAULT 0,
    creado_en       TEXT DEFAULT (datetime('now', 'localtime')),
    actualizado_en  TEXT
);

CREATE TABLE IF NOT EXISTS registros (
    id            INTEGER PRIMARY KEY,
    lote_id       INTEGER NOT NULL REFERENCES lotes(id) ON DELETE CASCADE,
    orden         INTEGER NOT NULL,
    nro           TEXT,
    estado        TEXT,
    titular       TEXT,
    documento     TEXT,
    cuenta        TEXT,
    valor         REAL,
    correo        TEXT,
    numero_egreso TEXT,
    total_egreso  REAL,
    listas        TEXT,
    veredicto     TEXT,
    UNIQUE (lote_id, orden)
);

CREATE TABLE IF NOT EXISTS renglones (
    id                INTEGER PRIMARY KEY,
    registro_id       INTEGER NOT NULL REFERENCES registros(id) ON DELETE CASCADE,
    orden             INTEGER NOT NULL,
    factura           TEXT,
    valor_k           REAL,
    valor_egreso      REAL,
    no_factura        TEXT,
    nit_tercero       TEXT,
    validacion        INTEGER,
    valor_pagar       REAL,
    radian            TEXT,
    orden_compra      TEXT,
    egreso_g          TEXT,
    compras_p         TEXT,
    entrada_e         TEXT,
    cantidad_factura  REAL,
    cantidad_recibida REAL,
    aprobado_por      TEXT,
    resto             TEXT,
    UNIQUE (registro_id, orden)
);

CREATE TABLE IF NOT EXISTS documentos (
    id         INTEGER PRIMARY KEY,
    hash       TEXT NOT NULL UNIQUE,
    nombre     TEXT,
    archivo    TEXT,
    categoria  TEXT,
    tamano     INTEGER,
    cargado_en TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS eventos (
    id       INTEGER PRIMARY KEY,
    cuando   TEXT DEFAULT (datetime('now', 'localtime')),
    tipo     TEXT NOT NULL,
    detalle  TEXT,
    lote     TEXT,
    registro TEXT
);

CREATE TABLE IF NOT EXISTS usuarios (
    id              INTEGER PRIMARY KEY,
    usuario         TEXT NOT NULL UNIQUE,
    nombre          TEXT NOT NULL DEFAULT '',
    clave_hash      TEXT NOT NULL,
    activo          INTEGER NOT NULL DEFAULT 1,
    intentos        INTEGER NOT NULL DEFAULT 0,
    bloqueado_hasta TEXT,
    creado          TEXT DEFAULT (datetime('now', 'localtime')),
    ultimo_acceso   TEXT
);

CREATE INDEX IF NOT EXISTS idx_registros_lote ON registros(lote_id);
CREATE INDEX IF NOT EXISTS idx_renglones_registro ON renglones(registro_id);
CREATE INDEX IF NOT EXISTS idx_eventos_cuando ON eventos(cuando);
"""

# Campos del renglon que tienen columna propia; el resto va en `resto`
COLUMNAS_RENGLON = {
    "factura": "factura", "valor": "valor_k", "valor_egreso": "valor_egreso",
    "no_factura": "no_factura", "nit_tercero": "nit_tercero",
    "valor_pagar": "valor_pagar", "radian": "radian",
    "orden_compra": "orden_compra", "egreso_g": "egreso_g",
    "compras_p": "compras_p", "entrada_e": "entrada_e",
    "cantidad_factura": "cantidad_factura",
    "cantidad_recibida": "cantidad_recibida", "aprobado_por": "aprobado_por",
}


def conectar() -> sqlite3.Connection:
    RUTA_BD.parent.mkdir(parents=True, exist_ok=True)
    conexion = sqlite3.connect(RUTA_BD, timeout=15)
    conexion.row_factory = sqlite3.Row
    conexion.execute("PRAGMA foreign_keys = ON")
    return conexion


def inicializar() -> None:
    with conectar() as conexion:
        conexion.executescript(ESQUEMA)


# --------------------------------------------------------------------------- #
# Documentos: se identifican por su contenido
# --------------------------------------------------------------------------- #

def hash_archivo(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def documento_por_hash(huella: str) -> dict | None:
    with conectar() as conexion:
        fila = conexion.execute(
            "SELECT * FROM documentos WHERE hash = ?", (huella,)).fetchone()
    return dict(fila) if fila else None


def registrar_documento(huella: str, nombre: str, archivo: str,
                        categoria: str, tamano: int) -> dict:
    """Guarda la ficha del documento. Si ya estaba, devuelve la existente."""
    existente = documento_por_hash(huella)
    if existente:
        return existente
    with conectar() as conexion:
        conexion.execute(
            "INSERT INTO documentos (hash, nombre, archivo, categoria, tamano) "
            "VALUES (?, ?, ?, ?, ?)",
            (huella, nombre, archivo, categoria, tamano),
        )
    return documento_por_hash(huella)


# --------------------------------------------------------------------------- #
# Bitacora
# --------------------------------------------------------------------------- #

def registrar_evento(tipo: str, detalle: str = "", lote: str = "",
                     registro: str = "") -> None:
    with conectar() as conexion:
        conexion.execute(
            "INSERT INTO eventos (tipo, detalle, lote, registro) VALUES (?, ?, ?, ?)",
            (tipo, detalle, lote, str(registro or "")),
        )


def leer_eventos(limite: int = 200) -> list[dict]:
    with conectar() as conexion:
        filas = conexion.execute(
            "SELECT cuando, tipo, detalle, lote, registro FROM eventos "
            "ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
    return [dict(f) for f in filas]


# --------------------------------------------------------------------------- #
# Trabajo: guardar y leer el estado completo
# --------------------------------------------------------------------------- #

def _json(valor) -> str | None:
    return json.dumps(valor, ensure_ascii=False) if valor else None


def _desde_json(texto):
    if not texto:
        return None
    try:
        return json.loads(texto)
    except (TypeError, json.JSONDecodeError):
        return None


def _aliviar(listas: dict) -> dict:
    """Quita los textos completos que no hace falta persistir.

    El texto de la entrada solo se usa mientras se valida; el de los correos sí
    se conserva porque la segunda opinión con IA lo necesita.
    """
    copia = {}
    for clave, lista in listas.items():
        nueva = []
        for elemento in (lista or []):
            if isinstance(elemento, dict) and isinstance(elemento.get("entrada"), dict):
                elemento = {**elemento,
                            "entrada": {k: v for k, v in elemento["entrada"].items()
                                        if k != "texto"}}
            nueva.append(elemento)
        copia[clave] = nueva
    return copia


def guardar_trabajo(trabajo: dict) -> dict:
    """Reemplaza el estado completo dentro de una transaccion."""
    lotes = trabajo.get("lotes") or []
    with conectar() as conexion:
        conexion.execute("BEGIN")
        hojas = [str(l.get("hoja") or f"Lote {i + 1}") for i, l in enumerate(lotes)]

        # Los lotes que ya no estan en la interfaz se eliminan
        if hojas:
            marcas = ",".join("?" * len(hojas))
            conexion.execute(f"DELETE FROM lotes WHERE hoja NOT IN ({marcas})", hojas)
        else:
            conexion.execute("DELETE FROM lotes")

        for posicion, lote in enumerate(lotes):
            info = lote.get("info") or {}
            hoja = hojas[posicion]
            conexion.execute(
                """INSERT INTO lotes (hoja, tipo_pago, nombre_pago, cuenta,
                       valor_total, num_registros, nit_cliente, fecha_creacion,
                       fecha_aplicacion, orden, actualizado_en)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
                   ON CONFLICT(hoja) DO UPDATE SET
                       tipo_pago = excluded.tipo_pago,
                       nombre_pago = excluded.nombre_pago,
                       cuenta = excluded.cuenta,
                       valor_total = excluded.valor_total,
                       num_registros = excluded.num_registros,
                       nit_cliente = excluded.nit_cliente,
                       fecha_creacion = excluded.fecha_creacion,
                       fecha_aplicacion = excluded.fecha_aplicacion,
                       orden = excluded.orden,
                       actualizado_en = datetime('now','localtime')""",
                (hoja, info.get("tipo_pago"), info.get("nombre_pago"),
                 info.get("cuenta"), info.get("valor_total"),
                 info.get("num_registros"), info.get("nit_cliente"),
                 info.get("fecha_creacion"), info.get("fecha_aplicacion"), posicion),
            )
            lote_id = conexion.execute(
                "SELECT id FROM lotes WHERE hoja = ?", (hoja,)).fetchone()["id"]

            # Los registros se reescriben completos: los renglones caen con ellos
            conexion.execute("DELETE FROM registros WHERE lote_id = ?", (lote_id,))

            for indice, registro in enumerate(lote.get("registros") or []):
                listas = _aliviar({c: registro.get(c) for c in LISTAS_REGISTRO})
                cursor = conexion.execute(
                    """INSERT INTO registros (lote_id, orden, nro, estado, titular,
                           documento, cuenta, valor, correo, numero_egreso,
                           total_egreso, listas, veredicto)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (lote_id, indice, str(registro.get("nro") or ""),
                     registro.get("estado"), registro.get("titular"),
                     registro.get("documento"), registro.get("cuenta"),
                     registro.get("valor"), registro.get("correo"),
                     registro.get("numero_egreso"), registro.get("total_egreso"),
                     _json(listas), _json(registro.get("veredicto"))),
                )
                registro_id = cursor.lastrowid

                for fila, renglon in enumerate(registro.get("renglones") or []):
                    resto = {k: v for k, v in renglon.items()
                             if k not in COLUMNAS_RENGLON and k != "validacion"}
                    conexion.execute(
                        """INSERT INTO renglones (registro_id, orden, factura,
                               valor_k, valor_egreso, no_factura, nit_tercero,
                               validacion, valor_pagar, radian, orden_compra,
                               egreso_g, compras_p, entrada_e, cantidad_factura,
                               cantidad_recibida, aprobado_por, resto)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (registro_id, fila, renglon.get("factura"),
                         renglon.get("valor"), renglon.get("valor_egreso"),
                         renglon.get("no_factura"), renglon.get("nit_tercero"),
                         None if renglon.get("validacion") is None
                         else int(bool(renglon.get("validacion"))),
                         renglon.get("valor_pagar"), renglon.get("radian"),
                         renglon.get("orden_compra"), renglon.get("egreso_g"),
                         renglon.get("compras_p"), renglon.get("entrada_e"),
                         renglon.get("cantidad_factura"),
                         renglon.get("cantidad_recibida"),
                         renglon.get("aprobado_por"), _json(resto)),
                    )

    return {"ok": True, "lotes": len(lotes)}


def leer_trabajo() -> dict:
    """Devuelve el estado con la misma forma que espera la interfaz."""
    with conectar() as conexion:
        lotes = []
        for lote in conexion.execute(
                "SELECT * FROM lotes ORDER BY orden, id").fetchall():
            registros = []
            for registro in conexion.execute(
                    "SELECT * FROM registros WHERE lote_id = ? ORDER BY orden",
                    (lote["id"],)).fetchall():
                listas = _desde_json(registro["listas"]) or {}
                datos = {
                    "nro": _numero_o_texto(registro["nro"]),
                    "estado": registro["estado"],
                    "titular": registro["titular"],
                    "documento": registro["documento"],
                    "cuenta": registro["cuenta"],
                    "valor": registro["valor"],
                    "correo": registro["correo"] or "",
                    "veredicto": _desde_json(registro["veredicto"]),
                    "docActivo": -1,
                    "renglones": [],
                }
                if registro["numero_egreso"]:
                    datos["numero_egreso"] = registro["numero_egreso"]
                if registro["total_egreso"] is not None:
                    datos["total_egreso"] = registro["total_egreso"]
                for clave in LISTAS_REGISTRO:
                    datos[clave] = listas.get(clave) or []

                for renglon in conexion.execute(
                        "SELECT * FROM renglones WHERE registro_id = ? ORDER BY orden",
                        (registro["id"],)).fetchall():
                    fila = _desde_json(renglon["resto"]) or {}
                    for origen, columna in COLUMNAS_RENGLON.items():
                        valor = renglon[columna]
                        if valor is not None:
                            fila[origen] = valor
                    if renglon["validacion"] is not None:
                        fila["validacion"] = bool(renglon["validacion"])
                    datos["renglones"].append(fila)

                registros.append(datos)

            lotes.append({
                "hoja": lote["hoja"],
                "info": {
                    "tipo_pago": lote["tipo_pago"],
                    "nombre_pago": lote["nombre_pago"],
                    "cuenta": lote["cuenta"],
                    "valor_total": lote["valor_total"],
                    "num_registros": lote["num_registros"],
                    "nit_cliente": lote["nit_cliente"],
                    "fecha_creacion": lote["fecha_creacion"],
                    "fecha_aplicacion": lote["fecha_aplicacion"],
                },
                "registros": registros,
            })

    return {"lotes": lotes}


def _numero_o_texto(valor):
    """El Nro. de registro se guarda como texto pero la interfaz lo usa como numero."""
    if valor in (None, ""):
        return None
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return valor


def borrar_trabajo() -> None:
    with conectar() as conexion:
        conexion.execute("DELETE FROM lotes")
    registrar_evento("borrado", "se descartaron todos los lotes capturados")


# --------------------------------------------------------------------------- #
# Migracion desde el JSON
# --------------------------------------------------------------------------- #

def migrar_desde_json(ruta: Path) -> dict:
    """Pasa un `trabajo.json` existente a la base. Deja el JSON como respaldo."""
    if not ruta.exists():
        return {"migrado": False, "motivo": "no hay trabajo.json"}

    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"migrado": False, "motivo": f"no se pudo leer: {exc}"}

    if not (datos.get("lotes") or []):
        return {"migrado": False, "motivo": "el JSON no tenia lotes"}

    with conectar() as conexion:
        hay = conexion.execute("SELECT COUNT(*) AS n FROM lotes").fetchone()["n"]
    if hay:
        return {"migrado": False, "motivo": "la base ya tiene lotes"}

    resultado = guardar_trabajo(datos)
    respaldo = ruta.with_suffix(".json.migrado")
    ruta.replace(respaldo)
    registrar_evento("migracion",
                     f"{resultado['lotes']} lote(s) desde {ruta.name}")
    return {"migrado": True, "lotes": resultado["lotes"], "respaldo": respaldo.name}


# --------------------------------------------------------------------------- #
# Consultas
# --------------------------------------------------------------------------- #

def resumen() -> dict:
    """Estado general: cuantos registros cuadran y cuantos quedan pendientes."""
    with conectar() as conexion:
        filas = conexion.execute("""
            SELECT l.hoja,
                   r.nro,
                   r.titular,
                   r.valor,
                   COALESCE(SUM(g.valor_k), 0)     AS suma_k,
                   COALESCE(SUM(g.valor_pagar), 0) AS suma_q,
                   COUNT(g.id)                     AS facturas
              FROM lotes l
              JOIN registros r ON r.lote_id = l.id
              LEFT JOIN renglones g ON g.registro_id = r.id
             GROUP BY r.id
             ORDER BY l.orden, r.orden
        """).fetchall()

    registros = []
    for f in filas:
        valor = f["valor"] or 0
        diferencia = valor - (f["suma_k"] or 0)
        registros.append({
            "hoja": f["hoja"], "nro": f["nro"], "titular": f["titular"],
            "valor": valor, "suma_k": f["suma_k"], "suma_q": f["suma_q"],
            "facturas": f["facturas"], "diferencia_l": diferencia,
            "diferencia_r": valor - (f["suma_q"] or 0),
            "estado": ("sin validar" if not f["suma_k"]
                       else "cuadra" if abs(diferencia) < 0.01 else "descuadre"),
        })

    return {
        "registros": registros,
        "totales": {
            "registros": len(registros),
            "cuadran": sum(1 for r in registros if r["estado"] == "cuadra"),
            "descuadres": sum(1 for r in registros if r["estado"] == "descuadre"),
            "sin_validar": sum(1 for r in registros if r["estado"] == "sin validar"),
        },
    }
