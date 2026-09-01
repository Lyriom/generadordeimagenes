# Poner el generador en la web

Se sirve por un solo puerto, el **8014**, detrás de un proxy con contraseña.
Backend, worker y Redis no publican nada: al exterior solo se llega por el proxy.

Esto no es una precaución teórica. Cada generación gasta créditos de Magnific de
la cuenta configurada en el servidor: sin contraseña, cualquiera que dé con la
URL los gasta.

## 1. Preparar el servidor

Cualquier máquina Linux con Docker sirve. Como referencia, con los cuatro
contenedores y un PSD de 150 MB en vuelo hace falta **4 GB de RAM y 20 GB de
disco**; el disco lo come `data/`, que guarda los proyectos y las variantes.

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker "$USER"          # cerrar y volver a abrir sesión

sudo mkdir -p /srv/generadordeimagenes && sudo chown "$USER" /srv/generadordeimagenes
git clone https://github.com/Lyriom/generadordeimagenes.git /srv/generadordeimagenes
cd /srv/generadordeimagenes/creative-variants-mvp
```

## 2. Configurar las claves

El `.env` vive **solo en el servidor**. No está en el repositorio y no debe
estarlo: lleva la clave de Magnific.

```bash
cp .env.example .env
nano .env        # pegar MAGNIFIC_API_KEY y, si se usa, OPENAI_API_KEY
```

Y la contraseña de acceso:

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'la-clave-que-quiera'
```

En el `.env`, **duplicando cada `$`**:

```env
CV_USER=equipo
CV_PASSWORD_HASH=$$2a$$14$$abc...
```

> Los `$` van dobles porque Docker Compose interpreta `$algo` como una variable
> y se comería medio hash. Con `$$` lo pasa tal cual.

## 3. Levantarlo

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Comprobar que solo hay una puerta:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
curl -o /dev/null -w '%{http_code}\n' http://localhost:8014/          # 401
curl -o /dev/null -w '%{http_code}\n' -u equipo:la-clave http://localhost:8014/   # 200
curl -m 3 http://localhost:8000/health                                # sin respuesta
```

## 4. Abrirlo al exterior

El proxy escucha en HTTP. Para exponerlo a internet hay que ponerle HTTPS
delante: sin cifrado, la contraseña de acceso viaja en claro.

- **Con dominio propio**: apuntar el DNS al servidor y dejar que Caddy pida el
  certificado. Cambiar `:8014` por el dominio en el `Caddyfile`, publicar los
  puertos 80 y 443 en vez del 8014, y quitar `auto_https off`.
- **Sin dominio**: un túnel (Cloudflare Tunnel o Tailscale) hacia
  `localhost:8014`. No hay que abrir ningún puerto en el cortafuegos y el
  certificado lo pone el túnel.
- **Solo red interna**: dejarlo como está y llegar por la IP del servidor.

## 5. Despliegue automático

`.github/workflows/ci.yml` corre la suite en cada empujón.
`.github/workflows/deploy.yml` actualiza el servidor cuando las pruebas pasan
en `main`.

Hay que dar de alta cuatro secretos en **Settings → Secrets and variables →
Actions**:

| Secreto | Qué es |
| --- | --- |
| `DEPLOY_HOST` | dirección del servidor |
| `DEPLOY_USER` | usuario con acceso a Docker |
| `DEPLOY_KEY` | clave SSH privada de ese usuario |
| `DEPLOY_PATH` | carpeta del repositorio en el servidor |

La clave se genera en el servidor y se autoriza a sí misma:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/deploy -N ''
cat ~/.ssh/deploy.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/deploy          # esto es lo que se pega en DEPLOY_KEY
```

## Copias de seguridad

Todo el trabajo vive en `creative-variants-mvp/data/`. Lo demás se reconstruye
desde el repositorio.

```bash
tar czf respaldo-$(date +%F).tgz -C /srv/generadordeimagenes/creative-variants-mvp data
```
