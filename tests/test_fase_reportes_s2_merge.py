# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S2-MERGE (una sola pantalla de ventas).

    python -m unittest discover -s tests -v

FASE-REPORTES-S2 dejo dos pantallas que hacian casi lo mismo: /pedidos/listar
(operativa, con el boton de venta nueva) y /pedidos/resumen (solo lectura, con
cliente y despacho). Compartian consulta y orden, asi que la duplicacion no
estaba en el codigo sino en la cabeza de quien mira: cual de las dos es "la"
pantalla de ventas.

Lo que se prueba aca no es el mapeo del despacho -- eso ya lo cubre
test_fase_reportes_s2 -- sino que la fusion no perdio nada:

    las siete columnas quedaron en la pantalla que sobrevive
    el boton de venta nueva sigue arriba de la tabla, con o sin ventas
    quedo una sola ruta sirviendo este listado, no dos
    el orden (lo ultimo vendido primero) es el mismo que antes

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
    CanalVenta,
    Empresa,
    Pedido,
    Usuario,
    db,
)
from app import app  # noqa: E402
from tests.ayuda_auth import request_anonimo  # noqa: E402

ENGINE_PRODUCTIVO = None

RUTA = '/pedidos/listar'

# Lo minimo para que la fila del pedido online tenga cliente, medio y despacho.
PAYLOAD = {
    'gateway_name': 'Pago Nube',
    'shipping_status': 'unshipped',
    'fulfillments': [{'status': 'PACKED'}],
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


class BaseFusion(unittest.TestCase):
    """Una empresa con los dos canales y sesion iniciada."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test MERGE')
        db.session.add(self.empresa)
        db.session.flush()

        usuario = Usuario(nombre='Roman Test', email='merge@test.local',
                          empresa_id=self.empresa.id, rol='admin', verificado=True)
        usuario.set_password('irrelevante')
        db.session.add(usuario)

        canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                              nombre='Korvo', activo=True, id_tienda_externo='9999')
        canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                  nombre='Venta manual / presencial', activo=True)
        db.session.add_all([canal_tn, canal_manual])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.canal_tn_id = canal_tn.id
        self.canal_manual_id = canal_manual.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(usuario.id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def pedido_tn(self, fecha=datetime(2026, 8, 22), id_externo='TN-1',
                  total='5000.00', comprador='Camila Valaco'):
        pedido = Pedido(empresa_id=self.empresa_id, canal_id=self.canal_tn_id,
                        id_externo=id_externo, fecha_pedido=fecha,
                        estado='pendiente', estado_externo='open',
                        comprador_nombre=comprador, moneda='ARS',
                        total_bruto=Decimal(total), total=Decimal(total),
                        raw_payload=PAYLOAD)
        db.session.add(pedido)
        db.session.commit()
        return pedido

    def pedido_mostrador(self, fecha=datetime(2026, 8, 23), total='1200.00'):
        pedido = Pedido(empresa_id=self.empresa_id, canal_id=self.canal_manual_id,
                        fecha_pedido=fecha, estado='completado', moneda='ARS',
                        total_bruto=Decimal(total), total=Decimal(total))
        db.session.add(pedido)
        db.session.commit()
        return pedido

    def html(self):
        resp = self.client.get(RUTA)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)


class TestLaPantallaQueQueda(BaseFusion):

    def test_ruta_fusionada_devuelve_columna_despacho(self):
        """Lo que antes solo estaba en /pedidos/resumen."""
        self.pedido_tn()

        html = self.html()

        self.assertIn('<th>Despacho</th>', html)
        # Y no solo el encabezado: la celda de la fila trae el estado.
        self.assertIn('<span class="despacho despacho-no">', html)

    def test_estan_las_siete_columnas(self):
        """Las cinco que compartian las dos pantallas mas cliente y despacho."""
        self.pedido_tn()

        html = self.html()

        for encabezado in ('<th>Fecha</th>', '<th>Canal</th>', '<th>Cliente</th>',
                           '<th class="num">Total</th>', '<th>Medio de cobro</th>',
                           '<th class="corte">Estado</th>', '<th>Despacho</th>'):
            self.assertIn(encabezado, html,
                          'falta la columna %s' % encabezado)

    def test_boton_nueva_venta_sigue_presente(self):
        """El boton operativo no se perdio al fusionar con la vista de solo
        lectura: es la unica forma de cargar una venta de mostrador."""
        self.pedido_tn()

        html = self.html()

        self.assertIn('/pedidos/manual/nuevo', html)
        self.assertIn('Nueva venta manual', html)

    def test_el_boton_esta_tambien_con_la_tabla_vacia(self):
        """Justo cuando no hay ventas es cuando mas se necesita cargarlas."""
        html = self.html()

        self.assertIn('Todavía no hay ventas cargadas', html)
        self.assertIn('/pedidos/manual/nuevo', html)

    def test_el_boton_esta_arriba_de_la_tabla(self):
        self.pedido_tn()

        html = self.html()

        self.assertLess(html.index('/pedidos/manual/nuevo'), html.index('<table>'),
                        'el boton quedo debajo de la tabla')

    def test_muestra_los_dos_canales_juntos(self):
        self.pedido_tn()
        self.pedido_mostrador()

        html = self.html()

        self.assertIn('Camila Valaco', html)   # cliente del pedido online
        self.assertIn('Pago Nube', html)       # medio que vino en el payload
        self.assertIn('Tiendanube', html)
        self.assertIn('Manual', html)
        self.assertIn('5000.00', html)
        self.assertIn('1200.00', html)

    def test_conserva_el_orden_de_lo_ultimo_vendido_primero(self):
        """El unico criterio de orden que tenia /pedidos/listar."""
        self.pedido_tn(fecha=datetime(2026, 8, 1), id_externo='vieja',
                       total='111.00')
        self.pedido_tn(fecha=datetime(2026, 8, 25), id_externo='nueva',
                       total='222.00')

        html = self.html()

        self.assertLess(html.index('222.00'), html.index('111.00'))


class TestNoQuedanDosRutas(BaseFusion):

    def test_ruta_vieja_no_duplica(self):
        """Solo una ruta sirve este listado.

        No alcanza con que /pedidos/resumen devuelva 404: lo que se afirma es
        que no quedo una segunda ruta -- ni un alias, ni un redirect -- que
        alguien pueda linkear pensando que es "la de verdad".
        """
        reglas = [str(r) for r in app.url_map.iter_rules()
                  if str(r).startswith('/pedidos/')]

        self.assertIn(RUTA, reglas)
        self.assertNotIn('/pedidos/resumen', reglas)

        vistas = [r.endpoint for r in app.url_map.iter_rules()
                  if str(r).startswith('/pedidos/')]
        self.assertNotIn('ventas.resumen_ventas', vistas)

    def test_la_plantilla_sobrante_se_borro(self):
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sobrante = os.path.join(raiz, 'templates', 'ventas_resumen.html')

        self.assertFalse(os.path.exists(sobrante),
                         'quedo viva la plantilla de la pantalla fusionada')

    def test_el_sidebar_no_linkea_dos_veces_al_mismo_listado(self):
        """El dashboard tenia una entrada por pantalla; ahora hay una sola."""
        html = self.client.get('/dashboard').get_data(as_text=True)

        self.assertEqual(html.count('href="%s"' % RUTA), 1)


class TestElRedirectDeLaVentaSigueLlegando(BaseFusion):
    """La ruta que sobrevive es a la que redirige guardar una venta.

    `nueva_venta_manual` termina en url_for('ventas.listar_pedidos'); si la
    fusion hubiera cambiado ese endpoint, la venta se guardaria y el vendedor
    terminaria en un 500 con la plata ya cobrada.
    """

    def test_el_endpoint_del_redirect_apunta_a_la_pantalla_fusionada(self):
        with app.test_request_context():
            from flask import url_for

            self.assertEqual(url_for('ventas.listar_pedidos'), RUTA)


class TestRequiereLogin(BaseFusion):

    def test_la_pantalla_requiere_login(self):
        resp = request_anonimo(self.ctx, 'get', RUTA)

        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])


if __name__ == '__main__':
    unittest.main()
