# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S3-ENVIO-VENDEDOR (el envio que pagas vos).

    python -m unittest discover -s tests -v

`total_envio` guarda lo que el COMPRADOR pago de envio (consumer_cost). Al lado,
en el mismo nodo del payload, viaja `merchant_cost`: lo que ese mismo envio le
costo a la TIENDA. Hasta esta slice no se guardaba en ningun lado.

No son el mismo numero por definicion, aunque en el pedido real de hoy empaten:

    envio bonificado  -> el comprador paga 0 y el flete lo paga igual la tienda
    envio fijo        -> el correo sale mas caro y la diferencia la come la tienda

`total_envio` es plata que ENTRA; `costo_envio_vendedor` es plata que SALE.
Sin las dos, el margen de un pedido con envio no se puede calcular.

Ruta confirmada contra la API real (pedido 2058709648, Camila Valaco):

    fulfillments[].shipping.merchant_cost.value = 7630  (ARS)

Lo que sostienen estos tests:

    merchant_cost presente               -> costo_envio_vendedor con el monto
    payload viejo (shipping_cost_owner)  -> se sigue leyendo
    merchant_cost ausente                -> None, y ese None NO es 0
    los dos montos                       -> se leen por separado y no se pisan

Fuente del vocabulario:
https://tiendanube.github.io/api-documentation/resources/fulfillment-order
"""

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ingestor_tiendanube  # noqa: E402
from ingestor_canal import ENTIDAD_PEDIDO, es_decimal  # noqa: E402

TOKEN_FALSO = 'tn_token_de_prueba_no_es_real_1234567890'
STORE_ID_FALSO = '9876543'

# El pedido 2058709648 tal como lo devuelve HOY el endpoint de detalle con
# ?aggregates=fulfillment_orders, recortado a lo que mira el mapeo. A
# diferencia del payload que hay guardado en la base, este si trae los dos
# costos de envio.
PAYLOAD_REAL = {
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
    'fulfillments': [{
        'id': '01M1CFC0HPCP82Z1DSGRAAFANV',
        'number': '1',
        'status': 'DISPATCHED',
        'shipping': {
            'type': 'pickup',
            'carrier': {'carrier_id': '5223566', 'name': 'Envio Nube'},
            'option': {'name': 'Envio Nube - Andreani a sucursal'},
            # Los dos numeros del pedido real. Empatan, y por eso los tests que
            # importan son los que los separan.
            'merchant_cost': {'value': 7630, 'currency': 'ARS'},
            'consumer_cost': {'value': 7630, 'currency': 'ARS'},
        },
    }],
    'products': [{
        'product_id': 360354459, 'variant_id': 1574653133, 'sku': None,
        'name': 'Tarjetero Minimalista de Aluminio (Negro)',
        'quantity': 1, 'price': '7490.00',
    }],
}

# Lo que la API devuelve en merchant_cost.value para ese pedido.
ENVIO_DEL_VENDEDOR_REAL = Decimal('7630')


def _sin_costos(payload):
    """El mismo payload como llega del endpoint de LISTA: sin ningun costo.

    No es un caso inventado: es exactamente lo que hay guardado hoy en la base.
    """
    copia = dict(payload)
    fulfillment = dict(copia['fulfillments'][0])
    envio = dict(fulfillment['shipping'])
    del envio['merchant_cost']
    del envio['consumer_cost']
    fulfillment['shipping'] = envio
    copia['fulfillments'] = [fulfillment]
    return copia


def _con_costos(payload, merchant=None, consumer=None, moneda='ARS'):
    """El payload con los costos que se le pidan, y solo esos."""
    copia = dict(payload)
    fulfillment = dict(copia['fulfillments'][0])
    envio = dict(fulfillment['shipping'])
    envio.pop('merchant_cost', None)
    envio.pop('consumer_cost', None)
    if merchant is not None:
        envio['merchant_cost'] = {'value': merchant, 'currency': moneda}
    if consumer is not None:
        envio['consumer_cost'] = {'value': consumer, 'currency': moneda}
    fulfillment['shipping'] = envio
    copia['fulfillments'] = [fulfillment]
    return copia


class TestCostoDeEnvioDelVendedor(unittest.TestCase):
    """normalizar() es puro: se testea con payloads, sin base ni red."""

    def setUp(self):
        self.ingestor = ingestor_tiendanube.IngestorTiendanube(
            canal_id=1, credenciales={'access_token': TOKEN_FALSO},
            id_tienda_externo=STORE_ID_FALSO)

    def _normalizar(self, payload):
        return self.ingestor.normalizar(ENTIDAD_PEDIDO, payload)

    def test_lee_merchant_cost_de_fulfillments(self):
        """fulfillments[].shipping.merchant_cost.value -> costo_envio_vendedor."""
        datos = self._normalizar(PAYLOAD_REAL)

        self.assertEqual(datos['costo_envio_vendedor'], ENVIO_DEL_VENDEDOR_REAL)
        self.assertTrue(es_decimal(datos['costo_envio_vendedor']))

    def test_merchant_cost_cae_a_campo_viejo_si_no_hay_nuevo(self):
        """Los pedidos guardados antes del 2025/04/24 no se rompen.

        `shipping_cost_owner` es donde vivia este mismo monto en el recurso
        Order antes de que Tiendanube lo mudara al fulfillment order.
        """
        viejo = dict(_sin_costos(PAYLOAD_REAL), shipping_cost_owner='3500.00')
        del viejo['fulfillments']

        self.assertEqual(self._normalizar(viejo)['costo_envio_vendedor'],
                         Decimal('3500.00'))

    def test_merchant_cost_ausente_queda_none_no_cero(self):
        """None es "no vino el dato"; 0 seria "el flete fue gratis".

        Es la unica diferencia de forma con total_envio, que es NOT NULL y por
        eso tiene que aplanar el faltante a 0. Aca la columna es nullable
        justamente para no tener que mentir.
        """
        datos = self._normalizar(_sin_costos(PAYLOAD_REAL))

        self.assertIsNone(datos['costo_envio_vendedor'])
        self.assertNotEqual(datos['costo_envio_vendedor'], Decimal('0'))

    def test_merchant_cost_cero_explicito_si_es_cero(self):
        """La contracara: si la API manda 0, ese 0 es un dato y se guarda."""
        datos = self._normalizar(_con_costos(PAYLOAD_REAL, merchant=0))

        self.assertEqual(datos['costo_envio_vendedor'], Decimal('0'))
        self.assertIsNotNone(datos['costo_envio_vendedor'])

    def test_el_fulfillment_gana_sobre_el_campo_viejo(self):
        """Si vienen los dos manda el nuevo, igual que con el del cliente."""
        payload = dict(PAYLOAD_REAL, shipping_cost_owner='1.00')

        self.assertEqual(self._normalizar(payload)['costo_envio_vendedor'],
                         ENVIO_DEL_VENDEDOR_REAL)

    def test_varios_despachos_suman_su_costo(self):
        """Un pedido partido en dos envios paga los dos fletes."""
        payload = dict(PAYLOAD_REAL)
        base = payload['fulfillments'][0]
        payload['fulfillments'] = [
            dict(base, id='A', shipping=dict(base['shipping'],
                 merchant_cost={'value': '7630.00', 'currency': 'ARS'})),
            dict(base, id='B', shipping=dict(base['shipping'],
                 merchant_cost={'value': '2370.00', 'currency': 'ARS'})),
        ]

        self.assertEqual(self._normalizar(payload)['costo_envio_vendedor'],
                         Decimal('10000.00'))

    def test_el_costo_es_decimal_y_no_pasa_por_float(self):
        """merchant_cost.value llega como numero JSON, no como string."""
        datos = self._normalizar(_con_costos(PAYLOAD_REAL, merchant=0.1))

        self.assertTrue(es_decimal(datos['costo_envio_vendedor']))
        self.assertEqual(datos['costo_envio_vendedor'], Decimal('0.1'))
        # Decimal(0.1) -no Decimal('0.1')- arrastra los 17 digitos del binario.
        # Que no sea ESE valor prueba que el numero JSON no paso por float.
        self.assertNotEqual(datos['costo_envio_vendedor'], Decimal(0.1))

    def test_fulfillments_como_ids_sueltos_no_rompe(self):
        """Sin aggregates la API manda strings, no objetos."""
        payload = dict(PAYLOAD_REAL,
                       fulfillments=['01BX5ZZKBKACTAV9WEVGEMMVRZ'],
                       shipping_cost_owner='3500.00')

        self.assertEqual(self._normalizar(payload)['costo_envio_vendedor'],
                         Decimal('3500.00'))


class TestLosDosCostosSonDatosDistintos(unittest.TestCase):
    """El riesgo real de esta slice no es leer mal: es cruzar los cables."""

    def setUp(self):
        self.ingestor = ingestor_tiendanube.IngestorTiendanube(
            canal_id=1, credenciales={'access_token': TOKEN_FALSO},
            id_tienda_externo=STORE_ID_FALSO)

    def _normalizar(self, payload):
        return self.ingestor.normalizar(ENTIDAD_PEDIDO, payload)

    def test_envio_bonificado_el_comprador_paga_cero_y_la_tienda_no(self):
        """El caso que justifica la columna entera."""
        datos = self._normalizar(_con_costos(PAYLOAD_REAL, merchant=7630, consumer=0))

        self.assertEqual(datos['total_envio'], Decimal('0'))
        self.assertEqual(datos['costo_envio_vendedor'], Decimal('7630'))

    def test_cada_monto_sale_de_su_propia_clave(self):
        """Montos distintos a proposito: si se cruzan, salta aca."""
        datos = self._normalizar(_con_costos(PAYLOAD_REAL, merchant=111, consumer=999))

        self.assertEqual(datos['costo_envio_vendedor'], Decimal('111'))
        self.assertEqual(datos['total_envio'], Decimal('999'))

    def test_falta_uno_y_esta_el_otro(self):
        """Que no venga merchant_cost no puede borrar consumer_cost, ni al reves."""
        solo_consumer = self._normalizar(_con_costos(PAYLOAD_REAL, consumer=7630))
        self.assertEqual(solo_consumer['total_envio'], Decimal('7630'))
        self.assertIsNone(solo_consumer['costo_envio_vendedor'])

        solo_merchant = self._normalizar(_con_costos(PAYLOAD_REAL, merchant=7630))
        self.assertEqual(solo_merchant['costo_envio_vendedor'], Decimal('7630'))
        # total_envio es NOT NULL: el faltante se aplana a 0 en el mapeo.
        self.assertEqual(solo_merchant['total_envio'], Decimal('0'))

    def test_el_helper_distingue_no_vino_de_vino_cero(self):
        """La distincion vive en envio_del_vendedor(), igual que en su hermano."""
        self.assertIsNone(
            ingestor_tiendanube.envio_del_vendedor(_sin_costos(PAYLOAD_REAL)))
        self.assertEqual(
            ingestor_tiendanube.envio_del_vendedor(_con_costos(PAYLOAD_REAL, merchant=0)),
            Decimal('0'))


if __name__ == '__main__':
    unittest.main()
