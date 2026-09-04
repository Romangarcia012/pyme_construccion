# -*- coding: utf-8 -*-
"""Tests de FASE-CAJA-GENERAL-S2 (arreglos de gasto/ingreso + libro de caja).

    python -m unittest discover -s tests -v

Tres mitades, aunque sean tres.

LO QUE ESTABA ROTO

`editar_gasto` y `editar_ingreso` hacian `request.form['fecha']` contra una
plantilla que no tenia ese input: la fecha se mostraba en un cuadrito FUERA
del <form>, asi que el navegador nunca la mandaba. Toda edicion terminaba en
KeyError, que el `except Exception` de la ruta mostraba como "Error al editar:
'fecha'". Nunca lo noto nadie porque las dos tablas tienen cero filas en
produccion. Y el alta, por el otro lado, hardcodeaba `datetime.now().date()`:
era imposible cargar el historico del Excel, que arranca el 28/07.

DE QUIEN ES LA CAJA

Las dos tablas venian de la base vieja de la constructora colgadas de
`usuario_id` NOT NULL con cascade destructivo. Sobre eso, "libro unico" es una
mentira: cada usuario veria su mitad, y borrarse la cuenta borraba la caja de
la empresa entera. Es el mismo problema que FASE-AUDITORIA-S2 le arreglo al
historial, y los tests son deliberadamente parecidos a los de esa suite.

EL LIBRO

El saldo corriente de /caja-general es la columna E del Excel: el saldo de
cada fila es el de la anterior mas la entrada menos la salida. El test que
importa es el que mira fila por fila -- una suma total coincidiria igual con
una implementacion que recalcule todo en cada fila, que es la que no queremos.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import importlib.util
import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy as sa  # noqa: E402

from tests.ayuda_auth import request_anonimo  # noqa: E402
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


def _cargar_migracion():
    """La migracion de esta slice, importada por ruta.

    Se importa el modulo de verdad en vez de copiar la lista de categorias:
    asi el test afirma sobre lo que la migracion realmente siembra, y si
    manana alguien le agrega una categoria alla, aca no hay nada que
    sincronizar a mano.
    """
    ruta = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'migrations', 'versions',
        '73ce489bb2fd_fase_caja_general_s2_caja_de_la_empresa_.py')
    spec = importlib.util.spec_from_file_location('migracion_caja_general', ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


MIGRACION = _cargar_migracion()


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


class BaseCaja(unittest.TestCase):
    """Una empresa con DOS usuarios, mas una segunda empresa de control.

    Dos usuarios y no uno por el mismo motivo que en FASE-AUDITORIA-S2: con
    uno solo, filtrar por usuario y filtrar por empresa dan lo mismo y los
    tests pasarian sin probar nada.
    """

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

        # Las categorias las pone la MIGRACION, no el test: es la unica via
        # por la que van a existir en produccion.
        MIGRACION.sembrar_categorias(db.session.connection())
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

        El pop/push es el guard que documenta tests/ayuda_auth.py: flask_login
        cachea el usuario resuelto en `g`, que vive en el app_context del
        setUp, y sin esto dos requests seguidos del mismo test lo comparten.
        Muerde de verdad en el test de borrar la cuenta, que termina en
        `logout_user()`.
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

    def categoria_id(self, nombre):
        # Por empresa_id directo desde FASE-CATEGORIA-S1: el join a usuario que
        # habia aca era el mismo parche que tenia app.py, y se fue con el.
        return (Categoria.query
                .filter_by(empresa_id=self.empresa_id, nombre=nombre)
                .one().id)

    def cargar_gasto(self, usuario_id, descripcion, monto, fecha,
                     categoria='Publicidad'):
        return self.post(usuario_id, '/gasto/nuevo', data={
            'descripcion': descripcion,
            'monto': str(monto),
            'fecha': fecha,
            'categoria_id': str(self.categoria_id(categoria)),
        })

    def cargar_ingreso(self, usuario_id, descripcion, monto, fecha,
                       categoria='Venta'):
        return self.post(usuario_id, '/ingreso/nuevo', data={
            'descripcion': descripcion,
            'monto': str(monto),
            'fecha': fecha,
            'categoria_id': str(self.categoria_id(categoria)),
        })


# =========================================================================
# PARTE 1: lo que estaba roto
# =========================================================================

class TestEdicionQueRompiaSiempre(BaseCaja):
    """`request.form['fecha']` contra una plantilla sin ese input.

    Los dos primeros tests fallan contra la version vieja: el POST devolvia
    "Error al editar: 'fecha'" y no guardaba una sola letra.
    """

    def test_editar_gasto_no_rompe(self):
        self.cargar_gasto(self.roman_id, 'Meta Ads prueba', '21000.00',
                          '2026-08-27')
        gasto = Gasto.query.one()

        # La plantilla tiene que MANDAR la fecha: ese era el bug, no la ruta.
        formulario = self.texto_de(self.roman_id, '/gasto/editar/%d' % gasto.id)
        self.assertIn('name="fecha"', formulario,
                      'la plantilla de edicion no manda la fecha, asi que la '
                      'ruta va a volver a recibir un form sin ese campo')

        respuesta = self.post(self.roman_id, '/gasto/editar/%d' % gasto.id, data={
            'descripcion': 'Meta Ads (corregido)',
            'monto': '21500.00',
            'fecha': '2026-08-28',
            'categoria_id': str(self.categoria_id('Publicidad')),
        })
        # El listado al que redirige no pinta los mensajes flash, asi que
        # la senial de que no se rompio es haber SALIDO del formulario: la
        # rama de error hace flash y vuelve a renderizar editar_gasto.html.
        texto = respuesta.get_data(as_text=True)
        self.assertNotIn('Error al editar', texto)
        self.assertNotIn('Editar Gasto', texto,
                         'volvio al formulario de edicion: el guardado fallo')

        db.session.expire_all()
        gasto = db.session.get(Gasto, gasto.id)
        self.assertEqual(gasto.descripcion, 'Meta Ads (corregido)')
        self.assertEqual(gasto.monto, Decimal('21500.00'))
        self.assertEqual(gasto.fecha, date(2026, 8, 28))

    def test_editar_ingreso_no_rompe(self):
        self.cargar_ingreso(self.roman_id, 'Venta 3 tarjeteros', '22500.00',
                            '2026-08-23')
        ingreso = Ingreso.query.one()

        formulario = self.texto_de(self.roman_id,
                                   '/ingreso/editar/%d' % ingreso.id)
        self.assertIn('name="fecha"', formulario)

        respuesta = self.post(self.roman_id, '/ingreso/editar/%d' % ingreso.id,
                              data={
                                  'descripcion': 'Venta 3 tarjeteros presencial',
                                  'monto': '22500.00',
                                  'fecha': '2026-08-24',
                                  'categoria_id': str(self.categoria_id('Venta')),
                              })
        texto = respuesta.get_data(as_text=True)
        self.assertNotIn('Error al editar', texto)
        self.assertNotIn('Editar Ingreso', texto,
                         'volvio al formulario de edicion: el guardado fallo')

        db.session.expire_all()
        ingreso = db.session.get(Ingreso, ingreso.id)
        self.assertEqual(ingreso.descripcion, 'Venta 3 tarjeteros presencial')
        self.assertEqual(ingreso.fecha, date(2026, 8, 24))

    def test_editar_sin_fecha_deja_la_que_estaba(self):
        """Un campo que no vino no puede significar "movela a hoy"."""
        self.cargar_gasto(self.roman_id, 'Aduana tax', '229000.00',
                          '2026-08-14', categoria='Aduana/Impuestos')
        gasto = Gasto.query.one()

        self.post(self.roman_id, '/gasto/editar/%d' % gasto.id, data={
            'descripcion': 'Aduana tax tarjeteros',
            'monto': '229000.00',
            'categoria_id': str(self.categoria_id('Aduana/Impuestos')),
        })

        db.session.expire_all()
        self.assertEqual(db.session.get(Gasto, gasto.id).fecha,
                         date(2026, 8, 14))


class TestFechaEnElAlta(BaseCaja):
    """Sin esto no hay forma de cargar el historico del Excel."""

    def test_crear_con_fecha_pasada(self):
        self.cargar_gasto(self.roman_id, 'COMPRA 300 TARJETEROS 603USD',
                          '969285.00', '2026-07-28',
                          categoria='Compra de mercadería')

        gasto = Gasto.query.one()
        self.assertEqual(gasto.fecha, date(2026, 7, 28),
                         'el alta guardo la fecha de hoy en vez de la que se '
                         'cargo: vuelve a estar hardcodeada')
        self.assertNotEqual(gasto.fecha, datetime.now().date())
        self.assertEqual(gasto.monto, Decimal('969285.00'))

    def test_el_alta_ofrece_el_campo(self):
        self.assertIn('name="fecha"',
                      self.texto_de(self.roman_id, '/gasto/nuevo'))
        self.assertIn('name="fecha"',
                      self.texto_de(self.roman_id, '/ingreso/nuevo'))

    def test_fecha_invalida_avisa_y_no_guarda(self):
        respuesta = self.post(self.roman_id, '/gasto/nuevo', data={
            'descripcion': 'Fecha imposible',
            'monto': '100.00',
            'fecha': '31/07/2026',
            'categoria_id': str(self.categoria_id('Publicidad')),
        })
        self.assertIn('La fecha no es válida',
                      respuesta.get_data(as_text=True))
        self.assertEqual(Gasto.query.count(), 0)


class TestValidacionDeIngreso(BaseCaja):
    """`nuevo_ingreso` no validaba la categoria: reventaba con KeyError y el
    except generico lo mostraba como "Error al agregar ingreso: 'categoria_id'".
    """

    def test_ingreso_sin_categoria_avisa_en_castellano(self):
        respuesta = self.post(self.roman_id, '/ingreso/nuevo', data={
            'descripcion': 'Venta suelta',
            'monto': '7500.00',
            'fecha': '2026-08-28',
        })
        texto = respuesta.get_data(as_text=True)
        self.assertIn('Debes seleccionar una categoría', texto)
        self.assertNotIn("Error al agregar ingreso: 'categoria_id'", texto,
                         'el KeyError sigue saliendo crudo a la pantalla')
        self.assertEqual(Ingreso.query.count(), 0)

    def test_ingreso_con_categoria_de_otra_empresa_no_entra(self):
        ajena = Categoria(nombre='Ajena', tipo='ingreso',
                          empresa_id=self.otra_empresa_id,
                          usuario_id=self.ajeno_id)
        db.session.add(ajena)
        db.session.commit()

        self.post(self.roman_id, '/ingreso/nuevo', data={
            'descripcion': 'Colado',
            'monto': '1.00',
            'fecha': '2026-08-28',
            'categoria_id': str(ajena.id),
        })
        self.assertEqual(Ingreso.query.count(), 0)

    def test_monto_invalido_avisa(self):
        respuesta = self.post(self.roman_id, '/ingreso/nuevo', data={
            'descripcion': 'Venta',
            'monto': 'mil quinientos',
            'fecha': '2026-08-28',
            'categoria_id': str(self.categoria_id('Venta')),
        })
        self.assertIn('El monto debe ser un número válido',
                      respuesta.get_data(as_text=True))
        self.assertEqual(Ingreso.query.count(), 0)


class TestFiltroDeFechasEnSQL(BaseCaja):
    """Los listados traian TODA la tabla y filtraban la lista en Python."""

    def setUp(self):
        super(TestFiltroDeFechasEnSQL, self).setUp()
        for dia, monto in ((7, '100.00'), (14, '200.00'), (28, '300.00')):
            self.cargar_gasto(self.roman_id, 'Gasto del %d' % dia, monto,
                              '2026-08-%02d' % dia)

    def _selects_de(self, ruta):
        """Las sentencias que la ruta le manda de verdad a la base."""
        capturadas = []

        def espia(conn, cursor, sentencia, parametros, contexto, muchos):
            capturadas.append(sentencia)

        motor = db.engine
        sa.event.listen(motor, 'before_cursor_execute', espia)
        try:
            respuesta = self.get(self.roman_id, ruta)
        finally:
            sa.event.remove(motor, 'before_cursor_execute', espia)
        return respuesta.get_data(as_text=True), capturadas

    def test_listar_filtra_en_sql_no_en_python(self):
        texto, sentencias = self._selects_de(
            '/gasto/listar?fecha_inicio=2026-08-10&fecha_fin=2026-08-20')

        # El resultado correcto...
        self.assertIn('Gasto del 14', texto)
        self.assertNotIn('Gasto del 7', texto)
        self.assertNotIn('Gasto del 28', texto)

        # ...y que lo haya conseguido la BASE, no un `for` despues.
        sobre_gasto = [s for s in sentencias
                       if 'FROM gasto' in s and 'gasto.fecha' in s]
        self.assertTrue(sobre_gasto, 'no salio ningun SELECT sobre gasto')
        self.assertTrue(
            any('gasto.fecha >=' in s and 'gasto.fecha <=' in s
                for s in sobre_gasto),
            'el SELECT sobre gasto no lleva el rango en el WHERE: el filtro '
            'volvio a hacerse en Python sobre la tabla entera.\n%s'
            % '\n'.join(sobre_gasto))

    def test_ingresos_tambien(self):
        for dia in (7, 14, 28):
            self.cargar_ingreso(self.roman_id, 'Ingreso del %d' % dia,
                                '50.00', '2026-08-%02d' % dia)

        texto, sentencias = self._selects_de(
            '/ingreso/listar?fecha_inicio=2026-08-10&fecha_fin=2026-08-20')
        self.assertIn('Ingreso del 14', texto)
        self.assertNotIn('Ingreso del 28', texto)
        self.assertTrue(
            any('ingreso.fecha >=' in s and 'ingreso.fecha <=' in s
                for s in sentencias))

    def test_una_fecha_basura_no_voltea_la_pantalla(self):
        respuesta = self.get(self.roman_id, '/gasto/listar?fecha_inicio=ayer')
        self.assertEqual(respuesta.status_code, 200)
        self.assertIn('Gasto del 7', respuesta.get_data(as_text=True))


# =========================================================================
# PARTE 2: de quien es la caja
# =========================================================================

class TestLaCajaEsDeLaEmpresa(BaseCaja):

    def test_libro_filtra_por_empresa_no_usuario(self):
        """Dos usuarios de la misma empresa ven EL MISMO libro."""
        self.cargar_gasto(self.roman_id, 'Publicidad Instagram', '12000.00',
                          '2026-08-23')
        self.cargar_ingreso(self.nachi_id, 'Venta 2 tarjeteros MELI',
                            '10088.20', '2026-08-19')

        de_roman = self.texto_de(self.roman_id, '/caja-general')
        de_nachi = self.texto_de(self.nachi_id, '/caja-general')

        for texto, quien in ((de_roman, 'Roman'), (de_nachi, 'Nachi')):
            self.assertIn('Publicidad Instagram', texto,
                          '%s no ve el gasto que cargo Roman' % quien)
            self.assertIn('Venta 2 tarjeteros MELI', texto,
                          '%s no ve el ingreso que cargo Nachi' % quien)

    def test_la_empresa_ajena_no_ve_nada(self):
        self.cargar_gasto(self.roman_id, 'Compra 500 microfonos', '758000.00',
                          '2026-08-24', categoria='Compra de mercadería')

        texto = self.texto_de(self.ajeno_id, '/caja-general')
        self.assertNotIn('Compra 500 microfonos', texto)
        self.assertIn('Todavía no hay movimientos', texto)

    def test_los_listados_tambien_son_de_la_empresa(self):
        self.cargar_gasto(self.roman_id, 'Gastos para empaquetar', '3000.00',
                          '2026-08-20', categoria='Envíos y empaque')

        self.assertIn('Gastos para empaquetar',
                      self.texto_de(self.nachi_id, '/gasto/listar'))
        self.assertNotIn('Gastos para empaquetar',
                         self.texto_de(self.ajeno_id, '/gasto/listar'))

    def test_nachi_puede_editar_lo_que_cargo_roman(self):
        self.cargar_gasto(self.roman_id, 'Meta Ads', '21000.00', '2026-08-27')
        gasto = Gasto.query.one()

        respuesta = self.post(self.nachi_id, '/gasto/editar/%d' % gasto.id,
                              data={
                                  'descripcion': 'Meta Ads prueba',
                                  'monto': '21000.00',
                                  'fecha': '2026-08-27',
                                  'categoria_id': str(
                                      self.categoria_id('Publicidad')),
                              })
        self.assertNotIn('No tienes permiso', respuesta.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(db.session.get(Gasto, gasto.id).descripcion,
                         'Meta Ads prueba')

    def test_la_empresa_ajena_no_puede_editar(self):
        self.cargar_gasto(self.roman_id, 'Meta Ads', '21000.00', '2026-08-27')
        gasto = Gasto.query.one()

        self.post(self.ajeno_id, '/gasto/editar/%d' % gasto.id, data={
            'descripcion': 'Robado',
            'monto': '1.00',
            'fecha': '2026-08-27',
        })
        db.session.expire_all()
        self.assertEqual(db.session.get(Gasto, gasto.id).descripcion,
                         'Meta Ads')


class TestBorrarLaCuentaNoBorraLaCaja(BaseCaja):
    """Era la accion mas destructiva del sistema: se llevaba puesto el libro
    entero de la empresa. Mismo arreglo que el historial en FASE-AUDITORIA-S2.
    """

    def test_borrar_usuario_no_borra_la_caja(self):
        self.cargar_gasto(self.nachi_id, 'Programa Mercado Libre', '45000.00',
                          '2026-08-19')
        self.cargar_ingreso(self.nachi_id, 'Venta 7 tarjeteros MELI',
                            '35308.70', '2026-08-20')

        self.post(self.nachi_id, '/cuenta/eliminar')

        self.assertIsNone(db.session.get(Usuario, self.nachi_id),
                          'la cuenta tendria que haberse borrado')

        gasto = Gasto.query.one()
        ingreso = Ingreso.query.one()
        for fila, etiqueta in ((gasto, 'gasto'), (ingreso, 'ingreso')):
            self.assertIsNone(
                fila.usuario_id,
                'la FK del %s tiene que quedar en NULL, no apuntando a un '
                'usuario que ya no existe' % etiqueta)
            self.assertEqual(
                fila.empresa_id, self.empresa_id,
                'el %s tiene que seguir siendo de la empresa' % etiqueta)

    def test_roman_sigue_viendo_la_caja_de_la_cuenta_borrada(self):
        """Es el punto de conservarlas: que se puedan LEER despues."""
        self.cargar_gasto(self.nachi_id, 'Regalo y Sorteo Homo', '15980.54',
                          '2026-08-27')
        self.post(self.nachi_id, '/cuenta/eliminar')

        self.assertIn('Regalo y Sorteo Homo',
                      self.texto_de(self.roman_id, '/caja-general'))

    def test_las_categorias_sobreviven_al_borrado(self):
        """Antes se REASIGNABAN a un heredero; desde FASE-CATEGORIA-S1 no hace
        falta tocarlas, porque el duenio es la empresa. Lo que importa es lo
        mismo de siempre: la empresa no se queda sin vocabulario."""
        duenio = db.session.get(
            Categoria, self.categoria_id('Publicidad')).usuario_id

        self.post(duenio, '/cuenta/eliminar')

        categorias = Categoria.query.filter_by(empresa_id=self.empresa_id).all()
        self.assertEqual(len(categorias), len(MIGRACION.CATEGORIAS_KORVO))


# =========================================================================
# PARTE 3: las categorias Korvo
# =========================================================================

class TestCategoriasKorvo(BaseCaja):

    def test_categorias_korvo_sembradas(self):
        esperadas = {nombre for nombre, _ in MIGRACION.CATEGORIAS_KORVO}
        self.assertEqual(len(esperadas), 7)

        for empresa_id in (self.empresa_id, self.otra_empresa_id):
            cargadas = {c.nombre for c in Categoria.query
                        .filter_by(empresa_id=empresa_id).all()}
            self.assertEqual(cargadas, esperadas,
                             'a la empresa %d le faltan categorias: %s'
                             % (empresa_id, esperadas - cargadas))

    def test_los_tipos_son_los_que_esperan_las_pantallas(self):
        por_nombre = dict(MIGRACION.CATEGORIAS_KORVO)
        self.assertEqual(por_nombre['Venta'], 'ingreso')
        self.assertEqual(por_nombre['Aporte de capital (socios)'], 'ingreso')
        self.assertEqual(por_nombre['Compra de mercadería'], 'gasto')

        # Y que las pantallas las ofrezcan de verdad, cada una las suyas.
        alta_gasto = self.texto_de(self.roman_id, '/gasto/nuevo')
        self.assertIn('Compra de mercadería', alta_gasto)
        self.assertNotIn('Aporte de capital', alta_gasto)

        alta_ingreso = self.texto_de(self.roman_id, '/ingreso/nuevo')
        self.assertIn('Aporte de capital (socios)', alta_ingreso)
        self.assertNotIn('Compra de mercadería', alta_ingreso)

    def test_sembrar_dos_veces_no_duplica(self):
        MIGRACION.sembrar_categorias(db.session.connection())
        db.session.commit()

        self.assertEqual(Categoria.query.filter_by(nombre='Venta').count(), 2,
                         'una fila por empresa, y hay dos empresas')

    def test_las_ve_el_socio_que_no_las_sembro(self):
        """La semilla la crea UN usuario, pero la categoria es de la empresa."""
        self.assertIn('Publicidad', self.texto_de(self.nachi_id, '/gasto/nuevo'))
        self.assertIn('Publicidad',
                      self.texto_de(self.nachi_id, '/categorias'))


# =========================================================================
# PARTE 4: el libro con saldo corriente
# =========================================================================

class TestSaldoCorriente(BaseCaja):
    """La columna E del Excel: `=SUM(E_anterior + entrada - salida)`."""

    def setUp(self):
        super(TestSaldoCorriente, self).setUp()
        # Cuatro movimientos, fechas distintas, cargados FUERA DE ORDEN a
        # proposito: el libro tiene que ordenarlos el.
        self.cargar_ingreso(self.roman_id, 'Venta 2 tarjeteros MELI',
                            '10088.20', '2026-08-19')
        self.cargar_gasto(self.roman_id, 'COMPRA 300 TARJETEROS', '969285.00',
                          '2026-07-28', categoria='Compra de mercadería')
        self.cargar_gasto(self.nachi_id, 'Publicidad Instagram', '12000.00',
                          '2026-08-23')
        self.cargar_ingreso(self.nachi_id, 'Aporte de los socios',
                            '1500000.00', '2026-07-27',
                            categoria='Aporte de capital (socios)')

    def _libro_crudo(self):
        """Los movimientos que arma la ruta, antes de la plantilla.

        Se espia `render_template` en vez de leer numeros del HTML: lo que hay
        que verificar es el acumulado, y en la pantalla llega ya formateado.
        """
        capturado = {}
        import app as modulo_app
        original = modulo_app.render_template

        def espia(plantilla, **contexto):
            if plantilla == 'caja_general.html':
                capturado.update(contexto)
            return original(plantilla, **contexto)

        modulo_app.render_template = espia
        try:
            self.get(self.roman_id, '/caja-general')
        finally:
            modulo_app.render_template = original
        return capturado['movimientos']

    def test_saldo_corriente_correcto(self):
        texto = self.texto_de(self.roman_id, '/caja-general')

        # El orden: 27/07, 28/07, 19/08, 23/08.
        posiciones = [texto.index(x) for x in ('Aporte de los socios',
                                              'COMPRA 300 TARJETEROS',
                                              'Venta 2 tarjeteros MELI',
                                              'Publicidad Instagram')]
        self.assertEqual(posiciones, sorted(posiciones),
                         'el libro no esta ordenado por fecha ascendente')

        #   1500000.00
        #   1500000.00 -  969285.00 =  530715.00
        #    530715.00 +   10088.20 =  540803.20
        #    540803.20 -   12000.00 =  528803.20
        for saldo in ('1500000.00', '530715.00', '540803.20', '528803.20'):
            self.assertIn(saldo, texto,
                          'falta el saldo acumulado %s: el saldo de cada fila '
                          'no es el de la anterior mas/menos el movimiento'
                          % saldo)

        # Los totales de arriba, que en el Excel son la fila 2.
        self.assertIn('1510088.20', texto, 'total de entradas')
        self.assertIn('981285.00', texto, 'total de salidas')

    def test_el_saldo_se_acumula_no_se_recalcula(self):
        """Un total suelto coincidiria igual; lo que distingue una cosa de la
        otra es que CADA fila lleve el acumulado hasta ella."""
        movimientos = self._libro_crudo()
        self.assertEqual(len(movimientos), 4)
        saldo = Decimal('0.00')
        for m in movimientos:
            saldo += m['entrada'] - m['salida']
            self.assertEqual(m['saldo'], saldo,
                             'la fila "%s" tiene saldo %s y el acumulado hasta '
                             'ella es %s' % (m['descripcion'], m['saldo'], saldo))

    def test_cada_fila_dice_su_categoria(self):
        """Lo que el Excel no tiene: alla la categoria vive adentro del texto
        libre de la columna DIARIO."""
        texto = self.texto_de(self.roman_id, '/caja-general')
        for categoria in ('Compra de mercadería', 'Publicidad', 'Venta',
                          'Aporte de capital (socios)'):
            self.assertIn(categoria, texto)

    def test_el_libro_vacio_no_explota(self):
        db.session.query(Gasto).delete()
        db.session.query(Ingreso).delete()
        db.session.commit()

        texto = self.texto_de(self.roman_id, '/caja-general')
        self.assertIn('Todavía no hay movimientos', texto)
        self.assertIn('0.00', texto)


class TestAuthDelLibro(BaseCaja):

    def test_caja_general_pide_login(self):
        respuesta = request_anonimo(self.ctx, 'get', '/caja-general')
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers.get('Location', ''))


if __name__ == '__main__':
    unittest.main()
