# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S2 (listado de ventas con estado de despacho).

    python -m unittest discover -s tests -v

Lo que se prueba no es que la pantalla se vea: es que la respuesta a "¿este
pedido ya salio?" sea la correcta. El dato no tiene columna propia -- se deriva
de pedido.raw_payload en cada lectura -- asi que lo unico que lo sostiene es el
mapeo del vocabulario de Tiendanube, y el mapeo solo lo sostienen estos tests.

    fulfillment PACKED (el pedido real de hoy) -> no
    fulfillment DISPATCHED / DELIVERED         -> si
    dos fulfillments y uno sin salir           -> no
    venta de mostrador                         -> mostrador, nunca si/no
    raw_payload NULL o sin las claves          -> sin dato, no un 500
    re-sync con payload nuevo                  -> el despacho se actualiza

El vocabulario que se afirma aca sale de la documentacion de Tiendanube:
https://tiendanube.github.io/api-documentation/resources/fulfillment-order
https://tiendanube.github.io/api-documentation/resources/order

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (  # noqa: E402
    DESPACHO_MOSTRADOR,
    DESPACHO_NO,
    DESPACHO_SI,
    DESPACHO_SIN_DATO,
    CanalVenta,
    Empresa,
    Pago,
    Pedido,
    Producto,
    Usuario,
    db,
)
from app import app  # noqa: E402
from tests.ayuda_auth import request_anonimo  # noqa: E402

ENGINE_PRODUCTIVO = None

# El pedido que hoy esta de verdad en la base, recortado a lo que mira el
# reporte. Es un retiro en sucursal de Andreani: shipping.extras.shippable es
# true, asi que su flujo pasa por DISPATCHED y no por READY_FOR_PICKUP.
PAYLOAD_REAL = {
    'id': 2058709648,
    'shipping_status': 'unshipped',
    'gateway_name': 'Pago Nube',
    'contact_name': 'Camila Valaco',
    'fulfillments': [{
        'id': '01M1CFC0HPCP82Z1DSGRAAFANV',
        'number': '1',
        'status': 'PACKED',
        'shipping': {
            'type': 'pickup',
            'extras': {'shippable': True},
            'carrier': {'name': 'Envío Nube'},
        },
    }],
}


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


class BaseDespacho(unittest.TestCase):
    """Una empresa con su canal de Tiendanube y su canal de mostrador."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-REPORTES-S2')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='fasereportes2@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True, id_tienda_externo='9999')
        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial', activo=True)
        db.session.add_all([self.canal_tn, self.canal_manual])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.canal_tn_id = self.canal_tn.id
        self.canal_manual_id = self.canal_manual.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def pedido_tn(self, payload, id_externo='2058709648', estado='open',
                  total=Decimal('45000.00')):
        pedido = Pedido(empresa_id=self.empresa_id, canal_id=self.canal_tn_id,
                        id_externo=id_externo, fecha_pedido=datetime(2026, 8, 20),
                        estado=estado, estado_externo='paid',
                        comprador_nombre='Camila Valaco',
                        total=total, raw_payload=payload)
        db.session.add(pedido)
        db.session.commit()
        return pedido

    def venta_mostrador(self, medio='efectivo', total=Decimal('12000.00')):
        pedido = Pedido(empresa_id=self.empresa_id, canal_id=self.canal_manual_id,
                        id_externo=None, fecha_pedido=datetime(2026, 8, 21),
                        estado='completado', total=total, nota='cliente de siempre')
        db.session.add(pedido)
        db.session.flush()
        db.session.add(Pago(pedido_id=pedido.id, canal_id=self.canal_manual_id,
                            procesador='manual', metodo=medio, estado='acreditado',
                            moneda='ARS', monto_bruto=total, monto_neto=total,
                            fecha_pago=datetime(2026, 8, 21)))
        db.session.commit()
        return pedido


class TestMapeoDeDespacho(BaseDespacho):
    """El vocabulario de Tiendanube -> si / no / sin dato."""

    def test_despachado_con_shipping_status_unshipped(self):
        """El unico pedido real de la base: empacado pero sin salir."""
        pedido = self.pedido_tn(PAYLOAD_REAL)
        self.assertEqual(pedido.estado_despacho, DESPACHO_NO)

    def test_despachado_con_shipping_status_shipped_o_equivalente(self):
        """El mismo pedido una vez despachado.

        Se prueban los dos estados de fulfillment que la documentacion define
        como "ya salio" y los tres nombres que puede tomar el shipping_status
        del pedido para lo mismo -- conviven dos generaciones de la API y el
        reporte tiene que entender las dos.
        """
        for estado in ('DISPATCHED', 'DELIVERED'):
            payload = dict(PAYLOAD_REAL)
            payload['fulfillments'] = [dict(PAYLOAD_REAL['fulfillments'][0],
                                            status=estado)]
            # El shipping_status queda deliberadamente atrasado: el fulfillment
            # manda, porque el del pedido esta deprecado del lado de Tiendanube.
            payload['shipping_status'] = 'unshipped'
            pedido = self.pedido_tn(payload, id_externo='tn-%s' % estado)
            self.assertEqual(pedido.estado_despacho, DESPACHO_SI,
                             'fulfillment %s tendria que contar como despachado' % estado)

        for estado in ('shipped', 'fulfilled', 'delivered'):
            # Sin fulfillments: el respaldo es el shipping_status del pedido.
            pedido = self.pedido_tn({'shipping_status': estado},
                                    id_externo='ss-%s' % estado)
            self.assertEqual(pedido.estado_despacho, DESPACHO_SI,
                             'shipping_status %s tendria que contar como despachado' % estado)

    def test_estados_previos_al_despacho_dan_no(self):
        """Todo lo que todavia esta en poder del vendedor.

        READY_FOR_PICKUP entra aca a proposito: la documentacion lo rechaza
        para el envio a domicilio, y en el retiro en el local significa que el
        paquete espera al cliente en el mostrador, no que salio.
        """
        for estado in ('UNPACKED', 'IN_PREPARATION', 'PACKED', 'READY_FOR_PICKUP'):
            payload = {'fulfillments': [{'status': estado}]}
            pedido = self.pedido_tn(payload, id_externo='prev-%s' % estado)
            self.assertEqual(pedido.estado_despacho, DESPACHO_NO,
                             'fulfillment %s no tendria que contar como despachado' % estado)

        for estado in ('unpacked', 'unshipped', 'packed',
                       'partially_packed', 'partially_fulfilled'):
            pedido = self.pedido_tn({'shipping_status': estado},
                                    id_externo='prevss-%s' % estado)
            self.assertEqual(pedido.estado_despacho, DESPACHO_NO,
                             'shipping_status %s no tendria que contar como despachado' % estado)

    def test_un_fulfillment_sin_salir_deja_el_pedido_en_no(self):
        """Dos paquetes, uno todavia en el deposito: el pedido no salio.

        Es la pregunta que se hace mirando el reporte -- "¿me puedo olvidar de
        este pedido?" -- y con una caja sin despachar la respuesta es que no.
        """
        payload = {'fulfillments': [{'status': 'DISPATCHED'}, {'status': 'PACKED'}]}
        pedido = self.pedido_tn(payload, id_externo='parcial')
        self.assertEqual(pedido.estado_despacho, DESPACHO_NO)

        payload = {'fulfillments': [{'status': 'DISPATCHED'}, {'status': 'DELIVERED'}]}
        pedido = self.pedido_tn(payload, id_externo='ambos')
        self.assertEqual(pedido.estado_despacho, DESPACHO_SI)

    def test_venta_presencial_no_aplica(self):
        """La venta de mostrador nunca contesta si/no: se entrega en el acto."""
        pedido = self.venta_mostrador()
        self.assertEqual(pedido.estado_despacho, DESPACHO_MOSTRADOR)
        self.assertNotIn(pedido.estado_despacho, (DESPACHO_SI, DESPACHO_NO))

        # Ni siquiera si alguien le dejara un payload encima: el canal manda.
        pedido.raw_payload = {'shipping_status': 'shipped'}
        db.session.commit()
        self.assertEqual(pedido.estado_despacho, DESPACHO_MOSTRADOR)

    def test_pedido_sin_raw_payload_no_rompe(self):
        """Payload NULL, vacio, o sin las claves esperadas: sin dato, no un 500."""
        casos = [
            ('nulo', None),
            ('vacio', {}),
            ('otras-claves', {'id': 1, 'total': '100.00'}),
            ('fulfillments-vacio', {'fulfillments': []}),
            # La API puede devolver solo los ids en vez de los objetos: ahi no
            # hay estado que leer y no se inventa uno.
            ('fulfillments-ids', {'fulfillments': ['01M1CFC0HPCP82Z1DSGRAAFANV']}),
            ('no-es-dict', 'unshipped'),
        ]
        for nombre, payload in casos:
            pedido = self.pedido_tn(payload, id_externo='sindato-%s' % nombre)
            self.assertEqual(pedido.estado_despacho, DESPACHO_SIN_DATO,
                             'el caso %s tendria que quedar sin dato' % nombre)

    def test_fulfillments_sin_estado_cae_al_shipping_status(self):
        """Ids en vez de objetos, pero el pedido si trae shipping_status."""
        pedido = self.pedido_tn({'fulfillments': ['abc'], 'shipping_status': 'shipped'},
                                id_externo='mixto')
        self.assertEqual(pedido.estado_despacho, DESPACHO_SI)


class TestListadoDeVentas(BaseDespacho):
    """La pantalla: una fila por venta con la columna de despacho."""

    def test_muestra_una_fila_por_venta_con_su_despacho(self):
        self.pedido_tn(PAYLOAD_REAL)
        self.venta_mostrador(medio='efectivo')

        resp = self.client.get('/pedidos/resumen')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn('Camila Valaco', html)      # cliente del pedido online
        self.assertIn('Pago Nube', html)          # medio de cobro de Tiendanube
        self.assertIn('Efectivo', html)           # medio de cobro del mostrador
        self.assertIn('Tiendanube', html)
        self.assertIn('Mostrador', html)          # la venta presencial
        self.assertIn('>No<', html)               # el pedido empacado sin salir

    def test_el_despachado_se_muestra_como_si(self):
        payload = dict(PAYLOAD_REAL)
        payload['fulfillments'] = [dict(PAYLOAD_REAL['fulfillments'][0],
                                        status='DISPATCHED')]
        self.pedido_tn(payload)

        html = self.client.get('/pedidos/resumen').get_data(as_text=True)
        # Se mira el span de la fila y no el nombre de la clase suelto: los
        # cuatro estados aparecen igual en el <style> de la plantilla.
        self.assertIn('<span class="despacho despacho-si">', html)
        self.assertNotIn('<span class="despacho despacho-no">', html)

    def test_cuenta_las_ventas_sin_despachar(self):
        self.pedido_tn(PAYLOAD_REAL, id_externo='uno')
        self.pedido_tn(PAYLOAD_REAL, id_externo='dos')
        # Ni la despachada ni la de mostrador entran en la cuenta.
        self.pedido_tn({'shipping_status': 'shipped'}, id_externo='tres')
        self.venta_mostrador()

        html = self.client.get('/pedidos/resumen').get_data(as_text=True)
        self.assertIn('2 ventas todavía sin despachar', html)

    def test_sin_ventas_no_explota(self):
        resp = self.client.get('/pedidos/resumen')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Todavía no hay ventas cargadas', resp.get_data(as_text=True))

    def test_no_muestra_ventas_de_otra_empresa(self):
        otra = Empresa(nombre='Otra empresa')
        db.session.add(otra)
        db.session.flush()
        canal_ajeno = CanalVenta(empresa_id=otra.id, tipo='tiendanube',
                                 nombre='Tienda ajena', activo=True)
        db.session.add(canal_ajeno)
        db.session.flush()
        db.session.add(Pedido(empresa_id=otra.id, canal_id=canal_ajeno.id,
                              id_externo='ajeno', fecha_pedido=datetime(2026, 8, 22),
                              estado='open', comprador_nombre='Cliente Ajeno',
                              total=Decimal('99999.00'), raw_payload=PAYLOAD_REAL))
        db.session.commit()

        html = self.client.get('/pedidos/resumen').get_data(as_text=True)
        self.assertNotIn('Cliente Ajeno', html)
        self.assertNotIn('99999', html)

    def test_requiere_login(self):
        resp = request_anonimo(self.ctx, 'get', '/pedidos/resumen')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])


class TestResyncActualizaElDespacho(BaseDespacho):
    """El motivo por el que el despacho no es una columna.

    El sync pisa raw_payload en cada corrida (sync_tiendanube._upsert_pedido lo
    asigna sin preguntar si el pedido es nuevo). Derivar el despacho de ahi es
    lo que hace que el reporte muestre el estado de hoy y no el del dia en que
    el pedido entro por primera vez.
    """

    def test_el_segundo_payload_pisa_al_primero(self):
        pedido = self.pedido_tn(PAYLOAD_REAL)
        self.assertEqual(pedido.estado_despacho, DESPACHO_NO)
        pedido_id = pedido.id

        # Segunda corrida del sync: el mismo pedido, ya despachado.
        payload_nuevo = dict(PAYLOAD_REAL)
        payload_nuevo['fulfillments'] = [dict(PAYLOAD_REAL['fulfillments'][0],
                                              status='DISPATCHED')]
        pedido.raw_payload = payload_nuevo
        db.session.commit()
        db.session.remove()

        recargado = db.session.get(Pedido, pedido_id)
        self.assertEqual(recargado.raw_payload['fulfillments'][0]['status'], 'DISPATCHED')
        self.assertEqual(recargado.estado_despacho, DESPACHO_SI)

    def test_el_upsert_del_sync_asigna_raw_payload_siempre(self):
        """El upsert no condiciona la asignacion a que el pedido sea nuevo.

        Se lee el fuente en vez de correr el sync entero contra la API: lo que
        importa es que nadie meta un `if es_nuevo:` delante de esa linea sin
        que la suite se ponga roja.
        """
        import inspect

        import sync_tiendanube

        fuente = inspect.getsource(sync_tiendanube._upsert_pedido)
        self.assertIn('pedido.raw_payload = crudo', fuente)

        cuerpo = fuente.split('pedido.raw_payload = crudo')[0]
        # La unica rama sobre es_nuevo del upsert es la que crea la fila, y
        # cierra antes de las asignaciones.
        self.assertNotIn('if es_nuevo', cuerpo.split('db.session.add(pedido)')[-1])


if __name__ == '__main__':
    unittest.main()
