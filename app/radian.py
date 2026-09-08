"""Validacion del RADIAN (columna S del papel de trabajo).

El soporte de RADIAN es un PDF de capturas del portal del proveedor
tecnologico: **no tiene texto**, es imagen pura, asi que hay que pasarlo por
OCR (RapidOCR, sin dependencias externas).

De ahi se valida:

1. Que esten los TRES estados de la DIAN en el historial:
   - Acuse de recibo (evento 030)
   - Recibo del bien o prestacion del servicio (evento 031)
   - Aceptacion expresa (evento 032)
2. Que el NUMERO DE DOCUMENTO sea el mismo numero de la factura.
3. Que el NUMERO DE IDENTIFICACION sea el NIT de la empresa que vende.
"""

from __future__ import annotations

import re
import unicodedata

import numpy as np
import pypdfium2 as pdfium

from factura_electronica import nit_igual, solo_digitos

ESCALA_OCR = 2          # suficiente para las capturas del portal
CONFIANZA_MINIMA = 0.5

# Los tres eventos que exige la DIAN, con las variantes que escribe el portal
EVENTOS_DIAN = {
    "acuse": {
        "titulo": "Acuse de recibo",
        "claves": ("acuse de recibo",),
    },
    "recibo": {
        "titulo": "Recibo del bien o prestación del servicio",
        "claves": ("recibo del bien", "prestacion del servicio"),
    },
    "aceptacion": {
        "titulo": "Aceptación expresa",
        "claves": ("aceptacion expresa",),
    },
}

# Etiquetas del portal: nunca son un valor. El soporte viene en dos
# disposiciones (etiqueta arriba del valor, o tabla con etiquetas lado a lado),
# asi que hay que descartarlas para no tomar una etiqueta como valor.
ETIQUETAS_PORTAL = (
    "razonsocial", "numerodeidentificacion", "versiondeldocumento",
    "tipodedocumento", "numerodedocumento", "fechadeemision", "totaldelafactura",
    "cufe", "casooradicado", "nombre", "fechaderecepcion", "estatusactual",
    "estatusdepago", "estatuslegal", "historialdecambios", "fechadelregistro",
    "creadopor", "estatus", "comentarios", "estadodocumento", "origenevento",
    "acciones", "validaciondian", "detalledeldocumento", "resumendeldocumento",
    "datosdeldocumento", "representacion", "adjuntos", "infodatos", "gestion",
    "enproduccion", "asignarestatus", "actualizarhistorial", "vereventos",
    "consultarenladian", "consultesu", "comprobante", "mostrandodesde",
    "documentoelectronico",
)

_MOTOR = None


def _motor():
    """RapidOCR se carga una sola vez: el arranque tarda unos segundos."""
    global _MOTOR
    if _MOTOR is None:
        from rapidocr_onnxruntime import RapidOCR
        _MOTOR = RapidOCR()
    return _MOTOR


def sin_tildes(texto: str) -> str:
    base = unicodedata.normalize("NFKD", str(texto or "").lower())
    return "".join(c for c in base if not unicodedata.combining(c))


def _compacto(texto: str) -> str:
    """El OCR se come los espacios: 'NUMERODEDOCUMENTO'. Se comparan sin ellos."""
    return re.sub(r"[^a-z0-9]", "", sin_tildes(texto))


def leer_ocr(ruta: str) -> list[dict]:
    """Pasa el PDF por OCR y devuelve las lineas con su posicion."""
    ocr = _motor()
    documento = pdfium.PdfDocument(ruta)
    lineas: list[dict] = []

    for indice in range(len(documento)):
        imagen = np.array(documento[indice].render(scale=ESCALA_OCR).to_pil().convert("RGB"))
        resultado, _ = ocr(imagen)
        for caja, texto, confianza in (resultado or []):
            try:
                if float(confianza) < CONFIANZA_MINIMA:
                    continue
            except (TypeError, ValueError):
                pass
            if len(str(texto).strip()) < 2:
                continue        # el CUFE viene vertical, un caracter por linea
            xs = [p[0] for p in caja]
            ys = [p[1] for p in caja]
            lineas.append({
                "texto": str(texto).strip(),
                "pagina": indice + 1,
                "x0": min(xs), "x1": max(xs),
                "y0": min(ys), "y1": max(ys),
            })
    return lineas


def _es_etiqueta(texto: str) -> bool:
    """Reconoce una etiqueta del portal, incluso si el OCR la partio.

    El OCR corta "Version del documento" en "Version", asi que se compara en
    los dos sentidos: la etiqueta conocida dentro del texto, o el texto dentro
    de la etiqueta conocida.
    """
    compacta = _compacto(texto)
    if not compacta:
        return True
    return any(
        conocida in compacta or (len(compacta) >= 5 and compacta in conocida)
        for conocida in ETIQUETAS_PORTAL
    )


def _valor_de(lineas: list[dict], indice: int) -> str:
    """Valor asociado a la etiqueta que esta en `indice`.

    El portal usa dos disposiciones: la etiqueta encima del valor (datos del
    documento) y la etiqueta a la izquierda (tabla del historial). Se busca
    primero a la derecha en la misma linea, y si no, justo debajo.
    """
    etiqueta = lineas[indice]
    misma_pagina = [
        (i, l) for i, l in enumerate(lineas)
        if l["pagina"] == etiqueta["pagina"] and i != indice
        and not _es_etiqueta(l["texto"])
    ]

    # A la derecha, con solape vertical
    derecha = [
        (l["x0"], l) for i, l in misma_pagina
        if l["x0"] > etiqueta["x1"] - 5
        and l["y0"] < etiqueta["y1"] and l["y1"] > etiqueta["y0"]
    ]
    if derecha:
        return min(derecha, key=lambda par: par[0])[1]["texto"]

    # Debajo, con solape horizontal
    abajo = [
        (l["y0"], l) for i, l in misma_pagina
        if l["y0"] >= etiqueta["y1"] - 4 and l["y0"] - etiqueta["y1"] < 70
        and l["x0"] < etiqueta["x1"] + 40 and l["x1"] > etiqueta["x0"] - 40
    ]
    if abajo:
        return min(abajo, key=lambda par: par[0])[1]["texto"]

    return ""


def _buscar(lineas: list[dict], *fragmentos: str) -> list[str]:
    """Todos los valores cuya etiqueta contenga alguno de los fragmentos."""
    valores = []
    for i, linea in enumerate(lineas):
        compacta = _compacto(linea["texto"])
        if any(_compacto(f) in compacta for f in fragmentos):
            valor = _valor_de(lineas, i)
            if valor:
                valores.append(valor)
    return valores


def _primer_numero(valores: list[str], minimo_digitos: int = 4) -> str:
    for valor in valores:
        digitos = solo_digitos(valor)
        if len(digitos) >= minimo_digitos:
            return valor.strip()
    return ""


def analizar_radian(ruta: str, nombre: str, esperado: dict | None = None) -> dict:
    """Valida un soporte de RADIAN contra la factura y el NIT del vendedor."""
    esperado = esperado or {}
    numero_esperado = str(esperado.get("numero") or "")
    nit_esperado = str(esperado.get("nit_tercero") or "")

    lineas = leer_ocr(ruta)
    texto_plano = " ".join(l["texto"] for l in lineas)
    normalizado = sin_tildes(texto_plano)

    # --- Datos del documento
    numero_documento = ""
    for valor in _buscar(lineas, "numerodedocumento"):
        limpio = valor.strip()
        # Sirve cualquier valor con digitos que no sea una fecha ni un importe
        if (re.search(r"\d", limpio) and not re.match(r"^\d{2}-\d{2}-\d{4}$", limpio)
                and "," not in limpio and len(limpio) <= 20):
            numero_documento = limpio
            break
    identificacion = _primer_numero(_buscar(lineas, "numerodeidentificacion",
                                            "deidentificacion"), 7)
    razon_social = ""
    for valor in _buscar(lineas, "razonsocial", "nombre"):
        if not solo_digitos(valor):
            razon_social = valor
            break
    tipo_documento = (_buscar(lineas, "tipodedocumento") or [""])[0]
    radicado = _primer_numero(_buscar(lineas, "casooradicado", "radicado"))

    # --- Estados del historial
    # Se descartan los valores que son en realidad otra etiqueta del portal
    estados = [
        v for v in _buscar(lineas, "estatus")
        if v and not _compacto(v).startswith("estatus")
    ]
    presentes = {}
    for clave, evento in EVENTOS_DIAN.items():
        hallado = any(sin_tildes(c) in normalizado for c in evento["claves"])
        presentes[clave] = hallado

    faltan_eventos = [
        EVENTOS_DIAN[c]["titulo"] for c, ok in presentes.items() if not ok
    ]

    # --- Estatus legal ante la DIAN (puede avisar que no hay eventos alla)
    estatus_legal = ""
    for i, linea in enumerate(lineas):
        if "estatuslegal" in _compacto(linea["texto"]):
            estatus_legal = _valor_de(lineas, i)
            break
    sin_eventos_dian = "no posee eventos registrados" in normalizado

    # --- Revisiones
    numero_ok = bool(numero_documento) and (
        not numero_esperado
        or solo_digitos(numero_documento) == solo_digitos(numero_esperado)
        or solo_digitos(numero_documento).lstrip("0") == solo_digitos(numero_esperado).lstrip("0")
    )
    nit_ok = bool(identificacion) and (
        not nit_esperado or nit_igual(identificacion, nit_esperado)
    )

    revisiones = {
        "tres_estados": not faltan_eventos,
        "numero_documento": numero_ok,
        "nit_vendedor": nit_ok,
    }
    faltantes = [clave for clave, ok in revisiones.items() if not ok]

    # --- Observacion para la columna S
    partes = []
    if faltan_eventos:
        partes.append("faltan estados: " + ", ".join(faltan_eventos))
    if not numero_ok:
        partes.append(
            f"RADIAN dice {numero_documento or 'sin número'} y la factura es "
            f"{numero_esperado or '—'}"
        )
    if not nit_ok:
        partes.append(
            f"RADIAN dice NIT {identificacion or 'sin NIT'} y el tercero es "
            f"{nit_esperado or '—'}"
        )
    # El "estatus legal ante la DIAN" NO entra en la columna S: los tres
    # estados son lo que se valida, y ese dato se informa en la interfaz.

    return {
        "nombre": nombre,
        "paginas": len({l["pagina"] for l in lineas}),
        "numero_documento": numero_documento,
        "identificacion": identificacion,
        "razon_social": razon_social,
        "tipo_documento": tipo_documento,
        "radicado": radicado,
        "estados": estados,
        "eventos_dian": presentes,
        "faltan_eventos": faltan_eventos,
        "estatus_legal": estatus_legal,
        "sin_eventos_dian": sin_eventos_dian,
        "esperado": {"numero": numero_esperado, "nit_tercero": nit_esperado},
        "revisiones": revisiones,
        "faltantes": faltantes,
        "columnas": {
            "S": "OK" if not faltantes else "Revisar",
            "S_obs": " · ".join(partes),
        },
    }
