# -*- coding: utf-8 -*-
"""Tests de FASE-EVA-S3 (el costo de capital se prorratea al periodo real).

    python -m unittest discover -s tests -v

EL BUG

`tasa_costo_capital` es una tasa ANUAL. `generar_analisis_completo` la cobraba
entera -- capital * tasa/100 -- contra unos ingresos y gastos que el dashboard
suma de toda la vida de la empresa, sin filtro de fecha (app.py:493). A los 45
dias de haber cargado el primer movimiento, Korvo pagaba un anio completo de
costo de capital contra la ganancia de mes y medio. El EVA salia mucho mas
negativo de lo que era, y el semaforo marcaba "No rentable" un negocio que
todavia no habia tenido tiempo de serlo.

FASE-EVA-S2 lo dejo anotado a proposito en el docstring: no era la slice.

LA PRUEBA QUE IMPORTA

Son dos, y hacen falta las dos:

  * `test_costo_capital_prorrateado_a_45_dias` fija que el fix ocurre.
  * `test_costo_capital_a_un_anio_completo_da_igual_que_antes` fija que ocurre
    SOLO donde correspondia. Un periodo de exactamente 365 dias tiene que dar
    el mismo numero que la formula vieja: si tambien cambiara ahi, lo que se
    escribio no es un prorrateo sino otra formula.

DE DONDE SALE EL PERIODO (Parte 1)

Del primer movimiento real: MIN(fecha) entre `gasto` e `ingreso`. No hay en el
modelo ningun campo de "inicio de operaciones", y `Empresa.fecha_creacion` no
sirve de sustituto -- es cuando se dio de alta la cuenta en este sistema, no
cuando arranco el negocio. `TestDeDondeSaleElPeriodo` lo deja fijado para que
nadie lo cambie por el campo equivocado sin darse cuenta.

QUE LE HIZO FASE-EVA-S5 A ESTE ARCHIVO

Nada a la formula: TestDeDondeSaleElPeriodo y TestProrrateo estan intactos, y
el prorrateo sigue calculandose en cada /dashboard. Lo que cambio es donde se
afirma: la nota "Costo de capital calculado sobre N días" colgaba del card de
EVA, y ese card salio del dashboard. Los tests que la buscaban en el HTML ahora
miran el dict que la ruta le pasa a la plantilla. El porque esta en el docstring
de TestPeriodoDelDashboard.

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


class ConfigFalsa:
    """Los tres parametros del EVA, sin tocar la base. Igual que en S2."""

    def __init__(self, capital_invertido=0.0, tasa_costo_capital=10.0,
                 tasa_impuestos=0.30):
        self.capital_invertido = capital_invertido
        self.tasa_costo_capital = tasa_costo_capital
        self.tasa_impuestos = tasa_impuestos


# Los numeros del enunciado, en un solo lugar.
CAPITAL = 300000.0
TASA = 10.0
INGRESOS = 150000.0
GASTOS = 120000.0
# utilidad_bruta 30000 - impuestos 9000
UTILIDAD_NETA = 21000.0
# Lo que costaba el capital cuando se cobraba el anio entero.
COSTO_ANUAL = 30000.0


# =====================================================================
# PARTE 1 - De donde sale el periodo
# =====================================================================

class TestDeDondeSaleElPeriodo(unittest.TestCase):
    """`dias_de_operacion`, la funcion sola. Sin base."""

    def test_cuenta_desde_el_primer_movimiento(self):
        hoy = date(2026, 9, 4)
        self.assertEqual(
            45, eva_utils.dias_de_operacion(hoy - timedelta(days=45), hoy=hoy))

    def test_sin_movimientos_no_hay_periodo(self):
        """None, no 0 y no 365.

        Mismo criterio que los guards de S2: "no se puede medir" es distinto de
        cualquier numero. Que pasa despues con ese None lo decide quien llama.
        """
        self.assertIsNone(eva_utils.dias_de_operacion(None))

    def test_el_mismo_dia_da_uno_y_no_cero(self):
        hoy = date(2026, 9, 4)
        self.assertEqual(1, eva_utils.dias_de_operacion(hoy, hoy=hoy))

    def test_una_fecha_futura_tampoco_da_negativo(self):
        """Una fecha tipeada mal a mano no puede devolverle plata a la empresa.

        Sin el `max(1, ...)` el prorrateo daria negativo, o sea un costo de
        capital negativo, o sea un EVA inflado por un error de tipeo.
        """
        hoy = date(2026, 9, 4)
        self.assertEqual(
            1, eva_utils.dias_de_operacion(hoy + timedelta(days=10), hoy=hoy))

    def test_no_existe_un_campo_de_inicio_de_operaciones(self):
        """El motivo por el que se usa el primer movimiento y no otra cosa.

        Si algun dia alguien agrega ese campo a Empresa, este test se pone rojo
        y obliga a decidir a conciencia cual de los dos manda -- en vez de que
        queden dos fuentes de verdad para el mismo periodo.
        """
        self.assertFalse(
            hasattr(Empresa, 'inicio_operaciones'),
            'Si Empresa gana un inicio de operaciones, `dias_de_operacion` '
            'tiene que decidir explicitamente si lo prefiere al primer '
            'movimiento.')


# =====================================================================
# PARTE 2 - El prorrateo
# =====================================================================

class TestProrrateo(unittest.TestCase):
    """La formula sola, con la config falsa."""

    def analisis(self, dias):
        return eva_utils.generar_analisis_completo(
            INGRESOS, GASTOS,
            ConfigFalsa(capital_invertido=CAPITAL, tasa_costo_capital=TASA),
            dias)

    def test_costo_capital_prorrateado_a_45_dias(self):
        """El caso del enunciado: 45 dias de operacion, no un anio."""
        esperado = CAPITAL * (TASA / 100) * (45 / 365)

        analisis = self.analisis(45)

        self.assertAlmostEqual(esperado, analisis['costo_capital'], places=6)
        self.assertAlmostEqual(3698.63, analisis['costo_capital'], places=2)
        # Y explicitamente NO el anio entero, que era el bug.
        self.assertNotAlmostEqual(COSTO_ANUAL, analisis['costo_capital'],
                                  places=2)

    def test_el_eva_usa_el_costo_prorrateado(self):
        """Lo que estaba mal no era un numero interno: era el veredicto.

        Con el anio entero el EVA daba -9000 y el semaforo decia "No rentable".
        Prorrateado a 45 dias da positivo: el negocio SI genero valor en el mes
        y medio que lleva operando.
        """
        analisis = self.analisis(45)

        self.assertAlmostEqual(UTILIDAD_NETA - analisis['costo_capital'],
                               analisis['eva'], places=6)
        self.assertGreater(analisis['eva'], 0)
        self.assertEqual('excelente', analisis['eva_estado'][0])

    def test_costo_capital_a_un_anio_completo_da_igual_que_antes(self):
        """365 dias => la cuenta vieja, clavada.

        Si esto se moviera, lo que se escribio no seria un prorrateo: seria
        otra formula que ademas cambia el unico caso que estaba bien.
        """
        analisis = self.analisis(365)

        self.assertEqual(COSTO_ANUAL, analisis['costo_capital'])
        self.assertEqual(-9000.0, analisis['eva'])
        self.assertEqual('malo', analisis['eva_estado'][0])
        # El resto de la formula tampoco se movio.
        self.assertEqual(30000.0, analisis['utilidad_bruta'])
        self.assertEqual(9000.0, analisis['impuestos'])
        self.assertEqual(UTILIDAD_NETA, analisis['utilidad_neta'])
        self.assertAlmostEqual(7.0, analisis['roi'])

    def test_primer_dia_no_divide_por_cero(self):
        """Un movimiento cargado hoy mismo: 1 dia, no una excepcion."""
        analisis = self.analisis(eva_utils.dias_de_operacion(date.today()))

        self.assertAlmostEqual(CAPITAL * (TASA / 100) * (1 / 365),
                               analisis['costo_capital'], places=6)
        self.assertEqual(1, analisis['dias_transcurridos'])

    def test_el_default_es_el_supuesto_viejo(self):
        """Sin periodo, la funcion se porta como antes de la slice.

        Es lo que mantiene verdes los tests de la formula de S2 sin retocarlos,
        y deja el supuesto escrito en la firma en vez de escondido en la cuenta.
        """
        sin_periodo = eva_utils.generar_analisis_completo(
            INGRESOS, GASTOS,
            ConfigFalsa(capital_invertido=CAPITAL, tasa_costo_capital=TASA))

        self.assertEqual(COSTO_ANUAL, sin_periodo['costo_capital'])

    def test_periodo_desconocido_se_comporta_como_el_default(self):
        """None llega desde el dashboard de una empresa sin un solo movimiento.

        Ese caso ya termina en el estado neutral de S2 (el EVA es None), asi que
        el numero no llega a pantalla; lo que se fija aca es que no reviente.
        """
        analisis = eva_utils.generar_analisis_completo(
            0.0, 0.0,
            ConfigFalsa(capital_invertido=CAPITAL, tasa_costo_capital=TASA),
            None)

        self.assertIsNone(analisis['eva'])
        self.assertFalse(analisis['periodo_conocido'])
        self.assertEqual(eva_utils.CLASE_NEUTRAL, analisis['eva_estado'][0])

    def test_el_prorrateo_es_lineal(self):
        """Medio anio cuesta la mitad. La propiedad, no un caso puntual."""
        medio = self.analisis(182)['costo_capital']
        entero = self.analisis(364)['costo_capital']

        self.assertAlmostEqual(entero / 2, medio, places=6)


# =====================================================================
# PARTE 2b - El periodo por el camino real (la ruta, no la formula sola)
#
# Se llamaba "Que se vea en pantalla" hasta FASE-EVA-S5, que saco del dashboard
# las tarjetas de margen/ROI/EVA y con ellas la nota que mostraba el periodo.
# El calculo no se toco: sigue haciendose en /dashboard sobre las filas reales,
# y es lo que estas clases fijan.
# =====================================================================

class BaseDashboard(unittest.TestCase):
    """Una empresa con capital cargado y movimientos fechados a voluntad."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Korvo', capital_invertido=CAPITAL,
                               tasa_costo_capital=TASA, tasa_impuestos=0.30)
        db.session.add(self.empresa)
        db.session.flush()

        self.roman = Usuario(nombre='Roman', email='roman@test.local',
                             empresa_id=self.empresa.id, verificado=True)
        self.roman.set_password('irrelevante')
        db.session.add(self.roman)
        db.session.commit()

        self.cat_gasto = Categoria(nombre='Compra de mercadería', tipo='gasto',
                                   empresa_id=self.empresa.id,
                                   usuario_id=self.roman.id)
        self.cat_ingreso = Categoria(nombre='Venta', tipo='ingreso',
                                     empresa_id=self.empresa.id,
                                     usuario_id=self.roman.id)
        db.session.add_all([self.cat_gasto, self.cat_ingreso])
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.roman_id = self.roman.id
        self.cat_gasto_id = self.cat_gasto.id
        self.cat_ingreso_id = self.cat_ingreso.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def cargar(self, hace_dias, monto_ingreso=INGRESOS, monto_gasto=GASTOS):
        """Un ingreso y un gasto fechados hace `hace_dias`.

        Los dos son lo que arma el PERIODO -- que es de lo que habla esta
        suite --, y eso no cambio en FASE-EVA-S4: el MIN(fecha) sigue saliendo
        de gasto e ingreso. Lo que cambio es que el monto del Ingreso ya no es
        lo que el dashboard llama ingresos; para eso esta `cargar_venta`.
        """
        fecha = date.today() - timedelta(days=hace_dias)
        db.session.add(Ingreso(descripcion='Aporte', monto=Decimal(monto_ingreso),
                               fecha=fecha, empresa_id=self.empresa_id,
                               usuario_id=self.roman_id,
                               categoria_id=self.cat_ingreso_id))
        db.session.add(Gasto(descripcion='Compra', monto=Decimal(monto_gasto),
                             fecha=fecha, empresa_id=self.empresa_id,
                             usuario_id=self.roman_id,
                             categoria_id=self.cat_gasto_id))
        db.session.commit()

    def cargar_venta(self, monto=INGRESOS):
        """La venta que hoy es "ingresos" para el dashboard (FASE-EVA-S4).

        Va aparte de `cargar` porque contesta otra pregunta: `cargar` fija el
        periodo, esto fija el ingreso. Los tests que solo miran el periodo no
        necesitan una venta, y los que miran el EVA en pantalla necesitan las
        dos cosas.
        """
        canal = CanalVenta.query.filter_by(empresa_id=self.empresa_id).first()
        if canal is None:
            canal = CanalVenta(empresa_id=self.empresa_id, tipo='manual',
                               nombre='Venta manual', activo=True)
            db.session.add(canal)
            db.session.commit()

        db.session.add(Pedido(empresa_id=self.empresa_id, canal_id=canal.id,
                              fecha_pedido=datetime(2026, 9, 1, 12, 0),
                              estado='open', comprador_nombre='Camila',
                              total=Decimal(str(monto)),
                              total_envio=Decimal('0.00'),
                              comision_plataforma=Decimal('0.00')))
        db.session.commit()

    def texto_del_dashboard(self):
        """El pop/push es el guard que documenta tests/ayuda_auth.py."""
        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(self.roman_id)
                sesion['_fresh'] = True
            return cli.get('/dashboard',
                           follow_redirects=True).get_data(as_text=True)
        finally:
            self.ctx.push()

    def analisis_del_dashboard(self):
        """El dict que /dashboard le pasa a la plantilla.

        FASE-EVA-S5: hace falta porque el periodo dejo de estar EN la pantalla.
        Seguia siendo el mismo calculo, hecho por el mismo camino real -- la
        ruta, con las filas en la base --, pero ya no hay texto donde buscarlo.
        Mirar el dict es lo mas cerca de la pantalla que se puede afirmar sin
        inventar una linea que no existe.
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
                cli.get('/dashboard', follow_redirects=True)
            finally:
                template_rendered.disconnect(anotar, app)
        finally:
            self.ctx.push()

        return capturado.get('ctx', {}).get('analisis', {})


class TestPeriodoDelDashboard(BaseDashboard):
    """El periodo que la RUTA calcula, con filas de verdad en la base.

    ANTES SE LLAMABA TestPeriodoEnPantalla, Y AFIRMABA SOBRE EL TEXTO

    Los tests de esta clase buscaban en el HTML la nota "Costo de capital
    calculado sobre 45 días de operación" que colgaba del card de EVA. Esa nota
    se fue con el card en FASE-EVA-S5: margen, ROI y EVA salieron del dashboard
    porque comparaban compras historicas contra ventas historicas -- caja con
    nombre de rentabilidad --, y el margen real ya vive en /reportes/margen.

    Lo que la nota explicaba NO se fue: la ruta sigue calculando el periodo
    desde el primer movimiento y sigue prorrateando el costo de capital con el
    (app.py). Eso es lo que estos tests siguen fijando, ahora sobre el dict que
    /dashboard le pasa a la plantilla en vez de sobre el texto renderizado.

    No se borraron porque lo que probaban no era la nota: era que el periodo
    salga del MIN de las fechas reales y no de la fila mas nueva. Ese bug puede
    volver igual sin que nadie mire la pantalla, y el dia que se retome el EVA
    la nota vuelve sobre un calculo que nunca dejo de estar probado.
    """

    def test_el_periodo_sale_de_las_filas_reales(self):
        """45 dias entre el primer movimiento y hoy, contados por la ruta."""
        self.cargar(hace_dias=45)

        self.assertEqual(45, self.analisis_del_dashboard()['dias_transcurridos'])

    def test_el_dashboard_usa_el_primer_movimiento_y_no_el_ultimo(self):
        """Con dos movimientos, manda el mas viejo.

        Si mandara el ultimo, cargar un gasto hoy resetearia el periodo a 1 dia
        y el costo de capital se desplomaria por haber tipeado una fila.
        """
        self.cargar(hace_dias=200)
        self.cargar(hace_dias=10)

        self.assertEqual(200,
                         self.analisis_del_dashboard()['dias_transcurridos'])

    def test_un_solo_dia_no_es_cero_dias(self):
        """Cargado hoy mismo: 1, que es el guard de division por cero."""
        self.cargar(hace_dias=0)

        self.assertEqual(1, self.analisis_del_dashboard()['dias_transcurridos'])

    def test_el_eva_de_la_ruta_es_el_prorrateado(self):
        """El prorrateo de S3 sigue vivo aunque nadie lo mire.

        Hasta FASE-EVA-S4 alcanzaba con `cargar`, porque el Ingreso que fechaba
        el periodo era ademas el ingreso de la cuenta. Ahora son dos cosas
        distintas y hacen falta las dos filas: la del periodo (45 dias) y la
        venta que da la utilidad.

        FASE-EVA-S5 le saco el `assertIn` sobre el texto -- el numero no se
        renderiza mas -- y le dejo la misma cuenta sobre el dict. El
        `assertNotEqual` contra -9000 es el que importa: -9000 es el EVA con el
        anio entero cobrado, o sea el bug que S3 arreglo.
        """
        self.cargar(hace_dias=45)
        self.cargar_venta()
        costo = CAPITAL * (TASA / 100) * (45 / 365)

        analisis = self.analisis_del_dashboard()

        self.assertAlmostEqual(costo, analisis['costo_capital'], places=6)
        self.assertAlmostEqual(UTILIDAD_NETA - costo, analisis['eva'], places=6)
        self.assertNotAlmostEqual(-9000.0, analisis['eva'], places=2)

    def test_sin_movimientos_no_hay_periodo(self):
        """El estado neutral de S2 sigue sin inventar un periodo."""
        analisis = self.analisis_del_dashboard()

        self.assertFalse(analisis['periodo_conocido'])
        self.assertIsNone(analisis['eva'])

    def test_el_estado_neutral_de_s2_sigue_intacto(self):
        """La cuenta vacia sigue sin alarmas rojas en la pantalla."""
        texto = self.texto_del_dashboard()

        for alarma in ('❌ Mal', '❌ No rentable',
                       '¡ALERTA! Gastas más del 80% de tus ingresos'):
            self.assertNotIn(alarma, texto)


class TestElPeriodoYaNoEstaEnPantalla(BaseDashboard):
    """FASE-EVA-S5: la nota del card de EVA no puede volver sola.

    El card se fue; la nota que colgaba de el, tambien. Si alguien vuelve a
    renderizar el periodo en el dashboard tiene que ser una decision, no un
    revert accidental de la plantilla.
    """

    def test_el_dashboard_no_habla_del_costo_de_capital(self):
        self.cargar(hace_dias=45)
        self.cargar_venta()

        texto = self.texto_del_dashboard()

        self.assertNotIn('Costo de capital calculado sobre', texto)
        self.assertNotIn('de operación', texto)

    # Que las TARJETAS de margen/ROI/EVA no esten es la afirmacion de la slice
    # que las saco, y vive en tests/test_fase_eva_s5.py. Aca queda solo la nota
    # al pie, que era de S3 y colgaba del card de EVA.


if __name__ == '__main__':
    unittest.main()
