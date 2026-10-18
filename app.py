import io
import logging
import os
import re
import secrets
import threading
import time
from urllib.parse import unquote, urlparse

import pymysql
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from pymysql.err import IntegrityError
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:
    register_heif_opener = None

MAX_IMAGE_BYTES = 2 * 1024 * 1024
FULL_EDGE = 1280
THUMB_EDGE = 480
PAGE_SIZE = 12
OPTIMIZED_NAME = "foto.jpg"
Image.MAX_IMAGE_PIXELS = 40_000_000
CEDULA_RE = re.compile(r"^\d{5,12}$")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "12341234")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "consulta-popular-dev-key")
app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_BYTES + (1024 * 1024)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("consulta")
if app.config["SECRET_KEY"] == "consulta-popular-dev-key":
    log.warning("SECRET_KEY sigue en el valor de desarrollo. Cámbiala antes de publicar.")
if register_heif_opener is None:
    log.warning("Las fotos HEIC se convertirán en el teléfono. El servidor no pudo activar ese formato.")

_schema_lock = threading.Lock()
_schema_ready = False

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS registros (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    cedula VARCHAR(12) NOT NULL,
    imagen MEDIUMBLOB NOT NULL,
    miniatura MEDIUMBLOB NULL,
    imagen_nombre VARCHAR(255) NOT NULL,
    imagen_tipo VARCHAR(64) NOT NULL,
    creado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_registros_cedula (cedula)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def db_settings():
    url = (os.environ.get("DATABASE_URL") or os.environ.get("MYSQL_URL") or "").strip()
    url = url.strip('"').strip("'")
    if url:
        if "://" in url and not url.startswith("mysql://"):
            scheme, rest = url.split("://", 1)
            if scheme.startswith("mysql"):
                url = "mysql://" + rest
        parsed = urlparse(url)
        try:
            port = parsed.port or 3306
        except ValueError as exc:
            raise pymysql.err.OperationalError(
                2003,
                "DATABASE_URL no es válida. Si la contraseña tiene @, # u otros signos, "
                "codifícalos o usa MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD y MYSQL_DATABASE por separado.",
            ) from exc
        return {
            "host": parsed.hostname or "127.0.0.1",
            "port": port,
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "database": unquote(parsed.path.lstrip("/")),
        }

    raw_port = (os.environ.get("MYSQL_PORT") or "3306").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise pymysql.err.OperationalError(
            2003, "MYSQL_PORT debe ser un número, por ejemplo 3306."
        ) from exc
    return {
        "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port": port,
        "user": os.environ.get("MYSQL_USER", "consulta"),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DATABASE", "consulta_popular"),
    }


def connect():
    settings = db_settings()
    if not settings["user"] or not settings["database"]:
        raise pymysql.err.OperationalError(
            1045, "Faltan MYSQL_USER y MYSQL_DATABASE (o DATABASE_URL)."
        )
    return pymysql.connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        database=settings["database"],
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=5,
        read_timeout=30,
        write_timeout=60,
        binary_prefix=True,
    )


def ensure_schema(connection):
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with connection.cursor() as cursor:
            cursor.execute(CREATE_TABLE_SQL)
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'registros'
                  AND COLUMN_NAME = 'miniatura'
                """
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute("ALTER TABLE registros ADD COLUMN miniatura MEDIUMBLOB NULL")
        connection.commit()
        _schema_ready = True


def normalize_cedula(value):
    return re.sub(r"[\s.\-]", "", (value or "").strip())


def detect_mime(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"heif", b"mif1", b"msf1"}:
            return "image/heic"
    return None


class ImageProcessError(Exception):
    pass


def _to_rgb(image):
    image = ImageOps.exif_transpose(image)
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _jpeg_bytes(image, quality):
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=False, progressive=True)
    return buffer.getvalue()


def prepare_image(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            already_light = (
                image.format == "JPEG"
                and max(image.size) <= FULL_EDGE
                and len(data) <= 700_000
            )
            rgb = _to_rgb(image)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageProcessError("No se pudo leer la foto.") from exc
    if not already_light:
        rgb.thumbnail((FULL_EDGE, FULL_EDGE), Image.Resampling.BILINEAR)
    full = data if already_light else _jpeg_bytes(rgb, 68)
    thumb = rgb.copy()
    thumb.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.BILINEAR)
    return full, _jpeg_bytes(thumb, 55)


def image_response(data, mime):
    response = Response(data, mimetype=mime or "image/jpeg")
    response.headers["Cache-Control"] = "private, max-age=86400"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def csrf_ok():
    sent = request.form.get("csrf_token", "")
    saved = session.get("_csrf", "")
    if not saved or not sent:
        return False
    return secrets.compare_digest(sent, saved)


def describe_exception(exc):
    text = f"{type(exc).__name__}: {exc}".strip()
    text = re.sub(r"(?i)(password|pwd|passwd)=([^\s&]+)", r"\1=***", text)
    return text[:800]


def render_page(cedula="", error=None, detail=None, success=False, already=False, status=200):
    if request.headers.get("X-Requested-With") == "fetch":
        response = jsonify(
            ok=bool(success or already),
            already=bool(already),
            error=error,
            detail=detail,
        )
        response.status_code = status
        return response
    return render_template(
        "index.html",
        csrf_token=csrf_token(),
        cedula=cedula,
        error=error,
        detail=detail,
        success=success,
        already=already,
    ), status


def report_failure(cedula, message, exc, status):
    detail = describe_exception(exc)
    app.logger.error("CAUSA DEL ERROR AL ENVIAR: %s", detail)
    try:
        settings = db_settings()
    except Exception as config_exc:
        app.logger.error("Configuración MySQL ilegible: %s", describe_exception(config_exc))
    else:
        app.logger.error(
            "MySQL host=%s port=%s user=%s database=%s",
            settings.get("host"),
            settings.get("port"),
            settings.get("user"),
            settings.get("database"),
        )
    app.logger.exception("Traza completa del envío")
    return render_page(cedula=cedula, error=message, detail=detail, status=status)


def save_registro(cedula, image_bytes, thumb_bytes, filename, mime):
    connection = connect()
    try:
        ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO registros (cedula, imagen, miniatura, imagen_nombre, imagen_tipo)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (cedula, image_bytes, thumb_bytes, filename, mime),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def admin_autenticado():
    return session.get("admin_ok") is True


def requested_page():
    raw = request.values.get("page", "1")
    try:
        return max(1, min(int(raw), 100000))
    except (TypeError, ValueError):
        return 1


def list_registros(page, per_page=PAGE_SIZE):
    page = max(1, min(page, 100000))
    connection = connect()
    try:
        ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*), SUM(miniatura IS NULL) FROM registros")
            total, pendientes = cursor.fetchone()
            total = int(total or 0)
            pendientes = int(pendientes or 0)
            pages = max(1, (total + per_page - 1) // per_page)
            if page > pages:
                page = pages
            offset = (page - 1) * per_page
            cursor.execute(
                """
                SELECT id, cedula, creado_en, (miniatura IS NOT NULL) AS tiene_mini
                FROM registros
                ORDER BY creado_en DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (per_page, offset),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    registros = []
    for row in rows:
        creado = row[2]
        if hasattr(creado, "strftime"):
            creado = creado.strftime("%d/%m/%Y %H:%M")
        registros.append(
            {
                "id": row[0],
                "cedula": row[1],
                "creado_en": creado or "",
                "tiene_mini": bool(row[3]),
            }
        )
    return {
        "registros": registros,
        "total": total,
        "pendientes": pendientes,
        "page": page,
        "pages": pages,
    }


def _blob_bytes(value):
    if isinstance(value, str):
        return value.encode("latin1", "surrogateescape")
    return value


def fetch_imagen(registro_id):
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT imagen, imagen_tipo FROM registros WHERE id = %s",
                (registro_id,),
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    if not row or row[0] is None:
        return None
    return _blob_bytes(row[0]), row[1]


def fetch_miniatura(registro_id):
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT miniatura FROM registros WHERE id = %s",
                (registro_id,),
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    if not row or row[0] is None:
        return None
    return _blob_bytes(row[0])


def shrink_one():
    connection = connect()
    try:
        ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK('consulta_shrink', 1)")
            if cursor.fetchone()[0] != 1:
                return None
            try:
                cursor.execute(
                    """
                    SELECT id, imagen
                    FROM registros
                    WHERE miniatura IS NULL
                       OR imagen_tipo <> 'image/jpeg'
                       OR imagen_nombre <> %s
                    ORDER BY (miniatura IS NULL) DESC, id
                    LIMIT 1
                    """,
                    (OPTIMIZED_NAME,),
                )
                row = cursor.fetchone()
                if not row:
                    return False
                registro_id, imagen = row
                imagen = _blob_bytes(imagen)
                try:
                    full, thumb = prepare_image(imagen)
                except ImageProcessError:
                    log.error("No se pudo aligerar el registro %s", registro_id)
                    thumb = _jpeg_bytes(Image.new("RGB", (8, 8), (255, 255, 255)), 60)
                    cursor.execute(
                        """
                        UPDATE registros
                        SET miniatura = %s, imagen_nombre = %s, imagen_tipo = 'image/jpeg'
                        WHERE id = %s
                        """,
                        (thumb, OPTIMIZED_NAME, registro_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE registros
                        SET imagen = %s, miniatura = %s, imagen_nombre = %s, imagen_tipo = 'image/jpeg'
                        WHERE id = %s
                        """,
                        (full, thumb, OPTIMIZED_NAME, registro_id),
                    )
                    log.info("Foto %s reducida de %s a %s bytes", registro_id, len(imagen), len(full))
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.execute("SELECT RELEASE_LOCK('consulta_shrink')")
                connection.commit()
    finally:
        connection.close()


def shrink_loop():
    time.sleep(8)
    while True:
        try:
            result = shrink_one()
        except Exception:
            log.exception("No se pudo aligerar fotos guardadas")
            time.sleep(20)
            continue
        if result is None:
            time.sleep(3)
            continue
        if result is False:
            log.info("Las fotos guardadas ya están aligeradas")
            return
        time.sleep(0.4)


def delete_registro(registro_id):
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM registros WHERE id = %s", (registro_id,))
            deleted = cursor.rowcount
        connection.commit()
        return deleted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@app.route("/admin", methods=["GET", "POST"])
def admin():
    error = None
    if request.method == "POST" and request.form.get("action") == "entrar":
        if not csrf_ok():
            error = "El formulario expiró. Actualiza la página."
        elif secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["admin_ok"] = True
            return redirect(url_for("admin"))
        else:
            error = "Contraseña incorrecta."

    if not admin_autenticado():
        status = 401 if error == "Contraseña incorrecta." else 200
        return render_template(
            "admin_login.html",
            csrf_token=csrf_token(),
            error=error,
        ), status

    try:
        listed = list_registros(requested_page())
    except pymysql.MySQLError as exc:
        app.logger.error("No se pudo listar registros: %s", describe_exception(exc))
        return render_template(
            "admin.html",
            csrf_token=csrf_token(),
            registros=[],
            total=0,
            pendientes=0,
            page=1,
            pages=1,
            error="No se pudo leer la base de datos.",
            detail=describe_exception(exc),
            aviso=None,
        ), 503

    return render_template(
        "admin.html",
        csrf_token=csrf_token(),
        error=None,
        detail=None,
        aviso=request.args.get("aviso"),
        **listed,
    )


@app.post("/admin/borrar/<int:registro_id>")
def admin_borrar(registro_id):
    if not admin_autenticado():
        abort(401)
    if not csrf_ok():
        return redirect(url_for("admin"))
    try:
        deleted = delete_registro(registro_id)
    except pymysql.MySQLError as exc:
        app.logger.error("No se pudo borrar el registro: %s", describe_exception(exc))
        return render_template(
            "admin.html",
            csrf_token=csrf_token(),
            registros=[],
            error="No se pudo borrar el registro.",
            detail=describe_exception(exc),
            aviso=None,
        ), 503
    return redirect(url_for("admin", aviso="borrado" if deleted else "ausente", page=requested_page()))


@app.post("/admin/salir")
def admin_salir():
    if csrf_ok():
        session.pop("admin_ok", None)
    return redirect(url_for("admin"))


@app.get("/admin/foto/<int:registro_id>")
def admin_foto(registro_id):
    if not admin_autenticado():
        abort(401)
    row = fetch_imagen(registro_id)
    if not row:
        abort(404)
    imagen, mime = row
    return image_response(imagen, mime)


@app.get("/admin/mini/<int:registro_id>")
def admin_mini(registro_id):
    if not admin_autenticado():
        abort(401)
    imagen = fetch_miniatura(registro_id)
    if not imagen:
        abort(404)
    return image_response(imagen, "image/jpeg")


@app.after_request
def cache_static(response):
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return render_page()


@app.post("/")
def submit():
    if not csrf_ok():
        return render_page(
            error="El formulario expiró. Actualiza la página y envíalo de nuevo.",
        )

    cedula = normalize_cedula(request.form.get("cedula", ""))
    if not CEDULA_RE.fullmatch(cedula):
        return render_page(
            cedula=cedula,
            error="Escribe una cédula válida: solo números, entre 5 y 12 dígitos.",
        )

    upload = request.files.get("imagen")
    if upload is None or not upload.filename:
        return render_page(cedula=cedula, error="Toma una foto con la cámara.")

    data = upload.read()
    if not data:
        return render_page(cedula=cedula, error="La imagen está vacía.")
    if len(data) > MAX_IMAGE_BYTES:
        return render_page(
            cedula=cedula,
            error="La foto es muy pesada. Tómala de nuevo.",
        )

    mime = detect_mime(data)
    if mime is None:
        return render_page(
            cedula=cedula,
            error="La imagen debe ser JPG, PNG, WEBP, GIF o HEIC.",
        )

    try:
        image_bytes, thumb_bytes = prepare_image(data)
    except ImageProcessError:
        return render_page(
            cedula=cedula,
            error="No se pudo leer la foto. Tómala de nuevo.",
        )
    log.info("Foto lista para guardar: %s bytes -> %s bytes", len(data), len(image_bytes))

    try:
        save_registro(cedula, image_bytes, thumb_bytes, OPTIMIZED_NAME, "image/jpeg")
    except IntegrityError:
        return render_page(success=True, already=True)
    except pymysql.MySQLError as exc:
        return report_failure(
            cedula,
            "No se pudo guardar en la base de datos.",
            exc,
            503,
        )
    except Exception as exc:
        return report_failure(
            cedula,
            "Ocurrió un error al enviar el formulario.",
            exc,
            500,
        )

    return render_page(success=True)


@app.errorhandler(RequestEntityTooLarge)
def too_large(_error):
    return render_page(error="La foto es muy pesada. Tómala de nuevo.")


@app.errorhandler(Exception)
def unexpected(exc):
    if isinstance(exc, HTTPException):
        return exc
    detail = describe_exception(exc)
    app.logger.error("CAUSA DEL ERROR AL ENVIAR: %s", detail)
    app.logger.exception("Traza completa del envío")
    try:
        return render_page(
            error="Ocurrió un error al enviar el formulario.",
            detail=detail,
            status=500,
        )
    except Exception:
        app.logger.exception("No se pudo mostrar la página de error")
        return detail, 500


if os.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn"):
    threading.Thread(target=shrink_loop, name="aligerar-fotos", daemon=True).start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8005")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
