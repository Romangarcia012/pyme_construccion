# -*- coding: utf-8 -*-
"""Tests de FASE-RESULTADOS-S2 (muestras en linea aparte, solo presentacion).

    python -m unittest discover -s tests -v

    el total no cambia      -> la utilidad neta es la misma con o sin la marca
    la cuenta cierra        -> antes de pruebas - muestras = utilidad neta
    la marca manda          -> renombrar la categoria no mueve el gasto de linea
    solo un gasto es prueba -> el CHECK rechaza un activo marcado

Reusa el escenario de FASE-RESULTADOS-S1. La app se repunta a SQLite en
memoria: la base productiva no se toca.
"""

import os
import sys
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.exc import IntegrityError  # noqa: E402

from app import app  # noqa: E402
from models import NATURALEZA_ACTIVO, NATURALEZA_GASTO, Categoria, db  # noqa: E402
from tests.test_fase_resultados_s1 import BaseResultados  # noqa: E402

ENGINE_PRODUCTIVO = None


def setUpModule():
    global ENGINE_PRODUCTIVO
    engines = db._app_engines[app]
    ENGINE_PRODUCTIVO = engines[None]
    engines[None] = db._make_engine(None, {'url': 'sqlite://'}, app)
    app.config['TESTING'] = True


def tearDownModule():
    with app.app_context():
        db.drop_all()
        db.session.remove()
    db._app_engines[app][None].dispose()
    db._app_engines[app][None] = ENGINE_PRODUCTIVO
    app.config['TESTING'] = False


class BasePruebas(BaseResultados):
    """El escenario de S1 mas una categoria de muestras con dos gastos."""

    def setUp(self):
        super().setUp()
        self.cat_pruebas = Categoria(nombre='Productos de prueba', tipo='gasto',
                                     naturaleza=NATURALEZA_GASTO, es_prueba=True,
                                     empresa_id=self.empresa_id)
        db.session.add(self.cat_pruebas)
        db.session.commit()
        self.ventas_tipicas()                  # utilidad bruta 26100
        self.gasto('5000.00')                  # publicidad
        self.gasto('271740.00', categoria=self.cat_pruebas, fecha=date(2026, 8, 4))
        self.gasto('17537.63', categoria=self.cat_pruebas)


class TestMuestrasEnLineaAparte(BasePruebas):

    def test_la_utilidad_neta_total_es_identica(self):
        """Con la marca o sin ella, la utilidad neta es la misma: la marca
        mueve el gasto de linea, no de la cuenta."""
        con_marca = self.resultado()

        self.cat_pruebas.es_prueba = False
        db.session.commit()
        sin_marca = self.resultado()

        #   26100 - 5000 - 271740 - 17537,63 = -268177,63
        self.assertEqual(Decimal('-268177.63'), con_marca['utilidad_neta'])
        self.assertEqual(con_marca['utilidad_neta'], sin_marca['utilidad_neta'])
        self.assertEqual(Decimal('0'), sin_marca['pruebas']['total'])

    def test_antes_de_pruebas_menos_muestras_es_la_neta(self):
        resultado = self.resultado()

        self.assertEqual(Decimal('21100.00'), resultado['utilidad_antes_de_pruebas'])
        self.assertEqual(Decimal('289277.63'), resultado['pruebas']['total'])
        self.assertEqual(2, resultado['pruebas']['cantidad'])
        self.assertEqual(resultado['utilidad_antes_de_pruebas']
                         - resultado['pruebas']['total'],
                         resultado['utilidad_neta'])
        # Los operativos ya no incluyen las muestras.
        self.assertEqual(Decimal('5000.00'), resultado['gastos']['total'])
        self.assertEqual(Decimal('37.0'), resultado['utilidad_antes_de_pruebas_pct'])

    def test_renombrar_la_categoria_no_mueve_el_gasto_de_linea(self):
        antes = self.resultado()

        self.cat_pruebas.nombre = 'I+D'
        db.session.commit()
        despues = self.resultado()

        self.assertEqual(antes['pruebas']['total'], despues['pruebas']['total'])
        self.assertEqual(antes['gastos']['total'], despues['gastos']['total'])

    def test_el_periodo_tambien_separa_las_pruebas(self):
        """El mismo bloque por mes: la muestra de agosto sale en agosto."""
        agosto = self.resultado(periodo='2026-08')

        self.assertEqual(Decimal('271740.00'), agosto['pruebas']['total'])
        self.assertEqual(Decimal('0'), agosto['gastos']['total'])

    def test_la_pantalla_muestra_las_dos_lineas(self):
        _, texto = self.margen()

        self.assertIn('Utilidad neta antes de pruebas', texto)
        self.assertIn('Muestras y productos de prueba', texto)
        self.assertIn('21100.00', texto)
        self.assertIn('289277.63', texto)


class TestLaMarca(BasePruebas):

    def test_un_activo_no_puede_ser_prueba(self):
        db.session.add(Categoria(nombre='Stock raro', tipo='gasto',
                                 naturaleza=NATURALEZA_ACTIVO, es_prueba=True,
                                 empresa_id=self.empresa_id))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_editar_categoria_pone_y_saca_la_marca(self):
        categoria_id = self.cat_publicidad.id
        self.client.post('/categoria/editar/%d' % categoria_id,
                         data={'nombre': 'Publicidad', 'naturaleza': NATURALEZA_GASTO,
                               'es_prueba': '1'})
        db.session.expire_all()
        self.assertTrue(db.session.get(Categoria, categoria_id).es_prueba)

        self.client.post('/categoria/editar/%d' % categoria_id,
                         data={'nombre': 'Publicidad', 'naturaleza': NATURALEZA_GASTO})
        db.session.expire_all()
        self.assertFalse(db.session.get(Categoria, categoria_id).es_prueba)

    def test_la_marca_se_ignora_si_la_categoria_no_es_gasto(self):
        self.client.post('/nueva-categoria', data={
            'nombre': 'Flete de compra', 'tipo': 'gasto',
            'naturaleza': NATURALEZA_ACTIVO, 'es_prueba': '1'})

        flete = Categoria.query.filter_by(nombre='Flete de compra').one()
        self.assertFalse(flete.es_prueba)


if __name__ == '__main__':
    unittest.main()
