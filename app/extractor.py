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


def extraer_texto(ruta: str) -> tuple[list[str], bool]:
    """Devuelve (texto por pagina, requiere_ocr)."""
    paginas: list[str] = []
    with pdfplumber.open(ruta) as pdf:
        for pagina in pdf.pages:
            paginas.append(pagina.extract_text() or "")
    con_texto = sum(1 for p in paginas if p.strip())
    return paginas, con_texto == 0


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
    r"|\.(?:pdf|jpe?g|png|mp4|xlsx?|docx?|msg)\b)",  # lista de archivos adjuntos
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
    r"(?:electr[oó]nica\s+)?(?:no\.?|nro\.?|n[o°]\.?|#)?\s*(?P<id>[A-Z]{0,4}-?\d{2,12})\b",
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
        valor = (m.group("id") or m.group(0)).strip().upper()
        valor = RE_PREFIJO_FACTURA.sub("", valor).strip()
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

                facturas = [f for f in numeros_factura(ventana) if f not in original]
                autor, fecha = _autor_de_linea(bloques, idx)

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
                    )
                )

    # Deduplica por (valor, linea) conservando el de mayor puntaje
    unicos: dict[tuple[float, str], Candidato] = {}
    for c in candidatos:
        llave = (c.valor, c.linea)
        if llave not in unicos or c.puntaje > unicos[llave].puntaje:
            unicos[llave] = c

    return sorted(unicos.values(), key=lambda c: (-c.puntaje, -c.valor))


def analizar(ruta: str, nombre: str, categoria: str) -> dict:
    """Analiza un PDF y devuelve el resumen listo para la interfaz."""
    paginas, requiere_ocr = extraer_texto(ruta)
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
        "asunto": asunto,
        "facturas_detectadas": facturas[:20],
        "candidatos": [asdict(c) for c in probables[:40]],
        "texto": texto_completo,
        "descartados": [asdict(c) for c in descartados[:20]],
        "sugerido": asdict(probables[0]) if probables and probables[0].puntaje > 0 else None,
    }
