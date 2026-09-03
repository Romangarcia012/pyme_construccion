# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S3-SNAPSHOT-FIX (el snapshot congela de verdad).

    python -m unittest discover -s tests -v

`pedido_item.costo_unitario_snapshot` existe para una sola cosa: que cambiar
`producto.costo_unitario` no reescriba el margen de las ventas ya hechas. No lo
cumplia. `_upsert_pedido` borra y reinserta TODAS las lineas del pedido en cada
corrida del sync -- pedido_item no tiene identidad propia del lado del canal, y
borrar y reescribir es lo unico que no duplica ni deja lineas fantasma -- y al
reinsertarlas volvia a leer el costo con `_costo_actual()`. O sea: el costo del
dia de la venta duraba hasta el proximo sync.

Con el cron cada 20 minutos, eso significa que el reporte de margen que viene
despues de esta slice iba a mostrar todos los pedidos historicos valuados al
costo de hoy.

El arreglo NO cambia el borrado y reinsercion: rescata los snapshots antes de
borrar (`_snapshots_previos`) y se los devuelve a la linea del mismo producto
(`_snapshot_de`). La venta manual (rutas_ventas._armar_venta) ya congelaba bien
y no se toca.

Lo que sostienen estos tests:

    costo cargado, sync, costo cambiado, resync  -> el snapshot NO se mueve
    linea nueva sin snapshot previo              -> se llena con el de hoy
    snapshot NULL y costo recien cargado         -> se completa (no hay nada
                                                    congelado que proteger)
    estado / total_envio / costo_envio_vendedor  -> SI se actualizan
    dos lineas del mismo producto                -> conserva los dos costos
    linea sin mapeo                              -> sigue en NULL
"""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cripto  # noqa: E402
import integracion_tiendanube as tn  # noqa: E402
import sync_tiendanube  # noqa: E402
from app import app  # noqa: E402
from ingestor_canal import ENTIDAD_PEDIDO, ENTIDAD_PRODUCTO  # noqa: E402
from models import (CanalVenta, CredencialCanal, Empresa, Pedido,  # noqa: E402
                    PedidoItem, Producto, SyncLog, Usuario, db)

TOKEN_FALSO = 'tn_token_de_prueba_no_es_real_1234567890'
STORE_ID_FALSO = '9876543'

ID_PEDIDO = 2058709648
ID_PRODUCTO = 360354459
ID_VARIANTE_NEGRO = 1574653133
ID_VARIANTE_GRIS = 1574653135

#: El costo real del Tarjetero Negro, el que Roman saca de KORVO.xlsx.
COSTO_DE_LA_VENTA = Decimal('3994.18')
#: El costo despues de que suba el proveedor. El margen de la venta vieja no
#: se tiene que enterar de este numero.
COSTO_POSTERIOR = Decimal('5100.00')


def setUpModule():
    """Repunta la app a SQLite en memoria. La base real no se toca."""
    global ENGINE_PRODUCTIVO
    engines = db._app_engines[app]
    ENGINE_PRODUCTIVO = engines[None]
    engines[None] = db._make_engine(None, {'url': 'sqlite://'}, app)
    app.config['TESTING'] = True
    with app.app_context():
        db.create_all()


def tearDownModule():
    with app.app_context():
        db.drop_all()
        db.session.remove()
    db._app_engines[app][None].dispose()
    db._app_engines[app][None] = ENGINE_PRODUCTIVO
    app.config['TESTING'] = False


# ---------------------------------------------------------------------------
# Payloads. El producto tiene dos variantes para que el sync arme los mapeos y
# las lineas del pedido resuelvan producto_id: sin mapeo el snapshot es NULL
# por definicion y no habria nada que testear.
# ---------------------------------------------------------------------------

def _producto():
    return {
        'id': ID_PRODUCTO,
        'name': 'Tarjetero Minimalista de Aluminio',
        'published': True,
        'variants': [
            {'id': ID_VARIANTE_NEGRO, 'price': '9900.00', 'stock': 198,
             'values': [{'es': 'Negro'}]},
            {'id': ID_VARIANTE_GRIS, 'price': '9900.00', 'stock': 100,
             'values': [{'es': 'Gris'}]},
        ],
    }


def _linea(variante, nombre, cantidad=1, precio='7490.00'):
    return {'product_id': ID_PRODUCTO, 'variant_id': variante, 'sku': None,
            'name': nombre, 'quantity': cantidad, 'price': precio}


def _pedido(estado='open', pago='paid', envio=7630, lineas=None):
    """El pedido de Camila. `envio` va a los dos costos del fulfillment."""
    return {
        'id': ID_PEDIDO,
        'number': '100',
        'created_at': '2026-08-31T17:55:12+0000',
        'status': estado,
        'payment_status': pago,
        'currency': 'ARS',
        'subtotal': '7490.00',
        'discount': '1423.10',
        'total': '13696.90',
        'customer': {'id': 337610261, 'name': 'Camila Valaco'},
        'products': lineas if lineas is not None else [
            _linea(ID_VARIANTE_NEGRO, 'Tarjetero Minimalista de Aluminio (Negro)')],
        'fulfillments': [{
            'id': '01M1CFC0HPCP82Z1DSGRAAFANV',
            'number': '1',
            'status': 'DISPATCHED',
            'shipping': {
                'type': 'pickup',
                'carrier': {'name': 'Envio Nube'},
                'option': {'name': 'Envio Nube - Andreani a sucursal'},
                'consumer_cost': {'value': envio, 'currency': 'ARS'},
                'merchant_cost': {'value': envio, 'currency': 'ARS'},
            },
        }],
    }


class RespuestaFalsa:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class ApiFalsa:
    """requests.get de mentira: productos, lista de pedidos y detalle.

    El detalle es el que importa desde FASE-REPORTES-S3-FIX2: es de donde
    salen consumer_cost y merchant_cost.
    """

    def __init__(self, pedidos, productos=None):
        self.pedidos = list(pedidos)
        self.productos = _producto() if productos is None else productos

    def __call__(self, url, headers=None, params=None, timeout=None):
        cabeceras = {'x-rate-limit-remaining': '35', 'x-rate-limit-reset': '1000'}

        if '/products' in url:
            return RespuestaFalsa(200, [self.productos], headers=cabeceras)

        cola = url.split('/orders/', 1)[1].strip('/') if '/orders/' in url else ''
        por_id = {str(p['id']): p for p in self.pedidos}
        if cola:
            if cola not in por_id:
                return RespuestaFalsa(404, {'message': 'Not found'})
            return RespuestaFalsa(200, por_id[cola], headers=cabeceras)

        if '/orders' in url:
            return RespuestaFalsa(200, self.pedidos, headers=cabeceras)

        return RespuestaFalsa(404, [])


class BaseSnapshot(unittest.TestCase):
    """Una empresa con Tiendanube conectada y nada mas."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        empresa = Empresa(nombre='Empresa Test SNAPSHOT')
        db.session.add(empresa)
        db.session.flush()

        usuario = Usuario(nombre='Roman Test', email='snapshot@test.local',
                          empresa_id=empresa.id, rol='admin', verificado=True)
        usuario.set_password('irrelevante')
        db.session.add(usuario)

        canal = CanalVenta(empresa_id=empresa.id, tipo='tiendanube',
                           nombre='Korvo', activo=True,
                           id_tienda_externo=STORE_ID_FALSO)
        db.session.add(canal)
        db.session.flush()
        db.session.add(CredencialCanal(
            canal_id=canal.id, tipo_credencial='oauth2', activo=True,
            access_token_cifrado=cripto.cifrar(TOKEN_FALSO)))
        db.session.commit()

        self.empresa_id = empresa.id
        self.canal_id = canal.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def correr(self, api):
        """Una corrida completa del sync con la API mockeada."""
        arranque = datetime.utcnow()
        for entidad in (ENTIDAD_PRODUCTO, ENTIDAD_PEDIDO):
            db.session.add(SyncLog(canal_id=self.canal_id, entidad=entidad,
                                   operacion=sync_tiendanube.OPERACION,
                                   estado='corriendo', fecha_inicio=arranque))
        db.session.commit()

        with mock.patch.object(tn.requests, 'get', side_effect=api):
            return sync_tiendanube.correr_backfill(self.canal_id, arranque)

    def el_pedido(self):
        return Pedido.query.filter_by(canal_id=self.canal_id,
                                      id_externo=str(ID_PEDIDO)).one()

    def negro(self):
        return Producto.query.filter_by(
            empresa_id=self.empresa_id,
            sku='TN-%s-%s' % (ID_PRODUCTO, ID_VARIANTE_NEGRO)).one()

    def gris(self):
        return Producto.query.filter_by(
            empresa_id=self.empresa_id,
            sku='TN-%s-%s' % (ID_PRODUCTO, ID_VARIANTE_GRIS)).one()

    def items(self):
        return (PedidoItem.query
                .filter_by(pedido_id=self.el_pedido().id)
                .order_by(PedidoItem.id)
                .all())

    def poner_costo(self, producto, costo):
        producto.costo_unitario = costo
        db.session.commit()


class TestElSnapshotNoSeReescribe(BaseSnapshot):
    """El corazon de la slice."""

    def test_snapshot_no_se_reescribe_en_resync(self):
        """Costo A al vender, costo B despues: la venta sigue valuada en A.

        Es el bug entero. Con el codigo viejo el segundo sync devolvia
        COSTO_POSTERIOR y el margen del pedido de agosto quedaba calculado con
        la lista de precios de hoy.
        """
        api = ApiFalsa([_pedido()])

        # El costo cargado y congelado por una corrida previa: eso es lo que
        # hay que proteger.
        self.correr(api)
        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)
        self.correr(api)
        self.assertEqual(self.items()[0].costo_unitario_snapshot, COSTO_DE_LA_VENTA)

        # Sube el proveedor y se sincroniza de nuevo el MISMO pedido.
        self.poner_costo(self.negro(), COSTO_POSTERIOR)
        self.correr(api)

        self.assertEqual(self.items()[0].costo_unitario_snapshot, COSTO_DE_LA_VENTA)
        self.assertEqual(self.negro().costo_unitario, COSTO_POSTERIOR)

    def test_el_snapshot_aguanta_varias_corridas_seguidas(self):
        """El cron corre cada 20 minutos: no alcanza con aguantar una."""
        api = ApiFalsa([_pedido()])
        self.correr(api)
        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)
        self.correr(api)

        self.poner_costo(self.negro(), COSTO_POSTERIOR)
        for _ in range(5):
            self.correr(api)

        self.assertEqual(self.items()[0].costo_unitario_snapshot, COSTO_DE_LA_VENTA)

    def test_snapshot_se_llena_la_primera_vez(self):
        """Linea nueva con el costo ya cargado: se congela el de hoy.

        El comportamiento de siempre, que el arreglo no tiene que romper.
        """
        self.correr(ApiFalsa([]))
        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)

        self.correr(ApiFalsa([_pedido()]))

        items = self.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].costo_unitario_snapshot, COSTO_DE_LA_VENTA)

    def test_snapshot_nulo_se_completa_cuando_aparece_el_costo(self):
        """NULL no es un snapshot: no hay nada congelado que proteger.

        Es el caso del pedido real de Camila, sincronizado antes de que
        existiera la pantalla de costos. Si el arreglo se pasara de mano y
        conservara tambien los NULL, ese pedido no tendria margen nunca.
        """
        api = ApiFalsa([_pedido()])
        self.correr(api)
        self.assertIsNone(self.items()[0].costo_unitario_snapshot)

        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)
        self.correr(api)

        self.assertEqual(self.items()[0].costo_unitario_snapshot, COSTO_DE_LA_VENTA)

    def test_dos_lineas_del_mismo_producto_conservan_sus_dos_costos(self):
        """Un producto repetido en el pedido no pierde ni mezcla snapshots."""
        lineas = [_linea(ID_VARIANTE_NEGRO, 'Tarjetero (Negro)'),
                  _linea(ID_VARIANTE_NEGRO, 'Tarjetero (Negro) - regalo', precio='0.00')]
        api = ApiFalsa([_pedido(lineas=lineas)])

        self.correr(api)
        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)
        self.correr(api)

        self.poner_costo(self.negro(), COSTO_POSTERIOR)
        self.correr(api)

        snapshots = [i.costo_unitario_snapshot for i in self.items()]
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots, [COSTO_DE_LA_VENTA, COSTO_DE_LA_VENTA])

    def test_cada_variante_conserva_su_propio_costo(self):
        """Negro y Gris son dos productos: no se pisan el snapshot."""
        lineas = [_linea(ID_VARIANTE_NEGRO, 'Tarjetero (Negro)'),
                  _linea(ID_VARIANTE_GRIS, 'Tarjetero (Gris)')]
        api = ApiFalsa([_pedido(lineas=lineas)])

        self.correr(api)
        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)
        self.poner_costo(self.gris(), Decimal('4500.00'))
        self.correr(api)

        self.poner_costo(self.negro(), COSTO_POSTERIOR)
        self.poner_costo(self.gris(), COSTO_POSTERIOR)
        self.correr(api)

        por_producto = {i.producto_id: i.costo_unitario_snapshot for i in self.items()}
        self.assertEqual(por_producto[self.negro().id], COSTO_DE_LA_VENTA)
        self.assertEqual(por_producto[self.gris().id], Decimal('4500.00'))

    def test_una_linea_sin_mapeo_sigue_en_null(self):
        """Sin producto_id no hay costo que congelar, ni antes ni ahora."""
        lineas = [{'product_id': 999999, 'variant_id': 888888, 'sku': None,
                   'name': 'Producto que no esta en el catalogo',
                   'quantity': 1, 'price': '100.00'}]
        api = ApiFalsa([_pedido(lineas=lineas)])

        self.correr(api)
        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)
        self.correr(api)

        items = self.items()
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].producto_id)
        self.assertIsNone(items[0].costo_unitario_snapshot)


class TestElRestoSigueActualizandose(BaseSnapshot):
    """La contracara: que el arreglo no congele el pedido entero.

    Todo lo que cambia con la vida real del pedido -- se despacho, cambio de
    estado, el correo cobro distinto -- tiene que seguir refrescandose en cada
    corrida. Congelar eso seria un bug peor que el que se esta arreglando.
    """

    def test_otros_campos_si_se_actualizan_en_resync(self):
        api = ApiFalsa([_pedido(estado='open', pago='pending', envio=7630)])
        self.correr(api)
        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)
        self.correr(api)

        pedido = self.el_pedido()
        self.assertEqual(pedido.estado_externo, 'pending')
        self.assertEqual(pedido.total_envio, Decimal('7630.00'))
        self.assertEqual(pedido.costo_envio_vendedor, Decimal('7630.00'))

        # Se pago, se cerro y el envio termino costando otra cosa.
        self.poner_costo(self.negro(), COSTO_POSTERIOR)
        self.correr(ApiFalsa([_pedido(estado='closed', pago='paid', envio=9100)]))

        pedido = self.el_pedido()
        self.assertEqual(pedido.estado_externo, 'paid')
        self.assertEqual(pedido.total_envio, Decimal('9100.00'))
        self.assertEqual(pedido.costo_envio_vendedor, Decimal('9100.00'))
        # ...y el unico que no se movio es el snapshot.
        self.assertEqual(self.items()[0].costo_unitario_snapshot, COSTO_DE_LA_VENTA)

    def test_las_lineas_siguen_reflejando_el_pedido_editado(self):
        """Cantidad y precio se refrescan; solo el costo queda congelado."""
        api = ApiFalsa([_pedido()])
        self.correr(api)
        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)
        self.correr(api)

        editado = _pedido(lineas=[
            _linea(ID_VARIANTE_NEGRO, 'Tarjetero Minimalista de Aluminio (Negro)',
                   cantidad=3, precio='7900.00')])
        self.poner_costo(self.negro(), COSTO_POSTERIOR)
        self.correr(ApiFalsa([editado]))

        item = self.items()[0]
        self.assertEqual(item.cantidad, 3)
        self.assertEqual(item.precio_unitario, Decimal('7900.00'))
        self.assertEqual(item.subtotal, Decimal('23700.00'))
        self.assertEqual(item.costo_unitario_snapshot, COSTO_DE_LA_VENTA)

    def test_una_linea_que_se_agrega_despues_toma_el_costo_de_hoy(self):
        """La linea vieja conserva el suyo, la nueva se congela con el actual."""
        api = ApiFalsa([_pedido()])
        self.correr(api)
        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)
        self.correr(api)

        self.poner_costo(self.gris(), COSTO_POSTERIOR)
        ampliado = _pedido(lineas=[
            _linea(ID_VARIANTE_NEGRO, 'Tarjetero (Negro)'),
            _linea(ID_VARIANTE_GRIS, 'Tarjetero (Gris)')])
        self.correr(ApiFalsa([ampliado]))

        por_producto = {i.producto_id: i.costo_unitario_snapshot for i in self.items()}
        self.assertEqual(por_producto[self.negro().id], COSTO_DE_LA_VENTA)
        self.assertEqual(por_producto[self.gris().id], COSTO_POSTERIOR)

    def test_el_sync_no_toca_el_costo_del_catalogo(self):
        """Tiendanube no manda costo confiable: producto.costo_unitario es de
        Roman y el sync no lo pisa, ni al crear ni al actualizar."""
        api = ApiFalsa([_pedido()])
        self.correr(api)
        self.poner_costo(self.negro(), COSTO_DE_LA_VENTA)
        self.correr(api)

        self.assertEqual(self.negro().costo_unitario, COSTO_DE_LA_VENTA)


if __name__ == '__main__':
    unittest.main(verbosity=2)
