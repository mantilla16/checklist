"""Relacion de mercancia recibida (F-CO-110): la columna X.

Es el documento con el que almacen deja constancia de lo que entro. Se valida
contra lo que ya esta cargado en pasos anteriores, sin volver a subir nada:

  1. el numero de factura, contra la factura electronica;
  2. la cantidad recibida, contra la entrada de inventario (columna W);
  3. las dos firmas del pie, auxiliar y supervisor de almacen.

Las firmas no son texto: son imagenes pegadas encima de su rotulo. Se dan por
presentes cuando hay una imagen justo sobre la etiqueta y solapada con ella.
El logotipo de la cabecera no cuenta, porque esta lejos de cualquier rotulo.
"""
from __future__ import annotations

import re

from lote_banco import IMAGENES, cajas, en_lineas, normalizar

# Etiqueta -> campo. El valor esta a la DERECHA, en la misma linea.
ETIQUETAS = {
    "fecha de recibo": "fecha_recibo",
    "proveedor": "proveedor",
    "factura": "factura",
    "orden de compra": "orden_compra",
    "numero de ingreso": "numero_ingreso",
    "nacional": "nacional",
    "importado": "importado",
}

# Encabezados de la tabla de mercancia
COLUMNAS = (
    ("codigo", "codigo"), ("siigo", "codigo"),
    ("descripcion", "descripcion"),
    ("um", "um"),
    ("cantidad", "cantidad"), ("recibida", "cantidad"),
    ("observaciones", "observaciones"),
)

FIRMAS = {"auxiliar de almacen": "auxiliar", "supervisor de almacen": "supervisor"}

# Una firma esta a lo sumo esta distancia por encima de su rotulo
ALTURA_FIRMA = 70
# Una imagen mas ancha que esto es la pagina escaneada entera, no una firma
ANCHO_MAXIMO_FIRMA = 0.5


def _cantidad(bruto: str) -> float | None:
    hallado = re.search(r"\d[\d.,]*", (bruto or "").replace(" ", ""))
    if not hallado:
        return None
    crudo = hallado.group(0)
    if "," in crudo and "." in crudo:
        crudo = crudo.replace(".", "").replace(",", ".")
    elif "," in crudo:
        crudo = crudo.replace(",", ".")
    try:
        return float(crudo)
    except ValueError:
        return None


def _limpiar_numero(texto) -> str:
    """Deja solo letras y digitos, sin ceros a la izquierda."""
    return re.sub(r"[^0-9A-Za-z]", "", str(texto or "")).upper().lstrip("0")


# --------------------------------------------------------------------------- #
# Encabezado: etiqueta a la izquierda, valor a la derecha
# --------------------------------------------------------------------------- #

def _etiquetas_de(linea: dict) -> list[dict]:
    palabras = linea["palabras"]
    encontradas: list[dict] = []
    usadas: set[int] = set()
    for inicio in range(len(palabras)):
        if inicio in usadas:
            continue
        for fin in range(min(inicio + 4, len(palabras)), inicio, -1):
            tramo = palabras[inicio:fin]
            clave = normalizar(" ".join(p["texto"] for p in tramo))
            if clave in ETIQUETAS:
                encontradas.append({"campo": ETIQUETAS[clave], "clave": clave,
                                    "x0": tramo[0]["x0"], "x1": tramo[-1]["x1"]})
                usadas.update(range(inicio, fin))
                break
    return sorted(encontradas, key=lambda e: e["x0"])


def _encabezado(lineas: list[dict]) -> dict:
    """Cada etiqueta se queda con lo que hay hasta la etiqueta siguiente.

    En una misma linea caben cuatro campos ("NACIONAL X ORDEN DE COMPRA
    20260331 IMPORTADO NUMERO DE INGRESO"), asi que el limite de un valor es
    donde empieza la etiqueta vecina.
    """
    info: dict = {}
    for linea in lineas:
        etiquetas = _etiquetas_de(linea)
        for posicion, etiqueta in enumerate(etiquetas):
            derecha = (etiquetas[posicion + 1]["x0"]
                       if posicion + 1 < len(etiquetas) else float("inf"))
            valor = " ".join(p["texto"] for p in linea["palabras"]
                             if p["x0"] >= etiqueta["x1"] - 1 and p["x1"] <= derecha + 1)
            if etiqueta["campo"] not in info:
                info[etiqueta["campo"]] = valor.strip()
    return info


# --------------------------------------------------------------------------- #
# Tabla de mercancia
# --------------------------------------------------------------------------- #

def _agrupar(palabras: list[dict], holgura: float = 8.0) -> list[list[dict]]:
    grupos: list[list[dict]] = []
    for palabra in sorted(palabras, key=lambda p: p["x0"]):
        for grupo in grupos:
            if palabra["x0"] <= max(p["x1"] for p in grupo) + holgura:
                grupo.append(palabra)
                break
        else:
            grupos.append([palabra])
    return grupos


def _bordes(ruta: str, altura: float) -> list[float]:
    """Bordes verticales del formato a la altura de la primera fila de datos.

    El formato esta enmarcado, asi que sus propias lineas dicen donde empieza y
    acaba cada columna. Es mas fiable que deducirlo del titulo: "DESCRIPCION"
    va centrado sobre una columna ancha y su centro no marca el limite.
    """
    if ruta.lower().endswith(tuple(IMAGENES)):
        return []
    try:
        import pdfplumber
        with pdfplumber.open(ruta) as documento:
            pagina = documento.pages[0]
            bordes = {round(r["x0"], 1) for r in pagina.rects + pagina.lines
                      if abs(r["x1"] - r["x0"]) < 2 and r["top"] <= altura <= r["bottom"]}
    except Exception:
        return []
    return sorted(bordes)


def _columnas_por_bordes(palabras: list[dict], bordes: list[float]) -> list[dict]:
    """Nombra cada hueco entre bordes con el titulo que cae dentro."""
    columnas = []
    for izquierda, derecha in zip(bordes, bordes[1:]):
        dentro = [p["texto"] for p in sorted(palabras, key=lambda p: (p["y0"], p["x0"]))
                  if (p["x0"] + p["x1"]) / 2 > izquierda and (p["x0"] + p["x1"]) / 2 < derecha]
        texto = normalizar(" ".join(dentro))
        campo = next((c for clave, c in COLUMNAS if clave in texto), "")
        columnas.append({"campo": campo or texto or f"col{len(columnas)}",
                         "x0": izquierda, "x1": derecha})
    return columnas


def _columnas(lineas: list[dict]) -> tuple[list[dict], int, int]:
    """Franjas de x de la tabla, con la primera y ultima linea del encabezado."""
    for indice, linea in enumerate(lineas):
        clave = normalizar(linea["texto"])
        if "codigo" in clave and "cantidad" in clave:
            palabras = list(linea["palabras"])
            fin = indice
            for extra in lineas[indice + 1:indice + 4]:
                if any(c in normalizar(extra["texto"])
                       for c in ("descripcion", "siigo", "recibida", "observaciones")):
                    palabras += extra["palabras"]
                    fin += 1
                else:
                    break

            columnas = []
            for grupo in _agrupar(palabras):
                texto = normalizar(" ".join(p["texto"] for p in
                                            sorted(grupo, key=lambda p: (p["y0"], p["x0"]))))
                campo = next((c for clave_col, c in COLUMNAS if clave_col in texto), "")
                columnas.append({"campo": campo or texto,
                                 "x0": min(p["x0"] for p in grupo),
                                 "x1": max(p["x1"] for p in grupo)})
            return sorted(columnas, key=lambda c: c["x0"]), indice, fin
    return [], 0, 0


def _filas(lineas: list[dict], columnas: list[dict], desde: int) -> list[dict]:
    if not columnas:
        return []
    fronteras = [(a["x1"] + b["x0"]) / 2 for a, b in zip(columnas, columnas[1:])]

    def columna_de(palabra):
        centro = (palabra["x0"] + palabra["x1"]) / 2
        for indice, frontera in enumerate(fronteras):
            if centro < frontera:
                return indice
        return len(columnas) - 1

    # Con los bordes reales la palabra cae dentro de su columna sin ambiguedad
    def columna_por_borde(palabra):
        centro = (palabra["x0"] + palabra["x1"]) / 2
        for indice, columna in enumerate(columnas):
            if columna["x0"] <= centro <= columna["x1"]:
                return indice
        return None

    filas = []
    for linea in lineas[desde + 1:]:
        if any(c in normalizar(linea["texto"]) for c in FIRMAS):
            break
        celdas: dict[int, list[str]] = {}
        for palabra in linea["palabras"]:
            indice = columna_por_borde(palabra)
            if indice is None:
                indice = columna_de(palabra)
            celdas.setdefault(indice, []).append(palabra["texto"])
        fila = {columnas[i]["campo"]: " ".join(v) for i, v in celdas.items()}
        if not fila.get("descripcion") and not fila.get("cantidad"):
            continue
        fila["cantidad_num"] = _cantidad(fila.get("cantidad", ""))
        filas.append(fila)
    return filas


# --------------------------------------------------------------------------- #
# Firmas
# --------------------------------------------------------------------------- #

def _tramo_de(linea: dict, etiqueta: str) -> tuple[float, float, float] | None:
    """Posicion de una etiqueta como tramo de palabras consecutivas.

    No vale buscar las palabras sueltas: los dos rotulos van en la misma linea
    y comparten "DE" y "ALMACEN", asi que cada etiqueta se quedaba con el ancho
    de las dos y una sola firma daba por firmadas ambas.
    """
    palabras = linea["palabras"]
    partes = etiqueta.split()
    for inicio in range(len(palabras) - len(partes) + 1):
        tramo = palabras[inicio:inicio + len(partes)]
        if normalizar(" ".join(p["texto"] for p in tramo)) == etiqueta:
            return (min(p["x0"] for p in tramo), max(p["x1"] for p in tramo),
                    min(p["y0"] for p in tramo))
    return None


def _firmas_en(imagenes: list[dict], lineas: list[dict]) -> dict:
    """Marca una firma cuando hay una imagen justo encima de su rotulo."""
    resultado = {campo: None for campo in FIRMAS.values()}
    for linea in lineas:
        for etiqueta, campo in FIRMAS.items():
            tramo = _tramo_de(linea, etiqueta)
            if tramo is None:
                continue
            x0, x1, arriba = tramo
            resultado[campo] = any(
                imagen["x0"] < x1 and imagen["x1"] > x0
                and arriba - ALTURA_FIRMA <= imagen["bottom"] <= arriba + 6
                for imagen in imagenes)
    return resultado


def _firmas(ruta: str, lineas: list[dict]) -> dict:
    """Firmas del pie. None por firma cuando no se puede saber.

    Si el documento llega escaneado la pagina entera es una sola imagen y
    dentro de ella no se distingue una firma de la hoja. Decir OK ahi seria
    mentir, asi que se deja sin determinar.
    """
    vacio = {campo: None for campo in FIRMAS.values()}
    if ruta.lower().endswith(tuple(IMAGENES)):
        return vacio

    try:
        import pdfplumber
        with pdfplumber.open(ruta) as documento:
            pagina = documento.pages[-1]
            ancho = pagina.width
            imagenes = [dict(im) for im in pagina.images
                        if (im["x1"] - im["x0"]) < ancho * ANCHO_MAXIMO_FIRMA]
            escaneada = any((im["x1"] - im["x0"]) >= ancho * ANCHO_MAXIMO_FIRMA
                            for im in pagina.images)
    except Exception:
        return vacio

    return vacio if escaneada else _firmas_en(imagenes, lineas)


# --------------------------------------------------------------------------- #
# Lectura y validacion
# --------------------------------------------------------------------------- #

def analizar_recibido(ruta: str, nombre: str = "") -> dict | None:
    """Devuelve None si el archivo no es una relacion de mercancia recibida."""
    palabras = cajas(ruta)
    if not palabras:
        return None

    lineas = en_lineas(palabras)
    texto = normalizar(" ".join(l["texto"] for l in lineas))
    if "relacion de mercancia recibida" not in texto and "f-co-110" not in texto:
        return None

    info = _encabezado(lineas)
    columnas, inicio, fin = _columnas(lineas)

    # Con los bordes del formato las columnas son exactas; el reparto por
    # cercania al titulo solo se usa si el documento no los trae
    if columnas and fin + 1 < len(lineas):
        bordes = _bordes(ruta, lineas[fin + 1]["y0"] + 1)
        if len(bordes) >= 3:
            # Solo las lineas del encabezado de la tabla: mas arriba esta el
            # titulo "RELACION DE MERCANCIA RECIBIDA", y esa palabra suelta
            # bautizaba como "cantidad recibida" la columna que le quedaba
            # debajo, que es la de cumplimiento.
            titulo = [p for l in lineas[inicio:fin + 1] for p in l["palabras"]]
            columnas = _columnas_por_bordes(titulo, bordes)

    filas = _filas(lineas, columnas, fin)
    cantidades = [f["cantidad_num"] for f in filas if f.get("cantidad_num") is not None]

    return {
        "nombre": nombre,
        "fecha_recibo": info.get("fecha_recibo", ""),
        "proveedor": re.sub(r"\s+", " ", info.get("proveedor", "")).strip(),
        "factura": re.sub(r"[^0-9A-Za-z-]", "", info.get("factura", "")),
        "orden_compra": re.sub(r"[^0-9A-Za-z-]", "", info.get("orden_compra", "")),
        "numero_ingreso": info.get("numero_ingreso", "").strip(),
        "lineas": filas,
        "cantidad_total": sum(cantidades) if cantidades else None,
        "firmas": _firmas(ruta, lineas),
    }


def validar_recibido(recibido: dict, esperado: dict | None = None) -> dict:
    """Columna X. `esperado` = {"factura", "cantidad_entrada"}."""
    esperado = esperado or {}
    factura_esperada = (esperado.get("factura") or "").strip()
    cantidad_entrada = esperado.get("cantidad_entrada")

    revisiones: dict[str, bool] = {}
    partes: list[str] = []

    # 1. El numero de factura, contra la factura electronica
    if factura_esperada:
        igual = (bool(recibido.get("factura"))
                 and _limpiar_numero(recibido["factura"]) == _limpiar_numero(factura_esperada))
        revisiones["factura"] = igual
        if not igual:
            partes.append(f"dice factura {recibido.get('factura') or 'sin numero'} y la "
                          f"factura es {factura_esperada}")

    # 2. La cantidad recibida, contra la entrada de inventario
    recibida = recibido.get("cantidad_total")
    if cantidad_entrada is not None:
        if recibida is None:
            revisiones["cantidad"] = False
            partes.append("no se pudo leer la cantidad recibida")
        else:
            igual = abs(float(recibida) - float(cantidad_entrada)) < 1e-6
            revisiones["cantidad"] = igual
            if not igual:
                partes.append(f"recibio {recibida:g} y la entrada dice "
                              f"{float(cantidad_entrada):g}")

    # 3. Las dos firmas del pie
    firmas = recibido.get("firmas") or {}
    if all(valor is None for valor in firmas.values()):
        partes.append("no se pueden comprobar las firmas: el documento llego escaneado")
    else:
        faltan = [nombre for nombre, campo in (("auxiliar de almacen", "auxiliar"),
                                               ("supervisor de almacen", "supervisor"))
                  if not firmas.get(campo)]
        revisiones["firmas"] = not faltan
        if faltan:
            partes.append("falta la firma de " + " y de ".join(faltan))

    # Las tres comprobaciones son las que pidio el control. Si alguna no se
    # pudo hacer (falta la entrada, falta la factura, el documento vino
    # escaneado) el resultado es Pendiente y no OK: decir OK habiendo
    # comprobado dos de tres es justo el error que hace inutil la revision.
    pendientes = [c for c in ("factura", "cantidad", "firmas") if c not in revisiones]
    if any(not ok for ok in revisiones.values()):
        estado = "Revisar"
    elif pendientes:
        estado = "Pendiente"
        partes.append("falta comprobar " + ", ".join(pendientes))
    else:
        estado = "OK"

    return {
        "recibido": recibido,
        "revisiones": revisiones,
        "faltantes": [campo for campo, ok in revisiones.items() if not ok],
        "columnas": {"X": estado, "X_obs": " · ".join(partes)},
    }
