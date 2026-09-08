"""Validacion de la factura electronica (columnas M a P del papel de trabajo).

Las facturas llegan en PDF con formatos muy distintos (World Office, Carvajal,
The Factory HKA, SAP, FedEx...), asi que la estrategia no es "leer cada
formato" sino VALIDAR contra lo que ya se conoce:

- el NIT del cliente sale del comprobante de egreso (quien paga);
- el NIT del tercero sale de "Documento titular" de la tabla azul;
- el numero de factura sale del egreso.

Se comprueba que esos datos aparezcan en la factura, y ademas que traiga CUFE
y codigo QR. De ahi salen:

    M  Factura     -> OK si pasa todo
    N  No. Factura -> el numero leido de la factura
    O  Nit tercero -> el NIT del emisor leido de la factura
    P  Validacion  -> VERDADERO si O coincide con el documento titular
"""

from __future__ import annotations

import logging
import re

import cv2
import numpy as np
import pdfplumber
import pypdfium2 as pdfium

logging.getLogger("pdfminer").setLevel(logging.ERROR)
# El decodificador de OpenCV avisa "ECI is not supported" en algunos QR
if hasattr(cv2, "utils") and hasattr(cv2.utils, "logging"):
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)

# El CUFE/CUDE es un SHA-384 en hexadecimal: 96 caracteres
RE_CUFE = re.compile(
    r"(?P<tipo>CUFE|CUDE|CUDS)\s*[:=]?\s*(?P<valor>[0-9a-fA-F]{96})", re.IGNORECASE)
RE_HEX96 = re.compile(r"\b[0-9a-fA-F]{96}\b")
RE_URL_DIAN = re.compile(
    r"(?:catalogo-vpfe|documentKey=)[^\s]*?([0-9a-fA-F]{96})", re.IGNORECASE)

# Numero de factura en distintos formatos
RE_NUMERO = re.compile(
    r"(?:factura\s+(?:electr[oó]nica\s+)?(?:de\s+venta\s+)?n[o°u]?\.?\s*:?\s*"
    r"|Nro\.?\s*Doc\.?\s*:?\s*|No\.?\s*Factura\s*:?\s*|FE\s*No\.?\s*)"
    r"([A-Z]{0,6}[-\s]?\d{1,12})",
    re.IGNORECASE,
)

# Etiquetas que identifican al comprador dentro de la factura
CLAVES_ADQUIRENTE = (
    "adquirente", "cliente", "datos cliente", "vendido a", "despachado a",
    "senores", "señores", "comprador", "receptor", "facturar a", "razon social/nombre",
)
CLAVES_EMISOR = ("emisor", "datos del emisor", "vendedor", "proveedor", "facturador")


# --------------------------------------------------------------------------- #
# NIT
# --------------------------------------------------------------------------- #

def solo_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def nit_igual(uno, otro) -> bool:
    """Compara NIT tolerando puntos, guiones y el digito de verificacion."""
    a, b = solo_digitos(uno), solo_digitos(otro)
    if not a or not b:
        return False
    if a == b:
        return True
    # 9001234560 (con DV) contra 900123456 (sin DV)
    return (len(a) == len(b) + 1 and a[:-1] == b) or (len(b) == len(a) + 1 and b[:-1] == a)


def patron_nit(nit: str) -> re.Pattern:
    """Regex que encuentra un NIT escrito con cualquier separador.

    900123456 casa con "900.123.456", "900,123,456-0", "900123456 0"...
    """
    digitos = solo_digitos(nit)
    if not digitos:
        return re.compile(r"(?!x)x")   # nunca casa
    cuerpo = r"[.,\s-]{0,2}".join(digitos)
    return re.compile(r"(?<!\d)" + cuerpo + r"(?:[.,\s-]{0,2}\d)?(?!\d)")


def aparece_nit(texto: str, nit: str) -> bool:
    """Busca el NIT con o sin digito de verificacion.

    La tabla azul es irregular: 9013334445 trae DV y 900555111 no. La factura
    puede escribirlo de la otra forma.
    """
    digitos = solo_digitos(nit)
    if not digitos:
        return False
    if patron_nit(digitos).search(texto):
        return True
    return len(digitos) >= 10 and bool(patron_nit(digitos[:-1]).search(texto))


# --------------------------------------------------------------------------- #
# Codigo QR
# --------------------------------------------------------------------------- #

_DETECTOR = cv2.QRCodeDetector()
ESCALAS = (6, 8, 4, 12, 16, 5)


def _limpiar_qr(texto: str) -> str:
    """Quita bytes de relleno que algunos generadores meten al inicio."""
    return "".join(c for c in texto if c.isprintable() or c in "\r\n").strip()


def _campos_qr(contenido: str) -> dict:
    """Lee los campos del QR de la DIAN.

    Vienen en tres formatos: uno por linea ("NumFac: FC91"), todos en una sola
    linea separados por espacios ("NumFac=FV149 FecFac=2026-08-19") o con doble
    espacio. Los valores no llevan espacios, asi que el barrido por token es el
    mas seguro y va primero.
    """
    campos = {}
    for m in re.finditer(r"([A-Za-z]{3,12})\s*[:=]\s*([^\s]+)", contenido):
        campos.setdefault(m.group(1).lower(), m.group(2).strip())

    for pieza in re.split(r"[\r\n]+|(?<=\S)\s{2,}", contenido):
        m = re.match(r"\s*([A-Za-z]{3,12})\s*[:=]\s*(.+?)\s*$", pieza)
        if m:
            campos.setdefault(m.group(1).lower(), m.group(2).strip())
    return campos


def _parece_qr(recorte: np.ndarray) -> bool:
    """Un QR tiene muchisimas transiciones blanco/negro; un logo no.

    Calibrado con facturas reales: QR 42-55 transiciones por fila, logos 2-6.
    """
    if recorte.size < 2500:
        return False
    normal = cv2.resize(recorte, (200, 200), interpolation=cv2.INTER_AREA)
    _, binaria = cv2.threshold(normal, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bits = (binaria > 127).astype(np.int8)
    filas = np.abs(np.diff(bits, axis=1)).sum(axis=1).mean()
    columnas = np.abs(np.diff(bits, axis=0)).sum(axis=0).mean()
    tinta = 1 - bits.mean()
    return filas >= 20 and columnas >= 20 and 0.25 <= tinta <= 0.75


def _decodificar_qr(documento) -> tuple[str | None, int | None]:
    """Intenta leer el QR a varias escalas. Devuelve (contenido, pagina)."""
    for indice in range(min(len(documento), 4)):
        pagina = documento[indice]
        for escala in ESCALAS:
            try:
                imagen = np.array(pagina.render(scale=escala).to_pil().convert("L"))
                ok, datos, _, _ = _DETECTOR.detectAndDecodeMulti(imagen)
            except Exception:
                continue
            if ok:
                for dato in datos or []:
                    limpio = _limpiar_qr(dato or "")
                    if len(limpio) > 10:
                        return limpio, indice + 1
    return None, None


def _buscar_qr_por_forma(ruta: str, documento) -> dict | None:
    """Cuando el QR no decodifica (logo encima, texto solapado) se detecta
    por su estructura: imagen cuadrada con densidad de modulos."""
    with pdfplumber.open(ruta) as pdf:
        paginas = [
            (indice, pagina.width, pagina.height, list(pagina.images))
            for indice, pagina in enumerate(pdf.pages[:4])
        ]

    escala = 6
    for indice, _, _, imagenes in paginas:
        candidatas = [
            im for im in imagenes
            if 30 < im["width"] < 300 and 0.7 <= im["width"] / max(im["height"], 1) <= 1.4
        ]
        if not candidatas:
            continue
        pagina = documento[indice]
        lienzo = np.array(pagina.render(scale=escala).to_pil().convert("L"))
        for im in candidatas:
            recorte = lienzo[
                max(int(im["top"] * escala), 0):int(im["bottom"] * escala),
                max(int(im["x0"] * escala), 0):int(im["x1"] * escala),
            ]
            if _parece_qr(recorte):
                return {"pagina": indice + 1,
                        "tamano": f"{round(im['width'])}x{round(im['height'])}"}
    return None


def leer_qr(ruta: str) -> dict:
    """Devuelve {presente, metodo, contenido, campos, pagina}."""
    documento = pdfium.PdfDocument(ruta)

    contenido, pagina = _decodificar_qr(documento)
    if contenido:
        return {"presente": True, "metodo": "decodificado", "contenido": contenido,
                "campos": _campos_qr(contenido), "pagina": pagina}

    forma = _buscar_qr_por_forma(ruta, documento)
    if forma:
        return {"presente": True, "metodo": "estructura", "contenido": None,
                "campos": {}, **forma}

    return {"presente": False, "metodo": "no encontrado", "contenido": None, "campos": {}}


# --------------------------------------------------------------------------- #
# CUFE y numero
# --------------------------------------------------------------------------- #

def leer_cufe(texto: str) -> dict:
    m = RE_CUFE.search(texto)
    if m:
        return {"presente": True, "tipo": m.group("tipo").upper(),
                "valor": m.group("valor").lower(), "fuente": "etiqueta"}
    m = RE_URL_DIAN.search(texto)
    if m:
        return {"presente": True, "tipo": "CUFE", "valor": m.group(1).lower(),
                "fuente": "url DIAN"}
    m = RE_HEX96.search(texto)
    if m:
        return {"presente": True, "tipo": "CUFE", "valor": m.group(0).lower(),
                "fuente": "sin etiqueta"}
    return {"presente": False, "tipo": "", "valor": "", "fuente": ""}


def _normalizar_numero(valor) -> str:
    """FC91 / FCHA24381 / FVE6044 / 00000004719 -> 91 / 24381 / 6044 / 4719."""
    digitos = solo_digitos(valor)
    return str(int(digitos)) if digitos else ""


def leer_numero(texto: str, campos_qr: dict, esperado: str = "") -> dict:
    """El numero del QR manda; si no, se busca en el texto."""
    del_qr = campos_qr.get("numfac", "")
    if del_qr:
        # Algunos generadores rellenan con 0xFF ("ÿÿÿÿ4719"): solo ASCII
        limpio = re.sub(r"[^A-Za-z0-9-]", "", del_qr)
        if limpio:
            return {"valor": limpio, "fuente": "QR"}

    for m in RE_NUMERO.finditer(texto):
        candidato = re.sub(r"\s+", "", m.group(1))
        if solo_digitos(candidato):
            return {"valor": candidato, "fuente": "texto"}

    # Ultimo recurso: confirmar que el numero esperado aparece en el documento
    if esperado and re.search(r"(?<!\d)" + re.escape(solo_digitos(esperado)) + r"(?!\d)", texto):
        return {"valor": esperado, "fuente": "esperado hallado"}

    return {"valor": "", "fuente": ""}


# --------------------------------------------------------------------------- #
# NIT del emisor y del adquirente
# --------------------------------------------------------------------------- #

RE_NIT_ETIQUETA = re.compile(
    r"\b(?:N\.?\s?I\.?\s?T\.?|NIT/CC|Nit)\b\s*[:.]?\s*(\d[\d.,\s-]{5,18}\d)",
    re.IGNORECASE,
)


def _nits_con_contexto(texto: str) -> list[dict]:
    hallados = []
    minuscula = texto.lower()
    for m in RE_NIT_ETIQUETA.finditer(texto):
        crudo = m.group(1)
        digitos = solo_digitos(crudo)
        if not (7 <= len(digitos) <= 11):
            continue
        ventana = minuscula[max(m.start() - 160, 0): m.end() + 60]
        papel = ""
        if any(c in ventana for c in CLAVES_ADQUIRENTE):
            papel = "adquirente"
        if any(c in ventana for c in CLAVES_EMISOR):
            papel = "emisor" if papel != "adquirente" else papel
        hallados.append({"nit": digitos, "posicion": m.start(), "papel": papel})
    return hallados


def leer_nits(texto: str, campos_qr: dict, esperado: dict) -> dict:
    """Determina el NIT del emisor y del adquirente."""
    nit_cliente = esperado.get("nit_cliente") or ""
    nit_tercero = esperado.get("nit_tercero") or ""

    # 1) El QR de la DIAN los trae explicitos: NitFac (emisor), DocAdq (adquirente)
    emisor = solo_digitos(campos_qr.get("nitfac", ""))
    adquirente = solo_digitos(campos_qr.get("docadq", ""))
    fuente = "QR" if (emisor or adquirente) else ""

    candidatos = _nits_con_contexto(texto)

    # 2) Si no vino del QR, se prefiere el que coincide con lo esperado
    if not emisor:
        if nit_tercero and aparece_nit(texto, nit_tercero):
            emisor = solo_digitos(nit_tercero)
            fuente = fuente or "coincide con la tabla azul"
        else:
            # El primero que no sea el cliente, dando prioridad al encabezado
            ajenos = [c for c in candidatos if not nit_igual(c["nit"], nit_cliente)]
            marcados = [c for c in ajenos if c["papel"] == "emisor"]
            elegido = (marcados or ajenos or [None])[0]
            if elegido:
                emisor = elegido["nit"]
                fuente = fuente or "texto"

    if not adquirente and nit_cliente and aparece_nit(texto, nit_cliente):
        adquirente = solo_digitos(nit_cliente)
        fuente = fuente or "texto"

    return {
        "emisor": emisor,
        "adquirente": adquirente,
        "fuente": fuente,
        "todos": sorted({c["nit"] for c in candidatos}),
    }


# --------------------------------------------------------------------------- #
# Analisis completo
# --------------------------------------------------------------------------- #

def analizar_factura(ruta: str, nombre: str, esperado: dict | None = None) -> dict:
    """Valida una factura electronica contra lo que ya se conoce del pago.

    `esperado` = {"nit_cliente": ..., "nit_tercero": ..., "numero": ...}
    """
    esperado = esperado or {}

    with pdfplumber.open(ruta) as pdf:
        paginas = [pagina.extract_text() or "" for pagina in pdf.pages]
    texto = "\n".join(paginas)
    sin_texto = not texto.strip()

    qr = leer_qr(ruta)
    cufe = leer_cufe(texto)
    try:
        cantidad = leer_cantidad_factura(ruta)
    except Exception:
        cantidad = {"total": None, "lineas": [], "cabecera": ""}
    numero = leer_numero(texto, qr["campos"], esperado.get("numero", ""))
    nits = leer_nits(texto, qr["campos"], esperado)

    nit_cliente = esperado.get("nit_cliente") or ""
    nit_tercero = esperado.get("nit_tercero") or ""

    # El NIT del cliente se acepta si viene en el QR o si aparece en el texto
    cliente_ok = bool(nit_cliente) and (
        nit_igual(nits["adquirente"], nit_cliente) or aparece_nit(texto, nit_cliente)
    )
    tercero_ok = bool(nit_tercero) and (
        nit_igual(nits["emisor"], nit_tercero) or aparece_nit(texto, nit_tercero)
    )
    numero_ok = bool(numero["valor"]) and (
        not esperado.get("numero")
        or _normalizar_numero(numero["valor"]) == _normalizar_numero(esperado["numero"])
    )

    revisiones = {
        "nit_cliente": cliente_ok,
        "nit_tercero": tercero_ok,
        "cufe": cufe["presente"],
        "qr": qr["presente"],
        "numero": numero_ok,
    }
    faltantes = [clave for clave, ok in revisiones.items() if not ok]

    return {
        "nombre": nombre,
        "sin_texto": sin_texto,
        "paginas": len(paginas),
        "qr": qr,
        "cufe": cufe,
        "numero": numero,
        "cantidad": cantidad,
        "nits": nits,
        "esperado": {"nit_cliente": nit_cliente, "nit_tercero": nit_tercero,
                     "numero": esperado.get("numero", "")},
        "revisiones": revisiones,
        "faltantes": faltantes,
        # Columnas del papel de trabajo
        "columnas": {
            "M": "OK" if not faltantes else "",
            "N": numero["valor"],
            # Si el NIT de la factura y el de la tabla azul son el mismo pero
            # escritos distinto (con o sin DV), se escribe el de la tabla azul
            # para que la comparacion en el Excel sea directa.
            "O": (solo_digitos(nit_tercero)
                  if tercero_ok and nit_tercero else nits["emisor"]),
            "P": tercero_ok,
        },
    }


def _diferencias(uno: str, otro: str) -> int:
    """Cuantos digitos difieren entre dos numeros de la misma longitud."""
    if len(uno) != len(otro):
        return 99
    return sum(1 for a, b in zip(uno, otro) if a != b)


def emparejar(analisis: dict, facturas: list[str]) -> dict:
    """Une el PDF con el renglon del registro que le corresponde.

    Devuelve {"factura": <la del registro>, "exacto": bool, "aviso": str}.
    Si no calza exacto se busca un numero casi igual: el egreso y la factura
    pueden traer un digito distinto (visto en 127212397 contra 157212397), y
    eso hay que mostrarlo, no taparlo.
    """
    leido = _normalizar_numero(analisis["columnas"]["N"])
    if not leido:
        return {"factura": "", "exacto": False, "aviso": ""}

    for factura in facturas:
        if _normalizar_numero(factura) == leido:
            return {"factura": factura, "exacto": True, "aviso": ""}

    for factura in facturas:
        propio = _normalizar_numero(factura)
        if 0 < _diferencias(propio, leido) <= 2:
            return {
                "factura": factura,
                "exacto": False,
                "aviso": f"el egreso dice {factura} y la factura dice "
                         f"{analisis['columnas']['N']}",
            }

    numeros = ", ".join(facturas) if facturas else "ninguna"
    return {
        "factura": "", "exacto": False,
        "aviso": f"el egreso relaciona {numeros}; esta factura es la "
                 f"{analisis['columnas']['N']}",
    }

# --------------------------------------------------------------------------- #
# Cantidad facturada
# --------------------------------------------------------------------------- #

# Cabecera de la columna de cantidad, en los formatos vistos:
# "Cantidad/Unidad" (SAP), "Cantidad" (World Office), "CANTIDAD" (Carvajal),
# "Cant." (HKA), "Cant" (Siigo del proveedor), "CANT" (papeleria propia).
RE_CABECERA_CANTIDAD = re.compile(r"^(?:cant|cantidad|cant\.|qty)", re.IGNORECASE)

# Donde termina la tabla de items y empiezan los totales
RE_FIN_TABLA = re.compile(
    r"(?i)^(subtotal|total|totales|son|valor\s+en\s+letras|impuestos|iva|"
    r"retefuente|reteiva|reteica|descuento|redondeo|base)\b")

TOLERANCIA_X = 45      # puntos de separacion contra el centro de la cabecera
TOLERANCIA_FILA = 4    # puntos para considerar dos palabras en la misma fila


def _numero_simple(bruto: str) -> float | None:
    """15 / 15.00 / 1,920.00 / 1.920,00 -> float."""
    texto = str(bruto or "").strip().rstrip(".,")
    if not re.fullmatch(r"[\d.,]+", texto):
        return None
    if "," in texto and "." in texto:
        # El separador decimal es el ultimo que aparece
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        partes = texto.split(",")
        texto = texto.replace(",", "." if len(partes[-1]) in (1, 2) else "")
    try:
        return float(texto)
    except ValueError:
        return None


def leer_cantidad_factura(ruta: str) -> dict:
    """Cantidad facturada, leida por la POSICION de la columna.

    Cada formato pone la cantidad en un sitio distinto, pero siempre bajo una
    cabecera que dice "Cantidad". Se ubica esa cabecera, se toma su centro
    horizontal y se leen los numeros que caen debajo y alineados con ella,
    parando donde empiezan los totales.
    """
    lineas: list[dict] = []
    cabeceras: list[str] = []

    with pdfplumber.open(ruta) as pdf:
        for indice, pagina in enumerate(pdf.pages, start=1):
            palabras = pagina.extract_words()
            if not palabras:
                continue

            for cabecera in palabras:
                if not RE_CABECERA_CANTIDAD.match(cabecera["text"].strip()):
                    continue
                # "TOTAL CANTIDADES:" no es la cabecera de la columna
                if cabecera["text"].strip().lower().startswith("cantidades"):
                    continue

                centro = (cabecera["x0"] + cabecera["x1"]) / 2
                cabeceras.append(cabecera["text"].strip())

                # Donde terminan los items y empiezan los totales.
                # Solo cuenta como corte la palabra que ABRE su fila: en unos
                # formatos la cabecera ocupa dos lineas ("VALOR TOTAL") y en
                # otros cada renglon lleva la palabra "IVA" en el medio.
                inicio_de_fila: dict[int, dict] = {}
                for palabra in palabras:
                    clave = round(palabra["top"] / TOLERANCIA_FILA)
                    if clave not in inicio_de_fila or \
                            palabra["x0"] < inicio_de_fila[clave]["x0"]:
                        inicio_de_fila[clave] = palabra

                limite = pagina.height
                for palabra in inicio_de_fila.values():
                    if palabra["top"] > cabecera["bottom"] + 10 and \
                            RE_FIN_TABLA.match(palabra["text"].strip()):
                        limite = min(limite, palabra["top"])

                candidatas = [
                    p for p in palabras
                    if cabecera["bottom"] + 1 < p["top"] < limite
                    and re.fullmatch(r"[\d.,]+", p["text"])
                    and abs((p["x0"] + p["x1"]) / 2 - centro) <= TOLERANCIA_X
                ]

                # Una cantidad por fila: la mas cercana al centro de la columna
                filas: dict[int, dict] = {}
                for p in candidatas:
                    clave = round(p["top"] / TOLERANCIA_FILA)
                    distancia = abs((p["x0"] + p["x1"]) / 2 - centro)
                    if clave not in filas or distancia < filas[clave]["distancia"]:
                        filas[clave] = {"palabra": p, "distancia": distancia}

                for datos in filas.values():
                    valor = _numero_simple(datos["palabra"]["text"])
                    if valor and valor > 0:
                        lineas.append({
                            "cantidad": valor,
                            "pagina": indice,
                            "texto": datos["palabra"]["text"],
                        })
                break      # una sola tabla de items por pagina

    # La factura puede repetir la misma pagina: no se suma dos veces
    unicas, vistas = [], set()
    for l in lineas:
        clave = (l["pagina"], l["texto"])
        if clave not in vistas:
            vistas.add(clave)
            unicas.append(l)

    return {
        "total": sum(l["cantidad"] for l in unicas) if unicas else None,
        "lineas": unicas,
        "cabecera": cabeceras[0] if cabeceras else "",
    }
