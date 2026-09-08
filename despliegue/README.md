# Despliegue en Ubuntu

## Antes de empezar

**Los datos no están en el repositorio.** El `.gitignore` excluye los PDF
cargados, la base de datos y cualquier `.xlsx`, porque son facturas, egresos y
órdenes reales con NIT, cuentas bancarias y valores de proveedores. La
plantilla de formato hay que copiarla al servidor por aparte.

## 1. Sistema

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx git
# RapidOCR y OpenCV necesitan estas dos:
sudo apt install -y libgl1 libglib2.0-0
```

## 2. Usuario y carpetas

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin checklist
sudo mkdir -p /opt/checklist /var/lib/checklist/{datos,subidas}
sudo chown -R checklist:checklist /var/lib/checklist
```

## 3. Código

```bash
sudo -u checklist git clone git@github.com:mantilla16/checklist.git /opt/checklist
cd /opt/checklist
sudo -u checklist python3 -m venv .venv
sudo -u checklist .venv/bin/pip install -r requirements.txt
```

La primera instalación descarga los modelos de OCR (unos 15 MB) la primera vez
que se usa el RADIAN.

## 4. Plantilla de formato

El libro se genera copiando el diseño de una plantilla. Hay que subirla:

```bash
scp "Revisión Pagos a Proveedores.xlsx" servidor:/tmp/plantilla.xlsx
sudo mv /tmp/plantilla.xlsx /var/lib/checklist/plantilla.xlsx
sudo chown checklist:checklist /var/lib/checklist/plantilla.xlsx
```

Sin ese archivo la aplicación funciona, pero el botón *Generar Excel* avisa que
no hay plantilla.

## 5. Servicio

```bash
sudo cp despliegue/checklist.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now checklist
sudo systemctl status checklist
journalctl -u checklist -f      # para ver los registros
```

## 6. Nginx

```bash
sudo cp despliegue/nginx-checklist.conf /etc/nginx/sites-available/checklist
sudo ln -s /etc/nginx/sites-available/checklist /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d checklist.ejemplo.com
```

Ajustar `server_name` en el archivo antes de recargar.

## 7. Actualizar

```bash
cd /opt/checklist
sudo -u checklist git pull
sudo -u checklist .venv/bin/pip install -r requirements.txt
sudo systemctl restart checklist
```

Los datos viven en `/var/lib/checklist`, así que una actualización no los toca.

## Respaldo

La base es un solo archivo:

```bash
sudo -u checklist sqlite3 /var/lib/checklist/datos/validacion.db \
  ".backup '/var/lib/checklist/respaldo-$(date +%F).db'"
```

`.backup` respalda en caliente sin detener el servicio. Los PDF están en
`/var/lib/checklist/subidas`, nombrados por su hash.

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
tiene 1 o 2 núcleos, bajar `--workers` a 2.
