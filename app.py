from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import os, mysql.connector
from flask import g
from collections import defaultdict
from mysql.connector import Error, DatabaseError, DataError

PALABRAS_PROHIBIDAS = ["tonto", "idiota", "feo", "malo", "estupido", "pendejo", "imbecil", "puto", "puta", "mierda"]

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "supersecretkey")

DB_CONFIG = dict(
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    database=os.getenv("DB_NAME")
)

def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


def execute(query, params=()):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

# Helpers
def fetch_one(query, params=()):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(query, params)
        return cur.fetchone()
    finally:
        if conn:
            conn.close()


def fetch_all(query, params=()):
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(query, params)
        return cur.fetchall()
    finally:
        conn.close()


def get_field(form, *keys):
    for k in keys:
        v = form.get(k)
        if v:
            return v
    return None

def get_user_limits(user_id):
    """Obtiene estado premium y contadores (asegura valores numéricos)."""
    return fetch_one("""
        SELECT 
            COALESCE(premium, FALSE) AS premium,
            COALESCE(contador_publicaciones, 0) AS contador_publicaciones,
            COALESCE(contador_respuestas, 0) AS contador_respuestas
        FROM usuarios
        WHERE id_usuario = %s
    """, (user_id,))

# --- NUEVO: obtener límite global de interacciones para usuarios free
def get_global_limit(default=5):
    try:
        row = fetch_one("SELECT valor FROM settings WHERE clave = %s", ('limit_free_interactions',))
        if row and row.get('valor') and str(row.get('valor')).isdigit():
            return int(row.get('valor'))
    except Exception:
        app.logger.debug("No se pudo leer limit_free_interactions, usando default=%s", default)
    return default


# Rutas
@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == "POST":
        email = get_field(request.form, "email", "correo", "correo_usuario")
        password = get_field(request.form, "password", "contrasena", "contrasena_usuario", "contraseña", "contraseña_usuario")

        email = (email or "").strip().lower()
        password = password or ""

        if not email or not password:
            flash("Completa correo y contraseña.", "warning")
            return render_template("login.html")

        # Nota: usamos backticks para la columna con ñ
        q = "SELECT id_usuario, nombre_usuario, correo_usuario, `contraseña_usuario` AS password_hash, COALESCE(premium, FALSE) AS premium FROM usuarios WHERE correo_usuario = %s"
        user = fetch_one(q, (email,))

        if user and check_password_hash(user.get("password_hash", ""), password):
            session['user_id'] = user['id_usuario']
            session['username'] = user['nombre_usuario']
            # guardar estado premium en la sesión para que la UI lo lea inmediatamente
            session['is_premium'] = bool(user.get('premium'))
            try:
                execute("UPDATE usuarios SET ultimo_acceso = NOW() WHERE id_usuario = %s", (user['id_usuario'],))
            except Exception:
                pass
            flash(f"Bienvenido {user['nombre_usuario']}!", "success")
            return redirect(url_for('dashboard'))
        else:
            flash("Credenciales incorrectas.", "danger")

    return render_template("login.html")

@app.route("/registro", methods=["GET", "POST"])
def registro():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    if request.method == "POST":
        username = get_field(request.form, "username", "nombre", "nombre_usuario") or ""
        email = get_field(request.form, "email", "correo", "correo_usuario") or ""
        password = get_field(request.form, "password", "contrasena", "contrasena_usuario", "contraseña", "contraseña_usuario") or ""

        username = username.strip()
        email = email.strip().lower()

        if not username or not email or not password:
            flash("Completa todos los campos.", "warning")
            return render_template("registro.html")

        try:
            exists = fetch_one("SELECT id_usuario FROM usuarios WHERE correo_usuario = %s", (email,))
            if exists:
                flash("El correo ya está registrado.", "warning")
                return render_template("registro.html")

            pw_hash = generate_password_hash(password)
            # Inserción (columna con ñ tal como la tienes)
            execute(
                "INSERT INTO usuarios (nombre_usuario, correo_usuario, `contraseña_usuario`) VALUES (%s, %s, %s)",
                (username, email, pw_hash)
            )
            flash("Registro exitoso. Ya puedes iniciar sesión.", "success")
            return redirect(url_for('login'))
        except DataError as de:
            # Data too long (por ejemplo hash > VARCHAR)
            flash("Error: datos demasiado largos (contraseña). Contacta al admin.", "danger")
            print("DataError (registro):", de)
            return render_template("registro.html")
        except Error as e:
            flash("Error en la base de datos.", "danger")
            print("DB error (registro):", e)
            return render_template("registro.html")

    return render_template("registro.html")


# DASHBOARD
@app.route("/dashboard")
def dashboard():
    if 'user_id' not in session:
        flash("Debes iniciar sesión.", "info")
        return redirect(url_for('login'))

    carreras = fetch_all("SELECT * FROM carreras")
    # parámetros de filtro
    carrera_filtro = request.args.get("carrera", "todas")
    q = request.args.get("q", "").strip()

    # base de la consulta
    sql = """
        SELECT p.*, u.nombre_usuario, c.nombre_carrera
        FROM publicaciones p
        JOIN usuarios u ON u.id_usuario = p.id_usuario
        LEFT JOIN carreras c ON c.id_carrera = p.id_carrera
    """
    where = []
    params = []

    # filtro de carrera
    if carrera_filtro and carrera_filtro != "todas":
        where.append("c.id_carrera = %s")
        params.append(carrera_filtro)

    # filtro de búsqueda (titulo, contenido, autor o carrera)
    if q:
        like = f"%{q}%"
        where.append("(p.titulo LIKE %s OR p.contenido LIKE %s OR u.nombre_usuario LIKE %s OR c.nombre_carrera LIKE %s)")
        params.extend([like, like, like, like])

    # construir WHERE si hace falta
    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY p.fecha_creacion DESC"

    publicaciones = fetch_all(sql, tuple(params))

    # Cargar comentarios
    comentarios = fetch_all("""
        SELECT r.*, u.nombre_usuario 
        FROM respuestas r
        JOIN usuarios u ON u.id_usuario = r.id_usuario
        ORDER BY r.fecha_creacion ASC
    """)

    com_by_pub = defaultdict(list)
    for c in comentarios:
        com_by_pub[c['id_publicacion']].append(c)

    def build_tree(com_list):
        nodes = {c['id_respuesta']: dict(c, children=[]) for c in com_list}
        roots = []
        for c in com_list:
            parent = c.get('id_respuesta_padre')
            if parent and parent in nodes:
                nodes[parent]['children'].append(nodes[c['id_respuesta']])
            else:
                roots.append(nodes[c['id_respuesta']])
        return roots

    comentarios_tree = {pub_id: build_tree(lst) for pub_id, lst in com_by_pub.items()}

    # Añadido: Obtener límites del usuario y estado premium
    user_limits = get_user_limits(session['user_id']) or {'premium': session.get('is_premium', False), 'contador_publicaciones': 0, 'contador_respuestas': 0}
    is_premium = bool(user_limits.get('premium'))
    interactions_left = None if is_premium else max(0, 5 - ((user_limits.get('contador_publicaciones') or 0) + (user_limits.get('contador_respuestas') or 0)))

    # debug
    print("DEBUG: q=", repr(q), "carrera_filtro=", carrera_filtro, "params=", params, "is_premium=", is_premium, "interactions_left=", interactions_left)

    return render_template("dashboard.html",
                           username=session.get("username"),
                           publicaciones=publicaciones,
                           carreras=carreras,
                           carrera_filtro=carrera_filtro,
                           comentarios=comentarios,
                           comentarios_tree=comentarios_tree,
                           q=q,
                           is_premium=is_premium,
                           interactions_left=interactions_left)


@app.route("/actualizar_premium", methods=["GET", "POST"])
def actualizar_premium():
    if 'user_id' not in session:
        flash("Debes iniciar sesión.", "info")
        return redirect(url_for('login'))

    if request.method == "POST":
        try:
            execute("UPDATE usuarios SET premium = TRUE WHERE id_usuario = %s", (session['user_id'],))
            # actualizar sesión para que el cambio se refleje sin relogin
            session['is_premium'] = True
            flash("¡Felicidades! Tu cuenta ha sido actualizada a Premium.", "success")
            return redirect(url_for('dashboard'))
        except Error as e:
            print("DB error (actualizar_premium):", e)
            flash("Error al actualizar a premium. Intenta nuevamente.", "danger")
            return redirect(url_for('dashboard'))

    return render_template('actualizar_premium.html', username=session.get('username'))

# LOGOUT
@app.route("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada.", "info")
    return redirect(url_for('login'))

# CREAR PUBLICACIÓN
@app.route("/crear_publicacion", methods=["POST"])
def crear_publicacion():
    if 'user_id' not in session:
        flash("Debes iniciar sesión.", "warning")
        return redirect(url_for('login'))

    # comprobar límites antes de crear
    user_limits = get_user_limits(session['user_id']) or {'premium': False, 'contador_publicaciones':0, 'contador_respuestas':0}
    if not bool(user_limits.get('premium')):
        limit = get_global_limit()
        used = int(user_limits.get('contador_publicaciones',0)) + int(user_limits.get('contador_respuestas',0))
        if used >= limit:
            flash(f"Has alcanzado el límite de interacciones ({limit}). Actualiza a premium para más.", "warning")
            return redirect(url_for("dashboard"))

    contenido = request.form.get("postText", "")
    imagen = None
    titulo = request.form.get("postTitle", "Sin título")
    id_carrera = request.form.get("postCarrera")

    if "postImages" in request.files:
        file = request.files["postImages"]
        if file.filename:
            upload_folder = "static/uploads"
            os.makedirs(upload_folder, exist_ok=True)
            imagen_filename = file.filename
            imagen_path = os.path.join(upload_folder, imagen_filename)
            file.save(imagen_path)
            imagen = f"uploads/{imagen_filename}"  # Solo la parte relativa a /static/

    execute(
        "INSERT INTO publicaciones (id_usuario, titulo, contenido, imagen, id_carrera) VALUES (%s, %s, %s, %s, %s)",
        (session["user_id"], titulo, contenido, imagen, id_carrera)
    )

    # intentar incrementar contador de publicaciones (si existe la columna)
    try:
        execute("UPDATE usuarios SET contador_publicaciones = COALESCE(contador_publicaciones,0) + 1 WHERE id_usuario = %s", (session["user_id"],))
    except Exception:
        app.logger.debug("No se pudo actualizar contador_publicaciones para usuario %s", session["user_id"])

    flash("Publicación creada.", "success")
    return redirect(url_for("dashboard"))

@app.route("/pagos_universidad")
def pagos_universidad():
    if 'user_id' not in session:
        flash("Debes iniciar sesión.", "info")
        return redirect(url_for('login'))
    return render_template("pagos_universidad.html", username=session.get("username"))

@app.route("/comentar/<int:id_publicacion>", methods=["POST"])
def comentar(id_publicacion):
    if 'user_id' not in session:
        flash("Debes iniciar sesión.", "warning")
        return redirect(url_for('login'))

    # comprobar límites antes de comentar
    user_limits = get_user_limits(session['user_id']) or {'premium': False, 'contador_publicaciones':0, 'contador_respuestas':0}
    if not bool(user_limits.get('premium')):
        limit = get_global_limit()
        used = int(user_limits.get('contador_publicaciones',0)) + int(user_limits.get('contador_respuestas',0))
        if used >= limit:
            flash(f"Has alcanzado el límite de interacciones ({limit}). Actualiza a premium para más.", "warning")
            return redirect(url_for("dashboard"))

    contenido = request.form.get("comentario", "").strip()
    if not contenido:
        flash("El comentario no puede estar vacío.", "warning")
        return redirect(url_for('dashboard'))

    # censura opcional
    for palabra in PALABRAS_PROHIBIDAS:
        contenido = contenido.replace(palabra, "***")

    parent_id = request.form.get("parent_id")
    if parent_id:
        execute(
            "INSERT INTO respuestas (id_publicacion, id_usuario, contenido, id_respuesta_padre) VALUES (%s, %s, %s, %s)",
            (id_publicacion, session["user_id"], contenido, parent_id)
        )
    else:
        execute(
            "INSERT INTO respuestas (id_publicacion, id_usuario, contenido) VALUES (%s, %s, %s)",
            (id_publicacion, session["user_id"], contenido)
        )

    # intentar incrementar contador de respuestas (si existe la columna)
    try:
        execute("UPDATE usuarios SET contador_respuestas = COALESCE(contador_respuestas,0) + 1 WHERE id_usuario = %s", (session["user_id"],))
    except Exception:
        app.logger.debug("No se pudo actualizar contador_respuestas para usuario %s", session["user_id"])

    flash("Comentario publicado.", "success")
    return redirect(url_for('dashboard'))

@app.route("/extraordinarios")
def extraordinarios():
    return render_template("extraordiarios.html")

@app.route("/help")
def help():
    if 'user_id' not in session:
        flash("Debes iniciar sesión.", "info")
        return redirect(url_for('login'))
    return render_template('help.html', username=session.get('username'))


# Ruta para crear reporte (permite enviar aun sin sesión; si hay sesión se guarda id_usuario)
@app.route('/crear_reporte', methods=['GET','POST'])
def crear_reporte():
    if request.method == 'POST':
        id_publicacion = request.form.get('id_publicacion') or None
        motivo = (request.form.get('motivo') or "").strip()
        descripcion = (request.form.get('descripcion') or "").strip()

        if not motivo or not descripcion:
            flash('Completa motivo y descripción del reporte.', 'warning')
            return render_template('crear_reporte.html', id_publicacion=id_publicacion)

        try:
            id_pub_val = int(id_publicacion) if id_publicacion else None
        except Exception:
            id_pub_val = None

        user_id = session.get('user_id') if 'user_id' in session else None

        # Detectar columnas reales en la tabla para evitar "Unknown column"
        try:
            cols_info = fetch_all("SHOW COLUMNS FROM reportes_publicaciones")
            cols = [c['Field'] for c in cols_info] if cols_info else []
        except Exception:
            cols = []

        # Candidatos comunes
        id_pub_candidates = ['id_publicacion', 'id_pub', 'publicacion_id']
        id_user_candidates = ['id_usuario', 'usuario_id', 'id_user']
        motivo_candidates = ['motivo', 'razon', 'categoria']
        desc_candidates = ['descripcion', 'detalle', 'mensaje', 'comentario', 'detalle_reporte', 'contenido']
        fecha_candidates = ['fecha_reporte', 'fecha', 'created_at', 'fecha_creacion']

        def pick(cands):
            for x in cands:
                if x in cols:
                    return x
            return None

        id_pub_col = pick(id_pub_candidates)
        id_user_col = pick(id_user_candidates)
        motivo_col = pick(motivo_candidates) or 'motivo'
        desc_col = pick(desc_candidates)
        fecha_col = pick(fecha_candidates)

        fields, placeholders, params = [], [], []

        if id_pub_col:
            fields.append(id_pub_col); placeholders.append('%s'); params.append(id_pub_val)
        if id_user_col:
            fields.append(id_user_col); placeholders.append('%s'); params.append(user_id)
        if motivo_col:
            fields.append(motivo_col); placeholders.append('%s'); params.append(motivo)
        if desc_col:
            fields.append(desc_col); placeholders.append('%s'); params.append(descripcion)
        if fecha_col:
            fields.append(fecha_col); placeholders.append('NOW()')

        if not fields:
            app.logger.error("Crear reporte: no se detectaron columnas válidas en reportes_publicaciones")
            flash('Error interno al crear reporte. Contacta soporte.', 'danger')
            return render_template('crear_reporte.html', id_publicacion=id_publicacion)

        sql = f"INSERT INTO reportes_publicaciones ({', '.join(fields)}) VALUES ({', '.join(placeholders)})"
        try:
            last = execute(sql, tuple(params))
            app.logger.info("Reporte insertado id=%s (user_id=%s, id_publicacion=%s) SQL=%s params=%s", last, user_id, id_pub_val, sql, params)
            flash('Reporte enviado. Gracias por tu colaboración.', 'success')
            return redirect(url_for('help'))   # <--- cambia destino a /help
        except Exception as e:
            app.logger.exception("Error al insertar reporte (dinámico): %s -- SQL=%s params=%s", e, sql, params)
            flash('Error al enviar el reporte. Intenta nuevamente.', 'danger')
            return render_template('crear_reporte.html', id_publicacion=id_publicacion)

    id_publicacion = request.args.get('id_publicacion')
    return render_template('crear_reporte.html', id_publicacion=id_publicacion)

@app.route("/perfil")
def perfil():
    if "user_id" not in session:
        return redirect(url_for("login"))

    # traer toda la fila y luego normalizar nombres (evita errores si la columna se llama distinto)
    usuario = fetch_one("SELECT * FROM usuarios WHERE id_usuario = %s", (session["user_id"],))

    display_name = usuario.get("nombre_usuario") if usuario else session.get("username", "")
    email = usuario.get("correo_usuario") or usuario.get("email") or ""
    role = usuario.get("rol") or usuario.get("role") or ""
    location = usuario.get("ubicacion") or usuario.get("location") or ""
    avatar = usuario.get("avatar") if usuario and "avatar" in usuario else None

    return render_template("perfil.html",
                           usuario=usuario,
                           display_name=display_name,
                           email=email,
                           role=role,
                           location=location,
                           avatar=avatar)

@app.route("/editar_perfil", methods=["GET", "POST"])
def editar_perfil():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        correo = request.form.get("correo", "").strip()
        rol = request.form.get("rol", "").strip()
        ubicacion = request.form.get("ubicacion", "").strip()
        telefono = request.form.get("telefono", "").strip()

        try:
            execute(
                "UPDATE usuarios SET nombre_usuario=%s, correo_usuario=%s, rol=%s, ubicacion=%s, telefono=%s WHERE id_usuario=%s",
                (nombre, correo, rol, ubicacion, telefono, session["user_id"])
            )
            session["username"] = nombre or session.get("username")
            flash("Perfil actualizado.", "success")
        except Exception as e:
            print("Error actualizar perfil:", e)
            flash("Error al actualizar el perfil.", "danger")
        return redirect(url_for("perfil"))

    usuario = fetch_one("SELECT * FROM usuarios WHERE id_usuario = %s", (session["user_id"],))
    return render_template("editar_perfil.html", usuario=usuario)


@app.route("/configuracion", methods=["GET", "POST"])
def configuracion():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        nombre_visible = request.form.get("nombre_visible", "").strip()
        correo = request.form.get("correo", "").strip()
        telefono = request.form.get("telefono", "").strip()

        try:
            execute(
                "UPDATE usuarios SET nombre_usuario=%s, correo_usuario=%s, telefono=%s WHERE id_usuario=%s",
                (nombre_visible, correo, telefono, session["user_id"])
            )
            session["username"] = nombre_visible or session.get("username")
            flash("Configuración guardada.", "success")
        except Exception as e:
            print("Error guardar configuración:", e)
            flash("Error al guardar configuración.", "danger")
        return redirect(url_for("perfil"))

    usuario = fetch_one("SELECT * FROM usuarios WHERE id_usuario = %s", (session["user_id"],))
    return render_template("configuracion.html", usuario=usuario)

@app.route('/')
def index():
    # Obtener publicaciones activas (no eliminadas)
    publicaciones = fetch_all("""
        SELECT p.id_publicacion, p.titulo, p.entradilla, p.contenido, p.imagen, 
               p.fecha_creacion, u.nombre_usuario, c.nombre_carrera
        FROM publicaciones p
        LEFT JOIN usuarios u ON p.id_usuario = u.id_usuario
        LEFT JOIN carreras c ON p.id_carrera = c.id_carrera
        WHERE p.estado = 'activo' OR p.estado IS NULL
        ORDER BY p.fecha_creacion DESC
        LIMIT 50
    """)
    
    # Obtener carrusel
    carousel_items = fetch_all("""
        SELECT * FROM carousel_items 
        WHERE activo=1 
        ORDER BY orden ASC, fecha_creacion DESC
    """)
    
    # Obtener noticias
    noticias = fetch_all("""
        SELECT id_publicacion, titulo, entradilla, imagen, fecha_publicacion
        FROM noticias
        WHERE estado = 'publicado' OR estado IS NULL
        ORDER BY COALESCE(fecha_publicacion, fecha_creacion) DESC
        LIMIT 6
    """)
    
    username = session.get('username') if 'user_id' in session else None
    carreras = fetch_all("SELECT id_carrera, nombre_carrera FROM carreras ORDER BY nombre_carrera")
    
    return render_template('dashboard.html', 
                           publicaciones=publicaciones,
                           carousel_items=carousel_items,
                           noticias=noticias,
                           username=username,
                           carreras=carreras,
                           carrera_filtro=request.args.get('carrera', 'todas'),
                           q=request.args.get('q', ''))

# --- RUN ---
if __name__ == "__main__":
    try:
        conn = get_connection()
        conn.close()
        print("Conexión a DB OK")
    except Exception as e:
        print("ERROR: No se pudo conectar a la DB:", e)

    print("App lista. Gunicorn se encargará de levantarla en Render.")


