# Plan de optimización de contexto — v2.1 (qgis-mcp)

**Sustituye a `plan_optimizacion_ia.md` y a la v2.** Integra el análisis externo
(`analisis_plan_optimizacion_qgis_mcp.md`) **después de verificarlo contra el código**: lo que
resistió la medición entra como fase; lo que no, está en §8 con el motivo.

Cada cifra marcada **[medido]** salió de ejecutar el código de este checkout (mcp 1.26, hoy).
Las marcadas **[estimar]** son órdenes de magnitud y no deben citarse en un PR.

Fecha: 2026-10-22 · Ámbito: `qgis_mcp_plugin/` + `src/qgis_mcp/`.

---

## 1. Línea base medida

**Métrica, definida una sola vez** (la que usará el test de Fase 0):

```
size(tool) = len(description)
           + len(json(parameters,    ensure_ascii=False))
           + len(json(output_schema, ensure_ascii=False))   # solo si existe
```

Los **81 de 125** tools sin `output_schema` suman 0. Contarlos como `{}` añade 162 chars al
total y rompe cualquier comparación: es la trampa que hacía que el mismo bloque pareciera medir
52.842 o 53.004 según cómo se escribiera el script.

| Bloque | Tools | Descripciones | Esquema entrada | Esquema salida | **Total** | ≈ tokens |
|---|---|---|---|---|---|---|
| `granular` (defecto) | 125 | 24.170 | 24.710 | 3.962 | **52.842** | ~13,2k |
| `compound`, medido como lo verá el cliente | 27 | 20.011 | 5.292 | 2.912 | **28.215** | ~7,1k |
| `compound`, medido como lo medirá el test (FastMCP limpio, sin `_strip_schema_titles`) | 27 | 20.011 | 7.127 | 2.912 | **30.050** | ~7,5k |
| perfil `read-only` (44 tools con `readOnlyHint`) | 44 | — | — | — | **15.626** | ~3,9k |

**[medido] Notas que cambian decisiones:**

1. `compound` ahorra 46,6% del bloque, pero **el test lo mediría 6,5% más alto** (30.050 vs
   28.215) porque `_strip_schema_titles()` se aplica solo al `mcp` global de `server.py`
   (`server.py:3233-3286`). Los títulos pydantic del `params` de compound son 1.835 chars.
2. Las descripciones son el mayor bulto: 24.170 de 52.842 en granular y 20.011 de 28.215 en
   compound (71%). Y **4.158 chars de esas descripciones son la misma nota repetida 27 veces**
   (`_PARAMS_NOTE`, 154 chars × 27).
3. `read-only` es el mejor ahorro por línea de código: **−70,4%** con 44 tools.
4. **Lado respuesta [medido, mcp 1.26]**: un `dict` de 30 capas viaja al cliente como JSON
   **indentado**: 3.111 chars frente a 1.839 compacto (**+69%**), y además el SDK adjunta
   `structuredContent` con 2.081 chars del mismo dato. El duplicado aparece **también** sin
   `structured_output=True`; ese flag solo quita el `output_schema` del listado.
5. Herramientas más caras en granular: `create_processing_model` 2.019,
   `create_postgresql_connection` 1.727, `set_raster_style` 1.479, `spatial_join` 1.264,
   `execute_processing` 1.206.

```python
# medición reproducible (desde la raíz del repo, con el .venv del proyecto)
import json, os, sys
os.environ.pop("QGIS_MCP_INSTANCES", None)   # si no, el bloque crece ~17% sin que nada cambie
sys.path.insert(0, os.path.abspath("src"))
import qgis_mcp.server as s

def size(t):
    out = getattr(t, "output_schema", None)
    return (len(t.description or "")
            + len(json.dumps(t.parameters, ensure_ascii=False, default=str))
            + (len(json.dumps(out, ensure_ascii=False, default=str)) if out else 0))

tools = s.mcp._tool_manager.list_tools()
print(len(tools), sum(size(t) for t in tools))
print("read-only:", sum(size(t) for t in tools if t.annotations and t.annotations.readOnlyHint))
```

`chars/4` es una aproximación; no hay tokenizer en el venv y no merece la pena añadir uno como
dependencia de test. La métrica es en caracteres para que sea estable y verificable.

**Anclaje de referencias.** Las líneas citadas son de este checkout (`main` = v0.15.2 + trabajo
local) y se verificaron también contra `upstream/main`: `handlers/system.py`, `helpers.py` y
`compound_tools.py` son byte-idénticos allí; en `handlers/features.py` upstream está desplazado
4-7 líneas en la mitad inferior (`get_layer_features` coincide en `:112`). Para un PR contra
upstream, ancla por **nombre de función** y vuelve a medir antes de fijar presupuestos.

---

## 2. Objetivos y criterios de aceptación

| ID | Objetivo | Aceptación verificable |
|---|---|---|
| **O1** | Ninguna respuesta de `execute_code` desborda el contexto | Script que imprime 10⁶ chars → ≤ 8.000 por stream, `stdout_truncated: true`, `stdout_total_chars: 1000000`; un traceback largo conserva su **última** línea; un script normal devuelve lo mismo que hoy |
| **O2** | Ningún listado devuelve el universo entero | `list_processing_algorithms()` sin argumentos devuelve resumen por proveedor + `hint`, no 1.000 filas; todos los topes exponen `count`/`total`/`truncated` |
| **O3** | Las respuestas no viajan con relleno | Las tools grandes devuelven JSON compacto; medido en el cliente antes/después (chars por respuesta y chars por sesión) |
| **O4** | `get_layer_features` puede pedir solo las columnas necesarias | Con `fields=["a","b"]` sobre capa de 40 campos el payload baja proporcionalmente; campo inexistente se rechaza listando los válidos; `expression` sobre columna no proyectada sigue filtrando |
| **O5** | El coste del bloque de herramientas no crece sin que alguien lo vea | `tests/test_context_budget.py` falla con los umbrales de §3.0 y lista las 5 tools más caras |
| **O6** | Existe un perfil de herramientas utilizable por el chat embebido | `QGIS_MCP_TOOL_PROFILE=read-only` registra 44 tools / ~15,6k chars y el chat lo usa en modo Plan, o se documenta por qué no |
| **O7** | La elección granular/compound/perfil es una decisión documentada | README con las cifras de §1 y los tres costes de compound |

### No-objetivos (con motivo)

- **Caché de metadatos de proyecto/capas**: se invalida con cualquier `execute_code`; el ahorro
  son milisegundos en un socket local. Ya existen los recursos `qgis://layers`, `qgis://project`,
  `qgis://layers/{id}/schema` (`server.py:3335-3400`).
- **`bbox` en `get_layer_features`**: `expression` ya cubre el filtrado espacial.
- **Redimensionar imágenes** de `render_map`/`get_canvas_screenshot`: son opt-in y bajar
  resolución cambia lo que ve el usuario.
- **Anotaciones por acción dentro de compound**: MCP anota por herramienta; arreglarlo exige
  volver a tools separadas (que es lo que hacen los perfiles, §3.4).
- **Reescribir las 125 descripciones**: −9% de descripciones ≈ 540 tokens, con riesgo de empeorar
  la selección y sin forma barata de comprobarlo. El presupuesto de Fase 0 ya evita que crezcan.

---

## 3. Fases

### 3.0 · Presupuesto de contexto bajo test (O5) · esfuerzo S · **hacer primero**

`tests/test_context_budget.py`, sin subprocess, con **dos correcciones** que el análisis externo
detectó y que aquí quedan cuantificadas:

1. **Compound no se puede medir en un `FastMCP` limpio tal cual**: da 30.050, no 28.215, porque
   `_strip_schema_titles()` solo se aplica al `mcp` global. Dos salidas válidas: (a) refactorizar
   `_strip_schema_titles(mcp_obj)` / `_strip_instance_param(mcp_obj)` para que acepten el servidor
   y llamarlas en el test —preferido, sin subprocess—; o (b) medir ambos modos en un subproceso
   que importe `server.py` con cada `QGIS_MCP_TOOL_MODE`.
2. **Entorno limpio**: `monkeypatch.delenv("QGIS_MCP_INSTANCES", raising=False)`; con dos
   instancias configuradas el bloque crece ~17% (45,8k → 53,7k según el docstring) y el test
   fallaría sin que nadie haya tocado nada.

```python
BUDGETS = {                    # con el margen ya decidido, no al revés
    "granular": {"total": 54_500, "max_tool": 2_200, "count": TOOL_COUNT},      # hoy 52.842
    "compound": {"total": 30_500, "max_tool": 3_300, "count": COMPOUND_TOOL_COUNT},  # hoy 30.050
}
# Si se elige la opción (a) —strips con el servidor como argumento— el test medirá 28.215
# y el techo de compound debe bajar a 29.000; dejarlo en 30.500 sería medio kilo de holgura.
```

**Mensaje de fallo útil**: las 5 tools más caras con desglose descripción/esquema, para ver qué
creció. Este test es lo que convierte una regresión de tokens en CI rojo.

---

### 3.1 · Tope de salida en `execute_code` (O1) · esfuerzo M · **prioridad 1**

**Problema:** `handlers/system.py:179` vuelca el `StringIO` completo en `stdout`/`stderr` y `:215`
el `traceback.format_exc()` entero. Un `print` en bucle o un `repr()` de una capa mete megabytes
en la conversación; el frame máximo son 10 MB (`protocol.py:22`).

**Diseño (corregido respecto a la v2):**

- Constantes en `handlers/system.py` (py3.9-safe): `_MAX_OUTPUT_CHARS = 8000`,
  `_MAX_OUTPUT_CHARS_CAP = 50000`, `_MAX_TRACEBACK_CHARS = 8000`, `_MAX_ERROR_CHARS = 2000`.
- `_clip(text, limit, keep_tail=0)`: `stdout`/`stderr` conservan **inicio y final**
  (`keep_tail=2000`, porque en un bucle largo la última línea es el estado final);
  **`traceback` conserva la cola** (`keep_tail=6000`), porque la excepción está al final.
- Recortar también **`error`** (`str(e)` puede llevar el `repr` de un objeto enorme). La v2 lo
  olvidaba.
- Flags solo cuando hay recorte: `stdout_truncated`, `stdout_total_chars`, `stderr_*`,
  `traceback_truncated`, `error_truncated`, con marcador `…[truncated: N of TOTAL chars]…`.
- Parámetro `max_output` en el comando y en las tools (granular + grupo `code` de compound), con
  tope duro `_MAX_OUTPUT_CHARS_CAP`.
- `helpers.code_failure_message` (`helpers.py:45`) debe mencionar el recorte y **dejar de repetir**
  el error: hoy concatena `error` y el `traceback`, que acaba en la misma línea.
- `timed_out` también pasa por `output()`, así que queda cubierto sin tocar esa rama.

**Tests:** hoy no existe ninguno del handler `execute_code`. Añadirlos en `tests/test_plugin_server.py`
(fixture `plugin_handlers`, vía `srv._dispatch("execute_code", ...)`) y en `tests/test_mcp_tools.py:172`
el pass-through de `max_output`.

---

### 3.2 · Topes en listados (O2) · esfuerzo S · **nuevo, alto valor**

Hallazgo del análisis externo, verificado: **`list_processing_algorithms` no tiene tope**
(`handlers/processing.py:587-608`): itera `registry.algorithms()` completo y devuelve todos cuando
no hay `search` ni `provider`. **[estimar]** cientos de algoritmos nativos+GDAL ≈ 20k chars; con
GRASS/SAGA puede pasar de 70k en **una** llamada.

Arreglo, en el estilo que ya usa `get_message_log`:

- `limit` (defecto 50, tope duro 100) + `count`/`total`/`truncated`.
- Sin `search` ni `provider`: devolver **solo el resumen por proveedor**
  (`{"providers": {"native": 280, "gdal": 60, ...}}`) y un `hint` que diga que pase `search` o
  `provider`. Un modelo que no sabe qué buscar gana más con eso que con 1.000 filas.

Mismo patrón, revisado uno a uno:

| Comando | Estado | Arreglo mínimo |
|---|---|---|
| `get_layers` (`layers.py:115`) | `limit=50` sin máximo | tope duro 100 |
| `get_message_log` (`system.py:244`) | `limit=100` sin máximo; **el texto del mensaje no se recorta** | tope duro 200 + `message` a ≤500 chars con marca |
| `list_plugins` (`system.py:267`) | devuelve `path` completo de cada plugin | quitar `path` del listado (sigue en `get_plugin_info`) |
| `get_field_statistics` (`features.py:200`) | `distinct_values` limitado a 50 valores, **no a su longitud** | truncar cada valor a ~80 chars |
| `list_connection_tables` (`connections.py:269`) | ya devuelve esquemas primero y tablas de un esquema | correcto: no tocar |

**Criterio:** ninguna respuesta de listado puede superar los ~20k chars con una llamada sin
argumentos. Test por comando con un registry/proyecto simulado grande.

---

### 3.3 · Serialización compacta de respuestas (O3) · esfuerzo S medir, M actuar · **nuevo**

**[medido] Hecho diferencial**: el SDK entrega el `dict` como JSON **indentado** (3.111 chars
frente a 1.839 compacto, **+69%**) y además adjunta `structuredContent` con el mismo dato. Y el
duplicado aparece **también** sin `structured_output=True`, así que ese flag **no** es el
interruptor del duplicado: solo elimina el `output_schema` del listado (3.962 chars en granular,
2.912 en compound).

**Qué hacer, en este orden:**

1. **Medir qué llega al modelo** en el cliente real (opencode) con una respuesta grande: si el
   cliente re-serializa el JSON al construir el contexto, el ahorro de compactar desaparece y esta
   fase se cae. Es el único dato que decide. 15 minutos con `get_layers` sobre un proyecto grande.
2. Si llega indentado: devolver `TextContent` con
   `json.dumps(..., separators=(",", ":"), ensure_ascii=False)` en las tools de respuesta grande
   (`get_layer_features`, los listados, `execute_processing`). Empezar por **una** y medir.
3. Evaluar quitar `structured_output=True` en 2-3 tools grandes para bajar el `output_schema`
   (~7,5% del bloque granular). Contrapartida: se pierde la validación de esquema de salida; no
   tocarlo en las que ya lo usan como contrato.

**Riesgo:** cambiar la forma del contenido rompe clientes que parsean el texto. Por eso va por
tools y con la medición del punto 1 delante.

---

### 3.4 · Perfiles de herramientas (O6) · esfuerzo M · **nuevo, el mayor ahorro**

**[medido]** `read-only` = 44 tools, **15.626 chars** frente a 52.842 (**−70,4%**). Mejor que
compound y sin sus tres costes (mantiene tipado, `instance=` y anotaciones por tool).

```python
_profile = os.environ.get("QGIS_MCP_TOOL_PROFILE", "all")      # all | read-only | core
```

- Aplicarlo **antes** de `_strip_schema_titles()` / `_strip_instance_param()`, con el mismo
  mecanismo que ya usa el modo compound (`server.py:3204-3230`).
- El chat embebido arranca siempre en `granular` hoy: `mcp_pack.desired_entries()` no pasa modo
  (`mcp_pack.py:43-75`) y `chat_runner.agent_env()` solo inyecta `QGIS_MCP_PORT`
  (`chat_runner.py:37-57`). `allow_modify` (Plan/Act) solo cambia flags de permiso, no las tools
  anunciadas.
- **Cuidado real:** si el `opencode serve` caliente vive entre turnos, el listado de tools queda
  fijado al arrancar; cambiar de Plan a Act exigiría reiniciar el serve o registrar dos entradas
  MCP (`qgis-ro` / `qgis-rw`). Hay que confirmarlo en `chat_runner` antes de prometer el cambio
  dinámico.
- **Calidad**: menos tools suele mejorar la selección, pero no hay forma barata de comprobarlo con
  la suite. Va con las 3 tareas guionadas de §5.

Fase en dos pasos: (a) el perfil existe y se mide por CLI; (b) el chat lo usa, con reinicio de
serve incluido.

---

### 3.5 · Proyección de atributos en `get_layer_features` (O4) · esfuerzo M

**Problema:** `handlers/features.py:150-152` serializa *todos* los campos de cada entidad. En una
capa de 40 columnas con `limit=50` es el mayor payload recurrente del proyecto.

**Diseño:**

- Plugin (`features.py:112`): `fields=None` (lista de nombres). Validar contra `layer.fields()`;
  si falta alguno → `CommandError("Unknown field(s): ...; available: ...")`. Aplicar
  `request.setSubsetOfAttributes([idx...])`; `_fid` se mantiene siempre; la respuesta devuelve en
  `fields` solo los proyectados.
- **Riesgo que el análisis externo señala y que la v2 daba por resuelto:** si `expression` filtra
  por una columna que **no** está en `fields`, el subconjunto podría alterar el resultado **en
  silencio**. Mitigación: añadir al subconjunto los campos de
  `QgsExpression(expression).referencedColumns()` sin devolverlos, y verificar en
  `tests/test_qgis_live.py` con `fields=["a"]` + `expression='"b" > 5'`.
- `server.py`, `client.py:233` y el grupo `features`/`query` de `compound_tools.py`: parámetro
  `fields: list[str] | None = None`. La descripción debe decir "columnas a **devolver**", porque
  `create_memory_layer` ya usa `fields` con otro significado (definiciones de campo).
- **Formato tabular: aplazado.** Repetir los nombres de campo 50 veces cuesta, pero el contrato
  documentado en AGENTS.md es "flat dicts con `_fid`", y `connections.py` ya devuelve
  `rows: [[...]]` como precedente. Cambiarlo es una decisión de API, no una optimización:
  primero medir cuánto pesan las claves en una capa real (si son >30% del payload, se propone
  `format="table"` en una versión menor). Igual para truncar celdas largas (`max_value_chars`):
  barato y útil, pero es un cambio de datos, no de formato — merece su propio PR.

---

### 3.6 · Política de modo, nota repetida y README (O7) · esfuerzo S

**6a. README.** Ampliar `### Compound tool mode` (línea ~343: hoy dice "cutting schema overhead
per LLM turn" sin cifras ni costes) con §1 y los tres costes medidos de compound: pierde
`instance=` y **se niega a arrancar** con multi-instancia (`server.py:3214`), pierde parámetros
tipados (`params: dict[str, Any]`, validados solo en runtime), y `layer`/`features`/`project`
marcan `destructiveHint=True` para *todas* sus acciones. Recomendación por defecto: **granular**;
compound para clientes sin prompt caching o con ventana pequeña; perfil `read-only` cuando el
agente solo consulta. **Arreglar además la lista de grupos: lista 25 y el modo registra 27 —
faltan `connection` y `editing`.**

**6b. `_PARAMS_NOTE` duplicada.** 154 chars × 27 = **4.158 chars** que dicen lo mismo en cada
tool de compound. Moverla una vez a `FastMCP(instructions=...)` (o reducirla a ~35 chars) ahorra
~3.000 chars sin tocar ninguna acción. Solo afecta a compound; el presupuesto de Fase 0 lo vigila.

---

## 4. Orden, dependencias y esfuerzo

| # | Fase | Depende de | Esfuerzo | Por qué aquí |
|---|---|---|---|---|
| 1 | 3.0 · presupuesto | — | S | Fija la vara, protege todo lo demás, detecta la trampa de los 1.835 chars |
| 2 | 3.1 · truncado `execute_code` | — | M | Único bug de contexto real; defensivo |
| 3 | 3.2 · topes de listados | — | S | Evita 20k+ tokens en una sola llamada |
| 4 | 3.3 · serialización (medir) | — | S | 15 min de medición que decide una fase entera |
| 5 | 3.4 · perfiles | 3.0 | M | Mayor ahorro medido (−70,4%); el paso del chat exige antes mirar `chat_runner` |
| 6 | 3.5 · proyección | — | M | Cambia API pública: tests + live test |
| 7 | 3.6 · README y nota | 3.0 | S | Cierra el círculo documental |

Total: dos sesiones de trabajo. PRs sugeridos: **A** = 3.0, **B** = 3.1 + 3.2 (topes de salida),
**C** = 3.3, **D** = 3.4, **E** = 3.5, **F** = 3.6.

---

## 5. Verificación

```bash
uv run --no-sync pytest tests/ --ignore=tests/test_qgis_live.py \
  --ignore=tests/test_stress.py --ignore=tests/test_tdd.py -q
uv run --no-sync ruff check src qgis_mcp_plugin tests
uv run --no-sync pytest tests/test_qgis_live.py -v      # con QGIS y el plugin cargado
```

**Medición por cambio** (antes/después, contra la instancia de desarrollo del puerto 9877):
`get_layer_features` 50×40 con y sin `fields`; `list_processing_algorithms()` sin argumentos;
`get_layers` con 50 capas; `get_message_log(limit=100)`; `execute_code("print('x'*10**6)")`;
`execute_code` con traceback profundo (que conserve la última línea); `list_plugins()`.

**Sesión completa**: 3 tareas guionadas ("filtrar + estadísticas", "buffer + render", "crear capa
y estilizar") en el mismo modelo, comparando tokens de entrada totales y número de llamadas con
`granular` / `read-only`. Es el único dato que decide si los perfiles valen la pena y si reducir
tools empeora la selección.

Bucle de desarrollo (AGENTS.md): `qgis_mcp_plugin/` → Plugin Reloader sin reiniciar QGIS;
`src/qgis_mcp/` → reiniciar el cliente MCP. Nada de `grab()`/`processEvents()` desde `execute_code`.

Restricciones: plugin stdlib-only en `wire/errors/registry/constants`, **Python 3.9** en el plugin
(`tests/test_py39_compat.py`), sin `qgis` fuera de `handlers/`.

---

## 6. Lo ya hecho — y una corrección

Confirmado en código: `include_geometry=False` por defecto y `_geometry` bajo petición
(`features.py:113,154`); `expression` server-side (`:122`); `limit > 50` **rechazado**, no recortado
(`helpers.feature_limit_error`); advertencias de processing topadas (10 × 500 chars,
`processing.py:54-56`); mutaciones devuelven contadores (`features.py:363,522`); `get_message_log`
con `limit` y flag `truncated` (`system.py:263`); recursos de proyecto/capas/esquema
(`server.py:3335-3400`); `_strip_schema_titles`/`_strip_instance_param` ya recortan ~2k chars y
17% respectivamente (`server.py:3233-3286`).

**Corrección a la v2:** "resultados grandes → recurso" **no ahorra tokens**. `server.py:1004-1008`
añade `features_resource` y un `_hint` **sin quitar `features`**; el JSON se guarda en memoria del
servidor (`_CACHE_MAX_ENTRIES = 32`) y el propio comentario admite que "no se leen en la mayoría de
sesiones" (`server.py:570-571`). El modelo solo recibe una copia extra si decide leer el recurso.
No es urgente y **no** duplica contexto por sí solo; queda como limpieza opcional (quitar el hint,
o borrar el mecanismo) y no como parte de este plan.

---

## 7. Fuera de alcance (no es contexto, pero consta)

| Tema | Estado | Prioridad |
|---|---|---|
| `get_layer_info` devuelve `layer.source()` sin enmascarar (`layers.py:238`); en PostGIS puede incluir usuario y, según cómo se guardó, contraseña. `connections.py` ya tiene `_redact_uri` para esto | verificado en código; falta confirmar con una conexión real | **seguridad**, PR aparte |
| `NoGeometry` nunca se activa con `include_geometry=False` (`QgsFeatureRequest` sin flag; el enum solo aparece en `compat.py`) | verificado; el proveedor carga geometrías que se descartan | rendimiento, P3 |
| Paginación por `offset` recorre y descarta en Python (`features.py:142-147`) → O(offset) | verificado | P3 |
| `error` y `traceback` repiten la misma línea final en `code_failure_message` | verificado | se arregla dentro de 3.1 |

---

## 8. Del análisis externo: qué entra, qué se corrige y qué no

| Punto del análisis | Veredicto | Dónde queda |
|---|---|---|
| `execute_code` sin tope (§4.2) | Correcto; su matiz de conservar la **cola** del traceback es una mejora real | 3.1 |
| `list_processing_algorithms` y listados sin tope (§3.2) | **Correcto y nuevo** | 3.2 |
| Perfiles de herramientas (§3.3) | **Correcto**; su estimación de −65% se queda corta: medido −70,4% | 3.4 |
| `_strip_*` solo en el `mcp` global → el test mediría otro número (§4.1) | **Correcto**, cuantificado: 30.050 vs 28.215 (+1.835 de títulos) | 3.0 |
| Dependencia de `QGIS_MCP_INSTANCES` en el test (§4.1) | Correcto (+17%) | 3.0 |
| `format` tabular y truncado de celdas (§3.4) | Idea válida, pero choca con el contrato "flat dicts" documentado | 3.5, aplazado con medición previa |
| Serialización con indentación y `structuredContent` (§3.5) | **Correcto y medido**: +69% en el texto. Pero `structured_output=False` **no** elimina `structuredContent`: solo el `output_schema` | 3.3 |
| `_PARAMS_NOTE` repetida (§3.6) | Correcto: 4.158 chars | 3.6b |
| "La caché a recurso duplica tokens" (§3.1) | **Matiz**: duplica memoria del servidor y añade un hint; al modelo solo le llega una copia extra si lee el recurso | §6, limpieza opcional |
| Fase 3b (recortar descripciones) | De acuerdo: bajo retorno, sin forma de medir calidad | No-objetivos |
| Enmascarar credenciales, `NoGeometry`, O(offset) | Correctos, pero son otro eje | §7 |
