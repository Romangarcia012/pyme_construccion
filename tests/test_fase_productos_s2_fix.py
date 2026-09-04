# -*- coding: utf-8 -*-
"""Tests de FASE-PRODUCTOS-S2-FIX (la comision de la venta de mostrador).

    python -m unittest discover -s tests -v

Mismo caso que FASE-REPORTES-S3-MARGEN-FIX, un campo mas alla. Ahi se fijo
`costo_envio_vendedor = 0.00` en la venta manual con el argumento de que el
flete de una entrega en persona es un cero real y no un dato faltante. La
comision de plataforma esta en la misma situacion y se habia quedado afuera:
en una venta de mostrador no hay Tiendanube ni Mercado Libre cobrandose nada,
porque no hubo plataforma.

La consecuencia era la misma: el reporte de margen exige `comision_plataforma`
no NULL para calcular, `_armar_venta` la dejaba NULL, y toda venta en efectivo
caia en "Sin margen: falta la comisión de plataforma" -- un cartel que pedia
cargar a mano un numero que estructuralmente no existe.

Lo que se prueba:

    venta manual          -> comision_plataforma 0.00, no NULL
    venta manual          -> ya entra sola al reporte de margen, sin que el
                             test le escriba ningun campo (el workaround que
                             FASE-PRODUCTOS-S2 necesito en su setUp)
    pago.comision         -> intacto: es otra mordida, de otro flujo
    NULL sigue            -> el fix es sobre el dato de origen, no sobre el
    significando lo          criterio del reporte
    mismo

Los dos ultimos son los que acotan el fix: si se pusieran rojos querria decir
que alguien lo extendio al procesador de pagos, o que "arreglo" el reporte
tratando el NULL como cero -- y ahi cada pedido de canal sin cargar mostraria
un margen inflado sin nada que lo delate.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    Empresa,
    Pago,
    Pedido,
    PedidoItem,
    Producto,
    Usuario,
    db,
)

ENGINE_PRODUCTIVO = None

RUTA_VENTA = '/pedidos/manual/nuevo'


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


class BaseMostrador(unittest.TestCase):
    """Una empresa con el canal manual, el de Tiendanube y un producto."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test PRODUCTOS-S2-FIX')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='productosfix@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial', activo=True)
        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True, id_tienda_externo='9999')
        db.session.add_all([self.canal_manual, self.canal_tn])

        self.martillo = Producto(empresa_id=self.empresa.id, sku='MART-500',
                                 nombre='Martillo 500g', stock=10,
                                 costo_unitario=Decimal('1200.00'),
                                 precio_lista=Decimal('2500.00'))
        db.session.add(self.martillo)
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.canal_tn_id = self.canal_tn.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def vender(self, sku='MART-500', cantidad=1, precio='2500.00', medio='efectivo'):
        """Una venta de mostrador, por el formulario de verdad.

        Pasa por la ruta y no por el modelo a proposito: lo que este fix
        cambia es lo que escribe `_armar_venta`, y armar el Pedido a mano en
        el test probaria el test.
        """
        return self.client.post(RUTA_VENTA, data={
            'sku': [sku],
            'cantidad': [str(cantidad)],
            'precio_unitario': [precio],
            'fecha': date.today().isoformat(),
            'medio': medio,
            'nota': '',
        }, follow_redirects=True)

    def unico_pedido(self):
        pedidos = Pedido.query.all()
        self.assertEqual(len(pedidos), 1, 'se esperaba exactamente un pedido')
        return pedidos[0]

    def reporte(self):
        """Lo que /reportes/margen le pasa a la plantilla."""
        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado['context'] = context

        template_rendered.connect(anotar, app)
        try:
            respuesta = self.client.get('/reportes/margen')
        finally:
            template_rendered.disconnect(anotar, app)

        return respuesta, capturado.get('context', {})


class TestLaComisionDeLaVentaManual(BaseMostrador):
    """Cero de verdad, no dato faltante."""

    def test_venta_manual_comision_plataforma_en_cero(self):
        self.vender()
        pedido = self.unico_pedido()

        self.assertIsNotNone(
            pedido.comision_plataforma,
            'una venta de mostrador no tiene plataforma que le cobre: es 0, '
            'no "no se sabe"')
        self.assertEqual(pedido.comision_plataforma, Decimal('0.00'))

    def test_los_tres_componentes_de_costo_del_pedido_nacen_cargados(self):
        """Envio cobrado, envio pagado y comision: los tres son 0 real.

        Es la lista completa de lo que `_faltantes()` le exige al PEDIDO. El
        componente que queda -- el costo de la mercaderia -- sigue saliendo
        del snapshot de la linea y puede faltar con razon, si Roman todavia no
        cargo el costo de ese producto.
        """
        self.vender()
        pedido = self.unico_pedido()
        self.assertEqual(pedido.total_envio, Decimal('0.00'))
        self.assertEqual(pedido.costo_envio_vendedor, Decimal('0.00'))
        self.assertEqual(pedido.comision_plataforma, Decimal('0.00'))

    def test_la_comision_del_procesador_no_se_toco(self):
        """`pago.comision` es otra mordida y sigue su propia regla.

        Son dos cosas distintas sobre la misma venta: lo que se queda la
        plataforma por vender y lo que se queda el procesador por cobrar. En
        efectivo la segunda ya era 0 porque no hay procesador; con tarjeta
        sigue en NULL hasta que la conciliacion traiga el numero real, y este
        fix no tiene nada que decir al respecto.
        """
        self.vender(medio='efectivo')
        self.assertEqual(Pago.query.one().comision, Decimal('0.00'))

    def test_con_tarjeta_la_del_procesador_sigue_en_NULL(self):
        """El medio de pago mueve `pago.comision`, no `comision_plataforma`."""
        self.vender(medio='tarjeta')

        self.assertIsNone(Pago.query.one().comision,
                          'con tarjeta la comision del procesador sigue sin saberse')
        self.assertEqual(self.unico_pedido().comision_plataforma, Decimal('0.00'),
                         'la de plataforma es 0 igual: no depende del medio de pago')


class TestLaVentaManualNaceCompleta(BaseMostrador):
    """La consecuencia visible: el reporte ya no necesita ayuda del test."""

    def test_venta_manual_ahora_completa_sin_setear_nada_en_el_test(self):
        """Ni una escritura a mano antes de mirar el reporte.

        FASE-PRODUCTOS-S2 tuvo que poner `comision_plataforma = 0.00` en su
        setUp para poder testear el agrupamiento: sin eso la venta caia en
        "faltan datos" y el test no llegaba a mirar lo que venia a mirar. Ese
        workaround describia el bug, no el disenio -- y este es el test que
        confirma que ya no hace falta.
        """
        self.vender(cantidad=2, precio='2500.00')

        respuesta, contexto = self.reporte()
        self.assertEqual(respuesta.status_code, 200)

        self.assertEqual(contexto['incompletos'], [],
                         'la venta nace con todo lo que el reporte exige')

        # 5000.00 de ingreso menos 2400.00 de costo congelado (2 x 1200.00).
        self.assertEqual(len(contexto['productos']), 1)
        fila = contexto['productos'][0]
        self.assertEqual(fila['nombre'], 'Martillo 500g')
        self.assertEqual(fila['pedidos'], 1)
        self.assertEqual(fila['ingreso_neto'], Decimal('5000.00'))
        self.assertEqual(fila['costo_total'], Decimal('2400.00'))
        self.assertEqual(fila['ganancia'], Decimal('2600.00'))
        self.assertEqual(fila['margen_pct'], Decimal('52.0'))

    def test_sin_el_fix_la_venta_manual_quedaba_afuera(self):
        """El caso que este fix arregla, reproducido volviendo el campo a NULL.

        Con la comision en NULL el reporte hace lo correcto -- se niega a
        inventarla -- y la venta desaparece de todas las sumas. Por eso el
        0.00 tiene que escribirse en el origen y no en el reporte.
        """
        self.vender()
        pedido = self.unico_pedido()
        pedido.comision_plataforma = None
        db.session.commit()

        _, contexto = self.reporte()

        self.assertEqual(len(contexto['incompletos']), 1)
        faltan = contexto['incompletos'][0]['faltan']
        self.assertEqual(len(faltan), 1)
        self.assertIn('comisión', faltan[0]['que'])
        self.assertEqual(contexto['productos'][0]['pedidos'], 0)


class TestElCriterioDelReporteNoCambio(BaseMostrador):
    """NULL sigue siendo "no se sabe" para todos los demas."""

    def test_un_pedido_de_tiendanube_sin_comision_sigue_sin_margen(self):
        """Lo que se corrigio es el dato de origen, no la regla que lo lee.

        Un pedido de canal al que le falta la comision tiene que seguir
        cayendo en "Sin margen" hasta que Roman la cargue: ahi el numero
        existe, solo que todavia no esta. Si este test se pusiera rojo querria
        decir que alguien "arreglo" el reporte tratando el NULL como cero.
        """
        pedido = Pedido(empresa_id=self.empresa_id, canal_id=self.canal_tn_id,
                        id_externo='1001', numero_externo='100',
                        fecha_pedido=datetime(2026, 8, 31, 17, 55, 12),
                        estado='closed',
                        total_bruto=Decimal('7490.00'),
                        total_descuentos=Decimal('0.00'),
                        total_envio=Decimal('7630.00'),
                        total_impuestos=Decimal('0.00'),
                        total=Decimal('15120.00'),
                        comision_plataforma=None,
                        costo_envio_vendedor=Decimal('7630.00'))
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, producto_id=self.martillo.id,
                                  descripcion='Martillo 500g', cantidad=1,
                                  precio_unitario=Decimal('7490.00'),
                                  descuento_unitario=Decimal('0.00'),
                                  costo_unitario_snapshot=Decimal('3994.18'),
                                  subtotal=Decimal('7490.00')))
        db.session.commit()

        _, contexto = self.reporte()

        self.assertEqual(len(contexto['incompletos']), 1)
        self.assertEqual(contexto['incompletos'][0]['etiqueta'], '#100')
        self.assertIn('comisión', contexto['incompletos'][0]['faltan'][0]['que'])


if __name__ == '__main__':
    unittest.main()
