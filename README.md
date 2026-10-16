# Consulta Popular Octubre 2026

Formulario con dos campos, cédula e imagen. Los dos se guardan en MySQL. La imagen queda dentro de la base, en la tabla `registros`, así que no hace falta un disco extra para archivos.

## Coolify

1. Conecta este repositorio en Coolify: **New Resource → Application**.
2. Rama `main`. Build pack: **Dockerfile**.
3. La app escucha en la variable `PORT`. Si Coolify no la define, usa el puerto **8005**. En Coolify, **Ports Exposes** debe ser `8005`. Health check: `/health`.
4. En el mismo proyecto crea **Database → MySQL** y una base, por ejemplo `consulta_popular`.
5. En la aplicación agrega estas variables. El host es el nombre interno que Coolify muestra en la base, no `localhost`.

```
SECRET_KEY=una-clave-larga-y-aleatoria
MYSQL_HOST=el-host-interno-de-coolify
MYSQL_PORT=3306
MYSQL_DATABASE=consulta_popular
MYSQL_USER=el-usuario
MYSQL_PASSWORD=la-contraseña
```

También vale una sola variable, con la URL que muestra Coolify:

```
DATABASE_URL=mysql://usuario:contraseña@host:3306/consulta_popular
```

6. Deploy. La tabla se crea sola en el primer envío.

Cada cédula solo puede registrarse una vez. Si se envía de nuevo, el formulario avisa que ya existe.

## Probar en local con Docker

```bash
docker compose up --build
```

Abre `http://localhost:8005`. Las claves de ese compose son solo para pruebas. Cámbialas antes de exponer el servicio.
