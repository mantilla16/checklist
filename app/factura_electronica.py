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
    r"(?:factura\s+(?:electr[oó]nica\s+)?(?:de\s+venta\s+)?n[o°u]?\s*\.?\s*:?\s*"
    r"|Nro\.?\s*Doc\.?\s*:?\s*|No\.?\s*Factura\s*:?\s*|FE\s*No\.?\s*"
    # La consulta del documento en el portal del proveedor: "Nro de documento:"
    # El dos puntos es obligatorio: sin el, "Numero de documento" es el titulo
    # de una columna y lo que sigue no es el numero
    r"|(?:Nro|N[uú]mero)\.?\s+de\s+documento\s*:\s*)"
    # World Office imprime "Factura Electronica De Venta No\n. FWS No. 5296":
    # la serie y un segundo "No." antes de las cifras
    r"([A-Z]{0,6}[-\s]?(?:No\.?\s*)?\d{1,12})",
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


# Rango de numeracion autorizado por la DIAN, que toda factura electronica
# imprime: "prefijo FV desde el numero 2001 al 3000", "habilita desde FWS 5001
# hasta FWS 8000", "prefijo MS desde el numero 1 al 8000". Es la pista mas
# firme para reconocer el numero sin depender de donde lo ponga cada formato:
# el numero de la factura lleva ese prefijo y cae dentro del rango.
RE_RANGO_DIAN = re.compile(
    r"(?:prefijo\s+(?P<p1>[A-Z]{1,6})\b.{0,60}?)?"
    r"desde\s+(?:el\s+)?(?:n[uú]mero\s+)?(?:(?P<p2>[A-Z]{1,6})\s*-?\s*)?(?P<desde>\d{1,10})"
    r"\s+(?:al|hasta)\s+(?:el\s+)?(?:n[uú]mero\s+)?(?:(?P<p3>[A-Z]{1,6})\s*-?\s*)?(?P<hasta>\d{1,10})",
    re.IGNORECASE | re.DOTALL)

# "No. FV 2295", "Nº FE14733", "No.: 346". Un numero de factura suele ir
# detras de un "No."; el contexto dice si es el de la factura u otro.
RE_NO_GENERICO = re.compile(
    r"\bN[o°º]\.?\s*:?\s*(?P<pref>[A-Z]{1,6})?[-\s]?(?P<num>\d{1,12})\b", re.IGNORECASE)
# Lo que precede a un "No." que NO es la factura
RE_OTRO_NO = re.compile(
    r"(?:cuota|cuenta|cta|orden|pedido|o\.?\s?c|remisi[oó]n|autorizaci[oó]n|resoluci[oó]n"
    r"|tel[eé]fono|cel|nit|cc|c\.c|documento\s+de\s+identidad|gu[ií]a|contrato|poliza|p[oó]liza)"
    r"[^\n]{0,25}$", re.IGNORECASE)


def rangos_dian(texto: str) -> list[tuple[str, int, int]]:
    """[(prefijo, desde, hasta)] de la autorizacion de numeracion."""
    rangos = []
    for m in RE_RANGO_DIAN.finditer(texto):
        prefijo = (m.group("p2") or m.group("p3") or m.group("p1") or "").upper()
        try:
            desde, hasta = int(m.group("desde")), int(m.group("hasta"))
        except ValueError:
            continue
        if 0 <= desde < hasta:
            rangos.append((prefijo, desde, hasta))
    return rangos


def _numero_del_nombre(nombre: str) -> list[tuple[str, str]]:
    """Pistas en el nombre del archivo: "Fra No MS 3906.pdf" -> ("MS", "3906"),
    "FRA FE14733 METROLOGIA.pdf" -> ("FE", "14733")."""
    tallo = re.sub(r"\.[A-Za-z0-9]+$", "", nombre or "")
    tallo = re.sub(r"(?i)\b(?:factura|fra|fact|no|nro|n[o°º]|de|electr[oó]nica|venta|copia)\b\.?", " ", tallo)
    pistas = []
    for m in re.finditer(r"\b([A-Z]{1,6})?[\s_-]?(\d{2,12})\b", tallo):
        pistas.append(((m.group(1) or "").upper(), m.group(2)))
    return pistas


def _con_prefijo(prefijo: str, digitos: str) -> str:
    return (prefijo or "").upper() + digitos


def leer_numero(texto: str, campos_qr: dict, esperado: str = "", nombre: str = "") -> dict:
    """El numero de la factura, por varias pistas que se suman, no por una
    posicion fija: cada formato lo pone en otro sitio ("No. FV 2295" en la
    otra columna, "Factura ... No\n. FWS No. 5296", en la otra linea...).

    Pistas, de mas a menos firme: el QR de la DIAN; el numero esperado (el
    que cito el egreso) impreso en el documento; un numero con el prefijo del
    rango autorizado y dentro de el; un "No." junto a la palabra factura; el
    nombre del archivo. Se escoge el candidato con mas puntos.
    """
    del_qr = campos_qr.get("numfac", "")
    if del_qr:
        # Algunos generadores rellenan con 0xFF ("ÿÿÿÿ4719"): solo ASCII
        limpio = re.sub(r"[^A-Za-z0-9-]", "", del_qr)
        if limpio:
            return {"valor": limpio, "fuente": "QR"}

    rangos = rangos_dian(texto)
    esperado_digitos = solo_digitos(esperado).lstrip("0")
    lineas = texto.splitlines()
    inicio_linea = []
    acumulado = 0
    for l in lineas:
        inicio_linea.append(acumulado)
        acumulado += len(l) + 1

    def linea_de(posicion: int) -> int:
        i = 0
        while i + 1 < len(inicio_linea) and inicio_linea[i + 1] <= posicion:
            i += 1
        return i

    def cerca_de_factura(posicion: int) -> bool:
        i = linea_de(posicion)
        vecinas = " ".join(lineas[max(0, i - 2): i + 2]).lower()
        return "factura" in vecinas or "invoice" in vecinas

    candidatos: dict[str, dict] = {}

    def sumar(prefijo: str, digitos: str, puntos: int, motivo: str, posicion: int):
        if not digitos or len(digitos) > 12:
            return
        if not prefijo and len(digitos) == 4 and 1900 <= int(digitos) <= 2100:
            return      # un año suelto
        clave = _con_prefijo(prefijo, digitos.lstrip("0") or digitos)
        c = candidatos.setdefault(clave, {"valor": _con_prefijo(prefijo, digitos),
                                          "puntos": 0, "motivos": [], "posicion": posicion})
        c["puntos"] += puntos
        c["motivos"].append(motivo)
        c["posicion"] = min(c["posicion"], posicion)

    # 1) La etiqueta clasica en la misma linea
    for m in RE_NUMERO.finditer(texto):
        crudo = re.sub(r"\s+", "", re.sub(r"(?i)\bNo\.?", "", m.group(1)))
        mp = re.fullmatch(r"([A-Za-z]*)-?(\d+)", crudo)
        if mp:
            sumar(mp.group(1), mp.group(2), 4, "etiqueta de factura", m.start())

    # 2) Cualquier "No. XXX 123" cuyo contexto no diga que es otra cosa
    for m in RE_NO_GENERICO.finditer(texto):
        antes = texto[max(0, m.start() - 40): m.start()]
        if RE_OTRO_NO.search(antes):
            continue
        puntos = 1 + (2 if cerca_de_factura(m.start()) else 0)
        sumar(m.group("pref") or "", m.group("num"), puntos, "detras de un No.", m.start())

    # 3) El prefijo autorizado por la DIAN, con el numero dentro del rango
    for prefijo, desde, hasta in rangos:
        if not prefijo:
            continue
        for m in re.finditer(r"(?<![A-Z])" + re.escape(prefijo) + r"\s*-?\s*(\d{1,10})\b", texto):
            n = int(m.group(1))
            if desde <= n <= hasta and n not in (desde, hasta):
                sumar(prefijo, m.group(1), 5, f"prefijo {prefijo} en el rango autorizado", m.start())

    # 4) El numero que cito el egreso, impreso en la factura
    if esperado_digitos:
        for m in re.finditer(r"(?<!\d)0*" + re.escape(esperado_digitos) + r"(?!\d)", texto):
            antes = texto[max(0, m.start() - 8): m.start()]
            mp = re.search(r"([A-Z]{1,6})\s*-?\s*$", antes)
            sumar(mp.group(1) if mp else "", esperado_digitos, 6, "coincide con el egreso", m.start())

    # 5) El nombre del archivo, si ese numero esta impreso en el documento
    for prefijo, digitos in _numero_del_nombre(nombre):
        if re.search(r"(?<!\d)0*" + re.escape(digitos.lstrip("0") or digitos) + r"(?!\d)", texto):
            sumar(prefijo, digitos, 2, "nombre del archivo", len(texto))

    # Coherencia con el rango: suma si cae dentro, resta si contradice
    for c in candidatos.values():
        mp = re.fullmatch(r"([A-Z]*)(\d+)", c["valor"])
        if not mp or not rangos:
            continue
        prefijo, n = mp.group(1), int(mp.group(2))
        for r_prefijo, desde, hasta in rangos:
            if prefijo == r_prefijo and desde <= n <= hasta:
                if "rango" not in " ".join(c["motivos"]):
                    c["puntos"] += 3
                    c["motivos"].append("dentro del rango autorizado")
            elif prefijo == r_prefijo:
                c["puntos"] -= 3
            elif r_prefijo and prefijo and prefijo != r_prefijo:
                c["puntos"] -= 1

    if not candidatos:
        return {"valor": "", "fuente": ""}
    mejor = max(candidatos.values(), key=lambda c: (c["puntos"], -c["posicion"]))
    if mejor["puntos"] < 2:
        return {"valor": "", "fuente": ""}
    return {"valor": mejor["valor"], "fuente": "texto (" + ", ".join(dict.fromkeys(mejor["motivos"])) + ")"}


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
        # Antes del NIT: la etiqueta de su recuadro ("CLIENTE ... NIT 901.."").
        # Despues: solo lo que queda en la MISMA linea. La linea siguiente ya
        # es otro recuadro: "Nit 900943984 3 ...\nCLIENTE AB MARINE" marcaba
        # al emisor como adquirente por el "CLIENTE" de la linea de abajo.
        resto_linea = minuscula[m.end(): m.end() + 60].split("\n", 1)[0]
        ventana = minuscula[max(m.start() - 160, 0): m.start()] + " " + resto_linea
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

    # 3) El NIT del recuadro del cliente. Es el que hay que revisar: el que la
    # propia factura dice que compro. Se prefiere a un hallazgo suelto en el
    # texto, donde el mismo numero puede aparecer por otros motivos.
    fuente_adquirente = "QR" if adquirente else ""
    if not adquirente:
        # Si hay varios con etiqueta de cliente, el que coincide con el esperado
        recuadros = [c["nit"] for c in candidatos if c["papel"] == "adquirente"]
        del_recuadro = next((n for n in recuadros if nit_igual(n, nit_cliente)),
                            recuadros[0] if recuadros else "")
        if del_recuadro:
            adquirente = del_recuadro
            fuente_adquirente = "recuadro del cliente"

    # 4) Sin recuadro reconocible, basta con que el NIT esperado este impreso
    if not adquirente and nit_cliente and aparece_nit(texto, nit_cliente):
        adquirente = solo_digitos(nit_cliente)
        fuente_adquirente = "texto sin etiqueta"

    return {
        "emisor": emisor,
        "adquirente": adquirente,
        "fuente": fuente or fuente_adquirente,
        "fuente_adquirente": fuente_adquirente,
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
        # Los enlaces no salen en el texto. En la consulta del portal el CUFE
        # impreso queda cortado por el borde de la pagina (80 de sus 96
        # caracteres), pero el enlace a la validacion de la DIAN lo trae entero
        enlaces = [h.get("uri") or "" for pagina in pdf.pages
                   for h in (pagina.hyperlinks or [])]
    texto = "\n".join(paginas)
    sin_texto = not texto.strip()

    qr = leer_qr(ruta)
    enlace_dian = next((e for e in enlaces if RE_URL_DIAN.search(e)), "")
    if not qr["presente"] and enlace_dian:
        # Sin imagen QR pero con el enlace de validacion de la DIAN, que es
        # exactamente lo que codifica el QR de una factura electronica. Se da
        # por presente y se dice de donde salio, para que no pase por un QR.
        qr = {**qr, "presente": True, "metodo": "enlace DIAN (sin imagen QR)",
              "contenido": enlace_dian}
    cufe = leer_cufe(texto)
    if not cufe["presente"] and enlace_dian:
        cufe = {**leer_cufe(enlace_dian), "fuente": "enlace DIAN del PDF"}
    # Una factura escaneada no tiene texto, pero su QR de la DIAN trae el CUFE
    # entero en el campo "CUFE"; antes solo se buscaba en el texto
    del_qr = str((qr.get("campos") or {}).get("cufe", "")).strip()
    if not cufe["presente"] and re.fullmatch(r"[0-9a-fA-F]{96}", del_qr):
        cufe = {"presente": True, "tipo": "CUFE", "valor": del_qr.lower(),
                "fuente": "QR"}
    try:
        cantidad = leer_cantidad_factura(ruta)
    except Exception:
        cantidad = {"total": None, "lineas": [], "cabecera": ""}
    numero = leer_numero(texto, qr["campos"], esperado.get("numero", ""), nombre)
    nits = leer_nits(texto, qr["campos"], esperado)

    nit_cliente = esperado.get("nit_cliente") or ""
    nit_tercero = esperado.get("nit_tercero") or ""

    # Si todavia no se sabe contra que comparar, la revision queda en None y no
    # en False: eso es "no se puede comprobar", no "esta mal". El NIT del
    # cliente sale del egreso y el del tercero de la tabla azul, asi que
    # mientras no esten cargados no hay nada que revisar, y marcarlos como
    # fallidos hace que la factura parezca defectuosa.
    if not nit_cliente:
        cliente_ok = None
    elif nits["adquirente"] and nits["fuente_adquirente"] != "texto sin etiqueta":
        # Se sabe a quien dice la factura que le vendio: se comparan los dos.
        # Aqui NO vale ademas el "aparece en algun sitio del texto": una
        # factura emitida a otra empresa puede mencionar de paso el NIT
        # correcto, y eso taparia justo el error que se esta buscando.
        cliente_ok = nit_igual(nits["adquirente"], nit_cliente)
    else:
        # No se pudo leer a quien le vendio: si el NIT esperado aparece, vale;
        # si no aparece en ninguna parte no se puede afirmar que no coincide,
        # solo que no se pudo comprobar. Hay formatos, como la consulta del
        # documento en el portal del proveedor, que no traen el comprador.
        # Una factura emitida a OTRA empresa si se detecta: trae el NIT de
        # esa otra en el recuadro del cliente y cae en la rama de arriba.
        cliente_ok = True if aparece_nit(texto, nit_cliente) else None
    tercero_ok = None if not nit_tercero else (
        nit_igual(nits["emisor"], nit_tercero) or aparece_nit(texto, nit_tercero)
    )
    # Solo si se pudo leer. Antes tambien se comparaba con el numero del
    # renglon, y cuando ese lo habia puesto el egreso (que cita el registro
    # contable P, no la factura) salia "no se pudo leer el numero" con el
    # numero bien leido a la vista. La comparacion con el egreso la hace la
    # cadena egreso -> P -> factura, y el emparejamiento con el renglon.
    numero_ok = bool(numero["valor"])

    revisiones = {
        "nit_cliente": cliente_ok,
        "nit_tercero": tercero_ok,
        "cufe": cufe["presente"],
        "qr": qr["presente"],
        "numero": numero_ok,
    }
    faltantes = [clave for clave, ok in revisiones.items() if ok is False]
    pendientes = [clave for clave, ok in revisiones.items() if ok is None]

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
        "pendientes": pendientes,
        # Columnas del papel de trabajo
        "columnas": {
            "M": "OK" if not faltantes and not pendientes else "",
            "N": numero["valor"],
            # Si el NIT de la factura y el de la tabla azul son el mismo pero
            # escritos distinto (con o sin DV), se escribe el de la tabla azul
            # para que la comparacion en el Excel sea directa.
            "O": (solo_digitos(nit_tercero)
                  if tercero_ok and nit_tercero else nits["emisor"]),
            # None cuando no hay tabla azul contra la que comparar: la celda
            # queda vacia en vez de decir FALSO
            "P": tercero_ok,
        },
    }


def _diferencias(uno: str, otro: str) -> int:
    """Cuantos digitos difieren entre dos numeros de la misma longitud."""
    if len(uno) != len(otro):
        return 99
    return sum(1 for a, b in zip(uno, otro) if a != b)


def revisar_contra_egreso(analisis: dict, par: dict, numeros_egreso: dict) -> str:
    """Compara el numero leido en la factura con el que escribio el egreso.

    Es la revision de la columna N. Se hace despues de emparejar porque hasta
    entonces no se sabe a que renglon pertenece la factura, y se hace SIEMPRE:
    antes solo se comparaba cuando el registro tenia una unica factura, con lo
    que en un pago con varias (el caso de gases) bastaba con que el numero
    fuera legible.

    Modifica el analisis en el sitio y devuelve la observacion, si hay.
    """
    leido = analisis["columnas"]["N"]
    del_egreso = (numeros_egreso or {}).get(par.get("factura") or "") or ""

    if not del_egreso:
        return ""      # el egreso no relaciona esta factura: nada que comparar

    if _normalizar_numero(leido) == _normalizar_numero(del_egreso):
        return ""

    # Solo el numero casi igual es un error seguro (digitacion, como 127212397
    # por 157212397). Si el egreso cita algo del todo distinto suele ser el
    # consecutivo del registro contable P, no la factura, y eso solo se puede
    # juzgar con el P a la vista: lo resuelve la interfaz en el paso 4.
    if _diferencias(_normalizar_numero(del_egreso), _normalizar_numero(leido)) > 2:
        return ""

    # Marca propia y no la de "numero": esa significa que no se pudo leer el
    # numero, y decir "falta: numero de factura" cuando el numero esta impreso
    # y lo que pasa es que el egreso escribio otro es enganoso.
    analisis["revisiones"]["numero_egreso"] = False
    if "numero_egreso" not in analisis["faltantes"]:
        analisis["faltantes"] = sorted(analisis["faltantes"] + ["numero_egreso"])
    analisis["columnas"]["M"] = ""
    return f"el egreso dice {del_egreso} y la factura dice {leido}"


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
    r"retefuente|reteiva|reteica|descuento|redondeo|base|observaciones)\b")

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
                # Se compara el renglon ENTERO desde su primera palabra: el
                # corte puede ser de varias palabras ("VALOR EN LETRAS") y,
                # palabra por palabra, nunca coincidia; entonces se colaban
                # como cantidades los numeros del texto legal de la ultima
                # pagina ("Ley 1231 de 2008", "vigente 2 Años").
                por_fila: dict[int, list[dict]] = {}
                for palabra in palabras:
                    por_fila.setdefault(round(palabra["top"] / TOLERANCIA_FILA), []).append(palabra)

                limite = pagina.height
                for fila_palabras in por_fila.values():
                    fila_palabras.sort(key=lambda p: p["x0"])
                    primera = fila_palabras[0]
                    texto_fila = " ".join(p["text"] for p in fila_palabras).strip()
                    if primera["top"] > cabecera["bottom"] + 10 and \
                            RE_FIN_TABLA.match(texto_fila):
                        limite = min(limite, primera["top"])

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

                for clave, datos in filas.items():
                    valor = _numero_simple(datos["palabra"]["text"])
                    if valor and valor > 0:
                        # El resto del renglon: la descripcion dice el empaque
                        # ("PH.JUMBO SCOTT BCO 4 ROLLOS X 250 MTS")
                        # Sin el codigo del producto ni los precios y el IVA
                        resto = [p["text"] for p in sorted(por_fila.get(clave, []),
                                                           key=lambda p: p["x0"])
                                 if p is not datos["palabra"]
                                 and not re.fullmatch(r"\d{7,}", p["text"])
                                 and not re.fullmatch(r"\$?[\d.,]*[.,][\d.,]*", p["text"])]
                        lineas.append({
                            "cantidad": valor,
                            "pagina": indice,
                            "texto": datos["palabra"]["text"],
                            "descripcion": " ".join(resto)[:160],
                        })
                break      # una sola tabla de items por pagina

    # La factura puede repetir la misma pagina: no se suma dos veces. Se
    # compara la pagina COMPLETA, no cada cantidad: dos items de 500 en la
    # misma factura son dos items (500 + 506 + 500 = 1506, no 1006).
    por_pagina: dict[int, list[dict]] = {}
    for l in lineas:
        por_pagina.setdefault(l["pagina"], []).append(l)
    unicas, vistas = [], set()
    for pagina in sorted(por_pagina):
        firma = tuple(l["texto"] for l in por_pagina[pagina])
        if firma not in vistas:
            vistas.add(firma)
            unicas.extend(por_pagina[pagina])

    return {
        "total": sum(l["cantidad"] for l in unicas) if unicas else None,
        "lineas": unicas,
        "cabecera": cabeceras[0] if cabeceras else "",
    }
