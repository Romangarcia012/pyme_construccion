# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S3-MARGEN-FIX (el flete de la venta de mostrador).

    python -m unittest discover -s tests -v

`costo_envio_vendedor` es nullable para poder decir "no se sabe" cuando el
payload de un canal no trae el dato. La venta de mostrador no tiene ese
problema: se entrega en persona, no hay flete, y eso ya estaba dicho para el
otro monto del envio -- `_armar_venta` fijaba `total_envio = 0.00` a mano. El
costo del vendedor se habia quedado afuera de ese mismo criterio.

La consecuencia no se veia hasta que existio el reporte de margen: como exige
los tres componentes de costo cargados, toda venta presencial caia en "Sin
margen" para siempre, y no habia pantalla desde donde arreglarlo -- ese campo
lo escribe el sync de Tiendanube, por donde una venta de mostrador nunca pasa.

Lo que se prueba:

    venta manual              -> costo_envio_vendedor 0.00, no NULL
    venta manual + comision   -> ya entra al reporte, con su margen calculado
    NULL sigue significando   -> el fix es sobre el dato de origen, no sobre
    lo mismo                     el criterio del reporte

El ultimo es el que impide que este fix se "arregle" mañana del lado
equivocado: si alguien hiciera que el reporte tratara el NULL como cero, el
pedido de Tiendanube al que le falta el flete pasaria a mostrar un margen
inflado sin que nada lo delate.

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
    Pedido,
    PedidoItem,
    Producto,
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


class BaseMostrador(unittest.TestCase):
    """Una empresa con el canal manual, el de Tiendanube y un producto."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test MARGEN-FIX')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='margenfix@test.local',
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
        return self.client.post('/pedidos/manual/nuevo', data={
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


class TestElFleteDeLaVentaManual(BaseMostrador):
    """Cero de verdad, no dato faltante."""

    def test_venta_manual_costo_envio_vendedor_en_cero(self):
        self.vender()
        pedido = self.unico_pedido()

        self.assertIsNotNone(pedido.costo_envio_vendedor,
                             'el flete de una venta de mostrador es 0, no "no se sabe"')
        self.assertEqual(pedido.costo_envio_vendedor, Decimal('0.00'))

    def test_los_dos_montos_del_envio_dicen_lo_mismo(self):
        """`total_envio` ya estaba en 0.00: el del vendedor lo acompaña.

        Son las dos caras del mismo envio que no existe -- lo que pago el
        comprador y lo que le costo a la tienda -- y no hay ninguna venta
        presencial donde uno sea cero y el otro un misterio.
        """
        self.vender()
        pedido = self.unico_pedido()
        self.assertEqual(pedido.total_envio, Decimal('0.00'))
        self.assertEqual(pedido.costo_envio_vendedor, Decimal('0.00'))

    def test_la_comision_de_plataforma_sigue_naciendo_en_NULL(self):
        """El fix es sobre el flete y solo sobre el flete.

        La comision del canal es harina de otro costal: una venta de mostrador
        no paga ninguna, pero eso lo confirma Roman cargando el 0 desde el
        listado, igual que en cualquier otro pedido. Ponerlo solo aca seria
        adivinar por el.
        """
        self.vender()
        self.assertIsNone(self.unico_pedido().comision_plataforma)


class TestLaVentaManualEntraAlReporte(BaseMostrador):
    """La consecuencia visible del fix, verificada contra la pantalla."""

    def test_venta_manual_ahora_es_completa_para_el_reporte(self):
        self.vender(cantidad=2, precio='2500.00')

        # Lo unico que queda por cargar a mano es la comision, que tiene
        # pantalla: se carga en 0 porque el mostrador no paga ninguna.
        pedido = self.unico_pedido()
        pedido.comision_plataforma = Decimal('0.00')
        db.session.commit()

        respuesta, contexto = self.reporte()
        self.assertEqual(respuesta.status_code, 200)

        # Ya no cae en "Sin margen"...
        self.assertEqual(contexto['incompletos'], [])

        # ...y el margen que muestra es el que da la cuenta: 5000.00 de
        # ingreso menos 2400.00 de costo congelado (2 x 1200.00).
        self.assertEqual(len(contexto['productos']), 1)
        fila = contexto['productos'][0]
        self.assertEqual(fila['nombre'], 'Martillo 500g')
        self.assertEqual(fila['pedidos'], 1)
        self.assertEqual(fila['ingreso_neto'], Decimal('5000.00'))
        self.assertEqual(fila['costo_total'], Decimal('2400.00'))
        self.assertEqual(fila['ganancia'], Decimal('2600.00'))
        self.assertEqual(fila['margen_pct'], Decimal('52.0'))
        # Sin envio de por medio, las dos columnas de margen coinciden.
        self.assertEqual(fila['margen_mercaderia_pct'], Decimal('52.0'))

    def test_sin_el_fix_la_venta_manual_quedaba_afuera(self):
        """El caso que este fix arregla, reproducido volviendo el campo a NULL.

        No es un test del pasado: es el que explica por que el 0.00 tiene que
        escribirse en el origen. Con el campo en NULL el reporte hace lo
        correcto -- se niega a inventar el flete -- y la venta desaparece de
        todas las sumas.
        """
        self.vender()
        pedido = self.unico_pedido()
        pedido.comision_plataforma = Decimal('0.00')
        pedido.costo_envio_vendedor = None
        db.session.commit()

        _, contexto = self.reporte()

        self.assertEqual(len(contexto['incompletos']), 1)
        faltan = contexto['incompletos'][0]['faltan']
        self.assertEqual(len(faltan), 1)
        self.assertIn('envío', faltan[0]['que'])
        self.assertEqual(contexto['productos'][0]['pedidos'], 0)


class TestElCriterioDelReporteNoCambio(BaseMostrador):
    """NULL sigue siendo "no se sabe" para todos los demas."""

    def test_un_pedido_de_tiendanube_sin_flete_sigue_sin_margen(self):
        """Lo que se corrigio es el dato de origen, no la regla que lo lee.

        Si este test se pusiera rojo querria decir que alguien "arreglo" el
        reporte tratando el NULL como cero, y entonces cada pedido al que le
        falta el flete mostraria un margen inflado sin nada que lo delate.
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
                        comision_plataforma=Decimal('248.59'),
                        costo_envio_vendedor=None)
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
        self.assertIn('envío', contexto['incompletos'][0]['faltan'][0]['que'])


if __name__ == '__main__':
    unittest.main()
