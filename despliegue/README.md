# Despliegue en Ubuntu

Instalación en el directorio del usuario (`~/checklist`), con los datos en una
carpeta aparte (`~/checklist-datos`) para que un `git pull` no los toque.

**Los datos no están en el repositorio.** El `.gitignore` excluye los PDF
cargados, la base de datos y cualquier `.xlsx`, porque son facturas, egresos y
órdenes reales con NIT, cuentas bancarias y valores de proveedores. La
plantilla de formato hay que subirla al servidor por aparte.

## 1. Librerías del sistema

RapidOCR y OpenCV necesitan estas dos:

```bash
sudo apt update
sudo apt install -y python3-venv libgl1 libglib2.0-0
```

## 2. Código e instalación

```bash
cd ~
git clone https://github.com/mantilla16/checklist.git
cd checklist
bash despliegue/instalar.sh
```

El script crea el entorno virtual, instala las dependencias, crea
`~/checklist-datos/{datos,subidas}` y comprueba que la aplicación carga.

## 3. Plantilla de formato

El libro se genera copiando el diseño de una plantilla. Desde tu máquina:

```bash
scp "Revisión Pagos a Proveedores.xlsx" rbbaq@servidor:~/checklist-datos/plantilla.xlsx
```

Sin ese archivo la aplicación funciona, pero *Generar Excel* avisa que no hay
plantilla.

## 4. Probarlo antes del servicio

```bash
cd ~/checklist
CHECKLIST_DATOS=~/checklist-datos/datos \
CHECKLIST_SUBIDAS=~/checklist-datos/subidas \
CHECKLIST_PLANTILLA=~/checklist-datos/plantilla.xlsx \
  .venv/bin/python app/server.py
```

Queda en `http://127.0.0.1:5000`. Para verlo desde tu máquina sin exponer el
puerto, un túnel SSH:

```bash
ssh -L 5000:127.0.0.1:5000 rbbaq@servidor
```

y abrir <http://127.0.0.1:5000> en el navegador local. Servida así, en la
raíz, también funciona: las rutas de la API son relativas.

## 5. Servicio

El archivo viene con `User=rbbaq` y las rutas de `/home/rbbaq`. Si tu usuario o
tus rutas son otros, edítalo antes de copiarlo.

```bash
# Comprobar que el puerto 8030 esté libre (8000 y 8020 ya están ocupados)
ss -ltnp | grep :8030 || echo "libre"

sudo cp ~/checklist/despliegue/checklist.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now checklist
systemctl status checklist
journalctl -u checklist -f      # registros en vivo
```

Si el 8030 está ocupado, cambia el `--bind 127.0.0.1:8030` del servicio y el
`proxy_pass` de nginx por otro puerto.

**Nota**: el servicio *no* usa `ProtectHome`, porque con la aplicación en
`/home` eso impediría que arrancara.

## 6. Nginx: servirla en /checklist/

El servidor ya tiene un sitio con analitica-puc en la raíz y asistente-rb en
/asistente/, así que **no se crea un sitio nuevo**: se agregan dos `location` al
bloque `server` existente. Están en
[`nginx-checklist.conf`](nginx-checklist.conf) listos para pegar:

```bash
sudo nano /etc/nginx/sites-available/<tu-sitio>   # pegar los dos location
sudo nginx -t && sudo systemctl reload nginx
```

Queda en `http://<tu-dominio>/checklist/`.

Dos detalles que importan:

- **La barra final.** El `location = /checklist` redirige a `/checklist/`,
  porque la interfaz resuelve la API con rutas relativas; sin la barra las
  peticiones saldrían de la subruta y caerían en el `/api/` de analitica-puc.
- **`proxy_pass` con barra final** (`http://127.0.0.1:8030/`) quita el prefijo
  antes de pasar la petición, así que la aplicación recibe `/` y `/api/...`,
  que es lo que espera.

## 7. Actualizar

```bash
cd ~/checklist
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart checklist
```

Los datos viven en `~/checklist-datos`, así que una actualización no los toca.

## Respaldo

La base es un solo archivo:

```bash
sqlite3 ~/checklist-datos/datos/validacion.db \
  ".backup '$HOME/checklist-datos/respaldo-$(date +%F).db'"
```

`.backup` respalda en caliente sin detener el servicio. Los PDF están en
`~/checklist-datos/subidas`, nombrados por su hash.

## Dos advertencias

**No hay control de acceso.** La aplicación no tiene usuarios ni contraseña:
quien llegue a la URL ve y modifica todo. Antes de exponerla a internet hay que
ponerle autenticación — lo más rápido es HTTP básico en nginx:

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd contabilidad
```

y dentro del `location /`:

```nginx
auth_basic "Validación de pagos";
auth_basic_user_file /etc/nginx/.htpasswd;
```

**El OCR consume CPU.** Cada RADIAN son ~20 segundos de un núcleo. Con 3
trabajadores de gunicorn se atienden 3 documentos a la vez; si el servidor
tiene 1 o 2 núcleos, bajar `--workers` a 2:

```bash
nproc      # cuántos núcleos hay
```
