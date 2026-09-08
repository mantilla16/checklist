"""Validacion con IA del valor aprobado en la cola de correos.

Las reglas de `extractor.py` encuentran los candidatos; aqui el modelo lee el
hilo completo y decide cual es el valor aprobado del pago, entendiendo el caso
de un pago repartido en varias facturas.

Requiere la variable de entorno ANTHROPIC_API_KEY. Si no esta configurada, la
funcion devuelve `disponible=False` y la interfaz sigue funcionando solo con
las reglas.
"""

from __future__ import annotations

import json
import os

import anthropic

MODELO = "claude-opus-5"

# Limite de texto por documento. Si un hilo excede esto se avisa en la respuesta
# en lugar de recortarlo en silencio.
MAX_CARACTERES = 120_000

INSTRUCCIONES = """\
Eres auditor de cuentas por pagar en Colombia. Analizas la cola de correos que \
soporta un pago a proveedores y determinas el VALOR APROBADO por el jefe o \
responsable, que se registrara en la columna "Valor segun aprobacion" del papel \
de trabajo.

Reglas del negocio:
- El valor aprobado es el que una persona con autoridad aprueba, autoriza o \
  confirma explicitamente para pagar ("apruebo", "autorizo", "procedamos por", \
  "visto bueno", "de acuerdo, pagar").
- Un mismo pago puede estar repartido en VARIAS FACTURAS. En ese caso el valor \
  aprobado es la suma de los valores aprobados de cada factura, y debes \
  devolver un renglon por cada uno.
- NO son valor aprobado: cantidades de unidades, numeros de factura, NIT, \
  telefonos, codigos contables, fechas, saldos informativos, ni valores que \
  solo se mencionan como consulta o propuesta sin aprobacion.
- Una nota credito o un descuento REDUCE el valor a pagar; no lo confundas con \
  una aprobacion de pago. Si el hilo trata de una nota credito, dilo en las \
  observaciones.
- Si el hilo no contiene una aprobacion explicita de un valor, devuelve \
  valor_total = 0, confianza = "baja" y explica que falta el correo de \
  aprobacion. NO inventes un valor.

Trabajas sobre evidencia: cada renglon debe citar el texto literal del correo \
que lo respalda. Si dudas entre dos lecturas, elige la mas conservadora y \
explicala en las observaciones.
"""

ESQUEMA = {
    "type": "object",
    "properties": {
        "valor_total": {
            "type": "number",
            "description": "Valor aprobado total. 0 si no hay aprobacion explicita.",
        },
        "pago_dividido": {
            "type": "boolean",
            "description": "true si el pago se reparte en varias facturas.",
        },
        "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
        "renglones": {
            "type": "array",
            "description": "Un renglon por cada valor aprobado que compone el total.",
            "items": {
                "type": "object",
                "properties": {
                    "valor": {"type": "number"},
                    "factura": {
                        "type": "string",
                        "description": "Numero de factura asociado, o cadena vacia.",
                    },
                    "concepto": {"type": "string"},
                    "aprobado_por": {
                        "type": "string",
                        "description": "Quien aprueba. Cadena vacia si no se identifica.",
                    },
                    "fecha_aprobacion": {"type": "string"},
                    "cita": {
                        "type": "string",
                        "description": "Texto literal del correo que respalda el valor.",
                    },
                },
                "required": [
                    "valor",
                    "factura",
                    "concepto",
                    "aprobado_por",
                    "fecha_aprobacion",
                    "cita",
                ],
                "additionalProperties": False,
            },
        },
        "observaciones": {
            "type": "string",
            "description": "Hallazgos, dudas o soportes faltantes. Vacio si no hay.",
        },
        "descartados": {
            "type": "array",
            "description": "Montos candidatos que NO son valor aprobado y por que.",
            "items": {
                "type": "object",
                "properties": {
                    "valor": {"type": "number"},
                    "motivo": {"type": "string"},
                },
                "required": ["valor", "motivo"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "valor_total",
        "pago_dividido",
        "confianza",
        "renglones",
        "observaciones",
        "descartados",
    ],
    "additionalProperties": False,
}


def hay_credenciales() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _armar_consulta(documentos: list[dict], valor_lote: float | None) -> str:
    partes: list[str] = []

    if valor_lote:
        partes.append(
            f"Valor del registro en el lote del banco: {valor_lote:,.2f} COP.\n"
            "Sirve de referencia, pero el valor aprobado se determina por el correo."
        )

    for doc in documentos:
        texto = doc.get("texto", "")
        aviso = ""
        if len(texto) > MAX_CARACTERES:
            aviso = (
                f"\n[AVISO: el documento tiene {len(texto)} caracteres y se envian "
                f"los primeros {MAX_CARACTERES}. Indicalo en observaciones.]"
            )
            texto = texto[:MAX_CARACTERES]

        candidatos = doc.get("candidatos") or []
        lista = "\n".join(
            f"  - {c['valor']:,.2f} | {c.get('texto_original', '')} | pag {c.get('pagina')}"
            f" | remitente: {c.get('remitente') or 'sin identificar'}"
            f" | linea: {c.get('linea', '')[:200]}"
            for c in candidatos
        ) or "  (las reglas no encontraron montos)"

        partes.append(
            f"<documento nombre=\"{doc.get('nombre')}\" tipo=\"{doc.get('categoria')}\">\n"
            f"<montos_detectados_por_reglas>\n{lista}\n</montos_detectados_por_reglas>\n"
            f"<texto>\n{texto}{aviso}\n</texto>\n</documento>"
        )

    partes.append(
        "Determina el valor aprobado del pago. Los montos de las reglas son una "
        "ayuda, no una restriccion: si el valor correcto no esta en esa lista, "
        "usa el del texto."
    )
    return "\n\n".join(partes)


def validar(documentos: list[dict], valor_lote: float | None = None) -> dict:
    """Pide al modelo el valor aprobado. Devuelve el dict del esquema + metadatos."""
    if not hay_credenciales():
        return {
            "disponible": False,
            "error": "Falta la variable de entorno ANTHROPIC_API_KEY.",
        }
    if not documentos:
        return {"disponible": False, "error": "No se envio ningun documento."}

    client = anthropic.Anthropic()
    try:
        respuesta = client.beta.messages.create(
            model=MODELO,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            system=INSTRUCCIONES,
            messages=[{"role": "user", "content": _armar_consulta(documentos, valor_lote)}],
            output_config={"format": {"type": "json_schema", "schema": ESQUEMA}},
        )
    except anthropic.AuthenticationError:
        return {"disponible": False, "error": "La credencial de Anthropic no es valida."}
    except anthropic.RateLimitError:
        return {"disponible": False, "error": "Limite de uso alcanzado. Reintenta en un momento."}
    except anthropic.APIStatusError as exc:
        return {"disponible": False, "error": f"Error {exc.status_code} de la API: {exc.message}"}
    except anthropic.APIConnectionError:
        return {"disponible": False, "error": "No se pudo conectar con la API de Anthropic."}

    if respuesta.stop_reason == "refusal":
        return {"disponible": False, "error": "El modelo declino procesar el contenido."}

    texto = next((b.text for b in respuesta.content if b.type == "text"), "")
    try:
        datos = json.loads(texto)
    except json.JSONDecodeError:
        return {"disponible": False, "error": "La respuesta del modelo no es JSON valido."}

    datos["disponible"] = True
    datos["modelo"] = respuesta.model
    datos["tokens"] = {
        "entrada": respuesta.usage.input_tokens,
        "salida": respuesta.usage.output_tokens,
    }
    return datos
