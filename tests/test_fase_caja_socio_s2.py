# -*- coding: utf-8 -*-
"""Tests de FASE-CAJA-SOCIO-S2 (corregir a que cuenta va un pedido puntual).

    python -m unittest discover -s tests -v

La slice agrega UNA excepcion: `pedido.cuenta_cobro_override_id`. La regla
general no se toca -- cada canal cobra donde dice `canal_venta.cuenta_cobro_id`
y la venta manual sigue cayendo en la cuenta de Roman -- y por eso el primer
bloque de esta suite es el que prueba que NO cambio nada.

El caso que la trajo: tres ventas manuales cargadas con montos agregados
entraron todas por el canal manual (siempre Roman), y una de ellas -- "ventas
Meli", $84.627,70 -- en la realidad la cobro Nachi. Sin el override habria que
elegir entre dos mentiras: cambiarle el canal al pedido, o cambiarle la cuenta
al canal manual y mover TODAS las presenciales de socio de una sola vez.

Lo que se prueba:

    sin override no cambia nada    -> el 99% de las filas sigue atribuyendose
                                      por canal, como siempre
    con override cambia de socio   -> y solo ese pedido, no el canal entero
    el canal no se pierde          -> la venta corregida aparece bajo el otro
                                      socio pero diciendo por donde entro
    los numeros reales             -> con los $84.627,70 de "ventas Meli",
                                      Roman baja exactamente eso y Nachi sube
                                      exactamente eso
    se edita en el listado         -> guardado en tanda, todo o nada, con el
                                      mismo criterio que las comisiones
    no se cruza de empresa         -> un id de cuenta ajena no entra por el
                                      formulario

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.ayuda_auth import request_anonimo  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    CuentaCobro,
    Empresa,
    Pedido,
    Usuario,
    db,
)

ENGINE_PRODUCTIVO = None

# Los tres montos reales que Roman cargo a mano, con el nombre con el que los
# cargo. Van como constantes para que el test se lea como el caso y no como
# tres numeros sueltos.
VENTAS_MELI = Decimal('84627.70')
VENTAS_PRESENCIALES = Decimal('97500.00')
SORTEO = Decimal('4.00')


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


class BaseOverride(unittest.TestCase):
    """El mismo reparto real que FASE-CAJA-SOCIO-S1, para poder comparar.

    Dos cuentas (Roman y Nachi), tres canales: Tiendanube y manual cobrando en
    la de Roman, Mercado Libre apagado cobrando en la de Nachi.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test CAJA-SOCIO-S2')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='cajasocio2@test.local',
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

        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True,
                                   id_tienda_externo='9999',
                                   cuenta_cobro_id=self.cuenta_roman.id)
        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial',
                                       activo=True,
                                       cuenta_cobro_id=self.cuenta_roman.id)
        self.canal_meli = CanalVenta(empresa_id=self.empresa.id,
                                     tipo='mercadolibre', nombre='Mercado Libre',
                                     activo=False,
                                     cuenta_cobro_id=self.cuenta_nachi.id)
        db.session.add_all([self.canal_tn, self.canal_manual, self.canal_meli])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.id_cuenta_roman = self.cuenta_roman.id
        self.id_cuenta_nachi = self.cuenta_nachi.id
        self.id_canal_tn = self.canal_tn.id
        self.id_canal_manual = self.canal_manual.id
        self.id_canal_meli = self.canal_meli.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def pedido(self, canal_id, total, estado='open', comprador='Cliente',
               nota=None, override=None):
        fila = Pedido(empresa_id=self.empresa_id, canal_id=canal_id,
                      fecha_pedido=datetime(2026, 9, 1, 12, 0),
                      estado=estado, comprador_nombre=comprador, nota=nota,
                      total=Decimal(total), cuenta_cobro_override_id=override)
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
        self.fail('el socio %r no aparece en el reporte' % (clave,))

    def lineas(self, contexto, clave):
        """{(nombre del canal, corregido): total} de un socio.

        La clave lleva el flag porque el mismo canal puede aparecer dos veces
        debajo de socios distintos: una por la regla y otra por la correccion.
        """
        return {(canal['nombre'], canal['corregido']): canal['total']
                for canal in self.socio(contexto, clave)['canales']}


class TestSinOverrideNoCambioNada(BaseOverride):
    """El 99% de las filas. Si esto se rompe, la slice rompio la regla."""

    def test_pedido_sin_override_usa_cuenta_del_canal(self):
        """Lo de siempre: el pedido va a la cuenta que dice su canal."""
        self.pedido(self.id_canal_tn, '13696.90')
        self.pedido(self.id_canal_manual, '5000.00')

        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'roman')['total'],
                         Decimal('18696.90'))
        self.assertEqual(self.socio(contexto, 'roman')['pedidos'], 2)
        self.assertEqual(self.socio(contexto, 'nachi')['total'], Decimal('0.00'))
        self.assertEqual(contexto['total_general'], Decimal('18696.90'))

    def test_el_override_nace_en_null(self):
        """Ningun alta lo escribe: es una correccion, no un campo del pedido."""
        fila = self.pedido(self.id_canal_manual, '5000.00')
        self.assertIsNone(fila.cuenta_cobro_override_id)

    def test_ninguna_linea_del_desglose_sale_marcada(self):
        """Sin correcciones, nada dice "reasignado": no hay nada que aclarar."""
        self.pedido(self.id_canal_tn, '13696.90')
        self.pedido(self.id_canal_manual, '5000.00')

        _, contexto = self.reporte()
        marcadas = [canal for fila in contexto['socios']
                    for canal in fila['canales'] if canal['corregido']]
        self.assertEqual(marcadas, [])

    def test_la_cuenta_efectiva_es_la_del_canal(self):
        """La regla, leida desde el modelo. Es la misma que aplica el reporte."""
        fila = self.pedido(self.id_canal_manual, '5000.00')
        self.assertEqual(fila.cuenta_cobro_efectiva.id, self.id_cuenta_roman)


class TestConOverride(BaseOverride):
    """La excepcion: ese pedido, y ninguno mas."""

    def test_pedido_con_override_va_a_la_otra_cuenta(self):
        """Dos ventas por el mismo canal, una corregida. Se parten los totales."""
        self.pedido(self.id_canal_manual, '5000.00')
        self.pedido(self.id_canal_manual, '3000.00',
                    override=self.id_cuenta_nachi)

        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'roman')['total'], Decimal('5000.00'))
        self.assertEqual(self.socio(contexto, 'roman')['pedidos'], 1)
        self.assertEqual(self.socio(contexto, 'nachi')['total'], Decimal('3000.00'))
        self.assertEqual(self.socio(contexto, 'nachi')['pedidos'], 1)
        self.assertEqual(contexto['total_general'], Decimal('8000.00'),
                         'corregir no puede crear ni borrar plata')

    def test_el_canal_del_pedido_no_se_toca(self):
        """Se corrige a quien se le atribuye, no por donde entro la venta."""
        fila = self.pedido(self.id_canal_manual, '3000.00',
                           override=self.id_cuenta_nachi)
        self.assertEqual(fila.canal_id, self.id_canal_manual)
        self.assertEqual(fila.cuenta_cobro_efectiva.id, self.id_cuenta_nachi)

    def test_la_linea_corregida_conserva_de_que_canal_vino(self):
        """Aparece debajo de Nachi, pero sigue diciendo "venta manual"."""
        self.pedido(self.id_canal_manual, '5000.00')
        self.pedido(self.id_canal_manual, '3000.00',
                    override=self.id_cuenta_nachi)

        _, contexto = self.reporte()
        self.assertEqual(
            self.lineas(contexto, 'nachi'),
            {('Mercado Libre', False): Decimal('0.00'),
             ('Venta manual / presencial', True): Decimal('3000.00')})
        self.assertEqual(
            self.lineas(contexto, 'roman'),
            {('Korvo', False): Decimal('0.00'),
             ('Venta manual / presencial', False): Decimal('5000.00')})

    def test_la_linea_corregida_muestra_la_cuenta_a_la_que_fue(self):
        self.pedido(self.id_canal_manual, '3000.00',
                    override=self.id_cuenta_nachi)

        respuesta, contexto = self.reporte()
        corregida = [canal for canal in self.socio(contexto, 'nachi')['canales']
                     if canal['corregido']][0]
        self.assertEqual(corregida['cuenta'], 'Nachi - Mercado Libre')
        self.assertIn('reasignado a mano', respuesta.get_data(as_text=True))

    def test_el_canal_entero_no_se_muda(self):
        """La regla del canal sigue siendo la de antes, no la de la excepcion."""
        self.pedido(self.id_canal_manual, '3000.00',
                    override=self.id_cuenta_nachi)
        canal = db.session.get(CanalVenta, self.id_canal_manual)
        self.assertEqual(canal.cuenta_cobro_id, self.id_cuenta_roman)

        # Una venta manual nueva, cargada despues de la correccion, sigue
        # cayendo en Roman: se corrigio un pedido, no el canal.
        self.pedido(self.id_canal_manual, '1000.00')
        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'roman')['total'], Decimal('1000.00'))

    def test_override_a_la_misma_cuenta_del_canal_no_abre_una_linea_aparte(self):
        """No es una correccion: es lo mismo escrito dos veces."""
        self.pedido(self.id_canal_manual, '5000.00')
        self.pedido(self.id_canal_manual, '3000.00',
                    override=self.id_cuenta_roman)

        _, contexto = self.reporte()
        self.assertEqual(self.lineas(contexto, 'roman'),
                         {('Korvo', False): Decimal('0.00'),
                          ('Venta manual / presencial', False): Decimal('8000.00')})

    def test_el_cancelado_corregido_sigue_sin_sumar(self):
        """El override no lo resucita: sigue siendo una venta que no fue."""
        self.pedido(self.id_canal_manual, '99999.00', estado='cancelled',
                    override=self.id_cuenta_nachi)

        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'nachi')['total'], Decimal('0.00'))
        self.assertEqual(contexto['total_general'], Decimal('0.00'))


class TestElCasoReal(BaseOverride):
    """Los tres montos que Roman cargo a mano, con los numeros de verdad."""

    def setUp(self):
        super(TestElCasoReal, self).setUp()
        # Las tres entraron por el canal manual, que atribuye a Roman.
        self.meli = self.pedido(self.id_canal_manual, VENTAS_MELI,
                                nota='ventas Meli')
        self.presenciales = self.pedido(self.id_canal_manual,
                                        VENTAS_PRESENCIALES,
                                        nota='Ventas Presenciales')
        self.sorteo = self.pedido(self.id_canal_manual, SORTEO, nota='Sorteo')
        self.id_meli = self.meli.id
        self.id_presenciales = self.presenciales.id
        self.id_sorteo = self.sorteo.id

    def test_antes_de_corregir_todo_es_de_roman(self):
        """El punto de partida, que es el problema.

            ventas Meli            84627.70
            Ventas Presenciales    97500.00
            Sorteo                     4.00
                                  ---------
                                  182131.70   todo atribuido a Roman
        """
        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'roman')['total'],
                         Decimal('182131.70'))
        self.assertEqual(self.socio(contexto, 'roman')['pedidos'], 3)
        self.assertEqual(self.socio(contexto, 'nachi')['total'], Decimal('0.00'))

    def test_corregir_ventas_meli_real(self):
        """Roman baja EXACTAMENTE los $84.627,70 y Nachi los sube.

            Roman antes   182131.70
            ventas Meli   -84627.70
                          ---------
            Roman despues  97504.00   (presenciales + sorteo)
            Nachi despues  84627.70
        """
        _, antes = self.reporte()
        roman_antes = self.socio(antes, 'roman')['total']
        nachi_antes = self.socio(antes, 'nachi')['total']

        pedido = db.session.get(Pedido, self.id_meli)
        pedido.cuenta_cobro_override_id = self.id_cuenta_nachi
        db.session.commit()

        _, despues = self.reporte()
        roman_despues = self.socio(despues, 'roman')['total']
        nachi_despues = self.socio(despues, 'nachi')['total']

        self.assertEqual(roman_despues, Decimal('97504.00'))
        self.assertEqual(nachi_despues, VENTAS_MELI)
        self.assertEqual(roman_antes - roman_despues, VENTAS_MELI,
                         'Roman tiene que bajar exactamente ese monto')
        self.assertEqual(nachi_despues - nachi_antes, VENTAS_MELI,
                         'y Nachi subirlo exactamente')
        self.assertEqual(despues['total_general'], Decimal('182131.70'),
                         'el total de la empresa no se mueve')

    def test_los_pedidos_tambien_se_reparten(self):
        """No solo la plata: el contador que la respalda tambien."""
        pedido = db.session.get(Pedido, self.id_meli)
        pedido.cuenta_cobro_override_id = self.id_cuenta_nachi
        db.session.commit()

        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'roman')['pedidos'], 2)
        self.assertEqual(self.socio(contexto, 'nachi')['pedidos'], 1)
        self.assertEqual(contexto['pedidos_totales'], 3)

    def test_las_otras_dos_no_se_mueven(self):
        """Corregir una no toca a las que estaban bien."""
        pedido = db.session.get(Pedido, self.id_meli)
        pedido.cuenta_cobro_override_id = self.id_cuenta_nachi
        db.session.commit()

        self.assertIsNone(db.session.get(Pedido, self.id_presenciales)
                          .cuenta_cobro_override_id)
        self.assertIsNone(db.session.get(Pedido, self.id_sorteo)
                          .cuenta_cobro_override_id)

        _, contexto = self.reporte()
        self.assertEqual(self.lineas(contexto, 'roman'),
                         {('Korvo', False): Decimal('0.00'),
                          ('Venta manual / presencial', False):
                              VENTAS_PRESENCIALES + SORTEO})

    def test_corregirla_desde_la_pantalla_da_el_mismo_numero(self):
        """El caso real de punta a punta, por donde Roman lo va a hacer."""
        self.client.post('/pedidos/cuentas', data={
            'pedido_id': [str(self.id_meli), str(self.id_presenciales),
                          str(self.id_sorteo)],
            'cuenta_cobro_override': [str(self.id_cuenta_nachi), '', ''],
        }, follow_redirects=True)

        _, contexto = self.reporte()
        self.assertEqual(self.socio(contexto, 'roman')['total'],
                         Decimal('97504.00'))
        self.assertEqual(self.socio(contexto, 'nachi')['total'], VENTAS_MELI)


class TestDondeSeEdita(BaseOverride):
    """El guardado en tanda del listado. Mismo criterio que las comisiones."""

    def test_el_listado_ofrece_las_cuentas(self):
        self.pedido(self.id_canal_manual, '5000.00')
        respuesta = self.client.get('/pedidos/listar')
        cuerpo = respuesta.get_data(as_text=True)
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('cuenta_cobro_override', cuerpo)
        self.assertIn('Nachi', cuerpo)
        # La opcion vacia dice cual seria la cuenta si nadie corrige nada.
        self.assertIn('Por canal', cuerpo)

    def test_guardar_reasigna(self):
        fila = self.pedido(self.id_canal_manual, '3000.00')
        respuesta = self.client.post('/pedidos/cuentas', data={
            'pedido_id': str(fila.id),
            'cuenta_cobro_override': str(self.id_cuenta_nachi),
        }, follow_redirects=True)

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(db.session.get(Pedido, fila.id).cuenta_cobro_override_id,
                         self.id_cuenta_nachi)

    def test_el_vacio_saca_la_correccion(self):
        """Volver a "por canal" es como se deshace, igual que la comision."""
        fila = self.pedido(self.id_canal_manual, '3000.00',
                           override=self.id_cuenta_nachi)
        self.client.post('/pedidos/cuentas', data={
            'pedido_id': str(fila.id),
            'cuenta_cobro_override': '',
        }, follow_redirects=True)

        self.assertIsNone(db.session.get(Pedido, fila.id).cuenta_cobro_override_id)

    def test_guarda_varias_en_una_tanda(self):
        una = self.pedido(self.id_canal_manual, '3000.00')
        otra = self.pedido(self.id_canal_manual, '4000.00')
        self.client.post('/pedidos/cuentas', data={
            'pedido_id': [str(una.id), str(otra.id)],
            'cuenta_cobro_override': [str(self.id_cuenta_nachi), ''],
        }, follow_redirects=True)

        self.assertEqual(db.session.get(Pedido, una.id).cuenta_cobro_override_id,
                         self.id_cuenta_nachi)
        self.assertIsNone(db.session.get(Pedido, otra.id).cuenta_cobro_override_id)

    def test_una_fila_mala_no_guarda_ninguna(self):
        """Todo o nada, igual que las comisiones: un exito parcial miente."""
        una = self.pedido(self.id_canal_manual, '3000.00')
        otra = self.pedido(self.id_canal_manual, '4000.00')
        respuesta = self.client.post('/pedidos/cuentas', data={
            'pedido_id': [str(una.id), str(otra.id)],
            'cuenta_cobro_override': [str(self.id_cuenta_nachi), 'cualquier cosa'],
        }, follow_redirects=True)

        self.assertIn('no es una cuenta valida', respuesta.get_data(as_text=True))
        self.assertIsNone(db.session.get(Pedido, una.id).cuenta_cobro_override_id,
                          'la buena tampoco se guarda')
        self.assertIsNone(db.session.get(Pedido, otra.id).cuenta_cobro_override_id)

    def test_no_se_puede_reasignar_a_una_cuenta_de_otra_empresa(self):
        """Lo que llega es una cadena del cliente; el <select> no garantiza nada."""
        otra_empresa = Empresa(nombre='Otra Empresa')
        db.session.add(otra_empresa)
        db.session.flush()
        cuenta_ajena = CuentaCobro(empresa_id=otra_empresa.id, nombre='MP Ajena',
                                   tipo='mercadopago', socio='nachi')
        db.session.add(cuenta_ajena)
        db.session.commit()
        id_ajena = cuenta_ajena.id

        fila = self.pedido(self.id_canal_manual, '3000.00')
        respuesta = self.client.post('/pedidos/cuentas', data={
            'pedido_id': str(fila.id),
            'cuenta_cobro_override': str(id_ajena),
        }, follow_redirects=True)

        self.assertIn('no es de esta empresa', respuesta.get_data(as_text=True))
        self.assertIsNone(db.session.get(Pedido, fila.id).cuenta_cobro_override_id)

    def test_no_se_puede_reasignar_un_pedido_de_otra_empresa(self):
        otra_empresa = Empresa(nombre='Otra Empresa 2')
        db.session.add(otra_empresa)
        db.session.flush()
        canal_ajeno = CanalVenta(empresa_id=otra_empresa.id, tipo='manual',
                                 nombre='Manual ajeno', activo=True)
        db.session.add(canal_ajeno)
        db.session.commit()
        ajeno = Pedido(empresa_id=otra_empresa.id, canal_id=canal_ajeno.id,
                       fecha_pedido=datetime(2026, 9, 1, 12, 0), estado='open',
                       total=Decimal('999.00'))
        db.session.add(ajeno)
        db.session.commit()
        id_ajeno = ajeno.id

        self.client.post('/pedidos/cuentas', data={
            'pedido_id': str(id_ajeno),
            'cuenta_cobro_override': str(self.id_cuenta_nachi),
        }, follow_redirects=True)

        self.assertIsNone(db.session.get(Pedido, id_ajeno).cuenta_cobro_override_id)

    def test_guardar_cuentas_no_toca_la_comision(self):
        """Son dos correcciones distintas sobre el mismo pedido."""
        fila = self.pedido(self.id_canal_manual, '3000.00')
        fila.comision_plataforma = Decimal('150.00')
        db.session.commit()

        self.client.post('/pedidos/cuentas', data={
            'pedido_id': str(fila.id),
            'cuenta_cobro_override': str(self.id_cuenta_nachi),
        }, follow_redirects=True)

        guardado = db.session.get(Pedido, fila.id)
        self.assertEqual(guardado.comision_plataforma, Decimal('150.00'))
        self.assertEqual(guardado.cuenta_cobro_override_id, self.id_cuenta_nachi)

    def test_guardar_comisiones_no_toca_la_cuenta(self):
        """Y al reves: el formulario de comisiones no manda el override."""
        fila = self.pedido(self.id_canal_manual, '3000.00',
                           override=self.id_cuenta_nachi)

        self.client.post('/pedidos/comisiones', data={
            'pedido_id': str(fila.id),
            'comision_plataforma': '150.00',
        }, follow_redirects=True)

        guardado = db.session.get(Pedido, fila.id)
        self.assertEqual(guardado.comision_plataforma, Decimal('150.00'))
        self.assertEqual(guardado.cuenta_cobro_override_id, self.id_cuenta_nachi)

    def test_pide_sesion(self):
        respuesta = request_anonimo(self.ctx, 'post', '/pedidos/cuentas')
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers.get('Location', ''))


class TestElAltaManualNoPregunta(BaseOverride):
    """La constraint de la slice: el formulario de venta nueva no cambio."""

    def test_el_alta_no_ofrece_elegir_cuenta(self):
        respuesta = self.client.get('/pedidos/manual/nuevo')
        cuerpo = respuesta.get_data(as_text=True)
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotIn('cuenta_cobro_override', cuerpo,
                         'el alta sigue siendo automatica: la cuenta la pone '
                         'el canal, y corregirla es otra pantalla')


if __name__ == '__main__':
    unittest.main(verbosity=2)
