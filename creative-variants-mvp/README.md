# Creative Variants MVP

Generador de artes publicitarios por producto a partir de **varios KV en PSD**.
Importa las capas reales de cada KV, recibe varios productos recortados en PNG y produce
la matriz KV × producto × formato. Exporta PNG final, PSD por capas y SVG editable
para Illustrator. JPG/PNG aplanados se mantienen
como flujo alternativo para ajustes finos.

- **Backend:** FastAPI (toda la lógica de negocio) · Python 3.11
- **Frontend:** Astro + TypeScript, servido por Nginx (solo presentación, consume la API)
- **Procesamiento:** Pillow + OpenCV + NumPy
- **Funciona sin GPU y sin claves externas.** OpenAI Images, SAM, PaddleOCR, FLUX y Adobe Firefly son
  proveedores **opcionales** que degradan a alternativas locales.

---

## 1. Advertencia importante sobre artes aplanados

Un JPG/PNG aplanado **no contiene las capas originales**. Este MVP no promete una
separación perfecta: implementa una **descomposición asistida**.

| Etapa | Qué hace | Qué esperar |
|---|---|---|
| Detección automática | Segmentación por contraste/bordes (OpenCV) o SAM, OCR con PaddleOCR, rostros con Haar cascade | Aproximada; cada capa trae confianza y advertencias |
| Corrección manual | Rectángulos, pincel add/subtract, re-segmentación, cambio de categoría y comportamiento | Es el paso que decide la calidad final |
| Extracción | PNG RGBA con los **píxeles originales** y bordes suavizados | Sin reescalado: se conserva la resolución |
| Reconstrucción de fondo | OpenCV local u OpenAI/FLUX/Adobe con credenciales | OpenAI genera un fondo premium una vez y lo reutiliza en el lote |
| Recomposición | 9 familias de layout con zonas relativas + variación determinista | Las capas bloqueadas nunca se deforman ni se regeneran |

Los elementos bloqueados (`locked: true`) se renderizan **siempre** desde su PNG
extraído con escala uniforme. Nunca se regeneran con IA.

---

## 2. Ejecución con Docker Compose (recomendado)

```bash
cd creative-variants-mvp
cp .env.example .env          # ajuste valores si lo necesita
docker compose up --build
```

| Servicio | URL | Notas |
|---|---|---|
| Frontend Astro | http://localhost:8501 | Interfaz visual en 4 secciones |
| Backend FastAPI | http://localhost:8000 | |
| Swagger / OpenAPI | http://localhost:8000/docs | Documentación automática |
| Health check | http://localhost:8000/health | Estado de los proveedores |

Los datos persisten en `./data` (montado como volumen). Nginx enruta `/api/` al
backend dentro de la red Docker; el navegador mantiene un único origen.

Detener y limpiar:

```bash
docker compose down            # conserva ./data
docker compose down -v         # también borra volúmenes anónimos
```

## 2.b Qué archivo subir (importante)

| Lo que sube | Qué pasa |
|---|---|
| **PSD** (KV editable) | **Mejor caso.** No se adivina nada: se importan las capas reales con su recorte exacto, su alfa, su orden y su posición. El fondo sale de las capas de relleno del propio PSD |
| **Arte publicitario aplanado** (JPG/PNG) | Descomposición asistida: detección aproximada + corrección manual en Ajustes finos |
| **PNG de producto recortado** (con transparencia) | El alfa se usa como máscara exacta del producto, pero **no hay textos ni logo que descomponer**: hay que crearlos en Ajustes finos |

### Un PSD con varias piezas (pliego)

Las agencias suelen entregar **un solo PSD con varios avisos** en el mismo lienzo
(por ejemplo el cuadrado y el vertical de dos productos). Al cargarlo se detecta
cuántas piezas trae y cada una se convierte en una plantilla independiente, con
sus capas reales y su propio tamaño de lienzo.

Cómo se detectan, en este orden:

1. **Artboards de Photoshop** (lo normal). Cada artboard es una pieza y su
   rectángulo es exacto; las capas se toman de dentro del artboard.
2. **Por geometría**, si el PSD no usa artboards: se busca el contenido separado
   por espacio vacío (los pasillos del pliego) y se recorta ahí. En este caso la
   app avisa para que revise el recorte antes de producir.

En la pantalla **Generar → Carpeta compartida** aparece una miniatura por pieza
con una casilla: se importan las marcadas (todas por defecto). En **Subir PSD** el
corte es automático y se informa cuántas piezas se encontraron.

Un PSD de una sola pieza se comporta exactamente como antes: un archivo, un
proyecto, sin sufijos en el nombre.

### PSD: importación en lugar de adivinanza

```bash
mkdir -p data/ingest && cp "MI KV.psd" data/ingest/     # admite subcarpetas
# En la interfaz: Generar → pestaña "Carpeta compartida"
```

Los PSD de 60–100 MB **no deben subirse por el navegador**: déjelos en `data/ingest/`
(ya montado como volumen) y use `GET /ingest` + `POST /projects/from-ingest`. El PSD no
se copia dentro del proyecto; solo se guarda su versión aplanada y los PNG de cada capa.

Medido con un KV real de 94 MB y 16 capas: **importación completa en 2,6 s**.

Qué se obtiene y qué no:

- ✅ Recortes exactos con alfa, posición y orden de capas reales.
- ✅ Fondo limpio sin inpainting (sale de las capas de relleno).
- ✅ La tipografía real de marca se preserva cuando el copy es un objeto inteligente
  (caso habitual): se importa como píxeles, no se rerenderiza con otra fuente.
- ⚠️ Los nombres de capa de Photoshop suelen ser genéricos (“Capa 5”, “Objeto
  inteligente vectorial copia”), así que la categoría se deduce del nombre y, si no
  dice nada, de la geometría. **Revise las categorías en Ajustes finos → Revisar lo detectado.**
- ⚠️ Solo las capas de tipo texto de Photoshop llegan como texto editable. Si el copy
  está vectorizado o en un objeto inteligente, llega como imagen —y entonces se
  reescribe desde *Textos y logos del arte*, que lo mide y lo convierte en texto.
- ⚠️ Las decoraciones importadas quedan **ancladas** a su posición relativa original:
  es lo que mantiene un CTA sobre su píldora. No participan en la reorganización.

Si sube un recorte transparente, la aplicación lo avisa explícitamente y le indica
que suba el arte aplanado si esperaba una descomposición completa.

> **Nota técnica:** la transparencia se aplana sobre **blanco**, nunca sobre negro
> (`Image.convert("RGB")` rellena el alfa con negro y ensucia fondos, paletas y
> segmentación). Ver `FLATTEN_BACKGROUND` en `services/imaging.py`.

### Cuando el producto no viene como capa

Muchos KV traen el producto **aplanado dentro de la fotografía**: la mesa sobre un
ciclorama, la sala montada en un cuarto. El importador ve una capa que cubre todo
el lienzo y es opaca, así que la toma como fondo —lo correcto para un fondo— y la
pieza se queda sin nada que reemplazar.

En el paso 3 aparece **“🔍 Detectar el producto con IA”**, que lo recupera. Hay dos
caminos según la foto:

| Foto | Qué pasa | Llamadas |
| --- | --- | --- |
| Producto sobre fondo liso | `remove-background` recorta el sujeto y el **barrido de estudio se rehace entero** a partir de la propia foto, sin IA: no hay nada que inventar, solo una superficie continua que modelar. | 1 |
| Ambiente (una sala en una habitación) | `remove-background` devuelve el cuarto entero, así que un modelo de edición deja el producto sobre fondo plano —y ahí sí se recorta— y además vacía el decorado. Lo vaciado se pega **solo donde estaba el mueble**: fuera de ahí no cambia un píxel. | 4 |

Medido con un KV real de Marcimex: la mesa de centro sale al 12 % del arte por el
camino corto; la sala en ambiente devolvía el 75 % —piso y alfombra incluidos— y
solo se separa por el segundo camino, quedando en el 13 %.

Tres cosas que conviene saber:

- ⚠️ En el camino de ambiente **el producto se regenera**: es fiel al original en
  estilo y color, pero no idéntico píxel a píxel.
- ✅ El fondo solo cambia dentro del hueco que ocupaba el producto. Perspectiva,
  línea del piso y gráficos del KV quedan intactos.
- 🧮 Quién limpia el fondo lo decide la plancha, no una preferencia. Si no tiene
  ningún borde marcado —un barrido de estudio— se modela entera desde sus píxeles
  limpios, descartando el producto y el margen donde cae su sombra, y se le
  devuelve el grano. Si tiene diseño encima —un panel de titular, una franja— se
  llama al modelo, que sí sabe reconstruir.

  Parchear solo el hueco fue el primer intento y no vale: cualquier método
  interpola desde un borde que incluye la sombra proyectada, así que la hereda y
  deja un bulto con la silueta del producto. Y pedirle a un modelo generativo que
  rellene un ciclorama termina siempre igual: mete un objeto donde no hay nada
  —en un KV real, un rollo de cartón con tipografía inventada—.
- 🔁 Ningún modelo generativo acierta siempre: de dos vaciados del mismo cuarto,
  uno salió impecable y el otro dejó un sofá puesto. Por eso la foto de partida se
  guarda intacta en `backgrounds/plancha_original.png` y **«Rehacer este recorte»**
  vuelve a empezar desde ella —no desde el intento anterior—, así que reintentar
  nunca empeora la pieza.

El modelo por defecto es `flux-kontext-max`, elegido midiéndolo con un KV real:
conserva la puerta, la planta, la repisa, el cuadro y la alfombra en su sitio,
donde `seedream-v4-5-edit` reencuadraba el cuarto entero.

```bash
POST /projects/{id}/layers/detect-product
{"provider": "auto", "scene_model": "flux-kontext-max", "force": false}
```

`force: true` repite el recorte en una pieza que ya lo tiene: retira la capa que
puso el detector y arranca otra vez desde la foto guardada.

### Cambiar el copy de la promoción y quitar elementos

El copy de un KV llega como **píxeles**. El PSD trae el precio, el nombre del
producto y el logo como objetos inteligentes, y el importador los conserva tal
cual para no perder la tipografía de marca. Es lo correcto mientras el texto no
cambie, y era justo lo que impedía producir la fila de artes de una promoción,
donde cada producto lleva su nombre, su modelo y su precio.

En **paso 2 → Textos y logos del arte** cada elemento se puede reescribir o
retirar de la pieza:

| Acción | Qué hace |
| --- | --- |
| Reescribir | La capa pasa a ser texto con el color, el peso, la interlínea y el sitio del original. Nada más se recoloca |
| Quitar del arte | Lo oculta y, si sus píxeles venían aplanados en el fondo, **también los borra de la plancha**. Sin eso, ocultar un logo aplanado no quitaba nada |
| Volver al original | Devuelve el PNG del arte importado. La plancha se rehace desde una copia anterior a cualquier edición, así que deshacer no deja restos |
| Separar en partes | Parte una capa que trae varias piezas en una capa por pieza, para poder tocar solo una |

**Separar en partes.** El PSD no trae el precio solo: en la misma capa vienen el
rótulo «PRECIO OFERTA», el precio, el precio anterior y el sello «EXCLUSIVO
ONLINE». Reescribir eso de una vez no sirve para lo que se hace siempre, que es
cambiar el precio y nada más. Cuando una capa trae varias piezas, la lista lo
avisa y ofrece separarlas; cada una pasa a ser un elemento normal —se reescribe,
se quita y se exporta por su cuenta— y «Volver a unir» deshace la separación.

Lo difícil no es cortar, es saber dónde. Un hueco no vale: en el arte real esas
cuatro piezas están a 4, 4 y 11 px unas de otras, más cerca de lo que separa la
tilde de una Á de su letra. Lo que las distingue es la forma de la mancha: una
tilde ocupa poco ancho, o lo reparte en marcas sueltas con aire en medio; un
rótulo, por corto que sea, ocupa una parte seria del ancho del bloque y lo
llena. Entre dos piezas de verdad basta con que cambie una de tres cosas —el
color, el cuerpo o un hueco grande— para separarlas; dos líneas de un mismo
párrafo no cambian ninguna y siguen juntas.

Qué se mide sobre los píxeles del original antes de escribir, y por qué:

- **Color de la tinta**, por el cubo de color más repetido. La mediana por canal
  inventa un color que no está en la imagen; cuantizar antes de contar agrupa el
  antialias con el trazo al que pertenece.
- **Alto de tinta**, para calcular el cuerpo. El texto nuevo se escribe al tamaño
  que hace que su tinta mida lo mismo, así que un precio más largo no se ve más
  pequeño.
- **Alineación**, comparando los bordes de las líneas entre sí. Con una sola
  línea no hay nada que comparar y se ancla hacia el lado del arte donde tiene
  sitio para crecer.
- **Grosor de trazo**, que decide redonda o negrita. Se escribe el texto **nuevo**
  en cada cara disponible y se conserva la que da el mismo grosor. La densidad de
  tinta no sirve: una línea de dígitos en negrita es menos densa que una de
  mayúsculas en redonda, y las dos poblaciones se solapan.
- **Cuántas líneas hay**, por los tramos horizontales con tinta. Solo se une al
  tramo de al lado un **fragmento** —la tilde de una Á, el punto de una i—,
  reconocido por ser mucho más bajo que una línea completa. Unir por cercanía
  fue el primer intento y estaba mal: dos líneas de copy con la interlínea
  normal de un KV están a menos de un tercio de su alto, así que se fusionaban
  en una y el alto de tinta salía del doble. El texto nuevo se escribía
  entonces a más del doble de cuerpo que el original.
- **Paso entre líneas**, en píxeles, para reproducir la interlínea exacta.

Dos garantías del render:

- Un copy reescrito **no lleva scrim ni píldora**. El color es el que eligió el
  diseñador sobre ese mismo fondo; taparlo con una caja gris sería estropear un
  precio que ya se leía.
- Crece **solo hacia donde su alineación se lo permite** y nunca dentro del
  vecino. Si no cabe, se reduce el cuerpo y se avisa con cuánto.
- **Quitar del arte le gana a reescribir**: escribir un texto para un elemento
  retirado no lo devuelve a la pieza. Y si se mueve a mano en *Ajustes finos*,
  la siguiente edición respeta el sitio nuevo en vez de devolverlo de un salto.

En una campaña de varias piezas, cada fila lleva **«A los N KV»**: aplica el
mismo cambio a la capa equivalente de las demás piezas del PSD. Reescribir el
precio ocho veces a mano es justo el trabajo que esta aplicación quita.

```bash
GET  /projects/{id}/texts                      # qué dice cada elemento y si es editable
POST /projects/{id}/layers/{layer_id}/text     # {"content": "$1.459,00"}
POST /projects/{id}/layers/{layer_id}/text     # {"removed": true}  · quitar del arte
POST /projects/{id}/layers/{layer_id}/text     # {"restore": true}  · volver al original
```

#### La tipografía es la mitad del resultado

Color, cuerpo, peso, interlínea y posición salen del arte. **Las letras salen de
la fuente que haya cargada**, así que sin la tipografía de marca un precio de la
campaña se escribe con la del sistema: mide y pesa igual, pero no es la misma
letra. Es lo único de esta función que no se puede deducir de los píxeles.

Se sube desde **Textos y logos del arte**, con las dos caras, y se puede aplicar
a los KV de la campaña de una vez:

```bash
POST /projects/{id}/references/font    # multipart: font=… font_bold=…
```

Tres detalles que importan:

- Antes solo se podía subir **al crear el proyecto**, tres pasos antes de que
  hiciera falta: cuando el usuario llegaba a reescribir el copy ya no había
  dónde ponerla. Ahora se añade a un KV ya importado.
- El copy **ya reescrito se recalcula** al subirla. El cuerpo y la caja de un
  texto se midieron contra las métricas de la cara anterior, y esos números
  dejan de valer; se rehacen desde el estilo del original, que es lo que se
  guarda y no cambia.
- Si solo se sube la redonda, se usa también para la negrita: la redonda de la
  marca se parece al arte más que una negrita ajena. El archivo guardado lleva
  la huella de su contenido en el nombre porque el renderer cachea la
  tipografía por ruta, y reusar `font.ttf` devolvía la anterior.

> El respaldo del sistema también tenía un fallo silencioso: la lista de
> tipografías de reserva solo traía redondas, así que fuera del contenedor
> —donde no hay DejaVu— una capa en negrita se pintaba redonda sin avisar.

### Un texto distinto por producto

Es el caso de la parrilla de una promoción: el mismo KV ocho veces, con otro
producto y con otro precio. En **paso 3 → Textos por producto** se marca qué
elementos cambian de una salida a otra y se escribe el valor de cada producto en
una tabla. Cada tanda se genera con su propia fila.

El juego de textos que viaja con la tanda es **completo, no un parche**: un
elemento reescrito para el producto anterior y ausente en esta tanda vuelve a su
texto original. Sin esa regla, el precio de la primera lavadora se quedaba
pegado en los siete artes siguientes.

```bash
POST /projects/{id}/auto
{"template_mode": true,
 "text_overrides": [{"layer_id": "…", "content": "$539.00"}]}
```

Los KV de una campaña suelen ser piezas del mismo PSD —el cuadrado y el vertical
del mismo aviso—, y sus capas no comparten identificador. La interfaz traduce
cada texto a la capa equivalente de cada pieza por categoría y posición dentro de
ella, que es lo que hace que una tabla sirva para toda la campaña.

## 2.c Tres reglas para obtener buenos resultados

1. **Dibuje las máscaras un poco más grandes que el elemento.** Lo que no se borra
   del arte original queda como fantasma en el fondo reconstruido. Si el CTA es una
   píldora de color, la máscara debe cubrir la píldora, no solo el texto.
2. **Agrupe el copy que debe viajar junto.** "HASTA 60% DE DESCUENTO" como una sola
   capa de precio se mantiene unido; en tres capas separadas el motor las reparte por
   distintas zonas del lienzo.
3. **Bloquee logo y producto.** Se renderizan desde su PNG con escala uniforme: no se
   deforman ni se regeneran nunca.

## 2.d Ponerlo en la web

En local se levantan los puertos sueltos (8501 la interfaz, 8000 la API). Para
servirlo a otras personas hay un compose aparte que cierra todo eso y deja una
sola puerta, el **8014**, detrás de un proxy con contraseña:

```bash
# hash de la contraseña → al .env, con los $ DOBLES
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'su-clave'

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

No es una precaución de manual: cada generación gasta créditos de Magnific de la
cuenta del servidor, así que una URL sin contraseña es una factura abierta.

El paso a paso para el servidor —subdominio en Plesk, secretos de GitHub y
despliegue automático en cada push— está en [DESPLIEGUE.md](DESPLIEGUE.md).

## 3. Ejecución local sin Docker

```bash
# Backend
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=../data uvicorn app.main:app --reload --port 8000

# Frontend (otra terminal)
cd frontend
npm ci
npm run dev
```

Astro enruta `/api` hacia `http://localhost:8000` durante el desarrollo; con
Docker Compose, esa misma ruta la resuelve Nginx dentro de la red interna.

**Aunque solo vaya a editar, instale las dependencias de los dos lados.** Todo
se construye en Docker, así que es fácil no tenerlas nunca en la máquina, y
entonces el editor marca en rojo un proyecto que funciona: sin `node_modules`,
el `tsconfig.json` no encuentra `astro/tsconfigs/strict`; sin el entorno de
Python, Pylance no resuelve numpy, cv2, PIL ni fastapi. Las dos carpetas
(`backend/.venv` y `frontend/node_modules`) están en el `.gitignore`, y
[.vscode/settings.json](../.vscode/settings.json) ya apunta a la primera.

---

## 4. Flujo de trabajo en la interfaz

La interfaz tiene **cuatro secciones**: *Campaña*, *Capas y KV*, *Generar* y
*Resultados*. Todas consumen los mismos contratos de FastAPI.

### Pantalla “Generar” — cuatro decisiones

1. **Carga el KV maestro.** Tres pestañas: subir un PSD (hasta `MAX_UPLOAD_MB`,
   4 GB de fábrica), tomarlo de la carpeta compartida (`data/ingest`, que no pasa por el
   navegador ni por ese tope: es la vía para los pliegos grandes) o abrir
   un trabajo anterior. Las referencias opcionales (KV, logo, tipografía) están dentro de
   un desplegable, porque casi nunca hacen falta.
2. **Revisa todas las capas.** El inventario recorre el árbol completo del PSD, incluidas
   rutas de grupo, capas ocultas, capas fuera de la pieza y capas que no se rasterizan por
   el límite de memoria. Las capas utilizables muestran miniatura y función editable.
   Debajo, **Textos y logos del arte** permite reescribir el copy —precio, nombre,
   condiciones— o quitar un elemento de la pieza, borrándolo también del fondo cuando
   venía aplanado.
3. **Carga y organiza los productos.** Sube uno o varios PNG transparentes. En
   **Textos por producto** se marca qué parte del copy cambia en cada arte y se escribe
   el valor de cada producto. El sistema retira
   automáticamente todos los productos originales y usa el PSD como plantilla editable.
   Logo, copy, fondo y decoraciones se conservan, mientras cada producto nuevo participa
   en una recomposición completa del arte. Se pueden producir separados o juntos, con
   disposición automática, en fila, apilada o superpuesta; una vista previa confirma cada
   combinación antes de generar.
4. **Elige formatos y genera.** El modo automático conserva el flujo anterior. La selección
   por plataforma ofrece presets de Meta, Google Ads y YouTube con proporción y guía de
   área segura. Un botón recorre los productos y conserva juntas todas las
   tandas. El backend hace cada tanda en una llamada
   (`POST /projects/{id}/auto`): detectar → recortar → preparar el fondo → componer.
   Se garantiza al menos una propuesta por preset elegido. Debajo aparece la galería con
   descarga individual y ZIP (PNG + PSD + SVG); “Qué hizo el sistema”
   despliega el informe paso a paso en lenguaje llano.

Los porcentajes de zona segura de Stories/Reels se guardan como recomendaciones de UI
del material de producción entregado, no como validaciones de rechazo. YouTube usa sus
resoluciones oficiales, pero este proyecto produce el **key visual estático**, no codifica
un archivo de video.

Medido: PSD de 94 MB ya importado → 9 variantes en **1,7 s** (promedio 84/100).
JPG plano de 1200×1200 → 6 variantes en **14 s** (incluye detección e inpainting).

Las opciones que antes ocupaban una pantalla entera (intensidad, instrucción textual,
semilla) están en el desplegable *Opciones (no hacen falta)*. La **instrucción textual**
interpreta palabras clave, sin IA: `producto grande`, `producto pequeño`,
`titular arriba`, `centrado`, `vertical`, `diagonal`, `dividido`, `izquierda`,
`derecha`, `minimal`, `texto grande`.

### Pantalla “Ajustes finos” — solo si hace falta

Un selector con tres herramientas; se muestra una sola a la vez (no son pestañas, para
no pagar el coste de renderizar las tres):

- **Revisar lo detectado.** El original con bounding boxes, categoría, texto reconocido y
  confianza (🟢 alta, 🟡 media, 🔴 baja). Permite corregir qué es cada elemento.
- **Corregir un recorte.** Previsualización de la máscara (verde = incluido), rectángulo
  numérico, auto-segmentar, pincel rectangular o elíptico para añadir/quitar zonas,
  comportamiento por capa (`visible`, `locked`, `movable`, `resizable`, `reorderable`,
  `replaceable`, `preserve_aspect_ratio`), edición de textos, orden, extracción de PNG y
  reconstrucción del fondo.
- **Control total.** La configuración completa de generación: cantidad, semilla, formatos,
  intensidad, familias de layout y la matriz de permisos por capa.

> El pincel de máscara se implementa con Canvas nativo del navegador; también se
> conservan la edición numérica y la re-segmentación asistida.

---

## 5. Motor de layouts

Nueve familias. Ocho reorganizan la composición con **zonas relativas**
(`x, y, width, height` en 0..1), así que funcionan en cualquier formato; la novena
reproduce el diseño original:

1. `product_left` — Producto izquierda / texto derecha
2. `product_right` — Producto derecha / texto izquierda
3. `product_center_headline_top` — Producto centrado / titular arriba
4. `headline_center_product_bottom` — Titular centrado / producto abajo / CTA inferior
5. `vertical_stack` — Composición vertical apilada
6. `diagonal_flow` — Composición diagonal
7. `split_blocks` — Dos bloques divididos
8. `hero_product_overlay` — Producto grande con texto en zona segura
9. `faithful` — **Fiel al original**: cada elemento en su sitio del arte importado

La familia `faithful` no entra en el sorteo aleatorio: `plan_variants` la asigna a la
**primera variante de cada formato cuya proporción se parezca a la del arte original**
(±18 %, `FAITHFUL_ASPECT_TOLERANCE`), y fuerza el fondo real (`plate`). Es la que
devuelve "el KV tal cual, con el producto nuevo". Volcar un banner 1200×400 a 1080×1920
no conserva el diseño: lo destruye, así que ahí no se usa.

```python
LAYOUTS["product_left"]["zones"] = {
    "logo":     [0.05, 0.04, 0.20, 0.09],
    "product":  [0.04, 0.22, 0.48, 0.60],
    "headline": [0.56, 0.18, 0.39, 0.22],
    "cta":      [0.56, 0.72, 0.32, 0.09],
    ...
}
```

**Adaptación por formato:** los layouts de columnas se recomponen automáticamente en 9:16
(`VERTICAL_OVERRIDES`) y los apilados en 16:9 (`LANDSCAPE_OVERRIDES`), así que un mismo
layout produce composiciones distintas según el lienzo.

Variación determinista por semilla sobre: escala, alineación, anclaje, espaciado,
posición, orden visual (z-index) y estilo de fondo (`plate`, `plate_blur`, `plate_zoom`,
`solid`, `gradient`, `duotone`). La misma semilla + misma configuración produce
exactamente el mismo resultado.

### Elementos anclados

Las capas importadas de un PSD que reproducen el diseño (decoraciones siempre, y todas
las capas en la familia `faithful`) se colocan **ancladas**: no se recolocan, no se
sortean y no participan en la resolución de solapes. Reglas concretas:

- **Un solo factor de escala** para todo el diseño (el que hace caber el arte completo).
  Escalar la posición con un factor y el tamaño con otro es lo que separaba el texto de
  un CTA de su píldora (`_pinned_box`).
- **Sin margen de seguridad**: un elemento a sangre toca el borde por definición.
- **Escenografía estirada al borde**: lo que abarcaba un eje entero del arte original
  (un fondo, una franja de lado a lado) se estira en ese eje hasta el borde del nuevo
  lienzo. Es la única deformación autorizada del sistema y **nunca** alcanza a producto,
  logo ni persona: esas capas son `pixel_critical` y el permiso (`Placement.stretch`) no
  se les concede.
- La **opacidad de capa del PSD se aplica** al importar: `composite()` de psd-tools
  devuelve la capa a plena intensidad, así que sin esto una forma al 34 % se veía tres
  veces más marcada que en el diseño.

### Reglas que el motor garantiza

- Imágenes ajustadas con *contain*: **nunca** se deforman ni se recortan (salvo la
  escenografía a sangre descrita arriba).
- Relación de aspecto preservada (verificada además por el puntaje de calidad).
- Todo dentro de los márgenes seguros del lienzo, excepto lo anclado a sangre.
- Solapamientos graves resueltos por prioridad (logo > producto > persona > legal >
  titular > precio > CTA > subtítulo > decoración).
- El **texto legal** queda anclado al pie y siempre visible.
- Sin texto sobre zonas de bajo contraste: se recolorea o se añade un fondo translúcido.
- Jerarquía tipográfica por categoría (topes de tamaño y de número de líneas).
- El CTA se dibuja como botón con color de marca.
- Los archivos originales nunca se modifican.

---

## 6. Validación de calidad

Puntaje 0–100 con advertencias accionables:

```json
{
  "score": 84,
  "warnings": ["'CTA' está demasiado cerca del margen."],
  "metrics": {"product_coverage": 0.31, "severe_overlaps": 0, "fill_ratio": 0.42}
}
```

Evalúa: elementos fuera del lienzo, solapamientos, márgenes, cobertura del producto,
tamaño mínimo del logo, legibilidad del texto, contraste real medido sobre la imagen
renderizada, presencia de elementos obligatorios, preservación de la relación de aspecto
y cantidad de espacio vacío.

**Lo que el puntaje no juzga** (y por qué): un elemento anclado reproduce el diseño
aprobado por el diseñador, así que no se le aplican las heurísticas de márgenes,
solapes, sangrado ni deformación autorizada. Castigar eso sería castigar la fidelidad.
Dos consecuencias más:

- El relleno se mide como **área de la unión** de las cajas de contenido, no como suma:
  sumar cuenta dos veces lo que se solapa (y en un KV importado casi todo se solapa), y
  la escenografía a sangre se excluye porque un fondo de color no es contenido.
- Si el copy del PSD llegó **rasterizado** (lo habitual), no se penaliza la ausencia de
  capas de texto: el texto está, como píxeles. Se avisa de que hay que reescribirlo
  desde *Textos y logos del arte*.
- Un elemento que el usuario **quitó a propósito** no cuenta como falta. Pedir de vuelta
  el logo que se acaba de retirar sería discutirle la decisión.

No hay modelo predictivo. La interfaz para conectar el **Predictor Creativo** está en
`backend/app/services/predictor.py`:

```python
from app.services.predictor import CreativePredictor, set_predictor

class MiPredictor:            # implementa CreativePredictor
    name = "ctr-v1"
    def available(self): return True
    def predict(self, project, plan, image, quality): return {"ctr_index": 0.83}

set_predictor(MiPredictor())
```

---

## 7. API

```text
GET    /health                                     Estado + proveedores activos
GET    /capabilities                               Formatos, layouts, intensidades y modelos de IA
GET    /ingest                                     Artes disponibles en data/ingest (?with_pieces=)
GET    /ingest/pieces                              Piezas que contiene un PSD (?source=)
GET    /ingest/pieces/preview                      Miniatura PNG de una pieza (?source=&index=)
POST   /projects/from-ingest                       Crear proyecto desde la ingesta (PSD)
POST   /projects/from-ingest/split                 Un proyecto por cada pieza del pliego
POST   /projects                                   Crear (multipart: name, artwork, kv?, logo?, font?)
POST   /projects/split                             Subir un pliego y crear un proyecto por pieza
GET    /projects                                   Listar
GET    /projects/{project_id}                      Obtener
DELETE /projects/{project_id}                      Eliminar
POST   /projects/{project_id}/auto                 Todo en un paso: detectar, recortar, fondo y generar
POST   /projects/{project_id}/analyze              Detección automática (segmentación + OCR)
PUT    /projects/{project_id}/layers               Actualizar / reordenar / eliminar capas
POST   /projects/{project_id}/layers               Crear capa manual (rectángulo o texto)
POST   /projects/{project_id}/layers/mask          Corregir máscara (pincel add/subtract)
GET    /projects/{project_id}/layers/replaceable   Elementos que pueden recibir otro producto
POST   /projects/{project_id}/layers/replace       Cambiar el producto (multipart: image, layer_id?, hide_others?)
POST   /projects/{project_id}/extract              Extraer capas como PNG RGBA
POST   /projects/{project_id}/reconstruct-background   Inpainting del fondo (provider, model)
POST   /projects/{project_id}/generate             Encolar generación de variantes
GET    /projects/{project_id}/tasks/{task_id}      Estado de una tarea encolada
GET    /projects/{project_id}/variants             Listar variantes
GET    /projects/{project_id}/variants/{id}        Metadatos (o imagen con ?download=true)
GET    /projects/{project_id}/export               ZIP (?variant_ids=&include_layers=)
GET    /projects/{project_id}/files/{ruta}         Servir un archivo del proyecto
GET    /projects/{project_id}/preview/detections   Original con bounding boxes
GET    /projects/{project_id}/preview/mask/{id}    Original con la máscara resaltada
```

`POST /projects/split` y `POST /projects/from-ingest/split` devuelven
`{projects, pieces_detected, pieces_imported, warnings}`. Con `pieces` se eligen
índices concretos (`[0, 2]`); vacío importa todas. Los endpoints sin `/split`
siguen devolviendo un único proyecto con el pliego completo.

El modelo de IA se elige por petición: `POST /reconstruct-background` acepta
`{"provider": "magnific", "model": "mystic"}` y `POST /auto` acepta
`{"background_provider": "magnific", "background_model": "mystic"}`. Los ids válidos
salen de `GET /capabilities` → `image_models`.

Camino corto (lo que hace la interfaz):

```bash
PID=$(curl -s -F "artwork=@arte.png" http://localhost:8000/projects | jq -r .project_id)
curl -s -X POST http://localhost:8000/projects/$PID/auto \
     -H 'Content-Type: application/json' -d '{"count":9}' | jq '.steps, (.variants|length)'
curl -s "http://localhost:8000/projects/$PID/export" -o variantes.zip
```

`formats` vacío o ausente = el tamaño nativo del arte (el formato soportado con la
proporción más cercana) más `1080x1080` y `1080x1350`.

Camino largo, paso por paso (control total):

```bash
PID=$(curl -s -F "name=Demo" -F "artwork=@arte.png" http://localhost:8000/projects | jq -r .project_id)
curl -s -X POST http://localhost:8000/projects/$PID/analyze -H 'Content-Type: application/json' -d '{}' >/dev/null
curl -s -X POST http://localhost:8000/projects/$PID/layers -H 'Content-Type: application/json' \
     -d '{"name":"Producto","category":"product","type":"image","x":200,"y":300,"width":600,"height":700,"locked":true}'
curl -s -X POST http://localhost:8000/projects/$PID/reconstruct-background -H 'Content-Type: application/json' -d '{}'
curl -s -X POST http://localhost:8000/projects/$PID/generate -H 'Content-Type: application/json' \
     -d '{"count":12,"seed":42,"formats":["1080x1080","1080x1350","1080x1920"],"intensity":"moderate"}' | jq '.variants[].quality.score'
curl -s -o variantes.zip "http://localhost:8000/projects/$PID/export"
```

---

## 8. Modelo de datos

```text
data/projects/{project_id}/
├── original/       arte original (nombre interno seguro, nunca modificado)
├── references/     KV, logo y tipografía opcionales
├── layers/         PNG RGBA extraídos
├── masks/          máscaras en escala de grises (tamaño del lienzo)
├── backgrounds/    máscara de borrado + fondo reconstruido
├── variants/       PNG de cada variante + thumbnails
├── exports/        PSD, SVG para Illustrator y ZIP generados
├── tmp/            temporales (se limpian de forma segura)
└── project.json    lienzo, capas, análisis, fondo y variantes
```

`project.json` (extracto):

```json
{
  "project_id": "uuid",
  "canvas": {"width": 1080, "height": 1350},
  "layers": [
    {
      "id": "uuid", "name": "Producto", "type": "image", "category": "product",
      "src": "layers/product_1a2b3c4d.png", "mask": "masks/product_1a2b3c4d.png",
      "x": 210, "y": 390, "width": 650, "height": 720, "rotation": 0, "z_index": 3,
      "visible": true, "locked": true, "movable": true, "resizable": true,
      "reorderable": true, "preserve_aspect_ratio": true, "confidence": 0.92
    },
    {
      "id": "uuid", "name": "Titular", "type": "text", "category": "headline",
      "content": "Conoce lo nuevo", "font_family": "DejaVu Sans", "font_size": 68,
      "font_weight": "bold", "color": "#FFFFFF", "text_align": "left",
      "x": 100, "y": 130, "width": 700, "height": 180, "z_index": 6
    }
  ]
}
```

---

## 9. Proveedores opcionales

Todos son **opcionales** y de carga diferida: nada se descarga al iniciar la aplicación.

### Segmentación: SAM 2 / SAM

```bash
pip install -r backend/requirements-sam.txt
# descargue el checkpoint a mano, p. ej.:
#   https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt
```

```env
SEGMENTATION_PROVIDER=sam
SAM_VARIANT=sam2
SAM_CHECKPOINT=/models/sam2.1_hiera_small.pt
SAM_MODEL_TYPE=configs/sam2.1/sam2.1_hiera_s.yaml
```

Descomente el volumen `./models:/models:ro` en `docker-compose.yml`. Si el checkpoint no
existe, el sistema avisa y usa el proveedor local de OpenCV.

### OCR: PaddleOCR

```bash
docker compose build --build-arg INSTALL_OCR=true backend
docker compose up -d backend
```

Sin PaddleOCR, `/analyze` devuelve una advertencia clara y las capas de texto se crean a
mano en Ajustes finos. No se intenta identificar la tipografía real: se usa una fuente por
defecto (DejaVu) modificable, o la tipografía que suba el usuario.

### Generación con IA: Magnific (recomendado)

Una sola clave abre todo el catálogo de Magnific y **el modelo se elige desde la
interfaz**, pieza por pieza. Pegue la clave y reinicie:

```env
INPAINTING_PROVIDER=auto        # usa Magnific en cuanto haya clave
MAGNIFIC_API_KEY=...            # https://www.magnific.com/user/organization/api-keys
MAGNIFIC_MODEL=ideogram-image-edit   # modelo por defecto
MAGNIFIC_SCENE_MODEL=flux-kontext-max   # separa producto y decorado en fotos de ambiente
```

```bash
docker compose up -d --build backend worker
```

Modelos disponibles (`GET /capabilities` → `image_models`):

| Modelo | Máscara real | Para qué sirve |
| --- | --- | --- |
| `ideogram-image-edit` | ✅ | Repinta **solo** el hueco de los productos borrados. El más fiel al KV; es el valor por defecto. |
| `mystic` | — | Modelo propio de Magnific. Fotorrealismo 1K/2K/4K guiado por la estructura del arte. |
| `flux-kontext-pro`, `flux-kontext-max` | — | Edición por instrucción con buena coherencia de contexto. |
| `flux-2-pro`, `flux-2-flex`, `flux-2-turbo`, `flux-2-klein` | — | Familia Flux 2: de máxima calidad a máxima velocidad. |
| `seedream-v4-edit`, `seedream-v4-5-edit`, `seedream-v5-lite-edit`, `seedream-v5-pro-edit` | — | Preservan muy bien textura, color e iluminación al editar. |
| `gemini-2-5-flash-image-preview`, `nano-banana-pro`, `nano-banana-pro-flash` | — | Google Gemini: rápidos y baratos; Pro llega a 4K. |

Cómo trabaja el proveedor:

1. **Con máscara** (`ideogram-image-edit`): se envía el arte y la máscara invertida
   (la API de Ideogram edita el área negra) y la IA solo repinta ese hueco.
2. **Sin máscara**: primero se limpia el arte en local con OpenCV —así el modelo no
   tiene que adivinar qué producto quitar— y se manda como referencia de estructura
   o imagen de entrada según el modelo.
3. En ambos casos el resultado se **recompone localmente solo dentro de la zona
   borrada**, con el borde suavizado (`MAGNIFIC_FEATHER`). El resto del KV queda
   idéntico al original, pixel a pixel.
4. Las imágenes viajan en base64 si pesan menos de `MAGNIFIC_INLINE_MAX_MB`; por
   encima se suben antes con la Upload Files API de Magnific.

Ajustes finos (todos opcionales, ver `.env.example`): `MAGNIFIC_RESOLUTION`,
`MAGNIFIC_RENDERING_SPEED`, `MAGNIFIC_MYSTIC_MODEL`, `MAGNIFIC_ENGINE`,
`MAGNIFIC_STRUCTURE_STRENGTH`, `MAGNIFIC_ADHERENCE`, `MAGNIFIC_HDR`,
`MAGNIFIC_CREATIVE_DETAILING`.

Si la llamada falla o se agota el tiempo, el backend cae a OpenCV Inpaint y lo
reporta en las advertencias de la pieza.

### Inpainting: OpenAI, FLUX Fill o Adobe Firefly

```env
INPAINTING_PROVIDER=auto      # usa el externo solo si hay credenciales
OPENAI_API_KEY=...            # GPT Image (solo reconstruye el fondo)
BFL_API_KEY=...               # FLUX (Black Forest Labs)
ADOBE_CLIENT_ID=...
ADOBE_CLIENT_SECRET=...
ADOBE_UPLOAD_BASE_URL=https://mi-dominio/publico
```

Si la llamada externa falla, el backend cae automáticamente a OpenCV Inpaint y lo
reporta en las advertencias. Las claves viven solo en el backend: **nunca** se exponen al
frontend.

---

## 10. Pruebas

```bash
# En Docker (recomendado; no requiere GPU ni claves)
docker compose run --rm --no-deps backend python -m pytest

# Local
cd backend && python -m pytest
```

La suite cubre: subidas válidas e inválidas (SVG, extensión falsa, tamaño, dimensiones),
creación de proyectos, escritura y lectura de `project.json`, integridad del archivo
original, extracción de capas por máscara, transparencia real del PNG, preservación de la
relación de aspecto, edición de máscaras con pincel, actualización/eliminación/reorden de
capas, reconstrucción de fondo, zonas relativas y adaptación por formato, generación
determinista por semilla, detección de solapamientos y de elementos fuera del lienzo,
renderizado de variantes, presets por plataforma, zonas seguras, disposición de grupos,
inventario completo del PSD, puntajes, creación del ZIP con PSD/SVG y bloqueo de path
traversal, manejo de PNG con transparencia (alfa como máscara, aplanado sobre blanco) y
penalización de composiciones vacías, importación de PSD multicapa (con un generador
de PSD sintéticos en `tests/psd_fixture.py`), clasificación por nombre de capa y la
carpeta de ingesta con su bloqueo de path traversal, y el **modo automático**
(`/auto`: los cuatro pasos en orden, elección del formato nativo por proporción, reuso de
capas ya listas y rechazo de formatos desconocidos), el **cambio de producto** (ajuste
sin deformar, recorte del aire sobrante, caja unión al ocultar los demás, aviso si el
PNG no tiene transparencia, rechazo de SVG y de capas inexistentes) y la **familia
fiel** (posiciones reproducidas, solo en formatos de proporción parecida, fondo real,
permiso de estirado restringido a la escenografía y sin penalizaciones de composición). Todas usan imágenes sintéticas
creadas en el momento.

La interfaz se valida con `astro check` y `astro build` dentro de su imagen Docker.
La integración HTTP se comprueba a través del proxy `/api`, además de la suite completa
del backend.

---

## 11. Seguridad implementada

- Validación real del tipo de archivo (magic bytes + decodificación con Pillow).
- **SVG rechazado** por contenido, antes que por extensión.
- Nombres internos seguros (UUID + extensión canónica); el nombre subido nunca se usa.
- Límite de tamaño configurable, aplicado durante la lectura por bloques.
- Protección contra path traversal (`resolve_inside`) y `project_id` validado como UUID.
- El contenido subido nunca se ejecuta; escritura atómica de `project.json`.
- Límite de píxeles de Pillow para evitar *decompression bombs*.
- Claves de API solo en el backend, vía `.env`.
- Borrado seguro del proyecto y de los temporales.

---

## 12. Limitaciones actuales

1. La segmentación local (OpenCV) es heurística: en fondos complejos o degradados suele
   proponer regiones amplias. **Habilite SAM** o corrija a mano para mejores resultados.
2. El inpainting con OpenCV es difuso cuando la zona borrada es grande o texturada.
3. Sin PaddleOCR no hay detección de texto: hay que crear las capas de texto a mano.
4. La tipografía original no se identifica. En artes aplanados se usa DejaVu o la fuente
   que suba el usuario; los PSD conservan la tipografía real si el copy viene como objeto
   inteligente. Las fuentes de Adobe Fonts (p. ej. **Obviously**) no se pueden empaquetar
   en el servidor: si necesita rerenderizar texto con ellas, suba el `.otf` que su licencia
   le permita usar.
5. El texto se renderiza horizontal: se avisa cuando el OCR detecta rotación.
6. La corrección de máscaras usa rectángulos y pincel numérico, no dibujo libre.
7. La instrucción textual es por palabras clave, no un modelo de lenguaje.
8. El puntaje de calidad son reglas de composición, no una predicción de desempeño.
9. Sin autenticación, cola de tareas ni multiusuario (fuera del alcance del MVP).
10. La generación es sincrónica: 30 variantes en 1920×1080 pueden tardar ~1 minuto.
11. **Los modos de fusión del PSD no se aplican.** La opacidad de capa sí (y también la
    de los grupos, acumulada), pero una capa en *multiplicar* o *trama* se compone en
    normal. En los KV probados no aparecen; si un arte los usa, sus decoraciones se verán
    más marcadas que en Photoshop.
12. Reproducir el diseño original solo tiene sentido en formatos de proporción parecida
    (±18 %). Fuera de ahí, el motor reorganiza: no hay forma de "conservar" un banner
    1200×400 dentro de un 1080×1920.
13. Illustrator se entrega como **SVG editable**, no como `.ai` nativo. Hay que abrir el
    SVG en Illustrator, comprobar la tipografía y guardarlo como `.ai`. Llegan como
    texto real dos cosas y solo dos: el **legal** cuando procede de una capa de texto
    o el usuario lo confirma, y el **copy reescrito** en *Textos y logos del arte*.
    En los dos casos el contenido, la tipografía, el cuerpo, el color y el sitio los
    conoce el sistema con exactitud. El resto del copy permanece raster a propósito:
    de un objeto inteligente de Photoshop no se sabe con qué estaba escrito, y
    convertirlo en texto sería publicar una imitación —o, en un legal, un OCR
    contractual incorrecto—. El SVG declara la **familia real del archivo** con el
    que se pintó; antes escribía siempre la del valor por defecto, así que un arte
    hecho con la fuente de marca llegaba al diseñador pidiéndole DejaVu. El
    manifiesto del ZIP lista lo editable en `editable_text_layers`.

## 13. Próximos pasos recomendados

1. Habilitar SAM 2 y evaluar la mejora de las máscaras frente al proveedor local.
2. Añadir un detector de logos y personas entrenado (p. ej. YOLO) para clasificar mejor.
3. Conectar FLUX Fill para fondos de calidad de producción.
4. Implementar el Predictor Creativo real sobre la interfaz ya provista.
5. Añadir plantillas de marca (paleta, tipografías, márgenes) reutilizables por cliente.
6. Persistir versiones de proyecto e historial de variantes aprobadas.
7. Mover la generación a un worker asíncrono cuando se supere el volumen del MVP.
8. Añadir exportación `.ai` nativa si Adobe publica una API de escritura adecuada.
9. Leer los modos de fusión del PSD y aplicarlos al componer (multiplicar, trama, etc.).
