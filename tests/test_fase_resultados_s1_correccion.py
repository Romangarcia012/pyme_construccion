# -*- coding: utf-8 -*-
"""Tests de FASE-RESULTADOS-S1-CORRECCION (scripts/corregir_fechas_resultados).

    python -m unittest discover -s tests -v

    dry-run             -> no escribe nada, ni en gastos ni en historial
    idempotente         -> dos corridas con --aplicar: la segunda no cambia
                           nada ni duplica historial
    ids                 -> se corrige la fila, no se reemplaza
    salvaguardas        -> --monto rechaza un gasto que no es el que cargo la
                           migracion, y sin el aporte de 42.000 no se escribe

La app se repunta a SQLite en memoria: la base productiva no se toca.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import (  # noqa: E402
    NATURALEZA_ACTIVO,
    NATURALEZA_GASTO,
    Categoria,
    Empresa,
    Gasto,
    Historial,
    Ingreso,
    db,
)
from scripts import corregir_fechas_resultados as script  # noqa: E402

ENGINE_PRODUCTIVO = None

# Lo que confirmo Roman contra KORVO.xlsx.
ARGS = ['--fecha', '34=2026-08-14', '--fecha', '35=2026-08-24',
        '--fecha', '36=2026-08-24', '--fecha', '38=2026-08-04',
        '--fecha', '39=2026-08-18', '--fecha', '40=2026-08-30',
        '--monto', '34=229000.00', '--monto', '39=17537.63',
        '--ajustar-aporte']


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


class BaseCorreccion(unittest.TestCase):
    """Korvo tal como lo dejo la migracion 52e5bd8bc955, con los mismos ids."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        empresa = Empresa(nombre='Korvo')
        db.session.add(empresa)
        db.session.flush()
        self.empresa_id = empresa.id
        categorias = {
            'Compra de mercadería': NATURALEZA_ACTIVO,
            'Flete de compra': NATURALEZA_ACTIVO,
            'Aduana/Impuestos': NATURALEZA_ACTIVO,
            'Productos de prueba': NATURALEZA_GASTO,
        }
        ids = {}
        for nombre, naturaleza in categorias.items():
            categoria = Categoria(nombre=nombre, tipo='gasto',
                                  naturaleza=naturaleza,
                                  empresa_id=self.empresa_id)
            db.session.add(categoria)
            db.session.flush()
            ids[nombre] = categoria.id
        aporte = Categoria(nombre='Aporte de capital (socios)', tipo='ingreso',
                           empresa_id=self.empresa_id)
        db.session.add(aporte)
        db.session.flush()

        de_cada_uno = {32: 'Compra de mercadería', 33: 'Flete de compra',
                       34: 'Aduana/Impuestos', 35: 'Compra de mercadería',
                       36: 'Flete de compra', 37: 'Aduana/Impuestos',
                       38: 'Productos de prueba', 39: 'Productos de prueba',
                       40: 'Productos de prueba', 41: 'Productos de prueba'}
        for gasto_id, descripcion, monto, fecha in script.RECARGADOS:
            db.session.add(Gasto(id=gasto_id, descripcion=descripcion,
                                 monto=Decimal(monto),
                                 fecha=date.fromisoformat(fecha),
                                 empresa_id=self.empresa_id,
                                 categoria_id=ids[de_cada_uno[gasto_id]],
                                 origen_fondo='capital'))
        db.session.add(Ingreso(id=10, descripcion='Aporte de capital (socios)',
                               monto=Decimal('2371037.83'),
                               fecha=date(2026, 7, 28),
                               empresa_id=self.empresa_id,
                               categoria_id=aporte.id))
        self.aporte_42 = Ingreso(descripcion='Aporte correccion taxes prod prueba',
                                 monto=Decimal('42000.00'),
                                 fecha=date(2026, 10, 19),
                                 empresa_id=self.empresa_id,
                                 categoria_id=aporte.id)
        db.session.add(self.aporte_42)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def correr(self, *argv):
        with redirect_stdout(io.StringIO()):
            return script.main(list(argv))

    def foto(self):
        db.session.expire_all()
        return (sorted((g.id, g.fecha, g.monto) for g in Gasto.query.all()),
                sorted((i.id, i.monto) for i in Ingreso.query.all()),
                Historial.query.count())


class TestCorreccion(BaseCorreccion):

    def test_el_dry_run_no_escribe_nada(self):
        antes = self.foto()

        self.assertEqual(0, self.correr(*ARGS))

        self.assertEqual(antes, self.foto())

    def test_aplicar_corrige_y_deja_historial(self):
        self.assertEqual(0, self.correr(*ARGS, '--aplicar'))
        db.session.expire_all()

        self.assertEqual(date(2026, 8, 14), db.session.get(Gasto, 34).fecha)
        self.assertEqual(Decimal('229000.00'), db.session.get(Gasto, 34).monto)
        self.assertEqual(Decimal('17537.63'), db.session.get(Gasto, 39).monto)
        self.assertEqual(date(2026, 7, 28), db.session.get(Gasto, 32).fecha)
        self.assertEqual(date(2026, 9, 14), db.session.get(Gasto, 41).fecha)
        self.assertEqual(Decimal('2355062.67'), db.session.get(Ingreso, 10).monto)
        # 6 fechas + 2 montos + 1 aporte, cada uno con su antes y despues.
        self.assertEqual(9, Historial.query.count())
        fila = Historial.query.filter_by(id_registro=39, tipo='gasto') \
            .filter(Historial.descripcion.like('Monto%')).one()
        self.assertEqual(('17568.67', '17537.63'),
                         (fila.valor_anterior, fila.valor_nuevo))

    def test_dos_corridas_no_cambian_nada_la_segunda(self):
        self.assertEqual(0, self.correr(*ARGS, '--aplicar'))
        despues_de_la_primera = self.foto()

        self.assertEqual(0, self.correr(*ARGS, '--aplicar'))

        self.assertEqual(despues_de_la_primera, self.foto())

    def test_los_ids_se_conservan(self):
        ids_antes = [fila[0] for fila in self.foto()[0]]

        self.correr(*ARGS, '--aplicar')

        self.assertEqual(ids_antes, [fila[0] for fila in self.foto()[0]])

    def test_monto_rechaza_un_gasto_que_no_cargo_la_migracion(self):
        with self.assertRaises(SystemExit):
            self.correr('--monto', '31=42000.00')

    def test_monto_rechaza_un_gasto_editado_a_mano(self):
        db.session.get(Gasto, 34).monto = Decimal('230000.00')
        db.session.commit()
        antes = self.foto()

        self.assertEqual(1, self.correr(*ARGS, '--aplicar'))

        self.assertEqual(antes, self.foto())

    def test_sin_el_aporte_de_42000_no_escribe(self):
        db.session.delete(self.aporte_42)
        db.session.commit()
        antes = self.foto()

        self.assertEqual(1, self.correr(*ARGS, '--aplicar'))

        self.assertEqual(antes, self.foto())


if __name__ == '__main__':
    unittest.main()
