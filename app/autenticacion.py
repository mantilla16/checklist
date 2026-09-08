"""Usuarios y sesion de la aplicacion.

La contrasena no se guarda en ninguna parte: se guarda su hash (scrypt, el
que trae Werkzeug con Flask, sin dependencias nuevas).

Los intentos fallidos se cuentan en la base y no en memoria, porque gunicorn
levanta varios trabajadores y cada uno tendria su propia cuenta: quien
probara contrasenas conseguiria tres veces mas intentos.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

import bd

CLAVE_MINIMA = 8
INTENTOS_MAXIMOS = 5
BLOQUEO_MINUTOS = 5

# Hash de descarte: se compara contra el aunque el usuario no exista, para que
# responder "no existe" y "clave mala" tarde lo mismo y no se pueda averiguar
# quien tiene cuenta midiendo el tiempo de respuesta.
_DESCARTE = generate_password_hash("x" * 24)

_FORMATO = "%Y-%m-%d %H:%M:%S"


def clave_de_sesion() -> str:
    """Secreto con el que se firman las cookies de sesion.

    Tiene que sobrevivir a los reinicios y ser identico en los tres
    trabajadores de gunicorn. Si se generara al arrancar, cada reinicio
    cerraria todas las sesiones y cada trabajador firmaria distinto.
    """
    indicado = os.environ.get("CHECKLIST_SECRETO")
    if indicado:
        return indicado

    ruta = bd.CARPETA_DATOS / "clave-sesion"
    if not ruta.exists():
        bd.CARPETA_DATOS.mkdir(parents=True, exist_ok=True)
        ruta.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            ruta.chmod(0o600)          # en Windows no tiene efecto
        except OSError:
            pass
    return ruta.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# Alta y mantenimiento
# --------------------------------------------------------------------------- #

def normalizar(usuario: str) -> str:
    return (usuario or "").strip().lower()


def crear(usuario: str, clave: str, nombre: str = "") -> dict:
    usuario = normalizar(usuario)
    if not usuario:
        raise ValueError("El usuario no puede estar vacio.")
    if len(clave or "") < CLAVE_MINIMA:
        raise ValueError(f"La contrasena debe tener al menos {CLAVE_MINIMA} caracteres.")

    with bd.conectar() as conexion:
        existe = conexion.execute(
            "SELECT 1 FROM usuarios WHERE usuario = ?", (usuario,)).fetchone()
        if existe:
            raise ValueError(f"El usuario '{usuario}' ya existe.")
        conexion.execute(
            "INSERT INTO usuarios (usuario, nombre, clave_hash) VALUES (?, ?, ?)",
            (usuario, (nombre or "").strip(), generate_password_hash(clave)),
        )
    bd.registrar_evento("usuario-creado", usuario)
    return {"usuario": usuario, "nombre": nombre}


def cambiar_clave(usuario: str, clave: str) -> None:
    usuario = normalizar(usuario)
    if len(clave or "") < CLAVE_MINIMA:
        raise ValueError(f"La contrasena debe tener al menos {CLAVE_MINIMA} caracteres.")
    with bd.conectar() as conexion:
        cambios = conexion.execute(
            "UPDATE usuarios SET clave_hash = ?, intentos = 0, bloqueado_hasta = NULL "
            "WHERE usuario = ?", (generate_password_hash(clave), usuario)).rowcount
    if not cambios:
        raise ValueError(f"No existe el usuario '{usuario}'.")
    bd.registrar_evento("clave-cambiada", usuario)


def activar(usuario: str, activo: bool = True) -> None:
    usuario = normalizar(usuario)
    with bd.conectar() as conexion:
        cambios = conexion.execute(
            "UPDATE usuarios SET activo = ?, intentos = 0, bloqueado_hasta = NULL "
            "WHERE usuario = ?", (1 if activo else 0, usuario)).rowcount
    if not cambios:
        raise ValueError(f"No existe el usuario '{usuario}'.")
    bd.registrar_evento("usuario-activado" if activo else "usuario-desactivado", usuario)


def listar() -> list[dict]:
    with bd.conectar() as conexion:
        filas = conexion.execute(
            "SELECT usuario, nombre, activo, intentos, bloqueado_hasta, creado, "
            "ultimo_acceso FROM usuarios ORDER BY usuario").fetchall()
    return [dict(f) for f in filas]


def hay_usuarios() -> bool:
    with bd.conectar() as conexion:
        return conexion.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is not None


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #

def _bloqueo_vigente(fila) -> int:
    """Minutos que faltan del bloqueo, 0 si no hay bloqueo activo."""
    hasta = fila["bloqueado_hasta"]
    if not hasta:
        return 0
    try:
        momento = datetime.strptime(hasta, _FORMATO)
    except ValueError:
        return 0
    falta = (momento - datetime.now()).total_seconds()
    return max(0, int(falta // 60) + 1) if falta > 0 else 0


def verificar(usuario: str, clave: str) -> tuple[dict | None, str]:
    """Devuelve (usuario, "") si entra, o (None, motivo) si no.

    El motivo que se muestra es el mismo para usuario inexistente y para clave
    mala: decir cual de los dos falla le regala media respuesta a quien esta
    probando contrasenas.

    El evento se registra despues de cerrar el bloque de la base y no dentro:
    `registrar_evento` abre su propia conexion, y hacerlo con una transaccion
    de escritura abierta aqui bloquea la base contra si misma.
    """
    usuario = normalizar(usuario)
    generico = "Usuario o contrasena incorrectos."
    persona: dict | None = None
    motivo = generico
    evento = ("", "")

    with bd.conectar() as conexion:
        fila = conexion.execute(
            "SELECT * FROM usuarios WHERE usuario = ?", (usuario,)).fetchone()

        if fila is None:
            # Se compara igual contra un hash de descarte para que responder
            # "no existe" tarde lo mismo que "clave mala".
            check_password_hash(_DESCARTE, clave or "")
            evento = ("entrada-fallida", f"{usuario or '(vacio)'}: no existe")

        elif not fila["activo"]:
            motivo = "Esta cuenta esta desactivada."
            evento = ("entrada-fallida", f"{usuario}: desactivado")

        elif (faltan := _bloqueo_vigente(fila)):
            motivo = (f"Demasiados intentos fallidos. Vuelve a intentar en "
                      f"{faltan} minuto(s).")
            evento = ("entrada-bloqueada", f"{usuario}: faltan {faltan} min")

        elif not check_password_hash(fila["clave_hash"], clave or ""):
            intentos = fila["intentos"] + 1
            if intentos >= INTENTOS_MAXIMOS:
                hasta = (datetime.now()
                         + timedelta(minutes=BLOQUEO_MINUTOS)).strftime(_FORMATO)
                conexion.execute(
                    "UPDATE usuarios SET intentos = 0, bloqueado_hasta = ? WHERE id = ?",
                    (hasta, fila["id"]))
                motivo = (f"Demasiados intentos fallidos. La cuenta queda "
                          f"bloqueada {BLOQUEO_MINUTOS} minutos.")
                evento = ("entrada-fallida", f"{usuario}: bloqueado {BLOQUEO_MINUTOS} min")
            else:
                conexion.execute("UPDATE usuarios SET intentos = ? WHERE id = ?",
                                 (intentos, fila["id"]))
                evento = ("entrada-fallida",
                          f"{usuario}: intento {intentos} de {INTENTOS_MAXIMOS}")

        else:
            conexion.execute(
                "UPDATE usuarios SET intentos = 0, bloqueado_hasta = NULL, "
                "ultimo_acceso = datetime('now', 'localtime') WHERE id = ?",
                (fila["id"],))
            persona = {"usuario": fila["usuario"], "nombre": fila["nombre"]}
            motivo = ""
            evento = ("entrada", usuario)

    if evento[0]:
        bd.registrar_evento(evento[0], evento[1])
    return persona, motivo
