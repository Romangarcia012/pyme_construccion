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

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eva_utils  # noqa: E402

from app import app  # noqa: E402
from models import (  # noqa: E402
    Categoria,
    Empresa,
    Gasto,
    Ingreso,
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
# PARTE 2b - Que se vea en pantalla
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
        fecha = date.today() - timedelta(days=hace_dias)
        db.session.add(Ingreso(descripcion='Venta', monto=Decimal(monto_ingreso),
                               fecha=fecha, empresa_id=self.empresa_id,
                               usuario_id=self.roman_id,
                               categoria_id=self.cat_ingreso_id))
        db.session.add(Gasto(descripcion='Compra', monto=Decimal(monto_gasto),
                             fecha=fecha, empresa_id=self.empresa_id,
                             usuario_id=self.roman_id,
                             categoria_id=self.cat_gasto_id))
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


class TestPeriodoEnPantalla(BaseDashboard):

    def test_periodo_visible_en_pantalla(self):
        """El numero deja de ser opaco: la pantalla dice sobre que se prorrateo."""
        self.cargar(hace_dias=45)

        texto = self.texto_del_dashboard()

        self.assertIn('Costo de capital calculado sobre 45', texto)
        self.assertIn('días de operación', texto)

    def test_el_dashboard_usa_el_primer_movimiento_y_no_el_ultimo(self):
        """Con dos movimientos, manda el mas viejo.

        Si mandara el ultimo, cargar un gasto hoy resetearia el periodo a 1 dia
        y el costo de capital se desplomaria por haber tipeado una fila.
        """
        self.cargar(hace_dias=200)
        self.cargar(hace_dias=10)

        self.assertIn('sobre 200', self.texto_del_dashboard())

    def test_un_solo_dia_se_dice_en_singular(self):
        self.cargar(hace_dias=0)

        texto = self.texto_del_dashboard()

        self.assertIn('sobre 1', texto)
        self.assertIn('día de operación', texto)

    def test_el_eva_de_la_pantalla_es_el_prorrateado(self):
        """El numero grande y la nota de abajo cuentan la misma historia."""
        self.cargar(hace_dias=45)
        costo = CAPITAL * (TASA / 100) * (45 / 365)

        texto = self.texto_del_dashboard()

        self.assertIn('$%.0f' % (UTILIDAD_NETA - costo), texto)
        self.assertIn('$%.0f' % costo, texto)
        self.assertNotIn('$-9000', texto)

    def test_sin_movimientos_no_habla_de_periodo(self):
        """El estado neutral de S2 no gana una linea que no puede llenar."""
        texto = self.texto_del_dashboard()

        self.assertNotIn('Costo de capital calculado sobre', texto)
        self.assertNotIn('de operación', texto)

    def test_el_estado_neutral_de_s2_sigue_intacto(self):
        """La cuenta vacia sigue sin alarmas rojas."""
        texto = self.texto_del_dashboard()

        for alarma in ('❌ Mal', '❌ No rentable',
                       '¡ALERTA! Gastas más del 80% de tus ingresos'):
            self.assertNotIn(alarma, texto)


if __name__ == '__main__':
    unittest.main()
