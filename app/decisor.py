"""Decide el valor aprobado a partir de los candidatos de `extractor.py`.

Motor determinístico: no usa IA ni servicios externos. La clave es distinguir
el ACTO de aprobación ("Aprobada por $ 62,029", "Ok procedamos por $150.000")
de la SOLICITUD de aprobación ("por favor su aprobación por $ 150,000"), porque
el valor aprobado y el aprobador salen del acto, no de la solicitud.

Devuelve la misma estructura que se pediria a un modelo, de modo que la
interfaz muestre un solo tipo de veredicto:

    {valor_total, pago_dividido, confianza, renglones[], observaciones,
     descartados[]}
"""

from __future__ import annotations

import re

from extractor import ordenes_del_correo, sin_tildes

# ACTO: alguien aprueba, autoriza o asiente. Es la evidencia que vale.
RE_ACTO = re.compile(
    r"(?<!\w)(?:aprobad[oa]s?|autorizad[oa]s?|apruebo|autorizo|aprobamos"
    r"|procedamos|procedan|proceder\s+con\s+el\s+pago|visto\s+bueno|vo\.?\s?bo"
    r"|de\s+acuerdo|conforme|autorizada\s+por)(?!\w)"
)

# SOLICITUD: se pide la aprobacion, todavia no la hay.
RE_SOLICITUD = re.compile(
    r"(?<!\w)(?:(?:su|tu)\s+(?:aprobacion|autorizacion|vo\.?\s?bo|visto\s+bueno)"
    r"|por\s+favor\s+(?:su|tu|nos)"
    r"|solicito|agradezco|requiero|me\s+confirmas|quedo\s+atent[oa]"
    r"|pendiente\s+de\s+(?:su|tu))(?!\w)"
)

# Contextos que no son un pago aprobado aunque traigan monto
RE_INFORMATIVO = re.compile(
    r"(?<!\w)(?:nota\s+credito|nota\s+debito|saldo|debito:|credito:"
    r"|registro\s+contable|iva|retencion|rete|subtotal|base\s+imponible)(?!\w)"
)


# Palabras de una razon social que no distinguen a nadie
GENERICAS = {
    "S", "A", "SA", "SAS", "LTDA", "LIMITADA", "CIA", "CO", "INC", "DE", "DEL",
    "LA", "EL", "LOS", "LAS", "Y", "E", "EN", "SOCIEDAD", "ANONIMA", "GRUPO",
    "EMPRESA", "COMERCIALIZADORA", "DISTRIBUIDORA", "SERVICIOS", "SUMINISTROS",
}


def _palabras_de_nombre(nombre: str) -> set[str]:
    texto = re.sub(r"[^A-Z0-9 ]", " ", sin_tildes(nombre or "").upper())
    return {p for p in texto.split() if p not in GENERICAS and len(p) >= 3}


def mismo_tercero(uno: str, otro: str) -> bool:
    """"INFOSISCO S A S" es "INFOSISCO SAS"; "CAMARA DE COMERCIO DE
    BARRANQUILLA" no es "COOPERATIVA DE TRANSPORTADORES"."""
    a, b = _palabras_de_nombre(uno), _palabras_de_nombre(otro)
    if not a or not b:
        return False
    comunes = a & b
    return len(comunes) >= max(1, (min(len(a), len(b)) + 1) // 2)


def _clasificar(candidato: dict) -> str:
    """'acto' | 'solicitud' | 'informativo' | 'mencion'

    La linea del monto manda. Si no dice nada, decide el encabezado de la
    lista en que esta: "Aprobadas:" aprueba todo lo que sigue; "Por favor su
    aprobacion de las siguientes facturas:" lo pide.
    """
    linea = sin_tildes(candidato.get("linea", ""))
    propio = _clasificar_texto(linea)
    if propio != "mencion":
        return propio
    encabezado = sin_tildes(candidato.get("encabezado", ""))
    if encabezado:
        del_encabezado = _clasificar_texto(encabezado)
        if del_encabezado in ("acto", "solicitud"):
            return del_encabezado
    return propio


def _clasificar_texto(linea: str) -> str:

    if RE_ACTO.search(linea):
        # "por favor su aprobacion" contiene "aprobacion", no un acto; pero
        # "Ok procedamos por $150.000" si lo es. Ante ambos, manda la solicitud.
        if RE_SOLICITUD.search(linea):
            return "solicitud"
        return "acto"
    if RE_SOLICITUD.search(linea):
        return "solicitud"
    if RE_INFORMATIVO.search(linea):
        return "informativo"
    return "mencion"


def _renglon(candidato: dict, documento: str, tipo: str) -> dict:
    facturas = candidato.get("facturas") or []
    return {
        "valor": candidato["valor"],
        "factura": facturas[0] if facturas else "",
        "concepto": documento,
        "aprobado_por": candidato.get("remitente") or "",
        "fecha_aprobacion": candidato.get("fecha") or "",
        "cita": candidato.get("linea", "")[:300],
        "tipo": tipo,
        "pagina": candidato.get("pagina"),
        "tercero": candidato.get("tercero") or "",
    }


def _elegir_del_documento(doc: dict) -> tuple[list[dict], list[dict]]:
    """Escoge los valores aprobados de UN documento. (renglones, descartados)"""
    candidatos = doc.get("candidatos") or []
    nombre = doc.get("nombre", "")
    # Los documentos cargados antes de leer la O.C. no la traen: se saca del texto
    ordenes = doc.get("ordenes_compra")
    if ordenes is None:
        ordenes = ordenes_del_correo(doc.get("texto") or "")

    clasificados: list[tuple[str, dict]] = [(_clasificar(c), c) for c in candidatos]
    actos = [c for t, c in clasificados if t == "acto"]
    solicitudes = [c for t, c in clasificados if t == "solicitud"]

    # Un acto de aprobacion manda sobre la solicitud del mismo valor
    elegidos = actos or solicitudes
    tipo = "acto" if actos else ("solicitud" if solicitudes else "")

    # Un mismo valor puede aparecer varias veces en el hilo (el correo se cita
    # a si mismo). Se conserva la primera aparicion de cada valor.
    # El numero de factura suele estar en la linea de la solicitud ("su
    # aprobacion a la FRA 157215132 por $ 62,029"), no en la de la aprobacion.
    facturas_por_valor: dict[float, str] = {}
    for c in candidatos:
        facturas = c.get("facturas") or []
        if facturas and c["valor"] not in facturas_por_valor:
            facturas_por_valor[c["valor"]] = facturas[0]

    # Una linea con varios montos ("VR UND 11.000 VR TOTAL 561.000") aprueba
    # uno solo: el mayor, que es el total del renglon
    mayor_por_linea: dict[str, float] = {}
    for c in elegidos:
        mayor_por_linea[c["linea"]] = max(mayor_por_linea.get(c["linea"], 0), c["valor"])
    elegidos = [c for c in elegidos if c["valor"] == mayor_por_linea[c["linea"]]]

    renglones: list[dict] = []
    vistos: set[float] = set()
    for c in elegidos:
        if c["valor"] in vistos:
            continue
        vistos.add(c["valor"])
        renglon = _renglon(c, nombre, tipo)
        renglon["ordenes"] = list(ordenes)
        renglon["documentos"] = [nombre]
        if not renglon["factura"]:
            renglon["factura"] = facturas_por_valor.get(c["valor"], "")
        renglones.append(renglon)

    descartados = [
        {
            "valor": c["valor"],
            "motivo": {
                "informativo": f"{nombre}: valor informativo (nota crédito, saldo o registro contable)",
                "mencion": f"{nombre}: se menciona sin aprobación explícita",
                "solicitud": f"{nombre}: es una solicitud de aprobación, no la aprobación",
            }.get(t, nombre),
        }
        for t, c in clasificados
        if c["valor"] not in vistos
    ]
    return renglones, descartados


def decidir(documentos: list[dict], titular: str = "") -> dict:
    """Consolida el valor aprobado de todos los documentos cargados.

    `titular` es el proveedor del registro. Un correo puede aprobar una lista
    de facturas de varios proveedores: solo cuentan las del titular; las demas
    se muestran descartadas con su nombre.
    """
    correos = [
        d for d in documentos
        if d.get("categoria") in (None, "", "correo", "otro") and not d.get("requiere_ocr")
    ]
    if not correos:
        return {
            "fuente": "reglas",
            "valor_total": 0,
            "pago_dividido": False,
            "confianza": "baja",
            "renglones": [],
            "observaciones": "No hay correos con texto para determinar el valor aprobado.",
            "descartados": [],
        }

    renglones: list[dict] = []
    descartados: list[dict] = []
    for doc in correos:
        nuevos, fuera = _elegir_del_documento(doc)
        renglones.extend(nuevos)
        descartados.extend(fuera)

    # El mismo hilo reenviado en varios PDF repite el mismo valor y la misma
    # cita: no se debe sumar dos veces. Pasa cuando UN aprobado cubre varias
    # facturas y cada carpeta lleva su copia del correo: se queda uno solo,
    # pero se recuerda en que correos estaba.
    unicos: list[dict] = []
    por_firma: dict[tuple, dict] = {}
    for r in renglones:
        firma = (r["valor"], sin_tildes(r["cita"])[:80])
        if firma in por_firma:
            visto = por_firma[firma]
            visto["documentos"] = list(dict.fromkeys(visto["documentos"] + r["documentos"]))
            visto["ordenes"] = list(dict.fromkeys(visto["ordenes"] + r["ordenes"]))
            continue
        por_firma[firma] = r
        unicos.append(r)

    # Aprobados de otros proveedores: fuera, con su nombre
    nota_terceros = ""
    if titular and any(r.get("tercero") for r in unicos):
        del_titular = [r for r in unicos if r.get("tercero") and mismo_tercero(r["tercero"], titular)]
        if del_titular:
            for r in unicos:
                if r not in del_titular:
                    descartados.insert(0, {
                        "valor": r["valor"],
                        "motivo": f"{r['concepto']}: aprobado de otro proveedor "
                                  f"({r.get('tercero') or 'sin nombre'}), no de {titular}",
                    })
            unicos = del_titular
        else:
            nombres = sorted({r["tercero"] for r in unicos if r.get("tercero")})
            nota_terceros = (f"El correo aprueba facturas de {', '.join(nombres)} y ninguno "
                             f"coincide con el titular ({titular}): revisa cual corresponde.")

    total = sum(r["valor"] for r in unicos)
    con_acto = [r for r in unicos if r["tipo"] == "acto"]

    if not unicos:
        confianza, nota = "baja", "No se encontró ningún valor aprobado en los correos."
    elif len(con_acto) == len(unicos) and all(r["aprobado_por"] for r in unicos):
        confianza, nota = "alta", ""
    elif con_acto:
        confianza = "media"
        nota = ("Algunos renglones provienen de una solicitud de aprobación y no "
                "de la aprobación misma. Verifica el correo del aprobador.")
    else:
        confianza = "baja"
        nota = ("Solo se encontraron solicitudes de aprobación. Falta el correo "
                "donde el jefe aprueba el valor.")

    if len(unicos) > 1:
        detalle = " + ".join(f"{r['valor']:,.2f}" for r in unicos)
        nota = (nota + " " if nota else "") + f"Pago repartido: {detalle}."
    if nota_terceros:
        confianza = "baja"
        nota = (nota + " " if nota else "") + nota_terceros

    return {
        "fuente": "reglas",
        "valor_total": total,
        "pago_dividido": len(unicos) > 1,
        "confianza": confianza,
        "renglones": unicos,
        "observaciones": nota,
        "descartados": descartados[:20],
    }
