# Plan · De KV suelto a fábrica de artes

**Estado**: diseñado y decidido. Etapa 1 codificada (ver §6).
**Para**: quien vaya a implementarlo (agente o persona) trabajando en este repo.
**Origen**: el usuario pidió cambiar el enfoque. Hoy cada KV es un trabajo de una
tarde que se borra a las ocho horas. Lo que hace falta es que un KV de cliente se
convierta **una vez** en una plantilla de marca con campos, que esa plantilla
tenga una adaptación aprobada por formato, y que producir doscientos artes sea
rellenar una matriz.

**Decisiones tomadas el 2026-09-16** (no volver a abrirlas sin motivo):

| Decisión | Elegido |
|---|---|
| De dónde salen los artes de la marca para aprender su estilo | Las tres: carpeta de referencias, API de Windsor y scraper. Un proveedor con tres implementaciones |
| Cómo se llena la matriz | Rejilla en la app **más** importar/exportar CSV-Excel |
| Adaptación a cada formato | **Máster aprobado por formato**, no recálculo en cada tanda |
| Orden de construcción | Plantillas + formatos → matriz → redes |

---

## 0 · Qué cambia, en una frase

**Hoy**: un KV es un proyecto que se borra en ocho horas, y cada tanda vuelve a
componer desde cero. Dos tandas del mismo KV no dan lo mismo.

**Con esto**: un KV se convierte una vez en una **plantilla de marca** —campos
nombrados, plancha limpia, un máster aprobado por formato— y producir es
rellenar celdas. La composición se revisa una vez y se reutiliza mil.

La diferencia no es de interfaz. Es que **la decisión de diseño se guarda**, en
lugar de recalcularse. Eso es lo que separa un generador de variantes de una
plantilla.

---

## 1 · Lo que ya está hecho y no hay que reescribir

Más de la mitad del trabajo existe. Conviene tenerlo presente para no duplicarlo:

| Pieza | Dónde | Qué aporta al plan |
|---|---|---|
| Importación de capas reales del PSD | `services/psd_import.py` | El KV llega con recortes exactos, alfa, orden y posición. Es la materia prima de la plantilla |
| Corte de pliegos en piezas | `psd_import.pieces` + `/projects/split` | Un PSD de agencia con cinco avisos da cinco plantillas |
| Categorías de capa | `models/project.py` (`LayerCategory`) | `product`, `price`, `headline`, `cta`, `legal`, `logo`… es el vocabulario con el que se deciden los campos |
| Reescritura de copy y borrado de la plancha | `services/art_text.py` | Vaciar un campo del fondo. **Sin esto una plantilla arrastra el precio viejo como fantasma** |
| Separación de una capa en partes | `art_text.split` (comprobada, 2 % de tolerancia) | El PSD trae precio + rótulo + sello en una capa; cada parte es un campo distinto |
| Reemplazo de producto sin deformar | `services/replacement.py` | Llenar un campo imagen |
| Zona del producto a mano | `ProductZone` (fracciones 0..1) | El precedente exacto de lo que es un campo: una caja en fracciones que vale para todos los formatos |
| Catálogo de formatos con áreas seguras | `models/formats.py` | Los formatos de los másters salen de aquí, no de una lista nueva |
| Motor `faithful` / `adaptive` / `source_flow` | `services/layout_engine.py`, `services/source_layout.py` | **Propone** el máster de cada formato. Ya sabe repartir la holgura y conservar el orden de lectura |
| Puntaje con defectos que invalidan | `services/quality.py` | Un máster no se puede aprobar si la pieza de prueba tiene un defecto que invalida |
| Render, PSD, SVG y ZIP | `services/renderer.py`, `services/export.py` | La producción masiva no dibuja nada nuevo: usa esto |
| Tipografías permanentes por cliente | `app/assets/client_fonts/<cliente>/` | El precedente de almacén que sobrevive al despliegue. La marca hereda de aquí |

---

## 2 · Los cinco huecos

### 2.1 La plantilla no existe como objeto

Lo más parecido que hay es `template_mode: true` en `AutoRequest`, que es un
**modo de una tanda** («no recompongas, sustituye»), no una cosa guardada. No hay
campos con nombre, no hay biblioteca, no se puede volver a abrir mañana.

Consecuencia práctica: el conocimiento de qué cambia en cada arte —el precio sí,
el logo no— vive en la cabeza de quien hizo la tanda y se pierde al cerrar.

### 2.2 Los proyectos se borran a las ocho horas

`PROJECT_RETENTION_HOURS=8`, `MAX_PROJECTS_KEPT=60`, y `storage.purge_expired_projects()`
corriendo. Es correcto para lo que hay hoy (un PSD de 100 MB se convierte en
cientos de MB de capas y variantes) y **es incompatible con una biblioteca**.

Una plantilla no puede vivir en `data/projects/`. Necesita almacén propio, como
ya se hizo con las tipografías de Marcimex.

### 2.3 La adaptación no se aprueba: se rehace

`plan_variants()` elige la composición en cada llamada, con semilla y sorteo.
Para nueve variantes creativas está bien. Para producir el catálogo de octubre en
cuatro formatos es lo contrario de lo que hace falta: nadie revisó esas 200
composiciones, y la próxima tanda dará otras.

### 2.4 No hay matriz

`AutoRequest.text_overrides` es el 10 % de una matriz: una lista de
`{layer_id, content}` para **una** tanda. Falta la otra dimensión (una fila por
arte), las imágenes por fila, la validación por celda y el CSV.

### 2.5 No hay perfil de marca

Nada mira cómo son los artes que la marca publica de verdad. La paleta se saca
del propio KV y los formatos los elige la persona.

---

## 3 · El modelo nuevo

Cinco objetos. Los tres primeros son la etapa 1 y 2; los dos últimos, la 3 y la 4.

```
Marca (Brand)
 └── Plantilla (Template)        ← nace de un KV, una vez
      ├── Campo (Slot) × n       ← lo que cambia en cada arte
      ├── Capa fija × n          ← lo que nunca cambia (logo, decoración, plancha)
      └── Máster (FormatMaster) × formato   ← la adaptación aprobada
 └── Tanda (Batch)               ← matriz de filas × campos → piezas
 └── Perfil de estilo (StyleProfile)   ← lo aprendido de redes
```

### 3.1 Dónde vive cada cosa

```
data/brands/<brand_id>/
├── brand.json                     nombre, paleta, tipografías, perfil de estilo
├── assets/                        logos y recursos de marca
├── references/                    artes descargados de redes (etapa 4)
└── templates/<template_id>/
    ├── template.json              campos, capas fijas, másters
    ├── plate.png                  la plancha ya vaciada de los campos
    ├── assets/                    PNG congelados de las capas fijas
    └── masters/<formato>.png      vista previa aprobada de cada formato

data/batches/<batch_id>/           tandas de producción (sí caducan: son salida)
```

**`data/brands/` no entra en el barrido de retención.** Es el punto entero.

### 3.2 La plantilla, en JSON

```json
{
  "template_id": "uuid",
  "brand_id": "uuid",
  "name": "CREDIFEST · KV Hero motos",
  "source_canvas": {"width": 1200, "height": 675},
  "plate": "plate.png",
  "fixed_layers": [
    {"layer_id": "…", "name": "Logo Marcimex", "category": "logo",
     "src": "assets/logo.png", "box": [0.78, 0.03, 0.19, 0.07], "z_index": 9}
  ],
  "slots": [
    {"id": "producto", "label": "Producto", "kind": "image",
     "category": "product", "required": true,
     "box": [0.42, 0.28, 0.44, 0.58], "fit": "contain", "min_px": 700},
    {"id": "precio", "label": "Precio", "kind": "text",
     "category": "price", "required": true, "box": [0.06, 0.74, 0.30, 0.10],
     "font_family": "Coco Sharp", "font_weight": "bold", "font_size": 96,
     "color": "#FFFFFF", "align": "left", "max_lines": 1, "min_font_size": 42}
  ],
  "masters": {
    "meta_feed_4_5": {
      "width": 1080, "height": 1350, "approved": true,
      "layout": "adaptive", "background_style": "plate",
      "placements": [{"ref": "producto", "box": [0.36, 0.31, 0.52, 0.42]}],
      "preview": "masters/meta_feed_4_5.png",
      "quality": {"score": 91, "warnings": []}
    }
  }
}
```

Tres decisiones dentro de ese JSON que no son de estilo:

1. **Todas las cajas en fracciones 0..1**, nunca en píxeles. Es lo que ya hace
   `ProductZone` y por el mismo motivo: la misma decisión tiene que valer para
   las cinco medidas de la tanda.
2. **El máster referencia el campo por `id`, no por `layer_id`.** El `id` es un
   slug estable (`precio`, `producto_2`) y es además **el encabezado de la
   columna en la matriz y en el CSV**. Un uuid en una cabecera de Excel no lo
   llena nadie.
3. **La plancha va congelada y ya vaciada.** Si el precio del KV original sigue
   pintado en el fondo, los doscientos artes llevan un precio fantasma debajo del
   suyo. Esto ya se resuelve con `art_text` + `rebuild_plate`; lo nuevo es
   hacerlo **al crear la plantilla** y guardar el resultado.

### 3.3 Qué es un campo y qué no

La regla, corta: **es campo lo que cambia de un arte al siguiente. Todo lo demás
es capa fija.** La propuesta automática sale de la categoría de la capa:

| Categoría de la capa | Propuesta | Por qué |
|---|---|---|
| `product` | Campo imagen, requerido | Es el caso de uso entero |
| `price` | Campo texto, requerido | Cambia siempre |
| `headline`, `subheadline` | Campo texto, opcional | A veces es de campaña y no cambia |
| `cta` | Campo texto, opcional, con valor por defecto | Suele repetirse |
| `legal` | Campo texto, opcional, valor por defecto = el original | Cambia por promoción, no por producto |
| `logo`, `decoration`, `background`, `person` | Capa fija | Es la identidad del KV |

Es una **propuesta**, no un veredicto: la pantalla deja convertir cualquier capa
fija en campo y al revés. Igual que «Revisar lo detectado», por el mismo motivo
—las categorías del PSD vienen de nombres como «Capa 15»—.

---

## 4 · Las cuatro etapas

Cada una deja el repo en verde y algo usable. No empezar la siguiente sin correr
las pruebas de la anterior.

### Etapa 1 · La plantilla existe y sobrevive

**Archivos nuevos**: `app/models/template.py`, `app/models/template_schemas.py`,
`app/services/template_store.py`, `app/services/templating.py`,
`app/api/templates.py`, `tests/test_templates.py`.
**Tocados**: `app/config.py` (carpeta de marcas), `app/main.py` (router),
`app/models/__init__.py`.

Qué hace:

1. `POST /brands` y `GET /brands` — la marca, con su catálogo de tipografías ya
   existente (`client_fonts`) como origen.
2. `POST /brands/{id}/templates/from-project` — toma un proyecto ya importado
   (PSD o arte analizado) y **propone** la plantilla: campos según §3.3, capas
   fijas congeladas a PNG dentro de la plantilla, cajas en fracciones.
3. `PUT /brands/{id}/templates/{tid}` — corregir la propuesta: convertir
   campo↔capa fija, renombrar ids, marcar requeridos, topes de texto.
4. `GET /brands/{id}/templates/{tid}`, `GET /brands/{id}/templates`, `DELETE`.

Las rutas van **anidadas** en la marca, no sueltas en `/templates/{id}`: así el
identificador de la marca se valida antes de tocar el disco en todos los caminos
y no hace falta mantener un índice aparte.

Lo que **no** hace la etapa 1: componer, renderizar ni producir. Solo que el
objeto exista, sea correcto y no se borre.

**Congelar es literal**: los PNG de las capas fijas y la plancha se **copian** a
la carpeta de la plantilla. Si se referencian al proyecto, la plantilla se rompe
sola en ocho horas, que es el defecto que esta etapa existe para arreglar.

**Pruebas**:
- De un proyecto con producto, precio, titular, logo y legal salen dos campos
  requeridos (`producto`, `precio`) y el logo queda fijo.
- Los ids de campo son slugs únicos y estables; dos precios dan `precio` y
  `precio_2`, no un uuid.
- Después de `storage.purge_expired_projects(retention_hours=0)` la plantilla
  sigue completa y sus archivos existen.
- Las cajas están en 0..1 y se recalculan bien contra un lienzo distinto.
- `brand_id` y `template_id` inválidos no escapan de la carpeta (mismo control
  que `validate_project_id`).

**Terminado cuando**: se puede crear una plantilla, cerrar el navegador, esperar
al barrido y volver a abrirla entera.

**Lo que se midió al codificarla** (KV real, `CYBER-...-900X660-ACCESORIOS.psd`,
15 capas, 100 MB): la plantilla ocupa **0,4 MB en 16 archivos** y la importación
propone **un solo campo**. No es un fallo del derivador: el PSD llama a sus capas
«Decoración 7» y la categoría sale del nombre y de la geometría, así que el
precio y el titular llegan como decoración.

Dos consecuencias que ya están en el código:

- El clasificador de visión (`semantic_layers`) **solo corría al analizar artes
  planos**; en un PSD no lo llamaba nadie, que es justo donde hace falta. Ahora
  se consulta una vez al crear la plantilla, y solo si la importación dejó menos
  de dos campos. Degrada como el resto de proveedores: sin clave, no consulta.
- Cuando aun así la propuesta sale coja, la respuesta lo **dice** («Solo se
  reconoció 1 campo… revisa las 13 capas fijas»). Una plantilla coja en silencio
  se descubre con la tanda de doscientos artes ya hecha.

Medir no era opcional aquí: `art_text.measure()` devuelve estilo para 10 de las
15 capas del KV —incluidos el producto y el logo—, así que **no sirve** como
detector de copy. Ascender a campo todo lo medible habría llenado la plantilla
de campos falsos.

---

### Etapa 2 · Un máster por formato, aprobado

**Archivos**: `app/services/master_builder.py` (nuevo),
`app/api/templates.py` (ampliado), `tests/test_masters.py`.

Qué hace:

1. `POST /templates/{id}/masters` con `{"formats": ["meta_feed_4_5", …]}`:
   por cada formato, monta un proyecto de trabajo en memoria desde la plantilla,
   llama al motor que ya existe (`plan_variants` con `template_mode`, que elige
   entre `faithful`, `adaptive` y `source_flow` según la proporción), renderiza
   una **pieza de prueba con los valores por defecto**, la puntúa y guarda el
   máster como *propuesto*.
2. `PUT /templates/{id}/masters/{formato}` — mover cajas a mano (la rejilla de
   la interfaz, en fracciones) y `approved: true`.
3. **Un máster con un defecto que invalida no se puede aprobar.** `quality.blocking_defects()`
   ya los enumera —incluido el letterbox que se arregló en el plan anterior—.
   La API devuelve 409 con el motivo en castellano.

Por qué un máster y no recomponer: lo pidió el usuario, y además es lo que hace
que la producción masiva sea barata y determinista. Producir deja de ser
«planificar + componer + puntuar + reintentar» y pasa a ser «pegar en cajas
conocidas». Sin IA, sin sorteo, sin semilla.

**El motor sigue siendo el que propone.** Esta etapa no escribe un motor nuevo:
`layout_engine` ya sabe adaptar. Lo que se añade es el **congelado** de su
resultado y la firma de una persona.

**Pruebas**:
- Un KV 900×660 genera másters para 1:1, 4:5 y 9:16, y el de 9:16 no es un
  letterbox (reutilizar la métrica `dead_bands_v` de `quality`).
- Un máster con defecto que invalida devuelve 409 al aprobarlo.
- Mover una caja a mano y volver a leer devuelve la caja movida, no la calculada.
- Dos llamadas seguidas al mismo máster aprobado dan **el mismo PNG byte a byte**
  (es lo que significa determinista).

---

### Etapa 3 · La matriz y la producción masiva

**Archivos**: `app/models/batch.py`, `app/services/batch.py`,
`app/services/batch_csv.py`, `app/api/batches.py`, `tests/test_batch.py`;
frontend: pantalla nueva *Producción*.

El modelo:

```json
{
  "batch_id": "uuid", "template_id": "uuid",
  "formats": ["meta_feed_4_5", "meta_stories"],
  "rows": [
    {"row_id": "r1", "values": {"producto": "assets/lavadora.png",
                                "precio": "$399", "titular": "HASTA 50% DTO."},
     "status": "ok", "issues": []}
  ]
}
```

Qué hace:

1. `POST /batches` desde una plantilla. Las columnas **son** los campos de la
   plantilla; no se declaran aparte.
2. `POST /batches/{id}/rows` (rejilla) y `POST /batches/{id}/import` (CSV/XLSX).
   La cabecera del CSV son los ids de campo. Una columna que no existe en la
   plantilla es un aviso con nombre, no un error mudo.
3. **Validación por celda, antes de producir.** Es la mitad del valor de esta
   etapa: producir 200 piezas para descubrir que 30 precios no caben es el
   defecto que hay que evitar.
   - texto: se mide con `renderer.fit_text` contra la caja del campo en **cada
     formato elegido**; si baja del `min_font_size`, la celda se marca.
   - imagen: existe, tiene alfa (`product_alpha`), y su resolución da para la
     caja mayor sin pasar de `MAX_UPSCALE`.
   - requerido vacío → fila bloqueada.
4. `POST /batches/{id}/produce` — encola en Celery (ya está montado). Una tanda =
   **un** proyecto de trabajo en `data/batches/`, no uno por fila: la plancha y
   las capas fijas se comparten y solo se intercambian los valores de cada fila.
   Salida: `filas × formatos` piezas, galería, ZIP y un informe por fila.
5. `GET /batches/{id}/export` — ZIP con nombres predecibles
   (`<fila>_<formato>.png`), que es lo que pide quien luego sube a Meta.

**Pruebas**:
- 3 filas × 2 formatos = 6 piezas, y el nombre de cada archivo es el esperado.
- Una fila con el precio demasiado largo se marca **antes** de producir.
- Un CSV con una columna de más produce igual y avisa de la columna.
- El valor de una fila no se filtra a la siguiente (el defecto que `art_text.apply_batch`
  ya documenta: el precio de la primera lavadora pegado en todos los artes).

---

### Etapa 4 · El perfil de marca desde redes

**Archivos**: `app/providers/social/__init__.py`, `folder.py`, `windsor.py`,
`scraper.py`; `app/services/brand_profile.py`; `tests/test_brand_profile.py`.

Tres fuentes, una interfaz, como ya se hace con segmentación e inpainting:

```python
class SocialProvider(Protocol):
    name: str
    def available(self) -> bool: ...
    def fetch(self, handle: str, limit: int) -> list[SocialPost]: ...
```

| Proveedor | Cuándo | Requisitos |
|---|---|---|
| `folder` | Siempre disponible. Artes subidos o dejados en `data/brands/<id>/references/` | Ninguno |
| `windsor` | Instagram y Facebook orgánico de una marca conectada en Windsor.ai | `WINDSOR_API_KEY` |
| `scraper` | Perfil público de cualquier marca | `APIFY_TOKEN` (o equivalente) |

`auto` los prueba en ese orden y degrada, igual que el resto de proveedores del
proyecto. **Ninguno es obligatorio**: sin credenciales, la carpeta funciona sola.

Qué se extrae (`brand_profile.analyze`), todo medible sobre los píxeles:

- **Paleta real**: k-means sobre el conjunto, no sobre un arte. Con su reparto,
  que es lo que distingue el color de marca del color de una foto.
- **Censo de formatos**: qué proporciones publica de verdad. Sirve para
  proponer los formatos de los másters en vez de adivinarlos.
- **Dónde va el logo**: plantilla del logo de marca buscada en las referencias →
  mapa de calor de esquinas. Marcimex lo pone arriba a la derecha en todas las
  piezas adjuntas; eso es un dato, no una opinión.
- **Bandas de texto y densidad**: dónde cae la tinta de copy y cuánto lienzo
  ocupa. Da el `max_lines` y el cuerpo mínimo razonables de los campos.
- **Elementos recurrentes**: sellos y bloques que se repiten en ≥ N piezas
  (el «CRÉDITO DIRECTO» y el «SUBE DE NIVEL» de los KV adjuntos). Se ofrecen
  como capas fijas candidatas de las plantillas nuevas.
- **Ficha en castellano** con una pasada de visión (reutilizar el patrón de
  `ENABLE_LAYER_VISION`, con su caché por huella). Es lo que se le enseña al
  cliente.

Usos, en orden de utilidad:

1. **Valores por defecto** de una plantilla nueva (paleta, formatos, esquina del
   logo, cuerpos de letra).
2. **Puntaje `brand_fit`** añadido a `quality`: una pieza con colores fuera de
   paleta o el logo en otra esquina se marca. **Aviso, nunca bloqueo**: el
   cliente manda sobre la estadística.
3. **Sugerencias en la matriz** (copys que la marca usa, longitudes típicas).

Dos cautelas que hay que escribir en el código, no solo aquí:

- El scraper solo toca **contenido público** y respeta las condiciones de la
  plataforma y un límite de peticiones. Se cachea por marca y no se reintenta en
  bucle.
- Las referencias descargadas son **material del cliente**: se guardan bajo la
  marca, se borran al borrarla, y no se reutilizan para otra.

**Pruebas**: con seis imágenes sintéticas de paleta conocida, la paleta detectada
contiene esos colores y el censo de formatos cuadra. Los proveedores con red se
prueban con el transporte HTTP parcheado, como `test_magnific.py`.

---

## 5 · Lo que este plan no hace, a propósito

- **No genera diseño nuevo con IA.** Una plantilla sale de un KV aprobado por el
  cliente. La IA sigue donde ya está: recortar, vaciar fondo, extender plancha.
- **No sustituye al diseñador.** Aprobar un máster es una firma humana. El
  sistema propone y mide; no publica solo.
- **No promete convertir cualquier formato en cualquier otro.** La limitación 12
  del README sigue viva: un banner 1200×400 no se «conserva» en 1080×1920. Ahí el
  máster saldrá de `source_flow` y habrá que mirarlo.
- **No toca la retención de proyectos.** Los proyectos siguen siendo desechables;
  lo que se vuelve permanente es la marca y su biblioteca.

---

## 6 · Estado de la implementación

| Etapa | Estado |
|---|---|
| 1 · La plantilla existe y sobrevive | **Codificada y en verde** — `app/models/template.py`, `app/models/template_schemas.py`, `app/services/template_store.py`, `app/services/templating.py`, `app/api/templates.py`, `tests/test_templates.py` (12 pruebas). Suite completa: 457 pasan, 6 se saltan. Falta la pantalla en el frontend |
| 2 · Máster por formato aprobado | Pendiente |
| 3 · Matriz y producción masiva | Pendiente |
| 4 · Perfil de marca desde redes | Pendiente |
