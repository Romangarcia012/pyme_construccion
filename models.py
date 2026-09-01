from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()

class Usuario(UserMixin, db.Model):
    __tablename__ = 'usuario'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)  # <-- ESTA LÍNEA
    rol = db.Column(db.String(20), default='usuario')
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False)
    activo = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    
    verificado = db.Column(db.Boolean, default=False)
    codigo_verificacion = db.Column(db.String(10))
    codigo_reset = db.Column(db.String(10))
    
    # Relaciones
    empresa = db.relationship('Empresa', backref='usuarios')
    gastos = db.relationship('Gasto', backref='usuario', lazy=True, cascade='all, delete-orphan')
    ingresos = db.relationship('Ingreso', backref='usuario', lazy=True, cascade='all, delete-orphan')
    categorias = db.relationship('Categoria', backref='usuario', lazy=True, cascade='all, delete-orphan')
    
    def set_password(self, password):
        self.password = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password, password)


class Empresa(db.Model):
    __tablename__ = 'empresa'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(150), nullable=False)
    ruc = db.Column(db.String(20))
    direccion = db.Column(db.String(200))
    telefono = db.Column(db.String(20))
    email = db.Column(db.String(120))
    descripcion = db.Column(db.Text)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Configuración EVA
    tasa_costo_capital = db.Column(db.Float, default=10.0)
    capital_invertido = db.Column(db.Float, default=0)
    tasa_impuestos = db.Column(db.Float, default=0.30)  # AGREGAR ESTA LÍNEA
    

class Categoria(db.Model):
    __tablename__ = 'categoria'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)  # 'gasto' o 'ingreso'
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    

class Gasto(db.Model):
    __tablename__ = 'gasto'
    id = db.Column(db.Integer, primary_key=True)
    descripcion = db.Column(db.String(200), nullable=False)
    monto = db.Column(db.Numeric(14, 2), nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    categoria_id = db.Column(db.Integer, db.ForeignKey('categoria.id'))
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    
    categoria = db.relationship('Categoria', backref='gastos')
    

class Ingreso(db.Model):
    __tablename__ = 'ingreso'
    id = db.Column(db.Integer, primary_key=True)
    descripcion = db.Column(db.String(200), nullable=False)
    monto = db.Column(db.Numeric(14, 2), nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    categoria_id = db.Column(db.Integer, db.ForeignKey('categoria.id'))
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    
    categoria = db.relationship('Categoria', backref='ingresos')

class Historial(db.Model):
    __tablename__ = 'historial'
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    accion = db.Column(db.String(50), nullable=False)  # 'crear', 'editar', 'eliminar'
    tipo = db.Column(db.String(50), nullable=False)  # 'gasto', 'ingreso', 'categoria', etc
    id_registro = db.Column(db.Integer)
    descripcion = db.Column(db.String(200))
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    
    usuario = db.relationship('Usuario', backref='historial')