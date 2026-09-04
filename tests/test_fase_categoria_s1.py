# -*- coding: utf-8 -*-
"""Tests de FASE-CATEGORIA-S1 (la categoria es de la empresa, no del usuario).

    python -m unittest discover -s tests -v

LA TERCERA TABLA CON EL MISMO PROBLEMA

`historial` (FASE-AUDITORIA-S2) y `gasto`/`ingreso` (FASE-CAJA-GENERAL-S2)
venian colgados de `usuario_id` NOT NULL con cascade destructivo. `categoria`
era la ultima, y estos tests son deliberadamente parecidos a los de esas dos
suites: la misma empresa con DOS usuarios, y una empresa de control.

QUE SE PRUEBA Y QUE NO

No se prueba que el join a `usuario` haya desaparecido -- eso lo dice el diff,
no un test. Se prueba lo que ese join intentaba lograr y no lograba del todo:
que los dos socios vean el mismo vocabulario, y que borrarse una cuenta no lo
toque. El test de la reasignacion es el interesante: antes las categorias
CAMBIABAN de usuario al borrar una cuenta (el parche en `eliminar_cuenta`);
ahora no cambia nada porque nunca quedan huerfanas, y eso es lo que se afirma.

Dos usuarios y no uno, otra vez, porque con uno solo filtrar por usuario y
filtrar por empresa dan lo mismo y los tests pasarian sin probar nada.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy as sa  # noqa: E402

from app import app  # noqa: E402
from models import (  # noqa: E402
    Categoria,
    Empresa,
    Gasto,
    Usuario,
    db,
)

ENGINE_PRODUCTIVO = None


def _cargar_migracion(archivo, nombre):
    ruta = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'migrations', 'versions', archivo)
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


# La semilla sigue viviendo en la migracion de FASE-CAJA-GENERAL-S2: esta slice
# le cambio el duenio a las categorias, no el vocabulario.
SEMILLA = _cargar_migracion(
    '73ce489bb2fd_fase_caja_general_s2_caja_de_la_empresa_.py',
    'migracion_caja_general')
MIGRACION = _cargar_migracion(
    '3e59146576ed_fase_categoria_s1_la_categoria_es_de_la_.py',
    'migracion_categoria')


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


class BaseCategoria(unittest.TestCase):
    """Una empresa con dos socios, y una segunda empresa de control."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Korvo')
        self.otra_empresa = Empresa(nombre='Empresa Ajena')
        db.session.add_all([self.empresa, self.otra_empresa])
        db.session.flush()

        self.roman = Usuario(nombre='Roman', email='roman@test.local',
                             empresa_id=self.empresa.id, verificado=True)
        self.roman.set_password('irrelevante')
        self.nachi = Usuario(nombre='Nachi', email='nachi@test.local',
                             empresa_id=self.empresa.id, verificado=True)
        self.nachi.set_password('irrelevante')
        self.ajeno = Usuario(nombre='Ajeno', email='ajeno@test.local',
                             empresa_id=self.otra_empresa.id, verificado=True)
        self.ajeno.set_password('irrelevante')
        db.session.add_all([self.roman, self.nachi, self.ajeno])
        db.session.commit()

        # Las categorias las pone la MIGRACION, no el test: es la unica via por
        # la que van a existir en produccion.
        SEMILLA.sembrar_categorias(db.session.connection())
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.otra_empresa_id = self.otra_empresa.id
        self.roman_id = self.roman.id
        self.nachi_id = self.nachi.id
        self.ajeno_id = self.ajeno.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def pedir(self, usuario_id, metodo, ruta, **kwargs):
        """Un request logueado, en su PROPIO app_context.

        El pop/push es el guard de tests/ayuda_auth.py: flask_login cachea el
        usuario resuelto en `g`, que vive en el app_context del setUp. Muerde
        de verdad en los tests que terminan en `logout_user()`.
        """
        self.ctx.pop()
        try:
            cli = app.test_client()
            with cli.session_transaction() as sesion:
                sesion['_user_id'] = str(usuario_id)
                sesion['_fresh'] = True
            return getattr(cli, metodo)(ruta, **kwargs)
        finally:
            self.ctx.push()

    def get(self, usuario_id, ruta):
        return self.pedir(usuario_id, 'get', ruta, follow_redirects=True)

    def post(self, usuario_id, ruta, **kwargs):
        kwargs.setdefault('follow_redirects', True)
        return self.pedir(usuario_id, 'post', ruta, **kwargs)

    def texto_de(self, usuario_id, ruta):
        return self.get(usuario_id, ruta).get_data(as_text=True)

    def nombres_de(self, empresa_id):
        return {c.nombre for c in
                Categoria.query.filter_by(empresa_id=empresa_id).all()}


# =========================================================================
# El filtro: por empresa, no por usuario
# =========================================================================

class TestFiltroPorEmpresa(BaseCategoria):

    def test_categoria_filtra_por_empresa_no_por_usuario(self):
        """Los dos socios ven el MISMO vocabulario.

        Reemplaza lo que antes sostenia el join a `usuario`. Se mira por las
        pantallas y no por la query, porque el sintoma que importa es el del
        socio que abre /gasto/nuevo y no encuentra con que etiquetar.
        """
        # Una categoria que crea Roman tiene que aparecerle a Nachi, que no la
        # creo, y no aparecerle al de la otra empresa.
        self.post(self.roman_id, '/nueva-categoria',
                  data={'nombre': 'Flete interno', 'tipo': 'gasto'})

        for usuario_id, quien in ((self.roman_id, 'Roman'),
                                  (self.nachi_id, 'Nachi')):
            for ruta in ('/categorias', '/gasto/nuevo'):
                self.assertIn('Flete interno', self.texto_de(usuario_id, ruta),
                              '%s no ve la categoria de su empresa en %s'
                              % (quien, ruta))

        self.assertNotIn('Flete interno',
                         self.texto_de(self.ajeno_id, '/categorias'),
                         'la categoria se filtro a la otra empresa')

    def test_la_categoria_nueva_nace_con_la_empresa_de_quien_la_creo(self):
        """Y con el usuario tambien: quien la tipeo sigue siendo un dato."""
        self.post(self.nachi_id, '/nueva-categoria',
                  data={'nombre': 'Flete interno', 'tipo': 'gasto'})

        creada = Categoria.query.filter_by(nombre='Flete interno').one()
        self.assertEqual(creada.empresa_id, self.empresa_id)
        self.assertEqual(creada.usuario_id, self.nachi_id)

    def test_no_se_puede_editar_ni_borrar_la_categoria_de_otra_empresa(self):
        """El chequeo de pertenencia tambien dejo de pasar por el usuario."""
        ajena = Categoria.query.filter_by(empresa_id=self.otra_empresa_id,
                                          nombre='Publicidad').one()

        self.post(self.roman_id, '/categoria/editar/%d' % ajena.id,
                  data={'nombre': 'Secuestrada'})
        self.post(self.roman_id, '/categoria/eliminar/%d' % ajena.id)

        sigue = db.session.get(Categoria, ajena.id)
        self.assertIsNotNone(sigue, 'borro una categoria de otra empresa')
        self.assertEqual(sigue.nombre, 'Publicidad',
                         'edito una categoria de otra empresa')


# =========================================================================
# El borrado de cuenta: la categoria sobrevive, y nadie la reasigna
# =========================================================================

class TestBorrarCuenta(BaseCategoria):

    def test_borrar_usuario_no_borra_las_categorias(self):
        """Era el cascade='all, delete-orphan'. Ahora la FK queda en NULL."""
        antes = self.nombres_de(self.empresa_id)
        self.assertEqual(len(antes), len(SEMILLA.CATEGORIAS_KORVO))

        self.post(self.roman_id, '/cuenta/eliminar')

        self.assertEqual(self.nombres_de(self.empresa_id), antes,
                         'borrar la cuenta se llevo puesto el vocabulario')

    def test_no_se_reasignan_categorias_al_borrar_cuenta(self):
        """El parche de `eliminar_cuenta` buscaba un heredero y le pasaba las
        categorias del que se iba. Ya no esta, y no hace falta: las que eran de
        Roman quedan con usuario_id en NULL -- NO pasan a Nachi -- y siguen
        colgadas de la empresa, que es lo unico que las hace visibles."""
        de_roman = {c.id for c in
                    Categoria.query.filter_by(usuario_id=self.roman_id).all()}
        self.assertTrue(de_roman, 'la semilla tiene que ser de alguien')

        self.post(self.roman_id, '/cuenta/eliminar')

        for categoria_id in de_roman:
            categoria = db.session.get(Categoria, categoria_id)
            self.assertIsNotNone(categoria)
            self.assertIsNone(categoria.usuario_id,
                              'la reasignacion sigue viva: la categoria %d '
                              'cambio de duenio en vez de quedar en NULL'
                              % categoria_id)
            self.assertEqual(categoria.empresa_id, self.empresa_id)

        self.assertEqual(
            Categoria.query.filter_by(usuario_id=self.nachi_id).count(), 0,
            'alguien le heredo las categorias a Nachi')

    def test_nachi_sigue_pudiendo_cargar_un_gasto_despues(self):
        """Es el punto de conservarlas: que la empresa siga operando."""
        self.post(self.roman_id, '/cuenta/eliminar')

        publicidad = Categoria.query.filter_by(
            empresa_id=self.empresa_id, nombre='Publicidad').one()
        self.post(self.nachi_id, '/gasto/nuevo', data={
            'descripcion': 'Ads de septiembre',
            'monto': '1500.00',
            'fecha': '2026-09-01',
            'categoria_id': str(publicidad.id),
        })

        gasto = Gasto.query.filter_by(descripcion='Ads de septiembre').one()
        self.assertEqual(gasto.categoria_id, publicidad.id)

    def test_los_gastos_de_la_cuenta_borrada_conservan_su_etiqueta(self):
        """El cascade viejo borraba la categoria y dejaba sin etiqueta a los
        gastos que FASE-CAJA-GENERAL-S2 acababa de salvar."""
        publicidad = Categoria.query.filter_by(
            empresa_id=self.empresa_id, nombre='Publicidad').one()
        self.post(self.roman_id, '/gasto/nuevo', data={
            'descripcion': 'Regalo y Sorteo Homo',
            'monto': '15980.54',
            'fecha': '2026-08-27',
            'categoria_id': str(publicidad.id),
        })

        self.post(self.roman_id, '/cuenta/eliminar')

        gasto = Gasto.query.filter_by(descripcion='Regalo y Sorteo Homo').one()
        self.assertIsNone(gasto.usuario_id)
        self.assertIsNotNone(gasto.categoria, 'el gasto quedo sin etiqueta')
        self.assertEqual(gasto.categoria.nombre, 'Publicidad')


# =========================================================================
# El backfill: las siete sembradas caen en la empresa correcta
# =========================================================================

class TestBackfill(unittest.TestCase):
    """Corre el backfill de verdad contra el esquema VIEJO.

    No hereda de BaseCategoria: necesita una tabla `categoria` como la de antes
    de esta revision (`empresa_id` inexistente, `usuario_id` NOT NULL) para que
    el UPDATE tenga algo que rellenar. Contra el modelo de hoy la columna ya
    viene puesta por el ORM y el test no probaria nada.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        # Se vuelve a la forma vieja de `categoria` a mano. Es SQLite: se tira
        # la tabla y se recrea sin `empresa_id`, que es lo que la migracion se
        # va a encontrar en Supabase.
        conexion = db.session.connection()
        conexion.execute(sa.text('DROP TABLE categoria'))
        conexion.execute(sa.text(
            'CREATE TABLE categoria ('
            '  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,'
            '  nombre VARCHAR(100) NOT NULL,'
            '  tipo VARCHAR(20) NOT NULL,'
            '  usuario_id INTEGER NOT NULL REFERENCES usuario (id),'
            '  fecha_creacion DATETIME)'))

        self.empresa = Empresa(nombre='Korvo')
        self.otra_empresa = Empresa(nombre='Empresa Ajena')
        db.session.add_all([self.empresa, self.otra_empresa])
        db.session.flush()

        for nombre, correo, empresa in (
                ('Roman', 'roman@test.local', self.empresa),
                ('Nachi', 'nachi@test.local', self.empresa),
                ('Ajeno', 'ajeno@test.local', self.otra_empresa)):
            usuario = Usuario(nombre=nombre, email=correo,
                              empresa_id=empresa.id, verificado=True)
            usuario.set_password('irrelevante')
            db.session.add(usuario)
        db.session.commit()

        # La semilla, contra el esquema viejo: cuelga de un usuario por empresa.
        SEMILLA.sembrar_categorias(db.session.connection())
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.otra_empresa_id = self.otra_empresa.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _correr_backfill(self):
        conexion = db.session.connection()
        conexion.execute(sa.text(
            'ALTER TABLE categoria ADD COLUMN empresa_id INTEGER'))
        MIGRACION._backfill(conexion)

    def test_categorias_sembradas_tienen_empresa_correcta(self):
        self._correr_backfill()

        filas = db.session.connection().execute(sa.text(
            'SELECT c.nombre, c.empresa_id, u.empresa_id '
            '  FROM categoria c JOIN usuario u ON u.id = c.usuario_id')).all()

        self.assertEqual(len(filas), 2 * len(SEMILLA.CATEGORIAS_KORVO),
                         'las siete por empresa, y hay dos empresas')
        for nombre, empresa_backfilleada, empresa_del_usuario in filas:
            self.assertEqual(empresa_backfilleada, empresa_del_usuario,
                             '%s quedo en la empresa equivocada' % nombre)

        por_empresa = db.session.connection().execute(sa.text(
            'SELECT empresa_id, count(*) FROM categoria GROUP BY empresa_id'))
        self.assertEqual(
            dict(por_empresa.all()),
            {self.empresa_id: len(SEMILLA.CATEGORIAS_KORVO),
             self.otra_empresa_id: len(SEMILLA.CATEGORIAS_KORVO)})

    def test_el_guard_corta_en_vez_de_inventar_una_empresa(self):
        """Una categoria apuntando a un usuario que no existe no se adivina."""
        conexion = db.session.connection()
        conexion.execute(sa.text(
            "INSERT INTO categoria (nombre, tipo, usuario_id) "
            "VALUES ('Huerfana', 'gasto', 9999)"))

        with self.assertRaises(RuntimeError) as capturado:
            self._correr_backfill()

        self.assertIn('sin empresa', str(capturado.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
