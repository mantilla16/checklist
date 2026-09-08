# Validación de Pagos a Proveedores

Herramienta local para validar los soportes en PDF de un lote de pagos —correos
de aprobación, facturas electrónicas, órdenes de compra, egresos, registros
contables y entradas de inventario— y generar el papel de trabajo en Excel con
el formato de una plantilla.

Lee los documentos, extrae los datos, los cruza entre sí y **deja dicho en cada
celda lo que no cuadra**. Quien revisa decide; la herramienta no aprueba nada
por su cuenta.

## Cómo ejecutar

```bash
pip install -r requirements.txt
python app/server.py
```

Luego abrir <http://127.0.0.1:5000>.

En Ubuntu hacen falta dos librerías del sistema para el OCR:

```bash
sudo apt install -y libgl1 libglib2.0-0
```

Para desplegar en un servidor, ver [`despliegue/README.md`](despliegue/README.md).

### Configuración

Todo es opcional; sin nada, la herramienta usa las carpetas del propio proyecto.

| Variable | Para qué |
|---|---|
| `CHECKLIST_DATOS` | carpeta de la base de datos y del libro generado |
| `CHECKLIST_SUBIDAS` | carpeta de los PDF e imágenes cargados |
| `CHECKLIST_PLANTILLA` | archivo `.xlsx` que se usa como formato de salida |
| `CHECKLIST_HOST`, `CHECKLIST_PUERTO` | dónde escucha en desarrollo |
| `ANTHROPIC_API_KEY` | habilita el botón opcional de segunda opinión con IA |

La plantilla aporta **solo el formato**: estilos, encabezado del lote, fila de
títulos y las filas de Total y Diferencias. **No se leen sus datos y el archivo
original nunca se modifica** — al exportar se genera un libro nuevo.

## Cómo se usa

1. **Arrastrar los comprobantes de egreso** (`g…pdf`, uno por pago). De ahí se
   arma el lote: encabezado y columnas B a G.
2. Seleccionar un registro y cargarle sus soportes, paso por paso.
3. **Generar Excel**: crea una hoja por lote con el formato de la plantilla y
   reporta qué registros quedaron sin validar o con diferencia.

Un lote vacío también se puede capturar a mano con `+ vacío`.

## Qué valida cada columna

| Columna | Documento | Validación |
|---|---|---|
| **B a G** | egreso `g…pdf` | titular, NIT, valor girado y desglose por factura |
| **J, K** | correo de aprobación | el valor aprobado en el correo |
| **L** | — | fórmula `=+G11-K11`: debe quedar en 0 |
| **M a P** | factura electrónica | NIT del cliente, NIT del tercero, CUFE y código QR |
| **Q** | registro contable `p…pdf` | el total del documento es el valor a pagar |
| **R** | — | fórmula `=+G11-Q11` |
| **S** | soporte de RADIAN | los tres estados DIAN, número de documento y NIT |
| **T** | orden de compra `y…pdf` | NIT del tercero y cantidad tope |
| **U** | egreso `g…pdf` | el valor del egreso contra el de la tabla del lote |
| **V** | registro contable `p…pdf` | la orden de compra (`O.C.`) y el número de factura (`FRA`) |
| **W** | entrada de inventario | NIT, firma, cantidad recibida, `FRA` y `O.C.` |

### Un pago repartido en varias facturas

Cuando un mismo giro paga dos o más facturas, el registro ocupa varias filas:
cada una con su `K`, y las columnas **L, M, P y R combinadas** con una resta por
sub-fila (`=+G12-K12-K13`), igual que en el formato original. Si hacen falta
filas, se insertan y lo de abajo se corre.

### Estados posibles

| Estado | Significa |
|---|---|
| `OK` | todas las validaciones de esa columna pasaron |
| `Revisar` | alguna falló; el motivo queda escrito en la misma celda |
| `Pendiente` | falta un dato para poder comparar |

Las observaciones **se escriben en la celda de la columna a la que pertenecen**:
si el hallazgo es del número de factura va en `N`, si es del NIT del tercero va
en `O`, y así.

## Cómo lee los documentos

La dificultad real es que cada proveedor emite en un formato distinto. La
estrategia no es reconocer formatos, sino **validar contra lo que ya se conoce**
y anclar la lectura a algo estable.

### Correos de aprobación

Distingue el **acto** de aprobación de la **solicitud**:

| Tipo | Ejemplo | ¿Vale? |
|---|---|---|
| Acto | "Aprobada por $ 62.029", "Ok procedamos por $150.000" | sí |
| Solicitud | "por favor su aprobación por $ 150.000" | no |
| Informativo | nota crédito, saldo, IVA, registro contable | no |

Antes de puntuar, cada número pasa un filtro de validez: se descartan los que
tienen `-`, `/`, `:`, `@` o `%` pegados (NIT, fechas, cuentas, códigos,
porcentajes), los años, los que no traen separador de miles ni símbolo de
moneda, y los pegados a texto. Se ignoran encabezados, firmas y adjuntos.

### Facturas electrónicas

- **CUFE/CUDE**: el hash de 96 caracteres, con etiqueta, dentro de la URL de la
  DIAN o suelto en el pie.
- **Código QR**: se decodifica renderizando la página a varias escalas. El QR de
  la DIAN trae `NumFac`, `NitFac` y `DocAdq`, así que cuando decodifica da los
  tres datos sin depender del formato del PDF. Cuando **no** decodifica —pasa
  con un logo encima o texto solapado— se detecta por su **estructura**: una
  imagen cuadrada con altísima densidad de transiciones blanco/negro (un QR da
  42–55 por fila; un logo, entre 2 y 6). La interfaz dice con qué método se
  detectó.
- **NIT**: la comparación tolera puntos, comas, guiones y el dígito de
  verificación, porque unas fuentes lo traen y otras no.
- **Cantidad**: se lee por la **posición de la columna**. Se ubica la cabecera
  que dice `Cantidad` (o `Cant.`, `CANT`, `Cantidad/Unidad`), se toma su centro
  horizontal y se leen los números alineados con ella, parando donde empiezan
  los totales. Solo cuenta como corte la palabra que **abre** su fila, porque
  hay formatos con la cabecera en dos líneas y otros que repiten `IVA` en cada
  renglón.

### RADIAN

El soporte es un PDF de capturas del portal: **imagen pura, sin texto**. Se pasa
por OCR (`rapidocr-onnxruntime`, sin binarios externos), unos 20 segundos por
documento. Se validan los tres eventos que exige la DIAN —`Acuse de recibo`,
`Recibo del bien o prestación del servicio` y `Aceptación expresa`— más el
número de documento y el NIT del vendedor.

El portal exporta en dos disposiciones (etiqueta encima del valor, o tabla con
etiquetas lado a lado), así que el valor se busca primero a la derecha en la
misma línea y luego debajo, descartando lo que sea otra etiqueta. Como el OCR se
come los espacios (`NUMERODEDOCUMENTO`) y parte etiquetas, la comparación se
hace sin espacios y en los dos sentidos.

### Documentos de Siigo

El egreso, el registro contable de compras, la orden de compra y la entrada de
inventario comparten la misma papelería, y de ahí salen los cruces:

| Campo | Documento | Va a |
|---|---|---|
| `Girado a` + `NIT` | egreso | titular y documento titular |
| `CANCELA FACTURA No.` | egreso | una fila por factura, con su valor |
| `O.C.` | registro P y entrada | se cruza con el número de la orden `Y` |
| `FRA` | registro P y entrada | se cruza con el número de la factura |
| `Entra 15.00PZ` | entrada | cantidad recibida |
| `REVISADO POR …` | entrada | la firma |

El documento P trae **dos** referencias de orden y no son la misma: `O.C.` es la
orden de compra y cruza con el documento `Y`; `Orden :` es otro consecutivo
interno que no cruza con nada. Los documentos de servicios no traen `O.C.`, y
eso es correcto: no hay orden de compra para un servicio.

La entrada de inventario puede venir en PDF o como imagen. **El PDF de Siigo no
trae el sello de firma** —solo el rótulo preimpreso `Aceptada Firma`—; el sello
está en la captura de pantalla.

## Almacenamiento

Todo vive en un solo archivo SQLite.

| Tabla | Qué guarda |
|---|---|
| `lotes` | encabezado del lote |
| `registros` | columnas B a G y J |
| `renglones` | una fila por factura con todas sus columnas y observaciones |
| `documentos` | cada PDF o imagen con su **hash SHA-256** |
| `eventos` | bitácora de lo que se validó y exportó, con fecha |

Los PDF quedan en disco nombrados por su hash, así que cargar el mismo archivo
dos veces no lo guarda dos veces. Para respaldar, se copia el `.db`.

Si existía un `trabajo.json` de una versión anterior, se migra solo al arrancar.

### Consultas

- `GET /api/resumen` — todos los registros de todos los lotes con sus
  diferencias L y R y su estado (`cuadra`, `descuadre`, `sin validar`).
- `GET /api/eventos` — la bitácora.
- `GET /api/mantenimiento` — archivos que ya nadie referencia;
  `POST /api/mantenimiento/limpiar` los borra.

## Segunda opinión con IA (opcional)

Con `ANTHROPIC_API_KEY` configurada aparece un botón que le pide el mismo
veredicto del correo a Claude, con esquema JSON fijo. **No hace falta para
nada**: el motor de reglas cubre el flujo completo y sin la variable el botón no
se muestra.

## Estructura

| Archivo | Rol |
|---|---|
| `app/server.py` | servidor Flask y API |
| `app/bd.py` | almacenamiento en SQLite |
| `app/extractor.py` | lectura de PDF y detección de montos |
| `app/decisor.py` | decide el valor aprobado del correo |
| `app/comprobantes.py` | egresos y registros contables de Siigo |
| `app/factura_electronica.py` | factura electrónica: NIT, CUFE, QR, cantidad |
| `app/radian.py` | RADIAN con OCR |
| `app/orden_compra.py` | orden de compra |
| `app/entrada.py` | entrada de inventario (PDF o imagen) |
| `app/excel_io.py` | lee el formato de la plantilla y genera el libro |
| `app/validador_ia.py` | segunda opinión opcional |
| `app/static/index.html` | interfaz |
| `wsgi.py`, `despliegue/` | despliegue con gunicorn, systemd y nginx |

## Lo que falta

| Columna | Qué haría |
|---|---|
| **X** | confirmación de recibido: cantidad igual a la de entrada y las dos firmas |
| **Y** | comentarios del proceso |

## Advertencias

- **No tiene control de acceso.** Quien llegue a la URL ve y modifica todo. Antes
  de exponerla, ponerle autenticación (ver `despliegue/README.md`).
- **Ninguna validación reemplaza la revisión.** La herramienta señala, no
  aprueba: cuando no puede leer un dato lo dice en vez de darlo por bueno.
- **Los datos no van al repositorio.** El `.gitignore` excluye los soportes, la
  base de datos y cualquier `.xlsx`, porque son facturas y egresos reales.
