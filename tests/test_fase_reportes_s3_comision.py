# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S3-COMISION (carga manual de la comision del canal).

    python -m unittest discover -s tests -v

Lo que la plataforma se queda por vender es la ultima pieza que falta para el
margen, y es la unica que NO se puede sincronizar: se verifico en
FASE-REPORTES-S3 que el payload de Tiendanube no la trae como linea aparte.
Asi que `pedido.comision_plataforma` nace vacia y solo se llena a mano desde el
listado de ventas.

Lo que se prueba:

    Decimal valido            -> se persiste con dos decimales
    negativo / texto / NaN    -> no guarda nada Y no rompe la pantalla
    vacio                     -> vuelve a NULL (borrar una comision mal cargada)
    NULL vs 0                 -> son cosas distintas y se leen distinto
    editar un pedido          -> no toca la comision de los demas
    una fila mala             -> tampoco se guardan las buenas de la misma tanda
    pedido de otra empresa    -> se ignora, no escribe nada ajeno
    sin sugerencia            -> el input arranca y se queda vacio

El de NULL vs 0 es el que protege al reporte que todavia no existe: si un dia
alguien trata "no la cargue" como "no hubo comision", el margen de cada pedido
sin cargar sale inflado y nadie lo va a notar mirando la pantalla.

`pago.comision` es otra cosa -- la mordida del procesador de pagos -- y hay un
test que verifica que esta ruta no la toca.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    Empresa,
    Pago,
    Pedido,
    Usuario,
    db,
)
from tests.ayuda_auth import request_anonimo  # noqa: E402

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


class BaseComision(unittest.TestCase):
    """Una empresa con tres pedidos y otra empresa con el suyo.

        TN-1   Tiendanube, comision NULL          -> cargar sobre vacio
        TN-2   Tiendanube, comision NULL          -> el vecino que no se toca
        MOSTR  manual, comision ya cargada en 0   -> editar sobre algo

    El pedido de la segunda empresa esta para probar que un id ajeno que entre
    por el formulario no se escribe.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-REPORTES-S3-COMISION')
        self.otra_empresa = Empresa(nombre='Empresa Ajena')
        db.session.add_all([self.empresa, self.otra_empresa])
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='fasecomision@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True, id_tienda_externo='9999')
        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial', activo=True)
        self.canal_ajeno = CanalVenta(empresa_id=self.otra_empresa.id, tipo='tiendanube',
                                      nombre='Tienda ajena', activo=True)
        db.session.add_all([self.canal_tn, self.canal_manual, self.canal_ajeno])
        db.session.flush()

        self.tn1 = Pedido(empresa_id=self.empresa.id, canal_id=self.canal_tn.id,
                          id_externo='2060210312', numero_externo='1041',
                          fecha_pedido=datetime(2026, 8, 20, 10, 0), estado='open',
                          moneda='ARS', total=Decimal('7490.00'),
                          comision_plataforma=None)
        self.tn2 = Pedido(empresa_id=self.empresa.id, canal_id=self.canal_tn.id,
                          id_externo='2060210313', numero_externo='1042',
                          fecha_pedido=datetime(2026, 8, 21, 11, 0), estado='open',
                          moneda='ARS', total=Decimal('14980.00'),
                          comision_plataforma=None)
        # Nace con 0 explicito: la venta de mostrador no paga comision de canal,
        # y eso es un dato sabido, no un dato faltante.
        self.mostrador = Pedido(empresa_id=self.empresa.id, canal_id=self.canal_manual.id,
                                fecha_pedido=datetime(2026, 8, 22, 12, 0),
                                estado='completado', moneda='ARS',
                                total=Decimal('25000.00'),
                                comision_plataforma=Decimal('0.00'))
        self.ajeno = Pedido(empresa_id=self.otra_empresa.id, canal_id=self.canal_ajeno.id,
                            id_externo='999', numero_externo='777',
                            fecha_pedido=datetime(2026, 8, 20, 10, 0), estado='open',
                            moneda='ARS', total=Decimal('1000.00'),
                            comision_plataforma=None)
        db.session.add_all([self.tn1, self.tn2, self.mostrador, self.ajeno])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.tn1_id = self.tn1.id
        self.tn2_id = self.tn2.id
        self.mostrador_id = self.mostrador.id
        self.ajeno_id = self.ajeno.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def guardar(self, pares, **extra):
        """POST al formulario de comisiones. `pares`: [(pedido_id, texto)]."""
        datos = {
            'pedido_id': [str(pedido_id) for pedido_id, _ in pares],
            'comision_plataforma': [texto for _, texto in pares],
        }
        datos.update(extra)
        return self.client.post('/pedidos/comisiones', data=datos,
                                follow_redirects=True)

    def comision_de(self, pedido_id):
        return db.session.get(Pedido, pedido_id).comision_plataforma

    def listado(self):
        return self.client.get('/pedidos/listar').get_data(as_text=True)

    def texto(self, respuesta):
        return respuesta.get_data(as_text=True)


class TestGuardarComisionValida(BaseComision):
    """El caso central: se tipea un numero y queda guardado."""

    def test_guardar_comision_valida(self):
        self.guardar([(self.tn1_id, '748.50')])
        self.assertEqual(self.comision_de(self.tn1_id), Decimal('748.50'))

    def test_el_post_termina_bien_y_avisa(self):
        respuesta = self.guardar([(self.tn1_id, '748.50')])
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('Se actualizo la comision de 1 pedido', self.texto(respuesta))

    def test_se_guarda_como_decimal_no_como_float(self):
        self.guardar([(self.tn1_id, '748.50')])
        self.assertIsInstance(self.comision_de(self.tn1_id), Decimal)

    def test_se_redondea_a_dos_decimales(self):
        self.guardar([(self.tn1_id, '748.567')])
        self.assertEqual(self.comision_de(self.tn1_id), Decimal('748.57'))

    def test_la_coma_decimal_se_acepta(self):
        """Es lo que tipea cualquiera en Argentina."""
        self.guardar([(self.tn1_id, '748,50')])
        self.assertEqual(self.comision_de(self.tn1_id), Decimal('748.50'))

    def test_varios_pedidos_en_una_sola_tanda(self):
        self.guardar([(self.tn1_id, '748.50'), (self.tn2_id, '1497.00')])
        self.assertEqual(self.comision_de(self.tn1_id), Decimal('748.50'))
        self.assertEqual(self.comision_de(self.tn2_id), Decimal('1497.00'))

    def test_el_input_vacio_vuelve_la_comision_a_null(self):
        """Asi se saca una comision mal cargada: se borra el input, no se pone 0."""
        self.guardar([(self.mostrador_id, '')])
        self.assertIsNone(self.comision_de(self.mostrador_id))

    def test_la_comision_guardada_se_ve_al_recargar(self):
        self.guardar([(self.tn1_id, '748.50')])
        self.assertIn('value="748.50"', self.listado())

    def test_no_toca_la_comision_del_procesador_de_pagos(self):
        """`pago.comision` es otra mordida y se carga por otro camino."""
        db.session.add(Pago(pedido_id=self.tn1_id, monto_bruto=Decimal('7490.00'),
                            monto_neto=Decimal('7490.00'), comision=None,
                            estado='pendiente', metodo='tarjeta',
                            procesador='manual', moneda='ARS',
                            fecha_pago=datetime(2026, 8, 20, 10, 0)))
        db.session.commit()
        self.guardar([(self.tn1_id, '748.50')])
        pago = Pago.query.filter_by(pedido_id=self.tn1_id).one()
        self.assertIsNone(pago.comision)
        self.assertEqual(self.comision_de(self.tn1_id), Decimal('748.50'))


class TestRechazaComisionInvalida(BaseComision):
    """Un input malo no puede guardar basura ni voltear la pantalla."""

    def test_rechaza_comision_invalida(self):
        for texto in ('-1', '-748.50', 'abc', '12abc', '1.2.3', '$748',
                      'NaN', 'Infinity'):
            with self.subTest(texto=texto):
                respuesta = self.guardar([(self.tn1_id, texto)])
                self.assertEqual(respuesta.status_code, 200)
                self.assertIsNone(self.comision_de(self.tn1_id),
                                  'se guardo basura con %r' % texto)

    def test_el_error_se_explica_con_el_numero_del_pedido(self):
        respuesta = self.texto(self.guardar([(self.tn1_id, '-5')]))
        self.assertIn('no puede ser negativa', respuesta)
        self.assertIn('#1041', respuesta)

    def test_el_pedido_sin_numero_externo_se_nombra_por_fecha(self):
        """La venta de mostrador no tiene numero de canal que mostrar."""
        respuesta = self.texto(self.guardar([(self.mostrador_id, 'abc')]))
        self.assertIn('22/08/2026', respuesta)

    def test_el_texto_avisa_que_no_es_un_numero(self):
        self.assertIn('no es un numero',
                      self.texto(self.guardar([(self.tn1_id, 'abc')])))

    def test_una_fila_mala_no_deja_guardar_las_buenas(self):
        """Todo o nada: si no, la pantalla mostraria un exito a medias."""
        self.guardar([(self.tn1_id, '748.50'), (self.tn2_id, '-1')])
        self.assertIsNone(self.comision_de(self.tn1_id))
        self.assertIsNone(self.comision_de(self.tn2_id))

    def test_un_valor_malo_no_pisa_la_comision_que_ya_estaba(self):
        self.guardar([(self.mostrador_id, 'abc')])
        self.assertEqual(self.comision_de(self.mostrador_id), Decimal('0.00'))

    def test_un_pedido_de_otra_empresa_se_ignora(self):
        """El id llega como texto del cliente: el filtro por empresa es lo
        unico que impide escribir en las ventas de otro."""
        respuesta = self.guardar([(self.ajeno_id, '999.00')])
        self.assertEqual(respuesta.status_code, 200)
        self.assertIsNone(db.session.get(Pedido, self.ajeno_id).comision_plataforma)

    def test_un_id_inexistente_no_rompe(self):
        respuesta = self.guardar([(999999, '100.00')])
        self.assertEqual(respuesta.status_code, 200)

    def test_un_id_que_no_es_numero_no_rompe(self):
        respuesta = self.guardar([('abc', '100.00')])
        self.assertEqual(respuesta.status_code, 200)

    def test_sin_ninguna_fila_avisa_que_no_hubo_cambios(self):
        self.assertIn('No hubo cambios', self.texto(self.guardar([])))


class TestComisionNullDistintaDeCero(BaseComision):
    """NULL y 0 no son lo mismo, y esta es la prueba que lo fija.

    0 afirma "esta venta no pago comision" -- cierto para la de mostrador.
    NULL dice "todavia no la cargue". Un reporte de margen que los confunda
    muestra ganancia de mas en cada pedido sin cargar, y no hay nada en la
    pantalla que lo delate.
    """

    def test_comision_null_distinta_de_cero(self):
        self.guardar([(self.tn1_id, '0'), (self.tn2_id, '')])
        self.assertEqual(self.comision_de(self.tn1_id), Decimal('0.00'))
        self.assertIsNone(self.comision_de(self.tn2_id))
        self.assertNotEqual(self.comision_de(self.tn1_id),
                            self.comision_de(self.tn2_id))

    def test_el_cero_explicito_se_persiste_y_no_se_convierte_en_null(self):
        self.guardar([(self.tn1_id, '0')])
        self.assertIsNotNone(self.comision_de(self.tn1_id))
        self.assertEqual(self.comision_de(self.tn1_id), Decimal('0.00'))

    def test_el_cero_con_coma_tambien_es_cero_y_no_null(self):
        self.guardar([(self.tn1_id, '0,00')])
        self.assertIsNotNone(self.comision_de(self.tn1_id))

    def test_los_espacios_solos_son_vacio_no_cero(self):
        self.guardar([(self.mostrador_id, '   ')])
        self.assertIsNone(self.comision_de(self.mostrador_id))

    def test_se_leen_distinto_en_la_pantalla(self):
        """El cero se ve escrito; el NULL deja el input en blanco."""
        self.guardar([(self.tn1_id, '0'), (self.tn2_id, '')])
        html = self.listado()
        self.assertIn('value="0.00"', html)
        # El pedido sin cargar no puede aparecer con un numero adentro: seria
        # un cero inventado que el primer submit terminaria guardando.
        self.assertIn('value=""', html)

    def test_pasar_de_cero_a_vacio_cuenta_como_cambio(self):
        """Si el cero se tratara como falsy, borrarlo pareceria no-cambio y la
        pantalla diria 'no hubo cambios' habiendo cambiado."""
        respuesta = self.guardar([(self.mostrador_id, '')])
        self.assertIn('Se actualizo la comision de 1 pedido', self.texto(respuesta))
        self.assertIsNone(self.comision_de(self.mostrador_id))


class TestEditarComisionNoAfectaOtrosPedidos(BaseComision):
    """Cargar una comision no puede derramar sobre las ventas vecinas."""

    def test_editar_comision_no_afecta_otros_pedidos(self):
        self.guardar([(self.tn1_id, '748.50'), (self.tn2_id, ''),
                      (self.mostrador_id, '0')])
        self.assertEqual(self.comision_de(self.tn1_id), Decimal('748.50'))
        self.assertIsNone(self.comision_de(self.tn2_id))
        self.assertEqual(self.comision_de(self.mostrador_id), Decimal('0.00'))

    def test_una_fila_sola_no_borra_las_que_no_vinieron(self):
        """El formulario manda todas las filas, pero si llega una sola las
        ausentes tienen que quedar como estaban, no volver a NULL."""
        self.guardar([(self.tn1_id, '748.50')])
        self.guardar([(self.tn2_id, '1497.00')])
        self.assertEqual(self.comision_de(self.tn1_id), Decimal('748.50'))
        self.assertEqual(self.comision_de(self.tn2_id), Decimal('1497.00'))
        self.assertEqual(self.comision_de(self.mostrador_id), Decimal('0.00'))

    def test_reeditar_pisa_solo_el_pedido_editado(self):
        self.guardar([(self.tn1_id, '748.50'), (self.tn2_id, '1497.00')])
        self.guardar([(self.tn1_id, '800.00')])
        self.assertEqual(self.comision_de(self.tn1_id), Decimal('800.00'))
        self.assertEqual(self.comision_de(self.tn2_id), Decimal('1497.00'))

    def test_no_toca_los_otros_montos_del_pedido(self):
        """La comision es una columna nueva; el total y el envio no se mueven."""
        self.guardar([(self.tn1_id, '748.50')])
        pedido = db.session.get(Pedido, self.tn1_id)
        self.assertEqual(pedido.total, Decimal('7490.00'))
        self.assertIsNone(pedido.costo_envio_vendedor)


class TestPantallaDeComisiones(BaseComision):
    """La columna nueva convive con las siete que ya estaban."""

    def test_la_columna_aparece_en_el_listado(self):
        html = self.listado()
        self.assertIn('comision_plataforma', html)
        self.assertIn('plataforma', html)

    def test_las_siete_columnas_anteriores_siguen(self):
        html = self.listado()
        for encabezado in ('Fecha', 'Canal', 'Cliente', 'Total',
                           'Medio de cobro', 'Estado', 'Despacho'):
            with self.subTest(encabezado=encabezado):
                self.assertIn('>%s</th>' % encabezado, html)

    def test_sin_comision_cargada_el_input_arranca_vacio(self):
        """No hay sugerencia posible: el canal no manda el dato en el pedido.
        Un input prellenado se guardaria solo al primer submit."""
        html = self.listado()
        self.assertIn('name="comision_plataforma"', html)
        self.assertIn('value=""', html)

    def test_cada_fila_manda_su_id(self):
        html = self.listado()
        for pedido_id in (self.tn1_id, self.tn2_id, self.mostrador_id):
            with self.subTest(pedido_id=pedido_id):
                self.assertIn('name="pedido_id" value="%d"' % pedido_id, html)

    def test_el_boton_de_guardar_esta(self):
        self.assertIn('Guardar comisiones', self.listado())

    def test_el_boton_de_venta_nueva_sigue_estando(self):
        """La pantalla fusionada de FASE-REPORTES-S2-MERGE no pierde nada."""
        self.assertIn('Nueva venta manual', self.listado())


class TestAuthDeComisiones(BaseComision):
    """La ruta nueva no puede quedar abierta."""

    def test_el_post_sin_sesion_redirige_al_login(self):
        respuesta = request_anonimo(
            self.ctx, 'post', '/pedidos/comisiones',
            data={'pedido_id': [str(self.tn1_id)],
                  'comision_plataforma': ['999.00']})
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers.get('Location', ''))

    def test_el_post_sin_sesion_no_escribe_nada(self):
        request_anonimo(self.ctx, 'post', '/pedidos/comisiones',
                        data={'pedido_id': [str(self.tn1_id)],
                              'comision_plataforma': ['999.00']})
        self.assertIsNone(self.comision_de(self.tn1_id))


if __name__ == '__main__':
    unittest.main()
