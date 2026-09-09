"""Lectura de comprobantes de egreso (Siigo) para armar el lote.

De estos documentos sale la informacion de las columnas B a G del papel de
trabajo. Formato tipico:

    EMPRESA EJEMPLO S.A.S. NIT : 900123456 -0
    TRANSFERENCIAS BANCARIAS G-002-00202609003
    | Girado a : PROVEEDOR TRES SAS       FECHA : 2026/09/01 |
    | NIT : 901,333,444-5                                           |
    | Banco : BANCOLOMBIA CTA CTE No. 000-00  C.Costo : 0001 100 ... |
    | LA SUMA DE : SETECIENTOS SESENTA Y SEIS MIL ...  |            |
    |                                                  | 766,590.00 |
    | 2205050100 - CANCELA FACTURA No. 00000000091-001 | 766,590.00 |
    | Girado :                                         | 766,590.00 |
"""

from __future__ import annotations

import re

RE_TIPO = re.compile(
    r"(TRANSFERENCIAS?\s+BANCARIAS?|COMPROBANTE\s+DE\s+EGRESO|EGRESO"
    r"|NOTA\s+DEBITO\s+PROVEEDOR|NOTA\s+CREDITO\s+PROVEEDOR|CHEQUE"
    r"|[A-Z\s]*COMPRAS[A-Z\s]*|ENTRADA[A-Z\s]*|ORDEN\s+DE\s+COMPRA[A-Z\s]*)"
    r"\s+((?:[A-Z]-?\d{3}-?\d{6,})|[A-Z]?\d{6,})",
    re.IGNORECASE,
)

# El documento P (registro contable de compras) usa "A Favor de" y "Valor :"
RE_A_FAVOR = re.compile(r"A\s+Favor\s+de\s*:\s*(.+?)\s*(?:\||Fecha\s*:|$)", re.IGNORECASE)
RE_CEDULA_NIT = re.compile(r"C[eé]dula\s*/\s*NIT\s*:\s*([\d][\d.,\s-]{5,18})", re.IGNORECASE)
RE_VALOR_ENCABEZADO = re.compile(r"Valor\s*:\s*([\d.,]+)", re.IGNORECASE)
RE_TOTAL_SIIGO = re.compile(r"T\s?o\s?t\s?a\s?l\s*\|?\s*([\d.,]+)", re.IGNORECASE)
RE_FACT_NO = re.compile(
    r"Fact\.?\s*No\.?\s*:?\s*(?:\d{3}\s*-\s*)?0*(\d{1,12})", re.IGNORECASE)
RE_ORDEN_COMPRA = re.compile(r"\bO\.?\s?C\.?\s+(\d{4,12})", re.IGNORECASE)
RE_ORDEN_INTERNA = re.compile(r"\bOrden\s*:\s*(\d{4,12})", re.IGNORECASE)
# "FRA FC91" y tambien "FRA No 157212397": el "No" no hace parte del numero
RE_FRA_ETIQUETA = re.compile(
    r"\bFRA\s+(?:N[o°]\.?\s*)?([A-Z]{0,5}[-\s]?\d{1,12})", re.IGNORECASE)
RE_GIRADO_A = re.compile(
    r"Girado\s*a\s*:\s*(.+?)\s{2,}(?:FECHA|$)|Girado\s*a\s*:\s*(.+?)\s+FECHA\s*:",
    re.IGNORECASE,
)
RE_FECHA = re.compile(r"FECHA\s*:\s*(\d{4}[/-]\d{2}[/-]\d{2}|\d{2}[/-]\d{2}[/-]\d{4})",
                      re.IGNORECASE)
RE_NIT = re.compile(r"\bNIT\s*:\s*([\d][\d.,\s]{6,20}(?:-\s?\d)?)", re.IGNORECASE)
RE_BANCO = re.compile(r"Banco\s*:\s*(.+?)(?:\s{2,}|\s*C\.?\s?Costo|\s*Cheque|$)",
                      re.IGNORECASE)
RE_GIRADO_TOTAL = re.compile(r"Girado\s*:\s*\|?\s*([\d.,]+)", re.IGNORECASE)
RE_SUMA_LETRAS = re.compile(r"LA\s+SUMA\s+DE\s*:\s*(.+?)(?:\s*\||$)", re.IGNORECASE)
RE_CANCELA = re.compile(
    r"(?:CANCELA|ABONO|PAGO)\s+(?:A\s+)?(?:LA\s+)?"
    r"FACTURA[S]?\s*(?:No\.?|Nro\.?|#)?\s*([\w-]+)",
    re.IGNORECASE,
)
RE_MONTO_TABLA = re.compile(r"\|\s*([\d]{1,3}(?:,\d{3})*\.\d{2}|[\d]{1,3}(?:\.\d{3})*,\d{2})\s*\|")


def _numero(bruto: str) -> float | None:
    """Convierte 4,093,736.00 o 4.093.736,00 a float."""
    texto = bruto.strip()
    if not re.fullmatch(r"[\d.,]+", texto):
        return None
    separadores = re.findall(r"[.,]", texto)
    if not separadores:
        return float(texto)
    ultimo = texto.rfind(separadores[-1])
    if len(texto) - ultimo - 1 in (1, 2):
        entero = re.sub(r"[.,]", "", texto[:ultimo])
        return float(f"{entero}.{texto[ultimo + 1:]}")
    return float(re.sub(r"[.,]", "", texto))


def _nit_limpio(bruto: str) -> str:
    """900,555,111-8 -> 900555111 (sin digito de verificacion)."""
    solo = re.sub(r"[^\d-]", "", bruto)
    return solo.split("-")[0]


def _factura_limpia(bruto: str) -> str:
    """00000000091-001 -> 91 ; mantiene los alfanumericos como vienen."""
    base = bruto.strip().upper().split("-")[0]
    if base.isdigit():
        return str(int(base))
    return base


def analizar_comprobante(texto: str) -> dict | None:
    """Extrae los datos del lote de un comprobante. None si no lo es."""
    tipo = RE_TIPO.search(texto)
    girado = RE_GIRADO_A.search(texto)
    if not (tipo or girado):
        return None

    lineas = texto.splitlines()

    titular = ""
    if girado:
        titular = (girado.group(1) or girado.group(2) or "").strip()
        titular = re.sub(r"\s*\|.*$", "", titular).strip()

    # El primer NIT del documento es el de la empresa que paga; el del
    # beneficiario viene despues de "Girado a".
    nits = [m for m in RE_NIT.finditer(texto)]

    # El primer NIT es el de la empresa que paga: ese es el NIT del CLIENTE
    # que despues hay que validar en la factura electronica.
    nit_empresa = _nit_limpio(nits[0].group(1)) if nits else ""
    documento = ""
    if girado:
        posteriores = [m for m in nits if m.start() > girado.start()]
        if posteriores:
            documento = _nit_limpio(posteriores[0].group(1))
    if not documento and len(nits) > 1:
        documento = _nit_limpio(nits[1].group(1))

    # Valor girado: la linea "Girado :" es el total del comprobante
    valor = None
    total = RE_GIRADO_TOTAL.search(texto)
    if total:
        valor = _numero(total.group(1))
    if valor is None:
        montos = [_numero(m.group(1)) for m in RE_MONTO_TABLA.finditer(texto)]
        montos = [m for m in montos if m]
        valor = max(montos) if montos else None

    banco = ""
    m_banco = RE_BANCO.search(texto)
    if m_banco:
        banco = re.sub(r"\s*\|.*$", "", m_banco.group(1)).strip()

    fecha = ""
    m_fecha = RE_FECHA.search(texto)
    if m_fecha:
        fecha = m_fecha.group(1).replace("/", "-")
        if re.match(r"^\d{2}-", fecha):          # dd-mm-yyyy -> yyyy-mm-dd
            dia, mes, anio = fecha.split("-")
            fecha = f"{anio}-{mes}-{dia}"

    suma_letras = ""
    m_letras = RE_SUMA_LETRAS.search(texto)
    if m_letras:
        suma_letras = m_letras.group(1).strip()

    facturas = []
    for m in RE_CANCELA.finditer(texto):
        limpia = _factura_limpia(m.group(1))
        if limpia and limpia not in facturas:
            facturas.append(limpia)

    # Conceptos de la tabla: "2205050100 - CANCELA FACTURA No. ... | 766,590.00"
    conceptos = []
    for linea in lineas:
        if "|" not in linea:
            continue
        partes = [p.strip() for p in linea.strip("|").split("|")]
        if len(partes) < 2 or not partes[0]:
            continue
        monto = _numero(partes[-1]) if partes[-1] else None
        etiqueta = re.sub(r"\s+", " ", partes[0])
        if monto and len(etiqueta) > 8 and not etiqueta.lower().startswith("girado"):
            m_fra = RE_CANCELA.search(etiqueta)
            conceptos.append({
                "concepto": etiqueta,
                "valor": monto,
                "factura": _factura_limpia(m_fra.group(1)) if m_fra else "",
            })

    return {
        "tipo": (tipo.group(1).upper() if tipo else "COMPROBANTE"),
        "numero": (tipo.group(2) if tipo else ""),
        "titular": titular,
        "documento": documento,
        "nit_cliente": nit_empresa,
        "valor": valor,
        "fecha": fecha,
        "banco": banco,
        "suma_en_letras": suma_letras,
        "facturas": facturas,
        "conceptos": conceptos[:12],
    }


def renglones_desde_comprobante(comprobante: dict) -> list[dict]:
    """Desglose por factura que trae el egreso.

    Es la estructura del pago: una linea por factura con el valor girado a cada
    una. El valor se deja como REFERENCIA (`valor_egreso`), no como el valor
    aprobado: ese sale del correo de aprobacion y debe coincidir con este.
    """
    renglones = []
    for concepto in comprobante.get("conceptos") or []:
        if not concepto.get("factura"):
            continue
        renglones.append({
            "factura": concepto["factura"],
            # El numero TAL COMO lo escribe el egreso. Se guarda aparte porque
            # es contra el que se revisa la columna N, y el egreso se equivoca:
            # hay uno que imprime 00127212397 donde la factura dice 157212397.
            "factura_egreso": concepto["factura"],
            "valor": None,
            "valor_egreso": concepto["valor"],
            "concepto": concepto["concepto"],
        })

    # Si el comprobante no detalla facturas, al menos una linea con el total
    if not renglones and comprobante.get("valor"):
        primera = (comprobante.get("facturas") or [""])[0]
        renglones.append({
            "factura": primera,
            "factura_egreso": primera,
            "valor": None,
            "valor_egreso": comprobante["valor"],
            "concepto": "",
        })
    return renglones


# --------------------------------------------------------------------------- #
# Documento P: registro contable de compras
# --------------------------------------------------------------------------- #

def analizar_registro_compras(texto: str) -> dict | None:
    """Lee un documento P de Siigo ("COMPRAS NACIONALES...", "OTRAS COMPRAS...").

    De aqui sale el "Valor a pagar" (columna Q): el total del documento, que ya
    trae aplicadas las retenciones y los IVA descontables.
    """
    tipo = RE_TIPO.search(texto)
    a_favor = RE_A_FAVOR.search(texto)
    if not (tipo and a_favor):
        return None

    numero = tipo.group(2)
    if not re.match(r"^P", numero, re.IGNORECASE):
        # Solo los documentos que empiezan con P son registro de compras
        if "COMPRA" not in tipo.group(1).upper():
            return None

    tercero = re.sub(r"\s*\|.*$", "", a_favor.group(1)).strip()

    nit = ""
    m_nit = RE_CEDULA_NIT.search(texto)
    if m_nit:
        nit = _nit_limpio(m_nit.group(1))

    # El total del documento aparece dos veces ("T o t a l"); tambien esta en
    # "Valor :" del encabezado. Se prefiere el total de la tabla.
    total = None
    totales = [_numero(m.group(1)) for m in RE_TOTAL_SIIGO.finditer(texto)]
    totales = [t for t in totales if t]
    if totales:
        total = totales[-1]
    if total is None:
        m_valor = RE_VALOR_ENCABEZADO.search(texto)
        if m_valor:
            total = _numero(m_valor.group(1))

    factura = ""
    m_fra = RE_FRA_ETIQUETA.search(texto)
    if m_fra:
        factura = re.sub(r"\s+", "", m_fra.group(1)).upper()
    if not factura:
        m_fact = RE_FACT_NO.search(texto)
        if m_fact:
            factura = m_fact.group(1)

    # "O.C. 20260331" es la orden de compra; "Orden : 608008" es otro
    # consecutivo interno de Siigo y no cruza con el documento Y.
    orden = ""
    m_orden = RE_ORDEN_COMPRA.search(texto)
    if m_orden:
        orden = m_orden.group(1)
    interna = ""
    m_interna = RE_ORDEN_INTERNA.search(texto)
    if m_interna:
        interna = m_interna.group(1)

    fecha = ""
    m_fecha = RE_FECHA.search(texto)
    if m_fecha:
        fecha = m_fecha.group(1).replace("/", "-")

    return {
        "tipo": tipo.group(1).strip().upper(),
        "numero": numero,
        "tercero": tercero,
        "nit": nit,
        "total": total,
        "factura": factura,
        "orden_compra": orden,
        "orden_interna": interna,
        "fecha": fecha,
    }


# --------------------------------------------------------------------------- #
# Validaciones de las columnas U y V
# --------------------------------------------------------------------------- #

def validar_egreso(comprobante: dict, valor_tabla) -> dict:
    """Columna U: el valor total del egreso contra el valor de la tabla azul."""
    total = comprobante.get("valor")
    try:
        esperado = float(valor_tabla) if valor_tabla not in (None, "") else None
    except (TypeError, ValueError):
        esperado = None

    ok = total is not None and esperado is not None and abs(total - esperado) < 0.01
    obs = ""
    if total is None:
        obs = "no se pudo leer el valor total del egreso"
    elif esperado is None:
        obs = "falta el valor del registro en la tabla azul"
    elif not ok:
        obs = (f"el egreso gira {total:,.2f} y la tabla azul dice "
               f"{esperado:,.2f}").replace(",", ".")

    return {
        "numero": comprobante.get("numero", ""),
        "total": total,
        "valor_tabla": esperado,
        "revisiones": {"valor_total": bool(ok)},
        "columnas": {"U": "OK" if ok else "Revisar", "U_obs": obs},
    }


def validar_registro_compras(documento: dict, esperado: dict | None = None) -> dict:
    """Columna V: la orden de compra y el numero de factura del documento P."""
    esperado = esperado or {}
    orden_esperada = str(esperado.get("orden_compra") or "")
    factura_esperada = str(esperado.get("factura") or "")

    def digitos(valor):
        limpio = re.sub(r"\D", "", str(valor or ""))
        return limpio.lstrip("0") or limpio

    orden_p = documento.get("orden_compra") or ""
    factura_p = documento.get("factura") or ""

    # La orden solo se compara si ambos lados la tienen: los documentos P-003
    # (servicios) no relacionan orden de compra.
    orden_ok = None
    if orden_esperada and orden_p:
        orden_ok = digitos(orden_p) == digitos(orden_esperada)

    factura_ok = None
    if factura_esperada and factura_p:
        factura_ok = digitos(factura_p) == digitos(factura_esperada)

    revisiones = {}
    if orden_ok is not None:
        revisiones["orden_compra"] = orden_ok
    if factura_ok is not None:
        revisiones["factura"] = factura_ok

    partes = []
    if orden_ok is False:
        partes.append(f"el P dice O.C. {orden_p} y la orden es {orden_esperada}")
    if factura_ok is False:
        partes.append(f"el P dice FRA {factura_p} y la factura es {factura_esperada}")
    if orden_esperada and not orden_p:
        partes.append("el documento P no relaciona orden de compra")

    faltantes = [clave for clave, ok in revisiones.items() if not ok]
    return {
        "numero": documento.get("numero", ""),
        "orden_compra": orden_p,
        "orden_interna": documento.get("orden_interna", ""),
        "factura": factura_p,
        "revisiones": revisiones,
        "faltantes": faltantes,
        "columnas": {
            "V": "OK" if (revisiones and not faltantes) else
                 ("Revisar" if faltantes else ""),
            "V_obs": " · ".join(partes),
        },
    }
