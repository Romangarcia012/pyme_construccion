# -*- coding: utf-8 -*-
"""Tests de FASE-EVA-S6 ("Ganancia Neta" pasa a llamarse "Flujo de Caja").

    python -m unittest discover -s tests -v

QUE SE RENOMBRO Y POR QUE

FASE-EVA-S5 saco del dashboard Margen %, ROI % y EVA porque los tres comparaban
los GASTOS historicos contra las VENTAS historicas, y en un negocio que compra
mercaderia para stock esa resta no mide rentabilidad: buena parte de esos gastos
es plata que sigue en el deposito sin vender.

La tercera tarjeta de la fila que quedo -- "Ganancia Neta / Ganancia real" --
tiene exactamente el mismo mecanismo. Resta TODO lo gastado contra lo ya
vendido. Mientras se reinvierta lo que entra en mas stock ese numero da rojo
siempre, y una tarjeta que dice "Ganancia real" en rojo se lee como "estas
perdiendo plata" aunque el negocio facture y tenga el deposito lleno.

Esta slice NO saca la tarjeta: el numero es cierto y es util -- es la posicion
de caja. Lo que corrige es como se llama.

QUE NO CAMBIO, Y ES LA MITAD QUE IMPORTA

El calculo. `analisis['utilidad_neta']` sigue siendo
`(ingresos - gastos) - (ingresos - gastos) * tasa_impuestos` y sigue saliendo
del mismo `generar_analisis_completo`. Un rename que de paso mueve el numero es
un bug con cara de cosmetica, asi que `TestElNumeroNoCambio` congela los mismos
valores que S2, S3, S4 y S5 ya venian congelando.

Las otras dos tarjetas -- Ingresos por Ventas y Gastos Totales -- estaban bien
etiquetadas y no se tocaron. `TestLasOtrasDosNoSeTocaron` lo fija.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    Categoria,
    Empresa,
    Gasto,
    Ingreso,
    Pedido,
    Usuario,
    db,
)

ENGINE_PRODUCTIVO = None

# Los mismos numeros congelados de S2/S3/S4/S5. Que sean los mismos es el punto:
# si esta slice hubiera tocado la cuenta, estos tests se caen.
#
#     utilidad_bruta = 150000 - 120000    = 30000
#     impuestos      = 30000 * 0.30       =  9000
#     utilidad_neta  = 30000 - 9000       = 21000
VENTA = '150000.00'
GASTO = '120000.00'
CAPITAL = 300000.0
UTILIDAD_NETA = 21000.0

# El caso al reves: se compro mucho mas de lo que se vendio. Es el escenario
# real que motivo la slice -- reinvertir lo que entra en mas stock.
#
#     utilidad_bruta = 50000 - 200000     = -150000
#     impuestos      = -150000 * 0.30     =  -45000
#     utilidad_neta  = -150000 - (-45000) = -105000
VENTA_CHICA = '50000.00'
GASTO_GRANDE = '200000.00'
UTILIDAD_NETA_NEGATIVA = -105000.0


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


class BaseEvaS6(unittest.TestCase):
    """Korvo en chico. Mismo armado que S5, para hablar de la misma pantalla."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Korvo', capital_invertido=CAPITAL,
                               tasa_costo_capital=10.0, tasa_impuestos=0.30)
        db.session.add(self.empresa)
        db.session.flush()

        self.roman = Usuario(nombre='Roman', email='evas6@test.local',
                             empresa_id=self.empresa.id, verificado=True)
        self.roman.set_password('irrelevante')
        db.session.add(self.roman)
        db.session.commit()

        self.cat_gasto = Categoria(nombre='Compra de mercadería', tipo='gasto',
                                   empresa_id=self.empresa.id,
                                   usuario_id=self.roman.id)
        self.cat_ingreso = Categoria(nombre='Aporte de capital (socios)',
                                     tipo='ingreso',
                                     empresa_id=self.empresa.id,
                                     usuario_id=self.roman.id)
        db.session.add_all([self.cat_gasto, self.cat_ingreso])
        db.session.commit()

        self.canal = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                nombre='Venta manual', activo=True)
        db.session.add(self.canal)
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.roman_id = self.roman.id
        self.cat_gasto_id = self.cat_gasto.id
        self.cat_ingreso_id = self.cat_ingreso.id
        self.canal_id = self.canal.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def venta(self, total=VENTA):
        """Una venta real, en Pedido: es de donde salen los ingresos (S4).

        Fechada hace un anio exacto, mismo criterio que S2 y S5: con el periodo
        en 365 dias el prorrateo de S3 no mueve los numeros congelados con cada
        dia que pasa.
        """
        hace_un_anio = date.today() - timedelta(days=365)
        db.session.add(Pedido(empresa_id=self.empresa_id,
                              canal_id=self.canal_id,
                              fecha_pedido=datetime(hace_un_anio.year,
                                                    hace_un_anio.month,
                                                    hace_un_anio.day, 12, 0),
                              estado='open', comprador_nombre='Camila',
                              total=Decimal(total),
                              total_envio=Decimal('0.00'),
                              comision_plataforma=Decimal('0.00')))
        db.session.commit()

    def gasto(self, monto=GASTO):
        hace_un_anio = date.today() - timedelta(days=365)
        db.session.add(Gasto(descripcion='Compra de mercadería',
                             monto=Decimal(monto), fecha=hace_un_anio,
                             empresa_id=self.empresa_id,
                             usuario_id=self.roman_id,
                             categoria_id=self.cat_gasto_id))
        db.session.commit()

    def aporte(self, monto='999999.00'):
        """El aporte de los socios. NO es un ingreso del dashboard (S4)."""
        hace_un_anio = date.today() - timedelta(days=365)
        db.session.add(Ingreso(descripcion='Aporte de los socios',
                               monto=Decimal(monto), fecha=hace_un_anio,
                               empresa_id=self.empresa_id,
                               usuario_id=self.roman_id,
                               categoria_id=self.cat_ingreso_id))
        db.session.commit()

    def cargar_el_caso_positivo(self):
        """Se vendio mas de lo que se gasto: la tarjeta da verde."""
        self.venta()
        self.gasto()
        self.aporte()

    def cargar_el_caso_negativo(self):
        """Se compro mucho mas de lo que se vendio: el caso que motivo la slice."""
        self.venta(VENTA_CHICA)
        self.gasto(GASTO_GRANDE)
        self.aporte()

    # -- acceso al dashboard --------------------------------------------

    def _pedir_dashboard(self):
        """Devuelve (texto, analisis) de una sola visita a /dashboard.

        El pop/push del contexto es el guard que documenta tests/ayuda_auth.py.
        """
        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado.setdefault('ctx', context)

        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(self.roman_id)
                sesion['_fresh'] = True
            template_rendered.connect(anotar, app)
            try:
                resp = cli.get('/dashboard', follow_redirects=True)
            finally:
                template_rendered.disconnect(anotar, app)
        finally:
            self.ctx.push()

        return (resp.get_data(as_text=True),
                capturado.get('ctx', {}).get('analisis', {}))

    def texto(self):
        return self._pedir_dashboard()[0]

    def analisis(self):
        return self._pedir_dashboard()[1]

    def status_code(self):
        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(self.roman_id)
                sesion['_fresh'] = True
            return cli.get('/dashboard').status_code
        finally:
            self.ctx.push()


# =====================================================================
# PARTE 1 - El nombre que prometia rentabilidad se fue
# =====================================================================

class TestElNombreCambio(BaseEvaS6):

    def test_tarjeta_no_dice_ganancia_real(self):
        """La afirmacion de la slice, en el caso que daba verde."""
        self.cargar_el_caso_positivo()

        self.assertNotIn(
            'Ganancia real', self.texto(),
            'El subtitulo "Ganancia real" volvio al dashboard. Ese numero '
            'resta TODA la mercaderia comprada -- incluida la que sigue en el '
            'deposito -- contra lo ya vendido: es caja, no ganancia. El margen '
            'real esta en /reportes/margen.')

    def test_tarjeta_no_dice_ganancia_real_tampoco_en_negativo(self):
        """La misma afirmacion en la rama que de verdad se leia mal.

        Hacen falta los dos casos: el subtitulo cuelga de un `{% if %}`, y un
        solo escenario no encuentra al viejo si quedo colgado de la otra rama.
        """
        self.cargar_el_caso_negativo()

        self.assertNotIn('Ganancia real', self.texto())

    def test_el_titulo_tampoco_promete_ganancia(self):
        """"Ganancia Neta" era el titulo, y es la mitad que mas se lee."""
        self.cargar_el_caso_negativo()

        texto = self.texto()

        self.assertNotIn('Ganancia Neta', texto)
        self.assertIn('Flujo de Caja', texto)

    def test_el_subtitulo_dice_que_incluye_el_stock_sin_vender(self):
        """El nombre nuevo no alcanza si no dice que esta midiendo."""
        self.cargar_el_caso_positivo()

        texto = self.texto()

        self.assertIn('Ventas menos todo lo gastado', texto)
        self.assertIn('incluida mercadería sin vender', texto)

    def test_la_aclaracion_sale_cuando_el_numero_es_negativo(self):
        """El caso real: rojo por reinvertir en stock, no por perder plata."""
        self.cargar_el_caso_negativo()

        texto, analisis = self._pedir_dashboard()

        self.assertLess(analisis['utilidad_neta'], 0)
        self.assertIn('no significa', texto)
        self.assertIn('pérdida real', texto)
        self.assertIn('Margen y Ganancia', texto)

    def test_la_aclaracion_no_sale_cuando_el_numero_es_positivo(self):
        """En verde no hay nada que aclarar y la letra chica sobra.

        Contrapeso del test de arriba: sin este, una nota siempre visible
        pasaria los dos.
        """
        self.cargar_el_caso_positivo()

        texto, analisis = self._pedir_dashboard()

        self.assertGreater(analisis['utilidad_neta'], 0)
        self.assertNotIn('no significa', texto)
        self.assertNotIn('pérdida real', texto)

    def test_el_dashboard_responde_200_sin_datos(self):
        self.assertEqual(200, self.status_code())

    def test_el_dashboard_responde_200_en_positivo(self):
        self.cargar_el_caso_positivo()
        self.assertEqual(200, self.status_code())

    def test_el_dashboard_responde_200_en_negativo(self):
        """La rama nueva es esta: si el `url_for` de la nota estuviera mal, 500."""
        self.cargar_el_caso_negativo()
        self.assertEqual(200, self.status_code())


# =====================================================================
# PARTE 2 - El numero no cambio
# =====================================================================

class TestElNumeroNoCambio(BaseEvaS6):
    """La mitad que hace segura a la otra: se renombro, no se recalculo."""

    def test_calculo_no_cambia(self):
        """Los mismos valores congelados que S2, S3, S4 y S5.

            ingresos       = facturado neto de las VENTAS  = 150000  (S4)
            gastos         = SUM(Gasto.monto)              = 120000
            utilidad_bruta = 150000 - 120000               =  30000
            impuestos      = 30000 * 0.30                  =   9000
            utilidad_neta  = 30000 - 9000                  =  21000

        El aporte de 999999 tiene que estar cargado y NO puede ser el ingreso:
        sin esa fila, este test pasaria igual con el bug de S4 puesto de vuelta.
        """
        self.cargar_el_caso_positivo()

        texto, analisis = self._pedir_dashboard()

        self.assertEqual(150000.0, analisis['ingresos'])
        self.assertEqual(120000.0, analisis['gastos'])
        self.assertEqual(30000.0, analisis['utilidad_bruta'])
        self.assertEqual(9000.0, analisis['impuestos'])
        self.assertEqual(UTILIDAD_NETA, analisis['utilidad_neta'])

        # Y el mismo numero en la pantalla, bajo el titulo nuevo.
        self.assertIn('$21000', texto)

    def test_el_numero_negativo_tambien_es_el_de_siempre(self):
        """La rama que ahora lleva aclaracion muestra la misma cuenta de antes."""
        self.cargar_el_caso_negativo()

        texto, analisis = self._pedir_dashboard()

        self.assertEqual(50000.0, analisis['ingresos'])
        self.assertEqual(200000.0, analisis['gastos'])
        self.assertEqual(UTILIDAD_NETA_NEGATIVA, analisis['utilidad_neta'])
        self.assertIn('$-105000', texto)

    def test_el_color_negativo_sigue_siendo_rojo(self):
        """Lo que enganaba era el nombre, no el color: el semaforo queda igual."""
        self.cargar_el_caso_negativo()

        self.assertIn('class="card malo"', self.texto())

    def test_el_color_positivo_sigue_siendo_verde(self):
        self.cargar_el_caso_positivo()

        self.assertIn('class="card excelente"', self.texto())

    def test_el_estado_neutral_de_s2_sigue_intacto(self):
        """Una cuenta sin una sola fila no gana alarmas rojas ni letra chica."""
        texto, analisis = self._pedir_dashboard()

        self.assertFalse(analisis['hay_movimiento'])
        self.assertIn('class="card neutral"', texto)
        self.assertIn('Sin movimientos todavía', texto)
        self.assertNotIn('pérdida real', texto)


# =====================================================================
# PARTE 3 - Las otras dos tarjetas
# =====================================================================

class TestLasOtrasDosNoSeTocaron(BaseEvaS6):
    """Estaban bien etiquetadas. Renombrar la tercera no las arrastra."""

    def test_ingresos_por_ventas_y_gastos_totales_intactos(self):
        self.cargar_el_caso_positivo()

        texto = self.texto()

        self.assertIn('Ingresos por Ventas', texto)
        self.assertIn('Facturado neto, sin envío ni comisión', texto)
        self.assertIn('$150000', texto)

        self.assertIn('Gastos Totales', texto)
        self.assertIn('Dinero gastado', texto)
        self.assertIn('$120000', texto)

    def test_margen_roi_y_eva_siguen_fuera(self):
        """FASE-EVA-S5 no se deshizo de paso."""
        self.cargar_el_caso_positivo()

        texto = self.texto()

        for titulo in ('Margen %', 'ROI %', 'EVA - Valor Económico Agregado'):
            self.assertNotIn(titulo, texto)


if __name__ == '__main__':
    unittest.main(verbosity=2)
