# -*- coding: utf-8 -*-
"""FASE-DEVOLUCIONES-S2: registrar una devolucion y devolver el stock.

La tabla `devolucion` existia desde FASE2-S1 y no la escribia nadie: cero
rutas, cero syncs, cero plantillas. Apuntaba solo a `pedido` y guardaba plata,
asi que ni siquiera alcanzaba para decir QUE volvio. S2 le agrega
`pedido_item_id` + `cantidad` (ver el modelo) y le pone encima las dos cosas
que faltaban: una pantalla para cargarla y el movimiento de stock.

QUE HACE Y QUE NO HACE

Suma al stock lo que volvio, y le avisa a Tiendanube el numero nuevo por el
mismo camino que la venta de mostrador (`stock_tiendanube.empujar_stock`, sin
una linea duplicada).

NO toca ningun reporte de plata. Vendido, margen y facturacion siguen dando
exactamente los mismos numeros que antes de esta slice: lo devuelto se muestra
en una columna al lado, nunca restandose de un calculo existente. Es una
decision tomada -- la alternativa (restar de vendido) obliga a resolver de una
sola vez el vinculo con el item, la cadena append-only y la reconstruccion de
`_stock_inicial`, todo antes de que exista una sola devolucion cargada.

UNA VEZ POR CADENA

`devolucion` es append-only: cada cambio de estado entra como fila nueva que
apunta a la anterior. Si el stock se moviera "cuando hay una devolucion", un
contracargo revisado tres veces sumaria tres veces. Por eso el stock se mueve
en la TRANSICION a estado terminal y solo si ningun antecesor de la cadena ya
lo movio (`_cadena_ya_devolvio_stock`).

QUE PASA SI ANTES HUBO SOBREVENTA -- EL CLAMP HEREDADO

La venta de mostrador no bloquea vender sin stock: descuenta y clampea en 0
(`rutas_ventas._descontar_stock`, "lo fisico manda, el sistema avisa"). Eso
hace que devolver NO sea simetrico con vender, y conviene tenerlo claro porque
el comportamiento es predecible aunque el numero final no sea el real:

    stock 3, se venden 5  -> el descuento real fue 3, no 5 (clamp en 0)
    se devuelven esas 5   -> la suma SI es de 5, sin clamp
    stock final           -> 5, cuando fisicamente hay 2

El sistema no puede saberlo: en el momento de la venta ya aviso que el numero
venia mal ("revisá el conteo real") y el error quedo enterrado en el clamp. La
devolucion no lo inventa ni lo corrige, lo arrastra. Es un problema heredado y
no se resuelve en esta slice; corregirlo requiere que alguien cuente lo que hay
en el estante, que es exactamente lo que el aviso de sobreventa pide.

La suma NO clampea contra nada a proposito. Simetria con la venta: alla el
criterio es que lo fisico manda, aca tambien -- si la mercaderia volvio, volvio.
Lo unico que se valida es contra el pedido (no se puede devolver mas de lo que
se vendio), y eso si se valida porque es un dato que se esta cargando ahora y
hay contra que compararlo, a diferencia de la venta donde el hecho ya ocurrio.

El blueprint se registra en app.py; ninguna ruta existente se toca.
"""

from datetime import datetime
from decimal import Decimal

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import aliased, joinedload

import stock_tiendanube
from models import (
    DEVOLUCION_CERRADA,
    ESTADOS_DEVOLUCION_TERMINALES,
    Devolucion,
    Pedido,
    PedidoItem,
    db,
)
from rutas_ventas import ETIQUETA_CANAL, _etiqueta_pedido

devoluciones_bp = Blueprint('devoluciones', __name__, url_prefix='/devoluciones')

# El unico tipo que esta pantalla carga. `devolucion.tipo` admite tambien
# 'contracargo' y 'cancelacion', que son eventos de PLATA y no de mercaderia:
# no los carga una persona en el mostrador, los traeria un sync que todavia no
# existe. Cuando exista, va a escribir la misma tabla con otro tipo y sin item.
TIPO_DEVOLUCION = 'devolucion'

CERO = Decimal('0.00')


class DevolucionInvalida(Exception):
    """Lo que el formulario trajo no arma una devolucion. El mensaje se le
    muestra tal cual a quien la esta cargando, asi que se escribe en criollo."""


# --------------------------------------------------------------------------
# El movimiento de stock
# --------------------------------------------------------------------------


def _cadena_ya_devolvio_stock(devolucion):
    """¿Algun evento anterior de esta cadena ya sumo el stock?

    Sube por `evento_previo_id` hasta el arranque de la cadena. Con que UN
    antecesor haya estado en estado terminal alcanza: la mercaderia volvio una
    sola vez, por mas veces que despues se haya revisado el expediente.

    El set de visitados no es paranoia gratuita: `evento_previo_id` apunta a la
    misma tabla, y un ciclo (una fila editada a mano en la base, un futuro sync
    con un bug) colgaria el request para siempre en vez de fallar.
    """
    visitados = set()
    previo_id = devolucion.evento_previo_id

    while previo_id is not None and previo_id not in visitados:
        visitados.add(previo_id)
        previo = db.session.get(Devolucion, previo_id)
        if previo is None:
            return False
        if previo.estado in ESTADOS_DEVOLUCION_TERMINALES:
            return True
        previo_id = previo.evento_previo_id

    return False


def devolver_stock(devolucion):
    """Suma al stock las unidades que volvieron. Devuelve el producto_id que
    cambio, o None si no habia nada que mover.

    Corre dentro de la misma transaccion que la devolucion: o se guarda el
    evento con su movimiento de stock, o no se guarda nada. Igual que el
    descuento de la venta (`rutas_ventas._descontar_stock`).

    Las cinco razones por las que puede no mover nada, y ninguna es un error:

    - La fila no esta en estado terminal: se registro el reclamo pero la
      mercaderia no volvio todavia.
    - No es un evento de mercaderia (sin item ni cantidad): un contracargo es
      plata, el comprador se quedo con el producto.
    - Un antecesor de la cadena ya lo movio.
    - La linea del pedido nunca se pudo atar a un producto del catalogo
      (`producto_id` NULL, contemplado desde FASE3-S2): no hay stock que sumar
      porque no se sabe de que producto se trata.
    - `producto.stock` es None. NULL es "nadie lleva la cuenta", no "hay cero":
      mismo criterio que en toda la app. Sumarle a un None lo convertiria en un
      numero, o sea inventaria un control de stock que nadie pidio.
    """
    if devolucion.estado not in ESTADOS_DEVOLUCION_TERMINALES:
        return None
    if devolucion.pedido_item_id is None or not devolucion.cantidad:
        return None
    if _cadena_ya_devolvio_stock(devolucion):
        return None

    item = db.session.get(PedidoItem, devolucion.pedido_item_id)
    if item is None or item.producto is None:
        return None

    producto = item.producto
    if producto.stock is None:
        return None

    # Sin techo ni piso: ver "EL CLAMP HEREDADO" arriba.
    producto.stock = producto.stock + devolucion.cantidad
    return producto.id


# --------------------------------------------------------------------------
# Lo devuelto, para los reportes (FASE-DEVOLUCIONES-S2 parte 4)
# --------------------------------------------------------------------------
#
# Las dos consultas de abajo son la MISMA pregunta con dos formas distintas,
# porque los dos reportes que la hacen ya vienen armados distinto: el resumen
# de S1 agrega en la base y nunca carga items, el de margen ya itera las lineas
# en Python. Comparten el predicado, que es lo unico delicado.
#
# QUE CUENTA COMO "DEVUELTO"
#
# La fila VIGENTE de cada cadena, y solo si quedo en estado terminal:
#
#   vigente  = ninguna otra fila la apunta por `evento_previo_id`. Es la
#              definicion que el propio modelo declara ("el estado vigente es
#              la ultima fila de la cadena"), no una regla nueva.
#   terminal = la mercaderia efectivamente volvio.
#
# Sumar todas las filas contaria tres veces un contracargo revisado tres veces.
# Y mirar solo el estado terminal sin mirar la vigencia contaria una devolucion
# que despues se dio de baja. Las dos condiciones juntas dan exactamente una
# fila por cadena, que es lo mismo que movio el stock.
#
# El unico caso donde esta cuenta y el stock se separan es una cadena cerrada y
# despues REABIERTA: el stock ya se movio (la mercaderia volvio) y la columna
# deja de contarla (el evento esta otra vez sin resolver). Hoy no hay forma de
# llegar ahi -- la pantalla crea cadenas de una sola fila, ya terminal -- y
# cuando la haya, es la pregunta que hay que hacerle a Roman antes que elegir
# por el.


def _base_devuelto(empresa_id):
    """El esqueleto comun: devoluciones vigentes y terminales de la empresa.

    Se filtra por `ESTADOS_NO_VENDIDOS` igual que los reportes que la consumen.
    Un pedido cancelado sale de "vendido", asi que sus devoluciones tienen que
    salir de "devuelto": dejarlas mostraria una fila que devolvio algo que,
    segun el mismo reporte, nunca se vendio.
    """
    from rutas_productos import ESTADOS_NO_VENDIDOS

    sucesor = aliased(Devolucion)

    return (db.session.query(Devolucion)
            .join(PedidoItem, PedidoItem.id == Devolucion.pedido_item_id)
            .join(Pedido, Pedido.id == Devolucion.pedido_id)
            .outerjoin(sucesor, sucesor.evento_previo_id == Devolucion.id)
            .filter(sucesor.id.is_(None))
            .filter(Devolucion.estado.in_(ESTADOS_DEVOLUCION_TERMINALES))
            .filter(Pedido.empresa_id == empresa_id)
            .filter(func.lower(Pedido.estado).notin_(ESTADOS_NO_VENDIDOS)))


def devuelto_por_producto_y_canal(empresa_id):
    """(producto_id, canal_id) -> unidades devueltas.

    La contraparte exacta de `rutas_productos._vendido_por_producto_y_canal`:
    mismas claves, mismo filtro de cancelados, misma forma de dict, para que el
    reporte pueda poner las dos columnas una al lado de la otra sin traducir
    nada en el medio.
    """
    filas = (_base_devuelto(empresa_id)
             .with_entities(PedidoItem.producto_id,
                            Pedido.canal_id,
                            func.sum(Devolucion.cantidad))
             .group_by(PedidoItem.producto_id, Pedido.canal_id)
             .all())

    return {(producto_id, canal_id): int(cantidad or 0)
            for producto_id, canal_id, cantidad in filas}


def devuelto_por_item(empresa_id):
    """pedido_item_id -> unidades devueltas.

    Para el reporte de margen, que ya tiene las lineas de cada pedido en la
    mano y solo necesita preguntarle a cada una cuanto volvio.
    """
    filas = (_base_devuelto(empresa_id)
             .with_entities(Devolucion.pedido_item_id,
                            func.sum(Devolucion.cantidad))
             .group_by(Devolucion.pedido_item_id)
             .all())

    return {item_id: int(cantidad or 0) for item_id, cantidad in filas}


# --------------------------------------------------------------------------
# La pantalla
# --------------------------------------------------------------------------


def _pedido_de_la_empresa(pedido_id):
    """El pedido, o un error si no es de la empresa de quien esta mirando.

    Filtrar por `empresa_id` y no solo por id es lo que impide que cambiando el
    numero de la URL alguien vea -- y devuelva stock de -- el pedido de otra
    empresa.
    """
    pedido = (Pedido.query
              .options(joinedload(Pedido.items).joinedload(PedidoItem.producto),
                       joinedload(Pedido.canal))
              .filter_by(id=pedido_id, empresa_id=current_user.empresa_id)
              .first())
    if pedido is None:
        raise DevolucionInvalida('Ese pedido no existe o no es de tu empresa.')
    return pedido


def _devuelto_por_item_del_pedido(pedido):
    """pedido_item_id -> unidades ya devueltas, dentro de este pedido.

    Es lo que hace que devolver 2 y despues 2 mas de un item de 3 no pase: sin
    esto cada carga se valida sola contra el pedido y entre las dos devuelven
    mas de lo que se vendio.
    """
    ids = [item.id for item in pedido.items]
    if not ids:
        return {}

    sucesor = aliased(Devolucion)
    filas = (db.session.query(Devolucion.pedido_item_id,
                              func.sum(Devolucion.cantidad))
             .outerjoin(sucesor, sucesor.evento_previo_id == Devolucion.id)
             .filter(sucesor.id.is_(None))
             .filter(Devolucion.estado.in_(ESTADOS_DEVOLUCION_TERMINALES))
             .filter(Devolucion.pedido_item_id.in_(ids))
             .group_by(Devolucion.pedido_item_id)
             .all())

    return {item_id: int(cantidad or 0) for item_id, cantidad in filas}


def _filas_del_pedido(pedido, ya_devuelto):
    """Las lineas del pedido con cuanto se puede devolver de cada una."""
    filas = []
    for item in pedido.items:
        devuelto = ya_devuelto.get(item.id, 0)
        filas.append({
            'id': item.id,
            'descripcion': item.descripcion,
            'sku': item.sku_externo or (item.producto.sku if item.producto else ''),
            'cantidad': item.cantidad,
            'devuelto': devuelto,
            'disponible': max(item.cantidad - devuelto, 0),
            # Sin producto atado no hay stock que mover, y conviene decirlo en
            # la pantalla en vez de que la devolucion parezca no haber hecho
            # nada. La linea igual se puede devolver: el evento queda
            # registrado aunque no haya inventario que corregir.
            'sin_producto': item.producto is None,
            'sin_control': item.producto is not None and item.producto.stock is None,
        })
    return filas


def _leer_item(form, filas):
    """Que linea del pedido eligieron. Devuelve la fila, no solo el id."""
    texto = (form.get('pedido_item_id') or '').strip()
    if not texto:
        raise DevolucionInvalida('Elegí qué producto se devolvió.')
    try:
        item_id = int(texto)
    except ValueError:
        raise DevolucionInvalida('El producto elegido no es válido.')

    for fila in filas:
        if fila['id'] == item_id:
            return fila
    raise DevolucionInvalida('Ese producto no es parte de este pedido.')


def _leer_cantidad(form, fila):
    """Cuantas unidades volvieron, validadas contra lo que se habia vendido.

    Aca SI se valida, a diferencia de la venta de mostrador, y la diferencia no
    es de criterio sino de momento: la venta describe algo que ya paso
    fisicamente y no hay forma de deshacerlo desde una pantalla; la devolucion
    se esta cargando ahora y hay un pedido real contra el cual comparar.
    Devolver 4 de un item que llevaba 3 no es un hecho que haya que registrar,
    es un tipeo.
    """
    texto = (form.get('cantidad') or '').strip()
    if not texto:
        raise DevolucionInvalida('Poné cuántas unidades se devolvieron.')
    try:
        cantidad = int(texto)
    except ValueError:
        raise DevolucionInvalida('La cantidad tiene que ser un número entero.')
    if cantidad <= 0:
        raise DevolucionInvalida('La cantidad tiene que ser mayor a cero.')

    if cantidad > fila['disponible']:
        if fila['devuelto']:
            raise DevolucionInvalida(
                'De "%s" se vendieron %d y ya se devolvieron %d: no se pueden '
                'devolver %d más.' % (fila['descripcion'], fila['cantidad'],
                                      fila['devuelto'], cantidad))
        raise DevolucionInvalida(
            'De "%s" se vendieron %d: no se pueden devolver %d.'
            % (fila['descripcion'], fila['cantidad'], cantidad))

    return cantidad


def _monto_devuelto(item, cantidad):
    """La plata que vuelve por esas unidades: lo que se cobro por ellas.

    `monto` es NOT NULL desde FASE2-S1, asi que dejarlo en 0 no seria "no se
    sabe" sino la afirmacion de que no volvio un peso. Se calcula con el precio
    y el descuento que quedaron guardados EN LA LINEA, no con el precio de
    lista de hoy: lo que se devuelve es lo que se cobro aquel dia.

    `comision_devuelta` queda en su default 0 y no se calcula: si la plataforma
    reintegra o no su comision es algo que esta pantalla no puede saber -- sale
    de una liquidacion, igual que `pedido.comision_plataforma`, que tambien se
    carga a mano. Como ningun reporte de esta slice usa ninguno de los dos
    montos (opcion C: no se resta nada), no hay ningun numero apoyado en esto.
    """
    precio = (item.precio_unitario or CERO) - (item.descuento_unitario or CERO)
    if precio < CERO:
        precio = CERO
    return (precio * cantidad).quantize(Decimal('0.01'))


@devoluciones_bp.route('/nueva')
@login_required
def elegir_pedido():
    """Paso 1: contra que venta se devuelve.

    Sin filtros ni paginacion, mismo criterio y mismo orden que el listado de
    ventas: lo ultimo vendido primero, que es donde caen casi todas las
    devoluciones.
    """
    pedidos = (Pedido.query
               .options(joinedload(Pedido.canal), joinedload(Pedido.items))
               .filter_by(empresa_id=current_user.empresa_id)
               .order_by(Pedido.fecha_pedido.desc(), Pedido.id.desc())
               .all())

    filas = []
    for pedido in pedidos:
        canal = pedido.canal
        filas.append({
            'id': pedido.id,
            'etiqueta': _etiqueta_pedido(pedido),
            'fecha': pedido.fecha_pedido,
            'canal': ETIQUETA_CANAL.get(canal.tipo if canal else None,
                                        canal.nombre if canal else '—'),
            'cliente': pedido.comprador_nombre or '',
            'total': pedido.total,
            'moneda': pedido.moneda,
            'items': len(pedido.items),
        })

    return render_template('devolucion_elegir_pedido.html', pedidos=filas)


@devoluciones_bp.route('/nueva/<int:pedido_id>', methods=['GET', 'POST'])
@login_required
def nueva_devolucion(pedido_id):
    """Paso 2: que linea volvio, cuantas unidades y por que."""
    try:
        pedido = _pedido_de_la_empresa(pedido_id)
    except DevolucionInvalida as error:
        flash(str(error), 'error')
        return redirect(url_for('devoluciones.elegir_pedido'))

    filas = _filas_del_pedido(pedido, _devuelto_por_item_del_pedido(pedido))

    def pantalla(enviado):
        return render_template('devolucion_nueva.html',
                               pedido=pedido,
                               etiqueta=_etiqueta_pedido(pedido),
                               filas=filas,
                               enviado=enviado)

    if request.method == 'GET':
        return pantalla({})

    enviado = {
        'pedido_item_id': (request.form.get('pedido_item_id') or '').strip(),
        'cantidad': (request.form.get('cantidad') or '').strip(),
        'motivo': (request.form.get('motivo') or '').strip(),
    }

    try:
        fila = _leer_item(request.form, filas)
        cantidad = _leer_cantidad(request.form, fila)
        item = db.session.get(PedidoItem, fila['id'])

        devolucion = Devolucion(
            pedido_id=pedido.id,
            pedido_item_id=item.id,
            cantidad=cantidad,
            tipo=TIPO_DEVOLUCION,
            motivo=enviado['motivo'] or None,
            moneda=pedido.moneda,
            monto=_monto_devuelto(item, cantidad),
            # Nace cerrada porque asi es el mostrador: el cliente esta parado
            # ahi con el producto en la mano. El estado 'abierta' existe para
            # un reclamo sin resolver, que hoy no tiene por donde entrar.
            estado=DEVOLUCION_CERRADA,
            fecha_evento=datetime.utcnow(),
        )
        db.session.add(devolucion)
        db.session.flush()

        producto_id = devolver_stock(devolucion)
        db.session.commit()
    except DevolucionInvalida as error:
        db.session.rollback()
        flash(str(error), 'error')
        return pantalla(enviado)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        flash('No se pudo registrar la devolución. Revisá los datos e '
              'intentá de nuevo.', 'error')
        return pantalla(enviado)

    flash('Devolución registrada: %d de "%s".'
          % (cantidad, fila['descripcion']), 'success')

    # De acá para abajo es todo DESPUES del commit y a proposito, con la misma
    # regla que la venta de mostrador: la devolucion ya esta guardada y ningun
    # problema para avisarle a Tiendanube la puede voltear.
    if producto_id is None:
        if fila['sin_producto']:
            flash('El ítem no está asociado a ningún producto del catálogo, '
                  'así que no hubo stock que corregir.', 'warning')
        elif fila['sin_control']:
            flash('Ese producto no lleva control de stock, así que no hubo '
                  'nada que sumar.', 'warning')
    else:
        try:
            fallidos = stock_tiendanube.empujar_stock(
                current_user.empresa_id, [producto_id])
        except Exception:  # noqa: BLE001
            # Igual que en la venta: el rollback va primero porque si lo que
            # se rompio dejo la sesion sucia, leer el nombre para el aviso
            # volveria a fallar. La devolucion ya esta commiteada.
            db.session.rollback()
            fallidos = [fila['descripcion']]

        if fallidos:
            flash('Devolución guardada, pero no se pudo actualizar el stock en '
                  'Tiendanube para: %s — revisalo a mano.' % ', '.join(fallidos),
                  'warning')

    return redirect(url_for('devoluciones.listar_devoluciones'))


@devoluciones_bp.route('/listar')
@login_required
def listar_devoluciones():
    """Todo lo que volvio, una fila por evento.

    Se muestran TODAS las filas, incluidas las que un evento posterior dejo sin
    efecto, y por eso hay una columna que dice cual esta vigente. Es una tabla
    append-only: esconder las filas viejas seria tirar justamente la historia
    que la tabla existe para guardar.
    """
    empresa_id = current_user.empresa_id

    devoluciones = (db.session.query(Devolucion)
                    .join(Pedido, Pedido.id == Devolucion.pedido_id)
                    .options(joinedload(Devolucion.pedido).joinedload(Pedido.canal),
                             joinedload(Devolucion.pedido_item))
                    .filter(Pedido.empresa_id == empresa_id)
                    .order_by(Devolucion.fecha_evento.desc(), Devolucion.id.desc())
                    .all())

    # Que filas tienen un sucesor. Una consulta sola en vez de preguntarselo a
    # cada fila, que serian N viajes para pintar una columna.
    superadas = {fila[0] for fila in
                 db.session.query(Devolucion.evento_previo_id)
                 .filter(Devolucion.evento_previo_id.isnot(None))
                 .all()}

    filas = []
    for devolucion in devoluciones:
        pedido = devolucion.pedido
        canal = pedido.canal if pedido else None
        item = devolucion.pedido_item
        filas.append({
            'fecha': devolucion.fecha_evento,
            'pedido': _etiqueta_pedido(pedido) if pedido else '—',
            'canal': ETIQUETA_CANAL.get(canal.tipo if canal else None,
                                        canal.nombre if canal else '—'),
            'producto': item.descripcion if item else '—',
            'cantidad': devolucion.cantidad,
            'monto': devolucion.monto,
            'moneda': devolucion.moneda,
            'motivo': devolucion.motivo or '',
            'estado': devolucion.estado,
            'vigente': devolucion.id not in superadas,
        })

    return render_template('devoluciones_listar.html', filas=filas)
