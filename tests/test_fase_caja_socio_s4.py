# -*- coding: utf-8 -*-
"""Tests de FASE-CAJA-SOCIO-S4 (la comision de plataforma le resta a la caja).

    python -m unittest discover -s tests -v

LA PREGUNTA

`pedido.comision_plataforma` se carga a mano desde el listado de ventas desde
FASE-REPORTES-S3, y el reporte de margen la usa. /reportes/caja-socio no: su
"tenes realmente" restaba los gastos pagados desde la cuenta y nada mas, asi
que mostraba como disponible una plata que Tiendanube o Mercado Libre ya se
habian quedado antes de depositar. El numero no estaba roto -- estaba de mas.

    facturado    = SUM(pedido.total)                     [NO cambia]
    comision     = SUM(pedido.comision_plataforma)       [nuevo]
    gastado      = SUM(gasto.monto) origen='facturacion' [NO cambia]
    saldo_real   = facturado - comision - gastado

Lo que se prueba:

    la comision resta             -> el saldo baja exactamente esa comision, y
                                     nada mas que esa comision
    el bruto no se movio          -> `total` sigue siendo SUM(pedido.total);
                                     el envio y los impuestos siguen adentro
    NULL no es cero               -> un pedido sin comision cargada no se
                                     resta como 0: se cuenta aparte y el
                                     faltante se ve en la pantalla
    0 explicito SI es cero        -> una venta de mostrador con comision 0
                                     cargada no es un faltante
    la comision sigue al pedido   -> una venta reasignada a mano se lleva su
                                     comision al socio que la cobro
    la de otro socio no le pega   -> la comision de Roman no le baja el saldo
                                     a Nachi
    la pantalla lo muestra        -> la resta va escrita como linea propia, no
                                     escondida dentro del resultado

EL BRUTO NO SE TOCA

`test_facturado_bruto_no_cambia` esta para fijar eso: "Facturado" sigue siendo
lo que pago el COMPRADOR -- envio e impuestos incluidos -- porque esa plata
efectivamente pasa por la cuenta y despues hay que pagarla. La comision no
pasa por ningun lado: se descuenta antes de depositar. Por eso una resta y lo
otro no, y por eso el bruto sigue mostrandose al lado del saldo.

ESTO NO ES EL REPORTE DE MARGEN

Aquel tambien resta comision y no comparte una linea con este. Contesta otra
pregunta -- cuanto se gano con un producto -- y para eso necesita ademas el
costo de la mercaderia y el flete. Este contesta cuanta plata hay en la
cuenta. La suite de margen queda intacta.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.ayuda_auth import request_anonimo  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    ORIGEN_FACTURACION,
    CanalVenta,
    Categoria,
    CuentaCobro,
    Empresa,
    Gasto,
    Pedido,
    Usuario,
    db,
)

ENGINE_PRODUCTIVO = None


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


class BaseCaja(unittest.TestCase):
    """El reparto real: Tiendanube cobra en Roman, Mercado Libre en Nachi.

    Los montos son redondos a proposito: la resta se tiene que poder verificar
    de un vistazo, sin sacar la calculadora para saber si el test esta bien.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test CAJA-SOCIO-S4')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test',
                               email='cajasocio4@test.local',
                               empresa_id=self.empresa.id, rol='admin',
                               verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.cuenta_roman = CuentaCobro(empresa_id=self.empresa.id,
                                        nombre='Roman - Presencial y Tiendanube',
                                        tipo='mercadopago', socio='roman')
        self.cuenta_nachi = CuentaCobro(empresa_id=self.empresa.id,
                                        nombre='Nachi - Mercado Libre',
                                        tipo='mercadopago', socio='nachi')
        db.session.add_all([self.cuenta_roman, self.cuenta_nachi])

        self.categoria = Categoria(nombre='Materiales', tipo='gasto',
                                   empresa_id=self.empresa.id)
        db.session.add(self.categoria)
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.id_cuenta_roman = self.cuenta_roman.id
        self.id_cuenta_nachi = self.cuenta_nachi.id
        self.id_categoria = self.categoria.id

        self.canal_roman = CanalVenta(empresa_id=self.empresa_id,
                                      tipo='tiendanube', nombre='Korvo',
                                      activo=True, id_tienda_externo='9999',
                                      cuenta_cobro_id=self.id_cuenta_roman)
        self.canal_nachi = CanalVenta(empresa_id=self.empresa_id,
                                      tipo='mercadolibre', nombre='Mercado Libre',
                                      activo=True,
                                      cuenta_cobro_id=self.id_cuenta_nachi)
        db.session.add_all([self.canal_roman, self.canal_nachi])
        db.session.commit()

        self.id_canal_roman = self.canal_roman.id
        self.id_canal_nachi = self.canal_nachi.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def venta(self, canal_id=None, total='10000.00', comision=None,
              override=None, estado='open'):
        """Un pedido. `comision=None` es la venta a la que no se la cargaron."""
        fila = Pedido(empresa_id=self.empresa_id,
                      canal_id=canal_id or self.id_canal_roman,
                      fecha_pedido=datetime(2026, 9, 1, 12, 0),
                      estado=estado, comprador_nombre='Camila',
                      total=Decimal(total),
                      comision_plataforma=(None if comision is None
                                           else Decimal(comision)),
                      cuenta_cobro_override_id=override)
        db.session.add(fila)
        db.session.commit()
        return fila

    def gasto(self, monto='3000.00', cuenta_pago_id=None):
        fila = Gasto(empresa_id=self.empresa_id, usuario_id=self.usuario_id,
                     categoria_id=self.id_categoria,
                     descripcion='Gasto sembrado', monto=Decimal(monto),
                     fecha=date(2026, 9, 1),
                     origen_fondo=ORIGEN_FACTURACION,
                     cuenta_pago_id=cuenta_pago_id or self.id_cuenta_roman)
        db.session.add(fila)
        db.session.commit()
        return fila

    def reporte(self):
        """Lo que /reportes/caja-socio le pasa a la plantilla."""
        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado['context'] = context

        template_rendered.connect(anotar, app)
        try:
            respuesta = self.client.get('/reportes/caja-socio')
        finally:
            template_rendered.disconnect(anotar, app)

        return respuesta, capturado.get('context', {})

    def socio(self, contexto, clave):
        for fila in contexto['socios']:
            if fila['clave'] == clave:
                return fila
        self.fail('no salio el socio %r en el reporte' % clave)


class TestComisionResta(BaseCaja):
    """PARTE 1: la comision sale del "tenes realmente"."""

    def test_comision_resta_de_tenes_realmente(self):
        """10.000 facturados, 1.500 de comision, 3.000 gastados -> 5.500.

        Y el saldo baja EXACTAMENTE la comision: 7.000 era lo que daba antes de
        esta slice, 5.500 es eso menos los 1.500. Si diera otra cosa, la
        comision se estaria restando dos veces o mordiendo algo mas.
        """
        self.venta(total='10000.00', comision='1500.00')
        self.gasto(monto='3000.00')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('10000.00'))
        self.assertEqual(roman['comision'], Decimal('1500.00'))
        self.assertEqual(roman['gastado'], Decimal('3000.00'))
        self.assertEqual(roman['saldo_real'], Decimal('5500.00'))
        self.assertEqual(roman['saldo_real'],
                         Decimal('7000.00') - roman['comision'])

    def test_comision_sola_sin_gastos_tambien_resta(self):
        """Sin un solo gasto cargado, la comision igual baja el saldo."""
        self.venta(total='10000.00', comision='1500.00')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['gastado'], Decimal('0.00'))
        self.assertEqual(roman['saldo_real'], Decimal('8500.00'))

    def test_facturado_bruto_no_cambia(self):
        """El "Facturado" sigue siendo lo que pago el comprador.

        Es el numero que se cruza contra el listado de ventas: si la comision
        se le descontara tambien a el, dejaria de coincidir con lo que dice
        cada pedido y no habria forma de cuadrar la pantalla contra nada.
        """
        self.venta(total='10000.00', comision='1500.00')
        self.venta(total='5000.00', comision='800.00')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('15000.00'))
        self.assertEqual(contexto['total_general'], Decimal('15000.00'))
        # Y el desglose por canal tampoco lo toca.
        canal = roman['canales'][0]
        self.assertEqual(canal['total'], Decimal('15000.00'))
        self.assertEqual(canal['comision'], Decimal('2300.00'))

    def test_pedido_cancelado_no_aporta_ni_total_ni_comision(self):
        """El filtro es el mismo para los dos numeros, o la resta no cierra."""
        self.venta(total='10000.00', comision='1500.00')
        self.venta(total='9999.00', comision='9999.00', estado='cancelled')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('10000.00'))
        self.assertEqual(roman['comision'], Decimal('1500.00'))
        self.assertEqual(roman['pedidos'], 1)


class TestComisionNull(BaseCaja):
    """PARTE 1: NULL no es 0, mismo criterio que el resto del modulo."""

    def test_comision_null_no_se_trata_como_cero(self):
        """El pedido sin comision cargada queda marcado, no asumido.

        Restar 0 seria afirmar "esta venta no pago comision". El dato real es
        "todavia nadie la cargo", y son cosas distintas: el saldo que se ve
        esta por ARRIBA del que va a quedar, y eso tiene que verse.
        """
        self.venta(total='10000.00', comision=None)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['comision'], Decimal('0.00'))
        self.assertEqual(roman['sin_comision'], 1)
        self.assertEqual(contexto['sin_comision_total'], 1)

        # Y el faltante se ve en la pantalla, no solo en el contexto.
        respuesta, _ = self.reporte()
        texto = respuesta.get_data(as_text=True)
        self.assertIn('sin cargar', texto)
        self.assertIn(u'Pedidos sin la comisión de plataforma cargada', texto)

    def test_comision_cero_explicito_no_es_un_faltante(self):
        """Una venta de mostrador no paga comision, y eso es un dato cargado."""
        self.venta(total='10000.00', comision='0.00')

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['comision'], Decimal('0.00'))
        self.assertEqual(roman['sin_comision'], 0)
        self.assertEqual(roman['saldo_real'], Decimal('10000.00'))

    def test_el_cargado_resta_aunque_haya_otro_sin_cargar(self):
        """El faltante de uno no frena la resta del otro.

        La pantalla no descarta el pedido incompleto -- su facturacion es real
        y ya entro --: resta lo que sabe y avisa por lo que no. Es lo contrario
        del reporte de margen, que ahi si tira el pedido entero afuera, y la
        diferencia es a proposito: alla el numero seria falso, aca es parcial y
        se dice que lo es.
        """
        self.venta(total='10000.00', comision='1500.00')
        self.venta(total='4000.00', comision=None)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')

        self.assertEqual(roman['total'], Decimal('14000.00'))
        self.assertEqual(roman['comision'], Decimal('1500.00'))
        self.assertEqual(roman['sin_comision'], 1)
        self.assertEqual(roman['saldo_real'], Decimal('12500.00'))

    def test_sin_ventas_no_hay_faltante(self):
        """Nachi no vendio nada: no le falta ninguna comision por cargar."""
        _, contexto = self.reporte()
        nachi = self.socio(contexto, 'nachi')

        self.assertEqual(nachi['sin_comision'], 0)
        self.assertEqual(nachi['comision'], Decimal('0.00'))


class TestComisionYAtribucion(BaseCaja):
    """La comision viaja con el pedido, igual que su facturacion."""

    def test_la_comision_sigue_al_pedido_reasignado(self):
        """Se le reasigno la venta a Nachi: la mordida tambien es de el.

        Si la comision se quedara en el canal, Nachi se llevaria la
        facturacion entera sin el costo de haberla hecho, y a Roman le
        restarian una comision de una venta que no cobro.
        """
        self.venta(total='10000.00', comision='1500.00',
                   override=self.id_cuenta_nachi)

        _, contexto = self.reporte()
        roman = self.socio(contexto, 'roman')
        nachi = self.socio(contexto, 'nachi')

        self.assertEqual(roman['total'], Decimal('0.00'))
        self.assertEqual(roman['comision'], Decimal('0.00'))
        self.assertEqual(nachi['total'], Decimal('10000.00'))
        self.assertEqual(nachi['comision'], Decimal('1500.00'))
        self.assertEqual(nachi['saldo_real'], Decimal('8500.00'))

    def test_la_comision_de_roman_no_le_baja_el_saldo_a_nachi(self):
        self.venta(total='10000.00', comision='1500.00')

        _, contexto = self.reporte()
        nachi = self.socio(contexto, 'nachi')

        self.assertEqual(nachi['comision'], Decimal('0.00'))
        self.assertEqual(nachi['saldo_real'], Decimal('0.00'))

    def test_el_total_general_suma_las_comisiones_de_todos(self):
        self.venta(canal_id=self.id_canal_roman, total='10000.00',
                   comision='1500.00')
        self.venta(canal_id=self.id_canal_nachi, total='6000.00',
                   comision='900.00')

        _, contexto = self.reporte()

        self.assertEqual(contexto['comision_total'], Decimal('2400.00'))
        self.assertEqual(contexto['total_general'], Decimal('16000.00'))
        self.assertEqual(contexto['saldo_real_total'], Decimal('13600.00'))


class TestPantalla(BaseCaja):
    """PARTE 2: la resta se ve, no solo su resultado."""

    def test_la_comision_es_una_linea_propia(self):
        """Entre el facturado y el gastado, con su monto escrito.

        Que solo bajara el numero final obligaria a rehacer la cuenta a mano
        para entender por que bajo -- que es exactamente lo que esta pantalla
        existe para evitar.
        """
        self.venta(total='10000.00', comision='1500.00')
        self.gasto(monto='3000.00')

        respuesta, _ = self.reporte()
        texto = respuesta.get_data(as_text=True)

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(u'Comisión de plataforma', texto)
        self.assertIn('1500.00', texto)   # la comision
        self.assertIn('10000.00', texto)  # el bruto, que no cambio
        self.assertIn('3000.00', texto)   # el gasto
        self.assertIn('5500.00', texto)   # lo que queda

        # Y en ese orden: primero el bruto, despues la mordida, despues el
        # gasto, y el saldo al final.
        self.assertLess(texto.index('10000.00'), texto.index('1500.00'))
        self.assertLess(texto.index('1500.00'), texto.index('3000.00'))
        self.assertLess(texto.index('3000.00'), texto.index('5500.00'))


class TestAuth(BaseCaja):
    """La pantalla sigue detras de @login_required."""

    def test_caja_socio_pide_login(self):
        respuesta = request_anonimo(self.ctx, 'get', '/reportes/caja-socio')
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers.get('Location', ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
