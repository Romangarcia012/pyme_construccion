# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S3-FIX (el envio que no cerraba).

    python -m unittest discover -s tests -v

El pedido real de la base no cerraba:

    subtotal 7490.00 - descuentos 1423.10 = 6066.90
    total                                 = 13696.90
    hueco                                 =  7630.00

y `pedido.total_envio` habia quedado en 0.00. La causa no era que Tiendanube no
mandara el envio: es que dejo de mandarlo DONDE lo buscabamos. El 2025/04/24
Tiendanube saco las propiedades de envio del recurso Order ("Removed deprecated
shipping properties from the Order resource in favor of Fulfillment Order
properties") y el monto se mudo adentro de cada fulfillment order:

    fulfillments[].shipping.consumer_cost.value

`shipping_cost_customer` no viene mas en una tienda migrada a multi-inventario,
y como se leia con .get() el 0 entraba sin hacer ruido. Peor: ese 0 es
indistinguible del 0 legitimo, que segun la doc del campo viejo significa envio
gratis. Un pedido con $7630 de envio quedaba igual que uno bonificado.

Lo que sostienen estos tests:

    consumer_cost presente            -> total_envio con el monto, no 0
    varios despachos                  -> se suman, no se pisan
    payload viejo (campo deprecado)   -> sigue leyendose, no se rompe nada
    fulfillments como ids sueltos     -> no explota, cae al campo viejo
    nada en ningun formato            -> 0 sin excepcion, y ese 0 es
                                         "no disponible", NO "gratis"
    traer_pedidos                     -> pide aggregates=fulfillment_orders,
                                         que es lo unico que trae el costo

Fuentes del vocabulario:
https://tiendanube.github.io/api-documentation/resources/fulfillment-order
https://tiendanube.github.io/api-documentation/guides/multi-inventory/access-order
https://tiendanube.github.io/api-documentation/CHANGELOG
"""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ingestor_tiendanube  # noqa: E402
import integracion_tiendanube as tn  # noqa: E402
from ingestor_canal import ENTIDAD_PEDIDO, es_decimal  # noqa: E402

TOKEN_FALSO = 'tn_token_de_prueba_no_es_real_1234567890'
STORE_ID_FALSO = '9876543'

# El pedido 2058709648 tal como esta hoy en Supabase, recortado a lo que mira
# el mapeo. Es el payload SIN el costo de envio: asi lo devolvio la API cuando
# se sincronizo, porque la llamada no pedia los fulfillment orders completos.
PAYLOAD_REAL_SIN_ENVIO = {
    'id': 2058709648,
    'number': '100',
    'created_at': '2026-08-31T17:55:12+0000',
    'status': 'open',
    'payment_status': 'paid',
    'currency': 'ARS',
    'subtotal': '7490.00',
    'discount': '1423.10',
    'discount_coupon': '749.00',
    'discount_gateway': '674.10',
    'total': '13696.90',
    'customer': {'id': 337610261, 'name': 'Camila Valaco'},
    'fulfillments': [{
        'id': '01M1CFC0HPCP82Z1DSGRAAFANV',
        'number': '1',
        'status': 'PACKED',
        'shipping': {
            'type': 'pickup',
            'extras': {'shippable': True},
            'carrier': {'name': 'Envio Nube'},
            'option': {'name': 'Envio Nube - Andreani a sucursal'},
        },
    }],
    'products': [{
        'product_id': 360354459, 'variant_id': 1574653133, 'sku': None,
        'name': 'Tarjetero Minimalista de Aluminio (Negro)',
        'quantity': 1, 'price': '7490.00',
    }],
}

# El hueco que el pedido real no explicaba. Es lo que consumer_cost tiene que
# devolver para que subtotal - descuentos + envio de el `total` de Tiendanube.
ENVIO_REAL = Decimal('7630.00')


def _con_envio(payload, valor, moneda='ARS'):
    """El mismo payload pero con el fulfillment order completo.

    `value` va como numero JSON, no como string: la doc del Fulfillment Order
    lo tipa Decimal, y es el unico importe de toda la API que no llega entre
    comillas.
    """
    copia = dict(payload)
    fulfillment = dict(copia['fulfillments'][0])
    fulfillment['shipping'] = dict(fulfillment['shipping'],
                                   consumer_cost={'value': valor, 'currency': moneda},
                                   merchant_cost={'value': 0, 'currency': moneda})
    copia['fulfillments'] = [fulfillment]
    return copia


class TestEnvioDelPedido(unittest.TestCase):
    """normalizar() es puro: se testea con payloads, sin base ni red."""

    def setUp(self):
        self.ingestor = ingestor_tiendanube.IngestorTiendanube(
            canal_id=1, credenciales={'access_token': TOKEN_FALSO},
            id_tienda_externo=STORE_ID_FALSO)

    def _normalizar(self, payload):
        return self.ingestor.normalizar(ENTIDAD_PEDIDO, payload)

    def test_lee_envio_desde_la_ruta_correcta_del_payload(self):
        """fulfillments[].shipping.consumer_cost.value -> total_envio."""
        datos = self._normalizar(_con_envio(PAYLOAD_REAL_SIN_ENVIO, 7630.00))

        self.assertEqual(datos['total_envio'], ENVIO_REAL)
        self.assertNotEqual(datos['total_envio'], Decimal('0.00'))

    def test_con_el_envio_leido_el_pedido_real_cierra(self):
        """La prueba de que 7630 era el envio y no otra cosa."""
        datos = self._normalizar(_con_envio(PAYLOAD_REAL_SIN_ENVIO, 7630.00))

        armado = datos['total_bruto'] - datos['total_descuentos'] + datos['total_envio']
        self.assertEqual(armado, datos['total'])
        self.assertEqual(armado, Decimal('13696.90'))

    def test_el_envio_es_decimal_y_no_pasa_por_float(self):
        """consumer_cost.value llega como numero JSON: el clasico Decimal(0.1)."""
        datos = self._normalizar(_con_envio(PAYLOAD_REAL_SIN_ENVIO, 0.1))

        self.assertTrue(es_decimal(datos['total_envio']))
        self.assertEqual(datos['total_envio'], Decimal('0.1'))
        # Decimal(0.1) -no Decimal('0.1')- arrastra los 17 digitos del binario:
        # 0.1000000000000000055511151231257827. Que no sea ESE valor es lo que
        # prueba que el numero JSON no paso por float en el camino.
        self.assertNotEqual(datos['total_envio'], Decimal(0.1))

    def test_varios_despachos_suman_su_envio(self):
        """Un pedido partido en dos envios cobra los dos."""
        payload = dict(PAYLOAD_REAL_SIN_ENVIO)
        base = payload['fulfillments'][0]
        payload['fulfillments'] = [
            dict(base, id='A', shipping=dict(base['shipping'],
                 consumer_cost={'value': '7630.00', 'currency': 'ARS'})),
            dict(base, id='B', shipping=dict(base['shipping'],
                 consumer_cost={'value': '2370.00', 'currency': 'ARS'})),
        ]

        self.assertEqual(self._normalizar(payload)['total_envio'], Decimal('10000.00'))

    def test_envio_gratis_es_cero_de_verdad(self):
        """consumer_cost 0 explicito: eso SI es envio bonificado."""
        datos = self._normalizar(_con_envio(PAYLOAD_REAL_SIN_ENVIO, 0))

        self.assertEqual(datos['total_envio'], Decimal('0'))

    def test_payload_viejo_sigue_leyendo_el_campo_deprecado(self):
        """Los pedidos guardados antes del 2025/04/24 no se rompen."""
        viejo = dict(PAYLOAD_REAL_SIN_ENVIO, shipping_cost_customer='3500.00')
        del viejo['fulfillments']

        self.assertEqual(self._normalizar(viejo)['total_envio'], Decimal('3500.00'))

    def test_el_fulfillment_gana_sobre_el_campo_viejo(self):
        """Si vienen los dos manda el nuevo: el viejo, cuando sobrevive,
        refleja solo el primer despacho."""
        payload = _con_envio(dict(PAYLOAD_REAL_SIN_ENVIO,
                                  shipping_cost_customer='1.00'), 7630.00)

        self.assertEqual(self._normalizar(payload)['total_envio'], ENVIO_REAL)

    def test_fulfillments_como_ids_sueltos_no_rompe(self):
        """Sin aggregates la API manda strings, no objetos."""
        payload = dict(PAYLOAD_REAL_SIN_ENVIO,
                       fulfillments=['01BX5ZZKBKACTAV9WEVGEMMVRZ'],
                       shipping_cost_customer='3500.00')

        self.assertEqual(self._normalizar(payload)['total_envio'], Decimal('3500.00'))

    def test_envio_ausente_no_rompe_y_no_inventa(self):
        """El payload real de hoy: ni consumer_cost ni el campo viejo.

        total_envio queda en 0 sin excepcion porque la columna es NOT NULL,
        pero ese 0 significa "Tiendanube no lo mando", NO "envio gratis". La
        diferencia se ve aca: el pedido NO cierra contra su propio `total`, y
        esos 7630 que faltan son justamente el envio que no vino. No se calcula
        el hueco ni se guarda como envio: seria inventar un dato que la API no
        dio, y el mismo hueco podria ser cualquier otro cargo.
        """
        datos = self._normalizar(PAYLOAD_REAL_SIN_ENVIO)

        self.assertEqual(datos['total_envio'], Decimal('0.00'))
        self.assertTrue(es_decimal(datos['total_envio']))

        armado = datos['total_bruto'] - datos['total_descuentos'] + datos['total_envio']
        self.assertNotEqual(armado, datos['total'])
        self.assertEqual(datos['total'] - armado, ENVIO_REAL)

    def test_envio_ausente_devuelve_none_antes_de_aplanarse_a_cero(self):
        """La distincion vive en el helper: None = no vino, CERO = gratis.

        Es lo unico que separa las dos lecturas del 0. Si envio_del_cliente()
        empezara a devolver CERO cuando no hay dato, el mapeo no tendria como
        volver a distinguirlas.
        """
        self.assertIsNone(ingestor_tiendanube.envio_del_cliente(PAYLOAD_REAL_SIN_ENVIO))
        self.assertEqual(
            ingestor_tiendanube.envio_del_cliente(_con_envio(PAYLOAD_REAL_SIN_ENVIO, 0)),
            Decimal('0'))


class TestPedirLosFulfillmentOrders(unittest.TestCase):
    """Sin el aggregate no hay costo que leer, por mas que el mapeo lo busque."""

    def test_traer_pedidos_pide_los_fulfillment_orders_completos(self):
        with mock.patch.object(tn, 'paginar', return_value=[]) as paginar:
            tn.traer_pedidos(STORE_ID_FALSO, TOKEN_FALSO)

        params = paginar.call_args.kwargs['params']
        self.assertEqual(params['aggregates'], 'fulfillment_orders')

    def test_el_aggregate_convive_con_el_filtro_de_fecha(self):
        with mock.patch.object(tn, 'paginar', return_value=[]) as paginar:
            tn.traer_pedidos(STORE_ID_FALSO, TOKEN_FALSO,
                             desde=datetime(2026, 8, 1), hasta=datetime(2026, 8, 31))

        params = paginar.call_args.kwargs['params']
        self.assertEqual(params['aggregates'], 'fulfillment_orders')
        self.assertIn('updated_at_min', params)
        self.assertIn('updated_at_max', params)


if __name__ == '__main__':
    unittest.main()
