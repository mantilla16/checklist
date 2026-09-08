"""Lectura de la orden de compra (documento Y) para la columna T.

Formato de Siigo:

    EMPRESA EJEMPLO S.A.S. NIT 900123456 -0
    ORDEN DE COMPRA MATERIAL NACIONAL 00020260331
    | Señores : PROVEEDOR UNO S.A. S.A. 860111222-3 | Fecha: 2026/07/14 |
    | Dir : ...                                                 | Fec.Ent. : 2026/09/22 |
    | Producto | Descripción Referencia |UNI| Cantidad | Vr. Unitario | Vr. Total |
    |0050007000044 | CILINDRO ARGON ... |PZ | 80.00| 164,835.00| 13,186,800.00|
    |              |ENTREGAS: 21/07: 20 CIL / 04/08: 15 CIL ...
    |              | Neto a Pagar | 15,692,292.00|

De aqui salen los controles de la columna T:

1. el NIT del tercero de la orden contra el de la factura;
2. la cantidad de la orden como TOPE de lo que puede facturarse.

Las fechas NO se validan (las entregas son parciales, la factura casi nunca
tiene la fecha de la orden). Se leen y se muestran como informacion.
"""

from __future__ import annotations

import re

from factura_electronica import nit_igual, solo_digitos

RE_ENCABEZADO = re.compile(
    r"ORDEN\s+DE\s+COMPRA[^\n\d]*(\d{6,})", re.IGNORECASE)
RE_SENORES = re.compile(
    r"Se[nñ]ores\s*:\s*(.+?)\s+(\d[\d.,\-\s]{6,16})\s*(?:\||Fecha)", re.IGNORECASE)
RE_FECHA_OC = re.compile(r"Fecha\s*:\s*(\d{4}/\d{2}/\d{2}|\d{2}/\d{2}/\d{4})", re.IGNORECASE)
RE_FECHA_ENTREGA = re.compile(
    r"Fec\.?\s*Ent\.?\s*:\s*(\d{4}/\d{2}/\d{2}|\d{2}/\d{2}/\d{4})", re.IGNORECASE)
RE_NETO = re.compile(r"Neto\s+a\s+Pagar\s*\|?\s*([\d.,]+)", re.IGNORECASE)
RE_TOTAL_BRUTO = re.compile(r"Total\s+Bruto\s*\|?\s*([\d.,]+)", re.IGNORECASE)

# |0050007000044 | CILINDRO ARGON ... |PZ | 80.00| 164,835.00| 13,186,800.00|
RE_LINEA = re.compile(
    r"\|\s*(?P<producto>\d{6,})\s*\|(?P<descripcion>[^|]*)\|(?P<unidad>[A-Z]{1,4})?\s*\|"
    r"\s*(?P<cantidad>[\d.,]+)\s*\|\s*(?P<unitario>[\d.,]+)\s*\|\s*(?P<total>[\d.,]+)\s*\|"
)
# ENTREGAS: 21/07: 20 CIL
RE_ENTREGA = re.compile(r"(\d{2}/\d{2})\s*:\s*([\d.,]+)\s*([A-Z]{2,6})?", re.IGNORECASE)


def _numero(bruto: str) -> float | None:
    texto = str(bruto or "").strip()
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


def _fecha_iso(bruto: str) -> str:
    """2026/07/14 o 14/07/2026 -> 2026-07-14."""
    texto = str(bruto or "").strip()
    if re.match(r"^\d{4}/", texto):
        return texto.replace("/", "-")
    partes = texto.split("/")
    if len(partes) == 3:
        return f"{partes[2]}-{partes[1]}-{partes[0]}"
    return texto


def analizar_orden(texto: str) -> dict | None:
    """Lee una orden de compra. None si el documento no lo es."""
    encabezado = RE_ENCABEZADO.search(texto)
    senores = RE_SENORES.search(texto)
    if not (encabezado or senores):
        return None

    numero = ""
    if encabezado:
        numero = encabezado.group(1).lstrip("0") or encabezado.group(1)

    tercero, nit = "", ""
    if senores:
        tercero = senores.group(1).strip()
        nit = solo_digitos(senores.group(2)).rstrip()
        # El NIT viene con digito de verificacion: 860111222-3
        crudo = senores.group(2).strip()
        if "-" in crudo:
            nit = solo_digitos(crudo.split("-")[0])

    fecha = _fecha_iso(RE_FECHA_OC.search(texto).group(1)) if RE_FECHA_OC.search(texto) else ""
    entrega = (_fecha_iso(RE_FECHA_ENTREGA.search(texto).group(1))
               if RE_FECHA_ENTREGA.search(texto) else "")

    # Lineas de la orden: la cantidad es el tope facturable
    lineas, vistas = [], set()
    for m in RE_LINEA.finditer(texto):
        cantidad = _numero(m.group("cantidad"))
        if cantidad is None:
            continue
        clave = (m.group("producto"), cantidad)
        if clave in vistas:      # la orden repite la misma pagina varias veces
            continue
        vistas.add(clave)
        lineas.append({
            "producto": m.group("producto"),
            "descripcion": re.sub(r"\s+", " ", m.group("descripcion")).strip(),
            "unidad": (m.group("unidad") or "").strip(),
            "cantidad": cantidad,
            "unitario": _numero(m.group("unitario")),
            "total": _numero(m.group("total")),
        })

    # Entregas parciales programadas (dd/mm : cantidad)
    entregas = []
    zona = texto.upper().split("ENTREGAS")
    if len(zona) > 1:
        for m in RE_ENTREGA.finditer(zona[1][:400]):
            dia_mes, cantidad, unidad = m.group(1), _numero(m.group(2)), m.group(3) or ""
            if cantidad and not any(e["dia_mes"] == dia_mes for e in entregas):
                entregas.append({"dia_mes": dia_mes, "cantidad": cantidad, "unidad": unidad})

    neto = RE_NETO.search(texto)
    bruto = RE_TOTAL_BRUTO.search(texto)

    return {
        "numero": numero,
        "tercero": tercero,
        "nit": nit,
        "fecha": fecha,
        "fecha_entrega": entrega,
        "lineas": lineas,
        "cantidad_total": sum(l["cantidad"] for l in lineas) if lineas else None,
        "entregas": entregas,
        "total_bruto": _numero(bruto.group(1)) if bruto else None,
        "neto_a_pagar": _numero(neto.group(1)) if neto else None,
    }


def validar_orden(orden: dict, esperado: dict | None = None) -> dict:
    """Compara la orden contra la factura. Devuelve la columna T y su detalle.

    `esperado` = {"nit_tercero", "fecha_factura", "cantidad_factura"}
    """
    esperado = esperado or {}
    nit_esperado = esperado.get("nit_tercero") or ""
    fecha_factura = esperado.get("fecha_factura") or ""
    cantidad_factura = esperado.get("cantidad_factura")

    nit_ok = bool(orden.get("nit")) and (
        not nit_esperado or nit_igual(orden["nit"], nit_esperado))

    # Las fechas NO se validan: la factura casi nunca tiene la fecha de la
    # orden (las entregas son parciales). Se calculan solo para mostrarlas.
    dias = None
    if fecha_factura and orden.get("fecha"):
        try:
            from datetime import date
            dias = (date.fromisoformat(fecha_factura)
                    - date.fromisoformat(orden["fecha"])).days
        except ValueError:
            dias = None

    # La cantidad de la orden es el tope
    tope = orden.get("cantidad_total")
    cantidad_ok = None
    if cantidad_factura is not None and tope is not None:
        cantidad_ok = float(cantidad_factura) <= float(tope) + 1e-6

    # ¿La fecha de la factura coincide con una entrega programada?
    entrega_coincide = None
    if fecha_factura and orden.get("entregas"):
        dia_mes = f"{fecha_factura[8:10]}/{fecha_factura[5:7]}"
        entrega_coincide = any(e["dia_mes"] == dia_mes for e in orden["entregas"])

    revisiones = {"nit_tercero": nit_ok}
    if cantidad_ok is not None:
        revisiones["cantidad_tope"] = cantidad_ok

    faltantes = [clave for clave, ok in revisiones.items() if not ok]

    partes = []
    if not nit_ok:
        partes.append(
            f"la orden dice NIT {orden.get('nit') or 'sin NIT'} y el tercero es "
            f"{nit_esperado or '—'}")
    if cantidad_ok is False:
        partes.append(
            f"la factura trae {cantidad_factura} y el tope de la orden es {tope}")

    return {
        "orden": orden,
        "dias_entre_orden_y_factura": dias,
        "entrega_coincide": entrega_coincide,
        "tope": tope,
        "cantidad_factura": cantidad_factura,
        "revisiones": revisiones,
        "faltantes": faltantes,
        "columnas": {
            "T": "OK" if not faltantes else "Revisar",
            "T_obs": " · ".join(partes),
        },
    }
