# -*- coding: utf-8 -*-
"""Tests de FASE-EVA-S5 (margen, ROI y EVA salen del dashboard).

    python -m unittest discover -s tests -v

QUE SE SACO Y POR QUE

El dashboard mostraba seis numeros. Tres de ellos -- Margen %, ROI % y EVA --
se calculan comparando los GASTOS historicos contra las VENTAS historicas.

En un negocio que compra mercaderia para stock esa resta no es un margen. Buena
parte de esos gastos es plata que todavia esta en el deposito sin vender: la
cuenta describe el movimiento de caja y se estaba presentando con nombre de
rentabilidad. Con los numeros reales de Korvo daba -1054% de margen y -97% de
ROI sobre un negocio que factura y tiene stock.

El margen de verdad ya existe, bien calculado, en /reportes/margen: ese
descuenta el costo de lo efectivamente VENDIDO -- no el de lo comprado -- y
ademas lo abre por producto y por canal. Es la segunda vez que se revisan estos
indicadores y las dos veces la conclusion fue la misma: no aplican al tamano de
este negocio.

QUE SE QUEDO

Ingresos por Ventas, Gastos Totales y Ganancia Neta. Los tres dicen algo cierto
sin interpretacion: cuanta plata entro por ventas, cuanta salio, y la diferencia
despues de impuestos. No son rentabilidad y no se presentan como tal.

LO QUE NO SE BORRO, A PROPOSITO

`eva_utils.py` entero: la formula, el prorrateo de FASE-EVA-S3, el estado
neutral de FASE-EVA-S2, `Empresa.capital_invertido` y
`Empresa.tasa_costo_capital`. La conclusion "no aplica" es sobre el NEGOCIO, no
sobre el codigo, y el dia que Korvo separe compras de costo de lo vendido esa
cuenta esta entera y probada. `TestLaCuentaSigueViva` fija que siga estandolo:
sin ese test, la proxima persona que lea el dashboard borra eva_utils creyendo
que quedo muerto.

LAS DOS PRUEBAS QUE IMPORTAN, Y HACEN FALTA LAS DOS

  * `test_dashboard_no_muestra_margen_roi_eva` -- que se fueron.
  * `TestLosTresQueQuedan` -- que los otros tres no cambiaron ni de valor ni de
    calculo. Sacar tres tarjetas es facil si uno se lleva puesto de paso el
    numero de al lado.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eva_utils  # noqa: E402

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

# Los numeros congelados de la slice. Son los mismos que usan S2 y S3 para que
# las tres suites hablen de la misma cuenta:
#
#     utilidad_bruta = 150000 - 120000    = 30000
#     impuestos      = 30000 * 0.30       =  9000
#     utilidad_neta  = 30000 - 9000       = 21000
VENTA = '150000.00'
GASTO = '120000.00'
CAPITAL = 300000.0
UTILIDAD_NETA = 21000.0


class ConfigFalsaS5:
    """Los tres parametros del EVA sin tocar la base. Igual que en S2, S3 y S4."""

    def __init__(self, capital_invertido=0.0, tasa_costo_capital=10.0,
                 tasa_impuestos=0.30):
        self.capital_invertido = capital_invertido
        self.tasa_costo_capital = tasa_costo_capital
        self.tasa_impuestos = tasa_impuestos


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


class BaseEvaS5(unittest.TestCase):
    """Korvo en chico, con capital cargado para que el EVA SI se pueda calcular.

    El capital importa: con `capital_invertido` en 0 el ROI y el EVA son None y
    el dashboard nunca los mostraba igual (estado neutral de S2). Un test que
    afirma "estas tarjetas no estan" sobre una empresa sin capital pasaria
    tambien con el bug puesto de vuelta.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Korvo', capital_invertido=CAPITAL,
                               tasa_costo_capital=10.0, tasa_impuestos=0.30)
        db.session.add(self.empresa)
        db.session.flush()

        self.roman = Usuario(nombre='Roman', email='evas5@test.local',
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

    def venta(self, total=VENTA, envio='0.00', comision='0.00'):
        """Una venta real, en Pedido: es de donde salen los ingresos (S4).

        Fechada hace un anio exacto, mismo criterio que S2: con el periodo en
        365 dias el costo de capital prorrateado de S3 da identico al supuesto
        viejo, y los numeros congelados no se mueven solos con cada dia que
        pasa.
        """
        hace_un_anio = date.today() - timedelta(days=365)
        db.session.add(Pedido(empresa_id=self.empresa_id,
                              canal_id=self.canal_id,
                              fecha_pedido=datetime(hace_un_anio.year,
                                                    hace_un_anio.month,
                                                    hace_un_anio.day, 12, 0),
                              estado='open', comprador_nombre='Camila',
                              total=Decimal(total), total_envio=Decimal(envio),
                              comision_plataforma=Decimal(comision)))
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

    def cargar_el_caso_completo(self):
        """Los tres tipos de fila, para que los seis numeros se puedan calcular."""
        self.venta()
        self.gasto()
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


# =====================================================================
# PARTE 1 - Las tres tarjetas se fueron
# =====================================================================

# Lo que el dashboard NO puede volver a mostrar. Se afirma contra el TITULO de
# cada tarjeta y no contra su valor: el valor cambia con los datos, el titulo
# es la pantalla.
TARJETAS_QUE_SE_FUERON = (
    'Margen %',
    'ROI %',
    'EVA - Valor Económico Agregado',
)


class TestLasTarjetasSeFueron(BaseEvaS5):

    def test_dashboard_no_muestra_margen_roi_eva(self):
        """La afirmacion de la slice, con los datos que SI los calculaban."""
        self.cargar_el_caso_completo()

        texto = self.texto()

        for titulo in TARJETAS_QUE_SE_FUERON:
            self.assertNotIn(
                titulo, texto,
                'La tarjeta "%s" volvio al dashboard. Compara compras '
                'historicas -- mercaderia que sigue en el deposito -- contra '
                'ventas historicas: eso es caja, no rentabilidad. El margen '
                'real esta en /reportes/margen.' % titulo)

    def test_tampoco_estan_los_valores(self):
        """No alcanza con sacar el titulo: el numero tampoco puede quedar.

        Con este caso el margen da 20.0%, el ROI 7.0% y el EVA -9000. Si
        alguien renombra la tarjeta en vez de sacarla, el test de arriba pasa y
        este no.
        """
        self.cargar_el_caso_completo()

        texto = self.texto()

        self.assertNotIn('20.0%', texto)     # margen
        self.assertNotIn('7.0%', texto)      # ROI
        self.assertNotIn('$-9000', texto)    # EVA

    def test_no_queda_el_semaforo_de_esas_tarjetas(self):
        """El veredicto tampoco: es la parte que mas se leia."""
        self.cargar_el_caso_completo()

        texto = self.texto()

        for veredicto in ('❌ No rentable', '✅ Rentable', '❌ Muy bajo'):
            self.assertNotIn(veredicto, texto)

    def test_no_queda_la_nota_del_costo_de_capital(self):
        """La nota al pie de FASE-EVA-S3 colgaba del card de EVA y se fue con el."""
        self.cargar_el_caso_completo()

        self.assertNotIn('Costo de capital calculado sobre', self.texto())

    def test_el_dashboard_sigue_respondiendo_200(self):
        """Con datos y sin datos: sacar tarjetas no puede romper la pantalla."""
        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(self.roman_id)
                sesion['_fresh'] = True
            self.assertEqual(200, cli.get('/dashboard').status_code)
        finally:
            self.ctx.push()

        self.cargar_el_caso_completo()

        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(self.roman_id)
                sesion['_fresh'] = True
            self.assertEqual(200, cli.get('/dashboard').status_code)
        finally:
            self.ctx.push()


# =====================================================================
# PARTE 2 - Los tres que quedan no se movieron
# =====================================================================

class TestLosTresQueQuedan(BaseEvaS5):
    """Ni de valor ni de calculo. Es la mitad que hace segura a la otra."""

    def test_ingresos_gastos_ganancia_siguen_igual(self):
        """Los tres numeros, en el dict y en la pantalla, con la misma cuenta.

            ingresos       = facturado neto de las VENTAS  = 150000  (S4)
            gastos         = SUM(Gasto.monto)              = 120000
            utilidad_bruta = 150000 - 120000               =  30000
            impuestos      = 30000 * 0.30                  =   9000
            utilidad_neta  = 30000 - 9000                  =  21000

        El aporte de 999999 tiene que estar cargado y NO puede ser el ingreso:
        sin esa fila, este test pasaria igual con el bug de S4 puesto de vuelta.
        """
        self.cargar_el_caso_completo()

        texto, analisis = self._pedir_dashboard()

        self.assertEqual(150000.0, analisis['ingresos'])
        self.assertEqual(120000.0, analisis['gastos'])
        self.assertEqual(30000.0, analisis['utilidad_bruta'])
        self.assertEqual(9000.0, analisis['impuestos'])
        self.assertEqual(UTILIDAD_NETA, analisis['utilidad_neta'])

        # Y los mismos tres, tal cual los formatea la plantilla.
        self.assertIn('Ingresos por Ventas', texto)
        self.assertIn('$150000', texto)
        self.assertIn('Gastos Totales', texto)
        self.assertIn('$120000', texto)
        self.assertIn('Ganancia Neta', texto)
        self.assertIn('$21000', texto)

    def test_los_ingresos_siguen_siendo_las_ventas_y_no_el_aporte(self):
        """El fix de FASE-EVA-S4, intacto."""
        self.venta()
        self.aporte('999999.00')

        self.assertEqual(150000.0, self.analisis()['ingresos'])

    def test_el_estado_neutral_de_s2_sigue_intacto(self):
        """Una cuenta sin una sola fila no gana alarmas rojas."""
        texto, analisis = self._pedir_dashboard()

        self.assertFalse(analisis['hay_movimiento'])
        self.assertIn('Todavía no cargaste ingresos ni gastos', texto)
        self.assertIn('Sin movimientos todavía', texto)
        for alarma in ('❌ Mal', '❌ No rentable', '❌ Demasiado alto'):
            self.assertNotIn(alarma, texto)

    def test_los_graficos_siguen_en_pie(self):
        """`gastos_cat` / `ingresos_cat` no salen de eva_utils y no se tocaron.

        El grafico de ingresos sigue siendo la tabla Ingreso -- el aporte de los
        socios --, que es otra cosa que la tarjeta "Ingresos por Ventas" y por
        eso no se llaman igual.
        """
        self.cargar_el_caso_completo()

        texto = self.texto()

        self.assertIn('Gastos por Categoría', texto)
        self.assertIn('Ingresos Cargados por Categoría', texto)
        self.assertIn('Aporte de capital (socios)', texto)


# =====================================================================
# PARTE 3 - Las recomendaciones del pie
# =====================================================================

class TestRecomendaciones(BaseEvaS5):
    """Que quedo del bloque que dependia de los indicadores que se fueron.

    Quedo UNA sola familia: la del ratio de gastos. Es la unica que se arma con
    los dos numeros que siguen en pantalla y, sobre todo, la unica que se
    enuncia por lo que de verdad es -- "gastaste el 80% de lo que entro" es una
    frase de caja, y es cierta aunque la mercaderia siga en el deposito.

    `ratio` y `margen` son el mismo numero al reves (margen = 100 - ratio): lo
    que los separa no es la cuenta, es lo que cada uno afirma. Esta anotado en
    el docstring de `generar_recomendaciones` para que nadie "unifique" los dos
    de vuelta creyendo que arregla un duplicado.
    """

    def test_ya_no_recomienda_sobre_margen_roi_ni_eva(self):
        """Los tres consejos que hablaban de las tarjetas que se fueron."""
        analisis = eva_utils.generar_analisis_completo(
            100000.0, 95000.0,
            ConfigFalsaS5(capital_invertido=CAPITAL))
        mensajes = ' '.join(r['mensaje'] for r in analisis['recomendaciones'])

        for texto in ('margen de ganancia es muy bajo',
                      'Tu margen está bajo',
                      'retorno sobre inversión es bajo',
                      'mejorar el retorno de tu inversión',
                      'Tu EVA es negativo'):
            self.assertNotIn(texto, mensajes)

    def test_tampoco_pide_cargar_el_capital(self):
        """Decia "para ver el ROI y el EVA, cargá el capital invertido".

        Ya no hay ROI ni EVA que ver: pedir un dato para una pantalla que no
        existe es mandar a Roman a llenar un campo por nada.
        """
        analisis = eva_utils.generar_analisis_completo(
            100000.0, 50000.0, ConfigFalsaS5(capital_invertido=0.0))
        mensajes = ' '.join(r['mensaje'] for r in analisis['recomendaciones'])

        self.assertNotIn('capital invertido', mensajes)

    def test_la_alerta_de_gastos_se_queda(self):
        """Gastar mas del 80% de lo que entro sigue siendo una noticia real."""
        analisis = eva_utils.generar_analisis_completo(
            100000.0, 95000.0, ConfigFalsaS5(capital_invertido=CAPITAL))

        self.assertEqual(1, len(analisis['recomendaciones']))
        self.assertEqual('peligro', analisis['recomendaciones'][0]['tipo'])
        self.assertIn('Gastas más del 80% de tus ingresos',
                      analisis['recomendaciones'][0]['mensaje'])

    def test_el_escalon_de_70_tambien(self):
        analisis = eva_utils.generar_analisis_completo(
            100000.0, 75000.0, ConfigFalsaS5(capital_invertido=CAPITAL))

        self.assertEqual('advertencia', analisis['recomendaciones'][0]['tipo'])
        self.assertIn('gastos son demasiado altos',
                      analisis['recomendaciones'][0]['mensaje'])

    def test_la_cuenta_vacia_sigue_recibiendo_un_solo_mensaje_informativo(self):
        """El arreglo de FASE-EVA-S2 no se perdio por el camino."""
        analisis = eva_utils.generar_analisis_completo(0.0, 0.0,
                                                       ConfigFalsaS5())
        recomendaciones = analisis['recomendaciones']

        self.assertEqual(1, len(recomendaciones))
        self.assertEqual('info', recomendaciones[0]['tipo'])
        self.assertIn('Todavía no cargaste ingresos ni gastos',
                      recomendaciones[0]['mensaje'])

    def test_un_negocio_sano_sigue_recibiendo_la_felicitacion(self):
        """Sin alarma que emitir, el bloque no se queda mudo."""
        analisis = eva_utils.generar_analisis_completo(
            100000.0, 40000.0, ConfigFalsaS5(capital_invertido=CAPITAL))

        self.assertEqual(1, len(analisis['recomendaciones']))
        self.assertEqual('exito', analisis['recomendaciones'][0]['tipo'])

    def test_el_pie_del_dashboard_sigue_existiendo(self):
        """El bloque no se saco: se quedo con la mitad que dice algo cierto."""
        self.venta()
        self.gasto('140000.00')      # 93.3% de lo que entro

        texto = self.texto()

        self.assertIn('Recomendaciones', texto)
        self.assertIn('Gastas más del 80% de tus ingresos', texto)


# =====================================================================
# PARTE 4 - La cuenta no se borro
# =====================================================================

class TestLaCuentaSigueViva(BaseEvaS5):
    """eva_utils no quedo muerto: quedo sin pantalla, que es otra cosa.

    Sin esta clase, la proxima persona que lea el dashboard y no encuentre
    margen/ROI/EVA borra eva_utils.py creyendo que sobra, y con el se van los
    tres guards de division por cero de S2 y el prorrateo de S3 -- dos slices
    enteras de trabajo, para tener que re-derivarlas el dia que el negocio
    crezca.
    """

    def test_la_formula_del_eva_no_se_toco(self):
        """Los mismos numeros congelados desde S2, con un anio de periodo.

            utilidad_bruta = 150000 - 120000    = 30000
            impuestos      = 30000 * 0.30       =  9000
            utilidad_neta  = 30000 - 9000       = 21000
            costo_capital  = 300000 * 10 / 100  = 30000
            EVA            = 21000 - 30000      = -9000
        """
        analisis = eva_utils.generar_analisis_completo(
            150000.0, 120000.0, ConfigFalsaS5(capital_invertido=CAPITAL), 365)

        self.assertEqual(30000.0, analisis['utilidad_bruta'])
        self.assertEqual(UTILIDAD_NETA, analisis['utilidad_neta'])
        self.assertEqual(30000.0, analisis['costo_capital'])
        self.assertEqual(-9000.0, analisis['eva'])
        self.assertAlmostEqual(20.0, analisis['margen_ganancia'])
        self.assertAlmostEqual(7.0, analisis['roi'])

    def test_el_prorrateo_de_s3_sigue_ahi(self):
        """Medio periodo, medio costo. No se cobra el anio entero."""
        analisis = eva_utils.generar_analisis_completo(
            150000.0, 120000.0, ConfigFalsaS5(capital_invertido=CAPITAL), 45)

        self.assertAlmostEqual(CAPITAL * 0.10 * (45 / 365),
                               analisis['costo_capital'], places=6)
        self.assertNotAlmostEqual(-9000.0, analisis['eva'], places=2)

    def test_los_indicadores_siguen_llegando_a_la_plantilla(self):
        """Se calculan y viajan; simplemente no se renderizan.

        Es lo que hace barata la vuelta atras: el dia que se retomen, la
        plantilla ya los tiene a mano.
        """
        self.cargar_el_caso_completo()

        analisis = self.analisis()

        for clave in ('margen_ganancia', 'roi', 'eva', 'ratio_gastos',
                      'costo_capital', 'dias_transcurridos'):
            self.assertIn(clave, analisis)
            self.assertIsNotNone(analisis[clave])

    def test_los_campos_de_config_siguen_en_el_modelo(self):
        """`capital_invertido` y `tasa_costo_capital` no se borraron."""
        empresa = db.session.get(Empresa, self.empresa_id)

        self.assertEqual(CAPITAL, empresa.capital_invertido)
        self.assertEqual(10.0, empresa.tasa_costo_capital)

    def test_config_eva_sigue_siendo_alcanzable(self):
        """PARTE 2b: la pantalla no quedo huerfana.

        El unico link a /config/eva nunca estuvo pegado a las tarjetas que se
        sacaron -- vive en el sidebar, como "Configuración" -- asi que no hubo
        que agregar ninguno. Este test lo fija: si alguien lo saca del menu, la
        pantalla se vuelve inalcanzable sin escribir la URL a mano.
        """
        texto = self.texto()

        self.assertIn('/config/eva', texto)
        self.assertIn('Configuración', texto)

    def test_config_eva_responde_y_guarda(self):
        """La ruta sigue viva y sigue escribiendo los tres parametros."""
        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(self.roman_id)
                sesion['_fresh'] = True

            self.assertEqual(200, cli.get('/config/eva').status_code)

            cli.post('/config/eva',
                     data={'nombre': 'Korvo', 'ruc': '', 'email': '',
                           'tasa_costo_capital': '12.5',
                           'capital_invertido': '500000',
                           'tasa_impuestos': '35'},
                     follow_redirects=True)
        finally:
            self.ctx.push()

        empresa = db.session.get(Empresa, self.empresa_id)
        self.assertEqual(12.5, empresa.tasa_costo_capital)
        self.assertEqual(500000.0, empresa.capital_invertido)
        self.assertAlmostEqual(0.35, empresa.tasa_impuestos)

    def test_config_eva_ya_no_promete_un_eva_en_pantalla(self):
        """El cuadro "Que es esto?" decia que el EVA mide si generas valor.

        Prometia un numero que ya no se muestra en ningun lado. Ahora dice que
        no se muestra y manda al reporte que si contesta la pregunta.
        """
        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(self.roman_id)
                sesion['_fresh'] = True
            texto = cli.get('/config/eva').get_data(as_text=True)
        finally:
            self.ctx.push()

        self.assertIn('no se muestra en el dashboard', texto)
        self.assertIn('Margen y Ganancia', texto)


if __name__ == '__main__':
    unittest.main()
