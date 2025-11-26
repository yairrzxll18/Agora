from flask import Flask, render_template, request, redirect, url_for, flash, session, g
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
import mysql.connector, os

# Config DB
DB_CONFIG = dict(
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    database=os.getenv("DB_NAME")
)

def get_connection():
    return mysql.connector.connect(**DB_CONFIG)

def fetch_one(query, params=()):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute(query, params)
        return cur.fetchone()
    finally:
        conn.close()

def fetch_all(query, params=()):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute(query, params)
        return cur.fetchall()
    finally:
        conn.close()

def execute(query, params=()):
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute(query, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

def create_app():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.join(base_dir, "templates")
    static_dir = os.path.join(base_dir, "static")
    app = Flask(__name__, template_folder=templates_dir, static_folder=static_dir)
    app.secret_key = os.environ.get("FLASK_SECRET", "dev_de_prueba")
    return app

app = create_app()

def admin_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'admin_user_id' not in session:
            flash("Debes iniciar sesión como administrador.", "info")
            return redirect(url_for('login'))
        row = fetch_one("SELECT COALESCE(administrador, FALSE) AS administrador FROM usuarios WHERE id_usuario = %s", (session['admin_user_id'],))
        if not row or not row.get('administrador'):
            flash("Acceso denegado.", "danger")
            session.pop('admin_user_id', None)
            session.pop('admin_username', None)
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

@app.route("/login", methods=["GET", "POST"])
def login():
    if 'admin_user_id' in session:
        return redirect(url_for('index'))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or not password:
            flash("Completa correo y contraseña.", "warning")
            return render_template("admin/login.html")
        q = "SELECT id_usuario, nombre_usuario, `contraseña_usuario` AS password_hash, COALESCE(administrador, FALSE) AS administrador FROM usuarios WHERE correo_usuario = %s"
        user = fetch_one(q, (email,))
        if user and user.get("administrador") and check_password_hash(user.get("password_hash",""), password):
            session['admin_user_id'] = user['id_usuario']
            session['admin_username'] = user['nombre_usuario']
            flash("Acceso panel admin OK.", "success")
            return redirect(url_for('index'))
        flash("Credenciales inválidas o usuario no es administrador.", "danger")
    return render_template("admin/login.html")

@app.route("/logout")
def logout():
    session.pop('admin_user_id', None)
    session.pop('admin_username', None)
    flash("Sesión admin cerrada.", "info")
    return redirect(url_for("login"))

@app.route("/")
@admin_login_required
def index():
    total_noticias = fetch_one("SELECT COUNT(*) AS c FROM noticias")['c'] or 0
    try:
        total_reportes = fetch_one("SELECT COUNT(*) AS c FROM reportes_publicaciones WHERE IFNULL(resuelto,0)=0")['c'] or 0
    except Exception:
        total_reportes = fetch_one("SELECT COUNT(*) AS c FROM reportes_publicaciones")['c'] or 0
    total_pagos = fetch_one("SELECT COUNT(*) AS c FROM pagos")['c'] or 0
    total_usuarios = fetch_one("SELECT COUNT(*) AS c FROM usuarios")['c'] or 0
    return render_template("admin/index.html",
                           total_noticias=total_noticias,
                           total_reportes=total_reportes,
                           total_pagos=total_pagos,
                           total_usuarios=total_usuarios)

# Noticias
@app.route("/noticias")
@admin_login_required
def noticias():
    noticias = fetch_all("SELECT n.*, u.nombre_usuario FROM noticias n LEFT JOIN usuarios u ON n.id_admin = u.id_usuario ORDER BY fecha_creacion DESC")
    return render_template("admin/noticias.html", noticias=noticias)

@app.route("/noticias/crear", methods=["GET", "POST"])
@admin_login_required
def noticias_crear():
    if request.method == "POST":
        titulo = (request.form.get("titulo") or "").strip()
        contenido = (request.form.get("contenido") or "").strip()
        imagen = None
        if "imagen" in request.files:
            f = request.files["imagen"]
            if f and f.filename:
                upload_folder = os.path.join(app.root_path, "static", "uploads")
                os.makedirs(upload_folder, exist_ok=True)
                name = secure_filename(f.filename)
                f.save(os.path.join(upload_folder, name))
                imagen = f"uploads/{name}"
        execute("INSERT INTO noticias (titulo, contenido, imagen, id_admin) VALUES (%s,%s,%s,%s)",
                (titulo, contenido, imagen, session['admin_user_id']))
        flash("Noticia creada.", "success")
        return redirect(url_for("noticias"))
    return render_template("admin/crear_noticia.html")

@app.route("/noticias/eliminar/<int:id_noticia>", methods=["POST"])
@admin_login_required
def noticias_eliminar(id_noticia):
    execute("DELETE FROM noticias WHERE id_noticia = %s", (id_noticia,))
    flash("Noticia eliminada.", "success")
    return redirect(url_for("noticias"))

# Reportes
@app.route("/reportes")
@admin_login_required
def reportes():
    reportes = fetch_all("""
        SELECT rp.*, p.titulo AS titulo_publicacion, u.nombre_usuario AS reportador
        FROM reportes_publicaciones rp
        LEFT JOIN publicaciones p ON p.id_publicacion = rp.id_publicacion
        LEFT JOIN usuarios u ON u.id_usuario = rp.id_usuario
        ORDER BY rp.fecha_reporte DESC
    """)
    return render_template("admin/reportes.html", reportes=reportes)

@app.route("/reportes/accion", methods=["POST"])
@admin_login_required
def reportes_accion():
    id_reporte = request.form.get("id_reporte")
    accion = request.form.get("accion")
    if not id_reporte or not accion:
        flash("Datos incompletos.", "warning")
        return redirect(url_for("reportes"))

    try:
        # Obtener fila del reporte
        reporte = fetch_one("SELECT * FROM reportes_publicaciones WHERE id_reporte = %s", (id_reporte,))
        id_publicacion = reporte.get("id_publicacion") if reporte else None

        # Eliminar publicación (y dependencias) y marcar reporte como resuelto
        if accion == "eliminar_publicacion" or accion == "eliminar":
            if not id_publicacion:
                flash("No hay publicación asociada a este reporte.", "warning")
                return redirect(url_for("reportes"))

            # Comprobar existencia de la publicación
            pub = fetch_one("SELECT id_publicacion FROM publicaciones WHERE id_publicacion = %s", (id_publicacion,))
            if not pub:
                # marcar reporte resuelto igualmente
                try:
                    execute("UPDATE reportes_publicaciones SET resuelto = TRUE, accion = %s, id_moderador = %s WHERE id_reporte = %s",
                            (accion, session.get('admin_user_id'), id_reporte))
                except Exception:
                    execute("UPDATE reportes_publicaciones SET accion = %s WHERE id_reporte = %s", (accion, id_reporte))
                flash("La publicación ya no existe; reporte marcado como resuelto.", "info")
                return redirect(url_for("reportes"))

            # Eliminar en orden correcto: reportes → respuestas → publicación
            try:
                execute("DELETE FROM reportes_publicaciones WHERE id_publicacion = %s", (id_publicacion,))
            except Exception:
                app.logger.debug("No se pudo eliminar reportes de la publicación %s", id_publicacion)

            try:
                execute("DELETE FROM respuestas WHERE id_publicacion = %s", (id_publicacion,))
            except Exception:
                app.logger.debug("No se pudo eliminar respuestas de la publicación %s", id_publicacion)

            # Eliminar la publicación físicamente
            try:
                execute("DELETE FROM publicaciones WHERE id_publicacion = %s", (id_publicacion,))
            except Exception as e:
                app.logger.exception("Error al eliminar publicación %s: %s", id_publicacion, e)
                flash("Error al eliminar la publicación. Revisa logs.", "danger")
                return redirect(url_for("reportes"))

            try:
                execute("INSERT INTO moderaciones (id_reporte, id_moderador, accion, fecha) VALUES (%s, %s, %s, NOW())",
                        (id_reporte, session.get('admin_user_id'), accion))
            except Exception:
                app.logger.debug("No se pudo insertar registro en moderaciones para reporte %s", id_reporte)

            flash("Publicación eliminada y reporte actualizado.", "success")
            return redirect(url_for("reportes"))

        # Revisar la publicación y marcar el reporte como ignorado/resuelto
        if accion == "revisar_ignorar" or accion == "revisar":
            try:
                execute("UPDATE reportes_publicaciones SET resuelto = TRUE, accion = %s, id_moderador = %s WHERE id_reporte = %s",
                        (accion, session.get('admin_user_id'), id_reporte))
            except Exception:
                execute("UPDATE reportes_publicaciones SET accion = %s WHERE id_reporte = %s", (accion, id_reporte))

            try:
                execute("INSERT INTO moderaciones (id_reporte, id_moderador, accion, fecha) VALUES (%s, %s, %s, NOW())",
                        (id_reporte, session.get('admin_user_id'), accion))
            except Exception:
                app.logger.debug("No se pudo insertar registro en moderaciones para reporte %s", id_reporte)

            flash("Reporte marcado como revisado (ignorado).", "info")
            if id_publicacion:
                return redirect(f"/publicacion/{id_publicacion}")
            return redirect(url_for("reportes"))

        # Acción desconocida
        flash("Acción no reconocida.", "warning")
    except Exception as e:
        app.logger.exception("Error al aplicar acción sobre reporte %s: %s", id_reporte, e)
        flash("Error al aplicar la acción.", "danger")

    return redirect(url_for("reportes"))

# Pagos
@app.route("/pagos")
@admin_login_required
def pagos():
    pagos = fetch_all("SELECT * FROM pagos ORDER BY fecha_pago ASC")
    return render_template("admin/pagos.html", pagos=pagos)

@app.route("/pagos/crear", methods=["GET", "POST"])
@admin_login_required
def pagos_crear():
    if request.method == "POST":
        titulo = (request.form.get("titulo") or "").strip()
        descripcion = (request.form.get("descripcion") or "").strip()
        fecha_pago = request.form.get("fecha_pago")
        execute("INSERT INTO pagos (titulo, descripcion, fecha_pago) VALUES (%s,%s,%s)", (titulo, descripcion, fecha_pago))
        flash("Fecha de pago creada.", "success")
        return redirect(url_for("pagos"))
    return render_template("admin/crear_pago.html")

# Limites
@app.route("/limites", methods=["GET", "POST"])
@admin_login_required
def limites():
    if request.method == "POST":
        nuevo = request.form.get("limit_free_interactions")
        if nuevo and nuevo.isdigit():
            try:
                execute("INSERT INTO settings (clave, valor) VALUES (%s, %s)", ('limit_free_interactions', str(int(nuevo))))
            except Exception:
                execute("UPDATE settings SET valor = %s WHERE clave = %s", (str(int(nuevo)), 'limit_free_interactions'))
            flash("Límite actualizado.", "success")
        else:
            flash("Valor inválido.", "warning")
        return redirect(url_for("limites"))

    # obtener límite
    limit_row = fetch_one("SELECT valor FROM settings WHERE clave = %s", ('limit_free_interactions',))
    limit = int(limit_row['valor']) if limit_row and limit_row.get('valor') and str(limit_row.get('valor')).isdigit() else 5

    # detectar si la tabla usuarios ya tiene columnas contador_publicaciones/contador_respuestas
    try:
        cols_info = fetch_all("SHOW COLUMNS FROM usuarios")
        cols = {c['Field'] for c in cols_info} if cols_info else set()
    except Exception:
        cols = set()

    usuarios = []
    if 'contador_publicaciones' in cols and 'contador_respuestas' in cols:
        # si existen columnas, traerlas directamente
        usuarios = fetch_all("SELECT id_usuario, nombre_usuario, contador_publicaciones, contador_respuestas, premium FROM usuarios ORDER BY nombre_usuario")
        # normalizar null a 0
        for u in usuarios:
            u['contador_publicaciones'] = int(u.get('contador_publicaciones') or 0)
            u['contador_respuestas'] = int(u.get('contador_respuestas') or 0)
    else:
        # calcular los contadores a partir de publicaciones/respuestas
        usuarios_basics = fetch_all("SELECT id_usuario, nombre_usuario, premium FROM usuarios ORDER BY nombre_usuario")
        for u in usuarios_basics:
            uid = u['id_usuario']
            try:
                cp = fetch_one("SELECT COUNT(*) AS c FROM publicaciones WHERE id_usuario = %s", (uid,)) or {'c': 0}
                cr = fetch_one("SELECT COUNT(*) AS c FROM respuestas WHERE id_usuario = %s", (uid,)) or {'c': 0}
                u['contador_publicaciones'] = int(cp.get('c') or 0)
                u['contador_respuestas'] = int(cr.get('c') or 0)
            except Exception:
                u['contador_publicaciones'] = 0
                u['contador_respuestas'] = 0
        usuarios = usuarios_basics

    return render_template("admin/limites.html", limit=limit, usuarios=usuarios)

# Registro de admin (opcional)
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        nombre = (request.form.get("nombre") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not nombre or not email or not password:
            flash("Completa todos los campos.", "warning")
            return render_template("admin/register.html")
        password_hash = generate_password_hash(password)
        try:
            execute("INSERT INTO usuarios (nombre_usuario, correo_usuario, `contraseña_usuario`, administrador) VALUES (%s,%s,%s,%s)",
                    (nombre, email, password_hash, True))
            flash("Usuario admin creado. Inicia sesión.", "success")
            return redirect(url_for("login"))
        except Exception:
            flash("No se pudo crear usuario.", "danger")
    return render_template("admin/register.html")

if __name__ == "__main__":
    print("Ejecutando admin standalone en http://127.0.0.1:5000/ (login: /login)")
    app.run(debug=True, host="127.0.0.1", port=5000)