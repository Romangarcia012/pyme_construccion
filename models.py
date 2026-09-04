from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from collections import OrderedDict
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
    # SIN cascade='all, delete-orphan' desde FASE-CAJA-GENERAL-S2, mismo
    # criterio que `historial`: la caja es de la EMPRESA, no de la persona que
    # tipeo la fila. Borrar una cuenta ponia en cero el libro entero -- y con
    # el la unica constancia de en que se gasto la plata. Ahora SQLAlchemy
    # anula `usuario_id` (por eso paso a nullable en las dos tablas) y la fila
    # sigue viva colgada de `empresa_id`.
    #
    # `categorias` PERDIO el cascade en FASE-CATEGORIA-S1, por el mismo
    # motivo y con el mismo mecanismo: el vocabulario con el que se etiqueta
    # la caja es de la empresa. Mientras colgaba del usuario hacia falta
    # reasignarlas a mano al borrar una cuenta (estaba en `eliminar_cuenta`)
    # para que la empresa no se quedara sin etiquetas; con `empresa_id` como
    # duenio real ese parche ya no hace falta y se fue.
    gastos = db.relationship('Gasto', backref='usuario', lazy=True)
    ingresos = db.relationship('Ingreso', backref='usuario', lazy=True)
    categorias = db.relationship('Categoria', backref='usuario', lazy=True)
    
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
    # FASE-CATEGORIA-S1: mismo par que Gasto e Ingreso, y por el mismo motivo.
    # El vocabulario con el que se etiqueta la caja es de la EMPRESA: las siete
    # categorias Korvo las sembro la migracion sobre UN usuario, y mientras esa
    # fuera la unica pista de a quien pertenecian hacian falta dos parches para
    # sostenerlo -- un join a usuario en cada consulta, y una reasignacion al
    # borrar la cuenta. Los dos se fueron con esta columna.
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'),
                           nullable=False, index=True)
    # NULLABLE: quien tipeo la categoria sigue siendo un dato util, pero no
    # puede ser el que decide si la fila existe. Al borrarse la cuenta queda en
    # NULL y la categoria sobrevive -- sin ella, los gastos que FASE-CAJA-
    # GENERAL-S2 acababa de salvar se quedaban sin etiqueta.
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    empresa = db.relationship('Empresa', backref='categorias')
    

# FASE-CAJA-GENERAL-S3: de que plata salio un gasto, con vocabulario fijo.
#
# La clave es lo que se guarda en gasto.origen_fondo y lo que se compara en
# codigo; el valor es como se escribe en pantalla, igual que en SOCIOS.
#
# Son los dos unicos bolsillos que existen y no se mezclan:
#
#   'facturacion' -> salio de la plata que entra por las ventas, y por eso
#                    tiene que decir de CUAL cuenta de cobro salio (la de
#                    Roman o la de Nachi). Esa plata le baja el saldo real a
#                    ese socio: es lo que /reportes/caja-socio resta.
#   'capital'     -> salio del pool que los socios aportaron, que es plata
#                    ajena a la facturacion. No le baja el saldo a nadie en
#                    particular, y por eso NO lleva cuenta de cobro.
#
# El aporte que forma ese pool ya entra por el otro lado, como Ingreso con la
# categoria "Aporte de capital (socios)" (FASE-CAJA-GENERAL-S2). Esto es la
# salida, no la entrada, y no se toca con aquello.
ORIGEN_FACTURACION = 'facturacion'
ORIGEN_CAPITAL = 'capital'

ORIGENES_FONDO = OrderedDict([
    (ORIGEN_FACTURACION, 'Facturación'),
    (ORIGEN_CAPITAL, 'Capital'),
])

# Lo que se muestra cuando la fila no lo dice. NO es un tercer origen: es la
# ausencia del dato, y se escribe distinto justamente para que se note que
# falta en vez de parecer una categoria mas.
ETIQUETA_SIN_ORIGEN = 'sin dato'


class Gasto(db.Model):
    __tablename__ = 'gasto'
    id = db.Column(db.Integer, primary_key=True)
    descripcion = db.Column(db.String(200), nullable=False)
    monto = db.Column(db.Numeric(14, 2), nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    # FASE-CAJA-GENERAL-S2: el duenio de la fila es la EMPRESA. Antes era el
    # usuario, y con eso un "libro unico de caja" no era unico: el dia que
    # Nachi tenga login, cada uno veria su mitad y nadie la caja. Es el mismo
    # arreglo que FASE-AUDITORIA-S2 le hizo a `historial`.
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'),
                           nullable=False, index=True)
    # NULLABLE desde la misma slice: quien tipeo la fila sigue siendo un dato
    # util, pero no puede ser el que decide si la fila existe. Al borrarse la
    # cuenta queda en NULL y el gasto sobrevive.
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)
    categoria_id = db.Column(db.Integer, db.ForeignKey('categoria.id'))
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    
    categoria = db.relationship('Categoria', backref='gastos')
    empresa = db.relationship('Empresa', backref='gastos')

    # --- Ampliacion FASE2-S1 (todos opcionales: no rompen filas existentes) ---
    proveedor = db.Column(db.String(150))
    cuenta_pago_id = db.Column(db.Integer, db.ForeignKey('cuenta_cobro.id'))
    comprobante = db.Column(db.String(100))
    deducible = db.Column(db.Boolean)
    moneda = db.Column(db.String(3))

    cuenta_pago = db.relationship('CuentaCobro', backref='gastos')

    # --- FASE-CAJA-GENERAL-S3: con que plata se pago ------------------------
    # Vocabulario fijo (ver ORIGENES_FONDO). NULLABLE a proposito: NULL es
    # "todavia no se dijo", no un tercer origen. Las filas viejas se quedan
    # asi -- no se les inventa de que bolsillo salieron -- y el reporte de caja
    # las cuenta aparte como "sin clasificar" en vez de hacer que el saldo
    # mienta por omision.
    origen_fondo = db.Column(db.String(20))

    __table_args__ = (
        # El mismo vocabulario que valida `_validar_origen_fondo`, pero del
        # lado de la base. No es redundante: el formulario no es el unico
        # camino de escritura -- las migraciones y los scripts escriben SQL
        # crudo, que no pasa por el modelo ni por su @validates.
        db.CheckConstraint(
            "origen_fondo IS NULL OR origen_fondo IN ('facturacion', 'capital')",
            name='ck_gasto_origen_fondo_vocabulario'),
        # La regla que le da sentido al campo: 'facturacion' SIN cuenta no
        # contesta la pregunta (¿de la de Roman o de la de Nachi?), y
        # 'capital' CON cuenta afirma algo falso -- que esa plata salio de lo
        # facturado por ese socio -- que le restaria de menos al saldo real.
        #
        # El caso origen_fondo NULL queda deliberadamente fuera de la regla:
        # cuenta_pago_id existe desde FASE2-S1 como campo suelto y una fila
        # vieja podria tenerlo cargado sin que nadie haya dicho de que
        # bolsillo salio. Apretarlo aca obligaria a inventar ese dato o a
        # borrar el que ya hay.
        db.CheckConstraint(
            "origen_fondo IS NULL"
            " OR (origen_fondo = 'facturacion' AND cuenta_pago_id IS NOT NULL)"
            " OR (origen_fondo = 'capital' AND cuenta_pago_id IS NULL)",
            name='ck_gasto_origen_fondo_cuenta'),
    )

    @db.validates('origen_fondo')
    def _validar_origen_fondo(self, clave, valor):
        """Se escribe con una de las claves de ORIGENES_FONDO, o no se escribe.

        Mismo criterio que `CuentaCobro._validar_socio`: un typo
        ('Facturacion', 'capitl') no puede quedar guardado. Se colaria en la
        base como un origen desconocido, no lo levantaria ni la suma de
        facturacion ni la de capital, y el gasto desapareceria de las dos
        columnas del reporte sin que nada lo delate.
        """
        if valor is None or valor == '':
            return None
        if valor not in ORIGENES_FONDO:
            raise ValueError(
                'origen_fondo invalido: %r. Los validos son %s.'
                % (valor, ', '.join(ORIGENES_FONDO)))
        return valor

    @property
    def origen_legible(self):
        """Como se escribe el origen en pantalla, cuenta incluida.

        'Facturación — Roman' / 'Capital' / 'sin dato'. Vive en el modelo y no
        en cada plantilla porque lo muestran tres pantallas distintas
        (/gasto/listar, /caja-general y el reporte de caja) y las tres tienen
        que decir lo mismo.

        Para el nombre del socio se usa el vocabulario (SOCIOS), no
        `cuenta.nombre`: el nombre es texto libre y renombrar la cuenta no
        tiene por que cambiar lo que dice esta linea. Si la cuenta no tiene
        socio asignado se cae al nombre, que es lo unico que queda.
        """
        if self.origen_fondo is None:
            return ETIQUETA_SIN_ORIGEN

        etiqueta = ORIGENES_FONDO[self.origen_fondo]
        if self.origen_fondo != ORIGEN_FACTURACION:
            return etiqueta

        cuenta = self.cuenta_pago
        if cuenta is None:
            # El CHECK de arriba no deberia dejar llegar aca. Si igual pasa
            # (una fila escrita antes del CHECK, una base sin migrar), se
            # muestra el hueco en vez de reventar la pantalla entera.
            return '%s — %s' % (etiqueta, ETIQUETA_SIN_ORIGEN)
        return '%s — %s' % (etiqueta, cuenta.etiqueta_socio)


class Ingreso(db.Model):
    __tablename__ = 'ingreso'
    id = db.Column(db.Integer, primary_key=True)
    descripcion = db.Column(db.String(200), nullable=False)
    monto = db.Column(db.Numeric(14, 2), nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    # Mismo par que en Gasto y por el mismo motivo (FASE-CAJA-GENERAL-S2).
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'),
                           nullable=False, index=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)
    categoria_id = db.Column(db.Integer, db.ForeignKey('categoria.id'))
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    
    categoria = db.relationship('Categoria', backref='ingresos')
    empresa = db.relationship('Empresa', backref='ingresos')

class Historial(db.Model):
    __tablename__ = 'historial'
    id = db.Column(db.Integer, primary_key=True)
    # NULLABLE desde FASE-AUDITORIA-S2, por dos razones distintas:
    #   1. Una cuenta que se elimina deja su historial atras. La fila sobrevive
    #      al usuario con usuario_id en NULL; antes se borraba el rastro junto
    #      con el que lo habia generado.
    #   2. Deja lugar para las acciones del sistema (el sync pisando una
    #      edicion hecha a mano). Esta slice todavia no las genera.
    # Quien fue sigue siendo legible en `descripcion` aunque la FK quede NULL.
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)
    # El historial es de la EMPRESA, no del usuario: sin esto, el dia que Nachi
    # tenga login, Roman no veria nada de lo que hizo Nachi -- que es
    # exactamente lo que esta fase existe para resolver.
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'),
                           nullable=False, index=True)
    accion = db.Column(db.String(50), nullable=False)  # 'crear', 'editar', 'eliminar'
    tipo = db.Column(db.String(50), nullable=False)  # 'gasto', 'ingreso', 'categoria', etc
    id_registro = db.Column(db.Integer)
    descripcion = db.Column(db.String(200))
    # Los completa el hook de auditoria.py via get_history(); las once llamadas
    # manuales a registrar_cambio() los dejan en NULL, como venian.
    valor_anterior = db.Column(db.Text)
    valor_nuevo = db.Column(db.Text)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

    usuario = db.relationship('Usuario', backref='historial')
    empresa = db.relationship('Empresa', backref='historial')

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
    """Un canal de venta: Tiendanube, Mercado Libre, o la carga manual.

    'manual' no es un canal externo. No tiene OAuth ni CredencialCanal y nace
    activo: lo unico que hace falta para cargar una venta de mostrador es que
    alguien la tipee. Por eso la invariante "canal activo => credencial activa"
    solo aplica a los canales externos.
    """
    __tablename__ = 'canal_venta'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    tipo = db.Column(db.String(30), nullable=False)  # 'tiendanube' | 'mercadolibre' | 'manual'
    nombre = db.Column(db.String(100), nullable=False)
    id_tienda_externo = db.Column(db.String(100))
    activo = db.Column(db.Boolean, nullable=False, default=False)
    # FASE-MP-S1: a que cuenta entra la plata de este canal. Nullable porque el
    # canal existe desde antes que la cuenta y porque un canal puede liquidar a
    # una cuenta que todavia no se conecto. No es lo mismo que la cuenta del
    # pago: esto es la regla general del canal, el pago dira lo que paso.
    cuenta_cobro_id = db.Column(db.Integer, db.ForeignKey('cuenta_cobro.id'), index=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_ultima_sync = db.Column(db.DateTime)

    empresa = db.relationship('Empresa', backref='canales_venta')
    cuenta_cobro = db.relationship('CuentaCobro', backref='canales_venta')

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
    # Text y no String(255): el scope lo escribe el proveedor, no nosotros.
    # Mercado Libre devuelve la lista de permisos concedidos y no cabe en 255;
    # el callback reventaba con StringDataRightTruncation DESPUES de que la
    # persona ya habia autorizado en la pantalla de MeLi. Un campo informativo
    # de texto plano no puede ser el que decide si una integracion funciona.
    scope = db.Column(db.Text)
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


# --- Estado de despacho (FASE-REPORTES-S2) -----------------------------------
#
# "¿Este pedido ya salio?" es un dato que Tiendanube manda y que ya se guarda
# entero dentro de pedido.raw_payload. NO tiene columna propia y es a proposito:
# con menos de 500 pedidos por mes calcularlo al vuelo cuesta nada, y una
# columna se congelaria en lo que decia el payload el dia que se sincronizo por
# primera vez. Como el sync pisa raw_payload en cada corrida (sync_tiendanube
# _upsert_pedido), leerlo derivado es lo unico que refleja el estado de hoy.
DESPACHO_SI = 'si'
DESPACHO_NO = 'no'
# La venta de mostrador se entrega en el acto: no tiene despacho que reportar.
DESPACHO_MOSTRADOR = 'mostrador'
# Un pedido de canal externo cuyo payload no trae informacion de despacho.
# No es lo mismo que 'mostrador': ahi el dato no aplica, aca falta.
DESPACHO_SIN_DATO = 'sin_dato'

# Vocabulario de fulfillments[].status de Tiendanube. Fuente:
# https://tiendanube.github.io/api-documentation/resources/fulfillment-order
#
#   UNPACKED          preparacion no empezada
#   IN_PREPARATION    picking / packing / produccion
#   PACKED            listo para despachar o retirar, todavia en el local
#   DISPATCHED        despachado             <- salio
#   READY_FOR_PICKUP  listo para que lo retiren
#   DELIVERED         entregado              <- salio
#
# READY_FOR_PICKUP queda del lado de "no": la documentacion lo rechaza para el
# tipo de envio 'ship', y en el pickup no despachable (retiro en el local) el
# flujo va PACKED -> READY_FOR_PICKUP, o sea que la mercaderia sigue en poder
# del vendedor esperando que el cliente pase. Cuando el retiro es en sucursal
# de un correo, ese envio si es despachable y pasa por DISPATCHED.
FULFILLMENT_DESPACHADOS = {'DISPATCHED', 'DELIVERED'}

# Vocabulario de order.shipping_status, que es el respaldo cuando el pedido no
# trae fulfillments. Conviven dos generaciones de nombres en la documentacion
# ('shipped'/'fulfilled' para lo mismo), asi que se aceptan las dos. Fuente:
# https://tiendanube.github.io/api-documentation/resources/order y
# https://tiendanube.github.io/api-documentation/guides/multi-inventory
#
#   unpacked / unshipped / packed / partially_packed / partially_fulfilled -> no
#   shipped / fulfilled / delivered                                        -> si
SHIPPING_STATUS_DESPACHADOS = {'shipped', 'fulfilled', 'delivered'}


def _despacho_de_payload(payload):
    """DESPACHO_SI / DESPACHO_NO / DESPACHO_SIN_DATO a partir del payload crudo.

    Un pedido puede tener mas de un fulfillment (dos paquetes que salen de
    depositos distintos). Solo cuenta como despachado cuando salieron TODOS: si
    falta uno, la respuesta util a "¿me puedo olvidar de este pedido?" es que no.
    """
    if not isinstance(payload, dict):
        return DESPACHO_SIN_DATO

    # Los fulfillments son la fuente preferida: el shipping_status del pedido
    # esta deprecado del lado de Tiendanube y es un resumen de estos.
    fulfillments = payload.get('fulfillments')
    if isinstance(fulfillments, list) and fulfillments:
        # La API puede devolver solo los ids en vez de los objetos completos.
        # En ese caso aca no hay estado que leer y se cae al shipping_status.
        estados = [f.get('status') for f in fulfillments if isinstance(f, dict)]
        estados = [e for e in estados if e]
        if len(estados) == len(fulfillments):
            todos = all(str(e).upper() in FULFILLMENT_DESPACHADOS for e in estados)
            return DESPACHO_SI if todos else DESPACHO_NO

    shipping_status = payload.get('shipping_status')
    if shipping_status:
        return (DESPACHO_SI
                if str(shipping_status).lower() in SHIPPING_STATUS_DESPACHADOS
                else DESPACHO_NO)

    return DESPACHO_SIN_DATO


class Pedido(db.Model):
    """Un pedido tal como lo devuelve el canal, ya normalizado.

    Tambien guarda las ventas de mostrador cargadas a mano (canal 'manual'),
    que no vienen de ninguna API y por eso no tienen id_externo.
    """
    __tablename__ = 'pedido'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    canal_id = db.Column(db.Integer, db.ForeignKey('canal_venta.id'), nullable=False, index=True)
    # NULL solo en las ventas manuales: no hay id del lado de ningun canal.
    # La UNIQUE (canal_id, id_externo) las deja convivir porque en Postgres un
    # NULL no colisiona con otro NULL; para los canales externos el upsert
    # sigue exigiendo el id, asi que la deduplicacion no se afloja.
    id_externo = db.Column(db.String(100))
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
    # Lo que el envio le costo AL VENDEDOR (merchant_cost), que no es lo mismo
    # que total_envio (consumer_cost, lo que pago el comprador): la tienda
    # puede bonificar el envio y comerse la diferencia, o cobrar de mas. Sin
    # este campo el margen real de un pedido con envio no se puede calcular.
    #
    # Nullable a proposito, y es la unica diferencia de forma con total_envio:
    # NULL significa "Tiendanube no mando el dato", no "el envio salio gratis".
    # Un CERO aca seria una afirmacion sobre la plata que nadie hizo.
    #
    # No confundir con pedido_item.costo_unitario_snapshot: eso es el costo de
    # la MERCADERIA, esto es el costo del FLETE.
    costo_envio_vendedor = db.Column(db.Numeric(14, 2))
    total_impuestos = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    total = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    # Lo que la plataforma (Tiendanube, Mercado Libre) se queda por vender.
    # NO viene en el payload como linea aparte -- se verifico en
    # FASE-REPORTES-S3 -- asi que la carga Roman a mano desde el listado.
    #
    # Va a nivel PEDIDO y no a nivel linea a proposito: la comision depende de
    # la forma de venta, no del producto, y prorratearla entre las lineas de un
    # mismo pedido inventaria una precision que el dato no tiene.
    #
    # Nullable con el mismo criterio que costo_envio_vendedor: NULL es "todavia
    # no la cargue", CERO es "confirmado que no hubo comision" (una venta de
    # mostrador no paga ninguna). Un reporte que los confunda va a mostrar
    # margen de mas en cada pedido sin cargar.
    #
    # No confundir con pago.comision, que es la del PROCESADOR de pagos
    # (Mercado Pago) y pertenece a otro flujo: son dos mordidas distintas
    # sobre la misma venta y se cargan por caminos distintos.
    comision_plataforma = db.Column(db.Numeric(14, 2))
    # Texto libre que escribe quien carga la venta a mano ("cliente Juan Perez",
    # "pago la mitad ahora"). No lo llena ningun sync: para los canales
    # externos el equivalente ya viaja en raw_payload.
    nota = db.Column(db.Text)
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

    @property
    def estado_despacho(self):
        """¿Ya salio el pedido? Derivado de raw_payload, sin columna propia.

        No se confunde con `estado` / `estado_externo` (el vocabulario de
        Tiendanube para la venta: open/closed/cancelled, paid/...) ni con
        `total_envio`, que es plata. Un pedido pagado y cerrado puede no haber
        salido todavia.
        """
        if self.canal is not None and self.canal.tipo == 'manual':
            return DESPACHO_MOSTRADOR
        return _despacho_de_payload(self.raw_payload)


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


# FASE-CAJA-SOCIO-S1: el vocabulario de socios, en un solo lugar.
#
# La clave es lo que se guarda en cuenta_cobro.socio y lo que se compara en
# codigo; el valor es como se escribe en pantalla. Que sean dos cosas
# distintas es justamente el punto: hasta ahora "de quien es esta cuenta" se
# deducia leyendo cuenta_cobro.nombre ('Roman - Presencial y Tiendanube'), asi
# que renombrar la cuenta -- un campo de texto libre, sin ninguna regla --
# cambiaba en silencio a quien se le atribuia la facturacion. Con la clave
# aparte, el nombre vuelve a ser lo que dice ser: una etiqueta.
#
# El orden importa: es el orden en que salen los socios en el reporte de caja,
# y no depende de que cuenta se cargo primero ni de como se llame.
SOCIOS = OrderedDict([
    ('roman', 'Roman'),
    ('nachi', 'Nachi'),
])


class CuentaCobro(db.Model):
    """Cuenta donde entra la plata: Mercado Pago o Tiendanube Pagos.
    metodo_ingesta queda restringido a 'api' (sin archivo ni carga manual)."""
    __tablename__ = 'cuenta_cobro'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    nombre = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(30), nullable=False)  # 'mercadopago' | 'tiendanube_pagos'
    metodo_ingesta = db.Column(db.String(20), nullable=False, default='api', server_default='api')
    # El user_id que devuelve el OAuth de Mercado Pago, o sea el id de la
    # cuenta del lado del procesador. Se llama asi por simetria con
    # canal_venta.id_tienda_externo: "el id de esta cosa alla afuera".
    id_cuenta_externa = db.Column(db.String(100))
    # FASE-CAJA-SOCIO-S1: de que socio es esta cuenta, con vocabulario fijo
    # (ver SOCIOS). Reemplaza al parseo de `nombre`, que era texto libre.
    #
    # Nullable a proposito: una cuenta de cobro puede existir sin dueño
    # asignado -- una cuenta de prueba, o una que se conecte antes de decidir
    # de quien es. NULL significa "todavia no se dijo", y el reporte de caja lo
    # muestra en su propia fila en vez de repartir esa plata entre los socios
    # conocidos: plata sin dueño se ve, no se esconde.
    socio = db.Column(db.String(20))
    moneda = db.Column(db.String(3), nullable=False, default='ARS')
    saldo_actual = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_ultima_sync = db.Column(db.DateTime)

    empresa = db.relationship('Empresa', backref='cuentas_cobro')

    __table_args__ = (
        db.UniqueConstraint('empresa_id', 'nombre', name='uq_cuenta_cobro_empresa_nombre'),
        db.CheckConstraint("metodo_ingesta = 'api'", name='ck_cuenta_cobro_metodo_ingesta_api'),
        # El mismo vocabulario que valida `_validar_socio`, pero del lado de la
        # base. No es redundante: hoy las cuentas de cobro NO se cargan por
        # ninguna pantalla, se siembran desde una migracion con SQL crudo
        # (ver 14291f8d0459), y ese camino no pasa por el modelo. El CHECK es
        # lo unico que cubre al unico camino de carga que existe.
        db.CheckConstraint(
            "socio IS NULL OR socio IN ('roman', 'nachi')",
            name='ck_cuenta_cobro_socio_vocabulario'),
    )

    @property
    def etiqueta_socio(self):
        """Como se nombra esta cuenta cuando lo que importa es de quien es.

        'Roman' / 'Nachi' del vocabulario, y el nombre completo de la cuenta
        solo cuando no tiene socio asignado -- que es el unico caso donde el
        texto libre sigue siendo lo mejor que hay. Lo usa el selector "¿de qué
        cuenta?" del formulario de gasto (FASE-CAJA-GENERAL-S3): ahi la
        pregunta es de que socio, no como se llama la cuenta en el panel de
        Mercado Pago.
        """
        return SOCIOS.get(self.socio) or self.nombre

    @db.validates('socio')
    def _validar_socio(self, clave, valor):
        """El socio se escribe con una de las claves de SOCIOS, o no se escribe.

        Un typo ('Roman', 'romn') no puede quedar guardado: la cuenta saldria
        en su propia fila del reporte como si fuera un tercer socio, y nadie
        se enteraria hasta cuadrar la plata a mano. Preferimos que reviente en
        el momento de cargarla.
        """
        if valor is None or valor == '':
            return None
        if valor not in SOCIOS:
            raise ValueError(
                'socio invalido: %r. Los validos son %s.'
                % (valor, ', '.join(SOCIOS)))
        return valor


class CredencialCuentaCobro(db.Model):
    """Credenciales OAuth de una cuenta de cobro (FASE-MP-S1).

    Es la hermana de CredencialCanal, pero colgada de cuenta_cobro en vez de
    canal_venta: quien cobra no es necesariamente quien vende. La cuenta de
    Mercado Pago de Nachi recibe las ventas de Mercado Libre, y la de Roman
    recibe las presenciales y las liquidaciones de Tiendanube; son dos
    autorizaciones OAuth distintas, de dos personas distintas, sobre la misma
    aplicacion de Mercado Pago.

    Diferencia con Tiendanube, y el motivo de que aca si haya refresh_token y
    expira_en: el token de Mercado Pago vence (180 dias documentados). El
    refresco automatico NO esta implementado todavia; expira_en existe para
    poder avisar "reconecta esta cuenta" antes de fallar contra la API.

    Los dos tokens van cifrados con Fernet, misma CREDENTIALS_ENCRYPTION_KEY
    que credencial_canal (ver cripto.py). El refresh_token es tan sensible como
    el access_token -- sirve para fabricar access_tokens nuevos -- asi que se
    cifra igual, no en texto plano.
    """
    __tablename__ = 'credencial_cuenta_cobro'
    id = db.Column(db.Integer, primary_key=True)
    cuenta_cobro_id = db.Column(db.Integer, db.ForeignKey('cuenta_cobro.id'),
                                nullable=False, index=True)
    access_token_cifrado = db.Column(db.Text)
    refresh_token_cifrado = db.Column(db.Text)
    expira_en = db.Column(db.DateTime)
    actualizado_en = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)

    cuenta_cobro = db.relationship('CuentaCobro', backref='credenciales')

    __table_args__ = (
        # Una cuenta tiene UNA credencial vigente, no una pila de tokens
        # viejos. Reconectar pisa la fila; asi no hay que decidir cual de tres
        # tokens es el bueno.
        db.UniqueConstraint('cuenta_cobro_id', name='uq_credencial_cuenta_cobro_cuenta'),
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
    por eso pedido_id es opcional.

    Las ventas manuales tambien dejan su fila aca, con procesador='manual' e
    id_externo NULL: todavia no hay ninguna cuenta de cobro conectada, asi que
    el medio elegido es un dato descriptivo para conciliar mas adelante.
    """
    __tablename__ = 'pago'
    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido.id'), index=True)
    canal_id = db.Column(db.Integer, db.ForeignKey('canal_venta.id'), index=True)
    cuenta_cobro_id = db.Column(db.Integer, db.ForeignKey('cuenta_cobro.id'), index=True)
    liquidacion_id = db.Column(db.Integer, db.ForeignKey('liquidacion.id'), index=True)
    procesador = db.Column(db.String(30), nullable=False)  # 'mercadopago' | 'tiendanube_pagos' | 'manual'
    # NULL en los pagos manuales: no los emitio ningun procesador.
    id_externo = db.Column(db.String(100))
    metodo = db.Column(db.String(50))
    cuotas = db.Column(db.Integer)
    estado = db.Column(db.String(30), nullable=False, default='pendiente')
    moneda = db.Column(db.String(3), nullable=False, default='ARS')
    monto_bruto = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    # NULL = "todavia no se sabe cuanto cobro el procesador", que es distinto
    # de 0 = "no hubo comision". Un cobro en efectivo es 0; uno con tarjeta
    # queda NULL hasta que la conciliacion real traiga el numero.
    comision = db.Column(db.Numeric(14, 2))
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


# FASE-DEVOLUCIONES-S2: el vocabulario de `devolucion.estado`.
#
# Hasta esta slice la columna existia con default 'abierta' y ningun otro valor
# escrito en ningun lado, porque nadie escribia la tabla. Aca se fija el
# vocabulario minimo, y la distincion que importa es una sola: si la
# mercaderia YA volvio o todavia no.
#
#   abierta -> el evento esta registrado pero sin resolver. El stock NO se
#              toca: nada volvio al deposito todavia. Es el estado de un
#              contracargo en disputa, donde la plata esta trabada y la
#              mercaderia se la quedo el comprador.
#   cerrada -> resuelto y la mercaderia esta de vuelta. Es el unico estado que
#              mueve stock, y lo mueve UNA sola vez por cadena.
#
# TERMINAL quiere decir "la cadena llego a donde tenia que llegar", no "la fila
# es la ultima". Son dos cosas distintas por el append-only: una cadena
# reabierta despues de cerrada tiene una fila terminal en el medio y una fila
# vigente que no lo es.
DEVOLUCION_ABIERTA = 'abierta'
DEVOLUCION_CERRADA = 'cerrada'

# Los estados que dan por vuelta la mercaderia. Es una tupla y no un valor
# suelto porque el dia que aparezca un segundo estado terminal (una devolucion
# aceptada parcialmente, por ejemplo) el chequeo de "ya se movio el stock" no
# tiene que salir a buscarse por el codigo: se agrega aca y sigue funcionando.
ESTADOS_DEVOLUCION_TERMINALES = (DEVOLUCION_CERRADA,)


class Devolucion(db.Model):
    """Devoluciones y contracargos. APPEND-ONLY: nunca se hace UPDATE ni
    DELETE sobre una fila. Cada cambio de estado entra como fila nueva que
    apunta a la anterior via evento_previo_id; el estado vigente es la ultima
    fila de la cadena. Un contracargo revisado tres veces deja tres filas, y
    esa historia es justamente lo que hay que poder auditar.

    FASE-DEVOLUCIONES-S2 la bajo a nivel de ITEM. Hasta aca la tabla apuntaba
    solo a `pedido` y guardaba plata (`monto`, `comision_devuelta`): con eso se
    puede decir "de este pedido se devolvieron $12.000", pero no QUE volvio ni
    CUANTAS unidades, que es exactamente lo que hace falta para sumarle al
    stock. `pedido_item_id` + `cantidad` cierran ese hueco.

    POR QUE DOS COLUMNAS Y NO UNA TABLA HIJA

    Una `devolucion_item` colgando de una tabla append-only obliga a elegir
    entre dos cosas malas: copiar los items en cada cambio de estado (tres
    revisiones de un contracargo = tres juegos de items identicos, y ninguna
    forma de saber cual vale) o colgarlos solo de la primera fila de la cadena
    (y romper el invariante de que cada fila es el estado COMPLETO del evento).
    Con las columnas adentro no hay que elegir: cada fila se describe entera a
    si misma, que es justo lo que el append-only pide.

    Lo que se pierde es poder decir "estas dos lineas volvieron en el mismo
    acto": una devolucion de dos productos son dos cadenas que comparten
    `pedido_id` y `fecha_evento`. Nada de lo que hoy existe consume ese
    agrupamiento, y si algun dia hace falta, se agrega la tabla hija sin tener
    que migrar estas filas -- las columnas son nullable justamente por eso.

    LAS DOS VAN JUNTAS O NO VA NINGUNA

    Nullable no es "opcional cada una por su lado": el CHECK de abajo exige que
    o esten las dos o no este ninguna. Una fila con `pedido_item_id` y sin
    `cantidad` seria una devolucion que no dice cuanto volvio, y una con
    `cantidad` sin item seria una cantidad de nada.

    Que puedan faltar LAS DOS es a proposito y no un descuido: un contracargo
    (`tipo='contracargo'`) es un evento de PLATA -- al comprador le devolvieron
    el dinero y se quedo con el producto. Obligar ahi a una cantidad seria
    obligar a inventarla. Por eso `cantidad` no es NOT NULL a secas: dentro de
    una devolucion de mercaderia es obligatoria, y el CHECK la hace
    obligatoria; fuera de ese caso no hay nada que contar.
    """
    __tablename__ = 'devolucion'
    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido.id'), nullable=False, index=True)
    # La linea del pedido que volvio. NULL = el evento no es de mercaderia
    # (ver el docstring). Apunta a `pedido_item` y no a `producto` a proposito:
    # el item es el que sabe cuantas unidades se habian vendido, y contra ese
    # numero se valida que no se devuelva de mas.
    pedido_item_id = db.Column(db.Integer, db.ForeignKey('pedido_item.id'), index=True)
    # Unidades que volvieron. Entero, siempre positivo (lo garantiza el CHECK):
    # una devolucion negativa seria una venta, y para eso ya esta `pedido`.
    cantidad = db.Column(db.Integer)
    pago_id = db.Column(db.Integer, db.ForeignKey('pago.id'), index=True)
    evento_previo_id = db.Column(db.Integer, db.ForeignKey('devolucion.id'), index=True)
    tipo = db.Column(db.String(20), nullable=False)  # 'devolucion' | 'contracargo' | 'cancelacion'
    # Sin UNIQUE, y hoy no molesta porque ningun sync escribe esta tabla: la
    # unica escritura es la pantalla de carga manual. El dia que exista un sync
    # de devoluciones va a hacer falta la UNIQUE (o un hash_dedup, como
    # `movimiento_cuenta`) o reimportar el mismo refund lo va a duplicar --
    # y duplicarlo suma stock dos veces, no solo una fila de mas.
    id_externo = db.Column(db.String(100), index=True)
    motivo = db.Column(db.String(255))
    moneda = db.Column(db.String(3), nullable=False, default='ARS')
    monto = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    comision_devuelta = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    estado = db.Column(db.String(30), nullable=False, default=DEVOLUCION_ABIERTA)
    fecha_evento = db.Column(db.DateTime, nullable=False, index=True)
    raw_payload = db.Column(db.JSON)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    pedido = db.relationship('Pedido', backref='devoluciones')
    pedido_item = db.relationship('PedidoItem', backref='devoluciones')
    pago = db.relationship('Pago', backref='devoluciones')
    evento_previo = db.relationship('Devolucion', remote_side=[id], backref='eventos_siguientes')

    __table_args__ = (
        db.CheckConstraint(
            '(pedido_item_id IS NULL AND cantidad IS NULL)'
            ' OR (pedido_item_id IS NOT NULL AND cantidad IS NOT NULL AND cantidad > 0)',
            name='ck_devolucion_item_y_cantidad'),
    )


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

    # Quien apreto "Sincronizar" (FASE-AUDITORIA-S3). NULL no es un dato que
    # falta: es la respuesta "lo disparo el cron". El cron corre sin sesion y
    # sin request, asi que no hay ninguna persona a la que atribuirle la
    # corrida -- y confundir eso con "no se sabe" seria peor que no guardarlo.
    #
    # Esto NO es una fila de historial: la escritura del sync sigue afuera de
    # TABLAS_AUDITADAS (FASE-AUDITORIA-S1) porque no tiene actor humano. Lo
    # que tiene actor humano es el CLIC, y el clic vive aca.
    #
    # Nullable tambien por el mismo motivo que en `historial`: borrar una
    # cuenta no puede borrar la bitacora. Sin cascade, SQLAlchemy anula la FK
    # y la corrida sigue registrada.
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), index=True)

    canal = db.relationship('CanalVenta', backref='sync_logs')
    cuenta_cobro = db.relationship('CuentaCobro', backref='sync_logs')
    usuario = db.relationship('Usuario', backref='sync_logs')


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
