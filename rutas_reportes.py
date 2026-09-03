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
"""

from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal

from flask import Blueprint, render_template, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from models import Pedido
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
