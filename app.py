from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_login import LoginManager, login_required, current_user, login_user, logout_user
from flask_migrate import Migrate
from models import db, Usuario, Categoria, Gasto, Ingreso, Empresa, Historial
from eva_utils import registrar_cambio, generar_analisis_completo
from datetime import datetime
from decimal import Decimal, InvalidOperation
from flask_mail import Mail, Message
from functools import wraps
import secrets
import string
import os
import sys
import io

# FORCE PRINT 
import sys
sys.stderr.write("🔍 INICIANDO APP - CARGANDO VARIABLES\n")
sys.stderr.flush()

# CONFIGURAR ENCODING UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# CARGAR VARIABLES DE ENTORNO
from dotenv import load_dotenv
import sys
sys.stderr.write("🔍 Intentando cargar .env...\n")
sys.stderr.flush()
load_dotenv()
sys.stderr.write(f"✓ MAIL_USERNAME: {os.environ.get('MAIL_USERNAME')}\n")
sys.stderr.flush()

# CREAR LA APP
app = Flask(__name__)
print("\n" + "="*60)
print("🚀 INICIANDO PYME - SISTEMA DE GESTIÓN FINANCIERA")
print("="*60)
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY no esta definida. Configurala como variable de entorno "
        "antes de arrancar la aplicacion."
    )
app.config['SECRET_KEY'] = SECRET_KEY
# BASE DE DATOS
# En produccion (Render) se define DATABASE_URL apuntando a Postgres.
# Sin esa variable, se cae al SQLite local para desarrollo.
DATABASE_URL = os.environ.get('DATABASE_URL')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL or 'sqlite:///pyme.db'


def _ofuscar_uri(uri):
    """Devuelve la URI de conexion con la password enmascarada, para loguear."""
    import re
    return re.sub(r'://([^:/@]+):([^@]+)@', r'://\1:****@', uri)


print(f"🗄️  BASE DE DATOS: {_ofuscar_uri(app.config['SQLALCHEMY_DATABASE_URI'])}")
print(f"🗄️  MOTOR: {'PostgreSQL' if DATABASE_URL else 'SQLite (fallback local)'}")

# CONFIGURACIÓN DE EMAIL
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME')

print(f"📧 MAIL_USERNAME: {app.config['MAIL_USERNAME']}")
print(f"🔐 MAIL_PASSWORD: {'*' * 20 if app.config['MAIL_PASSWORD'] else 'NO CONFIGURADO'}")
print("="*60 + "\n")

# INICIALIZAR MAIL Y DB
mail = Mail(app)
db.init_app(app)
migrate = Migrate(app, db)  # El esquema lo maneja Alembic, no db.create_all()

@app.route('/')
def index():
    return redirect(url_for('login'))

# FUNCIONES DE EMAIL
def generar_codigo():
    return ''.join(secrets.choice(string.digits) for _ in range(6))

import threading

def enviar_email(destinatario, asunto, cuerpo):
    """Envía email de forma asincrónica en un thread separado"""
    def _enviar():
        try:
            print(f"\n🔍 Intentando enviar email a: {destinatario}")
            print(f"📧 Servidor: {app.config['MAIL_SERVER']}")
            print(f"👤 Usuario: {app.config['MAIL_USERNAME']}")
            
            msg = Message(
                asunto,
                recipients=[destinatario],
                html=cuerpo,
                charset='utf-8'
            )
            mail.send(msg)
            print(f"✓ Email enviado exitosamente a {destinatario}\n")
        except Exception as e:
            print(f"✗ Error al enviar email: {e}\n")
    
    # Ejecutar en un thread separado para no bloquear
    thread = threading.Thread(target=_enviar)
    thread.daemon = True
    thread.start()
    return True

# LOGIN MANAGER
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return Usuario.query.get(int(user_id))

def require_role(roles):
    """Protege rutas por rol de usuario"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                flash('Debes iniciar sesión', 'danger')
                return redirect(url_for('login'))
            
            if current_user.rol not in roles:
                flash('No tienes permiso para acceder a esta página', 'danger')
                return redirect(url_for('dashboard'))
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# Hacemos que Usuario sea compatible con Flask-Login
Usuario.is_authenticated = True
Usuario.is_active = True
Usuario.is_anonymous = False

def get_id(self):
    return str(self.id)

Usuario.get_id = get_id


# ======================== AUTENTICACIÓN ========================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        usuario = Usuario.query.filter_by(email=email).first()
        
        if usuario and usuario.check_password(password) and usuario.activo:
            # Validar que el email esté verificado
            if not usuario.verificado:
                flash('Por favor verifica tu email antes de iniciar sesión.', 'warning')
                return redirect(url_for('login'))
            
            login_user(usuario)
            flash(f'¡Bienvenido {usuario.nombre}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Email o contraseña incorrectos', 'danger')
    
    return render_template('login.html')

@app.route('/cambiar-password', methods=['GET', 'POST'])
def cambiar_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        
        usuario = Usuario.query.filter_by(email=email).first()
        if not usuario:
            flash('Email no encontrado', 'danger')
            return redirect(url_for('cambiar_password'))
        
        # Generar código
        codigo = generar_codigo()
        usuario.codigo_reset = codigo
        db.session.commit()
        
        # Enviar email
        cuerpo_email = f"""
        <h2>Verifica tu email</h2>
        <p>Tu código de verificacion es:</p>
        <h1 style="color: #1e90ff; letter-spacing: 5px;">{codigo}</h1>
        <p>Este código expira en 10 minutos.</p>
        """
        
        print(f"🔥 ANTES DE ENVIAR EMAIL - email: {email}")
        if enviar_email(email, "Código de Restablecimiento - PYME", cuerpo_email):
            session['email_reset'] = email
            flash('Se envio un código a tu email.', 'success')
            return redirect(url_for('verificar_codigo_reset'))
        else:
            flash('Error al enviar email. Intenta de nuevo.', 'danger')
    
    return render_template('cambiar_password.html')

@app.route('/verificar-codigo-reset', methods=['GET', 'POST'])
def verificar_codigo_reset():
    if 'email_reset' not in session:
        return redirect(url_for('cambiar_password'))
    
    if request.method == 'POST':
        codigo = request.form.get('codigo', '').strip()
        usuario = Usuario.query.filter_by(email=session['email_reset']).first()
        
        if usuario and codigo == usuario.codigo_reset:
            session['user_reset_id'] = usuario.id
            session.pop('email_reset', None)
            flash('Código verificado. Ingresa tu nueva contraseña.', 'success')
            return redirect(url_for('nueva_password'))
        else:
            flash('Codigo incorrecto.', 'danger')
    
    return render_template('verificar_codigo_reset.html', email=session.get('email_reset'))

@app.route('/nueva-password', methods=['GET', 'POST'])
def nueva_password():
    if 'user_reset_id' not in session:
        return redirect(url_for('cambiar_password'))
    
    if request.method == 'POST':
        password_nueva = request.form.get('password_nueva', '').strip()
        password_confirma = request.form.get('password_confirma', '').strip()
        
        if not password_nueva or len(password_nueva) < 6:
            flash('La contraseña debe tener minimo 6 caracteres.', 'danger')
            return redirect(url_for('nueva_password'))
        
        if password_nueva != password_confirma:
            flash('Las contraseñas no coinciden.', 'danger')
            return redirect(url_for('nueva_password'))
        
        usuario = Usuario.query.get(session['user_reset_id'])
        usuario.set_password(password_nueva)
        usuario.codigo_reset = None
        db.session.commit()
        
        session.pop('user_reset_id', None)
        
        flash('Contraseña actualizada correctamente. Inicia sesión.', 'success')
        return redirect(url_for('login'))
    
    return render_template('nueva_password.html')

@app.route('/registro', methods=['GET', 'POST'])
def registro():
    if request.method == 'POST':
        nombre = request.form['nombre']
        email = request.form['email']
        password = request.form['password']
        
        if Usuario.query.filter_by(email=email).first():
            flash('El email ya esta registrado', 'error')
            return redirect(url_for('registro'))
        
        # Generar código de verificación
        codigo = generar_codigo()
        
        # Crear empresa
        empresa = Empresa(nombre=f"Empresa de {nombre}")
        db.session.add(empresa)
        db.session.flush()
        
        # Crear usuario sin verificar
        usuario = Usuario(nombre=nombre, email=email, empresa_id=empresa.id, verificado=False, codigo_verificacion=codigo)
        usuario.set_password(password)
        db.session.add(usuario)
        db.session.commit()
        
        # Enviar email
        cuerpo_email = f"""
        <h2>Verifica tu email</h2>
        <p>Tu codigo de verificacion es:</p>
        <h1 style="color: #1e90ff; letter-spacing: 5px;">{codigo}</h1>
        <p>Este codigo expira en 10 minutos.</p>
        """

        print(f"🔥 ANTES DE ENVIAR EMAIL - email: {email}")
        if enviar_email(email, "Codigo de Verificacion - PYME", cuerpo_email):
            session['user_id'] = usuario.id
            session['email_usuario'] = email
            flash('Se envio un codigo a tu email. Verifica tu cuenta.', 'success')
            return redirect(url_for('verificar_email'))
        else:
            db.session.delete(usuario)
            db.session.delete(empresa)
            db.session.commit()
            flash('Error al enviar email. Intenta de nuevo.', 'danger')
            return redirect(url_for('registro'))
    
    return render_template('registro.html')

@app.route('/verificar-email', methods=['GET', 'POST'])
def verificar_email():
    if 'user_id' not in session:
        return redirect(url_for('registro'))
    
    usuario = Usuario.query.get(session['user_id'])
    if not usuario:
        return redirect(url_for('registro'))
    
    if request.method == 'POST':
        codigo_ingresado = request.form.get('codigo', '').strip()
        
        if codigo_ingresado == usuario.codigo_verificacion:
            usuario.verificado = True
            usuario.codigo_verificacion = None
            db.session.commit()
            
            # Limpiar sesión temporal
            session.pop('user_id', None)
            session.pop('email_usuario', None)
            
            # Logear automáticamente al usuario
            login_user(usuario)
            
            flash('Email verificado correctamente. ¡Bienvenido!', 'success')
            return redirect(url_for('crear_empresa'))
        else:
            flash('Codigo incorrecto. Intenta de nuevo.', 'danger')
    
    return render_template('verificar_email.html', email=usuario.email)

@app.route('/crear-empresa', methods=['GET', 'POST'])
@login_required
def crear_empresa():
    usuario = current_user
    
    if request.method == 'POST':
        usuario.empresa.nombre = request.form['nombre']
        usuario.empresa.ruc = request.form.get('ruc')
        usuario.empresa.telefono = request.form.get('telefono')
        usuario.empresa.email = request.form.get('email')
        usuario.empresa.direccion = request.form.get('direccion')
        usuario.empresa.descripcion = request.form.get('descripcion')
        usuario.empresa.capital_invertido = float(request.form.get('capital_invertido', 0))
        
        db.session.commit()
        
        flash('¡Bienvenido! Tu empresa ha sido creada', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('crear_empresa.html', usuario=usuario)

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada', 'success')
    return redirect(url_for('login'))

# ======================== DASHBOARD ========================

@app.route('/dashboard')
@login_required
def dashboard():
    # FILTRAR POR USUARIO (SIN FILTRO DE FECHA)
    gastos = Gasto.query.filter_by(usuario_id=current_user.id).all()
    ingresos = Ingreso.query.filter_by(usuario_id=current_user.id).all()
    
    config = current_user.empresa
    
    # eva_utils opera con floats (tasa_impuestos/capital_invertido son Float),
    # asi que convertimos los totales Decimal en el borde para no mezclar tipos.
    total_gastos = float(sum(g.monto for g in gastos))
    total_ingresos = float(sum(i.monto for i in ingresos))
    
    analisis = generar_analisis_completo(total_ingresos, total_gastos, config)
    gastos_cat = gastos_por_categoria(gastos)
    ingresos_cat = ingresos_por_categoria(ingresos)
    
    # Últimos movimientos
    gastos_recientes = Gasto.query.filter_by(usuario_id=current_user.id).order_by(Gasto.fecha.desc()).limit(10).all()
    ingresos_recientes = Ingreso.query.filter_by(usuario_id=current_user.id).order_by(Ingreso.fecha.desc()).limit(10).all()
    
    return render_template('dashboard.html',
                         current_user=current_user,
                         usuario=current_user,
                         analisis=analisis,
                         gastos_cat=gastos_cat,
                         ingresos_cat=ingresos_cat,
                         gastos_recientes=gastos_recientes,
                         ingresos_recientes=ingresos_recientes)

# ======================== GASTOS ========================

@app.route('/gasto/nuevo', methods=['GET', 'POST'])
@login_required
def nuevo_gasto():
    if request.method == 'POST':
        try:
            descripcion = request.form.get('descripcion', '').strip()
            monto_str = request.form.get('monto', '').strip()
            
            # Validaciones
            if not descripcion:
                flash('La descripción es obligatoria', 'danger')
                return redirect(url_for('nuevo_gasto'))
            
            if not monto_str:
                flash('El monto es obligatorio', 'danger')
                return redirect(url_for('nuevo_gasto'))
            
            try:
                monto = Decimal(monto_str)
            except InvalidOperation:
                flash('El monto debe ser un número válido', 'danger')
                return redirect(url_for('nuevo_gasto'))
            
            if monto <= 0:
                flash('El monto debe ser mayor a 0', 'danger')
                return redirect(url_for('nuevo_gasto'))
            
            if not request.form.get('categoria_id'):
                flash('Debes seleccionar una categoría', 'danger')
                return redirect(url_for('nuevo_gasto'))
            
            # Verificar que la categoría pertenezca al usuario
            categoria = Categoria.query.get(int(request.form['categoria_id']))
            if not categoria or categoria.usuario_id != current_user.id:
                flash('Categoría inválida', 'danger')
                return redirect(url_for('nuevo_gasto'))
            
            gasto = Gasto(
                fecha=datetime.now().date(),
                descripcion=descripcion,
                monto=monto,
                categoria_id=int(request.form['categoria_id']),
                usuario_id=current_user.id
            )
            db.session.add(gasto)
            db.session.commit()
            
            registrar_cambio(current_user.id, 'crear', 'gasto', gasto.id, 
                           f'Gasto de ${monto} - {descripcion}')
            
            flash('✅ Gasto registrado exitosamente', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            flash(f'❌ Error al agregar gasto: {str(e)}', 'danger')
    
    # FILTRAR CATEGORÍAS DEL USUARIO
    categorias = Categoria.query.filter_by(usuario_id=current_user.id, tipo='gasto').all()
    return render_template('agregar_gasto.html', categorias=categorias)

@app.route('/gasto/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar_gasto(id):
    gasto = Gasto.query.get_or_404(id)
    
    # Verificar que pertenezca al usuario
    if gasto.usuario_id != current_user.id:
        flash('No tienes permiso', 'danger')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        try:
            descripcion = request.form.get('descripcion', '').strip()
            monto = Decimal(request.form.get('monto') or 0)
            
            if not descripcion or monto <= 0:
                flash('Complete todos los campos correctamente', 'danger')
                return redirect(url_for('editar_gasto', id=id))
            
            gasto.descripcion = descripcion
            gasto.monto = monto
            gasto.categoria_id = int(request.form['categoria_id'])
            gasto.fecha = datetime.strptime(request.form['fecha'], '%Y-%m-%d').date()
            
            db.session.commit()
            
            registrar_cambio(current_user.id, 'editar', 'gasto', gasto.id, 
                           f'Gasto actualizado - {descripcion}')
            
            flash('Gasto actualizado', 'success')
            return redirect(url_for('listar_gastos'))
        except Exception as e:
            flash(f'Error al editar: {str(e)}', 'danger')
    
    # FILTRAR CATEGORÍAS DEL USUARIO
    categorias = Categoria.query.filter_by(usuario_id=current_user.id, tipo='gasto').all()
    return render_template('editar_gasto.html', gasto=gasto, categorias=categorias)

@app.route('/gasto/eliminar/<int:id>', methods=['POST'])
@login_required
def eliminar_gasto(id):
    gasto = Gasto.query.get_or_404(id)
    
    # Verificar que pertenezca al usuario
    if gasto.usuario_id != current_user.id:
        flash('No tienes permiso', 'danger')
        return redirect(url_for('dashboard'))
    
    descripcion = gasto.descripcion
    monto = gasto.monto
    
    db.session.delete(gasto)
    db.session.commit()
    
    registrar_cambio(current_user.id, 'eliminar', 'gasto', id, 
                   f'Gasto eliminado - ${monto} - {descripcion}')
    
    flash('Gasto eliminado', 'success')
    return redirect(url_for('listar_gastos'))

@app.route('/gasto/listar')
@login_required
def listar_gastos():
    fecha_inicio = request.args.get('fecha_inicio')
    fecha_fin = request.args.get('fecha_fin')
    
    # FILTRAR POR USUARIO
    gastos = Gasto.query.filter_by(usuario_id=current_user.id).order_by(Gasto.fecha.desc()).all()
    
    if fecha_inicio:
        fecha_inicio_obj = datetime.strptime(fecha_inicio, '%Y-%m-%d').date()
        gastos = [g for g in gastos if g.fecha >= fecha_inicio_obj]
    
    if fecha_fin:
        fecha_fin_obj = datetime.strptime(fecha_fin, '%Y-%m-%d').date()
        gastos = [g for g in gastos if g.fecha <= fecha_fin_obj]
    
    return render_template('listar_gastos.html', gastos=gastos)

# ======================== INGRESOS ========================

@app.route('/ingreso/nuevo', methods=['GET', 'POST'])
@login_required
def nuevo_ingreso():
    if request.method == 'POST':
        try:
            descripcion = request.form.get('descripcion', '').strip()
            monto = Decimal(request.form.get('monto') or 0)
            
            if not descripcion or monto <= 0:
                flash('Complete todos los campos correctamente', 'danger')
                return redirect(url_for('nuevo_ingreso'))
            
            # Verificar que la categoría pertenezca al usuario
            categoria = Categoria.query.get(int(request.form['categoria_id']))
            if not categoria or categoria.usuario_id != current_user.id:
                flash('Categoría inválida', 'danger')
                return redirect(url_for('nuevo_ingreso'))
            
            ingreso = Ingreso(
                fecha=datetime.now().date(),
                descripcion=descripcion,
                monto=monto,
                categoria_id=int(request.form['categoria_id']),
                usuario_id=current_user.id
            )
            db.session.add(ingreso)
            db.session.commit()
            
            registrar_cambio(current_user.id, 'crear', 'ingreso', ingreso.id, 
                           f'Ingreso de ${monto} - {descripcion}')
            
            flash('Ingreso agregado exitosamente', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            flash(f'Error al agregar ingreso: {str(e)}', 'danger')
    
    # FILTRAR CATEGORÍAS DEL USUARIO
    categorias = Categoria.query.filter_by(usuario_id=current_user.id, tipo='ingreso').all()
    return render_template('agregar_ingreso.html', categorias=categorias)

@app.route('/ingreso/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar_ingreso(id):
    ingreso = Ingreso.query.get_or_404(id)
    
    # Verificar que pertenezca al usuario
    if ingreso.usuario_id != current_user.id:
        flash('No tienes permiso', 'danger')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        try:
            descripcion = request.form.get('descripcion', '').strip()
            monto = Decimal(request.form.get('monto') or 0)
            
            if not descripcion or monto <= 0:
                flash('Complete todos los campos correctamente', 'danger')
                return redirect(url_for('editar_ingreso', id=id))
            
            ingreso.descripcion = descripcion
            ingreso.monto = monto
            ingreso.categoria_id = int(request.form['categoria_id'])
            ingreso.fecha = datetime.strptime(request.form['fecha'], '%Y-%m-%d').date()
            
            db.session.commit()
            
            registrar_cambio(current_user.id, 'editar', 'ingreso', ingreso.id, 
                           f'Ingreso actualizado - {descripcion}')
            
            flash('Ingreso actualizado', 'success')
            return redirect(url_for('listar_ingresos'))
        except Exception as e:
            flash(f'Error al editar: {str(e)}', 'danger')
    
    # FILTRAR CATEGORÍAS DEL USUARIO
    categorias = Categoria.query.filter_by(usuario_id=current_user.id, tipo='ingreso').all()
    return render_template('editar_ingreso.html', ingreso=ingreso, categorias=categorias)

@app.route('/ingreso/eliminar/<int:id>', methods=['POST'])
@login_required
def eliminar_ingreso(id):
    ingreso = Ingreso.query.get_or_404(id)
    
    # Verificar que pertenezca al usuario
    if ingreso.usuario_id != current_user.id:
        flash('No tienes permiso', 'danger')
        return redirect(url_for('dashboard'))
    
    descripcion = ingreso.descripcion
    monto = ingreso.monto
    
    db.session.delete(ingreso)
    db.session.commit()
    
    registrar_cambio(current_user.id, 'eliminar', 'ingreso', id, 
                   f'Ingreso eliminado - ${monto} - {descripcion}')
    
    flash('Ingreso eliminado', 'success')
    return redirect(url_for('listar_ingresos'))

@app.route('/ingreso/listar')
@login_required
def listar_ingresos():
    fecha_inicio = request.args.get('fecha_inicio')
    fecha_fin = request.args.get('fecha_fin')
    
    # FILTRAR POR USUARIO
    ingresos = Ingreso.query.filter_by(usuario_id=current_user.id).order_by(Ingreso.fecha.desc()).all()
    
    if fecha_inicio:
        fecha_inicio_obj = datetime.strptime(fecha_inicio, '%Y-%m-%d').date()
        ingresos = [i for i in ingresos if i.fecha >= fecha_inicio_obj]
    
    if fecha_fin:
        fecha_fin_obj = datetime.strptime(fecha_fin, '%Y-%m-%d').date()
        ingresos = [i for i in ingresos if i.fecha <= fecha_fin_obj]
    
    return render_template('listar_ingresos.html', ingresos=ingresos)

# ======================== CONFIGURACIÓN ========================

@app.route('/config/eva', methods=['GET', 'POST'])
@login_required
def config_eva():
    empresa = current_user.empresa
    
    if request.method == 'POST':
        try:
            empresa.tasa_costo_capital = float(request.form['tasa_costo_capital'])
            empresa.capital_invertido = float(request.form['capital_invertido'])
            empresa.tasa_impuestos = float(request.form['tasa_impuestos']) / 100
            
            db.session.commit()
            
            registrar_cambio(current_user.id, 'editar', 'configuracion', empresa.id, 
                           'Configuración EVA actualizada')
            
            flash('Configuración actualizada', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            flash(f'Error: {str(e)}', 'danger')
    
    return render_template('configurar_eva.html', empresa=empresa)

# ======================== HISTORIAL ========================

@app.route('/historial')
@login_required
def ver_historial():
    # FILTRAR POR USUARIO
    historial = Historial.query.filter_by(usuario_id=current_user.id).order_by(Historial.fecha.desc()).all()
    return render_template('historial.html', historial=historial, usuario=current_user)

@app.route('/historial/limpiar', methods=['POST'])
@login_required
def limpiar_historial():
    # Limpiar solo el historial del usuario
    Historial.query.filter_by(usuario_id=current_user.id).delete()
    db.session.commit()
    
    flash('✅ Historial limpiado', 'success')
    return redirect(url_for('ver_historial'))

def registrar_cambio(usuario_id, accion, tipo, id_registro, descripcion):
    cambio = Historial(
        usuario_id=usuario_id,
        accion=accion,
        tipo=tipo,
        id_registro=id_registro,
        descripcion=descripcion
    )
    db.session.add(cambio)
    db.session.commit()

# ======================== CATEGORÍAS PERSONALIZABLES ========================

@app.route('/nueva-categoria', methods=['GET', 'POST'])
@login_required
def nueva_categoria():
    if request.method == 'POST':
        try:
            nombre = request.form.get('nombre', '').strip()
            tipo = request.form.get('tipo', '').strip()
            
            if not nombre:
                flash('El nombre es obligatorio', 'danger')
                return redirect(url_for('nueva_categoria'))
            
            if not tipo or tipo not in ['gasto', 'ingreso']:
                flash('Debes seleccionar un tipo valido', 'danger')
                return redirect(url_for('nueva_categoria'))
            
            if Categoria.query.filter_by(nombre=nombre, usuario_id=current_user.id).first():
                flash('Esta categoria ya existe', 'danger')
                return redirect(url_for('nueva_categoria'))
            
            categoria = Categoria(nombre=nombre, tipo=tipo, usuario_id=current_user.id)
            db.session.add(categoria)
            db.session.commit()
            
            registrar_cambio(current_user.id, 'crear', 'categoria', categoria.id, 
                           f'Categoria {nombre} creada')
            
            flash('Categoria creada exitosamente', 'success')
            return redirect(url_for('nueva_categoria'))
        except Exception as e:
            flash(f'Error: {str(e)}', 'danger')
            db.session.rollback()
    
    return render_template('nueva_categoria.html')

@app.route('/categorias')
@login_required
def listar_categorias():
    categorias = Categoria.query.filter_by(usuario_id=current_user.id).all()
    
    # Contar transacciones por categoría
    categorias_datos = []
    for cat in categorias:
        gastos = Gasto.query.filter_by(categoria_id=cat.id).count()
        ingresos = Ingreso.query.filter_by(categoria_id=cat.id).count()
        total_transacciones = gastos + ingresos
        
        categorias_datos.append({
            'categoria': cat,
            'total_transacciones': total_transacciones,
            'gastos': gastos,
            'ingresos': ingresos
        })
    
    return render_template('listar_categorias.html', categorias_datos=categorias_datos)

@app.route('/categoria/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar_categoria(id):
    categoria = Categoria.query.get_or_404(id)
    
    # Verificar que pertenezca al usuario
    if categoria.usuario_id != current_user.id:
        flash('No tienes permiso', 'danger')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        try:
            nombre = request.form.get('nombre', '').strip()
            
            if not nombre:
                flash('El nombre es obligatorio', 'danger')
                return redirect(url_for('editar_categoria', id=id))
            
            categoria.nombre = nombre
            db.session.commit()
            
            registrar_cambio(current_user.id, 'editar', 'categoria', categoria.id, 
                           f'Categoría actualizada - {nombre}')
            
            flash('Categoría actualizada', 'success')
            return redirect(url_for('listar_categorias'))
        except Exception as e:
            flash(f'Error: {str(e)}', 'danger')
    
    return render_template('editar_categoria.html', categoria=categoria)

@app.route('/categoria/eliminar/<int:id>', methods=['POST'])
@login_required
def eliminar_categoria(id):
    categoria = Categoria.query.get_or_404(id)
    
    # Verificar que pertenezca al usuario
    if categoria.usuario_id != current_user.id:
        flash('No tienes permiso', 'danger')
        return redirect(url_for('dashboard'))
    
    # Verificar si está en uso
    if Gasto.query.filter_by(categoria_id=id).first() or Ingreso.query.filter_by(categoria_id=id).first():
        flash('No puedes eliminar una categoría que está en uso', 'danger')
        return redirect(url_for('listar_categorias'))
    
    nombre = categoria.nombre
    db.session.delete(categoria)
    db.session.commit()
    
    registrar_cambio(current_user.id, 'eliminar', 'categoria', id, f'Categoría {nombre} eliminada')
    
    flash('Categoría eliminada', 'success')
    return redirect(url_for('listar_categorias'))

# ======================== EXPORTAR A EXCEL ========================

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from io import BytesIO
from flask import send_file

@app.route('/exportar/gastos')
@login_required
def exportar_gastos():
    # Obtener fechas de los parámetros GET
    fecha_inicio = request.args.get('fecha_inicio')
    fecha_fin = request.args.get('fecha_fin')
    
    gastos = Gasto.query.filter_by(usuario_id=current_user.id).all()
    
    # Filtrar por fecha si se proporciona
    if fecha_inicio:
        fecha_inicio = datetime.strptime(fecha_inicio, '%Y-%m-%d').date()
        gastos = [g for g in gastos if g.fecha >= fecha_inicio]
    
    if fecha_fin:
        fecha_fin = datetime.strptime(fecha_fin, '%Y-%m-%d').date()
        gastos = [g for g in gastos if g.fecha <= fecha_fin]
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Gastos"
    
    # Estilos
    header_fill = PatternFill(start_color="1e90ff", end_color="1e90ff", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=12)
    border = Border(left=Side(style='thin'), right=Side(style='thin'), 
                   top=Side(style='thin'), bottom=Side(style='thin'))
    
    # Headers
    headers = ['Fecha', 'Descripción', 'Categoría', 'Monto']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col)
        cell.value = header
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = border
    
    # Datos
    for row, gasto in enumerate(gastos, 2):
        ws.cell(row=row, column=1).value = gasto.fecha
        ws.cell(row=row, column=2).value = gasto.descripcion
        ws.cell(row=row, column=3).value = gasto.categoria.nombre
        ws.cell(row=row, column=4).value = float(gasto.monto)
        
        for col in range(1, 5):
            ws.cell(row=row, column=col).border = border
            if col == 4:
                ws.cell(row=row, column=col).number_format = '$#,##0.00'
    
    # Ancho de columnas
    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 30
    ws.column_dimensions['C'].width = 20
    ws.column_dimensions['D'].width = 12
    
    # Total
    total_row = len(gastos) + 2
    ws.cell(row=total_row, column=3).value = "TOTAL"
    ws.cell(row=total_row, column=3).font = Font(bold=True)
    ws.cell(row=total_row, column=4).value = f"=SUM(D2:D{len(gastos)+1})"
    ws.cell(row=total_row, column=4).font = Font(bold=True)
    ws.cell(row=total_row, column=4).number_format = '$#,##0.00'
    
    # Guardar en memoria
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    as_attachment=True, download_name=f'gastos_{datetime.now().strftime("%Y%m%d")}.xlsx')

@app.route('/exportar/ingresos')
@login_required
def exportar_ingresos():
    # Obtener fechas de los parámetros GET
    fecha_inicio = request.args.get('fecha_inicio')
    fecha_fin = request.args.get('fecha_fin')
    
    ingresos = Ingreso.query.filter_by(usuario_id=current_user.id).all()
    
    # Filtrar por fecha si se proporciona
    if fecha_inicio:
        fecha_inicio = datetime.strptime(fecha_inicio, '%Y-%m-%d').date()
        ingresos = [i for i in ingresos if i.fecha >= fecha_inicio]
    
    if fecha_fin:
        fecha_fin = datetime.strptime(fecha_fin, '%Y-%m-%d').date()
        ingresos = [i for i in ingresos if i.fecha <= fecha_fin]
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Ingresos"
    
    # Estilos
    header_fill = PatternFill(start_color="28a745", end_color="28a745", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=12)
    border = Border(left=Side(style='thin'), right=Side(style='thin'), 
                   top=Side(style='thin'), bottom=Side(style='thin'))
    
    # Headers
    headers = ['Fecha', 'Descripción', 'Categoría', 'Monto']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col)
        cell.value = header
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = border
    
    # Datos
    for row, ingreso in enumerate(ingresos, 2):
        ws.cell(row=row, column=1).value = ingreso.fecha
        ws.cell(row=row, column=2).value = ingreso.descripcion
        ws.cell(row=row, column=3).value = ingreso.categoria.nombre
        ws.cell(row=row, column=4).value = float(ingreso.monto)
        
        for col in range(1, 5):
            ws.cell(row=row, column=col).border = border
            if col == 4:
                ws.cell(row=row, column=col).number_format = '$#,##0.00'
    
    # Ancho de columnas
    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 30
    ws.column_dimensions['C'].width = 20
    ws.column_dimensions['D'].width = 12
    
    # Total
    total_row = len(ingresos) + 2
    ws.cell(row=total_row, column=3).value = "TOTAL"
    ws.cell(row=total_row, column=3).font = Font(bold=True)
    ws.cell(row=total_row, column=4).value = f"=SUM(D2:D{len(ingresos)+1})"
    ws.cell(row=total_row, column=4).font = Font(bold=True)
    ws.cell(row=total_row, column=4).number_format = '$#,##0.00'
    
    # Guardar en memoria
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    as_attachment=True, download_name=f'ingresos_{datetime.now().strftime("%Y%m%d")}.xlsx')

# ======================== FUNCIONES AUXILIARES ========================

def gastos_por_categoria(gastos):
    categorias_dict = {}
    for gasto in gastos:
        nombre = gasto.categoria.nombre
        if nombre not in categorias_dict:
            categorias_dict[nombre] = 0
        categorias_dict[nombre] += float(gasto.monto)
    return categorias_dict

def ingresos_por_categoria(ingresos):
    categorias_dict = {}
    for ingreso in ingresos:
        nombre = ingreso.categoria.nombre
        if nombre not in categorias_dict:
            categorias_dict[nombre] = 0
        categorias_dict[nombre] += float(ingreso.monto)
    return categorias_dict

@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404

@app.route('/cuenta/eliminar', methods=['GET', 'POST'])
@login_required
def eliminar_cuenta():
    if request.method == 'POST':
        try:
            usuario_id = current_user.id
            empresa_id = current_user.empresa_id
            nombre_usuario = current_user.nombre
            
            # Eliminar gastos
            Gasto.query.filter_by(usuario_id=usuario_id).delete()
            
            # Eliminar ingresos
            Ingreso.query.filter_by(usuario_id=usuario_id).delete()
            
            # Eliminar categorias
            Categoria.query.filter_by(usuario_id=usuario_id).delete()
            
            # Eliminar historial
            Historial.query.filter_by(usuario_id=usuario_id).delete()
            
            # Eliminar usuario
            db.session.delete(current_user)
            
            # Eliminar empresa si no tiene otros usuarios
            empresa = Empresa.query.get(empresa_id)
            if empresa and Empresa.query.filter_by(id=empresa_id).first():
                otros_usuarios = Usuario.query.filter_by(empresa_id=empresa_id).count()
                if otros_usuarios == 0:
                    db.session.delete(empresa)
            
            db.session.commit()
            
            logout_user()
            flash('Tu cuenta ha sido eliminada correctamente', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            flash(f'Error al eliminar cuenta: {str(e)}', 'danger')
            return redirect(url_for('dashboard'))
    
    return render_template('eliminar_cuenta.html', usuario=current_user)

if __name__ == '__main__':
    print("\n" + "="*50)
    print("🔍 CONFIGURACIÓN DE EMAIL:")
    print(f"✓ MAIL_USERNAME: {app.config['MAIL_USERNAME']}")
    print(f"✓ MAIL_PASSWORD: {'*' * 20}")
    print(f"✓ MAIL_SERVER: {app.config['MAIL_SERVER']}")
    print("="*50 + "\n")
    print("Iniciando servidor...")
    print("Abre: http://localhost:5000")
    app.run(debug=True, host='127.0.0.1', port=5000)