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

from collections import OrderedDict, namedtuple
from decimal import ROUND_HALF_UP, Decimal

from datetime import date, datetime

from flask import Blueprint, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.orm import joinedload

from models import (
    # FASE-RESULTADOS-S1: activo o gasto, la naturaleza de la categoria.
    NATURALEZA_ACTIVO,
    NATURALEZA_GASTO,
    ORIGEN_CAPITAL,
    ORIGEN_FACTURACION,
    # FASE-GASTO-MIXTO-S1: un gasto pagado entre varias cuentas y su reparto.
    ORIGEN_MIXTO,
    SOCIOS,
    CanalVenta,
    Categoria,
    CuentaCobro,
    Gasto,
    GastoOrigenMixto,
    Ingreso,
    Pedido,
    Producto,
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
        # FASE-RESULTADOS-S1: los dos costos del pedido, sueltos, para que el
        # resultado del negocio los escriba en su propio renglon sumando lo
        # mismo que aca se resta y no recalculandolo.
        'comision': pedido.comision_plataforma,
        'envio_vendedor': pedido.costo_envio_vendedor,
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
    los pedidos de varias lineas y los que no se pudieron calcular.

    FASE-RESULTADOS-S1 le agrego arriba el "Resultado del negocio" y el bloque
    de "Activos", y un filtro por mes (`?periodo=AAAA-MM`; sin el, todo). El
    resultado sale de ESTOS mismos pedidos -- se le pasan, no los vuelve a
    pedir -- y por eso su utilidad bruta es por construccion la suma de la
    ganancia de los pedidos completos de abajo.
    """
    empresa_id = current_user.empresa_id
    periodo = _periodo_de(request.args.get('periodo'))

    # FASE-RESULTADOS-S1: los mismos dos filtros que caja-socio
    # (`_es_venta`). Hasta aca solo salian los cancelados, y los regalos
    # entraban como ventas de $0 con su costo adentro: el Sorteo y el
    # microfono regalado restaban $26.116,72 de "ganancia" que no era la
    # rentabilidad de ninguna venta. El costo de lo regalado no se pierde: es
    # un renglon propio del resultado, como marketing.
    pedidos = _pedidos_vendidos(empresa_id, periodo)

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

    resultado = resultado_del_negocio(empresa_id, periodo, pedidos=pedidos)
    # Los activos son una foto de HOY y no del mes: el stock que hay no
    # depende de que periodo se este mirando. La conciliacion compara contra
    # todo lo comprado, asi que necesita el costo de lo vendido de siempre.
    resultado_historico = (resultado if periodo is None
                           else resultado_del_negocio(empresa_id))
    activos = activos_del_negocio(empresa_id, resultado_historico)

    return render_template('reportes_margen.html',
                           productos=filas_producto,
                           canales=filas_canal,
                           multilinea=multilinea,
                           incompletos=incompletos,
                           descuadres=descuadres,
                           total_pedidos=len(pedidos),
                           resultado=resultado,
                           activos=activos,
                           periodo=periodo,
                           periodos=_periodos_disponibles(empresa_id))


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
# Y de que cuenta se trata lo dice, para cada pedido, la misma regla de dos
# pasos que escribe `Pedido.cuenta_cobro_efectiva` (FASE-CAJA-SOCIO-S2):
# primero `pedido.cuenta_cobro_override_id` si esta cargado, y si no -- que es
# lo que pasa siempre -- la cuenta del canal. El override es la excepcion
# puntual, no una segunda regla general: existe para arreglar casos como el de
# "ventas Meli", una venta cargada por el canal manual que en la realidad
# cobro Nachi. Aca se resuelve en Python y no en la consulta a proposito: la
# consulta agrupa por el override crudo y es `_destinos_del_canal` quien sabe
# a que cuenta cobra cada canal.
#
# LO QUE NO SE OCULTA
#
#   - Un socio sin ventas sale igual, en cero, con sus canales listados. Hoy le
#     pasa a Nachi: Mercado Libre esta apagado. Sacarlo de la pantalla haria
#     ver un reparto de dos personas como si fuera de una sola.
#   - Un canal cuya cuenta no tiene socio -- o que no tiene cuenta -- no se
#     reparte entre los conocidos ni se descarta: cae en su propia fila. Plata
#     sin dueño se ve.
#   - Un canal con ventas corregidas aparece debajo de LOS DOS socios, una vez
#     por cada cuenta a la que fue a parar su plata, y la linea corregida se
#     marca como tal. Ni se esconde de donde vino la venta ni se disfraza una
#     correccion a mano de regla del canal.
# ==========================================================================

# Como se titula la fila de lo que no se le pudo atribuir a nadie. No es un
# socio: es una pregunta abierta.
ETIQUETA_SIN_SOCIO = 'Sin socio asignado'


def _es_venta():
    """Los dos filtros que deciden que un pedido ES una venta.

    Se escriben UNA vez y viajan como lista para que quien liste pedidos y
    quien los sume apliquen literalmente el mismo criterio:

        cancelado   no le entro a nadie (`ESTADOS_NO_VENDIDOS`, el mismo
                    criterio que margen y que devoluciones)
        regalo      la mercaderia salio del deposito pero no entro un peso
                    (FASE-CAJA-SOCIO-S5)

    Hasta FASE-DASHBOARD-MOVIMIENTOS-S1 estas dos lineas vivian sueltas dentro
    de `_facturado_por_canal` y no hacia falta mas: era el unico lugar que
    contestaba "esto es una venta". Al aparecer el segundo -- los ultimos
    movimientos del dashboard, que no suman sino que listan -- copiarlas seria
    la forma de que un dia dejen de ser iguales y la lista muestre un pedido
    que el total no cuenta, sin que nada avise.

    Devuelve criterios, no una consulta: las dos consultas tienen forma
    distinta (una agrupa y suma, la otra trae filas y ordena) y lo unico que
    comparten es a que pedidos miran.
    """
    return [func.lower(Pedido.estado).notin_(ESTADOS_NO_VENDIDOS),
            Pedido.es_regalo.is_(False)]


def _facturado_por_canal(empresa_id):
    """{(canal_id, cuenta_override_id): (facturado, pedidos, comision, sin comision)}.

    `facturado` es SUM(pedido.total - pedido.total_envio): la plata de la
    VENTA, sin el envio (FASE-CAJA-SOCIO-S5). El envio que paga el comprador
    entra a la cuenta y sale hacia el correo el mismo dia; contarlo como
    facturacion de un socio dice que le quedo algo de una plata que estaba de
    paso. `total_envio` es NOT NULL y vale 0.00 en la enorme mayoria de las
    filas -- toda venta de mostrador, donde no hay flete que cobrar --, asi
    que para esos pedidos la resta no cambia un peso.

    Lo que el envio SI deja a veces es una diferencia: cuando el correo cobra
    mas de lo que se le cobro al comprador, esa diferencia es un costo y vive
    en `costo_envio_vendedor`. No se toca aca -- restarla del facturado la
    contaria como "vendio menos" cuando en realidad "gasto mas".

    Un pedido cancelado no le entro a nadie. Se excluye con el mismo criterio
    que el resto de los reportes (`ESTADOS_NO_VENDIDOS`), asi los numeros de
    esta pantalla se pueden cruzar contra los de margen sin explicaciones.

    Un REGALO tampoco (FASE-CAJA-SOCIO-S5): no entro plata, y el pedido existe
    nada mas que para que el stock se descontara. Sale entero -- ni total, ni
    envio, ni comision --, y se cuenta aparte en `_regalos_por_canal` para que
    la pantalla lo pueda decir en vez de que el pedido desaparezca sin
    explicacion.

    El contador viaja al lado del monto y no en una consulta aparte: sin el, un
    canal en cero no se distingue de un canal que vendio y devolvio todo, y son
    dos situaciones muy distintas.

    Se suma en la base y no en Python a proposito: aca no hace falta abrir cada
    pedido -- no hay costos, ni items, ni snapshots -- y traerlos todos para
    sumar una columna serian miles de filas para llegar a dos numeros.

    FASE-CAJA-SOCIO-S2: la clave dejo de ser el canal solo. Un mismo canal
    puede repartir su facturacion entre dos cuentas cuando algun pedido suyo
    tiene una correccion cargada (`pedido.cuenta_cobro_override_id`), y el
    desglose de la pantalla tiene que poder mostrar las dos partes por
    separado. El override viaja CRUDO -- None cuando no hay correccion -- y es
    la funcion de arriba la que lo resuelve contra la cuenta del canal: la
    consulta no sabe a que cuenta cobra cada canal y no hace falta que lo sepa.

    FASE-CAJA-SOCIO-S4: la comision de plataforma sale de ESTA consulta y no
    de una aparte. El filtro que decide que pedido cuenta -- empresa y estado
    no cancelado -- es el mismo que el del monto facturado, y escribirlo dos
    veces es la forma de que un dia deje de serlo: la comision restaria sobre
    un conjunto de pedidos distinto del que se facturo y la resta daria
    cualquier cosa sin que nada avisara.

    Los pedidos SIN comision cargada viajan contados aparte y no sumados como
    cero. `sum()` ignora los NULL, asi que el monto ya sale bien; lo que se
    perderia sin ese contador es saber si un cero es "no hubo comision" o
    "todavia no la cargo nadie", que es la unica diferencia que importa cuando
    el numero de abajo dice cuanta plata te queda.
    """
    filas = (db.session.query(Pedido.canal_id,
                              Pedido.cuenta_cobro_override_id,
                              func.coalesce(
                                  func.sum(Pedido.total - Pedido.total_envio), 0),
                              func.count(Pedido.id),
                              func.coalesce(
                                  func.sum(Pedido.comision_plataforma), 0),
                              # count() de una columna no cuenta los NULL: la
                              # resta contra el total de la fila es cuantos
                              # pedidos no tienen la comision cargada.
                              func.count(Pedido.id)
                              - func.count(Pedido.comision_plataforma))
             .filter(Pedido.empresa_id == empresa_id)
             .filter(*_es_venta())
             .group_by(Pedido.canal_id, Pedido.cuenta_cobro_override_id)
             .all())
    return {(canal_id, override_id): (_decimal(total), int(cantidad or 0),
                                      _decimal(comision), int(sin_comision or 0))
            for canal_id, override_id, total, cantidad, comision, sin_comision
            in filas}


def _regalos_por_canal(empresa_id):
    """{(canal_id, cuenta_override_id): cuantos regalos} (FASE-CAJA-SOCIO-S5).

    Los pedidos que `_facturado_por_canal` deja afuera por ser regalos, con la
    misma clave, para poder decir en la pantalla que estan y que no suman. Un
    pedido que se cae de la cuenta sin dejar rastro obliga a que alguien
    descubra por que el total no cierra contra el listado de ventas.

    Solo el CONTADOR, no el monto. El monto de un regalo es un precio
    simbolico que se puso para poder cargarlo -- $1 la unidad -- y mostrarlo
    como plata invitaria a sumarlo o restarlo de algo. Lo que costo de verdad
    esa mercaderia es la compra, que ya se pago; desde FASE-RESULTADOS-S1 el
    resultado del negocio lo resta como marketing a partir del snapshot de
    costo del pedido (`_costo_de_regalos`), no de un gasto cargado a mano.
    """
    filas = (db.session.query(Pedido.canal_id,
                              Pedido.cuenta_cobro_override_id,
                              func.count(Pedido.id))
             .filter(Pedido.empresa_id == empresa_id)
             .filter(func.lower(Pedido.estado).notin_(ESTADOS_NO_VENDIDOS))
             .filter(Pedido.es_regalo.is_(True))
             .group_by(Pedido.canal_id, Pedido.cuenta_cobro_override_id)
             .all())
    return {(canal_id, override_id): int(cantidad or 0)
            for canal_id, override_id, cantidad in filas}


def _destinos_del_canal(canal, facturado, cuentas):
    """A que cuentas fue a parar lo que vendio este canal, y cuanto a cada una.

    Devuelve [(cuenta o None, total, pedidos, comision, sin_comision,
    corregido)], siempre con la cuenta del canal
    primero y aunque no haya vendido un peso: un canal que todavia no vendio
    nada tiene que seguir apareciendo debajo de su socio (hoy le pasa a Mercado
    Libre), y sacarlo de la pantalla haria ver un reparto de dos personas como
    si fuera de una sola.

    Las cuentas de mas -- si es que hay alguna -- son las correcciones puntuales
    de FASE-CAJA-SOCIO-S2. El canal no se pierde en el camino: la venta
    corregida aparece debajo del socio que de verdad la cobro, pero sigue
    diciendo por que canal entro. Son las dos cosas que hay que saber de esa
    plata, y ninguna reemplaza a la otra.

    Un override que apunta a la MISMA cuenta que ya le tocaba por canal no abre
    una fila nueva: no es una correccion, es lo mismo escrito dos veces.

    FASE-CAJA-SOCIO-S4: la comision viaja con el monto por el mismo camino y
    hasta el mismo destino. Una venta reasignada a mano se la cobro otro socio,
    y la mordida que la plataforma le hizo a esa venta tambien es de el: si la
    comision se quedara en el canal, el socio corregido se llevaria la
    facturacion entera sin el costo de haberla hecho.
    """
    cuenta_defecto = canal.cuenta_cobro
    id_defecto = cuenta_defecto.id if cuenta_defecto is not None else None

    vacio = (CERO, 0, CERO, 0)

    # Orden fijo y no el que devuelva la base: el destino por defecto primero,
    # las correcciones despues y por id de cuenta.
    acumulado = OrderedDict([(id_defecto, vacio)])
    for (canal_id, override_id), (total, pedidos, comision, sin_comision) in sorted(
            facturado.items(), key=lambda par: (par[0][1] or 0)):
        if canal_id != canal.id:
            continue
        destino_id = id_defecto if override_id is None else override_id
        antes = acumulado.get(destino_id, vacio)
        acumulado[destino_id] = (antes[0] + total,
                                 antes[1] + pedidos,
                                 antes[2] + comision,
                                 antes[3] + sin_comision)

    return [(cuentas.get(destino_id) if destino_id is not None else None,
             total, pedidos, comision, sin_comision, destino_id != id_defecto)
            for destino_id, (total, pedidos, comision, sin_comision)
            in acumulado.items()]


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

    FASE-GASTO-MIXTO-S1: LOS MIXTOS SE REPARTEN, NO SE IMPUTAN ENTEROS

    Un gasto con origen_fondo='mixto' salio de VARIAS cuentas a la vez, y a
    cada socio le tiene que bajar SU PARTE. El ejemplo real: $68.000 pagados
    53.000 de la cuenta de Roman y 15.000 de la de Nachi bajan 53.000 y 15.000
    respectivamente -- no 68.000 a uno, no 68.000 a los dos (que restaria
    $136.000 de una caja de la que salieron $68.000), no 34.000 a cada uno
    (que seria promediar un dato que se tiene exacto).

    Por eso son dos consultas y no una: los de 'facturacion' suman
    `gasto.monto` porque el gasto entero salio de esa cuenta, y los de 'mixto'
    suman `gasto_origen_mixto.monto`, que es la parte. Sumar las dos cosas en
    una sola consulta pediria un OUTER JOIN cuyo lado nulo volveria a imputar
    el total; separarlas mantiene cada regla legible por su lado.

    Una parte en CERO cuenta como gasto igual (suma $0 y +1): es un reparto
    que se hizo y que a esa cuenta le toco nada, distinto de un gasto que
    nunca la toco. Es la misma decision que el CHECK `monto >= 0` del modelo.
    """
    acumulado = {}

    def sumar(socio, monto, cantidad):
        antes = acumulado.get(socio, (CERO, 0))
        acumulado[socio] = (antes[0] + monto, antes[1] + cantidad)

    # 1. Los que salieron ENTEROS de una sola cuenta (como desde S3).
    for socio, (monto, cantidad) in _gasto_facturacion_por_socio(
            empresa_id).items():
        sumar(socio, monto, cantidad)

    # 2. Las PARTES de los que se pagaron entre varias cuentas.
    for socio, (monto, cantidad) in _gasto_mixto_por_socio(empresa_id).items():
        sumar(socio, monto, cantidad)

    # Una cuenta sin socio cae en la clave None, que es la misma fila donde ya
    # va la facturacion sin dueno. Los dos numeros de esa fila hablan de la
    # misma plata sin identificar, asi que se restan entre si igual que los de
    # cualquier socio.
    return acumulado


def _gasto_facturacion_por_socio(empresa_id, *condiciones):
    """{socio: (monto, cantidad)} de los gastos 'facturacion', ENTEROS.

    Las dos mitades de `_gastado_por_socio`, separadas en FASE-RESULTADOS-S1
    para que el resultado del negocio pueda mostrar cada origen en su propio
    renglon -- y con `condiciones` extra (naturaleza, periodo) -- sin
    escribir una tercera consulta que diga lo mismo. Sin condiciones dan
    exactamente lo que daban adentro de `_gastado_por_socio`.
    """
    return {socio: (Decimal(monto or 0), int(cantidad or 0))
            for socio, monto, cantidad in (
                db.session.query(CuentaCobro.socio,
                                 func.coalesce(func.sum(Gasto.monto), 0),
                                 func.count(Gasto.id))
                .join(CuentaCobro, Gasto.cuenta_pago_id == CuentaCobro.id)
                .filter(Gasto.empresa_id == empresa_id)
                .filter(Gasto.origen_fondo == ORIGEN_FACTURACION)
                .filter(*condiciones)
                .group_by(CuentaCobro.socio)
                .all())}


def _gasto_mixto_por_socio(empresa_id, *condiciones):
    """{socio: (monto, cantidad)} de las PARTES de los gastos 'mixto'.

    El filtro por `Gasto.origen_fondo == ORIGEN_MIXTO` no es redundante con
    la existencia de la fila: si un gasto dejo de ser mixto en una edicion,
    su reparto viejo no tiene que seguir restando. La ruta lo borra, y este
    filtro es la segunda red por si alguna vez no lo hiciera.
    """
    return {socio: (Decimal(monto or 0), int(cantidad or 0))
            for socio, monto, cantidad in (
                db.session.query(CuentaCobro.socio,
                                 func.coalesce(func.sum(GastoOrigenMixto.monto), 0),
                                 func.count(GastoOrigenMixto.id))
                .select_from(GastoOrigenMixto)
                .join(Gasto, GastoOrigenMixto.gasto_id == Gasto.id)
                .join(CuentaCobro,
                      GastoOrigenMixto.cuenta_cobro_id == CuentaCobro.id)
                .filter(Gasto.empresa_id == empresa_id)
                .filter(Gasto.origen_fondo == ORIGEN_MIXTO)
                .filter(*condiciones)
                .group_by(CuentaCobro.socio)
                .all())}


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
        # FASE-CAJA-SOCIO-S4: lo que la plataforma ya se quedo de esa
        # facturacion. Es plata que nunca llego a la cuenta, asi que resta.
        'comision': CERO,
        # Cuantos de sus pedidos no tienen la comision cargada. No es un cero:
        # es un dato que falta, y por eso se cuenta en vez de asumirse.
        'sin_comision': 0,
        # FASE-CAJA-GENERAL-S3: lo que ya salio de esa misma cuenta.
        'gastado': CERO,
        'gastos': 0,
        # facturado - comision - gastado_de_ahi. Es la plata que deberia quedar
        # en la cuenta si nadie la toco por fuera del sistema. Puede dar
        # negativo, y que se vea es el punto: significa que se pago mas de lo
        # que entro.
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

    A que socio va cada venta lo decide su cuenta EFECTIVA: la correccion
    puntual del pedido si la tiene, la del canal si no (FASE-CAJA-SOCIO-S2).
    El desglose sigue siendo por canal, asi que un pedido corregido cambia de
    socio sin dejar de decir por donde entro.

    LA CUENTA (FASE-CAJA-GENERAL-S3, ampliada en S4 y S5)

        facturado    = SUM(pedido.total - pedido.total_envio) de sus canales,
                       sin cancelados y sin regalos
        comision     = SUM(pedido.comision_plataforma) de esos mismos pedidos
        gastado      = SUM(gasto.monto) con origen_fondo='facturacion' y
                       cuenta_pago_id apuntando a la cuenta de ese socio
        saldo_real   = facturado - comision - gastado

    Hasta CAJA-GENERAL-S3 la pantalla mostraba solo el primero, y ese numero
    se leia como "lo que tiene Roman" cuando en realidad es "lo que Roman
    facturo desde siempre". Son dos cosas muy distintas en cuanto se paga el
    primer proveedor. Por eso van los dos juntos, uno debajo del otro: el
    historico no se saca -- sigue haciendo falta para cuadrar contra las
    ventas -- pero deja de ser el unico numero de la fila.

    POR QUE LA COMISION RESTA (FASE-CAJA-SOCIO-S4)

    La comision Tiendanube o Mercado Libre ya se la quedo antes de depositar:
    nunca fue plata del socio y no hay nada que hacer con ella. Restarla es la
    diferencia entre "cuanto vendiste" y "cuanto te queda", que es justamente
    lo que esta columna dice contestar.

    La resta va escrita como una linea propia en la pantalla: si solo bajara
    el numero final, la unica forma de entender por que seria hacer la cuenta
    a mano.

    POR QUE EL ENVIO NO SUMA (FASE-CAJA-SOCIO-S5)

    Hasta S4 `facturado` era `pedido.total` -- lo que pago el COMPRADOR, con
    el envio adentro -- y se justificaba diciendo que esa plata entra y
    despues hay que pagarla como cualquier otro gasto. Es cierto que entra;
    lo que no es cierto es que quede. El envio se cobra y se paga al correo,
    y en el medio no se queda nada de nadie: mostrarlo como facturacion de un
    socio infla su numero con plata que estaba de paso.

    Por eso la formula pasa a `total - total_envio`. `total_envio` es NOT NULL
    y vale 0.00 en toda venta de mostrador, asi que el caso comun no se mueve
    un peso: el unico pedido que cambia es el que cobro flete.

    Lo que sigue SIN restarse es `costo_envio_vendedor`, que es lo que el
    correo le cobro al vendedor. Cuando es mas que lo que pago el comprador
    -- pasa -- esa diferencia es un GASTO, no menos facturacion, y meterla
    aca la haria ver como si se hubiera vendido menos.

    LOS REGALOS NO SON VENTAS (FASE-CAJA-SOCIO-S5)

    Un pedido marcado `es_regalo` sale entero de la suma: ni total, ni envio,
    ni comision. El caso que lo trajo es el "Sorteo" -- mercaderia a un
    influencer, cargada como pedido de $1 la unidad para que el stock se
    descontara. El pedido tiene que existir porque la mercaderia se fue de
    verdad; lo que no existio es el ingreso.

    Que el pedido no sume no lo hace gratis: la mercaderia se pago al
    comprarla, y el resultado del negocio (/reportes/margen) resta su costo
    como marketing. Y el stock que descontó tampoco se revierte -- son tres
    cosas distintas sobre el mismo pedido y esta pantalla solo opina sobre
    una.

    El contador de regalos se muestra igual, aunque no sume: un pedido que
    desaparece de la cuenta sin decir nada obliga a que alguien descubra por
    que el total no cierra contra el listado de ventas.

    UN PEDIDO SIN COMISION CARGADA NO RESTA CERO

    Es el mismo criterio NULL != 0 de todo el modulo, aplicado al mismo lugar
    donde ya se ven los gastos sin origen: el pedido se cuenta aparte y el
    faltante se muestra. Un cero silencioso diria "esta venta no pago
    comision", que es una afirmacion, cuando el dato real es "todavia nadie
    la cargo". La diferencia se nota en el saldo: mientras haya pedidos sin
    comision, el "tenes realmente" esta por ARRIBA de lo que va a terminar
    siendo, y la pantalla lo dice en vez de dejar que se descubra despues.

    ESTO NO ES EL REPORTE DE MARGEN

    El de margen tambien resta comision, y no comparten una linea de codigo a
    proposito: aquel contesta "cuanto gane con este producto" y para eso
    necesita ademas el costo de la mercaderia y el flete, y descarta el pedido
    entero si le falta uno de los tres. Este contesta "cuanta plata hay en la
    cuenta", donde el costo de la mercaderia no viene al caso -- ya se pago, y
    si se pago desde esta cuenta ya esta contado como gasto.

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
    regalos = _regalos_por_canal(empresa_id)
    gastado = _gastado_por_socio(empresa_id)

    # Se parte de los CANALES, no de los pedidos: un canal que todavia no
    # vendio nada tiene que aparecer igual (Mercado Libre hoy), y si el barrido
    # empezara por los pedidos ese canal no existiria en la pantalla.
    canales = (CanalVenta.query
               .options(joinedload(CanalVenta.cuenta_cobro))
               .filter(CanalVenta.empresa_id == empresa_id)
               .order_by(CanalVenta.id)
               .all())

    # Todas las cuentas de la empresa, para poder resolver a que socio apunta
    # un override sin abrir una consulta por pedido corregido. El filtro por
    # empresa es el mismo de siempre: un override que apuntara a una cuenta
    # ajena no se encuentra aca y cae en "sin socio" en vez de mostrar el
    # nombre de una cuenta de otra empresa.
    cuentas = {cuenta.id: cuenta for cuenta in
               CuentaCobro.query.filter_by(empresa_id=empresa_id).all()}

    # Los socios del vocabulario nacen todos, en cero y en su orden, antes de
    # mirar una sola venta. Asi la pantalla no depende de que canales existan.
    socios = OrderedDict((clave, _nuevo_socio(clave, nombre))
                         for clave, nombre in SOCIOS.items())

    for canal in canales:
        for (cuenta, total, pedidos, comision, sin_comision,
             corregido) in _destinos_del_canal(canal, facturado, cuentas):
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

            grupo['total'] += total
            grupo['pedidos'] += pedidos
            grupo['comision'] += comision
            grupo['sin_comision'] += sin_comision
            grupo['canales'].append({
                'nombre': canal.nombre,
                'tipo': canal.tipo,
                'activo': canal.activo,
                'cuenta': cuenta.nombre if cuenta is not None else None,
                'total': total,
                'pedidos': pedidos,
                'comision': comision,
                'sin_comision': sin_comision,
                # FASE-CAJA-SOCIO-S2: esta linea no esta aca por la regla del
                # canal sino porque alguien corrigio a mano esos pedidos. Se
                # marca en la pantalla: una plata que aparece debajo de un
                # socio que no es el que le tocaba tiene que decir por que.
                'corregido': corregido,
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
        grupo['saldo_real'] = (grupo['total'] - grupo['comision']
                               - grupo['gastado'])

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
                           comision_total=sum((fila['comision']
                                               for fila in filas), CERO),
                           sin_comision_total=sum(fila['sin_comision']
                                                  for fila in filas),
                           gastado_total=sum((fila['gastado'] for fila in filas),
                                             CERO),
                           saldo_real_total=sum((fila['saldo_real']
                                                 for fila in filas), CERO),
                           regalos_totales=sum(regalos.values()),
                           capital_monto=capital_monto,
                           capital_cantidad=capital_cantidad,
                           sin_clasificar_monto=sin_clasificar_monto,
                           sin_clasificar_cantidad=sin_clasificar_cantidad)


# ==========================================================================
# FASE-RESUMEN-GENERAL-S1 -- todo junto, un solo numero
#
# Las dos pantallas de arriba estan separadas a proposito y ninguna de las dos
# se toca aca. /caja-general es el libro de movimientos cargados a mano -- el
# Excel de Roman -- y no mira un solo pedido. /reportes/caja-socio es al reves:
# mira los pedidos y no mira los ingresos cargados. Cada una contesta bien su
# pregunta y las dos juntas no contestan la que falta: "cuanta plata tengo en
# total, sumando todo".
#
# Esta pantalla la contesta LEYENDO las dos, sin mover ni duplicar una fila.
#
#     posicion_real = (aporte de capital
#                      + facturado neto de ventas
#                      - comision de plataforma)
#                     - gastos totales
#
# POR QUE NO SE DUPLICA NADA
#
# Porque las ventas NO estan en Ingreso. Estuvieron: hasta
# FASE-AUDITORIA-EXCEL-S3 se cargaban a mano ahi, y sumar los pedidos ademas
# de eso habria contado la misma plata dos veces. Desde entonces Ingreso tiene
# solo el aporte de los socios, y la venta vive unicamente en Pedido -- que es
# lo que hace que esta suma sea legitima. Si algun dia vuelve a cargarse una
# venta como Ingreso, este numero se infla y no hay nada aca que lo delate:
# `test_no_duplica_nada` es el que se para en esa puerta.
#
# LA OTRA MITAD DE LO MISMO: el facturado sale de `_facturado_por_canal`, la
# MISMA funcion que usa caja-socio, no de una consulta nueva escrita igual.
# Dos consultas gemelas es la forma de que un dia dejen de serlo y las dos
# pantallas digan numeros distintos sin que nada avise.
#
# EL GASTO VA ENTERO, SIN FILTRAR POR ORIGEN
#
# Es la unica diferencia de criterio con caja-socio, y es a proposito. Alla el
# origen decide a QUIEN se le resta, y por eso los de capital y los sin
# clasificar quedan afuera de la cuenta de cada socio -- restarselos a uno
# seria adivinar de que bolsillo salieron. Aca no hay a quien adivinarle: la
# pregunta es cuanto salio en total, y de que pozo salio no cambia la
# respuesta. Un gasto sin origen cargado entra igual, porque la plata se fue
# lo mismo.
#
# LO QUE ESTA PANTALLA NO ES
#
# No es plata conciliada. Nadie miro un extracto: es lo que deberia haber si
# todo lo cargado es cierto y nada se movio por fuera del sistema.
# ==========================================================================


class FacturadoNeto(namedtuple('FacturadoNeto',
                               'neto bruto comision pedidos sin_comision')):
    """Lo que dejaron las ventas de una empresa, en una sola pieza.

        bruto        SUM(pedido.total - pedido.total_envio), sin cancelados
                     y sin regalos
        comision     SUM(pedido.comision_plataforma) de esos mismos pedidos
        neto         bruto - comision
        pedidos      cuantos entraron a la suma
        sin_comision cuantos de esos no tienen la comision cargada

    `neto` y `bruto` viajan JUNTOS y no se elige uno al escribir la funcion,
    porque las dos pantallas que la usan necesitan cosas distintas de la misma
    cuenta: resumen-general muestra la resta escrita en dos lineas -- si el
    bruto ya viniera neto, la unica forma de saber cuanto se quedo la
    plataforma seria mirar otro reporte -- y el dashboard solo quiere el
    numero final. Devolver una sola de las dos obligaria a la otra a rehacer
    la resta por su cuenta, que es exactamente como se separan dos numeros que
    tenian que ser el mismo.
    """


def facturado_neto(empresa_id):
    """Cuanta plata dejaron las ventas. La UNICA definicion de eso.

    La usan /reportes/resumen-general y el dashboard (FASE-EVA-S4), y sale de
    `_facturado_por_canal` -- la misma consulta que reparte la facturacion por
    socio en /reportes/caja-socio. Los tres numeros no pueden separarse sin
    que alguien lo haga a proposito.

    Que quede afuera lo dice esa consulta y no esta funcion: los cancelados
    (no le entraron a nadie), los regalos (no entro plata) y el envio (se
    cobra y se le paga al correo el mismo dia). El detalle de por que, en su
    docstring.

    LOS PEDIDOS SIN COMISION CARGADA no se restan como cero: `sum()` ignora
    los NULL, asi que `neto` ya sale bien, y el contador viaja al lado para
    que quien muestre el numero pueda decir en que direccion esta incompleto.
    Mientras haya alguno, `neto` esta por ARRIBA del que va a terminar siendo.

    Devuelve Decimals. Quien trabaje en float -- el dashboard, porque
    `capital_invertido` y las tasas son Float -- convierte en su borde, que es
    donde ya lo hacia con los gastos.
    """
    facturado = _facturado_por_canal(empresa_id)
    bruto = sum((fila[0] for fila in facturado.values()), CERO)
    pedidos = sum(fila[1] for fila in facturado.values())
    comision = sum((fila[2] for fila in facturado.values()), CERO)
    sin_comision = sum(fila[3] for fila in facturado.values())
    return FacturadoNeto(neto=bruto - comision, bruto=bruto, comision=comision,
                         pedidos=pedidos, sin_comision=sin_comision)


def ventas_recientes(empresa_id, limite):
    """Las ultimas `limite` ventas, como filas para mostrar. No suma nada.

    La usa el bloque "Ultimos movimientos" del dashboard
    (FASE-DASHBOARD-MOVIMIENTOS-S1), que hasta esa slice mostraba Gasto e
    Ingreso y nada mas. Desde que las ventas salieron de Ingreso
    (FASE-AUDITORIA-EXCEL-S3) y viven en Pedido, una venta de ayer no aparecia
    en ningun lado de esa lista: el bloque decia "ultimos movimientos" y
    mostraba solo la mitad que no vende.

    QUE PEDIDO ENTRA lo dice `_es_venta()`, el mismo predicado que usa
    `_facturado_por_canal` para el Facturado de caja-socio y del
    resumen-general. No es un criterio parecido: es el mismo objeto de
    filtros, y por eso la lista no puede mostrar un pedido que el total de
    arriba no cuenta.

    CUANTO VALE CADA FILA es `total - total_envio`, tambien lo mismo que
    Facturado: el envio que paga el comprador entra y sale hacia el correo el
    mismo dia, y contarlo dice que la venta dejo una plata que estaba de paso.

    LA COMISION NO SE RESTA ACA, y esa es la unica diferencia con el `neto` de
    `facturado_neto`. Es a proposito: la comision es un dato que Roman carga a
    mano despues, y mientras falte vale NULL. Restarla por fila obligaria a
    elegir entre mostrar la venta con un guion o mentir tratando el NULL como
    cero, cuando la pregunta de este bloque es "que se movio y por cuanto" y
    no "cuanto quedo". Es la misma columna que muestra caja-socio bajo
    Facturado, que tampoco la resta.

    Trae los pedidos con su canal ya cargado: la fila lo nombra, y sin el
    joinedload serian `limite` consultas mas para escribir una palabra.
    """
    return (Pedido.query
            .options(joinedload(Pedido.canal))
            .filter(Pedido.empresa_id == empresa_id)
            .filter(*_es_venta())
            .order_by(Pedido.fecha_pedido.desc())
            .limit(limite)
            .all())


def _total_ingresos(empresa_id):
    """(suma, cantidad) de TODO lo cargado como Ingreso.

    Hoy eso es el aporte de capital de los socios y nada mas: las ventas
    salieron de esta tabla en FASE-AUDITORIA-EXCEL-S3 y viven en Pedido. Por
    eso no hace falta filtrar por categoria -- y filtrarla seria peor, porque
    ataria el numero al nombre exacto de una categoria que alguien puede
    renombrar desde la pantalla de Categorias.

    Que no se filtre significa que si mañana se carga un ingreso que NO es
    aporte, entra aca. Es correcto para "cuanta plata entro"; deja de serlo si
    ese ingreso es una venta que ya esta como pedido, que es exactamente el
    caso que `test_no_duplica_nada` vigila.
    """
    monto, cantidad = (db.session.query(func.coalesce(func.sum(Ingreso.monto), 0),
                                        func.count(Ingreso.id))
                       .filter(Ingreso.empresa_id == empresa_id)
                       .one())
    return Decimal(monto or 0), int(cantidad or 0)


@reportes_bp.route('/resumen-general')
@login_required
def resumen_general():
    """La posicion real de la empresa: los dos reportes sumados en un numero.

    Un solo bloque con las cuatro lineas de la cuenta, en el orden en que se
    hace, y el resultado abajo. El desglose se muestra entero y no solo el
    total: un numero suelto que dice "tenes $X" no se puede verificar contra
    nada, y la primera vez que no coincida con lo que Roman tiene en la mano
    no va a haber por donde empezar a mirar.

    NO TOCA NINGUNA DE LAS DOS PANTALLAS QUE SUMA. Solo lee. Cada una sigue
    mostrando exactamente lo que mostraba, y desde aca se linkean las dos:
    esta pantalla da el total, el detalle por socio o por movimiento sigue
    estando donde estaba.

    LOS PEDIDOS SIN COMISION CARGADA SE MARCAN, NO SE ASUMEN EN CERO

    Mismo criterio NULL != 0 de todo el modulo. `sum()` ignora los NULL, asi
    que la comision que se resta ya sale bien; lo que se perderia sin el
    contador es saber en que direccion esta mal el resultado. Mientras haya
    pedidos sin comision, la posicion real que dice esta pantalla esta por
    ARRIBA de la que va a terminar siendo, y decirlo es la diferencia entre un
    numero incompleto y un numero mentiroso.

    LOS REGALOS NO ENTRAN, y por eso se cuentan aparte: vienen ya excluidos de
    `_facturado_por_canal`, que es el mismo lugar donde los excluye caja-socio.
    """
    empresa_id = current_user.empresa_id

    # Las ventas salen de la MISMA funcion que caja-socio y que el dashboard.
    # No es una consulta nueva que da lo mismo: es la misma, y por eso los
    # numeros no se pueden separar sin que alguien lo haga a proposito.
    ventas = facturado_neto(empresa_id)

    regalos = sum(_regalos_por_canal(empresa_id).values())

    capital_monto, capital_cantidad = _total_ingresos(empresa_id)

    # TODOS los gastos, sin mirar origen_fondo. `true()` y no un filtro
    # omitido para no abrirle a `_total_gastos` una segunda forma de llamarse.
    gastos_monto, gastos_cantidad = _total_gastos(empresa_id, true())

    entra = capital_monto + ventas.neto
    posicion_real = entra - gastos_monto

    return render_template('reportes_resumen_general.html',
                           capital_monto=capital_monto,
                           capital_cantidad=capital_cantidad,
                           ventas_monto=ventas.bruto,
                           ventas_pedidos=ventas.pedidos,
                           comision_monto=ventas.comision,
                           sin_comision=ventas.sin_comision,
                           regalos=regalos,
                           gastos_monto=gastos_monto,
                           gastos_cantidad=gastos_cantidad,
                           entra=entra,
                           posicion_real=posicion_real)


# ==========================================================================
# FASE-RESULTADOS-S1 -- el resultado del negocio y lo que el negocio tiene
#
# Las pantallas de caja contestan "cuanta plata hay"; /reportes/margen
# contestaba "cuanto deja cada venta". Faltaba la cuenta que junta las dos:
# cuanto GANO el negocio, restando ademas lo que cuesta funcionar.
#
#     Ventas netas (sin envio, sin regalos, sin cancelados)
#   - Comisiones de plataforma
#   - Costo de lo vendido (CMV, el snapshot de cada linea)
#   +/- Envios (cobrado - pagado por el vendedor)
#   = Utilidad bruta                         <- suma de la ganancia de margen
#   - Gastos operativos (naturaleza 'gasto', CUALQUIER origen, sin pruebas)
#   - Costo de lo regalado (marketing)
#   = Utilidad neta antes de pruebas         <- FASE-RESULTADOS-S2
#   - Muestras y productos de prueba (categoria.es_prueba)
#   = Utilidad neta
#
# ACTIVO NO ES GASTO
#
# Una compra de mercaderia no baja la ganancia el dia que se paga: se
# convierte en stock, y baja la ganancia recien cuando se vende, via el CMV.
# Restarla tambien como gasto contaria la misma mercaderia dos veces. Eso es
# lo que decide `categoria.naturaleza`, y es lo unico que decide: de que
# bolsillo salio la plata (`origen_fondo`) no cambia si resta. Un gasto de
# publicidad pagado con capital resta igual que uno pagado con facturacion;
# el origen se muestra en subtotales, pero el total lo resta entero.
#
# NADA SE CALCULA DOS VECES
#
# La utilidad bruta sale de `_calcular` sobre los MISMOS pedidos que dibujan
# el resto de /reportes/margen; los gastos, de `_total_gastos` y de las dos
# mitades de `_gastado_por_socio`, las mismas consultas que usan caja-socio y
# resumen-general, con una condicion mas.
#
# LO QUE NO ES
#
# Es antes de impuestos a las ganancias, no descuenta devoluciones (opcion C:
# hoy no se restan de lo vendido ni del margen) y no es un balance.
# ==========================================================================

MESES = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
         'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')


class Periodo(namedtuple('Periodo', 'clave desde hasta etiqueta')):
    """Un mes calendario: [desde, hasta), con `hasta` el primero del siguiente."""


def _mes(anio, mes):
    hasta = date(anio + 1, 1, 1) if mes == 12 else date(anio, mes + 1, 1)
    return Periodo(clave='%04d-%02d' % (anio, mes), desde=date(anio, mes, 1),
                   hasta=hasta, etiqueta='%s %d' % (MESES[mes - 1], anio))


def _periodo_de(texto):
    """'AAAA-MM' -> Periodo; cualquier otra cosa -> None, que es "todo".

    Un parametro roto no es un error que mostrar: se cae al acumulado, que es
    lo que la pantalla mostraba antes de que existiera el filtro.
    """
    if not texto:
        return None
    try:
        anio, mes = (int(parte) for parte in texto.split('-'))
        return _mes(anio, mes)
    except (ValueError, TypeError):
        return None


def _periodos_disponibles(empresa_id):
    """Los meses con algo que mirar, del mas nuevo al mas viejo.

    Desde el primer pedido o el primer gasto -- lo que haya empezado antes --
    hasta hoy. Un mes sin movimientos en el medio aparece igual: decir "en
    agosto no paso nada" es un dato.
    """
    primer_pedido = (db.session.query(func.min(Pedido.fecha_pedido))
                     .filter(Pedido.empresa_id == empresa_id).scalar())
    primer_gasto = (db.session.query(func.min(Gasto.fecha))
                    .filter(Gasto.empresa_id == empresa_id).scalar())
    inicios = [fecha.date() if isinstance(fecha, datetime) else fecha
               for fecha in (primer_pedido, primer_gasto) if fecha]
    if not inicios:
        return []
    inicio = min(inicios)
    hoy = date.today()
    anio, mes = hoy.year, hoy.month
    periodos = []
    while (anio, mes) >= (inicio.year, inicio.month):
        periodos.append(_mes(anio, mes))
        anio, mes = (anio - 1, 12) if mes == 1 else (anio, mes - 1)
    return periodos


def _pedido_en_periodo(periodo):
    if periodo is None:
        return true()
    return and_(
        Pedido.fecha_pedido >= datetime.combine(periodo.desde,
                                                datetime.min.time()),
        Pedido.fecha_pedido < datetime.combine(periodo.hasta,
                                               datetime.min.time()))


def _pedidos_vendidos(empresa_id, periodo=None):
    """Los pedidos que SON ventas (`_es_venta`), con canal e items cargados.

    La consulta de /reportes/margen, y por lo tanto la del resultado: los dos
    tienen que mirar exactamente los mismos pedidos.
    """
    return (Pedido.query
            .options(joinedload(Pedido.canal),
                     joinedload(Pedido.items))
            .filter(Pedido.empresa_id == empresa_id)
            .filter(*_es_venta())
            .filter(_pedido_en_periodo(periodo))
            .order_by(Pedido.fecha_pedido.desc(), Pedido.id.desc())
            .all())


def _gasto_de_naturaleza(naturaleza):
    """Condicion sobre Gasto: su categoria tiene esta naturaleza."""
    return Gasto.categoria_id.in_(
        select(Categoria.id).where(Categoria.naturaleza == naturaleza))


def _gasto_sin_naturaleza():
    """El gasto no tiene categoria, o la tiene sin clasificar. NULL no es
    'gasto': no se resta ni se ignora, se muestra aparte."""
    return or_(Gasto.categoria_id.is_(None),
               Gasto.categoria_id.in_(
                   select(Categoria.id).where(Categoria.naturaleza.is_(None))))


def _gasto_en_periodo(periodo):
    if periodo is None:
        return true()
    return and_(Gasto.fecha >= periodo.desde, Gasto.fecha < periodo.hasta)


def gastos_por_naturaleza(empresa_id, periodo=None):
    """{'activo'|'gasto'|'sin_clasificar': (monto, cantidad)}.

    La usan el resultado y el dashboard, que parte su tarjeta de gastos en lo
    que se consumio y lo que se convirtio en stock.
    """
    en_periodo = _gasto_en_periodo(periodo)
    return {
        NATURALEZA_ACTIVO: _total_gastos(
            empresa_id, and_(_gasto_de_naturaleza(NATURALEZA_ACTIVO),
                             en_periodo)),
        NATURALEZA_GASTO: _total_gastos(
            empresa_id, and_(_gasto_de_naturaleza(NATURALEZA_GASTO),
                             en_periodo)),
        'sin_clasificar': _total_gastos(
            empresa_id, and_(_gasto_sin_naturaleza(), en_periodo)),
    }


def _por_socio_ordenado(por_socio):
    """{socio: (monto, n)} -> filas en el orden de SOCIOS, sin dueño al final."""
    claves = [clave for clave in SOCIOS if clave in por_socio]
    claves += [clave for clave in por_socio if clave not in SOCIOS]
    return [{'nombre': SOCIOS.get(clave, ETIQUETA_SIN_SOCIO),
             'monto': por_socio[clave][0], 'cantidad': por_socio[clave][1]}
            for clave in claves]


def _gasto_de_prueba(es_prueba):
    """Condicion sobre Gasto: su categoria es (o no es) de muestras.

    FASE-RESULTADOS-S2. Va por `categoria.es_prueba` y no por el nombre:
    renombrar "Productos de prueba" no puede pasar su gasto a operativos.
    """
    return Gasto.categoria_id.in_(
        select(Categoria.id).where(Categoria.es_prueba.is_(es_prueba)))


def _gastos_operativos(empresa_id, periodo=None, pruebas=False):
    """Los gastos de naturaleza 'gasto', con sus subtotales por origen.

    El total es UNO y resta entero. Los subtotales dicen de donde salio:

        facturacion  abierto por la cuenta (socio) que lo pago
        capital      del pool de los socios
        mixto        abierto por la PARTE que puso cada cuenta
        sin origen   no se dijo de que bolsillo, pero se gasto igual

    FASE-RESULTADOS-S2: `pruebas` parte el conjunto en dos que no se pisan
    -- los recurrentes (False) y las muestras (True) -- con la misma cuenta
    para los dos. Sumados dan exactamente lo que daba antes la funcion sola.
    """
    condicion = and_(_gasto_de_naturaleza(NATURALEZA_GASTO),
                     _gasto_de_prueba(pruebas),
                     _gasto_en_periodo(periodo))

    total, cantidad = _total_gastos(empresa_id, condicion)
    facturacion = _por_socio_ordenado(
        _gasto_facturacion_por_socio(empresa_id, condicion))
    mixto = _por_socio_ordenado(_gasto_mixto_por_socio(empresa_id, condicion))

    return {
        'total': total,
        'cantidad': cantidad,
        'facturacion': facturacion,
        'facturacion_total': _total_gastos(
            empresa_id, and_(condicion,
                             Gasto.origen_fondo == ORIGEN_FACTURACION))[0],
        'capital': _total_gastos(
            empresa_id, and_(condicion, Gasto.origen_fondo == ORIGEN_CAPITAL)),
        'mixto': mixto,
        'mixto_total': _total_gastos(
            empresa_id, and_(condicion, Gasto.origen_fondo == ORIGEN_MIXTO))[0],
        'sin_origen': _total_gastos(
            empresa_id, and_(condicion, Gasto.origen_fondo.is_(None))),
    }


def _costo_de_regalos(empresa_id, periodo=None):
    """Lo que costo la mercaderia regalada: snapshot x cantidad.

    Es la opcion B del Sorteo: el costo sale del PEDIDO marcado `es_regalo`,
    no de un gasto tipeado a mano -- que repetia una mercaderia ya pagada en
    la compra. Mismos pedidos que cuenta `_regalos_por_canal` (no cancelados).

    Un regalo con una linea sin costo cargado no suma cero: queda listado.
    """
    pedidos = (Pedido.query
               .options(joinedload(Pedido.items))
               .filter(Pedido.empresa_id == empresa_id)
               .filter(func.lower(Pedido.estado).notin_(ESTADOS_NO_VENDIDOS))
               .filter(Pedido.es_regalo.is_(True))
               .filter(_pedido_en_periodo(periodo))
               .all())

    costo = CERO
    completos = 0
    unidades = 0
    sin_costo = []
    for pedido in pedidos:
        if any(item.costo_unitario_snapshot is None for item in pedido.items):
            sin_costo.append(_etiqueta_pedido(pedido))
            continue
        completos += 1
        unidades += sum(item.cantidad for item in pedido.items)
        costo += sum((item.costo_unitario_snapshot * item.cantidad
                      for item in pedido.items), CERO)
    return {'costo': costo, 'pedidos': completos, 'unidades': unidades,
            'sin_costo': sin_costo}


def resultado_del_negocio(empresa_id, periodo=None, pedidos=None):
    """El estado de resultados de la empresa, en un dict listo para mostrar.

    `pedidos` lo pasa /reportes/margen para que el resultado se arme sobre la
    MISMA lista que dibuja sus cortes; sin el, se piden con el mismo criterio.

    Todo o nada por pedido, como en margen: un pedido al que le falta un
    costo no entra a ninguna linea y queda listado en `incompletos`.

    Los porcentajes son sobre las ventas netas, y sin ventas son None -- un
    guion en pantalla, no una division por cero.

    Los gastos SIN CLASIFICAR (categoria sin naturaleza) no se restan: no se
    sabe si son stock o consumo. Viajan en `sin_clasificar` para que la
    pantalla diga que la utilidad neta no los incluye.
    """
    if pedidos is None:
        pedidos = _pedidos_vendidos(empresa_id, periodo)

    ventas_netas = comisiones = cmv = CERO
    envio_cobrado = envio_pagado = utilidad_bruta = CERO
    completos = 0
    incompletos = []

    for pedido in pedidos:
        if _faltantes(pedido):
            incompletos.append({
                'etiqueta': _etiqueta_pedido(pedido),
                'canal': pedido.canal.nombre if pedido.canal else '—',
            })
            continue
        calculo = _calcular(pedido)
        completos += 1
        ventas_netas += calculo['ingreso_neto'] - calculo['envio']
        comisiones += calculo['comision']
        cmv += calculo['costo_mercaderia']
        envio_cobrado += calculo['envio']
        envio_pagado += calculo['envio_vendedor']
        # La ganancia del pedido tal cual la calcula margen. Es igual a
        # ventas - comisiones - cmv + (cobrado - pagado) por algebra, pero se
        # suma la de margen para que las dos pantallas no puedan separarse.
        utilidad_bruta += calculo['ganancia']

    regalos = _costo_de_regalos(empresa_id, periodo)
    gastos = _gastos_operativos(empresa_id, periodo)
    # FASE-RESULTADOS-S2: las muestras restan igual, pero en su propia linea,
    # despues de la utilidad del negocio recurrente.
    pruebas = _gastos_operativos(empresa_id, periodo, pruebas=True)
    sin_clasificar = _total_gastos(
        empresa_id, and_(_gasto_sin_naturaleza(), _gasto_en_periodo(periodo)))

    gastos_operativos_total = gastos['total'] + regalos['costo']
    utilidad_antes_de_pruebas = utilidad_bruta - gastos_operativos_total
    utilidad_neta = utilidad_antes_de_pruebas - pruebas['total']

    return {
        'periodo': periodo,
        'ventas_netas': ventas_netas,
        'comisiones': comisiones,
        'cmv': cmv,
        'envio_cobrado': envio_cobrado,
        'envio_pagado': envio_pagado,
        'envio_neto': envio_cobrado - envio_pagado,
        'utilidad_bruta': utilidad_bruta,
        'utilidad_bruta_pct': _porcentaje(utilidad_bruta, ventas_netas),
        'pedidos': completos,
        'incompletos': incompletos,
        'regalos': regalos,
        'gastos': gastos,
        'gastos_operativos_total': gastos_operativos_total,
        'gastos_operativos_pct': _porcentaje(gastos_operativos_total,
                                             ventas_netas),
        'utilidad_antes_de_pruebas': utilidad_antes_de_pruebas,
        'utilidad_antes_de_pruebas_pct': _porcentaje(utilidad_antes_de_pruebas,
                                                     ventas_netas),
        'pruebas': pruebas,
        'pruebas_pct': _porcentaje(pruebas['total'], ventas_netas),
        'sin_clasificar': sin_clasificar,
        'utilidad_neta': utilidad_neta,
        'utilidad_neta_pct': _porcentaje(utilidad_neta, ventas_netas),
    }


def activos_del_negocio(empresa_id, resultado_historico):
    """El stock valuado a costo y la conciliacion contra lo comprado.

    STOCK VALUADO = stock x costo_unitario, solo donde existen LOS DOS. Un
    producto sin costo, o sin control de stock (stock NULL, como el combo, que
    se arma con las piezas que ya cuentan los tarjeteros), va listado aparte
    y nunca como cero: valuarlo en 0 diria que no vale nada.

    CONCILIACION. Toda compra de naturaleza activo tiene que terminar en uno
    de tres lugares: se vendio (CMV), se regalo, o sigue en el deposito.

        diferencia = compras - (CMV + regalos + stock valuado)

    Lo que la explica cuando no da cero -- costos estimados o redondeados,
    stock mal cargado, productos sin costo -- se lee en el detalle.
    `resultado_historico` es el resultado SIN periodo: se compara contra todo
    lo comprado desde siempre.
    """
    valuados = []
    sin_costo = []
    sin_control = []
    stock_valuado = CERO
    for producto in (Producto.query.filter_by(empresa_id=empresa_id)
                     .order_by(Producto.nombre).all()):
        if producto.stock is None:
            sin_control.append({'nombre': producto.nombre,
                                'costo': producto.costo_unitario})
        elif producto.costo_unitario is None:
            sin_costo.append({'nombre': producto.nombre,
                              'stock': producto.stock})
        else:
            valor = producto.costo_unitario * producto.stock
            stock_valuado += valor
            valuados.append({'nombre': producto.nombre,
                             'stock': producto.stock,
                             'costo': producto.costo_unitario,
                             'valor': valor})

    compras, compras_cantidad = _total_gastos(
        empresa_id, _gasto_de_naturaleza(NATURALEZA_ACTIVO))

    cmv = resultado_historico['cmv']
    regalos = resultado_historico['regalos']['costo']
    explicado = cmv + regalos + stock_valuado

    return {
        'stock_valuado': stock_valuado,
        'productos': valuados,
        'sin_costo': sin_costo,
        'sin_control': sin_control,
        'compras': compras,
        'compras_cantidad': compras_cantidad,
        'cmv': cmv,
        'regalos': regalos,
        'explicado': explicado,
        'diferencia': compras - explicado,
        'pedidos_incompletos': len(resultado_historico['incompletos']),
        'regalos_sin_costo': len(resultado_historico['regalos']['sin_costo']),
    }
