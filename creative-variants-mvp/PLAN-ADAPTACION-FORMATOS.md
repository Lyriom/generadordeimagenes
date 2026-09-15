# Plan · Que una pieza se adapte de verdad al formato

**Estado**: diagnosticado y reproducido con un KV real. Sin codificar.
**Para**: quien vaya a implementarlo (agente o persona) trabajando en este repo.
**Origen**: el usuario eligió varios formatos con «conservar el diseño del KV» y
recibió piezas rotas: el arte encogido en una franja con bandas muertas arriba y
abajo, o recompuesto en un desorden. Puntuadas 100/100 y 78/100.

---

## 0 · Cómo reproducirlo (hazlo antes de tocar nada)

Guarda esto como `backend/tools/repro_formatos.py` y córrelo con
`PYTHONPATH=$PWD .venv/bin/python tools/repro_formatos.py` desde `backend/`.
Usa un KV real del repo, no una imagen sintética: el defecto vive en cómo se
reparte un arte de agencia, y un rectángulo de prueba no lo enseña.

```python
"""Reproduce la queja: un KV llevado a otra proporción sale con bandas."""
import os, pathlib, shutil

SALIDA = pathlib.Path("/tmp/repro-formatos")
os.environ["DATA_DIR"] = str(SALIDA / "data")
os.environ["INGEST_DIR"] = str(SALIDA / "data/ingest")
os.environ["ENABLE_OCR"] = "false"
os.environ["SEGMENTATION_PROVIDER"] = "local"
os.environ["INPAINTING_PROVIDER"] = "opencv"
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "true"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"

from fastapi.testclient import TestClient
from app.main import app

PSD = pathlib.Path("../data/ingest/CYBER-AGOSTO-2026CYBER-MS-900X660-ACCESORIOS.psd")

with TestClient(app) as client:
    cabecera = {"X-Session-Id": "repro"}
    creado = client.post(
        "/projects",
        data={"name": "repro"},
        files={"artwork": (PSD.name, PSD.read_bytes(), "image/vnd.adobe.photoshop")},
        headers=cabecera,
    ).json()
    pid = creado["project_id"]
    formatos = ["900x660", "meta_feed_square", "meta_feed_4_5", "meta_stories"]
    tarea = client.post(
        f"/projects/{pid}/auto",
        json={"count": 1, "formats": formatos, "template_mode": True},
        headers=cabecera,
    ).json()
    resultado = client.get(
        f"/projects/{pid}/tasks/{tarea['task_id']}", headers=cabecera
    ).json()["result"]
    piezas = SALIDA / "piezas"
    piezas.mkdir(parents=True, exist_ok=True)
    for v in resultado["variants"]:
        origen = SALIDA / "data" / "projects" / pid / v["image"]
        shutil.copy(origen, piezas / f"{v['format']}_{v['layout']}_{v['quality']['score']}.png")
        print(f"{v['format']:>18} {v['width']}x{v['height']} {v['layout']:<12} {v['quality']['score']}")
```

**Y ábrelas.** El defecto es visual; los números dicen que todo está bien.

### Lo que sale hoy (KV de 900×660, cuatro formatos, diseño conservado)

| formato pedido | lienzo | layout | puntaje | qué sale de verdad |
|---|---|---|---|---|
| `900x660` (nativo) | 900×660 | `faithful` | **100** | correcto |
| `meta_feed_square` | 1080×1080 | `faithful` | **100** | arte centrado, 13 % de banda arriba y 13 % abajo |
| `meta_feed_4_5` | 1080×1350 | `faithful` | **100** | **el arte ocupa una franja central; el 41 % del lienzo son bandas muertas** |
| `meta_stories` | 1080×1920 | `source_flow` | 78 | reflujo roto: el producto partido en dos, «cyber day.ec» tapado por la bolsa, un vacío enorme en el centro, el legal aplastado |

Dos piezas de cuatro son impublicables y ninguna de las dos está marcada como
tal. Eso es lo que hay que arreglar.

---

## 1 · Causa raíz (tres, encadenadas)

### 1.1 El modo anclado se refugia en «fiel» aunque la proporción no dé

`layout_engine.plan_variants()` (≈ línea 1857-1900) solo tiene dos composiciones
que conserven el diseño, y las ofrece con criterios que dejan un agujero enorme
en medio:

- `FAITHFUL_LAYOUT` — solo si `|aspecto_salida / aspecto_origen − 1| ≤ 0.18`
  (`FAITHFUL_ASPECT_TOLERANCE`).
- `SOURCE_FLOW_LAYOUT` — solo si el formato es «apretado»: una tira
  (`ancho/alto ≥ BANNER_ASPECT = 2.4`) o uno que obligue a recomponer
  (`_coverage < MIN_SOURCE_COVERAGE = 0.45`).

Un 1080×1350 salido de un 900×660 no cumple ninguna: el delta de proporción es
0.41 (pasa de 0.18) pero la cobertura es 0.59 (no baja de 0.45). El `elif
anchored: layout_key = FAITHFUL_LAYOUT` que cierra esa cadena lo manda a fiel, y
fiel con otra proporción es `_pinned_box()`: **una única escala uniforme
(`min(W/w, H/h)`) y centrado**. Eso es, por definición, un letterbox.

> No es un fallo de implementación de `faithful`: `faithful` hace exactamente lo
> que promete. Falta la composición intermedia.

### 1.2 El control de calidad no ve el letterbox

`quality.evaluate_variant()` sección 10 mide el vacío con `_union_coverage()`,
que es la fracción del lienzo cubierta por la **unión de las cajas**. En el 4:5
roto eso da 0.59 — muy por encima de `AIRY_FILL = 0.34` — porque el arte es
ancho: ocupa el 100 % del ancho y el 59 % del alto. La unión no distingue «59 %
bien repartido» de «59 % en una franja con dos bandas muertas».

Y aunque bajara, hay una segunda puerta que lo deja pasar:

```python
elif filled < AIRY_FILL and not all(p.pinned for p in content):
    # Una reproducción fiel va donde el diseñador lo puso: si el KV aprobado
    # es aireado, eso es su diseño y no un defecto de la pieza.
```

En modo fiel **todas** las capas van `pinned`, así que la regla no se aplica
nunca. El razonamiento es correcto en el formato nativo y falso en cuanto la
pieza cambia de proporción: ahí el vacío no lo puso el diseñador, lo puso el
motor. Resultado: 100/100 a una pieza con el 41 % de lienzo muerto, y por tanto
`variants._compose_until_clean()` no replantea nada.

### 1.3 El reflujo existe pero no está a la altura

La pieza de 1080×1920 (`source_flow`, 78/100) sale con el producto partido en
dos bandas, solapes sobre el logo, un vacío central y el legal aplastado contra
el CTA. Sus avisos lo dicen («El producto invade…», «El producto se ve demasiado
pequeño») y aun así se entrega. Es la única salida para proporciones muy
lejanas, así que tiene que ser buena.

---

## 2 · Los cambios, en orden de ejecución

Cada bloque es independiente y deja el repo en verde. Haz uno, corre las
pruebas, mira las imágenes del repro, y sigue.

### Bloque A · Que la calidad vea las bandas muertas *(primero: sin esto, nada de lo demás se puede medir)*

**Archivo**: `backend/app/services/quality.py`

1. Métrica nueva, junto a `_union_coverage`:

```python
def _dead_bands(placements, canvas_w: int, canvas_h: int) -> tuple[float, float]:
    """Franjas del lienzo, arriba+abajo e izquierda+derecha, donde no cae NADA.

    La unión de cajas no distingue «el 59 % bien repartido» de «el 59 % en una
    franja con dos bandas muertas», y esa diferencia es justo la que separa una
    pieza adaptada de un arte encogido en medio de un relleno.
    """
```

   Cómo: caja envolvente (bbox) de `content` (los que no son a sangre, el mismo
   filtro que ya usa la sección 10). Devuelve
   `((bbox.y0 + (H − bbox.y1)) / H, (bbox.x0 + (W − bbox.x1)) / W)`.

2. En `evaluate_variant`, sección 10, añade:
   - `metrics["dead_bands_v"]`, `metrics["dead_bands_h"]`.
   - Umbrales nuevos arriba del módulo, con su porqué:
     ```python
     #: Franja muerta a cada lado a partir de la cual la pieza está en un
     #: buzón: el arte flota en medio y el resto es relleno. 0.08 es el margen
     #: de seguridad típico; el doble de eso ya es una banda que se ve.
     DEAD_BAND_EDGE = 0.08
     #: …y cuánto suman las dos para que deje de ser aire y sea un defecto.
     DEAD_BAND_TOTAL = 0.18
     ```
   - Si `min(banda_arriba, banda_abajo) ≥ DEAD_BAND_EDGE` **y** la suma
     `≥ DEAD_BAND_TOTAL` (ídem en horizontal): penaliza 20, avisa con el
     porcentaje real, y **marca defecto que invalida**.

3. Añade la métrica a `BLOCKING_METRICS` (el dict que lee `blocking_defects()`,
   clave = nombre de la métrica, valor = cómo se dice en castellano). Por
   ejemplo `"letterbox": "el arte flota en una franja del lienzo"`. La métrica
   tiene que valer 0 o 1, como `empty_composition`, porque `blocking_defects()`
   evalúa la verdad del valor.

4. Quita la exención de `all(p.pinned)` **cuando la pieza no está en su
   proporción nativa**. `evaluate_variant` ya recibe `project` y `plan`: compara
   `plan.width/plan.height` con `project.canvas.width/height` con la misma
   tolerancia que usa el motor (`FAITHFUL_ASPECT_TOLERANCE`). Dentro de la
   tolerancia, la exención se queda como está (el KV aireado es del diseñador);
   fuera, se aplica la regla.

**Prueba** (`backend/tests/test_quality.py`): un plan con todas las capas en la
franja central de un lienzo alto tiene que dar `dead_bands_v > 0` y aparecer en
`blocking_defects()`. Y el mismo arte en su formato nativo, no.

**Ojo**: al terminar este bloque, el repro dará puntajes bajos en el 4:5 y el
motor intentará replantear tres veces sin arreglarlo, porque `replan()` solo
cambia la semilla. Eso lo resuelve el bloque siguiente. Es el orden correcto:
primero que el problema se vea, después que se pueda arreglar.

---

### Bloque B · La composición que falta: «adaptado al formato» *(el grueso)*

**Archivo**: `backend/app/services/layout_engine.py`

La idea, en una frase: **conservar el tamaño y la posición horizontal de cada
elemento, y repartir la holgura del eje que sobra entre las bandas del arte, en
vez de dejarla toda en los bordes.** Es lo que hace un diseñador cuando le piden
el mismo KV en 4:5: no encoge la pieza, separa los bloques.

1. **Constante y registro**:
   ```python
   ADAPTIVE_LAYOUT = "adaptive"
   LAYOUTS[ADAPTIVE_LAYOUT] = {
       "label": "Adaptado al formato (mismo diseño)",
       "adapt_from_source": True,
       "zones": dict(LAYOUTS[FAITHFUL_LAYOUT]["zones"]),
       "align": "center",
   }
   SYNTHETIC_LAYOUTS = {FAITHFUL_LAYOUT, SOURCE_FLOW_LAYOUT, ADAPTIVE_LAYOUT}
   ```

2. **Cálculo** (función nueva al lado de `_pinned_box`, que se queda intacta):

   ```python
   def _adaptive_boxes(layers, source_canvas, canvas_w, canvas_h) -> dict[str, tuple[int,int,int,int]]:
   ```

   - **Escala**: la del eje que se llena.
     `scale = canvas_w / source_w` si el destino es más alto de proporción que el
     origen; `canvas_h / source_h` si es más ancho. Una sola escala para todo:
     nada se deforma, igual que hoy.
   - **Holgura**: `slack = canvas_h − source_h * scale` (o la horizontal).
     Si `slack / canvas_h < 0.02` → devuelve `None` y que se use `faithful`: es
     la misma proporción y no hay nada que repartir.
   - **Bandas**: ya están implementadas y probadas en
     `services/source_layout.py`. Úsalas, no escribas otras:
     `reading_axis(items, source_canvas)` dice por qué eje fluye el arte y
     `clusters(items, axis)` devuelve los tramos (las capas agrupadas por su
     intervalo, con el solape mínimo ya calibrado en `GROUP_OVERLAP`). Los
     `Item` los construye `layout_engine._source_items()`, que también existe.
   - **Reparto**: los huecos son el de antes de la primera banda, los de entre
     bandas, y el de después de la última. Reparte `slack` proporcionalmente al
     hueco original que ya tenía cada uno, con dos topes:
     - los dos huecos exteriores no se llevan más de `EDGE_SLACK_SHARE = 0.35`
       del total entre los dos (si no, vuelve el letterbox por la puerta de
       atrás);
     - si el arte no tiene huecos interiores medibles, reparte a partes iguales
       entre las bandas; y solo si hay una sola banda, a los exteriores.
   - **El producto absorbe primero**: si hay banda de producto, deja que crezca
     hasta consumir `PRODUCT_SLACK_SHARE = 0.45` de la holgura, con el tope de
     `MAX_UPSCALE` que ya existe. En vertical el producto manda; es lo que evita
     el «producto demasiado pequeño» del reflujo.
   - **Anclas duras**: la banda con el `legal` se pega al pie (usa `LEGAL_FOOT`,
     ya existe); la del `logo`, arriba. Son los dos que nunca flotan.
   - **Zona manual del producto**: si `project.product_zone` existe, el producto
     NO entra en el reparto de bandas — va a su recuadro, como ya hace el resto
     del motor (`manual_zone` en `build_placements`). No lo rompas.

3. **Enganche en `build_placements`**: donde hoy decide `relative` (el bloque
   `learned_zone` / `relative` / `manual_zone`), añade la rama del layout
   adaptado: si `layout.get("adapt_from_source")` y hay `source_canvas`, las
   cajas salen de `_adaptive_boxes` y se colocan con `pinned=True`,
   `stretch` solo para lo que hoy ya lo lleva (escenografía a sangre).

4. **Elección del layout** en `plan_variants`, sustituye la cadena actual por:

   ```
   delta = abs((ancho/alto) / source_aspect - 1.0)
   si delta <= FAITHFUL_ASPECT_TOLERANCE            -> faithful
   si el formato es tira o necesita recomponer      -> source_flow (si reflow_viable)
   si delta <= ADAPTIVE_ASPECT_TOLERANCE (0.60)     -> adaptive
   si reflow_viable                                  -> source_flow
   si no                                             -> faithful + aviso de que no da
   ```
   Los deltas reales, para que no se calibre a ojo:

   | cambio de proporción | delta |
   |---|---|
   | 1:1 → 4:5 | 0.20 |
   | 900×660 → 1080×1080 | 0.27 |
   | 900×660 → 1080×1350 | 0.41 |
   | 1:1 → 9:16 | 0.44 |
   | 900×660 → 1080×1920 | 0.59 |
   | 1:1 → 1.91:1 | 0.91 |

   Un número fijo ahí es una apuesta. **Mejor una condición que se calibra
   sola**, `adaptive_viable()`: reparte la holgura y comprueba que ningún hueco
   resultante supere el alto de la banda más alta del arte. Si un hueco es
   mayor que el bloque más grande, la pieza deja de leerse como una composición
   y pasa a ser islas separadas: ahí manda el reflujo. Con eso
   `ADAPTIVE_ASPECT_TOLERANCE = 0.45` es solo un corte de seguridad barato que
   evita calcular en los casos absurdos, no la decisión.

5. **`replan()` tiene que poder cambiar de composición.** Hoy solo cambia la
   semilla, así que un defecto de layout se repite tres veces. Añade un
   parámetro (`fallback_layout: bool`) y, cuando el defecto que invalida sea el
   de bandas muertas, replantea con la siguiente composición de la cadena en vez
   de con otra semilla. El que decide es `variants._compose_until_clean()`, que
   ya conoce los defectos.

**Pruebas nuevas** (`backend/tests/test_layout_engine.py` o uno propio
`test_adaptive_layout.py`):
- Un arte 900×660 en 1080×1350 con `adaptive`: la caja envolvente del contenido
  cubre ≥ 85 % del alto del lienzo (hoy cubre el 59 %).
- Ninguna capa cambia de proporción (el mismo control que ya usa `test_rotation`).
- El orden vertical de las capas del origen se conserva en el destino.
- Con `product_zone` puesta, el producto sigue dentro del recuadro.
- El formato nativo sigue saliendo `faithful` y clavado (regresión).

---

### Bloque C · La tarjeta de resultados se desborda

**Archivo**: `frontend/src/styles/global.css`

`.result-open` **no tiene ninguna regla** y es el `<button>` que envuelve la
imagen; `.result-image img { width:100%; height:100% }` se mide contra ese botón
sin tamaño y la imagen se sale de la tarjeta, tapando el nombre del formato (se
ve en la captura de Resultados). Añade:

```css
.result-open {
  display: block; width: 100%; height: 100%;
  padding: 0; border: 0; background: none; cursor: zoom-in;
}
```

Y ya que se toca: la tarjeta enseña todas las piezas en una caja de 280 px de
alto, así que un 9:16 y un 1.91:1 se ven igual de altos. Usar
`aspect-ratio: var(--pieza)` con la proporción real de la variante hace que el
formato se reconozca de un vistazo, que es justo lo que el usuario está
comprobando en esa pantalla.

---

### Bloque D · El reflujo, a la altura *(el más abierto: instrumenta antes de tocar)*

**Archivos**: `backend/app/services/source_layout.py`, `layout_engine.py`
(`_source_items`, `FLOW_EXCLUDED`).

Defectos concretos vistos en la pieza 1080×1920 del repro, en orden de gravedad:

1. **El producto sale partido en dos sitios** (la bolsa arriba, el balón abajo).
   `_source_items` hace viajar los productos como **un** bloque
   (`bloques = [group]`), así que o esas capas no están clasificadas como
   `product`, o `derive_zones` parte el bloque. Empieza imprimiendo los `Item`
   que entran al reflujo: el diagnóstico está ahí.
2. **Solapes sin resolver**: «El producto invade 'logo copy copia'». En el
   reflujo sí corre `_resolve_overlaps`; comprobar por qué no separa ese par
   (probable: el logo es `decoration` y la decoración está exenta).
3. **Vacío central**: las bandas se reparten con los `ref_y` de
   `vertical_stack`, que están pensados para formatos de una pieza. Con mucha
   holgura hay que estirar los bloques, no el aire.

**Criterio de aceptación**, no lista de tareas: los cuatro PSD de `data/ingest`
llevados a `meta_stories` y `google_display_300x600` dan ≥ 85/100 y ninguno con
defecto que invalide. Mide antes y después con el repro.

---

### Bloque E · La IA que ya está instalada y no se enciende aquí

`services/background_expand.py` extiende el fondo con máscara (Ideogram vía
Magnific): los píxeles del arte no se tocan, el modelo solo pinta lo que quedaba
fuera. Está enganchado en `worker.py`, pero **solo se dispara si
`cover_upscale > SHARP_UPSCALE (1.6)`**. En un 1080×1080 → 1080×1350 el cover es
1.25, así que no se llama nunca y el fondo se resuelve recortando la plancha
(`resize_cover`), que se come los laterales del diseño.

Cuando exista el layout adaptado esto importa más, no menos: al repartir la
holgura queda más fondo a la vista. Propuesta: disparar también cuando
`|aspecto_salida/aspecto_origen − 1| > 0.25` aunque el cover sea bajo,
conservando el tope `MAX_INVENTED = 0.58` que ya evita que el modelo se invente
media pieza. Es una llamada por lienzo y se cachea en disco, así que una tanda
de veinte productos la paga una vez.

Cuidado con la memoria del proyecto: **esto no puede quedar detrás de un
interruptor apagado en producción**. Si se añade una condición, que venga
encendida.

---

## 3 · Lo que NO hay que hacer

- **No toques `faithful`.** Es correcto y es lo que garantiza que el formato
  nativo salga clavado. El trabajo es no llamarlo donde no toca.
- **No estires capas para llenar el lienzo.** Una sola escala uniforme, siempre.
  Deformar el KV aprobado de una marca es peor que cualquier banda.
- **No conviertas el letterbox en un recorte.** Llenar con `cover` recortaría el
  arte y se comería el legal o el logo.
- **No inventes con IA encima del arte.** La extensión de fondo va con máscara
  por una razón: lo aprobado no se regenera.

## 4 · Cómo saber que está hecho

Corre el repro y abre las cuatro imágenes. Hoy: dos impublicables con 100 y 78.
Objetivo:

| formato | qué se espera |
|---|---|
| nativo | idéntico a hoy, `faithful`, 100 |
| 1080×1080 | `adaptive`, bandas muertas < 8 %, ≥ 90 |
| 1080×1350 | `adaptive`, bandas muertas < 8 %, ≥ 90 |
| 1080×1920 | `source_flow` sin solapes ni producto diminuto, ≥ 85 |

Y la suite completa dentro del contenedor, que es donde la tipografía es DejaVu:

```bash
docker build -t cv-backend-ci ./backend
docker run --rm -e CELERY_TASK_ALWAYS_EAGER=1 -e MAGNIFIC_API_KEY= -e OPENAI_API_KEY= \
  cv-backend-ci python -m pytest tests/ -p no:cacheprovider --tb=short
```
