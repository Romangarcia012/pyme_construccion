# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S3-FIX2 (el detalle real por pedido).

    python -m unittest discover -s tests -v

FASE-REPORTES-S3-FIX agrego ?aggregates=fulfillment_orders y quedo pusheado y
desplegado. No alcanzo: `total_envio` del pedido real siguio en 0.00 despues de
sincronizar con el fix puesto. La causa no era el aggregate sino el ENDPOINT.

Verificado contra la tienda real (pedido 2058709648):

    GET /orders/{id}                          -> fulfillments: ["01M1CF..."]
    GET /orders?aggregates=fulfillment_orders -> fulfillment completo pero
                                                 shipping RECORTADO: type,
                                                 extras, carrier.name,
                                                 option.name. Sin costos.
    GET /orders/{id}?aggregates=fulfillment_orders
                                              -> consumer_cost y merchant_cost

O sea: el aggregate hace falta pero no alcanza. Los costos solo viajan en el
endpoint de DETALLE. El payload que habia guardado en la base era, campo por
campo, el de la lista.

Por eso traer_pedidos() ahora pide la lista y despues el detalle de cada pedido,
y guarda el de detalle. Al top level tiene exactamente las mismas claves que el
de la lista -verificado contra los dos pedidos reales-, asi que es un
superconjunto: no se pierde nada de lo que ya se leia.

Lo que sostienen estos tests:

    raw_payload guardado          -> el de detalle, no el de la lista
    los dos costos                -> 7630 y 7630 en el pedido de Camila
    pedido viejo ya guardado      -> se corrige solo en el proximo sync
    un pedido borrado a mitad     -> no voltea la corrida entera
    un error HTTP de verdad       -> SI la voltea, en vez de guardar a medias

Fuentes del vocabulario:
https://tiendanube.github.io/api-documentation/resources/order
https://tiendanube.github.io/api-documentation/resources/fulfillment-order
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
from ingestor_canal import (ENTIDAD_PEDIDO, ENTIDAD_PRODUCTO,  # noqa: E402
                            ErrorIngesta)
from models import (CanalVenta, CredencialCanal, Empresa, Pedido,  # noqa: E402
                    SyncLog, Usuario, db)

TOKEN_FALSO = 'tn_token_de_prueba_no_es_real_1234567890'
STORE_ID_FALSO = '9876543'

ENVIO_REAL = Decimal('7630.00')


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
# Los dos payloads reales del pedido 2058709648, recortados a lo que mira el
# mapeo. La unica diferencia entre los dos es la que esta slice arregla.
# ---------------------------------------------------------------------------

def _pedido_base():
    return {
        'id': 2058709648,
        'number': '100',
        'created_at': '2026-08-31T17:55:12+0000',
        'status': 'open',
        'payment_status': 'paid',
        'currency': 'ARS',
        'subtotal': '7490.00',
        'discount': '1423.10',
        'total': '13696.90',
        'customer': {'id': 337610261, 'name': 'Camila Valaco'},
        'products': [{
            'product_id': 360354459, 'variant_id': 1574653133, 'sku': 'TARJ-NEG',
            'name': 'Tarjetero Minimalista de Aluminio (Negro)',
            'quantity': 1, 'price': '7490.00',
        }],
    }


def _shipping_de_lista():
    """Lo unico que la lista devuelve del envio. Sin un solo importe."""
    return {
        'type': 'pickup',
        'extras': {'shippable': True},
        'carrier': {'name': 'Envio Nube'},
        'option': {'name': 'Envio Nube - Andreani a sucursal'},
    }


#: El pedido como lo devuelve GET /orders?aggregates=fulfillment_orders.
#: Es, literalmente, lo que estaba guardado en Supabase.
PEDIDO_DE_LISTA = dict(_pedido_base(), fulfillments=[{
    'id': '01M1CFC0HPCP82Z1DSGRAAFANV',
    'number': '1',
    'assigned_location': {'location_id': '01KZR7775NGMXMVCBKWVKRF1ZD',
                          'name': 'Casa Nachi'},
    'status': 'DISPATCHED',
    'shipping': _shipping_de_lista(),
    'tracking_info': {'code': '360003086917760', 'url': 'https://andreani/1'},
}])

#: El mismo pedido por GET /orders/{id}?aggregates=fulfillment_orders.
#: Mismas claves al top level, mas los dos costos adentro del shipping.
PEDIDO_DE_DETALLE = dict(_pedido_base(), fulfillments=[{
    'id': '01M1CFC0HPCP82Z1DSGRAAFANV',
    'number': '1',
    'assigned_location': {'location_id': '01KZR7775NGMXMVCBKWVKRF1ZD',
                          'name': 'Casa Nachi'},
    'status': 'DISPATCHED',
    'shipping': dict(_shipping_de_lista(),
                     carrier={'carrier_id': '5223566', 'name': 'Envio Nube'},
                     merchant_cost={'value': 7630, 'currency': 'ARS'},
                     consumer_cost={'value': 7630, 'currency': 'ARS'}),
    'tracking_info': {'code': '360003086917760', 'url': 'https://andreani/1'},
    # Lo que solo existe en el detalle. No se lee, pero se guarda.
    'recipient': {'name': 'Camila Valaco', 'email': 'camila@test.local'},
}])


class RespuestaFalsa:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class ApiDeDosEndpoints:
    """requests.get de mentira que distingue la lista del detalle.

    Es el mock que importa en esta slice: si los dos endpoints devolvieran lo
    mismo, no habria nada que testear.
    """

    def __init__(self, pedidos_lista, pedidos_detalle=None, detalle_404=False,
                 detalle_500=False):
        self.pedidos_lista = list(pedidos_lista)
        self.por_id = {str(p['id']): p for p in (pedidos_detalle or [])}
        self.detalle_404 = detalle_404
        self.detalle_500 = detalle_500
        self.llamadas = []

    def __call__(self, url, headers=None, params=None, timeout=None):
        params = params or {}
        self.llamadas.append({'url': url, 'params': dict(params)})
        cabeceras = {'x-rate-limit-remaining': '35', 'x-rate-limit-reset': '1000'}

        if '/products' in url:
            return RespuestaFalsa(200, [], headers=cabeceras)

        marca = '/orders/'
        cola = url.split(marca, 1)[1].strip('/') if marca in url else ''
        if cola:
            if self.detalle_500:
                return RespuestaFalsa(500, {'message': 'boom'}, headers=cabeceras)
            if self.detalle_404 or cola not in self.por_id:
                return RespuestaFalsa(404, {'message': 'Not found'})
            return RespuestaFalsa(200, self.por_id[cola], headers=cabeceras)

        if '/orders' in url:
            return RespuestaFalsa(200, self.pedidos_lista, headers=cabeceras)

        return RespuestaFalsa(404, [])

    # -- lecturas sobre lo que se llamo ---------------------------------

    @property
    def detalles_pedidos(self):
        return [ll for ll in self.llamadas
                if '/orders/' in ll['url'] and ll['url'].split('/orders/', 1)[1].strip('/')]


class BaseSync(unittest.TestCase):
    """Una empresa con Tiendanube conectada y nada mas."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        empresa = Empresa(nombre='Empresa Test FIX2')
        db.session.add(empresa)
        db.session.flush()

        usuario = Usuario(nombre='Roman Test', email='fix2@test.local',
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

    def correr(self, api):
        """Una corrida completa del backfill con la API mockeada."""
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
                                      id_externo='2058709648').one()


class TestSePideElDetalle(BaseSync):

    def test_sync_usa_payload_de_detalle_no_de_lista(self):
        """El raw_payload guardado tiene que ser el del detalle.

        La marca que los separa es `recipient` adentro del fulfillment: existe
        solo en el detalle. Si lo guardado no lo tiene, se guardo el de la
        lista y los costos no van a estar nunca.
        """
        api = ApiDeDosEndpoints([PEDIDO_DE_LISTA], [PEDIDO_DE_DETALLE])
        self.correr(api)

        guardado = self.el_pedido().raw_payload
        envio = guardado['fulfillments'][0]['shipping']

        self.assertIn('recipient', guardado['fulfillments'][0])
        self.assertIn('consumer_cost', envio)
        self.assertIn('merchant_cost', envio)

    def test_se_pide_el_detalle_de_cada_pedido_con_el_aggregate(self):
        """Una llamada de detalle por pedido, y con el aggregate puesto."""
        api = ApiDeDosEndpoints([PEDIDO_DE_LISTA], [PEDIDO_DE_DETALLE])
        self.correr(api)

        self.assertEqual(len(api.detalles_pedidos), 1)
        self.assertEqual(api.detalles_pedidos[0]['params']['aggregates'],
                         'fulfillment_orders')
        self.assertIn('/orders/2058709648', api.detalles_pedidos[0]['url'])

    def test_ambos_costos_se_llenan_con_detalle_real(self):
        """Lo que esta slice existe para arreglar: 7630 y 7630, ni None ni 0."""
        api = ApiDeDosEndpoints([PEDIDO_DE_LISTA], [PEDIDO_DE_DETALLE])
        self.correr(api)

        pedido = self.el_pedido()
        self.assertEqual(pedido.total_envio, ENVIO_REAL)
        self.assertEqual(pedido.costo_envio_vendedor, ENVIO_REAL)
        self.assertIsNotNone(pedido.costo_envio_vendedor)

    def test_con_el_envio_leido_el_pedido_real_cierra(self):
        """La prueba de que 7630 era el envio y no otra cosa."""
        api = ApiDeDosEndpoints([PEDIDO_DE_LISTA], [PEDIDO_DE_DETALLE])
        self.correr(api)

        p = self.el_pedido()
        self.assertEqual(p.total_bruto - p.total_descuentos + p.total_envio,
                         p.total)

    def test_el_pedido_no_pierde_nada_de_lo_que_traia_la_lista(self):
        """El detalle es un superconjunto: cambiar de endpoint no rompe nada."""
        api = ApiDeDosEndpoints([PEDIDO_DE_LISTA], [PEDIDO_DE_DETALLE])
        self.correr(api)

        pedido = self.el_pedido()
        self.assertEqual(pedido.numero_externo, '100')
        self.assertEqual(pedido.comprador_nombre, 'Camila Valaco')
        self.assertEqual(pedido.estado, 'open')
        self.assertEqual(pedido.estado_externo, 'paid')
        self.assertEqual(pedido.total_bruto, Decimal('7490.00'))
        self.assertEqual(len(pedido.items), 1)


class TestElPedidoViejoSeCorrigeSolo(BaseSync):
    """Lo que va a pasar con el pedido de Camila en el proximo cron."""

    def test_pedido_viejo_se_corrige_en_proximo_sync(self):
        # Primera corrida: el mundo de antes de esta slice. La lista es lo
        # unico que hay, y el detalle no existe.
        api_vieja = ApiDeDosEndpoints([PEDIDO_DE_LISTA], detalle_404=True)
        self.correr(api_vieja)

        viejo = self.el_pedido()
        self.assertEqual(viejo.total_envio, Decimal('0.00'))
        self.assertIsNone(viejo.costo_envio_vendedor)
        self.assertNotIn('consumer_cost',
                         viejo.raw_payload['fulfillments'][0]['shipping'])
        id_fila = viejo.id

        # Segunda corrida: el detalle ya contesta. El upsert lo pisa.
        db.session.remove()
        api_nueva = ApiDeDosEndpoints([PEDIDO_DE_LISTA], [PEDIDO_DE_DETALLE])
        self.correr(api_nueva)

        corregido = self.el_pedido()
        self.assertEqual(corregido.id, id_fila, 'tiene que ser la MISMA fila')
        self.assertEqual(corregido.total_envio, ENVIO_REAL)
        self.assertEqual(corregido.costo_envio_vendedor, ENVIO_REAL)
        self.assertIn('consumer_cost',
                      corregido.raw_payload['fulfillments'][0]['shipping'])
        self.assertEqual(Pedido.query.count(), 1, 'no se duplico el pedido')


class TestCuandoElDetalleNoViene(BaseSync):
    """Un endpoint mas es un modo de falla mas. Que no se coma la corrida."""

    def test_pedido_borrado_a_mitad_no_voltea_la_corrida(self):
        """404 en el detalle: se guarda el resumido y la corrida sigue.

        Perder el pedido entero seria peor que guardarlo sin los costos.
        """
        api = ApiDeDosEndpoints([PEDIDO_DE_LISTA], detalle_404=True)
        self.correr(api)

        pedido = self.el_pedido()
        self.assertEqual(pedido.numero_externo, '100')
        self.assertEqual(pedido.total_envio, Decimal('0.00'))
        self.assertIsNone(pedido.costo_envio_vendedor)

    def test_un_error_http_de_verdad_si_frena(self):
        """500 en el detalle NO se guarda a medias: falla y el cron reintenta.

        Es la decision opuesta al 404 de arriba, y a proposito: un 500 es un
        problema transitorio del otro lado, no un pedido que ya no existe.
        Tragarselo llenaria la base de pedidos sin costo sin que nadie se
        entere, que es exactamente el bug que esta slice arregla.
        """
        api = ApiDeDosEndpoints([PEDIDO_DE_LISTA], detalle_500=True)

        # Propagar es lo que ya hacia un 500 en la lista: correr_backfill
        # levanta y quien lo llamo cierra el sync_log en 'error'.
        with self.assertRaises(ErrorIngesta):
            self.correr(api)

        self.assertEqual(Pedido.query.count(), 0)


class TestTraerPedidosSinBase(unittest.TestCase):
    """El transporte solo, sin app ni base."""

    def test_la_lista_sigue_llevando_el_aggregate_y_el_filtro_de_fecha(self):
        """La paginacion de la lista no se toco: sigue igual que antes."""
        with mock.patch.object(tn, 'paginar', return_value=[]) as paginar:
            tn.traer_pedidos(STORE_ID_FALSO, TOKEN_FALSO,
                             desde=datetime(2026, 8, 1), hasta=datetime(2026, 8, 31))

        params = paginar.call_args.kwargs['params']
        self.assertEqual(params['aggregates'], 'fulfillment_orders')
        self.assertIn('updated_at_min', params)
        self.assertIn('updated_at_max', params)

    def test_un_pedido_sin_id_no_rompe_ni_pide_detalle(self):
        """Defensa: sin id no hay detalle que pedir, y no se pierde el pedido."""
        raro = {'number': '999'}
        with mock.patch.object(tn, 'paginar', return_value=[raro]), \
             mock.patch.object(tn, 'traer_pedido') as detalle:
            devueltos = tn.traer_pedidos(STORE_ID_FALSO, TOKEN_FALSO)

        detalle.assert_not_called()
        self.assertEqual(devueltos, [raro])

    def test_traer_pedido_devuelve_none_en_404(self):
        respuesta = RespuestaFalsa(404, {'message': 'Not found'})
        with mock.patch.object(tn, '_get_api', return_value=respuesta):
            self.assertIsNone(
                tn.traer_pedido(STORE_ID_FALSO, TOKEN_FALSO, 2058709648))

    def test_traer_pedido_rechaza_una_lista_donde_espera_un_objeto(self):
        """Si la API contesta un listado, es un bug: no se guarda cualquier cosa."""
        respuesta = RespuestaFalsa(200, [PEDIDO_DE_DETALLE])
        with mock.patch.object(tn, '_get_api', return_value=respuesta):
            with self.assertRaises(tn.ErrorTiendanube):
                tn.traer_pedido(STORE_ID_FALSO, TOKEN_FALSO, 2058709648)


if __name__ == '__main__':
    unittest.main()
