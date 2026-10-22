# Análisis del plan de optimización v2 contra el código fuente (qgis-mcp)

Fecha: 2026-10-22 · Base: `plan_optimizacion_ia_v2.md` + `Codigo_Fuente.md` (Repomix, v0.15.2 + trabajo local)

**Cómo leer este documento.** Cada afirmación lleva una etiqueta:

- **[Verificado]**: lo vi directamente en el código del archivo adjunto.
- **[Hipótesis]**: depende de algo que no está en este repo (SDK de MCP, comportamiento de un cliente, tamaño real de tus datos). Hay que medirlo antes de actuar.
- **[Estimación]**: orden de magnitud calculado a mano. No lo cites en un PR sin medirlo.

Limitación: el archivo Repomix elimina las líneas vacías, así que **los números de línea del plan no se pueden comprobar**. Verifiqué el contenido y el nombre de cada función, no su posición.

---

## 1. Veredicto

**El plan es sólido en lo que cubre, pero le faltan los problemas más baratos de arreglar y de mayor impacto.** La Fase 1 y la Fase 2 son correctas. Pero hay tres fugas de contexto que el plan no ve, y una de ellas está listada en su §6 como "ya hecho" cuando en realidad *aumenta* los tokens.

| Elemento del plan | Veredicto | Ajuste necesario |
|---|---|---|
| Fase 0 · presupuesto bajo test | **Recomendada, con 2 correcciones** | La medición de compound en el test no coincide con la de la tabla (§4.1) |
| Fase 1 · tope en `execute_code` | **Muy recomendada** | Recortar conservando el **final** del traceback, no solo el inicio (§4.2) |
| Fase 2 · `fields` en `get_layer_features` | **Recomendada** | Combinarla con formato tabular y truncado de celdas; ahí está el grueso del ahorro (§4.3, §3.4) |
| Fase 3a · documentar granular vs compound | **Recomendada** (coste cero) | Añadir que el chat embebido siempre usa granular (§3.3) |
| Fase 3b · recortar descripciones | **Baja prioridad** | Ahorra ~2k chars; un perfil de herramientas ahorra ~10× más (§3.3) |
| No-objetivos (caché de metadatos, `bbox`, imágenes) | **De acuerdo con los tres** | |
| §6 "ya hecho" | **Corregir una línea** | "Resultados grandes → recurso" no ahorra nada (§3.1) |

---

## 2. Qué coincide con el plan (verificado)

- `execute_code` vuelca `stdout`/`stderr` completos y `traceback.format_exc()` sin tope. **[Verificado]** en `qgis_mcp_plugin/handlers/system.py`, función `execute_code`, dentro del helper `output()`.
- `get_layer_features` serializa **todos** los campos de cada entidad, sin proyección. **[Verificado]** en `handlers/features.py`: el bucle `for field in layer.fields()`.
- `include_geometry=False` por defecto; para polígonos y líneas devuelve resumen + bbox y no WKT completo. **[Verificado]**
- `limit > 50` se rechaza con mensaje (`helpers.feature_limit_error`, `MAX_FEATURE_LIMIT = 50`). **[Verificado]**
- `get_message_log` devuelve `truncated`. **[Verificado]**
- `MAX_MESSAGE_SIZE = 10 MB` en `protocol.py`. **[Verificado]**
- `_strip_schema_titles()` y `_strip_instance_param()` ya existen en `server.py` y se ejecutan al importar. **[Verificado]** El plan los da por hechos pero no los menciona en §6; conviene añadirlos, porque son la razón por la que el test de Fase 0 puede dar números distintos (§4.1).

---

## 3. Hallazgos nuevos (ordenados por impacto / esfuerzo)

### 3.1 La "caché de resultados grandes a recurso" no ahorra tokens: los duplica  ·  **P0**

**[Verificado]** En `src/qgis_mcp/server.py`, `get_layer_features`:

```python
if len(result.get("features", [])) > 20:
    uri = _cache_as_resource(result["features"])
    result["features_resource"] = uri
    result["_hint"] = f"Result contains many features. You can also access them via {uri}"
return result
```

`result["features"]` **no se elimina**. La respuesta lleva las 21-50 entidades completas *y además* la URI y el texto del hint. El test `test_get_layer_features_caches_on_result_size_not_limit` solo comprueba que `features_resource` aparezca, no que la respuesta sea más corta.

Además, `_cache_as_resource` hace `json.dumps(data)` de cada resultado grande y lo guarda en memoria (hasta 32 entradas), sin que nadie lo lea en la mayoría de sesiones. El propio comentario del código lo admite: *"entries are never read back in most sessions"*.

**Qué hacer (elige una):**

1. **Eliminarlo.** Con `limit ≤ 50` y paginación por `offset`, el recurso no aporta nada. Es la opción más simple y quita el hint.
2. **Convertirlo en descarga real**: devolver solo `features[:5]` (vista previa) + `features_resource` + `total`. Riesgo: muchos clientes MCP no exponen la lectura de recursos al modelo, así que el modelo perdería datos sin poder recuperarlos. Solo si has comprobado que tu cliente (opencode) lee `qgis://cache/...`.

Recomiendo la 1. Corrige también la línea del §6 del plan.

---

### 3.2 `list_processing_algorithms` no tiene tope  ·  **P0**

**[Verificado]** El handler (`handlers/processing.py`) itera `registry.algorithms()` completo y, si no se pasa `search` ni `provider`, devuelve **todos**. La tool (`server.py`) tampoco lo limita. Es el mismo tipo de riesgo que `execute_code`, pero con un solo `list_processing_algorithms()` sin argumentos.

**[Estimación]** Cada entrada `{"id","name","provider"}` ocupa ~60-70 chars. Con solo los proveedores nativos y GDAL son unos cientos de algoritmos (~20k chars, ~5k tokens); con GRASS/SAGA instalados puede pasar de 1.000 (~70-100k chars, ~20k+ tokens) en una sola llamada. Mídelo en tu QGIS con `len(json.dumps(...))`.

**Qué hacer:**

- Parámetro `limit` (defecto 25-50) con tope duro (p. ej. 100) y campos `count`, `total`, `truncated` igual que `get_message_log`.
- Si no hay `search` ni `provider`, devolver **solo los proveedores con su número de algoritmos** (`{"providers": {"native": 280, "gdal": 60, ...}}`) y un mensaje: "pasa `search` o `provider`". Un modelo que no sabe qué buscar gana más con eso que con 1.000 filas.

**Otros listados sin tope, mismo patrón (revisar):**

| Comando | Qué pasa | Arreglo mínimo |
|---|---|---|
| `get_layers` | `limit` por defecto 50 pero sin máximo; el handler corta `all_layers[offset:offset+limit]` | tope duro 100 |
| `get_message_log` | `limit=100` sin máximo; cada mensaje sin recortar | tope duro 200 y mensajes a ≤500 chars |
| `list_plugins` | devuelve todos los plugins disponibles con `path` completo | quitar `path` por defecto (campo de bajo valor para el modelo) |
| `get_field_statistics` | `distinct_values` limita a 50 valores pero **no su longitud** | truncar cada valor a ~80 chars |
| `list_connection_tables` | no lo revisé a fondo | comprobar si pagina |

---

### 3.3 El chat embebido siempre carga las 125 herramientas, también en modo "Plan"  ·  **P1**

**[Verificado]** `mcp_pack.desired_entries()` registra el servidor con `uvx --from <zip> qgis-mcp-server` **sin variables de entorno de modo**, y `chat_runner.agent_env()` solo inyecta `QGIS_MCP_PORT`. Resultado: el agente embebido (opencode) arranca siempre en `granular`, con 125 herramientas (~13k tokens según tu medición). Encima carga otros dos servidores MCP (`filesystem` y `sequential-thinking`) cuyas herramientas también se envían cada turno.

**[Verificado]** El interruptor Plan/Act (`allow_modify`) solo cambia flags de permiso (`--auto` en opencode, el scope de `claude`). **No reduce las herramientas anunciadas**: en modo Plan el modelo sigue viendo las 18 herramientas destructivas y las 44 de solo lectura.

**Por qué importa más que el plan.** El plan argumenta bien que con *prompt caching* el bloque de herramientas cuesta ~10%. Pero ese argumento es válido para Anthropic directo. El chat embebido usa opencode, que habla con muchos proveedores (OpenRouter, etc.); **varios no ofrecen descuento por caché**, y entonces esos ~13k tokens (más los de `filesystem` y `sequential-thinking`) se pagan completos en **cada** llamada de cada turno, incluidas las llamadas intermedias del bucle de herramientas. Es el caso de uso donde más duele y el plan lo trata como secundario.

**Propuesta: perfiles de herramientas** (más fino que granular/compound y sin perder tipado ni `instance`):

```python
# server.py, antes de _strip_schema_titles() / _strip_instance_param()
_profile = os.environ.get("QGIS_MCP_TOOL_PROFILE", "all")   # all | read-only | core
if _profile == "read-only":
    for name, tool in list(mcp._tool_manager._tools.items()):
        if not (tool.annotations and tool.annotations.readOnlyHint):
            del mcp._tool_manager._tools[name]
elif _profile == "core":
    keep = CORE_TOOLS  # lista explícita, ver abajo
    for name in list(mcp._tool_manager._tools):
        if name not in keep:
            del mcp._tool_manager._tools[name]
```

- `read-only`: las 44 herramientas con `readOnlyHint=True`. **[Estimación]** con la media del plan (52.842 / 125 ≈ 423 chars/herramienta) son ~18k chars frente a ~53k: **−65%**.
- `core` (~15-20 herramientas): lectura de capas, `get_layer_features`, `get_field_statistics`, `list_processing_algorithms`, `get_algorithm_help`, `execute_processing`, añadir/quitar capas, `render_map`, `execute_code`. Las herramientas más caras del plan (`create_processing_model`, `create_postgresql_connection`, `set_raster_style`, `spatial_join`) quedarían fuera y se cubren con `execute_code` o con un segundo perfil. **[Estimación]** ~7-9k chars: **−80% o más**. Los nombres de la lista son ilustrativos; valídalos contra el listado real.
- El chat embebido pasaría `QGIS_MCP_TOOL_PROFILE=read-only` en modo Plan y `core` (o `all`) en modo Act.

**Cuidados:**

1. **[Hipótesis]** Revisa `ServeManager` en `chat_runner.py`: si el `opencode serve` caliente vive entre turnos, el listado de herramientas queda fijado al arrancar. Cambiar de Plan a Act exigiría reiniciar el serve, o registrar dos entradas MCP (`qgis-ro`, `qgis-rw`) y habilitar una u otra por turno.
2. Usa el mismo mecanismo de borrado (`_tool_manager._tools`) que ya usa el modo compound. Es API privada del SDK; el código ya depende de ella, pero un test que la ejercite avisará si `mcp >= 2.x` la cambia.
3. **Calidad**: menos herramientas suele mejorar la selección, pero no hay forma barata de comprobarlo con tu suite. Haz 3-5 tareas guionadas antes y después (§6).
4. `sequential-thinking` (pack "Razonamiento") añade una herramienta con descripción larga y **invita al modelo a hacer más llamadas**, cada una con el contexto completo. Decisión de producto: valora dejarlo desactivado por defecto y medir.

---

### 3.4 `get_layer_features` repite los nombres de campo en cada entidad  ·  **P1**

**[Verificado]** Cada entidad es un dict plano `{"_fid":…, "campo1":…, "campo2":…}`. Con 40 campos y 50 entidades, los 40 nombres se repiten 50 veces.

**[Estimación]** Si el nombre medio mide ~10 caracteres, las claves cuestan ~14 chars por campo (comillas, dos puntos, coma) → ~560 chars por entidad solo en claves, probablemente más de la mitad del payload. Un formato tabular elimina esa repetición.

Ya hay precedente en tu código: `handlers/connections.py` devuelve `"rows": [[...], ...]` para consultas SQL. Es el mismo patrón.

```python
columns = ["_fid"] + projected_names            # + "_geometry" si include_geometry
rows.append([feature.id()] + [self._convert_attribute(feature.attribute(n)) for n in projected_names])
return {"feature_count": ..., "matched": ..., "columns": columns, "rows": rows, "crs": ...}
```

**Compatibilidad:** añade `format: "records" | "table"` con `"records"` por defecto (los tests actuales esperan `features`), mide, y si el ahorro es el esperado cambia el defecto en una versión menor con nota de cambios.

**Dos extras baratos en el mismo cambio:**

- **Truncar celdas largas** (`max_value_chars`, defecto ~200). Una columna de descripciones de 5.000 caracteres anula cualquier otra optimización. Marcar con `…[+N]`.
- **Omitir nulos** en formato `records` (`drop_nulls`) o dejarlos como `null` en `table` (cuestan 4 chars).

La Fase 2 (`fields`) y esto se complementan: `fields` reduce columnas, `table` elimina la repetición de las que quedan.

---

### 3.5 Serialización de respuestas: indentación y datos duplicados  ·  **P1, medir primero**

**[Hipótesis]** Esto depende del SDK de MCP (`mcp[cli]>=1.20,<3`), no de tu código. En el SDK `FastMCP`, un resultado `dict` se convierte en contenido de texto con JSON **indentado**, y con `structured_output=True` el mismo dato viaja además como `structuredContent`. Según el cliente, el modelo puede recibir una de las dos copias o ambas.

**Qué comprobar (15 minutos):** en un test con el `mock_connection` existente, llama a `srv.mcp.call_tool("get_layers", {})` con una respuesta grande simulada y mira (a) si el texto lleva saltos de línea e indentación y (b) si `structuredContent` está presente. Después revisa en los logs de opencode qué parte llega al modelo.

**Si se confirma:**

- Las herramientas con respuestas grandes (`get_layer_features`, listados) podrían devolver un `CallToolResult` con `TextContent` en JSON compacto (`separators=(",", ":")`, `ensure_ascii=False`).
- Quitar `structured_output=True` también elimina el `output_schema` (**[Verificado]** en el plan: 3.962 chars, ~7,5% del bloque de herramientas). Contrapartida: pierdes la validación de esquema de salida. Pruébalo primero en **una** herramienta.
- Nota: el plugin ya serializa con `json.dumps(...)` por defecto (con espacios tras `,` y `:`); en el socket local no importa, solo importa lo que sale hacia el modelo.

---

### 3.6 Compound repite la misma nota 27 veces  ·  **P2**

**[Verificado]** `compound_tools.py` añade `_PARAMS_NOTE` (~155 chars) a la descripción de **cada** una de las 27 herramientas, y las descripciones tienen líneas como `- ping: no params`, `- diagnose: no params`.

**[Estimación]** ~4.000 chars solo en la nota repetida, cerca del 15-20% de las descripciones de compound (20.011). Opciones: moverla una sola vez a `instructions` del `FastMCP`, o reducirla a "Parámetros dentro de `params`." (~35 chars). Ahorra ~3.000 chars sin tocar ninguna acción. Colapsar líneas "no params" en una sola ("sin parámetros: ping, diagnose, get_qgis_info") ahorra algo más.

Esto cambia la lectura del plan: la afirmación de que "agrupar casi no reduce las descripciones" es cierta, pero parte de lo que queda es **repetición pura**, no contenido.

---

### 3.7 Otras observaciones (no son tokens, pero conviene anotarlas)

| Tema | Observación | Prioridad |
|---|---|---|
| **Credenciales en `get_layer_info`** | **[Verificado]** devuelve `layer.source()` tal cual. En capas PostgreSQL ese string puede incluir usuario y, según cómo se guardó, contraseña. **[Hipótesis]** si QGIS la enmascara depende de la versión y del almacenamiento. Pruébalo con una conexión real; si aparece, enmascarar `password=` antes de devolverlo. Es de seguridad, y además se acumula en el historial de la conversación. | P1 (seguridad) |
| **`NoGeometry` en `get_layer_features`** | **[Verificado]** con `include_geometry=False` el `QgsFeatureRequest` no activa `NoGeometry`, así que el proveedor carga geometrías que luego se descartan. Es rendimiento, no tokens. **[Hipótesis]** si el `expression` usa `$geometry`, QGIS debería pedirla por su cuenta; confírmalo en `test_qgis_live.py` antes de activar el flag. | P3 |
| **Paginación con `offset`** | **[Verificado]** recorre y descarta `offset` entidades en Python. Con capas grandes y offsets altos es O(offset). No es un problema de tokens. | P3 |
| **Mensaje de error duplicado** | **[Verificado]** `error=str(e)` y `traceback` contienen la misma línea final, y `code_failure_message` los concatena. Con el recorte de Fase 1, deja el mensaje en `error` y envía solo las últimas N líneas de traceback. | P2 |

---

## 4. Correcciones a las fases del plan

### 4.1 Fase 0 — el test puede medir otra cosa que la tabla

**Problema 1: compound se mide distinto.** El plan mide compound importando `qgis_mcp.server` con `QGIS_MCP_TOOL_MODE=compound`, y el test lo mide registrando `register_compound_tools` en un `FastMCP` limpio. **[Verificado]** `_strip_schema_titles()` y `_strip_instance_param()` se aplican solo sobre el `mcp` global de `server.py`. En el `FastMCP` limpio no se aplican, así que el test medirá un número distinto de 28.215 (con títulos e `instance` sin quitar). El umbral de 29.500 puede quedarse corto o sobrado sin que nadie lo note.

**Arreglo:** refactorizar ambas funciones para que reciban el servidor (`_strip_schema_titles(mcp_obj)`) y llamarlas en el test sobre el `FastMCP` limpio, o medir ambos modos importando el módulo en un subproceso. Lo primero es más rápido y no requiere subprocess.

**Problema 2: dependencia del entorno.** `_strip_instance_param` depende de `get_instances()`, que lee `QGIS_MCP_INSTANCES`. Si el entorno de CI o de tu máquina define dos instancias, el bloque crece ~17% (el propio docstring lo dice: 45,8k → 53,7k) y el test falla sin que nada haya cambiado. El test debe hacer `monkeypatch.delenv("QGIS_MCP_INSTANCES", raising=False)` y fijar `ensure_ascii=False`, como ya señala el plan.

**Ajuste menor:** el margen del 4% sobre granular (55.000 vs 52.842) está bien para detectar una herramienta nueva de 1.500 chars, pero se come con tres o cuatro herramientas pequeñas. Considera 54.000 y subirlo conscientemente cuando toque.

### 4.2 Fase 1 — dónde recortar

El plan pone el marcador "al final del texto", lo que sugiere conservar el **inicio** y cortar el final. Para `stdout` es razonable. Para el **traceback es lo contrario**: la línea que importa (`ValueError: ...`) está al final. Un traceback de 12.000 chars recortado por la cola pierde justo la excepción.

```python
def _clip(text, limit, keep_tail=0):
    total = len(text)
    if total <= limit:
        return text, False, total
    omitted = total - limit
    if keep_tail:
        head = limit - keep_tail
        return (text[:head] + f"\n…[truncated: {omitted} of {total} chars omitted]…\n"
                + text[-keep_tail:]), True, total
    return text[:limit] + f"\n…[truncated: {omitted} of {total} chars]", True, total
```

- `traceback`: `_clip(tb, 8000, keep_tail=6000)`.
- `stdout`/`stderr`: inicio y final (`keep_tail=2000`): en bucles largos suele interesar la última línea (el estado final).
- **Recorta también `error`** (`str(e)` puede contener el `repr` de un objeto enorme) a ~2.000 chars. El plan solo lista `stdout`, `stderr` y `traceback`.
- **[Verificado]** el camino `timed_out` también pasa por `output()`, así que queda cubierto si el recorte vive ahí.
- Mantén el recorte compatible con Python 3.9 (sin `str | None` en runtime), como pide `test_py39_compat.py`.
- 8.000 chars por stream son ~2k tokens cada uno; en el peor caso (stdout + stderr + traceback) ~6k tokens. Es razonable como tope, pero si tus sesiones suelen usar `print` para depurar, 4.000 de defecto con `max_output` para ampliar es más agresivo y sigue siendo útil.

### 4.3 Fase 2 — `fields` y la expresión

El plan afirma que `QgsFeatureRequest` evalúa el filtro antes del subconjunto de atributos y lo delega a un test vivo. Es lo correcto: **[Hipótesis]** no lo puedo confirmar desde este código. Añade a `test_qgis_live.py` un caso concreto: `fields=["a"]` con `expression='"b" > 5'`, donde `b` **no** está en `fields`. Si QGIS no añade las columnas referenciadas por la expresión, el filtro devolvería resultados vacíos o erróneos *en silencio*, que es peor que no tener la optimización. Si falla, incluye en el subconjunto los campos de `QgsExpression(expression).referencedColumns()` y no los devuelvas.

Otros puntos de la Fase 2:

- La clave `fields` ya existe en la **respuesta** (lista de nombres). Usar el mismo nombre en la **petición** es coherente, pero `create_memory_layer` usa `fields` con otro significado (definiciones de campo). Es otro comando, no choca, pero deja la descripción de la tool inequívoca ("columnas a devolver").
- Si añades el formato tabular (§3.4), la respuesta pasa a llevar `columns` en vez de `fields`; decide el nombre una sola vez para no migrar dos veces.

### 4.4 Fase 3b — recortar descripciones

El objetivo (−2.170 chars, −9% de descripciones) vale ~540 tokens. Un perfil `read-only`/`core` (§3.3) ahorra del orden de 8-10k tokens y no exige reescribir 125 descripciones ni comprobar que la selección de herramientas no empeora. **Haz 3a, deja 3b para después de los perfiles, y solo con una forma de medir calidad.** El test de Fase 0 ya evita que crezca.

---

## 5. Plan de acción propuesto (reordenado)

| # | Cambio | Impacto | Esfuerzo | Riesgo | Notas |
|---|---|---|---|---|---|
| 1 | Quitar la caché de recursos duplicada (§3.1) | Medio, constante | XS | Muy bajo | ~10 líneas + test; corregir §6 |
| 2 | Tope en `list_processing_algorithms` y demás listados (§3.2) | **Alto** (evita 20k+ tokens en una llamada) | S | Bajo | Mismo estilo que `get_message_log` |
| 3 | Fase 1: tope en `execute_code` con head+tail (§4.2) | Alto (defensivo) | M | Bajo | Añadir los tests del handler que hoy no existen |
| 4 | Fase 0: test de presupuesto corregido (§4.1) | Protege todo | S | Bajo | Antes de tocar herramientas |
| 5 | Perfiles de herramientas + uso en el chat embebido (§3.3) | **Muy alto** en clientes sin caché | M | Medio (ServeManager, calidad) | Mide con tareas guionadas |
| 6 | Fase 2 + formato tabular + truncado de celdas (§3.4, §4.3) | Alto en capas anchas | M | Medio (API pública) | Una sola PR |
| 7 | Medir serialización / `structuredContent` (§3.5) | Posiblemente alto | S medir, M actuar | Medio | Solo si la medición lo confirma |
| 8 | Fase 3a: README con cifras y los 3 costes de compound | Informativo | S | Ninguno | Añadir perfiles y que el chat usa granular |
| 9 | Deduplicar `_PARAMS_NOTE` en compound (§3.6) | Bajo-medio, solo compound | XS | Bajo | |
| 10 | Seguridad: enmascarar credenciales en `get_layer_info` (§3.7) | Seguridad | S | Bajo | Verificar primero que ocurre |
| 11 | `NoGeometry`, paginación, error duplicado | Bajo | S | Bajo | Oportunista |
| 12 | Fase 3b: recortar descripciones | Bajo | M | Medio (calidad) | Solo con forma de medir |

Agrupación sugerida en PRs: **PR A** = 1+2+3 ("topes de salida"), **PR B** = 4, **PR C** = 5, **PR D** = 6, **PR E** = 8+9.

---

## 6. Cómo medir (antes y después de cada cambio)

**Bloque de herramientas.** El script del plan sirve; añade el perfil y la variable de entorno limpia:

```python
import json, os, sys
os.environ.pop("QGIS_MCP_INSTANCES", None)       # evita que el test mida otra cosa
sys.path.insert(0, os.path.abspath("src"))
import qgis_mcp.server as s
tools = s.mcp._tool_manager.list_tools()
def size(t):
    return (len(t.description or "")
            + len(json.dumps(t.parameters, ensure_ascii=False, default=str))
            + len(json.dumps(getattr(t, "output_schema", None) or {}, ensure_ascii=False, default=str)))
print(len(tools), sum(size(t) for t in tools))
ro = [t for t in tools if t.annotations and t.annotations.readOnlyHint]
print("solo lectura:", len(ro), sum(size(t) for t in ro))
```

**Tamaño de respuestas.** Contra la instancia de desarrollo (puerto 9877) con `QgisMCPClient`, registra `len(json.dumps(resultado))` de estos escenarios, antes y después:

| Escenario | Qué demuestra |
|---|---|
| `get_layer_features` 50 entidades × 40 campos, con y sin `fields`/`table` | §3.4 y Fase 2 |
| `list_processing_algorithms()` sin argumentos | §3.2 |
| `get_layers` con 50 capas | tope |
| `get_message_log(limit=100)` | tope |
| `execute_code("print('x'*10**6)")` | Fase 1 (criterio O1 del plan) |
| `execute_code` con un traceback profundo | que se conserve la última línea |
| `list_plugins()` | quitar `path` |

**Sesión completa.** El coste real es tokens de entrada acumulados, no el tamaño de una respuesta. Define 3 tareas guionadas (p. ej. "filtra una capa y calcula estadísticas", "buffer + render", "crea una capa y estilízala"), ejecútalas con opencode contra el mismo modelo, y compara tokens de entrada totales y número de llamadas con `granular`, `read-only` y `core`. También sirve para comprobar que reducir herramientas no empeora la selección. Este es el único dato que decide si los perfiles merecen la pena.

---

## 7. Lo que no recomiendo

- **Caché de metadatos de proyecto/capas**: de acuerdo con el plan. Se invalida con cualquier `execute_code`.
- **Reducir la resolución de `render_map`/`get_canvas_screenshot`**: de acuerdo con el plan; cambia lo que el usuario ve.
- **Resumir automáticamente resultados con otro modelo**: añade latencia, coste y puede perder el dato exacto.
- **Cambiar compound como solución general**: pierde `instance`, tipado y anotaciones por acción (los tres costes del plan, correctos). Los perfiles dan un ahorro mayor sin esas pérdidas.
- **Reescribir las 125 descripciones ahora**: el retorno es bajo y no hay forma barata de comprobar que no empeora la elección de herramientas.

---

## 8. Riesgos y puntos sin verificar

1. **Comportamiento del SDK de MCP** (§3.5): hipótesis. No cambies nada hasta medirlo.
2. **`ServeManager` y cambio de perfil** (§3.3): no analicé a fondo cómo se reinicia el `opencode serve` caliente.
3. **Proveedores sin caché**: la importancia de §3.3 depende de qué modelos usan tus usuarios con opencode. Si casi todos van por Anthropic con caché, baja a P2.
4. **Tamaños reales**: todas las cifras [Estimación] salen de medias; tus capas, tus proveedores de Processing y tus mensajes de log reales mandan.
5. **Calidad de selección de herramientas**: nada de este análisis la mide; hazlo con las tareas guionadas.
6. **Código de la versión local**: algunas funciones (p. ej. `journaling` de mutaciones en `session.py`) no las analicé; no afectan a lo anterior pero conviene revisarlas si añades campos a las respuestas.

---

## 9. Apéndice: dónde aterriza cada cambio

| Cambio | Archivo → función |
|---|---|
| Quitar caché a recurso | `src/qgis_mcp/server.py` → `get_layer_features`, `_cache_as_resource`, `cached_resource` |
| Tope de listados | `qgis_mcp_plugin/handlers/processing.py` → `list_processing_algorithms`; `handlers/layers.py` → `get_layers`; `handlers/system.py` → `get_message_log`, `list_plugins`; `handlers/features.py` → `get_field_statistics` |
| Tope de `execute_code` | `handlers/system.py` → `execute_code` (helper `output`); `src/qgis_mcp/helpers.py` → `code_failure_message`; `server.py` → tool `execute_code`; `compound_tools.py` → grupo `code` |
| Perfiles de herramientas | `server.py` → bloque "Compound tool mode" (justo antes de `_strip_schema_titles`); `qgis_mcp_plugin/chat_runner.py` → `agent_env`; `mcp_pack.py` → `desired_entries` (entorno del servidor `qgis`) |
| Proyección + formato tabular | `handlers/features.py` → `get_layer_features`; `server.py` y `client.py` → tool/método `get_layer_features`; `compound_tools.py` → grupo de features |
| Test de presupuesto | `tests/test_context_budget.py` (nuevo); `server.py` → `_strip_schema_titles`, `_strip_instance_param` (aceptar el servidor como argumento) |
| Deduplicar nota compound | `compound_tools.py` → `_PARAMS_NOTE` y las descripciones de cada grupo |
| Enmascarar credenciales | `handlers/layers.py` → `get_layer_info` (campo `source`) |

Como indica el propio plan para PRs contra upstream: ancla por **nombre de función**, no por número de línea, y vuelve a medir antes de fijar presupuestos.
