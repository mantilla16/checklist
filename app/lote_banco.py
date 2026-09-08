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


def _monto(bruto: str) -> float | None:
    """"COP $ 4.093.736,00" -> 4093736.0"""
    hallado = re.search(r"[\d][\d.,]*", (bruto or "").replace(" ", ""))
    if not hallado:
        return None
    crudo = hallado.group(0)
    # Formato colombiano: el punto separa miles y la coma los decimales
    if "," in crudo:
        crudo = crudo.replace(".", "").replace(",", ".")
    else:
        crudo = crudo.replace(".", "")
    try:
        return float(crudo)
    except ValueError:
        return None


def _fecha_iso(bruto: str) -> str:
    """"4 Sep 2026" o "04/09/2026" -> "2026-09-04"."""
    texto = sin_tildes(bruto or "").strip()

    hallado = re.search(r"(\d{1,2})\s*[/-]\s*(\d{1,2})\s*[/-]\s*(\d{4})", texto)
    if hallado:
        dia, mes, ano = (int(g) for g in hallado.groups())
        return f"{ano:04d}-{mes:02d}-{dia:02d}"

    hallado = re.search(r"(\d{1,2})\s+([A-Za-z]{3,})\.?\s+(\d{4})", texto)
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
        imagenes = [np.array(Image.open(ruta).convert("RGB"))]
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
    otra etiqueta o una linea sin nada bajo esa columna.
    """
    partes: list[str] = []
    for linea in lineas[indice + 1:]:
        if _etiquetas_de(linea):
            break
        debajo = [p["texto"] for p in linea["palabras"]
                  if p["x0"] < x1 and p["x1"] > x0]
        if not debajo:
            break
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
        for indice, palabras in celdas.items():
            palabras.sort(key=lambda p: (p["y0"], p["x0"]))
            fila[columnas[indice]["campo"]] = " ".join(p["texto"] for p in palabras)
        registros.append(fila)
    return registros


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


def analizar_lote(ruta: str, nombre: str = "") -> dict:
    """Encabezado del lote y tabla de registros de la pantalla del portal."""
    palabras = cajas(ruta)
    if not palabras:
        return {"nombre": nombre, "error": "No se pudo leer el documento"}

    lineas = en_lineas(palabras)
    bruto = _encabezado(lineas)

    inicio_tabla = next((i for i, l in enumerate(lineas)
                         if "registros agregados" in normalizar(l["texto"])), 0)
    columnas, fin_encabezado = _columnas(lineas, inicio_tabla)
    filas = _registros(lineas, columnas, fin_encabezado)

    info = {
        "tipo_pago": bruto.get("tipo_pago", ""),
        "nombre_pago": bruto.get("nombre_pago", ""),
        "cuenta": _cuenta(bruto.get("cuenta", "")),
        "cuenta_detalle": re.sub(r"\s+", " ", bruto.get("cuenta", "")).strip(),
        "valor_total": _monto(bruto.get("valor_total", "")),
        "num_registros": (int(_monto(bruto.get("num_registros", "")) or 0)
                          or _declarados(lineas) or None),
        "fecha_aplicacion": _fecha_iso(bruto.get("fecha_aplicacion", "")),
    }

    registros = []
    for posicion, fila in enumerate(filas, start=1):
        referencia = fila.get("referencia", "")
        registros.append({
            "nro": posicion,
            "titular": re.sub(r"\s+", " ", fila.get("titular", "")).strip(),
            "documento": re.sub(r"[^\d]", "", fila.get("documento", "")),
            "cuenta": _cuenta(fila.get("cuenta", "")),
            "cuenta_detalle": re.sub(r"\s+", " ", fila.get("cuenta", "")).strip(),
            "valor": _monto(fila.get("valor", "")),
            "tipo_transaccion": re.sub(r"\s+", " ", fila.get("tipo_transaccion", "")).strip(),
            "referencia": re.sub(r"\s+", " ", referencia).strip(),
            "facturas": _facturas_de(referencia),
        })

    avisos = []
    if info["num_registros"] and info["num_registros"] != len(registros):
        avisos.append(f"la pantalla declara {info['num_registros']} registros y se "
                      f"leyeron {len(registros)}")
    suma = sum(r["valor"] or 0 for r in registros)
    if info["valor_total"] is not None and abs(suma - info["valor_total"]) > 0.5:
        avisos.append(f"los registros suman {suma:,.2f} y el total del lote dice "
                      f"{info['valor_total']:,.2f}")
    for registro in registros:
        if registro["valor"] is None:
            avisos.append(f"no se leyo el valor del registro {registro['nro']}")
        if not registro["documento"]:
            avisos.append(f"no se leyo el documento del registro {registro['nro']}")

    return {"nombre": nombre, "info": info, "registros": registros,
            "avisos": avisos, "suma_registros": suma}
