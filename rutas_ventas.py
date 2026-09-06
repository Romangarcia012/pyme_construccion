"""Rutas de ventas cargadas a mano.

FASE3-S4: la venta de mostrador. No viene de ninguna API -- la tipea el
vendedor en el momento -- pero termina en las mismas tres tablas que las de
Tiendanube (pedido / pedido_item / pago), para que despues un solo reporte
pueda sumar todos los canales sin tratar a este como un caso aparte.

El medio de cobro que se guarda es descriptivo, para poder conciliar cuando
exista la integracion con MercadoPago; ninguna cuenta de cobro conectada
interviene aca.

FASE-STOCK-S1 le agrego dos cosas a la venta: descuenta `producto.stock` en la
misma transaccion, y despues de commitear le avisa a Tiendanube el numero nuevo
(ver `stock_tiendanube.py`). Esa es la unica llamada HTTP de este modulo, y no
puede voltear una venta ya guardada: si falla, se avisa y se sigue.

FASE-CAJA-SOCIO-S2 le sumo al listado una correccion puntual: a que cuenta se
le atribuye un pedido, cuando no es la que le tocaria por su canal. Es una
excepcion editable a mano, no una regla nueva.

FASE-CAJA-SOCIO-S3 le agrega ese mismo control al alta: se puede elegir la
cuenta en el momento de cargar la venta, en vez de cargarla y despues ir a
corregirla al listado. Es el MISMO campo (`cuenta_cobro_override_id`) y la
misma validacion; lo unico que cambia es que ahora hay dos lugares donde
setearlo. El default sigue siendo vacio -- "la que le toca por canal" -- asi
que quien no toca el selector carga exactamente la misma venta que antes.

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
from sqlalchemy.orm import joinedload

import stock_tiendanube
from models import (
    DESPACHO_MOSTRADOR,
    DESPACHO_NO,
    DESPACHO_SI,
    DESPACHO_SIN_DATO,
    CanalVenta,
    CuentaCobro,
    Devolucion,
    Pago,
    Pedido,
    PedidoItem,
    Producto,
    db,
)

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

# Como se lee en pantalla el estado de despacho que deriva `Pedido`. El guion
# es para el pedido de un canal externo cuyo payload no trajo el dato: decir
# "No" ahi seria afirmar algo que no se sabe.
ETIQUETA_DESPACHO = {
    DESPACHO_SI: 'Sí',
    DESPACHO_NO: 'No',
    DESPACHO_MOSTRADOR: 'Mostrador',
    DESPACHO_SIN_DATO: '—',
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


def _descontar_stock(items):
    """Resta del stock lo que se acaba de vender. FASE-STOCK-S1.

    Corre dentro de la misma transaccion que el pedido: o se guarda la venta
    con su descuento, o no se guarda nada.

    Tres reglas:

    - `stock` NULL es "nadie lleva la cuenta de este producto", no "hay cero".
      Esos productos no se tocan (y despues tampoco se empujan a Tiendanube).
    - Un resultado negativo se guarda como 0. La venta ya ocurrio fisicamente:
      el mostrador entrego la mercaderia y no hay forma de bloquearla desde
      aca. Un stock negativo no describiria nada real, un 0 si.
    - Ese caso igual se devuelve para avisarlo: vender mas de lo que el sistema
      creia tener significa que el numero venia mal, y alguien tiene que
      enterarse.

    Devuelve (sobrevendidos, producto_ids): los nombres a avisar y los ids
    cuyo stock cambio, que son los unicos que hay que empujar a la tienda.
    """
    sobrevendidos = []
    producto_ids = []

    for item in items:
        producto = item['producto']
        if producto.stock is None:
            continue

        restante = producto.stock - item['cantidad']
        if restante < 0:
            restante = 0
            if producto.nombre not in sobrevendidos:
                sobrevendidos.append(producto.nombre)

        producto.stock = restante
        if producto.id not in producto_ids:
            producto_ids.append(producto.id)

    return sobrevendidos, producto_ids


def _armar_venta(canal, items, fecha, medio, nota, cuenta_override_id=None,
                 es_regalo=False):
    """Escribe pedido + items + pago, y descuenta el stock vendido.

    No commitea: el commit lo hace la ruta, para que las cuatro escrituras
    entren o no entren juntas.

    `cuenta_override_id` llega ya validado desde la ruta y por defecto es None,
    que es el caso normal: la venta cae en la cuenta que dice su canal.

    `es_regalo` (FASE-CAJA-SOCIO-S5) marca que esto no es una venta. Lo unico
    que cambia es la marca: el pedido se escribe igual, con sus items y su
    precio simbolico, y el stock se descuenta igual -- la mercaderia se va del
    deposito lo mismo. Quien decide que hacer con esa marca es cada reporte;
    hoy la mira /reportes/caja-socio, para no contar como facturacion una
    plata que nunca entro.
    """
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
        # El envio es CERO de verdad, no un dato que falte: la venta de
        # mostrador se entrega en persona, no hay flete que pagar ni que
        # cobrar. Por eso van los dos montos en 0.00 y no en NULL --
        # `costo_envio_vendedor` es nullable justamente para poder decir "no
        # se sabe" cuando el payload de un canal no trae el dato, y aca se
        # sabe. Dejarlo en NULL sacaba a toda venta presencial del reporte de
        # margen (FASE-REPORTES-S3-MARGEN), que exige los tres componentes de
        # costo cargados y no tiene por que adivinar este.
        costo_envio_vendedor=Decimal('0.00'),
        total_impuestos=Decimal('0.00'),
        total=total,
        # Mismo criterio que el envio, y por la misma razon: la comision de
        # plataforma es CERO de verdad en una venta de mostrador -- no hay
        # Tiendanube ni Mercado Libre cobrandose nada por una venta que se
        # hizo en el local. `comision_plataforma` es nullable para poder decir
        # "todavia no la cargue" en los pedidos de un canal, donde el numero
        # lo carga Roman mirando la liquidacion; aca no hay liquidacion que
        # mirar. Dejarlo en NULL mandaba toda venta presencial a "Sin margen:
        # falta la comision de plataforma" (FASE-REPORTES-S3-MARGEN), sin
        # ninguna pantalla que lo destrabara con sentido.
        #
        # No confundir con `pago.comision`, mas abajo: esa es la del PROCESADOR
        # de pagos y sigue su propia regla.
        comision_plataforma=Decimal('0.00'),
        # FASE-CAJA-SOCIO-S3. None salvo que quien carga la venta haya elegido
        # otra cuenta en el alta. Se escribe aca, en el mismo INSERT que el
        # resto de la venta: no hay ningun momento en el que el pedido exista
        # atribuido al socio equivocado.
        cuenta_cobro_override_id=cuenta_override_id,
        # FASE-CAJA-SOCIO-S5. Se escribe en el mismo INSERT que el resto: un
        # regalo no pasa por un momento en el que figure como venta.
        es_regalo=bool(es_regalo),
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

    sobrevendidos, producto_ids = _descontar_stock(items)

    db.session.flush()
    return pedido, sobrevendidos, producto_ids


def _cuenta_por_canal_manual():
    """Como se llama la cuenta donde cae la venta manual si nadie elige otra.

    Sirve para nombrarla dentro de la opcion "Por canal (...)" del selector,
    igual que en el listado: elegir otra tiene que ser una decision consciente
    y no a ciegas.

    Consulta sin crear: `_canal_manual()` siembra el canal cuando falta, y
    dibujar un formulario no es motivo para escribir una fila.
    """
    canal = CanalVenta.query.filter_by(
        empresa_id=current_user.empresa_id, tipo=TIPO_MANUAL).first()
    if canal is None or canal.cuenta_cobro is None:
        return None
    return canal.cuenta_cobro.etiqueta_socio


def _contexto_formulario(productos, enviado):
    """Todo lo que la plantilla del alta necesita, en un solo lugar.

    Tres render_template distintos dibujan el mismo formulario: el GET, el
    error de datos y el error inesperado. Con el contexto armado en cada uno,
    el dia que se agrega un campo -- como el selector de cuenta de esta
    slice -- se agrega en dos de los tres y el tercero vuelve mutilado.
    """
    return {
        'productos': productos,
        'medios': MEDIOS_COBRO,
        'hoy': date.today().isoformat(),
        'enviado': enviado,
        # FASE-CAJA-SOCIO-S3. Lo unico que suma el selector de cuenta: entre
        # cuales elegir, y como se llama la que le tocaria por canal.
        'cuentas': _cuentas_de_cobro(current_user.empresa_id),
        'cuenta_por_canal': _cuenta_por_canal_manual(),
    }


@ventas_bp.route('/manual/nuevo', methods=['GET', 'POST'])
@login_required
def nueva_venta_manual():
    """Carga de una venta de mostrador."""
    productos = _productos_de_la_empresa()

    if request.method == 'GET':
        return render_template('venta_manual.html',
                               **_contexto_formulario(productos, {}))

    enviado = {
        'fecha': (request.form.get('fecha') or '').strip(),
        'medio': (request.form.get('medio') or '').strip(),
        'nota': (request.form.get('nota') or '').strip(),
        # FASE-CAJA-SOCIO-S3. Vacio es "la que le toca por canal", que es el
        # caso normal. Se lee aparte de 'medio' porque son dos preguntas
        # distintas: el medio es COMO pago el cliente, esto es A QUIEN se le
        # atribuye la plata.
        'cuenta': (request.form.get('cuenta_cobro_override') or '').strip(),
        # FASE-CAJA-SOCIO-S5. Un checkbox sin tildar no viaja en el POST, asi
        # que la ausencia ES el "no" -- no hace falta validar nada ni hay
        # forma de que llegue un tercer valor.
        'regalo': bool(request.form.get('es_regalo')),
    }

    try:
        fecha = _leer_fecha(enviado['fecha'])
        medio = enviado['medio']
        if medio not in MEDIOS_VALIDOS:
            raise DatosInvalidos('Elegi un medio de cobro.')

        # FASE-CAJA-SOCIO-S3. Va antes de escribir nada, y con la misma lectura
        # que usa el listado (`_leer_cuenta_override`, mas abajo en este
        # modulo): una cuenta que no es de esta empresa voltea la venta entera
        # en vez de guardarla atribuida a cualquiera.
        cuenta_override_id = _leer_cuenta_override(
            enviado['cuenta'], 'que estas cargando',
            {cuenta.id for cuenta in _cuentas_de_cobro(current_user.empresa_id)})

        items = _leer_items(request.form, {p.sku: p for p in productos})
        pedido, sobrevendidos, producto_ids = _armar_venta(
            _canal_manual(), items, fecha, medio, enviado['nota'],
            cuenta_override_id, enviado['regalo'])
        db.session.commit()
        total_vendido = pedido.total
    except (DatosInvalidos, CuentaInvalida) as error:
        # Las dos son lo mismo para quien esta cargando: el formulario vuelve
        # con lo que habia escrito y el motivo arriba. Se atrapan juntas para
        # no duplicar el render; CuentaInvalida vive con el resto del override.
        db.session.rollback()
        flash(str(error), 'error')
        return render_template('venta_manual.html',
                               **_contexto_formulario(productos, enviado))
    except Exception:
        # Nada a medias: si algo se rompio despues del primer flush, el pedido
        # no puede quedar sin sus items o sin su pago.
        db.session.rollback()
        flash('No se pudo guardar la venta. Revisa los datos e intenta de nuevo.', 'error')
        return render_template('venta_manual.html',
                               **_contexto_formulario(productos, enviado))

    # FASE-CAJA-SOCIO-S5. El mensaje dice cual de las dos cosas se guardo: un
    # "Venta registrada por $4" despues de tildar Regalo haria dudar de si la
    # marca entro, que es justo lo que no se quiere despues de cargar algo raro.
    if enviado['regalo']:
        flash('Regalo registrado. El stock se descontó y no cuenta como '
              'facturación.', 'success')
    else:
        flash('Venta registrada por %s %s.' % (MONEDA_DEFECTO, total_vendido), 'success')

    # Todo lo que sigue pasa DESPUES del commit y a proposito: la venta ya esta
    # guardada y ninguno de estos avisos la puede deshacer.
    if sobrevendidos:
        flash('Se vendió más de lo que figuraba en stock para: %s. El stock quedó '
              'en 0; revisá el conteo real.' % ', '.join(sobrevendidos), 'warning')

    # El push a Tiendanube es lo unico de esta ruta que sale a internet. Se
    # envuelve entero por las dudas: un fallo que stock_tiendanube no haya
    # previsto no puede terminar en un 500 sobre una venta ya cobrada.
    try:
        fallidos = stock_tiendanube.empujar_stock(current_user.empresa_id, producto_ids)
    except Exception:  # noqa: BLE001
        # El rollback va primero: si lo que se rompio dejo la sesion sucia,
        # leer los nombres para el aviso volveria a fallar. La venta ya esta
        # commiteada, asi que no hay nada suyo que perder.
        db.session.rollback()
        fallidos = [p.nombre for p in productos if p.id in producto_ids]

    if fallidos:
        flash('Venta guardada, pero no se pudo actualizar el stock en Tiendanube '
              'para: %s — revisalo a mano.' % ', '.join(fallidos), 'warning')

    return redirect(url_for('ventas.listar_pedidos'))


def _medio_de_cobro(pedido, tipo_canal):
    """Con que se cobro la venta, en criollo.

    Dos origenes distintos porque el dato vive en dos lados: la venta de
    mostrador escribe una fila en `pago` con el medio que eligio el vendedor;
    el pedido de Tiendanube no deja fila en `pago` (el sync no las crea), pero
    trae el nombre de la pasarela dentro de raw_payload.
    """
    if tipo_canal == TIPO_MANUAL:
        vistos = []
        for pago in pedido.pagos:
            etiqueta = dict(MEDIOS_COBRO).get(pago.metodo, pago.metodo)
            if etiqueta and etiqueta not in vistos:
                vistos.append(etiqueta)
        return ', '.join(vistos)

    payload = pedido.raw_payload
    if isinstance(payload, dict):
        return payload.get('gateway_name') or payload.get('gateway') or ''
    return ''


def _cuentas_de_cobro(empresa_id):
    """Las cuentas de cobro de la empresa, para el selector de reasignacion.

    Mismo criterio que el selector de gasto (`app._cuentas_de`): sin filtrar
    por `activo`, porque una cuenta apagada que ya tiene pedidos reasignados
    tiene que seguir apareciendo -- si desapareciera del <select>, el proximo
    guardado se llevaria puesta la correccion.
    """
    return (CuentaCobro.query
            .filter_by(empresa_id=empresa_id)
            .order_by(CuentaCobro.id)
            .all())


@ventas_bp.route('/listar')
@login_required
def listar_pedidos():
    """Todo lo vendido, venga del canal que venga: una fila por venta.

    FASE-REPORTES-S2-MERGE: hasta esta slice esto eran dos pantallas -- este
    listado operativo (cinco columnas y el boton de venta nueva) y un
    /pedidos/resumen de solo lectura que repetia esas cinco y agregaba cliente y
    despacho. Compartian consulta y orden, asi que iban a divergir por accidente
    y no por diseno. Quedo una sola con las siete columnas y el boton arriba: la
    vista de solo lectura no le saca nada operativo a nadie.

    Sin filtros ni paginacion, igual que antes: el orden es fijo, lo ultimo
    vendido primero.

    El despacho no se consulta a Tiendanube aca: sale de raw_payload, que el
    sync pisa en cada corrida (ver `Pedido.estado_despacho`).
    """
    pedidos = (Pedido.query
               .options(joinedload(Pedido.canal).joinedload(CanalVenta.cuenta_cobro),
                        joinedload(Pedido.cuenta_cobro_override),
                        joinedload(Pedido.pagos))
               .filter_by(empresa_id=current_user.empresa_id)
               .order_by(Pedido.fecha_pedido.desc(), Pedido.id.desc())
               .all())

    cuentas = _cuentas_de_cobro(current_user.empresa_id)

    filas = []
    for pedido in pedidos:
        canal = pedido.canal
        tipo = canal.tipo if canal else None
        despacho = pedido.estado_despacho
        cuenta_del_canal = canal.cuenta_cobro if canal is not None else None
        filas.append({
            # El id viaja al formulario de comision: es lo que aparea cada
            # input con su pedido, igual que el sku en el listado de costos.
            'id': pedido.id,
            'fecha': pedido.fecha_pedido,
            'canal': ETIQUETA_CANAL.get(tipo, canal.nombre if canal else '-'),
            # El pedido online trae el nombre del comprador; la venta de
            # mostrador no pide ninguno, y la nota es texto libre que puede no
            # ser un nombre. Antes que inventar un cliente, va el guion.
            'cliente': pedido.comprador_nombre or '',
            'moneda': pedido.moneda,
            'total': pedido.total,
            'medio': _medio_de_cobro(pedido, tipo),
            'estado': pedido.estado,
            'despacho': despacho,
            'despacho_etiqueta': ETIQUETA_DESPACHO.get(despacho, despacho),
            'nota': pedido.nota,
            # Crudo, sin formatear: NULL tiene que llegar a la plantilla como
            # None para que el input quede VACIO y no muestre un 0 inventado.
            'comision_plataforma': pedido.comision_plataforma,
            # FASE-CAJA-SOCIO-S2. Los dos van juntos a proposito: el selector
            # tiene que poder decir "por canal (Roman)" en la opcion vacia, y
            # para eso hace falta saber cual seria la cuenta si nadie corrigiera
            # nada. None en el override es lo normal, no un faltante.
            'cuenta_override_id': pedido.cuenta_cobro_override_id,
            'cuenta_por_canal': (cuenta_del_canal.etiqueta_socio
                                 if cuenta_del_canal is not None else None),
            # FASE-CAJA-SOCIO-S5. Se muestra en TODA fila, no solo en las
            # manuales: un pedido de un canal externo tambien puede terminar
            # siendo un regalo (un canje, una reposicion sin cargo), y
            # esconder el checkbox ahi obligaria a volver a la consola.
            'es_regalo': pedido.es_regalo,
            # FASE-VENTAS-EDITAR-S1. Lo que decide si la fila muestra el boton
            # de editar. Las dos condiciones a la vez, igual que la ruta: la
            # plantilla no puede ofrecer un boton que despues rebota.
            'es_manual': tipo == TIPO_MANUAL and pedido.id_externo is None,
        })

    # Cuantas ventas estan esperando salir. Es lo unico que se cuenta arriba
    # porque es la unica pregunta que se hace mirando esta pantalla de apuro.
    pendientes = sum(1 for fila in filas if fila['despacho'] == DESPACHO_NO)

    return render_template('pedidos_listar.html', filas=filas,
                           pendientes=pendientes, cuentas=cuentas)


# --------------------------------------------------------------------------
# FASE-REPORTES-S3-COMISION: lo que la plataforma se queda por vender
# --------------------------------------------------------------------------
# La ultima pieza que falta para el margen. Tiendanube y Mercado Libre cobran
# una comision por venta que NO viaja en el payload como linea aparte -- se
# verifico en FASE-REPORTES-S3 -- asi que no hay nada que sincronizar: la carga
# Roman a mano, mirando la liquidacion del canal.
#
# Va por PEDIDO, no por linea de producto: la comision depende de la forma de
# venta, no del producto, y repartirla entre las lineas de un pedido inventaria
# una precision que el dato no tiene.
#
# Y no hay sugerencia posible, a diferencia del costo de producto (donde
# Tiendanube manda un `cost` parcial): el input arranca vacio y se queda vacio
# hasta que alguien lo llene.


class ComisionInvalida(Exception):
    """Lo que vino en el formulario no es una comision. El mensaje se le
    muestra a quien la estaba cargando, asi que se escribe en criollo."""


def _etiqueta_pedido(pedido):
    """Como nombrar un pedido en un mensaje de error.

    El numero del canal es lo que Roman tiene delante cuando mira la
    liquidacion; la venta de mostrador no tiene ninguno y cae en la fecha.
    """
    if pedido.numero_externo:
        return '#%s' % pedido.numero_externo
    if pedido.fecha_pedido:
        return 'del %s' % pedido.fecha_pedido.strftime('%d/%m/%Y')
    return 'id %s' % pedido.id


def _leer_comision(texto, etiqueta):
    """El texto del input -> Decimal con dos decimales, o None si vino vacio.

    Mismo criterio que `rutas_productos._leer_costo`, y por el mismo motivo: el
    vacio es un valor con significado, no un error. Borrar el input es como se
    saca una comision mal cargada y se vuelve a "no la se". Guardar 0 en su
    lugar seria afirmar que el canal no cobro nada por esa venta, y un reporte
    de margen leeria esa afirmacion como buena.

    La coma se acepta como decimal porque es lo que tipea cualquiera aca.
    """
    texto = (texto or '').strip().replace(',', '.')
    if not texto:
        return None
    try:
        comision = Decimal(texto)
    except (InvalidOperation, ValueError):
        raise ComisionInvalida('La comision del pedido %s no es un numero.' % etiqueta)
    # NaN e Infinity pasan por Decimal() sin quejarse y romperian recien contra
    # la base, con la transaccion ya a medias.
    if not comision.is_finite():
        raise ComisionInvalida('La comision del pedido %s no es un numero.' % etiqueta)
    if comision < 0:
        raise ComisionInvalida(
            'La comision del pedido %s no puede ser negativa.' % etiqueta)
    return comision.quantize(Decimal('0.01'))


@ventas_bp.route('/comisiones', methods=['POST'])
@login_required
def guardar_comisiones():
    """Guarda las comisiones de plataforma que se cargaron en el listado.

    Es una sola tanda para toda la pantalla y es todo o nada, igual que los
    costos de producto: si una fila trae basura no se guarda ninguna y el
    formulario vuelve con el error. Guardar las buenas y descartar la mala
    dejaria la pantalla mostrando un exito parcial que nadie pidio.

    Lo que esta ruta NO toca: `pago.comision`. Esa es la mordida del PROCESADOR
    de pagos y se llena sola desde el sync de Mercado Pago; esta es la del
    CANAL de venta. Sobre la misma venta pueden convivir las dos.
    """
    # El filtro por empresa es lo unico que impide que un pedido ajeno entre
    # por el formulario: lo que llega son cadenas que mando el cliente.
    pedidos = {pedido.id: pedido for pedido in
               Pedido.query.filter_by(empresa_id=current_user.empresa_id).all()}

    ids = request.form.getlist('pedido_id')
    comisiones = request.form.getlist('comision_plataforma')

    try:
        cambios = []
        for indice, crudo in enumerate(ids):
            try:
                pedido_id = int((crudo or '').strip())
            except (TypeError, ValueError):
                continue
            pedido = pedidos.get(pedido_id)
            if pedido is None:
                # Una fila que no corresponde a un pedido de la empresa se
                # ignora en silencio en vez de voltear la tanda: el listado se
                # arma desde el servidor, asi que esto solo pasa si algo cambio
                # entre que se abrio la pantalla y se apreto guardar.
                continue
            texto = comisiones[indice] if indice < len(comisiones) else ''
            comision = _leer_comision(texto, _etiqueta_pedido(pedido))
            if comision != pedido.comision_plataforma:
                cambios.append((pedido, comision))

        # Recien se escribe cuando TODAS las filas pasaron la validacion: con
        # la asignacion adentro del bucle de arriba, una fila mala mas abajo
        # dejaria las anteriores ya modificadas en la sesion.
        for pedido, comision in cambios:
            pedido.comision_plataforma = comision
        db.session.commit()
    except ComisionInvalida as error:
        db.session.rollback()
        flash(str(error), 'error')
        return redirect(url_for('ventas.listar_pedidos'))
    except Exception:  # noqa: BLE001
        db.session.rollback()
        flash('No se pudieron guardar las comisiones. Revisa los datos e '
              'intenta de nuevo.', 'error')
        return redirect(url_for('ventas.listar_pedidos'))

    if cambios:
        flash('Se actualizo la comision de %d pedido%s.'
              % (len(cambios), '' if len(cambios) == 1 else 's'), 'success')
    else:
        flash('No hubo cambios que guardar.', 'warning')
    return redirect(url_for('ventas.listar_pedidos'))


# --------------------------------------------------------------------------
# FASE-CAJA-SOCIO-S2: corregir a que cuenta va un pedido puntual
# --------------------------------------------------------------------------
# La regla general no se toca y no vive aca: cada canal cobra en la cuenta que
# dice `canal_venta.cuenta_cobro_id`, y la venta manual cae en la de Roman
# salvo que se diga otra cosa.
#
# FASE-CAJA-SOCIO-S3: el alta de venta manual pasa a preguntar la cuenta, con
# este mismo campo y esta misma lectura (`_leer_cuenta_override`). Esta
# pantalla NO se reemplaza: sigue siendo el unico lugar donde corregir una
# venta YA cargada -- las de los canales externos, que no pasan por ningun
# formulario, y las manuales donde se eligio mal.
#
# Lo que esta pantalla permite es la EXCEPCION: tres ventas manuales cargadas
# con montos agregados entraron todas por el canal manual, y una de ellas
# ("ventas Meli", $84.627,70) en la realidad la cobro Nachi. Sin esto habria
# que elegir entre dos mentiras -- cambiarle el canal al pedido, o cambiarle la
# cuenta al canal manual y mover TODAS las presenciales de socio.
#
# Vacio es el valor normal y significa "la que le toca por canal". No es un
# campo que haya que completar en cada fila: se toca el dia que hay algo que
# corregir, y el resto del tiempo se ignora.


class CuentaInvalida(Exception):
    """Lo que vino en el formulario no es una cuenta de esta empresa. El
    mensaje se le muestra a quien estaba corrigiendo, asi que va en criollo."""


def _leer_cuenta_override(texto, etiqueta, cuentas_validas):
    """El value del <select> -> id de cuenta, o None si es "por canal".

    Mismo criterio que `_leer_comision`: el vacio es un valor con significado
    -- "sacame la correccion, que valga la regla del canal" -- y no un error.

    La validacion contra `cuentas_validas` es lo unico que impide reasignar un
    pedido a la cuenta de otra empresa: lo que llega es una cadena que mando el
    cliente, y el <select> del servidor no es ninguna garantia.
    """
    texto = (texto or '').strip()
    if not texto:
        return None
    try:
        cuenta_id = int(texto)
    except (TypeError, ValueError):
        raise CuentaInvalida(
            'La cuenta del pedido %s no es una cuenta valida.' % etiqueta)
    if cuenta_id not in cuentas_validas:
        raise CuentaInvalida(
            'La cuenta elegida para el pedido %s no es de esta empresa.'
            % etiqueta)
    return cuenta_id


@ventas_bp.route('/cuentas', methods=['POST'])
@login_required
def guardar_cuentas():
    """Guarda las reasignaciones de cuenta cargadas en el listado.

    Una sola tanda para toda la pantalla y todo o nada, igual que las
    comisiones: si una fila trae basura no se guarda ninguna y el formulario
    vuelve con el error. Un exito parcial dejaria a alguien creyendo que
    corrigio dos ventas cuando corrigio una.

    Va por su propia ruta y no dentro de `guardar_comisiones` porque son dos
    cosas distintas sobre el mismo pedido: la comision es plata que se carga
    mirando la liquidacion del canal; esto es a quien se le atribuye la venta.
    Mezclarlas obligaria a guardar las dos para tocar una.

    Lo que esta ruta NO toca: el canal del pedido. La venta siguio entrando por
    donde entro, y el reporte de caja por socio sigue mostrando de que canal
    vino aunque la plata se le cuente a otro socio.
    """
    # El filtro por empresa es lo unico que impide que un pedido ajeno entre
    # por el formulario, igual que en las comisiones.
    pedidos = {pedido.id: pedido for pedido in
               Pedido.query.filter_by(empresa_id=current_user.empresa_id).all()}
    cuentas_validas = {cuenta.id for cuenta
                       in _cuentas_de_cobro(current_user.empresa_id)}

    ids = request.form.getlist('pedido_id')
    elegidas = request.form.getlist('cuenta_cobro_override')

    try:
        cambios = []
        for indice, crudo in enumerate(ids):
            try:
                pedido_id = int((crudo or '').strip())
            except (TypeError, ValueError):
                continue
            pedido = pedidos.get(pedido_id)
            if pedido is None:
                # Una fila que no corresponde a un pedido de la empresa se
                # ignora en silencio en vez de voltear la tanda: el listado se
                # arma desde el servidor, asi que esto solo pasa si algo cambio
                # entre que se abrio la pantalla y se apreto guardar.
                continue
            texto = elegidas[indice] if indice < len(elegidas) else ''
            cuenta_id = _leer_cuenta_override(texto, _etiqueta_pedido(pedido),
                                              cuentas_validas)
            if cuenta_id != pedido.cuenta_cobro_override_id:
                cambios.append((pedido, cuenta_id))

        # Recien se escribe cuando TODAS las filas pasaron la validacion, por
        # lo mismo que en las comisiones: con la asignacion adentro del bucle,
        # una fila mala mas abajo dejaria las anteriores ya modificadas.
        for pedido, cuenta_id in cambios:
            pedido.cuenta_cobro_override_id = cuenta_id
        db.session.commit()
    except CuentaInvalida as error:
        db.session.rollback()
        flash(str(error), 'error')
        return redirect(url_for('ventas.listar_pedidos'))
    except Exception:  # noqa: BLE001
        db.session.rollback()
        flash('No se pudieron guardar las cuentas. Revisa los datos e '
              'intenta de nuevo.', 'error')
        return redirect(url_for('ventas.listar_pedidos'))

    if cambios:
        flash('Se reasigno la cuenta de %d pedido%s.'
              % (len(cambios), '' if len(cambios) == 1 else 's'), 'success')
    else:
        flash('No hubo cambios que guardar.', 'warning')
    return redirect(url_for('ventas.listar_pedidos'))


# --------------------------------------------------------------------------
# FASE-CAJA-SOCIO-S5: esto fue un regalo, no una venta
# --------------------------------------------------------------------------
# La tercera cosa que se corrige por tanda desde este listado, y por su propia
# ruta por lo mismo que las otras dos: son preguntas distintas sobre el mismo
# pedido y guardar una no tiene por que tocar las demas.
#
# No hay validacion de vocabulario que hacer -- un checkbox tiene dos estados
# y los dos son validos --, asi que no hay excepcion propia. Lo unico que se
# valida es lo de siempre: que el pedido sea de esta empresa.


@ventas_bp.route('/regalos', methods=['POST'])
@login_required
def guardar_regalos():
    """Guarda que pedidos son regalos y cuales no.

    Todo o nada como las otras dos tandas, aunque aca no haya nada que pueda
    fallar por dato invalido: lo que se juega es la consistencia de la
    pantalla, no la validacion.

    LO QUE NO HACE, Y ES A PROPOSITO

    Marcar un pedido como regalo no devuelve el stock ni borra su pago: la
    mercaderia salio del deposito igual y el pedido siguio existiendo. Y
    desmarcarlo tampoco descuenta nada de nuevo. Lo unico que cambia es si esa
    plata cuenta como facturacion en /reportes/caja-socio.

    Tampoco carga el costo como gasto. Regalar no es gratis -- la mercaderia
    se pago cuando se compro --, pero ese gasto se carga donde se cargan todos
    y no lo puede inventar esta ruta: no sabe con que plata se pago ni en que
    fecha, y un gasto automatico se sumaria al que ya se cargo a mano.
    """
    # El checkbox sin tildar NO viaja en el POST, asi que la lista de tildados
    # es lo unico que llega. Los `pedido_id` ocultos son los que dicen sobre
    # que filas estamos opinando: sin ellos, "no vino tildado" no se
    # distinguiria de "esa fila no estaba en la pantalla".
    ids_en_pantalla = request.form.getlist('pedido_id')
    tildados = set(request.form.getlist('es_regalo'))

    pedidos = {pedido.id: pedido for pedido in
               Pedido.query.filter_by(empresa_id=current_user.empresa_id).all()}

    try:
        cambios = []
        for crudo in ids_en_pantalla:
            crudo = (crudo or '').strip()
            try:
                pedido_id = int(crudo)
            except (TypeError, ValueError):
                continue
            pedido = pedidos.get(pedido_id)
            if pedido is None:
                # Igual que en las otras dos tandas: una fila que no es de
                # esta empresa se ignora en vez de voltear el guardado.
                continue
            quiere = crudo in tildados
            if quiere != bool(pedido.es_regalo):
                cambios.append((pedido, quiere))

        for pedido, quiere in cambios:
            pedido.es_regalo = quiere
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        flash('No se pudieron guardar los regalos. Intenta de nuevo.', 'error')
        return redirect(url_for('ventas.listar_pedidos'))

    if cambios:
        flash('Se actualizo la marca de regalo en %d pedido%s.'
              % (len(cambios), '' if len(cambios) == 1 else 's'), 'success')
    else:
        flash('No hubo cambios que guardar.', 'warning')
    return redirect(url_for('ventas.listar_pedidos'))


# --------------------------------------------------------------------------
# FASE-VENTAS-EDITAR-S1: corregir una venta manual ya cargada
# --------------------------------------------------------------------------
# Hasta esta slice, de una venta ya guardada se podian corregir tres cosas
# desde el listado -- la comision, la cuenta y la marca de regalo -- y ninguna
# de las cuatro que de verdad se tipean mal: la fecha, el medio, el producto y
# el monto. Una venta cargada con el precio equivocado no tenia arreglo dentro
# de la app.
#
# SOLO LA VENTA MANUAL, Y NO ES UNA LIMITACION TEMPORAL
#
# Un pedido de Tiendanube o Mercado Libre lo reescribe el sync en cada corrida
# (`sync_tiendanube._upsert_pedido` pisa cabecera e items): editarlo aca seria
# escribir algo que la proxima sincronizacion borra sin avisar. La fuente de
# verdad de esos pedidos es el canal, y se corrigen alla.
#
# COMO SE RECONOCE UNA VENTA MANUAL
#
# Por las dos cosas a la vez: `canal.tipo == 'manual'` y `id_externo IS NULL`.
# Con una sola alcanzaria -- el unico escritor de pedidos ademas de este modulo
# es el sync, que revienta con ErrorIngesta si el payload viene sin id, asi que
# hoy NULL implica manual -- pero el dia que se sume un canal que no traiga id
# propio, la doble condicion es lo que impide que sus pedidos caigan aca por
# accidente.
#
# EL STOCK SE AJUSTA POR LA DIFERENCIA
#
# El alta descuenta lo vendido una vez. Editar no puede volver a descontar lo
# mismo: lo que se aplica es el NETO entre lo que las lineas viejas se habian
# llevado y lo que se llevan las nuevas. Bajar de 3 a 1 devuelve 2 unidades,
# subir de 1 a 3 descuenta 2, y cambiar el producto de una linea devuelve al
# viejo y descuenta del nuevo.
#
# LO QUE NO SE TOCA
#
# `costo_unitario_snapshot` de una linea que sobrevive a la edicion: es el
# costo del dia de la venta y no el de hoy, porque la venta no se hizo de
# nuevo. Una linea AGREGADA hoy si nace con el costo de hoy, que es lo mismo
# que hace el alta -- no hay ningun snapshot anterior que respetarle.


def _lineas_del_pedido(pedido):
    """Los items guardados -> las filas que dibuja el formulario.

    El sku sale del producto y no de `sku_externo` porque el <datalist> del
    formulario ofrece los del catalogo: una linea cuyo producto se renombro
    tiene que volver a aparecer elegida, no vacia.
    """
    lineas = []
    for item in pedido.items:
        producto = item.producto
        lineas.append({
            'sku': producto.sku if producto is not None else (item.sku_externo or ''),
            'cantidad': item.cantidad,
            'precio_unitario': item.precio_unitario,
        })
    return lineas


def _lineas_enviadas(form):
    """Lo que vino en el POST, crudo, para redibujar el formulario tras un error.

    Sin validar a proposito: es justamente lo que NO paso la validacion lo que
    hay que devolverle a quien lo escribio. Si el POST no trajo ninguna fila
    (formulario roto), se devuelve una vacia para que la tabla no quede sin
    ninguna y sin forma de cargar.
    """
    skus = form.getlist('sku')
    cantidades = form.getlist('cantidad')
    precios = form.getlist('precio_unitario')

    lineas = []
    for indice, sku in enumerate(skus):
        lineas.append({
            'sku': (sku or '').strip(),
            'cantidad': cantidades[indice] if indice < len(cantidades) else '',
            'precio_unitario': precios[indice] if indice < len(precios) else '',
        })
    return lineas or [{'sku': '', 'cantidad': '1', 'precio_unitario': ''}]


def _pedido_manual_editable(pedido_id):
    """El pedido de la URL, o un error que explica por que no se puede editar.

    Filtrar por `empresa_id` -- y no solo por id -- es lo unico que impide que
    cambiando el numero de la URL alguien edite el pedido de otra empresa.
    """
    pedido = (Pedido.query
              .options(joinedload(Pedido.items).joinedload(PedidoItem.producto),
                       joinedload(Pedido.canal),
                       joinedload(Pedido.pagos))
              .filter_by(id=pedido_id, empresa_id=current_user.empresa_id)
              .first())
    if pedido is None:
        raise DatosInvalidos('Ese pedido no existe o no es de tu empresa.')

    canal = pedido.canal
    if canal is None or canal.tipo != TIPO_MANUAL or pedido.id_externo is not None:
        raise DatosInvalidos(
            'Ese pedido no es una venta manual: entro por %s y ahi se corrige. '
            'Editarlo aca lo pisaria la proxima sincronizacion.'
            % ETIQUETA_CANAL.get(canal.tipo if canal else None,
                                 canal.nombre if canal else 'otro canal'))
    return pedido


def _contexto_edicion(pedido, productos, enviado, lineas):
    """Todo lo que la plantilla de edicion necesita, en un solo lugar.

    Mismo motivo que `_contexto_formulario`: tres render_template distintos
    dibujan este formulario (el GET, el error de datos y el error inesperado) y
    armar el contexto en cada uno hace que el dia que se agregue un campo,
    alguno vuelva mutilado.
    """
    return {
        'pedido': pedido,
        'productos': productos,
        'medios': MEDIOS_COBRO,
        'enviado': enviado,
        'lineas': lineas,
        'cuentas': _cuentas_de_cobro(current_user.empresa_id),
        'cuenta_por_canal': _cuenta_por_canal_manual(),
    }


def _diferencia_de_stock(pedido, items):
    """producto_id -> cuantas unidades MAS se vendieron con la edicion.

    Positivo: hay que descontar esa diferencia. Negativo: hay que devolverla.
    Cero: esa linea quedo igual y el stock no se toca (no entra al dict).

    Se calcula sobre las CANTIDADES totales por producto y no linea por linea:
    partir una linea de 3 en dos de 2 y 1 no mueve una sola unidad, y asi el
    resultado no depende de como se hayan reacomodado las filas.

    Un item viejo sin `producto_id` (una linea que nunca se pudo mapear a un
    producto del catalogo) no aporta: nunca descontó stock de nada, asi que no
    hay nada que devolverle.
    """
    saldo = {}
    for item in pedido.items:
        if item.producto_id is None:
            continue
        saldo[item.producto_id] = saldo.get(item.producto_id, 0) - item.cantidad
    for item in items:
        producto_id = item['producto'].id
        saldo[producto_id] = saldo.get(producto_id, 0) + item['cantidad']
    return {pid: delta for pid, delta in saldo.items() if delta}


def _aplicar_diferencia_de_stock(saldo):
    """Mueve el stock por el neto de la edicion. FASE-VENTAS-EDITAR-S1.

    Mismas dos reglas que `_descontar_stock`, por el mismo motivo:

    - `stock` NULL es "nadie lleva la cuenta de este producto". No se toca ni
      para descontar ni para devolver: inventarle un numero al editar seria
      empezar a llevar una cuenta a partir de un dato que no existe.
    - Un resultado negativo se guarda como 0 y se avisa. La mercaderia ya
      salio; un stock negativo no describiria nada real.

    LO QUE ESTE AJUSTE NO PUEDE DESHACER, Y HAY QUE SABERLO

    Si la venta original sobrevendio -- se cargaron 5 con 3 en stock y el stock
    quedo en 0 en vez de en -2 --, bajar despues esa venta a 1 devuelve las 4
    unidades sobre ese 0 y deja 4, no 2. La informacion se perdio en el recorte
    del alta, no aca, y no hay forma de recuperarla desde la base. Por eso el
    aviso de sobreventa dice "revisá el conteo real": ese conteo es el que
    manda, no este numero.

    Devuelve (sobrevendidos, producto_ids), igual que `_descontar_stock`.
    """
    sobrevendidos = []
    producto_ids = []

    for producto_id, delta in saldo.items():
        producto = db.session.get(Producto, producto_id)
        if producto is None or producto.stock is None:
            continue

        restante = producto.stock - delta
        if restante < 0:
            restante = 0
            if producto.nombre not in sobrevendidos:
                sobrevendidos.append(producto.nombre)

        producto.stock = restante
        if producto.id not in producto_ids:
            producto_ids.append(producto.id)

    return sobrevendidos, producto_ids


def _reescribir_items(pedido, items):
    """Deja el pedido con exactamente estas lineas, reusando las que ya estaban.

    POR QUE REUSAR Y NO BORRAR TODO Y VOLVER A INSERTAR

    Dos motivos, y ninguno es de rendimiento:

    - `costo_unitario_snapshot`. Borrar la fila destruye el costo del dia de la
      venta; una fila nueva solo puede copiar el costo de HOY, y ahi el
      "snapshot" deja de congelar nada. Es exactamente el agujero que
      `sync_tiendanube._snapshots_previos` tapa del otro lado.
    - `devolucion.pedido_item_id` es una FK a estas filas.

    El apareo es POR PRODUCTO y no por posicion: si la linea 1 pasa de Martillo
    a Destornillador y la 2 al reves, aparear por posicion le dejaria a cada una
    el costo congelado de la otra. Dos lineas del mismo producto se aparean en
    orden, que es lo unico que se puede decir de ellas.

    Una linea AGREGADA hoy nace con el costo de hoy -- lo mismo que hace el
    alta. No hay ningun snapshot anterior que respetarle.
    """
    sobrantes = list(pedido.items)
    por_producto = {}
    for fila in sobrantes:
        por_producto.setdefault(fila.producto_id, []).append(fila)

    for item in items:
        producto = item['producto']
        disponibles = por_producto.get(producto.id)
        if disponibles:
            fila = disponibles.pop(0)
            sobrantes.remove(fila)
        else:
            fila = PedidoItem(pedido_id=pedido.id,
                              costo_unitario_snapshot=producto.costo_unitario)
            db.session.add(fila)
        fila.producto_id = producto.id
        fila.sku_externo = producto.sku
        fila.descripcion = producto.nombre
        fila.cantidad = item['cantidad']
        fila.precio_unitario = item['precio_unitario']
        fila.descuento_unitario = Decimal('0.00')
        fila.subtotal = item['subtotal']

    # Las que sobraron se van, salvo que alguien haya cargado una devolucion
    # contra ellas: esa devolucion dice cuantas unidades volvieron de ESA linea,
    # y sin la linea el dato queda colgado (ademas de romper la FK). Se avisa en
    # vez de borrar en cascada: quitar la linea vendida sin tocar la devolucion
    # dejaria stock devuelto de algo que nunca se vendio.
    for fila in sobrantes:
        if Devolucion.query.filter_by(pedido_item_id=fila.id).count():
            raise DatosInvalidos(
                'No se puede quitar el item "%s": tiene una devolucion '
                'cargada. Anula la devolucion primero.' % fila.descripcion)
        db.session.delete(fila)


def _reescribir_pago(pedido, medio, fecha, total):
    """Deja el pago del pedido diciendo lo mismo que la venta editada.

    Un pedido manual nace con EXACTAMENTE un pago (`_armar_venta`), asi que el
    caso normal es actualizar ese. Si no hay ninguno -- un pedido manual tocado
    a mano en la base -- se crea, porque un pedido sin pago no aparece en
    ninguna conciliacion.

    No se borra y se vuelve a crear por lo mismo que las lineas: `devolucion` y
    `conciliacion` apuntan a `pago.id`.

    La regla de comision/estado es la del alta, sin excepciones: al cambiar de
    tarjeta a efectivo el pago pasa a acreditado con comision 0, y al reves
    vuelve a pendiente con la comision en NULL -- que es "todavia no se sabe",
    no "no hubo".
    """
    en_efectivo = medio in MEDIOS_SIN_COMISION

    pago = next((p for p in sorted(pedido.pagos, key=lambda fila: fila.id or 0)
                 if p.procesador == PROCESADOR_MANUAL), None)
    if pago is None:
        pago = Pago(pedido_id=pedido.id, canal_id=pedido.canal_id,
                    procesador=PROCESADOR_MANUAL, id_externo=None,
                    moneda=MONEDA_DEFECTO, impuestos=Decimal('0.00'))
        db.session.add(pago)

    pago.metodo = medio
    pago.estado = 'acreditado' if en_efectivo else 'pendiente'
    pago.monto_bruto = total
    pago.comision = Decimal('0.00') if en_efectivo else None
    pago.monto_neto = total
    pago.fecha_pago = fecha
    pago.fecha_acreditacion = fecha if en_efectivo else None


@ventas_bp.route('/manual/editar/<int:pedido_id>', methods=['GET', 'POST'])
@login_required
def editar_venta_manual(pedido_id):
    """Corrige una venta de mostrador ya cargada.

    Es el alta con dos diferencias: el pedido ya existe (asi que se actualiza en
    vez de insertarse) y el stock se mueve por la DIFERENCIA en vez de
    descontarse entero.

    Incluye la cuenta y la comision, que ya eran editables desde el listado. No
    es una segunda implementacion: son las mismas dos lecturas
    (`_leer_cuenta_override`, `_leer_comision`) sobre las mismas dos columnas.
    Lo que cambia es de donde se llega -- corregir el monto y la cuenta de la
    misma venta en un solo formulario, en vez de en dos pantallas.

    La auditoria no necesita nada aca: el hook de `before_flush` ya mira
    `Pedido` y deja una fila por campo cambiado, con el valor viejo y el nuevo.
    """
    try:
        pedido = _pedido_manual_editable(pedido_id)
    except DatosInvalidos as error:
        flash(str(error), 'error')
        return redirect(url_for('ventas.listar_pedidos'))

    productos = _productos_de_la_empresa()

    if request.method == 'GET':
        pago = next((p for p in pedido.pagos if p.procesador == PROCESADOR_MANUAL),
                    None)
        enviado = {
            'fecha': (pedido.fecha_pedido.date().isoformat()
                      if pedido.fecha_pedido else ''),
            'medio': pago.metodo if pago is not None else '',
            'nota': pedido.nota or '',
            'cuenta': (str(pedido.cuenta_cobro_override_id)
                       if pedido.cuenta_cobro_override_id else ''),
            # Vacio si es NULL: el input tiene que quedar VACIO y no mostrar un
            # 0 inventado, igual que en el listado.
            'comision': ('' if pedido.comision_plataforma is None
                         else str(pedido.comision_plataforma)),
            'regalo': bool(pedido.es_regalo),
        }
        return render_template('venta_manual_editar.html',
                               **_contexto_edicion(pedido, productos, enviado,
                                                   _lineas_del_pedido(pedido)))

    enviado = {
        'fecha': (request.form.get('fecha') or '').strip(),
        'medio': (request.form.get('medio') or '').strip(),
        'nota': (request.form.get('nota') or '').strip(),
        'cuenta': (request.form.get('cuenta_cobro_override') or '').strip(),
        'comision': (request.form.get('comision_plataforma') or '').strip(),
        'regalo': bool(request.form.get('es_regalo')),
    }
    lineas = _lineas_enviadas(request.form)

    try:
        fecha = _leer_fecha(enviado['fecha'])
        medio = enviado['medio']
        if medio not in MEDIOS_VALIDOS:
            raise DatosInvalidos('Elegi un medio de cobro.')

        cuenta_override_id = _leer_cuenta_override(
            enviado['cuenta'], _etiqueta_pedido(pedido),
            {cuenta.id for cuenta in _cuentas_de_cobro(current_user.empresa_id)})
        comision = _leer_comision(enviado['comision'], _etiqueta_pedido(pedido))

        items = _leer_items(request.form, {p.sku: p for p in productos})

        # El saldo se calcula ANTES de reescribir las lineas: despues, las
        # cantidades viejas ya no existen en ningun lado.
        saldo = _diferencia_de_stock(pedido, items)

        total = sum((item['subtotal'] for item in items), Decimal('0.00'))
        _reescribir_items(pedido, items)

        pedido.fecha_pedido = fecha
        pedido.total_bruto = total
        pedido.total = total
        pedido.cuenta_cobro_override_id = cuenta_override_id
        pedido.comision_plataforma = comision
        pedido.es_regalo = enviado['regalo']
        pedido.nota = enviado['nota'] or None

        _reescribir_pago(pedido, medio, fecha, total)
        sobrevendidos, producto_ids = _aplicar_diferencia_de_stock(saldo)

        db.session.commit()
        total_guardado = pedido.total
    except (DatosInvalidos, CuentaInvalida, ComisionInvalida) as error:
        # Las tres son lo mismo para quien esta corrigiendo: el formulario
        # vuelve con lo que habia escrito y el motivo arriba.
        db.session.rollback()
        flash(str(error), 'error')
        return render_template('venta_manual_editar.html',
                               **_contexto_edicion(pedido, productos, enviado,
                                                   lineas))
    except Exception:  # noqa: BLE001
        # Nada a medias: la venta queda como estaba, con su stock como estaba.
        db.session.rollback()
        flash('No se pudo guardar la venta. Revisa los datos e intenta de nuevo.',
              'error')
        return render_template('venta_manual_editar.html',
                               **_contexto_edicion(pedido, productos, enviado,
                                                   lineas))

    flash('Venta actualizada: %s %s.' % (MONEDA_DEFECTO, total_guardado), 'success')

    # Todo lo que sigue pasa DESPUES del commit y a proposito: la correccion ya
    # esta guardada y ninguno de estos avisos la puede deshacer.
    if sobrevendidos:
        flash('La correccion dejo vendido mas de lo que figuraba en stock para: '
              '%s. El stock quedo en 0; revisa el conteo real.'
              % ', '.join(sobrevendidos), 'warning')

    try:
        fallidos = stock_tiendanube.empujar_stock(current_user.empresa_id,
                                                  producto_ids)
    except Exception:  # noqa: BLE001
        # El rollback va primero, igual que en el alta: si lo que se rompio
        # dejo la sesion sucia, leer los nombres para el aviso volveria a
        # fallar. La correccion ya esta commiteada.
        db.session.rollback()
        fallidos = [p.nombre for p in productos if p.id in producto_ids]

    if fallidos:
        flash('Venta corregida, pero no se pudo actualizar el stock en Tiendanube '
              'para: %s - revisalo a mano.' % ', '.join(fallidos), 'warning')

    return redirect(url_for('ventas.listar_pedidos'))
