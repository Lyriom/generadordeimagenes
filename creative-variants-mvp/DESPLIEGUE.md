# Subirlo al servidor (Plesk + Docker)

Mismo esquema que mkpone y futbolecuador: el código se sincroniza por rsync a un
build-context del servidor, allí se reconstruye con `docker compose`, y Plesk
hace de proxy del subdominio al puerto del contenedor.

Dos diferencias con esos, y las dos importan:

- **Cuatro contenedores, no uno**: interfaz, API, worker y Redis. Solo el proxy
  publica puerto (el **8014**); los otros tres quedan en la red interna de Docker.
- **Hay datos que no se pueden borrar**: `data/` guarda los proyectos, los
  recortes y las artes generadas. El rsync la excluye; sin esa línea, cada
  despliegue borraría el trabajo del equipo.

---

## Paso 1 · Crear el subdominio en Plesk

Websites & Domains → **Add Subdomain**. Por ejemplo `generador.misiva.com.ec`.

Plesk crea la carpeta `httpdocs`. **No se sirven archivos desde ahí**: el sitio
lo sirve el contenedor y Plesk solo reenvía.

## Paso 2 · Preparar el build-context

Por SSH, en el servidor:

```bash
mkdir -p /var/www/vhosts/misiva.com.ec/httpdocs/GENERADOR
cd /var/www/vhosts/misiva.com.ec/httpdocs/GENERADOR
```

Esa ruta es la que hay que poner en `DEPLOY_PATH`, en el flujo de GitHub
(`.github/workflows/deploy.yml`, línea marcada con una flecha). Si prefieres otra,
cámbiala ahí y usa la misma en todos los pasos.

La primera vez conviene traer el código a mano para poder arrancarlo y
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
MAGNIFIC_API_KEY=...          # la de Magnific
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
excluye: se queda en el servidor pase lo que pase.

## Paso 4 · Levantarlo a mano una vez

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

La primera construcción tarda unos minutos. Comprobar que hay una sola puerta:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
curl -o /dev/null -w '%{http_code}\n' http://localhost:8014/                 # 401
curl -o /dev/null -w '%{http_code}\n' -u equipo:la-clave http://localhost:8014/  # 200
curl -m 3 http://localhost:8000/health                                        # sin respuesta
```

Si el proxy entra en crash-loop nada más arrancar, es el kernel viejo del host
—lo mismo que le pasó a mkpone con `nginx:alpine`—: descomenta el bloque
`security_opt` del servicio `proxy` en `docker-compose.prod.yml`.

## Paso 5 · Que Plesk mande el subdominio al 8014

En el subdominio → **Apache & nginx Settings**:

1. Desmarca **Proxy mode** (que nginx sirva directo, sin pasar por Apache).
2. En **Additional nginx directives**, pega:

```nginx
client_max_body_size 300M;

location / {
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

- **`Upgrade` y `Connection`**: Streamlit habla por websocket. Sin ellas la
  página carga y se queda colgada al primer clic.
- **`client_max_body_size 300M`**: los PSD llegan a 200 MB y el límite por
  defecto los corta.
- **`proxy_read_timeout 600s`**: una tanda de generación tarda minutos.

Después, el certificado: SSL/TLS Certificates → **Let's Encrypt**.

## Paso 6 · Los secretos de GitHub

En el repositorio → Settings → Secrets and variables → **Actions**. Los mismos
nombres que en mkpone:

| Secreto | Qué es |
| --- | --- |
| `DEPLOY_SSH_HOST` | la IP o el host del servidor |
| `DEPLOY_SSH_USER` | el usuario SSH |
| `DEPLOY_SSH_PORT` | el puerto SSH |
| `DEPLOY_SSH_KEY` | la clave privada, completa |

Si no tienes una llave dedicada, se genera en el servidor:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/deploy_generador -N ''
cat ~/.ssh/deploy_generador.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/deploy_generador          # esto es lo que va en DEPLOY_SSH_KEY
```

## Paso 7 · A partir de aquí, solo `git push`

Cada push a `main`:

1. Construye las imágenes y corre las 159 pruebas. **Si algo falla, no despliega.**
2. Sincroniza el código, respetando `.env` y `data/`.
3. Reconstruye los contenedores y limpia imágenes viejas.
4. Comprueba que el proxy responde 401 (es decir: está pidiendo contraseña).

Se puede lanzar a mano desde la pestaña **Actions** → *CI/CD a Plesk (Docker)* →
*Run workflow*.

---

## Mantenimiento

**Ver qué pasa:**

```bash
cd /var/www/vhosts/misiva.com.ec/httpdocs/GENERADOR
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f --tail=100
```

**Copia de seguridad.** Todo el trabajo está en `data/`; lo demás se reconstruye
desde el repositorio:

```bash
tar czf respaldo-$(date +%F).tgz -C /var/www/vhosts/misiva.com.ec/httpdocs/GENERADOR data
```

**Espacio.** Con los cuatro contenedores y un PSD de 150 MB en vuelo hacen falta
unos **4 GB de RAM**. El disco lo come `data/`: cada proyecto guarda el arte
aplanado, los recortes de cada capa y cada variante generada.

**Añadir personas.** Una línea por persona dentro del bloque `basic_auth` del
`Caddyfile`, cada una con su hash.
