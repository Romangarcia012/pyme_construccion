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

    # --- Ampliacion FASE2-S1 (todos opcionales: no rompen filas existentes) ---
    proveedor = db.Column(db.String(150))
    cuenta_pago_id = db.Column(db.Integer, db.ForeignKey('cuenta_cobro.id'))
    comprobante = db.Column(db.String(100))
    deducible = db.Column(db.Boolean)
    moneda = db.Column(db.String(3))

    cuenta_pago = db.relationship('CuentaCobro', backref='gastos')
    

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

# ============================================================================
# FASE2-S1 - Modelo de datos del ecommerce
# ----------------------------------------------------------------------------
# Tablas nuevas, puramente aditivas. Ninguna toca las 6 tablas originales
# (empresa, usuario, categoria, historial, gasto, ingreso) mas alla de la
# ampliacion opcional de Gasto.
#
# Reglas de diseno:
#   - Todo campo de plata es Numeric(14, 2). Nunca Float: la conciliacion
#     compara importes por igualdad y el error binario de Float genera
#     diferencias fantasma de centavos.
#   - Las tasas de cambio son Numeric(18, 6) porque son un ratio, no un monto.
#   - raw_payload guarda la respuesta cruda de la API para poder reprocesar
#     sin volver a pegarle al canal.
# ============================================================================


class CanalVenta(db.Model):
    """Un canal de venta conectado (Tiendanube, Mercado Libre)."""
    __tablename__ = 'canal_venta'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    tipo = db.Column(db.String(30), nullable=False)  # 'tiendanube' | 'mercadolibre'
    nombre = db.Column(db.String(100), nullable=False)
    id_tienda_externo = db.Column(db.String(100))
    activo = db.Column(db.Boolean, nullable=False, default=False)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_ultima_sync = db.Column(db.DateTime)

    empresa = db.relationship('Empresa', backref='canales_venta')

    __table_args__ = (
        db.UniqueConstraint('empresa_id', 'tipo', name='uq_canal_venta_empresa_tipo'),
    )


class CredencialCanal(db.Model):
    """Credenciales OAuth/API de un canal. Los tokens se guardan cifrados;
    el cifrado lo aporta el ingestor de la Fase 3."""
    __tablename__ = 'credencial_canal'
    id = db.Column(db.Integer, primary_key=True)
    canal_id = db.Column(db.Integer, db.ForeignKey('canal_venta.id'), nullable=False, index=True)
    tipo_credencial = db.Column(db.String(30), nullable=False, default='oauth2')  # 'oauth2' | 'api_key'
    access_token_cifrado = db.Column(db.Text)
    refresh_token_cifrado = db.Column(db.Text)
    scope = db.Column(db.String(255))
    expira_en = db.Column(db.DateTime)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    canal = db.relationship('CanalVenta', backref='credenciales')


class Producto(db.Model):
    """Catalogo interno. El SKU es la identidad; los ids de cada canal viven
    en MapeoProductoCanal."""
    __tablename__ = 'producto'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    sku = db.Column(db.String(60), nullable=False)
    nombre = db.Column(db.String(200), nullable=False)
    descripcion = db.Column(db.Text)
    costo_unitario = db.Column(db.Numeric(14, 2))   # costo VIGENTE, cambia con el tiempo
    precio_lista = db.Column(db.Numeric(14, 2))
    moneda = db.Column(db.String(3), nullable=False, default='ARS')
    stock = db.Column(db.Integer)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    empresa = db.relationship('Empresa', backref='productos')

    __table_args__ = (
        db.UniqueConstraint('empresa_id', 'sku', name='uq_producto_empresa_sku'),
    )


class MapeoProductoCanal(db.Model):
    """Traduce el id externo de un producto/variante al Producto interno."""
    __tablename__ = 'mapeo_producto_canal'
    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'), nullable=False, index=True)
    canal_id = db.Column(db.Integer, db.ForeignKey('canal_venta.id'), nullable=False, index=True)
    id_producto_externo = db.Column(db.String(100), nullable=False)
    # '' en vez de NULL: en Postgres un NULL no colisiona consigo mismo y la
    # UNIQUE de abajo dejaria entrar duplicados de productos sin variante.
    id_variante_externo = db.Column(db.String(100), nullable=False, server_default='', default='')
    sku_externo = db.Column(db.String(100))
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    producto = db.relationship('Producto', backref='mapeos')
    canal = db.relationship('CanalVenta', backref='mapeos_producto')

    __table_args__ = (
        db.UniqueConstraint('canal_id', 'id_producto_externo', 'id_variante_externo',
                            name='uq_mapeo_canal_producto_variante'),
    )


class Pedido(db.Model):
    """Un pedido tal como lo devuelve el canal, ya normalizado."""
    __tablename__ = 'pedido'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    canal_id = db.Column(db.Integer, db.ForeignKey('canal_venta.id'), nullable=False, index=True)
    id_externo = db.Column(db.String(100), nullable=False)
    numero_externo = db.Column(db.String(50))
    fecha_pedido = db.Column(db.DateTime, nullable=False, index=True)
    estado = db.Column(db.String(30), nullable=False, default='pendiente')
    estado_externo = db.Column(db.String(50))
    comprador_nombre = db.Column(db.String(150))
    comprador_email = db.Column(db.String(150))
    comprador_doc = db.Column(db.String(30))
    moneda = db.Column(db.String(3), nullable=False, default='ARS')
    total_bruto = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total_descuentos = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total_envio = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total_impuestos = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    raw_payload = db.Column(db.JSON)
    fecha_sync = db.Column(db.DateTime)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    empresa = db.relationship('Empresa', backref='pedidos')
    canal = db.relationship('CanalVenta', backref='pedidos')
    items = db.relationship('PedidoItem', backref='pedido', cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('canal_id', 'id_externo', name='uq_pedido_canal_id_externo'),
    )


class PedidoItem(db.Model):
    """Linea de un pedido."""
    __tablename__ = 'pedido_item'
    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido.id'), nullable=False, index=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'), index=True)  # null si no hay mapeo aun
    sku_externo = db.Column(db.String(100))
    descripcion = db.Column(db.String(200), nullable=False)
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    precio_unitario = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    descuento_unitario = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    # Congela el costo del producto AL MOMENTO de la venta. Sin esto, cambiar
    # producto.costo_unitario reescribiria el margen de todos los pedidos
    # historicos. No es redundante: es la unica copia del costo de ese dia.
    costo_unitario_snapshot = db.Column(db.Numeric(14, 2))
    subtotal = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    producto = db.relationship('Producto', backref='items_vendidos')


class CuentaCobro(db.Model):
    """Cuenta donde entra la plata: Mercado Pago o Tiendanube Pagos.
    metodo_ingesta queda restringido a 'api' (sin archivo ni carga manual)."""
    __tablename__ = 'cuenta_cobro'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    nombre = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(30), nullable=False)  # 'mercadopago' | 'tiendanube_pagos'
    metodo_ingesta = db.Column(db.String(20), nullable=False, default='api', server_default='api')
    identificador_externo = db.Column(db.String(100))
    moneda = db.Column(db.String(3), nullable=False, default='ARS')
    saldo_actual = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_ultima_sync = db.Column(db.DateTime)

    empresa = db.relationship('Empresa', backref='cuentas_cobro')

    __table_args__ = (
        db.UniqueConstraint('empresa_id', 'nombre', name='uq_cuenta_cobro_empresa_nombre'),
        db.CheckConstraint("metodo_ingesta = 'api'", name='ck_cuenta_cobro_metodo_ingesta_api'),
    )


class Liquidacion(db.Model):
    """Payout del procesador a la cuenta: agrupa pagos y descuenta comisiones."""
    __tablename__ = 'liquidacion'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    cuenta_cobro_id = db.Column(db.Integer, db.ForeignKey('cuenta_cobro.id'), nullable=False, index=True)
    procesador = db.Column(db.String(30), nullable=False)
    id_externo = db.Column(db.String(100))
    fecha_liquidacion = db.Column(db.Date, nullable=False, index=True)
    periodo_desde = db.Column(db.Date)
    periodo_hasta = db.Column(db.Date)
    moneda = db.Column(db.String(3), nullable=False, default='ARS')
    monto_bruto = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    comisiones = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    impuestos = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    retenciones = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    monto_neto = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    estado = db.Column(db.String(20), nullable=False, default='pendiente')
    raw_payload = db.Column(db.JSON)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    empresa = db.relationship('Empresa', backref='liquidaciones')
    cuenta_cobro = db.relationship('CuentaCobro', backref='liquidaciones')

    __table_args__ = (
        db.UniqueConstraint('procesador', 'id_externo', name='uq_liquidacion_procesador_id_externo'),
    )


class Pago(db.Model):
    """Cobro individual en el procesador. Puede llegar antes que su pedido,
    por eso pedido_id es opcional."""
    __tablename__ = 'pago'
    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido.id'), index=True)
    canal_id = db.Column(db.Integer, db.ForeignKey('canal_venta.id'), index=True)
    cuenta_cobro_id = db.Column(db.Integer, db.ForeignKey('cuenta_cobro.id'), index=True)
    liquidacion_id = db.Column(db.Integer, db.ForeignKey('liquidacion.id'), index=True)
    procesador = db.Column(db.String(30), nullable=False)  # 'mercadopago' | 'tiendanube_pagos'
    id_externo = db.Column(db.String(100), nullable=False)
    metodo = db.Column(db.String(50))
    cuotas = db.Column(db.Integer)
    estado = db.Column(db.String(30), nullable=False, default='pendiente')
    moneda = db.Column(db.String(3), nullable=False, default='ARS')
    monto_bruto = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    comision = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    impuestos = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    monto_neto = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    fecha_pago = db.Column(db.DateTime, nullable=False, index=True)
    fecha_acreditacion = db.Column(db.DateTime)
    raw_payload = db.Column(db.JSON)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    pedido = db.relationship('Pedido', backref='pagos')
    canal = db.relationship('CanalVenta', backref='pagos')
    cuenta_cobro = db.relationship('CuentaCobro', backref='pagos')
    liquidacion = db.relationship('Liquidacion', backref='pagos')

    __table_args__ = (
        db.UniqueConstraint('procesador', 'id_externo', name='uq_pago_procesador_id_externo'),
    )


class MovimientoCuenta(db.Model):
    """Linea del extracto de la cuenta de cobro. monto va firmado:
    positivo entra, negativo sale."""
    __tablename__ = 'movimiento_cuenta'
    id = db.Column(db.Integer, primary_key=True)
    cuenta_id = db.Column(db.Integer, db.ForeignKey('cuenta_cobro.id'), nullable=False, index=True)
    fecha = db.Column(db.DateTime, nullable=False, index=True)
    tipo = db.Column(db.String(30), nullable=False)  # cobro|comision|retiro|devolucion|contracargo|impuesto|ajuste
    descripcion = db.Column(db.String(255))
    moneda = db.Column(db.String(3), nullable=False, default='ARS')
    monto = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    saldo_posterior = db.Column(db.Numeric(14, 2))
    # id nativo del procesador, cuando lo trae
    id_externo_procesador = db.Column(db.String(120))
    # Huella del movimiento. UNIQUE: reimportar el mismo extracto no puede
    # duplicar plata, aunque el procesador no haya dado id propio.
    hash_dedup = db.Column(db.String(64), nullable=False, unique=True)
    conciliado = db.Column(db.Boolean, nullable=False, default=False)
    raw_payload = db.Column(db.JSON)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    cuenta = db.relationship('CuentaCobro', backref='movimientos')

    __table_args__ = (
        db.UniqueConstraint('cuenta_id', 'id_externo_procesador',
                            name='uq_movimiento_cuenta_id_externo'),
    )


class Conciliacion(db.Model):
    """Match entre un Pago (lo que dice el canal) y un MovimientoCuenta
    (lo que realmente entro). La diferencia es el hallazgo."""
    __tablename__ = 'conciliacion'
    id = db.Column(db.Integer, primary_key=True)
    pago_id = db.Column(db.Integer, db.ForeignKey('pago.id'), index=True)
    movimiento_id = db.Column(db.Integer, db.ForeignKey('movimiento_cuenta.id'), index=True)
    estado = db.Column(db.String(20), nullable=False, default='pendiente')  # conciliado|parcial|discrepancia|pendiente
    diferencia = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    metodo = db.Column(db.String(20), nullable=False, default='automatico')
    nota = db.Column(db.String(255))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    fecha_conciliacion = db.Column(db.DateTime, default=datetime.utcnow)

    pago = db.relationship('Pago', backref='conciliaciones')
    movimiento = db.relationship('MovimientoCuenta', backref='conciliaciones')
    usuario = db.relationship('Usuario', backref='conciliaciones')

    __table_args__ = (
        db.UniqueConstraint('pago_id', 'movimiento_id', name='uq_conciliacion_pago_movimiento'),
    )


class Devolucion(db.Model):
    """Devoluciones y contracargos. APPEND-ONLY: nunca se hace UPDATE ni
    DELETE sobre una fila. Cada cambio de estado entra como fila nueva que
    apunta a la anterior via evento_previo_id; el estado vigente es la ultima
    fila de la cadena. Un contracargo revisado tres veces deja tres filas, y
    esa historia es justamente lo que hay que poder auditar."""
    __tablename__ = 'devolucion'
    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido.id'), nullable=False, index=True)
    pago_id = db.Column(db.Integer, db.ForeignKey('pago.id'), index=True)
    evento_previo_id = db.Column(db.Integer, db.ForeignKey('devolucion.id'), index=True)
    tipo = db.Column(db.String(20), nullable=False)  # 'devolucion' | 'contracargo' | 'cancelacion'
    id_externo = db.Column(db.String(100), index=True)
    motivo = db.Column(db.String(255))
    moneda = db.Column(db.String(3), nullable=False, default='ARS')
    monto = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    comision_devuelta = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    estado = db.Column(db.String(30), nullable=False, default='abierta')
    fecha_evento = db.Column(db.DateTime, nullable=False, index=True)
    raw_payload = db.Column(db.JSON)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    pedido = db.relationship('Pedido', backref='devoluciones')
    pago = db.relationship('Pago', backref='devoluciones')
    evento_previo = db.relationship('Devolucion', remote_side=[id], backref='eventos_siguientes')


class SyncLog(db.Model):
    """Bitacora de cada corrida de polling contra un canal o cuenta.
    Reemplaza a webhook_event mientras el volumen no justifique webhooks."""
    __tablename__ = 'sync_log'
    id = db.Column(db.Integer, primary_key=True)
    canal_id = db.Column(db.Integer, db.ForeignKey('canal_venta.id'), index=True)
    cuenta_cobro_id = db.Column(db.Integer, db.ForeignKey('cuenta_cobro.id'), index=True)
    entidad = db.Column(db.String(30), nullable=False)  # pedido|pago|liquidacion|movimiento|producto
    operacion = db.Column(db.String(20), nullable=False, default='poll')
    estado = db.Column(db.String(20), nullable=False, default='ok')  # ok|error|parcial
    cursor_desde = db.Column(db.DateTime)
    cursor_hasta = db.Column(db.DateTime)
    registros_leidos = db.Column(db.Integer, nullable=False, default=0)
    registros_nuevos = db.Column(db.Integer, nullable=False, default=0)
    registros_actualizados = db.Column(db.Integer, nullable=False, default=0)
    registros_error = db.Column(db.Integer, nullable=False, default=0)
    mensaje_error = db.Column(db.Text)
    duracion_ms = db.Column(db.Integer)
    fecha_inicio = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    fecha_fin = db.Column(db.DateTime)

    canal = db.relationship('CanalVenta', backref='sync_logs')
    cuenta_cobro = db.relationship('CuentaCobro', backref='sync_logs')


class ReglaCategorizacion(db.Model):
    """Regla para asignar categoria automaticamente a un gasto/ingreso
    importado. Se evalua por prioridad ascendente; la primera que matchea gana."""
    __tablename__ = 'regla_categorizacion'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    nombre = db.Column(db.String(100), nullable=False)
    campo = db.Column(db.String(30), nullable=False)      # descripcion|proveedor|concepto
    operador = db.Column(db.String(20), nullable=False, default='contiene')  # contiene|igual|empieza_con|regex
    valor = db.Column(db.String(200), nullable=False)
    categoria_id = db.Column(db.Integer, db.ForeignKey('categoria.id'), nullable=False, index=True)
    tipo_destino = db.Column(db.String(20), nullable=False, default='gasto')  # 'gasto' | 'ingreso'
    prioridad = db.Column(db.Integer, nullable=False, default=100)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    empresa = db.relationship('Empresa', backref='reglas_categorizacion')
    categoria = db.relationship('Categoria', backref='reglas_categorizacion')


class TipoCambio(db.Model):
    """Cotizacion diaria. tasa es Numeric(18, 6): es un ratio, no un monto,
    y con 2 decimales el redondeo se propaga a cada conversion."""
    __tablename__ = 'tipo_cambio'
    id = db.Column(db.Integer, primary_key=True)
    moneda_origen = db.Column(db.String(3), nullable=False)
    moneda_destino = db.Column(db.String(3), nullable=False, default='ARS')
    fecha = db.Column(db.Date, nullable=False, index=True)
    tasa = db.Column(db.Numeric(18, 6), nullable=False)
    fuente = db.Column(db.String(50))
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('moneda_origen', 'moneda_destino', 'fecha',
                            name='uq_tipo_cambio_par_fecha'),
    )
