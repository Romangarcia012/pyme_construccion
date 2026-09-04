# -*- coding: utf-8 -*-
"""FASE-REPORTES-S3-MARGEN-BUILD: cuanto deja de verdad cada venta.

Es la pantalla a la que apuntaban todas las slices anteriores de S3. Ya
existian las tres piezas del costo por separado -- el costo de la mercaderia
congelado en cada linea (`pedido_item.costo_unitario_snapshot`), la comision
del canal cargada a mano (`pedido.comision_plataforma`) y lo que el flete le
costo al vendedor (`pedido.costo_envio_vendedor`, que baja del payload) --
pero no habia ningun lugar donde se restaran del ingreso.

LA CUENTA

    ingreso_neto = total_bruto - total_descuentos + total_envio
    costo_total  = SUM(costo_unitario_snapshot * cantidad)
                   + comision_plataforma + costo_envio_vendedor
    ganancia     = ingreso_neto - costo_total
    margen_pct   = ganancia / ingreso_neto
    margen_mercaderia_pct = ganancia / (ingreso_neto - total_envio)

Las dos ultimas no son la misma pregunta. `margen_pct` es sobre toda la plata
que entro, flete incluido, y contesta "de lo que facturamos, cuanto quedo".
`margen_mercaderia_pct` saca el envio de los dos lados -- del ingreso y, via
costo_envio_vendedor, del costo -- y contesta "cuanto deja el producto", que
es lo que se compara contra el Excel y lo que decide un precio. Un pedido
donde el flete pasa derecho (el comprador paga exactamente lo que sale) tiene
los mismos pesos de ganancia en las dos, pero porcentajes muy distintos: el
envio infla el denominador de la primera sin aportar nada.

TODO O NADA POR PEDIDO

Un pedido entra al calculo solo si sus TRES componentes de costo son no-NULL.
Si falta uno, no se estima ni se completa con cero: el pedido sale de todas
las sumas y aparece en su propio bloque diciendo que le falta. Un margen
calculado sobre un costo que en realidad nadie cargo sale inflado y no hay
nada en la pantalla que lo delate -- por eso el faltante se muestra en vez de
taparse.

NULL no es 0, y esa distincion es de las piezas de abajo (`_leer_comision`,
`envio_del_vendedor`), no de este modulo: aca un 0 explicito es un dato
cargado y entra al calculo como cualquier otro numero.

QUE NO HACE

No prorratea nada por linea. La comision y el flete son del PEDIDO, no del
producto, y repartirlos entre las lineas inventaria una precision que el dato
no tiene. De ahi sale la unica rareza de la pantalla: el corte por producto
agrupa solo los pedidos de UNA linea, y los de varias van a un bloque aparte
sin descomponer.

Solo lee: no escribe una fila, no genera migracion, no toca el modelo.

El blueprint se registra en app.py; ninguna ruta existente se toca.

FASE-CAJA-SOCIO-S1 agrego una segunda pantalla en el mismo blueprint,
/reportes/caja-socio, que contesta otra pregunta -- cuanto factura cada socio
-- y no comparte ni una linea de calculo con esta. Su documentacion esta al
pie del archivo, arriba de su propia ruta.
"""

from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal

from flask import Blueprint, render_template, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from models import (
    ORIGEN_CAPITAL,
    ORIGEN_FACTURACION,
    SOCIOS,
    CanalVenta,
    CuentaCobro,
    Gasto,
    Pedido,
    db,
)
from rutas_devoluciones import devuelto_por_item
from rutas_productos import (
    ESTADOS_NO_VENDIDOS,
    ETIQUETA_SIN_IDENTIFICAR,
    _clave_de_grupo,
    _mapeos_por_producto,
    _partir_nombre,
)

reportes_bp = Blueprint('reportes', __name__, url_prefix='/reportes')

CERO = Decimal('0.00')

# Los porcentajes se muestran con un decimal. Mas precision no cambia ninguna
# decision de precio y hace la tabla ilegible; menos esconde la diferencia
# entre dos productos parecidos.
PASO_PORCENTAJE = Decimal('0.1')


def _decimal(valor):
    """Una columna Numeric que puede venir en None -> Decimal.

    Solo se usa sobre las columnas NOT NULL del pedido (total_bruto,
    total_descuentos, total_envio, total), que igual pueden llegar en None si
    una fila se escribio sin pasar por el default. Las que de verdad
    significan algo cuando son NULL -- comision, flete, snapshot -- nunca
    pasan por aca: se preguntan antes, en `_faltantes`.
    """
    return CERO if valor is None else valor


def _porcentaje(parte, base):
    """parte/base en porciento, o None si la base no permite dividir.

    Base cero no es margen cero: es una pregunta sin respuesta (un pedido
    regalado, un grupo que todavia no facturo). El None llega crudo a la
    plantilla y sale como un guion.
    """
    if not base:
        return None
    return (parte / base * 100).quantize(PASO_PORCENTAJE, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Que le falta a un pedido para poder calcularle el margen
# --------------------------------------------------------------------------
# Cada faltante dice tambien DONDE se carga, porque el reporte no sirve de
# nada si quien lo mira no sabe adonde ir. Los dos primeros tienen pantalla;
# el flete del vendedor no, y eso se dice tal cual en vez de mandar a alguien
# a buscar un formulario que no existe.


def _faltantes(pedido):
    """Lo que impide calcular el margen de este pedido, en criollo."""
    faltan = []

    if not pedido.items:
        # No deberia pasar -- ni el sync ni la venta manual escriben un pedido
        # sin lineas -- pero si pasa, sumarle costo cero seria afirmar que la
        # venta fue toda ganancia.
        faltan.append({
            'que': 'el pedido no tiene líneas',
            'donde': None,
            'url': None,
        })

    sin_costo = [item.descripcion for item in pedido.items
                 if item.costo_unitario_snapshot is None]
    if sin_costo:
        faltan.append({
            'que': 'el costo de %s' % ', '.join('"%s"' % nombre for nombre in sin_costo),
            'donde': 'Stock y Costos',
            'url': url_for('productos.listar_stock'),
        })

    if pedido.comision_plataforma is None:
        faltan.append({
            'que': 'la comisión de la plataforma',
            'donde': 'Ver Ventas',
            'url': url_for('ventas.listar_pedidos'),
        })

    if pedido.costo_envio_vendedor is None:
        # Este no tiene pantalla: lo baja el sync de Tiendanube desde el
        # payload del pedido. Un pedido sincronizado antes de que existiera la
        # columna se llena solo en la proxima corrida.
        faltan.append({
            'que': 'el costo del envío para el vendedor',
            'donde': 'lo trae el sync de Tiendanube',
            'url': None,
        })

    return faltan


def _calcular(pedido):
    """Los numeros del pedido, o None si le falta algun componente.

    El costo sale del snapshot de la linea y NO del producto: el costo vigente
    pudo cambiar despues de la venta, y usarlo aca reescribiria el margen de
    todo el historico cada vez que alguien corrige un costo.
    """
    if _faltantes(pedido):
        return None

    envio = _decimal(pedido.total_envio)
    ingreso_neto = (_decimal(pedido.total_bruto)
                    - _decimal(pedido.total_descuentos)
                    + envio)

    costo_mercaderia = sum(
        (item.costo_unitario_snapshot * item.cantidad for item in pedido.items),
        CERO)
    costo_total = (costo_mercaderia
                   + pedido.comision_plataforma
                   + pedido.costo_envio_vendedor)

    ganancia = ingreso_neto - costo_total

    return {
        'ingreso_neto': ingreso_neto,
        'envio': envio,
        'costo_mercaderia': costo_mercaderia,
        'costo_total': costo_total,
        'ganancia': ganancia,
        'margen_pct': _porcentaje(ganancia, ingreso_neto),
        'margen_mercaderia_pct': _porcentaje(ganancia, ingreso_neto - envio),
        'unidades': sum(item.cantidad for item in pedido.items),
    }


def _cierra(pedido, calculo):
    """El ingreso reconstruido, ¿coincide con el total que guardo el canal?

    Es un control de sanidad sobre el payload, no sobre la cuenta: si
    bruto - descuentos + envio no da el total que el propio pedido tiene
    guardado, entonces alguna de las cuatro columnas no significa lo que este
    reporte cree (un impuesto que se sumo aparte, un descuento contado dos
    veces). La fila se muestra igual, marcada: esconderla dejaria el reporte
    mintiendo en silencio.
    """
    return calculo['ingreso_neto'] == _decimal(pedido.total)


# --------------------------------------------------------------------------
# Los cortes
# --------------------------------------------------------------------------


def _nuevo_grupo(nombre):
    """El acumulador de un corte, con todo en cero y ningun pedido adentro."""
    return {
        'nombre': nombre,
        'productos': [],       # solo lo usa el corte por producto, para el nombre
        'pedidos': 0,          # los que entraron al calculo
        'pedidos_totales': 0,  # los que caen en este grupo, completos o no
        'incompletos': 0,
        'descuadres': 0,
        'unidades': 0,
        # FASE-DEVOLUCIONES-S2. NO se acumula en `_acumular` junto con el resto
        # y es a proposito: `_acumular` corre solo sobre los pedidos que tienen
        # los tres costos cargados, y lo devuelto no depende de eso. Un pedido
        # al que le falta la comision cae en "Sin margen" pero su devolucion
        # existe igual, y esconderla haria que la columna dependiera de si
        # Roman ya cargo un numero que no tiene nada que ver.
        'devuelto': 0,
        'ingreso_neto': CERO,
        'envio': CERO,
        'costo_total': CERO,
        'ganancia': CERO,
    }


def _acumular(grupo, calculo):
    grupo['pedidos'] += 1
    grupo['unidades'] += calculo['unidades']
    grupo['ingreso_neto'] += calculo['ingreso_neto']
    grupo['envio'] += calculo['envio']
    grupo['costo_total'] += calculo['costo_total']
    grupo['ganancia'] += calculo['ganancia']


def _cerrar(grupo):
    """Los porcentajes del grupo, calculados sobre los totales.

    Es la unica forma correcta de agregarlos: promediar los porcentajes de
    cada pedido le daria el mismo peso a una venta de $500 que a una de
    $50.000, y el numero resultante no seria el margen de nada.
    """
    grupo['margen_pct'] = _porcentaje(grupo['ganancia'], grupo['ingreso_neto'])
    grupo['margen_mercaderia_pct'] = _porcentaje(
        grupo['ganancia'], grupo['ingreso_neto'] - grupo['envio'])
    return grupo


def _clave_de_producto(item, mapeos_por_producto):
    """En que fila del corte por producto cae la linea de un pedido.

    Es `_clave_de_grupo` de FASE-REPORTES-S1 con `con_canal=False`: las
    variantes de color se siguen juntando por el id del producto padre, pero
    el canal sale de la clave. En S1 el canal era una COLUMNA y tenia sentido
    que partiera; aca la pregunta es cuanto deja el producto, y el mismo
    producto vendido por dos canales tiene que sumar en una sola fila. El
    corte por canal, que esta al lado, contesta la otra mitad.

    Una linea que ningun mapeo pudo atar a un producto (producto_id NULL, ya
    contemplado desde FASE3-S2) cae en su propio renglon en vez de perderse.
    """
    if item.producto is None:
        return ('sin-producto',)
    return _clave_de_grupo(item.producto, mapeos_por_producto, con_canal=False)


def _nombre_de_grupo(productos):
    """El encabezado de una fila del corte por producto.

    Mismo criterio que S1: si todas las variantes comparten el nombre base se
    usa ese, y si no se muestra el nombre completo de la primera. Fabricar un
    nombre comun a partir de textos que no lo tienen es peor que repetir uno.
    """
    if not productos:
        return ETIQUETA_SIN_IDENTIFICAR
    bases = [_partir_nombre(producto.nombre)[0] for producto in productos]
    if len(set(bases)) == 1 and bases[0]:
        return bases[0]
    return productos[0].nombre


def _etiqueta_pedido(pedido):
    """Como se nombra un pedido en los bloques que van a nivel pedido.

    El numero del canal es lo que Roman tiene delante cuando cruza contra la
    liquidacion; la venta de mostrador no tiene ninguno y cae en la fecha.
    Mismo criterio que `rutas_ventas._etiqueta_pedido`.
    """
    if pedido.numero_externo:
        return '#%s' % pedido.numero_externo
    if pedido.fecha_pedido:
        return 'del %s' % pedido.fecha_pedido.strftime('%d/%m/%Y')
    return 'id %s' % pedido.id


@reportes_bp.route('/margen')
@login_required
def margen():
    """Ganancia y margen por producto y por canal.

    Un solo barrido de pedidos alimenta los cuatro bloques: los dos cortes,
    los pedidos de varias lineas y los que no se pudieron calcular. Sin
    filtros ni rango de fechas -- se veran cuando haya volumen que lo pida.
    """
    empresa_id = current_user.empresa_id

    # Mismo filtro de cancelados que S1: un pedido dado de baja no vendio ni
    # costo nada, y contarlo ensuciaria las dos puntas de la cuenta.
    pedidos = (Pedido.query
               .options(joinedload(Pedido.canal),
                        joinedload(Pedido.items))
               .filter(Pedido.empresa_id == empresa_id)
               .filter(func.lower(Pedido.estado).notin_(ESTADOS_NO_VENDIDOS))
               .order_by(Pedido.fecha_pedido.desc(), Pedido.id.desc())
               .all())

    # Los mapeos de todos los productos vendidos, de una sola vez: son los que
    # deciden que variantes van juntas, y pedirlos por fila serian N+1 viajes.
    ids_producto = {item.producto_id for pedido in pedidos for item in pedido.items
                    if item.producto_id}
    mapeos = _mapeos_por_producto(ids_producto)

    # Unidades devueltas por linea. Se muestra al lado del margen sin entrar en
    # ninguna cuenta: ingreso, costo y ganancia dan lo mismo que antes de esta
    # slice. Restarlas obligaria a decidir tambien que pasa con el costo de la
    # mercaderia que volvio, y esa pregunta todavia no esta contestada.
    devuelto_items = devuelto_por_item(empresa_id)

    por_producto = OrderedDict()
    por_canal = OrderedDict()
    multilinea = []
    incompletos = []
    descuadres = []

    for pedido in pedidos:
        canal = pedido.canal
        grupo_canal = por_canal.get(pedido.canal_id)
        if grupo_canal is None:
            # Solo los canales que vendieron algo. A diferencia de S1 -- donde
            # un canal en cero es una columna que informa "por ahi no sale
            # nada" -- aca una fila sin pedidos no tiene ni ganancia ni margen
            # que mostrar: seria un renglon de guiones.
            grupo_canal = _nuevo_grupo(canal.nombre if canal else '—')
            por_canal[pedido.canal_id] = grupo_canal

        # El corte por producto solo admite pedidos de UNA linea. Con dos
        # productos en el mismo pedido no hay forma de saber cuanto de la
        # comision y del flete le toca a cada uno, y repartirlo seria inventar.
        grupo_producto = None
        if len(pedido.items) == 1:
            item = pedido.items[0]
            clave = _clave_de_producto(item, mapeos)
            grupo_producto = por_producto.get(clave)
            if grupo_producto is None:
                grupo_producto = _nuevo_grupo(None)
                por_producto[clave] = grupo_producto
            if (item.producto is not None
                    and item.producto not in grupo_producto['productos']):
                grupo_producto['productos'].append(item.producto)

        grupo_canal['pedidos_totales'] += 1
        if grupo_producto is not None:
            grupo_producto['pedidos_totales'] += 1

        # Antes del corte por faltantes de mas abajo, para que una devolucion
        # de un pedido incompleto igual se vea (ver `_nuevo_grupo`).
        devuelto_pedido = sum(devuelto_items.get(item.id, 0)
                              for item in pedido.items)
        grupo_canal['devuelto'] += devuelto_pedido
        if grupo_producto is not None:
            grupo_producto['devuelto'] += devuelto_pedido

        faltan = _faltantes(pedido)
        if faltan:
            grupo_canal['incompletos'] += 1
            if grupo_producto is not None:
                grupo_producto['incompletos'] += 1
            incompletos.append({
                'etiqueta': _etiqueta_pedido(pedido),
                'fecha': pedido.fecha_pedido,
                'canal': canal.nombre if canal else '—',
                'total': pedido.total,
                'faltan': faltan,
            })
            continue

        calculo = _calcular(pedido)
        cierra = _cierra(pedido, calculo)

        if not cierra:
            grupo_canal['descuadres'] += 1
            if grupo_producto is not None:
                grupo_producto['descuadres'] += 1
            descuadres.append({
                'etiqueta': _etiqueta_pedido(pedido),
                'fecha': pedido.fecha_pedido,
                'canal': canal.nombre if canal else '—',
                'ingreso_neto': calculo['ingreso_neto'],
                'total': pedido.total,
            })

        _acumular(grupo_canal, calculo)
        if grupo_producto is not None:
            _acumular(grupo_producto, calculo)
        else:
            # Los de varias lineas no se descomponen: se muestran enteros, a
            # nivel pedido, con el detalle de que productos llevaban. Del
            # corte por canal SI participan -- ahi la unidad es el pedido y no
            # hay nada que repartir.
            fila = dict(calculo)
            fila.update({
                'etiqueta': _etiqueta_pedido(pedido),
                'fecha': pedido.fecha_pedido,
                'canal': canal.nombre if canal else '—',
                'productos': ', '.join(item.descripcion for item in pedido.items),
                'cierra': cierra,
            })
            multilinea.append(fila)

    for grupo in por_producto.values():
        grupo['nombre'] = _nombre_de_grupo(grupo['productos'])
        # El objeto Producto no tiene por que viajar a la plantilla: ya cumplio
        # su unica funcion, que era armar el nombre del grupo.
        grupo.pop('productos')
        _cerrar(grupo)

    filas_producto = sorted(por_producto.values(),
                            key=lambda grupo: grupo['nombre'].lower())
    filas_canal = [_cerrar(grupo) for grupo in por_canal.values()]

    return render_template('reportes_margen.html',
                           productos=filas_producto,
                           canales=filas_canal,
                           multilinea=multilinea,
                           incompletos=incompletos,
                           descuadres=descuadres,
                           total_pedidos=len(pedidos))


# ==========================================================================
# FASE-CAJA-SOCIO-S1 -- Cuanto factura cada socio
# --------------------------------------------------------------------------
# QUE ES Y QUE NO ES
#
# Esto NO es plata reconciliada contra Mercado Pago. Es FACTURACION: cuanto
# deberia haber cobrado cada socio segun por que canal se vendio. La cuenta de
# Roman cobra Tiendanube y las ventas presenciales, la de Nachi cobra Mercado
# Libre. Si manana el Mercado Pago de alguno muestra otro numero, la diferencia
# es un error de esa persona -- cobro por fuera, se olvido de cargar algo -- y
# no algo que este reporte tenga que detectar. Por eso no toca ni necesita
# CuentaCobro/Pago/Liquidacion/MovimientoCuenta: la cadena entera es
# pedido -> canal -> cuenta de cobro -> socio, y termina ahi.
#
# POR QUE pedido.total Y NO ingreso_neto NI total_bruto
#
# La pregunta es "cuanta plata le entro a esta persona", y lo que le entra es
# lo que el comprador pago: el producto, menos el descuento, mas el envio y los
# impuestos. Eso es `total`, el mismo numero que el reporte de margen usa como
# control (`_cierra`) y el que figura en el listado de ventas.
#
#   - total_bruto queda afuera porque es antes de descuentos y sin envio: nadie
#     cobro nunca ese numero.
#   - ingreso_neto queda afuera porque es una cuenta del reporte de MARGEN
#     (bruto - descuentos + envio), armada para compararse contra costos. Ahi
#     tiene sentido reconstruirla; aca lo que se quiere es el monto que se
#     cobro, y ese ya esta guardado.
#
# DE DONDE SALE EL SOCIO
#
# De cuenta_cobro.socio, el campo de vocabulario fijo que agrega esta misma
# slice. Antes habia que leer cuenta_cobro.nombre y adivinar: renombrar una
# cuenta cambiaba la atribucion sin que nada avisara.
#
# LO QUE NO SE OCULTA
#
#   - Un socio sin ventas sale igual, en cero, con sus canales listados. Hoy le
#     pasa a Nachi: Mercado Libre esta apagado. Sacarlo de la pantalla haria
#     ver un reparto de dos personas como si fuera de una sola.
#   - Un canal cuya cuenta no tiene socio -- o que no tiene cuenta -- no se
#     reparte entre los conocidos ni se descarta: cae en su propia fila. Plata
#     sin dueño se ve.
# ==========================================================================

# Como se titula la fila de lo que no se le pudo atribuir a nadie. No es un
# socio: es una pregunta abierta.
ETIQUETA_SIN_SOCIO = 'Sin socio asignado'


def _facturado_por_canal(empresa_id):
    """{canal_id: suma de pedido.total}, con el mismo filtro de cancelados.

    Un pedido cancelado no le entro a nadie. Se excluye con el mismo criterio
    que el resto de los reportes (`ESTADOS_NO_VENDIDOS`), asi los numeros de
    esta pantalla se pueden cruzar contra los de margen sin explicaciones.

    Se suma en la base y no en Python a proposito: aca no hace falta abrir cada
    pedido -- no hay costos, ni items, ni snapshots -- y traerlos todos para
    sumar una columna serian miles de filas para llegar a dos numeros.
    """
    filas = (db.session.query(Pedido.canal_id,
                              func.coalesce(func.sum(Pedido.total), 0))
             .filter(Pedido.empresa_id == empresa_id)
             .filter(func.lower(Pedido.estado).notin_(ESTADOS_NO_VENDIDOS))
             .group_by(Pedido.canal_id)
             .all())
    return {canal_id: _decimal(total) for canal_id, total in filas}


def _pedidos_por_canal(empresa_id):
    """{canal_id: cuantos pedidos}, mismo filtro. Es el respaldo del monto.

    Sin el contador, un canal en cero no se distingue de un canal que vendio y
    devolvio todo, y son dos situaciones muy distintas.
    """
    filas = (db.session.query(Pedido.canal_id, func.count(Pedido.id))
             .filter(Pedido.empresa_id == empresa_id)
             .filter(func.lower(Pedido.estado).notin_(ESTADOS_NO_VENDIDOS))
             .group_by(Pedido.canal_id)
             .all())
    return {canal_id: int(cantidad or 0) for canal_id, cantidad in filas}


def _gastado_por_socio(empresa_id):
    """{clave_de_socio: (cuanto salio de su cuenta, cuantos gastos)}.

    Solo los gastos con origen_fondo='facturacion' (FASE-CAJA-GENERAL-S3):
    son los unicos que salieron de la plata que entra por las ventas, y por
    lo tanto los unicos que le bajan el saldo a un socio en particular.

    Los de 'capital' NO entran aca a proposito. Salieron del pool que
    aportaron los socios, que es plata ajena a la facturacion: restarlos de lo
    que factura Roman diria que le queda menos de lo que realmente le queda.
    Se muestran aparte, como referencia.

    Los que tienen origen_fondo NULL tampoco entran, y por el motivo
    contrario: no se sabe de que bolsillo salieron, y elegir uno seria
    adivinar. Se cuentan por separado (`_gastos_sin_clasificar`) para que el
    faltante se vea en la pantalla en vez de esconderse dentro de un saldo.

    El socio sale del JOIN a cuenta_cobro y no del id de la cuenta: es el
    mismo criterio que el resto de la pantalla -- de quien es una cuenta lo
    dice `cuenta_cobro.socio`, no su nombre ni su id.
    """
    filas = (db.session.query(CuentaCobro.socio,
                              func.coalesce(func.sum(Gasto.monto), 0),
                              func.count(Gasto.id))
             .join(CuentaCobro, Gasto.cuenta_pago_id == CuentaCobro.id)
             .filter(Gasto.empresa_id == empresa_id)
             .filter(Gasto.origen_fondo == ORIGEN_FACTURACION)
             .group_by(CuentaCobro.socio)
             .all())
    # Una cuenta sin socio cae en la clave None, que es la misma fila donde ya
    # va la facturacion sin dueno. Los dos numeros de esa fila hablan de la
    # misma plata sin identificar, asi que se restan entre si igual que los de
    # cualquier socio.
    return {socio: (Decimal(monto or 0), int(cantidad or 0))
            for socio, monto, cantidad in filas}


def _total_gastos(empresa_id, condicion):
    """(suma, cantidad) de los gastos de la empresa que cumplen `condicion`.

    Se suma en la base por lo mismo que `_facturado_por_canal`: para llegar a
    dos numeros no hace falta abrir cada gasto.
    """
    monto, cantidad = (db.session.query(func.coalesce(func.sum(Gasto.monto), 0),
                                        func.count(Gasto.id))
                       .filter(Gasto.empresa_id == empresa_id)
                       .filter(condicion)
                       .one())
    return Decimal(monto or 0), int(cantidad or 0)


def _nuevo_socio(clave, nombre):
    return {
        'clave': clave,
        'nombre': nombre,
        # `total` es lo FACTURADO: la suma de pedido.total de sus canales.
        # Es un total historico de ventas y no cambia con esta slice.
        'total': CERO,
        # FASE-CAJA-GENERAL-S3: lo que ya salio de esa misma cuenta.
        'gastado': CERO,
        'gastos': 0,
        # facturado - gastado_de_ahi. Es la plata que deberia quedar en la
        # cuenta si nadie la toco por fuera del sistema. Puede dar negativo, y
        # que se vea es el punto: significa que se pago mas de lo que entro.
        'saldo_real': CERO,
        'pedidos': 0,
        'canales': [],
    }


@reportes_bp.route('/caja-socio')
@login_required
def caja_socio():
    """Cuanto factura cada socio, cuanto ya gasto de ahi, y que le queda.

    Una fila por socio con el desglose de sus canales debajo. Sin rango de
    fechas: es el acumulado, igual que el resto de los reportes de la fase.

    LA CUENTA (FASE-CAJA-GENERAL-S3)

        facturado    = SUM(pedido.total) de sus canales, sin cancelados
        gastado      = SUM(gasto.monto) con origen_fondo='facturacion' y
                       cuenta_pago_id apuntando a la cuenta de ese socio
        saldo_real   = facturado - gastado

    Hasta esta slice la pantalla mostraba solo el primero, y ese numero se
    leia como "lo que tiene Roman" cuando en realidad es "lo que Roman
    facturo desde siempre". Son dos cosas muy distintas en cuanto se paga el
    primer proveedor. Por eso van los dos juntos, uno debajo del otro: el
    historico no se saca -- sigue haciendo falta para cuadrar contra las
    ventas -- pero deja de ser el unico numero de la fila.

    LO QUE NO RESTA

    Los gastos de capital no le bajan el saldo a ningun socio: salieron del
    pool que aportaron los tres, no de lo que factura este canal. Van en su
    propio bloque al pie, como referencia.

    Los gastos sin origen tampoco. No se sabe de que bolsillo salieron y
    elegir uno seria inventar el dato; se cuentan aparte para que se vea que
    faltan por clasificar, en vez de que el saldo mienta por omision.
    """
    empresa_id = current_user.empresa_id

    facturado = _facturado_por_canal(empresa_id)
    contados = _pedidos_por_canal(empresa_id)
    gastado = _gastado_por_socio(empresa_id)

    # Se parte de los CANALES, no de los pedidos: un canal que todavia no
    # vendio nada tiene que aparecer igual (Mercado Libre hoy), y si el barrido
    # empezara por los pedidos ese canal no existiria en la pantalla.
    canales = (CanalVenta.query
               .options(joinedload(CanalVenta.cuenta_cobro))
               .filter(CanalVenta.empresa_id == empresa_id)
               .order_by(CanalVenta.id)
               .all())

    # Los socios del vocabulario nacen todos, en cero y en su orden, antes de
    # mirar una sola venta. Asi la pantalla no depende de que canales existan.
    socios = OrderedDict((clave, _nuevo_socio(clave, nombre))
                         for clave, nombre in SOCIOS.items())

    for canal in canales:
        cuenta = canal.cuenta_cobro
        clave = cuenta.socio if cuenta is not None else None

        grupo = socios.get(clave)
        if grupo is None:
            # Cae aca el canal sin cuenta de cobro y el que tiene una cuenta
            # sin socio. Los dos casos van al mismo lugar porque la respuesta
            # es la misma -- "no se sabe de quien es esta plata" -- y el
            # detalle de cual de los dos es se lee en la columna de la cuenta.
            grupo = socios.get(None)
            if grupo is None:
                grupo = _nuevo_socio(None, ETIQUETA_SIN_SOCIO)
                socios[None] = grupo

        total = facturado.get(canal.id, CERO)
        pedidos = contados.get(canal.id, 0)

        grupo['total'] += total
        grupo['pedidos'] += pedidos
        grupo['canales'].append({
            'nombre': canal.nombre,
            'tipo': canal.tipo,
            'activo': canal.activo,
            'cuenta': cuenta.nombre if cuenta is not None else None,
            'total': total,
            'pedidos': pedidos,
        })

    # El gasto se imputa DESPUES de repartir los canales, y sobre `socios`
    # directamente: un socio puede tener gastos pagados desde su cuenta sin
    # tener todavia un canal que cobre ahi, y ese caso tiene que salir igual.
    for clave, (monto, cantidad) in gastado.items():
        grupo = socios.get(clave)
        if grupo is None:
            grupo = _nuevo_socio(clave, SOCIOS.get(clave, ETIQUETA_SIN_SOCIO))
            socios[clave] = grupo
        grupo['gastado'] += monto
        grupo['gastos'] += cantidad

    for grupo in socios.values():
        grupo['saldo_real'] = grupo['total'] - grupo['gastado']

    filas = list(socios.values())

    # Los dos bloques que se muestran APARTE, sin restarle a nadie.
    capital_monto, capital_cantidad = _total_gastos(
        empresa_id, Gasto.origen_fondo == ORIGEN_CAPITAL)
    sin_clasificar_monto, sin_clasificar_cantidad = _total_gastos(
        empresa_id, Gasto.origen_fondo.is_(None))

    return render_template('reportes_caja_socio.html',
                           socios=filas,
                           total_general=sum((fila['total'] for fila in filas),
                                             CERO),
                           pedidos_totales=sum(fila['pedidos'] for fila in filas),
                           gastado_total=sum((fila['gastado'] for fila in filas),
                                             CERO),
                           saldo_real_total=sum((fila['saldo_real']
                                                 for fila in filas), CERO),
                           capital_monto=capital_monto,
                           capital_cantidad=capital_cantidad,
                           sin_clasificar_monto=sin_clasificar_monto,
                           sin_clasificar_cantidad=sin_clasificar_cantidad)
