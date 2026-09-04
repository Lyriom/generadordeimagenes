# Subirlo al servidor (Plesk + Docker)

Mismo esquema que mkpone y futbolecuador: el código se sincroniza por rsync a un
build-context del servidor, allí se reconstruye con `docker compose`, y el nginx
de Plesk hace de proxy del subdominio al puerto del contenedor.

Dos diferencias con esos, y las dos importan:

- **Cuatro contenedores, no uno**: interfaz, API, worker y Redis. Solo el proxy
  publica puerto (el **8014**, y solo en `127.0.0.1`); los otros tres quedan en
  la red interna de Docker.
- **Hay datos que no se pueden borrar**: `data/` guarda los proyectos, los
  recortes y las artes generadas. El rsync la excluye; sin esa línea, cada
  despliegue borraría el trabajo del equipo.

Los valores ya puestos en el flujo:

| Qué | Valor |
| --- | --- |
| Subdominio | `generador.misiva.com.ec` |
| Build-context | `/var/www/vhosts/misiva.com.ec/GENERADOR` |
| Puerto interno | `127.0.0.1:8014` |

---

## Paso 0 · Fusionar a `main`

El flujo solo despliega desde `main`. Mientras el trabajo siga en
`feat/magnific-y-pliegos-psd` no se sube nada, por muchos push que haya.

```bash
git checkout main && git merge feat/magnific-y-pliegos-psd && git push
```

Hazlo **al final**, cuando los pasos 1–6 estén hechos: ese push es el que
dispara el primer despliegue automático.

## Paso 1 · El subdominio en Plesk

Websites & Domains → **Add Subdomain** → `generador.misiva.com.ec`. *(Hecho.)*

Plesk crea una carpeta `httpdocs` para él. **Se queda vacía**: el sitio lo sirve
el contenedor y Plesk solo reenvía.

## Paso 2 · Preparar el build-context

Por SSH, en el servidor:

```bash
mkdir -p /var/www/vhosts/misiva.com.ec/GENERADOR
cd /var/www/vhosts/misiva.com.ec/GENERADOR
```

> **Por qué ahí y no dentro de `httpdocs`.** Todo lo que cuelga de un `httpdocs`
> lo sirve nginx como archivo. Si el código viviera ahí, cualquiera podría
> descargar el `.env` —con la clave de Magnific dentro— escribiendo la URL. Esta
> carpeta es hermana de `httpdocs`, no de un vhost, así que no la sirve nadie.

Comprueba primero que la ruta base es esa (en Plesk es lo normal, pero conviene
verlo):

```bash
ls -d /var/www/vhosts/misiva.com.ec
```

Si tu servidor usara otra raíz, cámbiala en **una sola línea**:
`DEPLOY_PATH`, en `.github/workflows/deploy.yml`.

La primera vez hay que traer el código a mano, para poder arrancarlo y
comprobarlo antes de automatizar nada:

```bash
git clone https://github.com/Lyriom/generadordeimagenes.git /tmp/gen
cp -r /tmp/gen/creative-variants-mvp/. .
rm -rf /tmp/gen
```

## Paso 3 · Las claves, solo en el servidor

```bash
cp .env.example .env
nano .env
```

Hay que rellenar dos cosas:

```env
MAGNIFIC_API_KEY=...            # la de Magnific
CV_USER=equipo
CV_PASSWORD_HASH=$$2a$$14$$...  # ver abajo
```

El hash se genera así:

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'la-clave-que-quieras'
```

> **Duplica cada `$` al pegarlo.** Docker Compose interpreta `$algo` como una
> variable y se come medio hash; el proxy acaba rechazando la contraseña
> correcta sin decir por qué. Es el único paso donde es fácil perder media hora.

Este `.env` **no está en el repositorio y no debe estarlo**, y el rsync lo
excluye: se queda en el servidor pase lo que pase. El flujo aborta el despliegue
si no lo encuentra, para no levantar nunca el sitio sin contraseña.

Para comprobar que el hash llega entero antes de arrancar nada:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm --no-deps --entrypoint sh proxy \
  -c 'case "$CV_PASSWORD_HASH" in "\$2"*) echo "hash OK";; *) echo "hash MAL: revise los \$ dobles";; esac'
```

> No sirve mirarlo con `docker compose config`: ese comando vuelve a duplicar los
> `$` en su salida —para que lo que imprime siga siendo un compose válido—, así
> que un hash correcto ahí se ve igual que uno mal escrito. Hay que preguntárselo
> al contenedor, que es quien lo recibe de verdad.

Y `chmod 600 .env`: lleva la clave de Magnific en texto plano y los permisos por
defecto de Plesk dejan que la lea cualquier otro usuario del servidor.

## Paso 4 · Levantarlo a mano una vez

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

La primera construcción tarda unos minutos. Comprobar que hay una sola puerta:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8014/   # 200
```

El sitio va **sin contraseña** desde el 01/09/2026, a petición del equipo: el
bloque `basic_auth` está comentado en el `Caddyfile`. Para volver a ponerla basta
descomentar esas tres líneas, comprobar que `CV_PASSWORD_HASH` está en el `.env`
y reiniciar el proxy —y cambiar el 200 por 401 en la última comprobación del
flujo de GitHub—.

No compruebes el `:8000` de este servidor: ese puerto ya lo ocupa otro proyecto
y responde su propio `/health`. El backend nuestro no publica puerto ninguno.

Si el proxy entra en crash-loop nada más arrancar, es el kernel viejo del host
—lo mismo que le pasó a mkpone con `nginx:alpine`—: descomenta el bloque
`security_opt` del servicio `proxy` en `docker-compose.prod.yml`.

## Paso 5 · Que Plesk mande el subdominio al 8014

En `generador.misiva.com.ec` → **Apache & nginx Settings**:

1. Desmarca **Proxy mode** (que nginx sirva directo, sin pasar por Apache).
2. En **Additional nginx directives**, pega:

```nginx
location ~ ^/ {
    client_max_body_size 0;

    proxy_pass http://127.0.0.1:8014;
    proxy_http_version 1.1;

    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";

    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}
```

Tres cosas de ahí no son opcionales:

- **`Upgrade` y `Connection`**: ya no son necesarios para Astro, pero pueden
  conservarse sin efecto para mantener compatible el bloque del proxy.
- **`client_max_body_size 0`**: sin tope. Un pliego de agencia con cinco KV
  pasa de 300 MB y el límite por defecto lo corta con un 413 seco, sin decir
  por qué. Quien decide el tamaño máximo es el backend (`MAX_UPLOAD_MB`), que
  contesta con un mensaje que se entiende; los proxys de en medio van sin
  límite a propósito, porque tres números que hay que mantener iguales acaban
  siempre en uno que se quedó atrás. Va **dentro** del `location`: Plesk ya
  escribe esa misma directiva en el vhost, y declararla otra vez arriba hace
  que nginx rechace toda la configuración con «directive is duplicate». Pon
  además el campo *Maximum allowed HTTP request body size* de esa pantalla a 0.
- **`location ~ ^/` y no `location /`**: Plesk ya define un `location /` en cada
  uno de sus dos bloques de servidor (el que reenvía a Apache), así que el
  nuestro daría «duplicate location». Los `location` de expresión regular van en
  otra lista, no chocan, y ganan sobre el prefijo `/`. El
  `^~ /.well-known/acme-challenge/` de Plesk sigue teniendo prioridad sobre el
  regex, así que Let's Encrypt puede seguir emitiendo el certificado.
- **`proxy_read_timeout 600s`**: una tanda de generación tarda minutos.

Después, el certificado: SSL/TLS Certificates → **Let's Encrypt**.

## Paso 6 · Los secretos de GitHub

En `Lyriom/generadordeimagenes` → Settings → Secrets and variables → **Actions**.
Los mismos cuatro nombres y los mismos valores que en `FUTBOLECUADOR_BACk`
(GitHub no comparte secretos entre repositorios: hay que volver a pegarlos):

| Secreto | Qué es |
| --- | --- |
| `DEPLOY_SSH_HOST` | la IP o el host del servidor (`69.64.85.167`) |
| `DEPLOY_SSH_USER` | el usuario SSH — tiene que poder ejecutar `docker` |
| `DEPLOY_SSH_PORT` | el puerto SSH |
| `DEPLOY_SSH_KEY` | la clave privada, completa, con las líneas `BEGIN`/`END` |

Si no tienes una llave dedicada, se genera en el servidor:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/deploy_generador -N ''
cat ~/.ssh/deploy_generador.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

**No copies la clave privada del terminal al portapapeles.** Basta que se pierda
un salto de línea para que el despliegue falle con `error in libcrypto:
Permission denied`, que no dice nada de lo que pasa de verdad. Pásala por
archivo, desde tu máquina:

```bash
scp root@SERVIDOR:~/.ssh/deploy_generador /tmp/deploy_generador
gh secret set DEPLOY_SSH_KEY --repo Lyriom/generadordeimagenes < /tmp/deploy_generador
rm /tmp/deploy_generador
```

Un aviso sobre ese `scp`: pide contraseña, así que **no pegues los tres comandos
de golpe** —el prompt de la contraseña se traga las líneas siguientes y parece
que se ejecutaron—. Uno a uno.

## Paso 7 · A partir de aquí, solo `git push`

Cada push a `main`:

1. Construye las imágenes y corre las 159 pruebas. **Si algo falla, no despliega.**
2. Comprueba que el build-context es el correcto y que el `.env` está.
3. Sincroniza el código, respetando `.env` y `data/`.
4. Reconstruye los contenedores y limpia imágenes viejas.
5. Comprueba que el proxy responde 200.

Los despliegues a `main` **se encolan, no se cancelan**: un rsync cortado a la
mitad dejaría el servidor con medio repositorio.

Se puede lanzar a mano desde la pestaña **Actions** → *CI/CD a Plesk (Docker)* →
*Run workflow*. En las ramas y en los pull requests corre solo la parte de
pruebas, sin tocar el servidor.

---

## Mantenimiento

**Ver qué pasa:**

```bash
cd /var/www/vhosts/misiva.com.ec/GENERADOR
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f --tail=100
```

**Copia de seguridad.** Todo el trabajo está en `data/`; lo demás se reconstruye
desde el repositorio:

```bash
tar czf respaldo-$(date +%F).tgz -C /var/www/vhosts/misiva.com.ec/GENERADOR data
```

**Espacio.** Con los cuatro contenedores y un PSD de 150 MB en vuelo hacen falta
unos **4 GB de RAM**. La imagen del backend ocupa unos **2 GB** desde que el OCR
y SAM van dentro (antes 0,9 GB), y durante un despliegue conviven la vieja y la
nueva: el flujo comprueba que haya **6 GB libres** antes de reconstruir y aborta
si no los hay, dejando el sitio en pie con la versión anterior. Si aborta:

```bash
docker system df                      # ver qué ocupa
docker system prune -af --volumes     # OJO: borra imágenes sin usar de TODOS los proyectos
```

El disco lo come `data/`: cada proyecto guarda el arte aplanado, los recortes de
cada capa y cada variante generada.

**Añadir personas.** Una línea por persona dentro del bloque `basic_auth` del
`Caddyfile`, cada una con su hash.

---

## Si algo no responde

| Síntoma | Casi siempre es |
| --- | --- |
| El flujo aborta en *Verificar build-context* | La carpeta del Paso 2 no existe, o falta el `.env` del Paso 3 |
| Pide contraseña y la buena no entra | Los `$` del hash sin duplicar en el `.env` (solo aplica si se ha vuelto a activar el `basic_auth`) |
| Plesk rechaza las directivas con «duplicate location "/"» | Hay que usar `location ~ ^/`, no `location /` |
| La página carga y se cuelga al primer clic | Faltan `Upgrade`/`Connection` en las directivas de nginx |
| «413» al subir un PSD | Falta `client_max_body_size 0` (y el campo *Maximum allowed HTTP request body size* de esa misma pantalla) |
| «supera el límite de N MB» al subir un PSD | Es el backend: el tope vive en `docker-compose.yml` (`MAX_UPLOAD_MB`), no en el `.env`. Para un pliego muy grande, mejor déjalo en `data/ingest`, que no pasa por el navegador ni por ese tope |
| El despliegue aborta en *Comprobar que hay disco* | El servidor no tiene 6 GB libres. Mire la salida del paso: trae `df -h` y `docker system df` |
| El despliegue aborta en *Comprobar que el OCR y SAM quedaron activos* | La imagen se construyó sin ellos, o el `.env` del servidor los apaga. `docker compose logs backend` y `curl -s localhost:8014/api/health` |
| Plesk rechaza las directivas con «directive is duplicate» | `client_max_body_size` fuera del `location`: Plesk ya la pone en el vhost |
| La tanda muere a los 60 s | Falta `proxy_read_timeout 600s` |
| `cv-proxy` en crash-loop | El `security_opt` comentado del Paso 4 |
| El deploy falla con «error in libcrypto» | `DEPLOY_SSH_KEY` mal pegado: súbelo con `gh secret set` desde el archivo |
| «could not find an available, non-overlapping IPv4 address pool» | El pool de Docker está lleno (unos 31 huecos y este servidor tiene 39 redes). Ya está resuelto con la subred fija de `docker-compose.prod.yml`; si esa chocara con algo, `CV_SUBNET=10.x.y.0/24` en el `.env`. **No** reinicie el demonio: reiniciaría todos los proyectos del servidor |
