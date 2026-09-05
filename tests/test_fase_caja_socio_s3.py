# -*- coding: utf-8 -*-
"""Tests de FASE-CAJA-SOCIO-S3 (elegir la cuenta al CARGAR la venta manual).

    python -m unittest discover -s tests -v

S2 dejo `pedido.cuenta_cobro_override_id` editable desde /pedidos/listar: se
cargaba la venta y despues se iba a corregir a quien se le atribuia. S3 mueve
esa decision al momento en que se sabe -- el alta -- sin sacar la correccion
posterior, que sigue haciendo falta para las ventas de canales externos y para
las manuales donde se eligio mal.

No hay campo nuevo ni migracion: es el MISMO `cuenta_cobro_override_id`, la
misma lectura (`_leer_cuenta_override`) y la misma regla de atribucion. Lo
unico que cambia es que ahora se puede setear en dos lugares.

Lo que se prueba:

    el default no cambio nada     -> cargar sin tocar el selector deja el
                                     override en NULL y la venta en Roman
    elegir Nachi en el alta       -> queda seteado desde el primer commit, sin
                                     pasar por /pedidos/listar
    la caja lo ve sin correccion  -> el reporte por socio ya la cuenta bien
    medio != cuenta               -> "efectivo" y "Nachi" son dos datos
                                     independientes y se guardan los dos
    no se cruza de empresa        -> una cuenta ajena voltea la venta entera
                                     en vez de guardarla mal atribuida
    S2 sigue vivo                 -> el selector del listado no se toco

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.ayuda_auth import request_anonimo  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    CuentaCobro,
    Empresa,
    Pago,
    Pedido,
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


class BaseAltaConCuenta(unittest.TestCase):
    """El reparto real: el canal manual cobra en la cuenta de Roman.

    Es la premisa de toda la slice -- si el canal manual no cayera en Roman,
    elegir Nachi en el alta no corregiria nada.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test CAJA-SOCIO-S3')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='cajasocio3@test.local',
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
        db.session.flush()

        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial',
                                       activo=True,
                                       cuenta_cobro_id=self.cuenta_roman.id)
        db.session.add(self.canal_manual)

        self.producto = Producto(empresa_id=self.empresa.id, sku='MART-500',
                                 nombre='Martillo 500g',
                                 costo_unitario=Decimal('1200.00'),
                                 precio_lista=Decimal('2500.00'))
        db.session.add(self.producto)
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.id_cuenta_roman = self.cuenta_roman.id
        self.id_cuenta_nachi = self.cuenta_nachi.id
        self.id_canal_manual = self.canal_manual.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def cargar(self, cuenta=None, medio='efectivo', precio='5000.00',
               cantidad='1', sku='MART-500', seguir=True):
        """POST al alta de venta manual.

        `cuenta` None significa "no mando el campo" -- el navegador siempre lo
        manda, pero un formulario viejo o un cliente cualquiera puede no
        hacerlo, y ese caso tiene que caer en el mismo default que el vacio.
        """
        datos = {
            'sku': [sku],
            'cantidad': [cantidad],
            'precio_unitario': [precio],
            'fecha': date.today().isoformat(),
            'medio': medio,
            'nota': '',
        }
        if cuenta is not None:
            datos['cuenta_cobro_override'] = cuenta
        return self.client.post('/pedidos/manual/nuevo', data=datos,
                                follow_redirects=seguir)

    def unico_pedido(self):
        pedidos = Pedido.query.all()
        self.assertEqual(len(pedidos), 1, 'se esperaba exactamente un pedido')
        return pedidos[0]

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
        self.fail('el socio %r no aparece en el reporte' % (clave,))


class TestElDefaultNoCambioNada(BaseAltaConCuenta):
    """Lo que carga Roman todos los dias. Si esto se rompe, la slice rompio el
    alta que ya funcionaba."""

    def test_venta_manual_por_canal_default(self):
        """No tocar el selector -> override NULL y la venta es de Roman."""
        self.cargar(cuenta='')

        pedido = self.unico_pedido()
        self.assertIsNone(pedido.cuenta_cobro_override_id,
                          'vacio es "por canal", no una cuenta elegida')
        self.assertEqual(pedido.cuenta_cobro_efectiva.id, self.id_cuenta_roman)
        self.assertEqual(pedido.canal_id, self.id_canal_manual)

    def test_sin_mandar_el_campo_es_lo_mismo_que_vacio(self):
        """Un POST que ni menciona la cuenta carga la venta igual."""
        self.cargar(cuenta=None)

        pedido = self.unico_pedido()
        self.assertIsNone(pedido.cuenta_cobro_override_id)
        self.assertEqual(pedido.cuenta_cobro_efectiva.id, self.id_cuenta_roman)

    def test_el_alta_sigue_guardando_todo_lo_de_siempre(self):
        """El selector nuevo no se llevo puesto nada del resto de la venta."""
        self.cargar(cuenta='', precio='2500.00', cantidad='2')

        pedido = self.unico_pedido()
        self.assertEqual(pedido.total, Decimal('5000.00'))
        self.assertEqual(len(pedido.items), 1)
        self.assertEqual(pedido.items[0].cantidad, 2)
        self.assertEqual(Pago.query.filter_by(pedido_id=pedido.id).count(), 1)

    def test_el_stock_se_sigue_descontando(self):
        """La otra escritura del alta, que tampoco tiene que ver con la cuenta."""
        self.producto.stock = 10
        db.session.commit()

        self.cargar(cuenta=self.id_cuenta_nachi, cantidad='3')

        self.assertEqual(db.session.get(Producto, self.producto.id).stock, 7)


class TestElegirLaCuentaAlCargar(BaseAltaConCuenta):
    """El motivo de la slice: no tener que volver a corregir."""

    def test_venta_manual_elige_nachi_al_cargar(self):
        """Queda seteado desde el primer commit, sin pasar por el listado."""
        self.cargar(cuenta=str(self.id_cuenta_nachi))

        pedido = self.unico_pedido()
        self.assertEqual(pedido.cuenta_cobro_override_id, self.id_cuenta_nachi)
        self.assertEqual(pedido.cuenta_cobro_efectiva.id, self.id_cuenta_nachi)

    def test_elegir_la_cuenta_no_cambia_el_canal(self):
        """La venta siguio entrando por el mostrador. Cambia a quien se le
        cuenta la plata, no por donde entro."""
        self.cargar(cuenta=str(self.id_cuenta_nachi))

        self.assertEqual(self.unico_pedido().canal_id, self.id_canal_manual)

    def test_elegir_roman_explicitamente_tambien_se_guarda(self):
        """Elegir la que ya tocaba deja de ser NULL: quien lo eligio lo dijo, y
        el dia que cambie la cuenta del canal esta venta no se muda sola."""
        self.cargar(cuenta=str(self.id_cuenta_roman))

        pedido = self.unico_pedido()
        self.assertEqual(pedido.cuenta_cobro_override_id, self.id_cuenta_roman)
        self.assertEqual(pedido.cuenta_cobro_efectiva.id, self.id_cuenta_roman)

    def test_el_formulario_ofrece_las_cuentas(self):
        cuerpo = self.client.get('/pedidos/manual/nuevo').get_data(as_text=True)
        self.assertIn('cuenta_cobro_override', cuerpo)
        self.assertIn('¿A qué cuenta entra esta venta?', cuerpo)
        self.assertIn('value="%d"' % self.id_cuenta_nachi, cuerpo)
        self.assertIn('Nachi', cuerpo)

    def test_el_default_del_formulario_dice_de_quien_es_el_canal(self):
        """"Por canal (Roman)": elegir otra tiene que ser consciente."""
        cuerpo = self.client.get('/pedidos/manual/nuevo').get_data(as_text=True)
        self.assertIn('Por canal (Roman)', cuerpo)

    def test_el_formulario_no_ofrece_cuentas_de_otra_empresa(self):
        otra = Empresa(nombre='Ferreteria Ajena')
        db.session.add(otra)
        db.session.flush()
        db.session.add(CuentaCobro(empresa_id=otra.id, nombre='Cuenta ajena SA',
                                   tipo='mercadopago', socio='nachi'))
        db.session.commit()

        cuerpo = self.client.get('/pedidos/manual/nuevo').get_data(as_text=True)
        self.assertNotIn('Cuenta ajena SA', cuerpo)


class TestLaCajaLoVeSinCorreccion(BaseAltaConCuenta):
    """El punto entero: que el reporte ya este bien la primera vez."""

    def test_caja_socio_ve_lo_elegido_al_cargar(self):
        """Sin ningun POST a /pedidos/cuentas de por medio."""
        self.cargar(cuenta=str(self.id_cuenta_nachi), precio='84627.70')

        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'nachi')['total'],
                         Decimal('84627.70'))
        self.assertEqual(self.socio(contexto, 'nachi')['pedidos'], 1)
        self.assertEqual(self.socio(contexto, 'roman')['total'], Decimal('0.00'))

    def test_dos_ventas_del_mismo_dia_se_parten_segun_lo_elegido(self):
        """El caso real: se carga una tanda y cada una va a quien la cobro."""
        self.cargar(cuenta='', precio='97500.00')
        self.cargar(cuenta=str(self.id_cuenta_nachi), precio='84627.70')

        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'roman')['total'],
                         Decimal('97500.00'))
        self.assertEqual(self.socio(contexto, 'nachi')['total'],
                         Decimal('84627.70'))
        self.assertEqual(contexto['total_general'], Decimal('182127.70'),
                         'elegir la cuenta no crea ni borra plata')

    def test_la_venta_elegida_conserva_de_que_canal_vino(self):
        """Aparece debajo de Nachi, pero diciendo que entro por el mostrador."""
        self.cargar(cuenta=str(self.id_cuenta_nachi), precio='84627.70')

        _, contexto = self.reporte()
        canales = self.socio(contexto, 'nachi')['canales']
        self.assertEqual(len(canales), 1)
        self.assertIn('manual', canales[0]['nombre'].lower())
        self.assertTrue(canales[0]['corregido'],
                        'la linea se marca igual que si se hubiera corregido '
                        'despues: es el mismo campo')


class TestMedioYCuentaSonDosCosas(BaseAltaConCuenta):
    """Conviven en el mismo formulario y no se pisan."""

    def test_medio_cobro_no_se_confunde_con_cuenta(self):
        """Efectivo + Nachi: los dos campos, los dos correctos."""
        self.cargar(cuenta=str(self.id_cuenta_nachi), medio='efectivo')

        pedido = self.unico_pedido()
        pago = Pago.query.filter_by(pedido_id=pedido.id).one()
        self.assertEqual(pago.metodo, 'efectivo')
        self.assertEqual(pedido.cuenta_cobro_override_id, self.id_cuenta_nachi)

    def test_cambiar_el_medio_no_cambia_la_cuenta(self):
        """Tarjeta y efectivo con la misma cuenta elegida dan la misma cuenta."""
        self.cargar(cuenta=str(self.id_cuenta_nachi), medio='tarjeta')

        pedido = self.unico_pedido()
        pago = Pago.query.filter_by(pedido_id=pedido.id).one()
        self.assertEqual(pago.metodo, 'tarjeta')
        self.assertEqual(pedido.cuenta_cobro_override_id, self.id_cuenta_nachi)

    def test_la_cuenta_elegida_no_toca_el_medio(self):
        """Y al reves: elegir cuenta no escribe ningun medio de cobro."""
        self.cargar(cuenta=str(self.id_cuenta_nachi), medio='mercado_pago')

        pago = Pago.query.filter_by(pedido_id=self.unico_pedido().id).one()
        self.assertEqual(pago.metodo, 'mercado_pago')

    def test_sin_medio_no_se_guarda_nada(self):
        """La validacion vieja sigue siendo la primera: elegir cuenta no
        alcanza para cargar una venta sin medio de cobro."""
        self.cargar(cuenta=str(self.id_cuenta_nachi), medio='')

        self.assertEqual(Pedido.query.count(), 0)


class TestNoSeCruzaDeEmpresa(BaseAltaConCuenta):
    """Lo que llega es una cadena que mando el cliente; el <select> no es
    ninguna garantia."""

    def test_una_cuenta_de_otra_empresa_no_carga_la_venta(self):
        otra = Empresa(nombre='Ferreteria Ajena')
        db.session.add(otra)
        db.session.flush()
        ajena = CuentaCobro(empresa_id=otra.id, nombre='Cuenta ajena',
                            tipo='mercadopago', socio='nachi')
        db.session.add(ajena)
        db.session.commit()

        self.cargar(cuenta=str(ajena.id))

        self.assertEqual(Pedido.query.count(), 0,
                         'antes que guardar la venta atribuida a cualquiera, '
                         'no se guarda')

    def test_una_cuenta_que_no_es_un_numero_no_carga_la_venta(self):
        self.cargar(cuenta='cualquier cosa')

        self.assertEqual(Pedido.query.count(), 0)

    def test_la_cuenta_invalida_no_descuenta_stock(self):
        """Se valida antes de escribir nada: la venta no existio a medias."""
        self.producto.stock = 10
        db.session.commit()

        self.cargar(cuenta='cualquier cosa')

        self.assertEqual(db.session.get(Producto, self.producto.id).stock, 10)

    def test_el_formulario_vuelve_con_lo_elegido(self):
        """Un error en otro campo no puede hacer perder la cuenta elegida."""
        respuesta = self.cargar(cuenta=str(self.id_cuenta_nachi), medio='')
        cuerpo = respuesta.get_data(as_text=True)

        self.assertIn('value="%d" selected' % self.id_cuenta_nachi, cuerpo)

    def test_pide_sesion(self):
        request_anonimo(self.ctx, 'post', '/pedidos/manual/nuevo', data={
            'sku': ['MART-500'], 'cantidad': ['1'],
            'precio_unitario': ['2500.00'], 'medio': 'efectivo',
            'cuenta_cobro_override': str(self.id_cuenta_nachi)})
        self.assertEqual(Pedido.query.count(), 0)


class TestElListadoSigueCorrigiendo(BaseAltaConCuenta):
    """S3 agrega un lugar donde elegir; no saca el de S2."""

    def test_el_selector_del_listado_sigue_existiendo(self):
        self.cargar(cuenta='')

        cuerpo = self.client.get('/pedidos/listar').get_data(as_text=True)
        self.assertIn('form-cuentas', cuerpo)
        self.assertIn('cuenta_cobro_override', cuerpo)

    def test_se_puede_corregir_lo_elegido_en_el_alta(self):
        """Elegir mal en el alta se arregla donde se arreglaba antes."""
        self.cargar(cuenta=str(self.id_cuenta_nachi))
        pedido_id = self.unico_pedido().id

        self.client.post('/pedidos/cuentas', data={
            'pedido_id': str(pedido_id),
            'cuenta_cobro_override': '',
        }, follow_redirects=True)

        self.assertIsNone(db.session.get(Pedido, pedido_id)
                          .cuenta_cobro_override_id)


if __name__ == '__main__':
    unittest.main(verbosity=2)
