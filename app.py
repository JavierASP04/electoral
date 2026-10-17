import logging
import os
import re
import secrets
import threading
from urllib.parse import unquote, urlparse

import pymysql
from flask import Flask, render_template, request, session
from pymysql.err import IntegrityError
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename

MAX_IMAGE_BYTES = 8 * 1024 * 1024
CEDULA_RE = re.compile(r"^\d{5,12}$")
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "consulta-popular-dev-key")
app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_BYTES + (1024 * 1024)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("consulta")
if app.config["SECRET_KEY"] == "consulta-popular-dev-key":
    log.warning("SECRET_KEY sigue en el valor de desarrollo. Cámbiala antes de publicar.")

_schema_lock = threading.Lock()
_schema_ready = False

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS registros (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    cedula VARCHAR(12) NOT NULL,
    imagen MEDIUMBLOB NOT NULL,
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


def image_filename(original_name, mime):
    cleaned = secure_filename(original_name or "")[:255]
    if cleaned:
        return cleaned
    return "imagen" + MIME_EXTENSIONS[mime]


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


def render_page(cedula="", error=None, detail=None, success=False, status=200):
    return render_template(
        "index.html",
        csrf_token=csrf_token(),
        cedula=cedula,
        error=error,
        detail=detail,
        success=success,
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


def save_registro(cedula, image_bytes, filename, mime):
    connection = connect()
    try:
        ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO registros (cedula, imagen, imagen_nombre, imagen_tipo)
                VALUES (%s, %s, %s, %s)
                """,
                (cedula, image_bytes, filename, mime),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
            status=400,
        )

    cedula = normalize_cedula(request.form.get("cedula", ""))
    if not CEDULA_RE.fullmatch(cedula):
        return render_page(
            cedula=cedula,
            error="Escribe una cédula válida: solo números, entre 5 y 12 dígitos.",
            status=400,
        )

    upload = request.files.get("imagen")
    if upload is None or not upload.filename:
        return render_page(cedula=cedula, error="Toma una foto con la cámara.", status=400)

    data = upload.read()
    if not data:
        return render_page(cedula=cedula, error="La imagen está vacía.", status=400)
    if len(data) > MAX_IMAGE_BYTES:
        return render_page(
            cedula=cedula,
            error="La imagen supera el máximo de 8 MB.",
            status=400,
        )

    mime = detect_mime(data)
    if mime is None:
        return render_page(
            cedula=cedula,
            error="La imagen debe ser JPG, PNG, WEBP, GIF o HEIC.",
            status=400,
        )

    try:
        save_registro(cedula, data, image_filename(upload.filename, mime), mime)
    except IntegrityError:
        return render_page(
            cedula=cedula,
            error="Esta cédula ya fue registrada.",
            status=409,
        )
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
    return render_page(error="La imagen supera el máximo de 8 MB.", status=413)


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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8005")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
