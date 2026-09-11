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

import re

RE_USUARIO = re.compile(r"[a-z0-9._-]{3,32}")
CLAVES_PROHIBIDAS = {
    "12345678", "123456789", "contrasena", "password", "qwertyui", "11111111",
    "checklist", "administrador", "proveedores",
}

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


def revisar_clave(clave: str, usuario: str = "") -> None:
    """Reglas de la contrasena. Lanza ValueError con el motivo concreto.

    No se piden simbolos ni mayusculas: obligan a apuntarla en un papel y no
    aportan tanto como la longitud. Si se prohibe lo evidente, que es repetir
    el nombre de usuario o una de las cuatro de siempre.
    """
    clave = clave or ""
    if len(clave) < CLAVE_MINIMA:
        raise ValueError(f"La contrasena debe tener al menos {CLAVE_MINIMA} caracteres.")
    if usuario and normalizar(usuario) in clave.lower():
        raise ValueError("La contrasena no puede contener el nombre de usuario.")
    if clave.lower() in CLAVES_PROHIBIDAS:
        raise ValueError("Esa contrasena es de las mas usadas; elige otra.")


def crear(usuario: str, clave: str, nombre: str = "", admin: bool | None = None) -> dict:
    """Crea una cuenta. El primer usuario es administrador por necesidad:
    si no, no habria quien pudiera crear a los demas."""
    usuario = normalizar(usuario)
    if not usuario:
        raise ValueError("El usuario no puede estar vacio.")
    if not RE_USUARIO.fullmatch(usuario):
        raise ValueError("El usuario solo admite letras, numeros, punto, guion y "
                         "guion bajo, entre 3 y 32 caracteres.")
    revisar_clave(clave, usuario)

    with bd.conectar() as conexion:
        existe = conexion.execute(
            "SELECT 1 FROM usuarios WHERE usuario = ?", (usuario,)).fetchone()
        if existe:
            raise ValueError(f"El usuario '{usuario}' ya existe.")
        primero = conexion.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone() is None
        es_admin = primero if admin is None else bool(admin)
        conexion.execute(
            "INSERT INTO usuarios (usuario, nombre, clave_hash, admin) "
            "VALUES (?, ?, ?, ?)",
            (usuario, (nombre or "").strip(), generate_password_hash(clave),
             1 if es_admin else 0),
        )
    bd.registrar_evento("usuario-creado",
                        f"{usuario}{' (administrador)' if es_admin else ''}")
    return {"usuario": usuario, "nombre": nombre, "admin": es_admin}


def cambiar_clave(usuario: str, clave: str) -> None:
    usuario = normalizar(usuario)
    revisar_clave(clave, usuario)
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
        fila = _cuenta(conexion, usuario)
        # Desactivar al ultimo administrador deja el sistema sin quien pueda
        # volver a activarlo desde la aplicacion
        if not activo and _otras_cuentas_activas(conexion, usuario) == 0:
            raise ValueError("Es la unica cuenta activa; nadie podria entrar.")
        if not activo and fila["admin"] and _otros_administradores(conexion, usuario) == 0:
            raise ValueError("Es el unico administrador activo. Nombra otro antes "
                             "de desactivarlo.")
        conexion.execute(
            "UPDATE usuarios SET activo = ?, intentos = 0, bloqueado_hasta = NULL "
            "WHERE usuario = ?", (1 if activo else 0, usuario))
    bd.registrar_evento("usuario-activado" if activo else "usuario-desactivado", usuario)


def _cuenta(conexion, usuario: str):
    fila = conexion.execute("SELECT * FROM usuarios WHERE usuario = ?",
                            (usuario,)).fetchone()
    if fila is None:
        raise ValueError(f"No existe el usuario '{usuario}'.")
    return fila


def _otros_administradores(conexion, usuario: str) -> int:
    """Cuantos administradores activos quedarian sin contar a este."""
    return conexion.execute(
        "SELECT COUNT(*) FROM usuarios WHERE admin = 1 AND activo = 1 "
        "AND usuario <> ?", (usuario,)).fetchone()[0]


def _otras_cuentas_activas(conexion, usuario: str) -> int:
    return conexion.execute(
        "SELECT COUNT(*) FROM usuarios WHERE activo = 1 AND usuario <> ?",
        (usuario,)).fetchone()[0]


def es_admin(usuario: str) -> bool:
    with bd.conectar() as conexion:
        fila = conexion.execute(
            "SELECT admin, activo FROM usuarios WHERE usuario = ?",
            (normalizar(usuario),)).fetchone()
    return bool(fila and fila["admin"] and fila["activo"])


def cambiar_admin(usuario: str, admin: bool) -> None:
    """Da o quita el permiso de administrar usuarios.

    No se puede quitar al ultimo administrador activo: nadie podria volver a
    darselo a nadie y habria que entrar por la linea de comandos del servidor.
    """
    usuario = normalizar(usuario)
    with bd.conectar() as conexion:
        _cuenta(conexion, usuario)
        if not admin and _otros_administradores(conexion, usuario) == 0:
            raise ValueError("Es el unico administrador activo. Nombra otro antes "
                             "de quitarle el permiso.")
        conexion.execute("UPDATE usuarios SET admin = ? WHERE usuario = ?",
                         (1 if admin else 0, usuario))
    bd.registrar_evento("admin-concedido" if admin else "admin-retirado", usuario)


def desbloquear(usuario: str) -> None:
    """Levanta el bloqueo por intentos fallidos sin cambiar la contrasena."""
    usuario = normalizar(usuario)
    with bd.conectar() as conexion:
        _cuenta(conexion, usuario)
        conexion.execute(
            "UPDATE usuarios SET intentos = 0, bloqueado_hasta = NULL "
            "WHERE usuario = ?", (usuario,))
    bd.registrar_evento("usuario-desbloqueado", usuario)


def borrar(usuario: str) -> None:
    """Elimina la cuenta. Los eventos que dejo se conservan."""
    usuario = normalizar(usuario)
    with bd.conectar() as conexion:
        fila = _cuenta(conexion, usuario)
        if _otras_cuentas_activas(conexion, usuario) == 0:
            raise ValueError("Es la unica cuenta activa; nadie podria entrar.")
        if fila["admin"] and _otros_administradores(conexion, usuario) == 0:
            raise ValueError("Es el unico administrador activo; no se puede borrar.")
        conexion.execute("DELETE FROM usuarios WHERE usuario = ?", (usuario,))
    bd.registrar_evento("usuario-borrado", usuario)


def cambiar_clave_propia(usuario: str, actual: str, nueva: str) -> None:
    """Cambio de contrasena por el propio dueno, comprobando la actual.

    Se exige la actual porque una sesion abierta y desatendida no debe bastar
    para quedarse con la cuenta.
    """
    usuario = normalizar(usuario)
    with bd.conectar() as conexion:
        fila = _cuenta(conexion, usuario)
        if not check_password_hash(fila["clave_hash"], actual or ""):
            raise ValueError("La contrasena actual no es correcta.")
    revisar_clave(nueva, usuario)
    cambiar_clave(usuario, nueva)


def listar() -> list[dict]:
    with bd.conectar() as conexion:
        filas = conexion.execute(
            "SELECT usuario, nombre, activo, admin, intentos, bloqueado_hasta, "
            "creado, ultimo_acceso FROM usuarios ORDER BY usuario").fetchall()
    cuentas = []
    for fila in filas:
        cuenta = dict(fila)
        cuenta["activo"] = bool(cuenta["activo"])
        cuenta["admin"] = bool(cuenta["admin"])
        cuenta["bloqueado"] = _bloqueo_vigente(fila) > 0
        cuentas.append(cuenta)
    return cuentas


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
