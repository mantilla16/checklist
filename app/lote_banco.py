"""Lee la pantalla del portal bancario que confirma el lote de pagos.

De aqui salen el encabezado del lote y la tabla azul (columnas B a G): antes
se armaban con los comprobantes de egreso, pero el egreso es del banco hacia
un tercero y no conoce ni el nombre del lote, ni la cuenta destino, ni el
valor total programado.

El documento puede ser imagen o PDF y llega como captura de pantalla, asi que
no se puede leer con expresiones regulares sobre el texto plano: las celdas se
parten en varias lineas ("Cuenta corriente" debajo del numero de cuenta) y una
sola linea de texto mezcla dos columnas. Se lee por posicion, igual que la
cantidad de la factura: se localizan los encabezados y cada palabra se asigna
a la columna sobre la que cae.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from pathlib import Path

IMAGENES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
ESCALA_PDF = 2
# Una captura recortada de la pantalla llega estrecha, y a ese tamano el OCR
# pierde cifras: lee "1.786.00.00" donde dice 1.786.000,00. Ampliarla antes de
# leerla lo corrige, y ampliar de mas no estropea las que ya venian grandes.
ANCHO_MINIMO_OCR = 2400

MESES = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
    "jan": 1, "apr": 4, "aug": 8, "dec": 12,
}

# Encabezado: etiqueta -> campo. El valor esta DEBAJO de la etiqueta, no al
# lado; el portal usa dos columnas y una linea de texto cruza las dos.
ETIQUETAS = {
    "tipo de pago": "tipo_pago",
    "nombre del pago": "nombre_pago",
    "producto a debitar": "cuenta",
    "fecha de aplicacion": "fecha_aplicacion",
    "cantidad de registros": "num_registros",
    "valor total del pago": "valor_total",
    "producto origen": "producto_origen",
}

# Tabla de registros: se reconoce la columna por su palabra distintiva y no
# por el titulo completo, porque el OCR desfigura justo las palabras con
# tilde ("Numero" sale "Nimero") y un titulo de dos lineas puede perder una.
DISTINTIVAS = (
    ("transaccion", "tipo_transaccion"),
    ("destinatario", "titular"),
    ("destino", "cuenta"),          # "Producto destino"
    ("celular", "celular"),
    ("referencia", "referencia"),
    ("valor", "valor"),
)


def campo_de_columna(clave: str) -> str:
    if clave.strip() in ("#", "n", "no"):
        return "nro"
    # "Tipo de documento" y "Numero de documento" comparten la palabra, asi
    # que las separa la presencia de "tipo"
    if "documento" in clave:
        return "tipo_documento" if "tipo" in clave else "documento"
    for distintiva, campo in DISTINTIVAS:
        if distintiva in clave:
            return campo
    return ""


def sin_tildes(texto: str) -> str:
    limpio = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in limpio if not unicodedata.combining(c))


def normalizar(texto: str) -> str:
    return re.sub(r"\s+", " ", sin_tildes(texto).lower()).strip(" .:")


# La coma decimal se pierde en el OCR de tres maneras: como punto
# ("775.200.00", ya cubierto mas abajo), como nada, o como un espacio
# ("2.764.800 00"). Este ultimo caso hay que arreglarlo ANTES de quitar los
# espacios, porque despues "2.764.80000" ya no se distingue de un numero mayor.
RE_DECIMALES_SUELTOS = re.compile(r"(\d{1,3}(?:\.\d{3})+)\s+(\d{2})(?!\d)")


def _coma_recuperada(texto: str) -> str:
    return RE_DECIMALES_SUELTOS.sub(r"\1,\2", texto or "")


def _monto(bruto: str) -> float | None:
    """"COP $ 4.093.736,00" -> 4093736.0"""
    bruto = _coma_recuperada(bruto)
    # El mas largo y no el primero: en la celda se cuela texto de la columna
    # vecina ("Cuenta de ahorro" con su numero de cuenta delante del monto), y
    # tomando el primero se leia el numero de cuenta como valor del pago.
    candidatos = re.findall(r"[\d][\d.,]*", (bruto or "").replace(" ", ""))
    if not candidatos:
        return None
    crudo = max(candidatos, key=lambda c: sum(d.isdigit() for d in c))
    # Formato colombiano: el punto separa miles y la coma los decimales
    if "," in crudo:
        crudo = crudo.replace(".", "").replace(",", ".")
    else:
        # El OCR confunde la coma decimal con un punto ("1.269.399.00"). Un
        # grupo de miles siempre tiene tres cifras, asi que un ultimo grupo de
        # dos son los decimales. Sin esto ese valor se leia como 126.939.900.
        grupos = crudo.split(".")
        if len(grupos) > 1 and len(grupos[-1]) == 2:
            crudo = "".join(grupos[:-1]) + "." + grupos[-1]
        else:
            crudo = crudo.replace(".", "")
    try:
        return float(crudo)
    except ValueError:
        return None


# Formas validas de un monto en pesos. Se comprueba la FORMA y no solo que
# haya digitos: a poca resolucion el OCR pierde cifras ("1.786.00.00" por
# 1.786.000,00) y un valor de dinero mal leido sin avisar es lo peor que puede
# pasar en un papel de trabajo.
FORMAS_MONTO = (
    re.compile(r"^\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?$"),   # 1.786.000,00
    re.compile(r"^\d{1,3}(?:\.\d{3})*\.\d{2}$"),         # coma leida como punto
    re.compile(r"^\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?$"),   # formato anglosajon
    re.compile(r"^\d+$"),                                  # sin separadores
)


def monto_con_forma(bruto: str) -> tuple[float | None, bool]:
    """Devuelve (valor, forma_valida). Sin forma valida, el valor no es fiable."""
    bruto = _coma_recuperada(bruto)
    candidatos = re.findall(r"[\d][\d.,]*", (bruto or "").replace(" ", ""))
    if not candidatos:
        return None, True          # no hay nada que leer, no es un error de forma
    crudo = max(candidatos, key=lambda c: sum(d.isdigit() for d in c))
    return _monto(crudo), any(forma.match(crudo) for forma in FORMAS_MONTO)


def _fecha_iso(bruto: str) -> str:
    """"4 Sep 2026" o "04/09/2026" -> "2026-09-04"."""
    texto = sin_tildes(bruto or "").strip()

    hallado = re.search(r"(\d{1,2})\s*[/-]\s*(\d{1,2})\s*[/-]\s*(\d{4})", texto)
    if hallado:
        dia, mes, ano = (int(g) for g in hallado.groups())
        return f"{ano:04d}-{mes:02d}-{dia:02d}"

    # El espacio entre el mes y el ano se pierde en el OCR ("4 Sep2026")
    hallado = re.search(r"(\d{1,2})\s*([A-Za-z]{3,})\.?\s*(\d{4})", texto)
    if hallado:
        mes = MESES.get(hallado.group(2)[:3].lower())
        if mes:
            return f"{int(hallado.group(3)):04d}-{mes:02d}-{int(hallado.group(1)):02d}"
    return ""


def _cuenta(bruto: str) -> str:
    """Del "787 - 590934 - 14 Cuenta corriente - Bancolombia" deja el numero.

    Los espacios alrededor de los guiones se pierden de forma irregular en el
    OCR ("477 -969056 -72"), asi que se normalizan todos a "477-969056-72".
    """
    hallado = re.search(r"\d[\d\s.\-]{5,}\d", bruto or "")
    if not hallado:
        return ""
    limpio = re.sub(r"\s*-\s*", "-", hallado.group(0))
    return re.sub(r"\s+", "", limpio).strip("-")


# --------------------------------------------------------------------------- #
# Lectura: palabras con su posicion
# --------------------------------------------------------------------------- #

def cajas(ruta: str) -> list[dict]:
    """Palabras del documento con su rectangulo, sea imagen o PDF."""
    extension = Path(ruta).suffix.lower()

    if extension not in IMAGENES:
        import pdfplumber
        with pdfplumber.open(ruta) as documento:
            palabras = []
            for pagina in documento.pages:
                for p in pagina.extract_words(use_text_flow=False):
                    palabras.append({"texto": p["text"], "x0": p["x0"], "x1": p["x1"],
                                     "y0": p["top"], "y1": p["bottom"]})
            if palabras:
                return palabras          # PDF con texto: exacto y sin OCR

    return _ocr(ruta, extension)


def _ocr(ruta: str, extension: str) -> list[dict]:
    import numpy as np
    from radian import _motor

    if extension in IMAGENES:
        from PIL import Image
        imagen = Image.open(ruta).convert("RGB")
        if imagen.width < ANCHO_MINIMO_OCR:
            factor = ANCHO_MINIMO_OCR / imagen.width
            imagen = imagen.resize(
                (ANCHO_MINIMO_OCR, max(1, round(imagen.height * factor))),
                Image.LANCZOS)
        imagenes = [np.array(imagen)]
    else:
        import pypdfium2 as pdfium
        documento = pdfium.PdfDocument(ruta)
        imagenes = [np.array(documento[i].render(scale=ESCALA_PDF).to_pil().convert("RGB"))
                    for i in range(len(documento))]

    motor = _motor()
    palabras: list[dict] = []
    desplazamiento = 0.0
    for imagen in imagenes:
        resultado, _ = motor(imagen)
        for caja, texto, _confianza in (resultado or []):
            if not str(texto).strip():
                continue
            xs = [p[0] for p in caja]
            ys = [p[1] for p in caja]
            palabras.append({
                "texto": str(texto).strip(),
                "x0": min(xs), "x1": max(xs),
                "y0": min(ys) + desplazamiento, "y1": max(ys) + desplazamiento,
            })
        desplazamiento += imagen.shape[0]
    return palabras


def en_lineas(palabras: list[dict], tolerancia: float = 6.0) -> list[dict]:
    """Agrupa por renglon visual. Cada linea guarda sus palabras ordenadas."""
    lineas: list[dict] = []
    for palabra in sorted(palabras, key=lambda p: (p["y0"], p["x0"])):
        centro = (palabra["y0"] + palabra["y1"]) / 2
        for linea in lineas:
            if abs(centro - linea["centro"]) <= max(tolerancia, linea["alto"] / 2):
                linea["palabras"].append(palabra)
                linea["y0"] = min(linea["y0"], palabra["y0"])
                linea["y1"] = max(linea["y1"], palabra["y1"])
                linea["centro"] = (linea["y0"] + linea["y1"]) / 2
                break
        else:
            lineas.append({"palabras": [palabra], "y0": palabra["y0"],
                           "y1": palabra["y1"], "centro": centro,
                           "alto": palabra["y1"] - palabra["y0"]})
    for linea in lineas:
        linea["palabras"].sort(key=lambda p: p["x0"])
        linea["texto"] = " ".join(p["texto"] for p in linea["palabras"])
    return sorted(lineas, key=lambda l: l["y0"])


# --------------------------------------------------------------------------- #
# Encabezado: etiqueta arriba, valor debajo
# --------------------------------------------------------------------------- #

def _etiqueta_parecida(clave: str) -> str:
    """Etiqueta del encabezado admitiendo el desgaste del OCR.

    Las capturas del portal se leen bien casi siempre, pero las palabras con
    tilde son las que primero se estropean ("aplicacion" -> "apiicacion"), y
    son justo las de las etiquetas.
    """
    if len(clave) < 8:
        return ""      # demasiado corta: se parece a todo
    parecidas = difflib.get_close_matches(clave, list(ETIQUETAS), n=1, cutoff=0.82)
    return ETIQUETAS[parecidas[0]] if parecidas else ""


def _etiquetas_de(linea: dict) -> list[dict]:
    """Etiquetas conocidas que hay en la linea, con su posicion horizontal.

    Se prueban tramos de palabras consecutivas porque el OCR parte "Valor
    total del pago" en una caja o en cuatro segun la captura.
    """
    palabras = linea["palabras"]
    encontradas: list[dict] = []
    usadas: set[int] = set()

    for inicio in range(len(palabras)):
        if inicio in usadas:
            continue
        for fin in range(min(inicio + 5, len(palabras)), inicio, -1):
            tramo = palabras[inicio:fin]
            clave = normalizar(" ".join(p["texto"] for p in tramo))
            campo = ETIQUETAS.get(clave) or _etiqueta_parecida(clave)
            if campo:
                encontradas.append({"campo": campo,
                                    "x0": tramo[0]["x0"], "x1": tramo[-1]["x1"]})
                usadas.update(range(inicio, fin))
                break
    return sorted(encontradas, key=lambda e: e["x0"])


def _valor_bajo(lineas: list[dict], indice: int, x0: float, x1: float,
                maximo: int = 3) -> str:
    """Texto de las lineas siguientes que cae bajo la columna de la etiqueta.

    Se admiten varias lineas porque la cuenta ocupa dos ("477 - 969056 - 72"
    y debajo "Cuenta corriente - Bancolombia"), y se corta en cuanto aparece
    otra etiqueta.

    Una linea sin nada bajo la columna NO corta antes de haber encontrado el
    valor: el portal pone un boton "Cambiar" a la derecha de cada campo, y el
    OCR lo lee como una linea propia entre la etiqueta y su valor. Cortar ahi
    dejaba vacio todo el encabezado del lote. Solo se toleran dos lineas asi,
    para no ir a buscar el valor a la seccion siguiente.
    """
    partes: list[str] = []
    vacias = 0
    for linea in lineas[indice + 1:]:
        if _etiquetas_de(linea):
            break
        debajo = [p["texto"] for p in linea["palabras"]
                  if p["x0"] < x1 and p["x1"] > x0]
        if not debajo:
            if partes:
                break                 # ya se tenia el valor: aqui termina
            vacias += 1
            if vacias > 2:
                break
            continue
        partes.append(" ".join(debajo))
        if len(partes) >= maximo:
            break
    return " ".join(partes).strip()


def _encabezado(lineas: list[dict]) -> dict:
    info: dict = {}
    for indice, linea in enumerate(lineas):
        etiquetas = _etiquetas_de(linea)
        for posicion, etiqueta in enumerate(etiquetas):
            # El limite derecho es donde empieza la etiqueta vecina: el portal
            # pone dos campos por linea y sin esto el valor se los come.
            siguiente = etiquetas[posicion + 1]["x0"] if posicion + 1 < len(etiquetas) else None
            derecha = (siguiente - 5) if siguiente else etiqueta["x1"] + 320
            bruto = _valor_bajo(lineas, indice, etiqueta["x0"] - 12, derecha)
            if bruto and etiqueta["campo"] not in info:
                info[etiqueta["campo"]] = bruto
    return info


# --------------------------------------------------------------------------- #
# Tabla de registros: cada palabra a la columna sobre la que cae
# --------------------------------------------------------------------------- #

def _columnas(lineas: list[dict], desde: int) -> tuple[list[dict], int]:
    """Localiza los encabezados de la tabla y devuelve sus tramos de x.

    El encabezado puede ocupar dos lineas ("Numero de" / "documento"), asi que
    se junta la siguiente cuando aporta palabras a las mismas columnas.
    """
    for indice in range(desde, len(lineas)):
        texto = normalizar(lineas[indice]["texto"])
        if "destinatario" in texto and ("valor" in texto or "referencia" in texto):
            palabras = list(lineas[indice]["palabras"])
            fin = indice
            for extra in lineas[indice + 1:indice + 3]:
                claves = normalizar(extra["texto"])
                if any(c in claves for c in ("documento", "transaccion", "destino",
                                             "celular")):
                    palabras += extra["palabras"]
                    fin = lineas.index(extra)
                else:
                    break

            grupos = _agrupar_por_x(palabras)
            columnas = []
            for grupo in grupos:
                clave = normalizar(" ".join(p["texto"] for p in
                                            sorted(grupo, key=lambda p: (p["y0"], p["x0"]))))
                columnas.append({
                    "campo": campo_de_columna(clave) or clave,
                    "x0": min(p["x0"] for p in grupo),
                    "x1": max(p["x1"] for p in grupo),
                })
            return sorted(columnas, key=lambda c: c["x0"]), fin
    return [], desde


def _agrupar_por_x(palabras: list[dict], holgura: float = 6.0) -> list[list[dict]]:
    """Junta las palabras que comparten franja horizontal (misma columna)."""
    grupos: list[list[dict]] = []
    for palabra in sorted(palabras, key=lambda p: p["x0"]):
        for grupo in grupos:
            if palabra["x0"] <= max(p["x1"] for p in grupo) + holgura:
                grupo.append(palabra)
                break
        else:
            grupos.append([palabra])
    return grupos


def _limites(columnas: list[dict]) -> list[float]:
    """Fronteras entre columnas: el punto medio entre una y la siguiente.

    Se usan los puntos medios y no el ancho del encabezado porque el dato es
    mas ancho que su titulo: "COP $ 4.093.736,00" no cabe bajo "Valor".
    """
    fronteras = []
    for anterior, siguiente in zip(columnas, columnas[1:]):
        fronteras.append((anterior["x1"] + siguiente["x0"]) / 2)
    return fronteras


def _registros(lineas: list[dict], columnas: list[dict], desde: int) -> list[dict]:
    if not columnas:
        return []
    fronteras = _limites(columnas)

    def columna_de(palabra: dict) -> int:
        centro = (palabra["x0"] + palabra["x1"]) / 2
        for indice, frontera in enumerate(fronteras):
            if centro < frontera:
                return indice
        return len(columnas) - 1

    fin_tabla = next((i for i in range(desde + 1, len(lineas))
                      if "mostrando" in normalizar(lineas[i]["texto"])), len(lineas))

    # Cada fila se ancla en su valor, no en el numero de orden: el OCR descarta
    # los numeros de una sola cifra, con lo que la columna "#" desaparece.
    # El monto lleva "COP $" y es el dato mas grande y mas fiable de la fila.
    indice_valor = next((i for i, c in enumerate(columnas) if c["campo"] == "valor"), None)
    if indice_valor is None:
        return []

    anclas: list[float] = []
    for linea in lineas[desde + 1:fin_tabla]:
        for palabra in linea["palabras"]:
            if columna_de(palabra) == indice_valor and re.search(r"\d[\d.,]{4,}",
                                                                 palabra["texto"]):
                anclas.append((linea["y0"] + linea["y1"]) / 2)
                break

    if not anclas:
        return []

    # La fila llega hasta la mitad del camino a la siguiente: las celdas de
    # varias lineas ("Cuenta corriente" debajo del numero) caen arriba y abajo
    # de su propio monto.
    techo = lineas[desde]["y1"] if desde < len(lineas) else 0
    suelo = lineas[fin_tabla - 1]["y1"] + 40 if fin_tabla else float("inf")
    bandas = []
    for posicion, ancla in enumerate(anclas):
        arriba = techo if posicion == 0 else (anclas[posicion - 1] + ancla) / 2
        abajo = suelo if posicion == len(anclas) - 1 else (ancla + anclas[posicion + 1]) / 2
        bandas.append((arriba, abajo))

    registros = []
    for arriba, abajo in bandas:
        celdas: dict[int, list[dict]] = {}
        for linea in lineas[desde + 1:fin_tabla]:
            centro = (linea["y0"] + linea["y1"]) / 2
            if not (arriba <= centro < abajo):
                continue
            for palabra in linea["palabras"]:
                celdas.setdefault(columna_de(palabra), []).append(palabra)

        fila = {}
        todas: list[dict] = []
        for indice, palabras in celdas.items():
            palabras.sort(key=lambda p: (p["y0"], p["x0"]))
            fila[columnas[indice]["campo"]] = " ".join(p["texto"] for p in palabras)
            todas += palabras
        # El texto completo de la fila sirve de respaldo: los limites de columna
        # salen del centro de los titulos, y un dato mas ancho que su titulo se
        # corre a la columna vecina.
        todas.sort(key=lambda p: (p["y0"], p["x0"]))
        fila["_texto"] = " ".join(p["texto"] for p in todas)
        registros.append(fila)
    return registros


# NIT con sus puntos de miles: 9.007.998.491, 802.022.539. Es lo que separa el
# documento del numero de cuenta, que va con guiones y espacios.
RE_NIT_PUNTEADO = re.compile(r"^\d{1,3}(?:\.\d{3}){2,3}$")
RE_MONTO_COP = re.compile(r"C\s*[O0]\s*P", re.IGNORECASE)


def _registros_por_contenido(lineas: list[dict]) -> list[dict]:
    """Filas de una captura que no trae los titulos de las columnas.

    Cada fila se ancla en su monto ("COP $ ...") y, dentro de su franja, cada
    dato se reconoce por como es y por donde queda respecto de los demas, que
    es lo unico que se mantiene de una captura a otra:

      a la derecha del monto ........ la referencia
      el NIT con puntos de miles .... el documento
      entre el NIT y el monto ....... la cuenta (con guiones, en varias lineas)
      a la izquierda del NIT ........ el destinatario

    No se reutilizan las columnas de la captura anterior: cada recorte tiene
    otro ancho y otro margen, asi que sus coordenadas no coinciden.
    """
    palabras = [p for l in lineas for p in l["palabras"]]
    montos = [p for p in palabras if RE_MONTO_COP.search(p["texto"])
              and re.search(r"\d", p["texto"])]
    if not montos:
        return []

    montos.sort(key=lambda p: (p["y0"] + p["y1"]) / 2)
    centros = [(p["y0"] + p["y1"]) / 2 for p in montos]

    filas = []
    for posicion, monto in enumerate(montos):
        arriba = -1e9 if posicion == 0 else (centros[posicion - 1] + centros[posicion]) / 2
        abajo = 1e9 if posicion == len(montos) - 1 else (centros[posicion] + centros[posicion + 1]) / 2
        franja = [p for p in palabras
                  if arriba <= (p["y0"] + p["y1"]) / 2 < abajo and p is not monto]
        orden = lambda grupo: sorted(grupo, key=lambda p: (p["y0"], p["x0"]))

        referencia = [p for p in franja if p["x0"] >= monto["x1"] - 2]
        izquierda = [p for p in franja if p["x1"] <= monto["x0"] + 2]

        nit = next((p for p in orden(izquierda)
                    if RE_NIT_PUNTEADO.match(p["texto"].replace(" ", ""))), None)
        if nit is not None:
            cuenta = [p for p in izquierda if p["x0"] >= nit["x1"] - 2 and p is not nit]
            nombre = [p for p in izquierda if p["x1"] <= nit["x0"] + 2]
        else:
            cuenta, nombre = [], izquierda

        texto = lambda grupo: " ".join(p["texto"] for p in orden(grupo))
        filas.append({
            "valor": monto["texto"],
            "documento": nit["texto"] if nit else "",
            "cuenta": texto(cuenta),
            "titular": _titular_del_texto(texto(nombre), RUIDO_COLUMNA_NOMBRE),
            "referencia": texto(referencia),
            "_texto": texto(franja + [monto]),
        })
    return filas


# --------------------------------------------------------------------------- #
# Resultado
# --------------------------------------------------------------------------- #

def _facturas_de(referencia: str) -> list[str]:
    """De "F 157215132 157212397" saca las dos facturas.

    La referencia del pago es lo unico que dice cuantas facturas cubre un
    registro, que es justo el caso de gases industriales: un valor neto con
    dos facturas.
    """
    return re.findall(r"\d{2,}", referencia or "")


def _declarados(lineas: list[dict]) -> int:
    """Cuantos pagos dice el portal, leidos de "Mostrando 1-4 de 4 pagos".

    Hace falta porque el OCR descarta los numeros de una sola cifra y la
    "Cantidad de registros" del encabezado suele ser justo eso.
    """
    for linea in lineas:
        hallado = re.search(r"mostrando\s+[\d\s-]+de\s+(\d+)\s+pago",
                            normalizar(linea["texto"]))
        if hallado:
            return int(hallado.group(1))
    return 0


def _es_ruido_de_pantalla(texto: str) -> bool:
    """Botones y avisos del portal que el OCR lee como si fueran datos.

    "Cambiar" esta al lado de cada campo del encabezado, y el globo flotante
    "Necesitas ayuda?" tapa la ultima fila visible y se metia en la referencia.
    """
    limpio = normalizar(texto).strip("?¿ ")
    return (limpio in ("cambiar", "ayuda", "necesitas", "necesitas ayuda")
            or "necesitas ayuda" in limpio)


def _leer_pantalla(ruta: str) -> tuple[dict, list[dict]]:
    """Lee una imagen: el encabezado en bruto y sus filas de la tabla."""
    palabras = [p for p in cajas(ruta) if not _es_ruido_de_pantalla(p["texto"])]
    if not palabras:
        return {}, []

    lineas = en_lineas(palabras)
    bruto = _encabezado(lineas)
    bruto["_declarados"] = _declarados(lineas)

    inicio_tabla = next((i for i, l in enumerate(lineas)
                         if "registros agregados" in normalizar(l["texto"])), 0)
    columnas, fin_encabezado = _columnas(lineas, inicio_tabla)
    if columnas:
        return bruto, _registros(lineas, columnas, fin_encabezado)

    # Sin titulos de columna: es la continuacion de una tabla partida. El
    # portal no repite los titulos en la segunda captura, asi que se lee por
    # el contenido de cada fila y no por la columna en la que cae.
    return bruto, _registros_por_contenido(lineas)


# Palabras del propio formulario, que nunca son parte del nombre del proveedor
RUIDO = {
    "abono", "cuenta", "cuentas", "ahorro", "ahorros", "corriente", "banco",
    "bancos", "bancolombia", "davivienda", "occidente", "villas", "bbva", "av",
    "popular", "agrario", "caja", "social", "itau", "scotiabank", "colpatria",
    "falabella", "pichincha", "cop", "transferencia", "debito", "credito",
    "pago", "pagos", "abonoa", "de", "del", "la", "el",
    # Restos de un "COP $" mal leido
    "cops", "cop", "copz", "cor",
}


# Lo unico que acompana al nombre en su propia columna es el tipo de
# transaccion. Cuando la columna ya esta aislada no hace falta el filtro
# amplio, que quita "de", "la" y "el" porque abundan en "Cuenta de ahorro" y
# "Banco de Bogota", y con eso destrozaba nombres como "solmaq de la costa".
RUIDO_COLUMNA_NOMBRE = {"abono", "abonoa", "cuenta", "a"}


def _titular_del_texto(texto: str, ruido: set[str] | None = None) -> str:
    """Nombre del proveedor sacado del texto de la fila.

    Respaldo para cuando el reparto por columnas deja el titular vacio: se
    toman las palabras de letras que no son del formulario ni de un banco.
    """
    # Solo tramos que tengan alguna letra: los puntos de los miles se colaban
    # como palabras sueltas
    palabras = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ][A-Za-zÁÉÍÓÚÑáéíóúñ&']*", texto or "")
    utiles: list[str] = []
    for palabra in palabras:
        limpia = sin_tildes(palabra).lower()
        # Las de una letra solo valen como enlace ("chapman y asociado"); el
        # resto son restos del OCR, como la S de un "COP$" mal leido
        if len(limpia) == 1:
            if limpia in ("y", "e") and utiles and limpia not in (ruido or ()):
                utiles.append(palabra)
            continue
        if limpia not in (RUIDO if ruido is None else ruido):
            utiles.append(palabra)
    while utiles and sin_tildes(utiles[-1]).lower() in ("y", "e"):
        utiles.pop()
    return " ".join(utiles).strip()


def _registro_desde_fila(fila: dict, posicion: int) -> dict:
    referencia = fila.get("referencia", "")
    crudo_valor = re.sub(r"\s+", " ", fila.get("valor", "")).strip()
    valor, forma_ok = monto_con_forma(crudo_valor)
    if not forma_ok:
        valor = None               # mejor vacio y avisando que mal y en silencio

    titular = re.sub(r"\s+", " ", fila.get("titular", "")).strip()
    de_respaldo = False
    if not titular:
        titular = _titular_del_texto(fila.get("_texto", ""))
        de_respaldo = bool(titular)
    return {
        "nro": posicion,
        "titular": titular,
        "documento": re.sub(r"[^\d]", "", fila.get("documento", "")),
        "cuenta": _cuenta(fila.get("cuenta", "")),
        "cuenta_detalle": re.sub(r"\s+", " ", fila.get("cuenta", "")).strip(),
        "valor": valor,
        "valor_crudo": crudo_valor,
        "tipo_transaccion": re.sub(r"\s+", " ", fila.get("tipo_transaccion", "")).strip(),
        "referencia": re.sub(r"\s+", " ", referencia).strip(),
        "facturas": _facturas_de(referencia),
        # Lo que se leyo de la fila, tal cual: si un campo sale torcido, aqui
        # se ve por que sin tener que volver a pasar el OCR
        "crudo": re.sub(r"\s+", " ", fila.get("_texto", "")).strip(),
        "titular_de_respaldo": de_respaldo,
    }


def analizar_lote(rutas, nombre: str = "") -> dict:
    """Encabezado del lote y tabla de registros de la pantalla del portal.

    Admite varias imagenes: cuando el lote tiene muchos registros la tabla no
    cabe en una captura y llega partida, con el encabezado de la tabla repetido
    en cada trozo. Las filas se concatenan en el orden en que llegan los
    archivos y del encabezado del lote se toma el primer valor que aparezca,
    porque los datos del pago solo estan en la primera captura.
    """
    if isinstance(rutas, (str, Path)):
        rutas = [rutas]
    rutas = [str(r) for r in rutas]
    if not rutas:
        return {"nombre": nombre, "error": "No se recibio ninguna imagen"}

    brutos: list[dict] = []
    ilegibles: list[str] = []
    capturas: list[tuple[dict, list[dict]]] = []
    for ruta in rutas:
        bruto, propias = _leer_pantalla(ruta)
        if not bruto and not propias:
            ilegibles.append(Path(ruta).name)
            continue
        capturas.append((bruto, propias))

    # La captura con el encabezado del lote es el principio de la tabla, sea
    # cual sea el orden en que se arrastraron: si la continuacion llegara
    # primero, la fila 5 quedaria numerada como 1 y la columna B del Excel
    # saldria corrida. Entre las demas se respeta el orden de llegada.
    con_encabezado = lambda bruto: any(bruto.get(c) for c in
                                       ("nombre_pago", "valor_total", "tipo_pago"))
    capturas.sort(key=lambda c: 0 if con_encabezado(c[0]) else 1)

    filas: list[dict] = []
    for bruto, propias in capturas:
        brutos.append(bruto)
        filas.extend(propias)

    if not brutos:
        return {"nombre": nombre, "error": "No se pudo leer ninguna de las imagenes"}

    def primero(campo: str) -> str:
        return next((b.get(campo, "") for b in brutos if b.get(campo)), "")

    declarados = next((b["_declarados"] for b in brutos if b.get("_declarados")), 0)
    info = {
        "tipo_pago": primero("tipo_pago"),
        "nombre_pago": primero("nombre_pago"),
        "cuenta": _cuenta(primero("cuenta")),
        "cuenta_detalle": re.sub(r"\s+", " ", primero("cuenta")).strip(),
        "valor_total": _monto(primero("valor_total")),
        "num_registros": (int(_monto(primero("num_registros")) or 0)
                          or declarados or None),
        "fecha_aplicacion": _fecha_iso(primero("fecha_aplicacion")),
    }

    # Dos capturas pueden solaparse: la misma fila no se cuenta dos veces
    registros: list[dict] = []
    vistos: set[tuple] = set()
    repetidas = 0
    for fila in filas:
        registro = _registro_desde_fila(fila, len(registros) + 1)
        huella = (registro["documento"], registro["valor"], registro["referencia"])
        if huella in vistos and any(huella):
            repetidas += 1
            continue
        vistos.add(huella)
        registros.append(registro)

    avisos = []
    for nombre_ilegible in ilegibles:
        avisos.append(f"no se pudo leer {nombre_ilegible}")
    if repetidas:
        avisos.append(f"{repetidas} fila(s) venian repetidas en dos capturas y se "
                      f"contaron una sola vez")
    if info["num_registros"] and info["num_registros"] != len(registros):
        avisos.append(f"la pantalla declara {info['num_registros']} registros y se "
                      f"leyeron {len(registros)}"
                      + ("; puede faltar una captura" if len(rutas) == 1 else ""))
    suma = sum(r["valor"] or 0 for r in registros)
    if info["valor_total"] is not None and abs(suma - info["valor_total"]) > 0.5:
        avisos.append(f"los registros suman {suma:,.2f} y el total del lote dice "
                      f"{info['valor_total']:,.2f}")
    for registro in registros:
        if not registro["titular"]:
            avisos.append(f"no se leyo el destinatario del registro "
                          f"{registro['nro']}; la fila dice: {registro['crudo'][:120]}")
        elif registro["titular_de_respaldo"]:
            avisos.append(f"el destinatario del registro {registro['nro']} se "
                          f"deduzco del texto de la fila: \"{registro['titular']}\"")
        if registro["valor"] is None:
            crudo = registro.get("valor_crudo") or ""
            avisos.append(
                f"el valor del registro {registro['nro']} no se pudo leer con "
                f"seguridad" + (f': la celda dice "{crudo[:60]}"' if crudo else "")
                + "; escribelo a mano")
        if not registro["documento"]:
            avisos.append(f"no se leyo el documento del registro {registro['nro']}")

    return {"nombre": nombre, "info": info, "registros": registros,
            "avisos": avisos, "suma_registros": suma, "capturas": len(brutos)}
