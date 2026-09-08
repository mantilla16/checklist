"""Entrada de inventario (columna W).

El soporte puede venir de dos formas y son el mismo documento:

- el PDF de Siigo ("ENTRADA PROVEEDOR NACIONAL No. E-001-00202608008"), o
- una captura de pantalla (`Entrada Siigo.png`), que es la que trae el sello
  de revisión. El PDF limpio solo trae el rótulo "Aceptada Firma".

Se valida:

1. el NIT del tercero;
2. que venga FIRMADO (sello "REVISADO POR COSTOS" con nombre y fecha);
3. la cantidad recibida contra la cantidad de la factura;
4. que el FRA sea el de la factura y la O.C. la de la orden de compra.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pdfplumber

from factura_electronica import nit_igual, solo_digitos

logging.getLogger("pdfminer").setLevel(logging.ERROR)

IMAGENES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}

RE_NUMERO = re.compile(
    r"ENTRADA[A-Z\s]*\s+No\.?\s*([A-Z]-?\d{3}-?\d{6,})", re.IGNORECASE)
RE_SENORES = re.compile(
    r"SE[NÑ]ORES\s*:?\s*(.+?)\s+NIT\s*:?\s*(\d[\d.,\-\s]{6,16})", re.IGNORECASE)
RE_FECHA = re.compile(r"FECHA\s*:?\s*(\d{4}/\d{2}/\d{2})", re.IGNORECASE)
RE_NIT_SUELTO = re.compile(r"NIT\s*:?\s*(\d[\d.,\-\s]{6,16})", re.IGNORECASE)
# El OCR lee la "O" de "O.C." como un cero
RE_OC = re.compile(r"(?<![A-Z0-9])[O0]\.?\s?C\.?\s+(\d{4,12})", re.IGNORECASE)
RE_FRA = re.compile(
    r"\bFRA\s+(?:N[o°]\.?\s*)?([A-Z]{0,5}[-\s]?\d{1,12})", re.IGNORECASE)
# "Entra 15.00PZ" / "Entra 1,920.00ROL"
RE_ENTRA = re.compile(
    r"Entra\s+([\d.,]+)\s*([A-Z]{2,4})?", re.IGNORECASE)

# Sello de revisión: "REVISADO POR COSTOS / NOMBRE APELLIDO 05-08-2026"
RE_SELLO = re.compile(
    r"(REVISAD[OA]\s+POR[^\n]{0,40}|V[oº]\.?\s?B[oº]\.?|VISTO\s+BUENO"
    r"|APROBAD[OA]\s+POR[^\n]{0,40})", re.IGNORECASE)
RE_NOMBRE_FECHA = re.compile(
    r"([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s\.]{5,40})\s+(\d{2}[-/]\d{2}[-/]\d{4,5})")


def _cantidad(bruto: str) -> float | None:
    texto = str(bruto or "").strip().rstrip(".,")
    if not re.fullmatch(r"[\d.,]+", texto):
        return None
    if texto.count(",") and texto.count("."):
        texto = texto.replace(",", "")
    elif texto.count(","):
        texto = texto.replace(",", "." if len(texto.split(",")[-1]) == 2 else "")
    try:
        return float(texto)
    except ValueError:
        return None


def leer_texto(ruta: str) -> tuple[str, str]:
    """Devuelve (texto, metodo). Usa OCR si el soporte es imagen o PDF escaneado."""
    extension = Path(ruta).suffix.lower()

    if extension not in IMAGENES:
        with pdfplumber.open(ruta) as pdf:
            texto = "\n".join(p.extract_text() or "" for p in pdf.pages)
        if texto.strip():
            return texto, "texto del PDF"

    # Imagen, o PDF sin capa de texto
    import numpy as np
    from radian import _motor

    if extension in IMAGENES:
        from PIL import Image
        lienzos = [np.array(Image.open(ruta).convert("RGB"))]
    else:
        import pypdfium2 as pdfium
        documento = pdfium.PdfDocument(ruta)
        lienzos = [
            np.array(documento[i].render(scale=2).to_pil().convert("RGB"))
            for i in range(min(len(documento), 4))
        ]

    ocr = _motor()
    lineas = []
    for lienzo in lienzos:
        resultado, _ = ocr(lienzo)
        # Se ordena por fila para reconstruir las lineas del documento
        cajas = []
        for caja, texto, confianza in (resultado or []):
            try:
                if float(confianza) < 0.4:
                    continue
            except (TypeError, ValueError):
                pass
            ys = [p[1] for p in caja]
            xs = [p[0] for p in caja]
            cajas.append((min(ys), min(xs), str(texto).strip()))
        cajas.sort(key=lambda c: (round(c[0] / 12), c[1]))

        actual, y_actual = [], None
        for y, _x, texto in cajas:
            if y_actual is None or abs(y - y_actual) <= 12:
                actual.append(texto)
                y_actual = y if y_actual is None else y_actual
            else:
                lineas.append(" ".join(actual))
                actual, y_actual = [texto], y
        if actual:
            lineas.append(" ".join(actual))

    return "\n".join(lineas), "OCR"


def analizar_entrada(texto: str) -> dict | None:
    """Extrae los datos de una entrada de inventario."""
    if not re.search(r"ENTRADA", texto, re.IGNORECASE):
        return None

    numero = ""
    m = RE_NUMERO.search(texto)
    if m:
        numero = m.group(1)

    tercero, nit = "", ""
    m = RE_SENORES.search(texto)
    if m:
        tercero = re.sub(r"\s*\|.*$", "", m.group(1)).strip()
        crudo = m.group(2).strip()
        nit = solo_digitos(crudo.split("-")[0] if "-" in crudo else crudo)

    fecha = ""
    m = RE_FECHA.search(texto)
    if m:
        fecha = m.group(1).replace("/", "-")

    # El OCR parte la linea de SEÑORES, asi que se recogen todos los NIT del
    # documento: el primero es el de la empresa que recibe.
    nits = []
    for m in RE_NIT_SUELTO.finditer(texto):
        crudo = m.group(1).strip()
        limpio = solo_digitos(crudo.split("-")[0] if "-" in crudo else crudo)
        if 7 <= len(limpio) <= 11 and limpio not in nits:
            nits.append(limpio)

    lineas = []
    for m in RE_ENTRA.finditer(texto):
        cantidad = _cantidad(m.group(1))
        if cantidad:
            lineas.append({"cantidad": cantidad, "unidad": (m.group(2) or "").upper()})

    orden = ""
    m = RE_OC.search(texto)
    if m:
        orden = m.group(1)

    factura = ""
    m = RE_FRA.search(texto)
    if m:
        factura = re.sub(r"\s+", "", m.group(1)).upper()

    # Firma / sello de revisión
    sello = RE_SELLO.search(texto)
    revisor, fecha_sello = "", ""
    if sello:
        cola = texto[sello.end(): sello.end() + 160]
        m_nf = RE_NOMBRE_FECHA.search(cola)
        if m_nf:
            revisor = re.sub(r"\s+", " ", m_nf.group(1)).strip()
            fecha_sello = m_nf.group(2)
        else:
            m_nombre = re.search(r"([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s\.]{5,40})", cola)
            if m_nombre:
                revisor = re.sub(r"\s+", " ", m_nombre.group(1)).strip()

    return {
        "numero": numero,
        "tercero": tercero,
        "nit": nit,
        "nits": nits,
        "texto": texto[:6000],
        "fecha": fecha,
        "lineas": lineas,
        "cantidad_total": sum(l["cantidad"] for l in lineas) if lineas else None,
        "unidades": sorted({l["unidad"] for l in lineas if l["unidad"]}),
        "orden_compra": orden,
        "factura": factura,
        "firma": {
            "presente": bool(sello),
            "sello": re.sub(r"\s+", " ", sello.group(1)).strip() if sello else "",
            "revisor": revisor,
            "fecha": fecha_sello,
        },
    }


def validar_entrada(entrada: dict, esperado: dict | None = None) -> dict:
    """Columna W: NIT, firma, cantidad y los cruces de FRA y O.C."""
    esperado = esperado or {}
    nit_esperado = esperado.get("nit_tercero") or ""
    factura_esperada = str(esperado.get("factura") or "")
    orden_esperada = str(esperado.get("orden_compra") or "")
    cantidad_factura = esperado.get("cantidad_factura")

    def digitos(valor):
        limpio = re.sub(r"\D", "", str(valor or ""))
        return limpio.lstrip("0") or limpio

    # El NIT de la propia empresa que recibe no cuenta como el del tercero
    nit_propio = esperado.get("nit_cliente") or ""
    candidatos = [entrada.get("nit")] + list(entrada.get("nits") or [])
    candidatos = [c for c in candidatos if c]
    del_tercero = [c for c in candidatos
                   if not (nit_propio and nit_igual(c, nit_propio))]
    if not nit_esperado:
        nit_ok = bool(del_tercero)
    else:
        nit_ok = any(nit_igual(c, nit_esperado) for c in del_tercero)
    firma_ok = bool(entrada.get("firma", {}).get("presente"))

    factura_ok = None
    if factura_esperada and entrada.get("factura"):
        factura_ok = digitos(entrada["factura"]) == digitos(factura_esperada)

    orden_ok = None
    if orden_esperada and entrada.get("orden_compra"):
        orden_ok = digitos(entrada["orden_compra"]) == digitos(orden_esperada)

    cantidad_ok = None
    recibida = entrada.get("cantidad_total")
    if cantidad_factura is not None and recibida is not None:
        try:
            cantidad_ok = abs(float(recibida) - float(cantidad_factura)) < 0.01
        except (TypeError, ValueError):
            cantidad_ok = None

    revisiones = {"nit_tercero": nit_ok, "firmado": firma_ok}
    for clave, valor in (("factura", factura_ok), ("orden_compra", orden_ok),
                         ("cantidad", cantidad_ok)):
        if valor is not None:
            revisiones[clave] = valor
    # Lo que no se pudo leer queda como pendiente, no como aprobado
    if factura_esperada and not entrada.get("factura"):
        revisiones["factura"] = False
    if orden_esperada and not entrada.get("orden_compra"):
        revisiones["orden_compra"] = False

    faltantes = [clave for clave, ok in revisiones.items() if not ok]

    partes = []
    if not nit_ok:
        if del_tercero:
            partes.append(f"la entrada dice NIT {', '.join(del_tercero)} y el "
                          f"tercero es {nit_esperado or '—'}")
        else:
            partes.append("no se pudo leer el NIT del tercero en la entrada")
    # Un campo ilegible no se da por bueno en silencio
    if factura_esperada and not entrada.get("factura"):
        partes.append("no se pudo leer el FRA en la entrada")
    if orden_esperada and not entrada.get("orden_compra"):
        partes.append("no se pudo leer la O.C. en la entrada")
    if not firma_ok:
        partes.append("la entrada no viene firmada")
    if factura_ok is False:
        partes.append(f"la entrada dice FRA {entrada['factura']} "
                      f"y la factura es {factura_esperada}")
    if orden_ok is False:
        partes.append(f"la entrada dice O.C. {entrada['orden_compra']} "
                      f"y la orden es {orden_esperada}")
    if cantidad_ok is False:
        partes.append(f"la entrada recibe {recibida:g} y la factura trae "
                      f"{float(cantidad_factura):g}")

    # La cantidad es una de las validaciones: si no se puede comparar, la
    # columna queda PENDIENTE, no aprobada.
    pendiente = cantidad_factura is None or recibida is None
    if cantidad_factura is None:
        partes.append("falta la cantidad de la factura para comparar")
    elif recibida is None:
        partes.append("no se pudo leer la cantidad recibida en la entrada")

    if faltantes:
        estado = "Revisar"
    elif pendiente:
        estado = "Pendiente"
    else:
        estado = "OK"

    return {
        "entrada": entrada,
        "cantidad_recibida": recibida,
        "cantidad_factura": cantidad_factura,
        "revisiones": revisiones,
        "faltantes": faltantes,
        "pendiente": pendiente,
        "columnas": {
            "W": estado,
            "W_obs": " · ".join(partes),
        },
    }
