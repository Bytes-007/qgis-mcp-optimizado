# Cutting MCP context cost ~99% with zero data loss (measured A/B benchmark)

> **TL;DR for the maintainer:** the plugin's biggest context hogs are two single calls —
> `list_processing_algorithms` (~16.5k tokens) and `execute_code` dumping stdout (~250k tokens
> for a 1 MB print). Capping them, adding opt-in field projection and a `max_value_chars`
> clip cuts those to ~50 / ~2,000 / ~200 tokens **while a 21/21 A/B suite verifies the
> returned content is identical to upstream**. New params are opt-in; default behavior
> is byte-identical (only additive keys when a cap actually bites). Reproduce with
> `uv run --no-sync benchmarks/{context_bench,quality_ab}.py`.

Comparativa medida: plugin original vs optimizado

**Fecha:** 2026-10-22 · **Entorno:** QGIS 3.44 LTR con el mismo proyecto abierto en dos instancias.

| Instancia | Puerto | Plugin |
|---|---|---|
| QGIS normal | 9876 | original (upstream, tags v0.15.2) |
| QGIS dev | 9877 | optimizado (rama mejorada: tope de stdout, topes en listados, proyección de campos) |

## Metodología

Mismas 6 llamadas sobre el mismo proyecto/capa en ambos puertos, usando `QgisMCPClient`
directo (socket, sin MCP intermedio). El "costo" se mide en bytes del payload JSON de la
respuesta; los tokens se estiman a ~4 bytes/token.

Reproducible con: `uv run --no-sync benchmarks/context_bench.py` (con los dos QGIS levantados).

## Resultados medidos (bytes de respuesta)

| Llamada | Original | Optimizado | Nota |
|---|---:|---:|---|
| `list_processing_algorithms` () | **66,706** | **213** | el original vuelca 759 algoritmos; el optimizado devuelve resumen + paginación |
| `execute_code` imprimiendo ~1 MB | **1,000,064** | **8,180** | stdout truncado con `stdout_truncated` + `stdout_total_chars` |
| `get_layer_features` + `fields=['region','area_ha']` | **error** (parámetro no aceptado) | 158 | función nueva: el original ni siquiera la acepta |
| `get_layer_features` celda de 3.000 chars + `max_value_chars=100` | ~3,077/feature | ~202/feature | opt-in: `…[2900 chars omitted]` + `value_chars_clipped` |
| `execute_code` con traceback | 477 | 410 | el optimizado evita duplicar la excepción |
| `get_layers` (limit 3) | 558 | 589 | equivalente (+ paginación) |
| `get_layer_features` (3 feats, todos los campos) | 357 | 357 | **byte-idéntico** |

## Estimación de tokens (÷4, aproximación sin tokenizer)

| Llamada | Original | Optimizado | Ahorro |
|---|---:|---:|---:|
| `list_processing_algorithms` | ~16,500 tok | ~50 tok | **~99.7%** |
| `execute_code` salida 1 MB | ~250,000 tok | ~2,000 tok | **~99.2%** |

Nota: la proyección de campos (`fields=`) no es un "ahorro" comparable de llamada a
llamada — en el original la llamada se **rechaza** (error, sin datos); en el optimizado
devuelve los features pedidos con solo las columnas proyectadas. Con 50 features, fue
de 3,801 bytes (todos los campos) a 2,041 bytes; en capas de muchas columnas la
diferencia es proporcionalmente mayor.

## Verificación de calidad (no solo menos tokens)

Menos bytes solo cuenta si la información que llega sigue siendo correcta. Se ejecutó una
batería de 21 comprobaciones A/B (`uv run --no-sync benchmarks/quality_ab.py`, mismas
llamadas a ambos puertos, comparación campo a campo): **21/21 sin diferencias de contenido**.

Qué se verificó:

- `get_layers`: mismos ids y mismo orden; el contenido de cada capa compartida es idéntico —
  el optimizado solo añade las claves `count`/`truncated` de paginación.
- `get_project_info`: filename, título, CRS, capas y `layer_count` iguales.
- `get_layer_features` (todos los campos): payload **byte a byte idéntico** entre ambos plugins.
- Proyección de campos (`fields=['NOMBRE_VER','NOMB_MPIO']`): el optimizado devuelve
  exactamente el mismo slice de columnas que el original entrega en su respuesta completa
  (el original rechaza el parámetro, la comparación se hace contra sus filas completas) —
  solo se eliminan columnas no pedidas, nunca se alteran valores.
- Filtro por `expression`: mismos features.
- `list_processing_algorithms`: los recuentos por proveedor son iguales en todos los
  proveedores comunes; los 20 ids de algoritmos muestreados del volcado completo del
  original son todos descubribles en el optimizado vía paginación por proveedor
  (`list_processing_algorithms {provider:native, limit:100}`) y la búsqueda (`buffer`)
  devuelve los mismos 12 resultados.
- `execute_code` con salida enorme: bandera `stdout_truncated` correcta, se conserva la
  cabeza de la salida, y los resultados pequeños son idénticos.
- `max_value_chars` (opt-in, nuevo): con valores normales la respuesta es idéntica a la
  llamada sin el parámetro; con un valor forzado de 3.000 caracteres se recorta a 121
  (`…[2900 chars omitted]`, `value_chars_clipped: true`), ahorrando ~2.875 bytes por
  feature sin tocar números ni geometría. El upstream rechaza el parámetro.
- `get_message_log` / `get_layer_info`: mismas claves y mismos nombres de campos.

Única diferencia ambiental (no causada por el fork): el perfil "QGIS normal" tiene el
plugin externo QuickOSM instalado (12 algoritmos más). El fork no oculta ni altera
ningún algoritmo de los proveedores que ambos comparten.

Nota sobre resultados grandes (>20 features): al nivel del servidor MCP, el fork
corrige un bug de upstream — cuando los features se cachéan como recurso
(`features_resource`), ya no permanecen *también* en la respuesta (pagaban el
contexto dos veces). Esta corrección no se ve en la batería: la cliente raw
(`client.py`) nunca pasa por esa capa; la comparación A/B mide el nivel plugin.

## Por qué importa

La`list_processing_algorithms` sin tope y el `execute_code` sin recorte son las dos vías por
las que una sola llamada puede consumir un ventana de contexto entera; el plugin optimizado
las acota siempre, no solo en casos felices. Ninguna de las dos diferencias requiere cambiar
el flujo de trabajo: los parámetros nuevos (limit/offset, `fields`) son opcionales.
