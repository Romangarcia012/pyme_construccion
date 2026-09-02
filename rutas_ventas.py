"""Rutas de ventas cargadas a mano.

FASE3-S4: la venta de mostrador. No viene de ninguna API -- la tipea el
vendedor en el momento -- pero termina en las mismas tres tablas que las de
Tiendanube (pedido / pedido_item / pago), para que despues un solo reporte
pueda sumar todos los canales sin tratar a este como un caso aparte.

Lo que NO hace esta slice: ninguna llamada HTTP, ninguna cuenta de cobro
conectada. El medio de cobro que se guarda es descriptivo, para poder conciliar
cuando exista la integracion con MercadoPago.

El blueprint se registra en app.py; ninguna ruta existente se toca.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from models import CanalVenta, Pago, Pedido, PedidoItem, Producto, db

ventas_bp = Blueprint('ventas', __name__, url_prefix='/pedidos')

TIPO_MANUAL = 'manual'
NOMBRE_CANAL_MANUAL = 'Venta manual / presencial'
MONEDA_DEFECTO = 'ARS'
PROCESADOR_MANUAL = 'manual'

# Una venta de mostrador nace cerrada: no hay estados intermedios como en un
# pedido online, que puede quedar esperando pago o despacho.
ESTADO_PEDIDO_MANUAL = 'completado'

# El medio es descriptivo. Efectivo se acredita en el acto y no paga comision;
# tarjeta y Mercado Pago quedan pendientes con la comision en NULL, porque el
# numero real recien aparece cuando el procesador liquide.
#
# OJO: estas dos reglas son una primera aproximacion. Cuando se construya la
# conciliacion contra MercadoPago hay que revisarlas -- en particular si
# 'pendiente' sigue siendo el estado correcto para una tarjeta que el cliente
# ya paso por el posnet, y quien pasa el pago a 'acreditado'.
MEDIOS_COBRO = [
    ('efectivo', 'Efectivo'),
    ('tarjeta', 'Tarjeta'),
    ('mercado_pago', 'Mercado Pago'),
]
MEDIOS_VALIDOS = {clave for clave, _ in MEDIOS_COBRO}
MEDIOS_SIN_COMISION = {'efectivo'}

# Como se muestra cada canal en el listado. El fallback es el nombre que tenga
# la fila, asi un canal futuro no aparece en blanco.
ETIQUETA_CANAL = {
    'tiendanube': 'Tiendanube',
    'mercadolibre': 'Mercado Libre',
    TIPO_MANUAL: 'Manual',
}


class DatosInvalidos(Exception):
    """Lo que el formulario trajo no arma una venta. El mensaje se le muestra
    a quien esta cargando, asi que se escribe en criollo."""


def _canal_manual():
    """El canal manual de la empresa del usuario.

    La migracion de esta slice lo sembro para las empresas que existian
    entonces; una empresa creada despues no lo tiene, asi que se crea al vuelo
    -- mismo criterio que usa rutas_integraciones con los canales externos.
    """
    canal = CanalVenta.query.filter_by(
        empresa_id=current_user.empresa_id, tipo=TIPO_MANUAL).first()
    if canal is None:
        canal = CanalVenta(empresa_id=current_user.empresa_id, tipo=TIPO_MANUAL,
                           nombre=NOMBRE_CANAL_MANUAL, activo=True)
        db.session.add(canal)
        db.session.flush()
    return canal


def _productos_de_la_empresa():
    return (Producto.query
            .filter_by(empresa_id=current_user.empresa_id, activo=True)
            .order_by(Producto.nombre)
            .all())


def _leer_fecha(texto):
    if not texto:
        return datetime.combine(date.today(), datetime.min.time())
    try:
        return datetime.strptime(texto.strip(), '%Y-%m-%d')
    except ValueError:
        raise DatosInvalidos('La fecha no es valida. Se espera formato AAAA-MM-DD.')


def _leer_cantidad(texto, fila):
    try:
        cantidad = int((texto or '').strip())
    except ValueError:
        raise DatosInvalidos('La cantidad del item %d no es un numero entero.' % fila)
    if cantidad <= 0:
        raise DatosInvalidos('La cantidad del item %d tiene que ser mayor a cero.' % fila)
    return cantidad


def _leer_precio(texto, fila):
    texto = (texto or '').strip().replace(',', '.')
    try:
        precio = Decimal(texto)
    except (InvalidOperation, ValueError):
        raise DatosInvalidos('El precio del item %d no es un numero.' % fila)
    if precio < 0:
        raise DatosInvalidos('El precio del item %d no puede ser negativo.' % fila)
    return precio.quantize(Decimal('0.01'))


def _leer_items(form, productos_por_sku):
    """Las lineas del formulario -> una lista de dicts ya validados.

    Las filas totalmente vacias se descartan en silencio: el formulario arranca
    con varias y nadie tiene por que llenarlas todas.
    """
    skus = form.getlist('sku')
    cantidades = form.getlist('cantidad')
    precios = form.getlist('precio_unitario')

    items = []
    for indice, sku in enumerate(skus):
        sku = (sku or '').strip()
        cantidad_txt = cantidades[indice] if indice < len(cantidades) else ''
        precio_txt = precios[indice] if indice < len(precios) else ''

        if not sku and not (cantidad_txt or '').strip() and not (precio_txt or '').strip():
            continue

        fila = indice + 1
        if not sku:
            raise DatosInvalidos('Falta elegir el producto del item %d.' % fila)

        producto = productos_por_sku.get(sku)
        if producto is None:
            raise DatosInvalidos(
                'No existe ningun producto con el codigo "%s" en tu catalogo.' % sku)

        cantidad = _leer_cantidad(cantidad_txt, fila)
        precio = _leer_precio(precio_txt, fila)
        items.append({
            'producto': producto,
            'cantidad': cantidad,
            'precio_unitario': precio,
            'subtotal': (precio * cantidad).quantize(Decimal('0.01')),
        })

    if not items:
        raise DatosInvalidos('La venta no tiene ningun item cargado.')
    return items


def _armar_venta(canal, items, fecha, medio, nota):
    """Escribe pedido + items + pago. No commitea: el commit lo hace la ruta,
    para que las tres tablas entren o no entren juntas."""
    total = sum((item['subtotal'] for item in items), Decimal('0.00'))

    pedido = Pedido(
        empresa_id=canal.empresa_id,
        canal_id=canal.id,
        id_externo=None,           # no hay id de ningun canal: la venta es interna
        fecha_pedido=fecha,
        estado=ESTADO_PEDIDO_MANUAL,
        moneda=MONEDA_DEFECTO,
        total_bruto=total,
        total_descuentos=Decimal('0.00'),
        total_envio=Decimal('0.00'),
        total_impuestos=Decimal('0.00'),
        total=total,
        nota=nota or None,
    )
    db.session.add(pedido)
    db.session.flush()

    for item in items:
        producto = item['producto']
        db.session.add(PedidoItem(
            pedido_id=pedido.id,
            producto_id=producto.id,
            sku_externo=producto.sku,
            descripcion=producto.nombre,
            cantidad=item['cantidad'],
            precio_unitario=item['precio_unitario'],
            descuento_unitario=Decimal('0.00'),
            subtotal=item['subtotal'],
            # Congela el costo de HOY, igual que el sync de Tiendanube. Queda
            # NULL si Roman todavia no cargo el costo de ese producto: es el
            # estado real, no un dato faltante que haya que rellenar con cero.
            costo_unitario_snapshot=producto.costo_unitario,
        ))

    en_efectivo = medio in MEDIOS_SIN_COMISION
    db.session.add(Pago(
        pedido_id=pedido.id,
        canal_id=canal.id,
        procesador=PROCESADOR_MANUAL,
        id_externo=None,
        metodo=medio,
        estado='acreditado' if en_efectivo else 'pendiente',
        moneda=MONEDA_DEFECTO,
        monto_bruto=total,
        # Efectivo: no hay procesador, la comision es 0 de verdad. Tarjeta y
        # Mercado Pago: NULL hasta que la conciliacion traiga el numero real.
        comision=Decimal('0.00') if en_efectivo else None,
        impuestos=Decimal('0.00'),
        monto_neto=total,
        fecha_pago=fecha,
        fecha_acreditacion=fecha if en_efectivo else None,
    ))
    db.session.flush()
    return pedido


@ventas_bp.route('/manual/nuevo', methods=['GET', 'POST'])
@login_required
def nueva_venta_manual():
    """Carga de una venta de mostrador."""
    productos = _productos_de_la_empresa()

    if request.method == 'GET':
        return render_template('venta_manual.html', productos=productos,
                               medios=MEDIOS_COBRO, hoy=date.today().isoformat(),
                               enviado={})

    enviado = {
        'fecha': (request.form.get('fecha') or '').strip(),
        'medio': (request.form.get('medio') or '').strip(),
        'nota': (request.form.get('nota') or '').strip(),
    }

    try:
        fecha = _leer_fecha(enviado['fecha'])
        medio = enviado['medio']
        if medio not in MEDIOS_VALIDOS:
            raise DatosInvalidos('Elegi un medio de cobro.')

        items = _leer_items(request.form, {p.sku: p for p in productos})
        pedido = _armar_venta(_canal_manual(), items, fecha, medio, enviado['nota'])
        db.session.commit()
    except DatosInvalidos as error:
        db.session.rollback()
        flash(str(error), 'error')
        return render_template('venta_manual.html', productos=productos,
                               medios=MEDIOS_COBRO, hoy=date.today().isoformat(),
                               enviado=enviado)
    except Exception:
        # Nada a medias: si algo se rompio despues del primer flush, el pedido
        # no puede quedar sin sus items o sin su pago.
        db.session.rollback()
        flash('No se pudo guardar la venta. Revisa los datos e intenta de nuevo.', 'error')
        return render_template('venta_manual.html', productos=productos,
                               medios=MEDIOS_COBRO, hoy=date.today().isoformat(),
                               enviado=enviado)

    flash('Venta registrada por %s %s.' % (MONEDA_DEFECTO, pedido.total), 'success')
    return redirect(url_for('ventas.listar_pedidos'))


@ventas_bp.route('/listar')
@login_required
def listar_pedidos():
    """Todo lo vendido, venga del canal que venga.

    Sin filtros ni paginacion todavia: es la pantalla minima para que Roman vea
    lo que cargo sin tener que entrar a Supabase.
    """
    pedidos = (Pedido.query
               .filter_by(empresa_id=current_user.empresa_id)
               .order_by(Pedido.fecha_pedido.desc(), Pedido.id.desc())
               .all())

    # El medio de cobro solo se muestra en las ventas manuales: es el unico
    # canal donde alguien lo eligio a mano. Un pedido puede tener mas de un
    # pago, asi que se listan todos los medios distintos.
    filas = []
    for pedido in pedidos:
        canal = pedido.canal
        tipo = canal.tipo if canal else None
        medios = []
        if tipo == TIPO_MANUAL:
            vistos = []
            for pago in pedido.pagos:
                etiqueta = dict(MEDIOS_COBRO).get(pago.metodo, pago.metodo)
                if etiqueta and etiqueta not in vistos:
                    vistos.append(etiqueta)
            medios = vistos
        filas.append({
            'fecha': pedido.fecha_pedido,
            'canal': ETIQUETA_CANAL.get(tipo, canal.nombre if canal else '-'),
            'moneda': pedido.moneda,
            'total': pedido.total,
            'estado': pedido.estado,
            'medios': ', '.join(medios),
            'nota': pedido.nota,
        })

    return render_template('pedidos_listar.html', filas=filas)
