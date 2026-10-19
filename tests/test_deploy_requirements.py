# -*- coding: utf-8 -*-
"""Tests de DEPLOY-FIX: lo que se importa tiene que estar fijado en
requirements.txt.

    python -m unittest discover -s tests -v

EL BUG QUE LOS TRAJO

9b8d10a y 71b9409 no arrancaban en Render: gunicorn salia con status 1 a los
~18 segundos. No era el codigo de esos commits. SQLAlchemy no estaba en
requirements.txt -- entraba como dependencia suelta de Flask-SQLAlchemy --, y
el build instalo SQLAlchemy 2.1, donde el driver por defecto de una URL
`postgresql://` paso de psycopg2 a psycopg (v3). psycopg no esta instalado,
y `db.init_app(app)` revienta al importar app.py:

    ModuleNotFoundError: No module named 'psycopg'

En desarrollo no se veia porque el Python local tiene SQLAlchemy 2.0.48.

Estos tests no levantan la app ni tocan ninguna base: leen requirements.txt y
el codigo fuente.
"""

import ast
import os
import re
import sys
import unittest
from importlib import metadata

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Carpetas que no son codigo del proyecto.
IGNORAR = {'venv', '.venv', '.git', '__pycache__', 'instance', 'node_modules'}


def _requirements():
    """{nombre normalizado: version fijada o None}."""
    fijados = {}
    with open(os.path.join(RAIZ, 'requirements.txt'), encoding='utf-8') as archivo:
        for linea in archivo:
            linea = linea.split('#', 1)[0].strip()
            if not linea:
                continue
            nombre, _, version = linea.partition('==')
            fijados[_normalizar(nombre)] = version.strip() or None
    return fijados


def _normalizar(nombre):
    return re.sub(r'[-_.]+', '-', nombre).strip().lower()


def _modulos_locales():
    locales = set()
    for nombre in os.listdir(RAIZ):
        ruta = os.path.join(RAIZ, nombre)
        if nombre.endswith('.py'):
            locales.add(nombre[:-3])
        elif os.path.isdir(ruta) and nombre not in IGNORAR:
            locales.add(nombre)
    # Los tests se importan entre si por nombre suelto.
    locales.update(n[:-3] for n in os.listdir(os.path.join(RAIZ, 'tests'))
                   if n.endswith('.py'))
    return locales


def _imports_de_terceros():
    """{modulo de primer nivel: [archivos que lo importan]} fuera de la
    libreria estandar y del proyecto."""
    locales = _modulos_locales()
    encontrados = {}
    for carpeta, subcarpetas, archivos in os.walk(RAIZ):
        subcarpetas[:] = [s for s in subcarpetas
                          if s not in IGNORAR and not s.startswith('.')]
        for nombre in archivos:
            if not nombre.endswith('.py'):
                continue
            ruta = os.path.join(carpeta, nombre)
            with open(ruta, encoding='utf-8') as archivo:
                arbol = ast.parse(archivo.read(), filename=ruta)
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Import):
                    modulos = [alias.name for alias in nodo.names]
                elif isinstance(nodo, ast.ImportFrom) and nodo.level == 0 and nodo.module:
                    modulos = [nodo.module]
                else:
                    continue
                for modulo in modulos:
                    primero = modulo.split('.')[0]
                    if primero in sys.stdlib_module_names or primero in locales:
                        continue
                    encontrados.setdefault(primero, []).append(
                        os.path.relpath(ruta, RAIZ))
    return encontrados


class TestRequirements(unittest.TestCase):

    def test_todo_paquete_importado_esta_fijado_en_requirements(self):
        """Lo que el codigo importa de forma directa se instala con la version
        que se probo, no con la que haya salida el dia del build.

        Una dependencia que entra "de rebote" (SQLAlchemy via
        Flask-SQLAlchemy, alembic via Flask-Migrate) no tiene version: el
        build de Render agarra la ultima, y cuando esa ultima cambia de
        comportamiento el deploy se rompe sin que haya cambiado una linea.
        """
        fijados = _requirements()
        distribuciones = metadata.packages_distributions()

        faltan = []
        for modulo, archivos in sorted(_imports_de_terceros().items()):
            dists = distribuciones.get(modulo)
            if not dists:
                faltan.append('%s (importado en %s) no esta instalado'
                              % (modulo, archivos[0]))
                continue
            if not any(fijados.get(_normalizar(d)) for d in dists):
                faltan.append('%s (paquete %s, importado en %s) no esta '
                              'fijado con == en requirements.txt'
                              % (modulo, '/'.join(dists), archivos[0]))

        self.assertEqual([], faltan)

    def test_sqlalchemy_queda_en_2_0(self):
        """SQLAlchemy 2.1 cambia el driver por defecto de `postgresql://` a
        psycopg 3; la app esta escrita y probada contra psycopg2."""
        version = _requirements().get('sqlalchemy')
        self.assertIsNotNone(version, 'SQLAlchemy tiene que estar fijado')
        self.assertTrue(version.startswith('2.0.'), version)

    def test_el_driver_de_postgres_esta_instalado_y_en_requirements(self):
        """La URL de Supabase no dice driver (`postgresql://...`): lo elige
        SQLAlchemy. Ese driver tiene que poder importarse y estar en
        requirements -- es exactamente lo que fallo en Render."""
        from sqlalchemy.engine import make_url

        dialecto = make_url('postgresql://u:p@localhost/db').get_dialect()
        dialecto.import_dbapi()  # ModuleNotFoundError si no esta instalado

        self.assertEqual('psycopg2', dialecto.driver)
        self.assertIn('psycopg2-binary', _requirements())


if __name__ == '__main__':
    unittest.main()
