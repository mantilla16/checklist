"""Extraccion de texto y de montos aprobados desde PDF de correos / facturas.

Parte 1 del proyecto: obtener el "Valor segun aprobacion" (columna K del Excel)
a partir de la cola de correos en PDF. Un mismo pago puede venir repartido en
varias facturas, por lo que el extractor devuelve TODOS los candidatos con su
contexto y un puntaje, y la interfaz permite seleccionar y sumar los que aplican.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict

import logging

import pdfplumber

# pdfminer emite avisos de fuentes en PDF de Outlook que no afectan la extraccion
logging.getLogger("pdfminer").setLevel(logging.ERROR)


# --------------------------------------------------------------------------- #
# Utilidades de texto
# --------------------------------------------------------------------------- #

def sin_tildes(texto: str) -> str:
    """Normaliza a minusculas sin tildes para comparar palabras clave."""
    base = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in base if not unicodedata.combining(c))


# Un correo impreso a PDF y luego escaneado, o una captura pegada como imagen,
# no trae texto. A esta escala una pagina carta queda en unos 2500 px de ancho,
# que es donde el OCR deja de perder cifras en los montos.
ESCALA_OCR = 3


# --------------------------------------------------------------------------- #
# Espacios que el OCR se come
#
# Con un escaneo de baja resolucion el OCR lee bien las letras pero pega las
# palabras: "Aprobadapor$785.400". La busqueda del monto exige palabras enteras
# (asi "ok" no coincide dentro de "Outlook"), de modo que no reconoce
# "Aprobada" y el correo queda sin monto. No se afloja esa regla para todo el
# texto: se devuelven los espacios, y solo en las lineas que vienen de OCR y
# claramente estan pegadas.
# --------------------------------------------------------------------------- #

# Las palabras que usan la busqueda del monto y el veredicto, mas las que las
# rodean en un correo de aprobacion. Las de una letra no entran: partirian
# cualquier cosa.
VOCABULARIO_CORREO = sorted({
    "aprobada", "aprobado", "aprobadas", "aprobados", "aprobacion", "aprobar",
    "aprueba", "apruebo", "aprobamos", "autorizada", "autorizado",
    "autorizacion", "autorizo", "autoriza", "procedamos", "procedan", "proceder",
    "visto", "bueno", "acuerdo", "conforme", "confirmo", "por", "favor", "su",
    "tu", "sus", "nos", "valor", "total", "pagar", "pago", "pagos", "de", "del",
    "para", "con", "solicito", "agradezco", "requiero", "pendiente", "quedo",
    "atento", "atenta", "nota", "credito", "debito", "saldo", "retencion",
    "subtotal", "orden", "compra", "factura", "facturas", "buenos", "buenas",
    "dias", "tardes", "senor", "senora", "envio", "adjunto", "cancelar",
    "requisicion", "asunto", "enviado", "gracias", "cordial", "saludo",
}, key=len, reverse=True)


def _sin_tildes(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", texto)
                   if not unicodedata.combining(c))


def _separar_palabras(tramo: str) -> str:
    """Parte un tramo de letras pegadas en las palabras conocidas que contiene.

    Avanza de izquierda a derecha probando primero la palabra mas larga
    ("aprobada" antes que "a..."). Solo parte si el resultado es LIMPIO: todo
    palabras conocidas salvo, como mucho, un trozo desconocido. Eso separa
    "Aprobadapor" y "SenorHermes", pero deja entera "importancia", que daria
    "im por tancia": dos trozos desconocidos son senal de que la palabra era
    una sola y "por" solo estaba dentro.
    """
    comparar = _sin_tildes(tramo).lower()
    partes: list[str] = []
    desconocidos = 0
    resto = ""
    i = 0
    while i < len(tramo):
        hallada = next((v for v in VOCABULARIO_CORREO if comparar.startswith(v, i)), None)
        if hallada:
            if resto:
                partes.append(resto)
                desconocidos += 1
                resto = ""
            partes.append(tramo[i:i + len(hallada)])
            i += len(hallada)
        else:
            resto += tramo[i]
            i += 1
    if resto:
        partes.append(resto)
        desconocidos += 1

    conocidas = len(partes) - desconocidos
    if len(partes) < 2 or conocidas == 0 or desconocidos > 1:
        return tramo
    return " ".join(partes)


def _devolver_espacios(linea: str) -> str:
    """Devuelve a una linea de OCR los espacios que el OCR se comio."""
    # El signo de pesos pegado a la palabra anterior se separa siempre:
    # "por$785.400" no tiene otra lectura
    linea = re.sub(r"(?<=[^\W\d_])(?=\$)", " ", linea)

    # Las letras pegadas a cifras, solo si la linea viene pegada entera. En una
    # linea normal "FC91" u "OC20260331" son codigos y se dejan como estan.
    letras = sum(c.isalpha() for c in linea)
    if letras >= 6 and linea.count(" ") * 15 < letras:
        linea = re.sub(r"(?<=[^\W\d_])(?=\d)", " ", linea)
        linea = re.sub(r"(?<=\d)(?=[^\W\d_])", " ", linea)

    linea = re.sub(r"[^\W\d_]+", lambda m: _separar_palabras(m.group(0)), linea)
    return re.sub(r" {2,}", " ", linea).strip()


def _ocr_de_pagina(documento, indice: int) -> str:
    """Texto de una pagina sin capa de texto, reconstruido por lineas.

    Se rearma linea por linea, igual que lo entrega pdfplumber, porque la
    busqueda del monto aprobado trabaja por lineas: mira la frase que rodea
    al numero ("Aprobada por $ 62,029") y no solo el numero.
    """
    import numpy as np
    from lote_banco import en_lineas
    from radian import _motor

    imagen = np.array(documento[indice].render(scale=ESCALA_OCR).to_pil().convert("RGB"))
    resultado, _ = _motor()(imagen)
    palabras = []
    for caja, texto, _confianza in (resultado or []):
        if not str(texto).strip():
            continue
        xs = [p[0] for p in caja]
        ys = [p[1] for p in caja]
        palabras.append({"texto": str(texto).strip(), "x0": min(xs), "x1": max(xs),
                         "y0": min(ys), "y1": max(ys)})
    return "\n".join(_devolver_espacios(linea["texto"]) for linea in en_lineas(palabras))


# Una imagen pegada en el cuerpo del correo (la tabla que se aprueba, una
# captura de la factura). Mas chica que esto es un icono o una firma.
IMAGEN_MINIMA = (120, 18)      # puntos: ancho, alto


def _texto_de_imagenes(pagina_plumber, documento, indice: int) -> list[tuple[float, str]]:
    """Lineas de texto leidas con OCR DENTRO de las imagenes pegadas en una
    pagina que si tiene capa de texto. Devuelve (altura_en_puntos, linea).

    El correo de aprobacion puede traer lo aprobado como imagen ("Aprobada:"
    y debajo la tabla con la factura y el valor, pegada desde Excel): la capa
    de texto no la ve y el monto se perdia. El OCR se hace sobre la pagina
    completa (sobre el recorte solo no reconoce nada) y se conservan las
    cajas que caen dentro de las imagenes.
    """
    cajas_imagen = [
        (im["x0"], im["top"], im["x1"], im["bottom"]) for im in pagina_plumber.images
        if im["x1"] - im["x0"] >= IMAGEN_MINIMA[0] and im["bottom"] - im["top"] >= IMAGEN_MINIMA[1]
    ]
    if not cajas_imagen:
        return []
    import numpy as np
    from lote_banco import en_lineas
    from radian import _motor

    imagen = np.array(documento[indice].render(scale=ESCALA_OCR).to_pil().convert("RGB"))
    resultado, _ = _motor()(imagen)
    palabras = []
    for caja, texto, _confianza in (resultado or []):
        if not str(texto).strip():
            continue
        xs = [p[0] / ESCALA_OCR for p in caja]
        ys = [p[1] / ESCALA_OCR for p in caja]
        cx, cy = sum(xs) / 4, sum(ys) / 4
        if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in cajas_imagen):
            palabras.append({"texto": str(texto).strip(), "x0": min(xs), "x1": max(xs),
                             "y0": min(ys), "y1": max(ys)})
    lineas = []
    for linea in en_lineas(palabras):
        alturas = [w["y0"] for w in linea.get("palabras", [])] or [linea.get("y0", 0)]
        lineas.append((min(alturas), _devolver_espacios(linea["texto"])))
    return lineas


def _insertar_lineas_de_imagen(pagina_plumber, texto: str, leidas: list[tuple[float, str]]) -> str:
    """Mete las lineas leidas en la imagen en el texto de la pagina, debajo de
    la ultima linea de texto que esta por encima de ellas ("Aprobada:")."""
    if not leidas:
        return texto
    try:
        lineas_texto = pagina_plumber.extract_text_lines()
    except Exception:
        lineas_texto = []
    salida = texto.splitlines()
    for altura, linea in sorted(leidas, key=lambda l: l[0]):
        previa = None
        for lt in lineas_texto:
            if lt["top"] < altura and (previa is None or lt["top"] > previa["top"]):
                previa = lt
        posicion = len(salida)
        if previa:
            clave = previa["text"].strip()
            for i, l in enumerate(salida):
                if l.strip() == clave:
                    posicion = i + 1
                    break
        salida.insert(posicion, linea)
        # Las siguientes lineas de la misma imagen van debajo de esta
        lineas_texto = lineas_texto + [{"top": altura, "text": linea}]
    return "\n".join(salida)


def extraer_texto(ruta: str) -> tuple[list[str], bool]:
    """Devuelve (texto por pagina, requiere_ocr)."""
    paginas, sin_leer, _ = extraer_texto_con_ocr(ruta, usar_ocr=False)
    return paginas, sin_leer


def extraer_texto_con_ocr(ruta: str, usar_ocr: bool = True) -> tuple[list[str], bool, list[int]]:
    """Texto por pagina; las paginas sin capa de texto se leen con OCR.

    Devuelve (paginas, sigue_sin_texto, paginas_leidas_con_ocr). Las leidas con
    OCR se informan aparte para avisar que el monto viene de una lectura de
    imagen y conviene mirarlo dos veces.
    """
    paginas: list[str] = []
    con_imagenes: list[tuple[int, object]] = []
    with pdfplumber.open(ruta) as pdf:
        for i, pagina in enumerate(pdf.pages):
            texto = pagina.extract_text() or ""
            paginas.append(texto)
            if texto.strip() and pagina.images:
                con_imagenes.append((i, pagina))

        # Paginas con texto Y con imagenes pegadas: lo que dice la imagen
        # tambien cuenta (la tabla aprobada pegada desde Excel)
        if usar_ocr and con_imagenes:
            try:
                import pypdfium2 as pdfium
                documento = pdfium.PdfDocument(ruta)
                for i, pagina in con_imagenes:
                    leidas = _texto_de_imagenes(pagina, documento, i)
                    if leidas:
                        paginas[i] = _insertar_lineas_de_imagen(pagina, paginas[i], leidas)
            except Exception:
                pass    # sin OCR, el texto de la capa queda como estaba

    con_ocr: list[int] = []
    vacias = [i for i, texto in enumerate(paginas) if not texto.strip()]
    if usar_ocr and vacias:
        try:
            import pypdfium2 as pdfium
            documento = pdfium.PdfDocument(ruta)
            for indice in vacias:
                texto = _ocr_de_pagina(documento, indice)
                if texto.strip():
                    paginas[indice] = texto
                    con_ocr.append(indice + 1)
        except Exception:
            pass        # sin OCR disponible queda como antes: avisa que no hay texto

    sigue_sin_texto = not any(p.strip() for p in paginas)
    return paginas, sigue_sin_texto, con_ocr


# --------------------------------------------------------------------------- #
# Montos
# --------------------------------------------------------------------------- #

# $ 3,134,384  |  $150.000  |  2.984.384,00  |  1,786,190.00  |  COP 4.093.736
RE_MONTO = re.compile(
    r"(?:(?P<moneda>\$|COP|USD)\s*)?"
    r"(?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d{4,}(?:[.,]\d{1,2})?)"
    r"(?:\s*(?P<moneda2>COP|USD|pesos))?",
    re.IGNORECASE,
)

# "150 mil pesos", "3 millones"
RE_LETRAS = re.compile(r"\b(\d{1,3})\s*(mil|millones?)\s*(?:de\s*)?pesos?\b", re.IGNORECASE)

# Lineas de encabezado de correo: nunca contienen el monto aprobado
RE_ENCABEZADO = re.compile(
    r"^\s*(?:para|de|desde|cc|cco|bcc|to|from|sent|enviado|asunto|subject|fecha|date"
    r"|\d+\s+archivos?\s+adjuntos?|outlook)\s*:?",
    re.IGNORECASE,
)

# Lineas de firma / pie de correo
RE_FIRMA = re.compile(
    r"(?:phone|tel[eé]fonos?|www\.|facebook|http|@\w+\.\w+|cra\.|calle\s|\bkm\s|manzana"
    r"|zona\s+franca|horario\s+de"
    r"|\w\.(?:pdf|jpe?g|png|mp4|xlsx?|docx?|msg)\b(?![.:]))",  # lista de archivos adjuntos
    re.IGNORECASE,
)

# Caracteres que, pegados al numero, indican fecha, NIT, cuenta o codigo
BORDES_INVALIDOS = set("-/:@%°#*_")


# Sufijo contable de las notas debito/credito: "2,984,384.00CR"
RE_SUFIJO_CONTABLE = re.compile(r"^(?:CR|DB|COP|USD)(?![A-Za-z])", re.IGNORECASE)


def _es_monto_real(
    linea: str, m: re.Match, valor: float, tiene_moneda: bool
) -> tuple[bool, bool]:
    """Valida el número como monto. Devuelve (es_monto, tiene_moneda)."""
    ini, fin = m.span("num")
    antes = linea[ini - 1] if ini > 0 else " "
    despues = linea[fin] if fin < len(linea) else " "

    # 900555111-8, 2026/07/24, 787-590934-14, 1405051100-0345, 2.5%
    if antes in BORDES_INVALIDOS or despues in BORDES_INVALIDOS:
        return False, tiene_moneda
    if antes.isalnum():
        return False, tiene_moneda
    if despues.isalnum():
        # Se acepta solo si lo pegado es un marcador contable, no "1,768.00ROL"
        if not RE_SUFIJO_CONTABLE.match(linea[fin:fin + 4]):
            return False, tiene_moneda
        tiene_moneda = True

    bruto = m.group("num")
    tiene_separadores = bool(re.search(r"[.,]\d{3}", bruto))

    if not tiene_moneda:
        # Sin "$"/"COP" solo se acepta un numero con separadores de miles
        if not tiene_separadores:
            return False, tiene_moneda
        # 2.026 / 1.247 con separador serian montos validos, pero un año no
        if 1900 <= valor <= 2100:
            return False, tiene_moneda
    elif not tiene_separadores and valor < 10_000:
        # "$ 2026" suelto es casi siempre un año o un consecutivo
        return False, tiene_moneda

    return valor >= 1_000, tiene_moneda


def parsear_monto(bruto: str) -> float | None:
    """Convierte un numero con separadores mixtos (CO/US) a float."""
    texto = bruto.strip()
    if not re.fullmatch(r"[\d.,]+", texto):
        return None

    separadores = re.findall(r"[.,]", texto)
    if not separadores:
        return float(texto)

    ultimo = texto.rfind(separadores[-1])
    decimales = len(texto) - ultimo - 1
    # Solo es separador decimal si deja 1 o 2 digitos a la derecha
    if decimales in (1, 2) and len(separadores) >= 1:
        entero = re.sub(r"[.,]", "", texto[:ultimo])
        return float(f"{entero}.{texto[ultimo + 1:]}")
    return float(re.sub(r"[.,]", "", texto))


# Palabras que indican que el monto esta siendo aprobado / autorizado
CLAVES_APROBACION = {
    # "Aprobada por $ 62,029" es la aprobacion real; "su aprobacion por" es la
    # solicitud. Las formas en participio pesan igual o mas que el sustantivo.
    "aprobada": 12,
    "aprobado": 12,
    "aprobadas": 12,
    "aprobados": 12,
    "aprobacion": 10,
    "aprueba": 10,
    "apruebo": 10,
    "aprobar": 8,
    "autorizada": 11,
    "autorizado": 11,
    "autorizacion": 9,
    "autorizo": 9,
    "autoriza": 8,
    "procedamos": 9,
    "proceder": 6,
    "visto bueno": 9,
    "vo.bo": 9,
    "de acuerdo": 5,
    "ok": 4,
    "confirmo": 4,
    "por valor de": 6,
    "valor total": 5,
    "total a pagar": 7,
    "valor a pagar": 7,
    "cancelar": 3,
    "pago": 2,
}

# Contextos que normalmente NO son el valor aprobado del pago
CLAVES_RUIDO = {
    "telefono": -8,
    "phone": -8,
    "nit": -8,
    "cra.": -6,
    "calle": -6,
    "km": -6,
    "cuenta": -4,
    "codigo": -5,
    "und": -8,
    "unidades": -8,
    "rollos": -8,
    "cantidad": -6,
    "cant.": -6,
    "descargar": -6,
    "1435": -4,
    "2205": -4,
}

# FC91, NC54, FV149, SFX-743708  |  "Fra No 157215132", "Factura No. 4819"
RE_FACTURA = re.compile(
    r"\b(?:FC|NC|FV|FE|SETP|SFX)[- ]?\d{2,}\b"
    r"|\b(?:f(?:ra|actura|act)\.?|nota\s+cr[eé]dito|doc\.?)\s*"
    r"(?:electr[oó]nica\s+)?(?:de\s+venta\s+)?(?:no\.?|nro\.?|n[o°]\.?|#)?\s*:?\s*"
    r"(?P<id>[A-Z]{0,4}[- ]?\d{2,12})\b"
    # "No. FEMA 31891": sin la palabra factura, pero con la serie en letras
    r"|\bN[o°]\.?\s*(?P<id2>[A-Z]{2,5}[- ]?\d{3,12})\b",
    re.IGNORECASE,
)

RE_PREFIJO_FACTURA = re.compile(
    r"^(?:FRA|FACTURA|FACT|NOTA\s+CR[EÉ]DITO|DOC)\.?\s*(?:NO\.?|NRO\.?|N[O°]\.?|#)?\s*",
    re.IGNORECASE,
)


def numeros_factura(texto: str) -> list[str]:
    """Extrae identificadores de factura (evita telefonos, NIT y cantidades)."""
    hallados: list[str] = []
    for m in RE_FACTURA.finditer(texto):
        valor = (m.group("id") or m.group("id2") or m.group(0)).strip().upper()
        valor = RE_PREFIJO_FACTURA.sub("", valor).strip().replace(" ", "")
        if valor and not re.fullmatch(r"\d{1,3}", valor):
            hallados.append(valor)
    return hallados


@dataclass
class Candidato:
    valor: float
    texto_original: str
    linea: str
    contexto: str
    pagina: int
    puntaje: int
    claves: list[str] = field(default_factory=list)
    remitente: str = ""
    fecha: str = ""
    facturas: list[str] = field(default_factory=list)
    # El proveedor al que pertenece el monto, cuando el correo aprueba una
    # lista de varios ("INFOSISCO S A S" encima de su factura)
    tercero: str = ""
    # La linea que abre la lista ("Aprobadas:", "Por favor su aprobacion de:")
    encabezado: str = ""


RE_REMITENTE = re.compile(
    r"^(?:Desde|From|De)\s*:?\s*(.+?)\s*(?:<([^>]+)>)?\s*$", re.IGNORECASE
)
RE_ENVIADO = re.compile(r"^(?:Enviado(?:\s+el)?|Fecha|Sent)\s*:?\s*(.+)$", re.IGNORECASE)


def _bloques_correo(lineas: list[str]) -> list[tuple[int, str, str]]:
    """Ubica los encabezados de la cola de correos: (indice, remitente, fecha)."""
    bloques: list[tuple[int, str, str]] = []
    for i, linea in enumerate(lineas):
        m = RE_REMITENTE.match(linea.strip())
        if not m:
            continue
        remitente = (m.group(1) or "").strip()
        if len(remitente) > 90 or not remitente:
            continue
        fecha = ""
        for j in range(i + 1, min(i + 4, len(lineas))):
            mf = RE_ENVIADO.match(lineas[j].strip())
            if mf:
                fecha = mf.group(1).strip()
                break
        bloques.append((i, remitente, fecha))
    return bloques


RE_CABECERA_FECHA = re.compile(
    r"^(?:fecha|enviad[oa](?:\s+el)?|sent|date)\b.*\d{1,2}[/\-. ]\w+[/\-. ]\d{2,4}", re.IGNORECASE)
RE_CABECERA_PERSONAS = re.compile(r"^(?:para|to|cc|cco|bcc|asunto|subject)\s*:?\s+\S", re.IGNORECASE)


def _es_cabecera_de_correo(linea: str) -> bool:
    """"Fecha Mar 01/09/2026", "Para Libia ...", "De: ...". No la cabecera de
    una tabla pegada que empieza por FECHA ("FECHA FACT FACTURA N ...")."""
    return bool(RE_REMITENTE.match(linea) or RE_CABECERA_FECHA.match(linea)
                or RE_CABECERA_PERSONAS.match(linea))


def _es_nombre_de_tercero(linea: str) -> bool:
    """Una razon social en mayusculas: "CAMARA DE COMERCIO DE BARRANQUILLA".

    No un concepto ("INSCRIPCION ACTAS Y DOC. Poder" mezcla mayusculas y
    minusculas), ni una linea con cifras o montos.
    """
    limpio = linea.strip()
    if not (3 <= len(limpio) <= 90) or limpio.endswith(":"):
        return False
    if re.search(r"[\d$]", limpio) or RE_ENCABEZADO.match(limpio) or RE_FIRMA.search(limpio):
        return False
    letras = [c for c in limpio if c.isalpha()]
    if len(letras) < 4:
        return False
    # La cabecera de una tabla pegada ("FECHA FACT FACTURA N APELLIDOS Y
    # NOMBRES CANTIDAD VR TOTAL") tambien va en mayusculas, pero no es nadie
    palabras_tabla = {"FECHA", "FACTURA", "FACT", "CANTIDAD", "VALOR", "TOTAL", "NOMBRES",
                      "APELLIDOS", "DETALLE", "UND", "UNITARIO", "DESCRIPCION", "ITEM"}
    tokens = set(re.sub(r"[^A-Z ]", " ", sin_tildes(limpio).upper()).split())
    if len(tokens & palabras_tabla) >= 2:
        return False
    return sum(c.isupper() for c in letras) / len(letras) >= 0.9


def _tercero_de_linea(lineas: list[str], idx: int, inicio_bloque: int) -> str:
    """El nombre del proveedor mas cercano por encima del monto, dentro de la
    misma lista. Se saltan las lineas de concepto y las de otras facturas; se
    para en el encabezado de la lista o en el del correo."""
    for j in range(idx - 1, max(idx - 8, inicio_bloque, -1), -1):
        linea = lineas[j].strip()
        if not linea:
            continue
        if linea.endswith(":") or _es_cabecera_de_correo(linea):
            break
        if _es_nombre_de_tercero(linea):
            return linea
    return ""


def _encabezado_de_lista(lineas: list[str], idx: int, inicio_bloque: int) -> str:
    """La linea que abre la lista donde esta el monto ("Aprobadas:")."""
    for j in range(idx - 1, max(idx - 20, inicio_bloque, -1), -1):
        linea = lineas[j].strip()
        if _es_cabecera_de_correo(linea):
            break
        if linea.endswith(":") and len(linea) <= 120:
            return linea
    return ""


def _inicio_de_bloque(bloques, indice) -> int:
    inicio = -1
    for i, _r, _f in bloques:
        if i <= indice:
            inicio = i
        else:
            break
    return inicio


def _autor_de_linea(bloques, indice) -> tuple[str, str]:
    autor, fecha = "", ""
    for i, remitente, f in bloques:
        if i <= indice:
            autor, fecha = remitente, f
        else:
            break
    return autor, fecha


_CACHE_CLAVES: dict[str, re.Pattern] = {}


def _contiene(texto: str, clave: str) -> bool:
    """Busca la clave como palabra completa ('ok' no debe casar con 'Outlook')."""
    patron = _CACHE_CLAVES.get(clave)
    if patron is None:
        patron = re.compile(r"(?<!\w)" + re.escape(clave) + r"(?!\w)")
        _CACHE_CLAVES[clave] = patron
    return bool(patron.search(texto))


def _ventana_util(lineas: list[str], idx: int) -> str:
    """Contexto de +-2 lineas, sin encabezados ni firmas que ensucian la lectura."""
    trozos = [
        l.strip()
        for l in lineas[max(0, idx - 2): idx + 3]
        if l.strip() and not RE_ENCABEZADO.match(l) and not RE_FIRMA.search(l)
    ]
    return " ".join(trozos)


def buscar_candidatos(paginas: list[str]) -> list[Candidato]:
    """Encuentra montos con su contexto y los puntua como 'valor aprobado'."""
    candidatos: list[Candidato] = []

    for num_pagina, texto in enumerate(paginas, start=1):
        lineas = texto.splitlines()
        bloques = _bloques_correo(lineas)

        for idx, linea in enumerate(lineas):
            # Los encabezados y las firmas nunca traen el valor aprobado
            if RE_ENCABEZADO.match(linea) or RE_FIRMA.search(linea):
                continue

            ventana = _ventana_util(lineas, idx)
            norm_linea = sin_tildes(linea)
            norm_ventana = sin_tildes(ventana)

            encontrados: list[tuple[str, float, bool]] = []
            for m in RE_MONTO.finditer(linea):
                valor = parsear_monto(m.group("num"))
                if valor is None:
                    continue
                tiene_moneda = bool(m.group("moneda") or m.group("moneda2"))
                valido, tiene_moneda = _es_monto_real(linea, m, valor, tiene_moneda)
                if not valido:
                    continue
                encontrados.append((m.group(0).strip(), valor, tiene_moneda))
            for m in RE_LETRAS.finditer(linea):
                factor = 1_000 if m.group(2).lower().startswith("mil") else 1_000_000
                encontrados.append((m.group(0).strip(), float(m.group(1)) * factor, True))

            for original, valor, tiene_moneda in encontrados:
                puntaje = 0
                claves: list[str] = []

                for clave, peso in CLAVES_APROBACION.items():
                    if _contiene(norm_linea, clave):
                        puntaje += peso
                        claves.append(clave)
                    elif _contiene(norm_ventana, clave):
                        puntaje += max(1, peso // 2)
                        claves.append(f"{clave} (cerca)")
                for clave, peso in CLAVES_RUIDO.items():
                    if _contiene(norm_linea, clave):
                        puntaje += peso

                if tiene_moneda:
                    puntaje += 5
                if valor >= 100_000:
                    puntaje += 2

                # El numero de la propia linea manda; el de la ventana solo si
                # la linea no trae ninguno (en una lista, la ventana alcanza
                # la factura del proveedor de arriba)
                propias = [f for f in numeros_factura(linea) if f not in original]
                facturas = propias or [f for f in numeros_factura(ventana) if f not in original]
                autor, fecha = _autor_de_linea(bloques, idx)
                inicio_bloque = _inicio_de_bloque(bloques, idx)

                candidatos.append(
                    Candidato(
                        valor=valor,
                        texto_original=original,
                        linea=linea.strip(),
                        contexto=ventana.strip()[:400],
                        pagina=num_pagina,
                        puntaje=puntaje,
                        claves=sorted(set(claves)),
                        remitente=autor,
                        fecha=fecha,
                        facturas=sorted(set(facturas))[:5],
                        tercero=_tercero_de_linea(lineas, idx, inicio_bloque),
                        encabezado=_encabezado_de_lista(lineas, idx, inicio_bloque),
                    )
                )

    # Deduplica por (valor, linea) conservando el de mayor puntaje
    unicos: dict[tuple[float, str], Candidato] = {}
    for c in candidatos:
        llave = (c.valor, c.linea)
        if llave not in unicos or c.puntaje > unicos[llave].puntaje:
            unicos[llave] = c

    return sorted(unicos.values(), key=lambda c: (-c.puntaje, -c.valor))


# "APROBACION OC 20260354", "O.C. 20260204", "orden de compra No. 2026035"
RE_ORDEN_CORREO = re.compile(
    r"(?<![a-z0-9])(?:o\.?\s?c\.?|ordenes?\s+de\s+compra)\s*(?:no\.?|n°|#|:)?\s*(\d{6,12})(?!\d)")


def ordenes_del_correo(texto: str) -> list[str]:
    """O.C. que cita el correo, para saber a que facturas cubre su aprobado.

    Primero las del asunto, que es donde compras la escribe siempre ("RE:
    APROBACION OC 20260354 || ASEO"); si ningun asunto trae, las del cuerpo.
    """
    lineas = sin_tildes(texto or "").lower().splitlines()
    asuntos = [l for l in lineas if l.strip().startswith("asunto")]
    for fuente in (asuntos, lineas):
        ordenes = list(dict.fromkeys(m.group(1) for l in fuente for m in RE_ORDEN_CORREO.finditer(l)))
        if ordenes:
            return ordenes
    return []


def analizar(ruta: str, nombre: str, categoria: str) -> dict:
    """Analiza un PDF y devuelve el resumen listo para la interfaz."""
    paginas, requiere_ocr, paginas_ocr = extraer_texto_con_ocr(ruta)
    candidatos = buscar_candidatos(paginas)
    texto_completo = "\n".join(paginas)

    asunto = ""
    for linea in texto_completo.splitlines()[:12]:
        limpio = linea.strip()
        if limpio and limpio.lower() != "outlook":
            asunto = limpio
            break

    facturas = sorted(set(numeros_factura(texto_completo)))

    # Los de puntaje negativo son cantidades o codigos: se guardan aparte para
    # poder auditarlos, pero no se muestran en el listado principal.
    probables = [c for c in candidatos if c.puntaje >= 0]
    descartados = [c for c in candidatos if c.puntaje < 0]

    return {
        "nombre": nombre,
        "categoria": categoria,
        "paginas": len(paginas),
        "requiere_ocr": requiere_ocr,
        "paginas_ocr": paginas_ocr,
        "asunto": asunto,
        "facturas_detectadas": facturas[:20],
        "ordenes_compra": ordenes_del_correo(texto_completo),
        "candidatos": [asdict(c) for c in probables[:40]],
        "texto": texto_completo,
        "descartados": [asdict(c) for c in descartados[:20]],
        "sugerido": asdict(probables[0]) if probables and probables[0].puntaje > 0 else None,
    }
