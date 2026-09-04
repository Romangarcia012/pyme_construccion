# -*- coding: utf-8 -*-
"""Tests de FASE-AUDITORIA-S2 (arreglos criticos + hook de auditoria).

    python -m unittest discover -s tests -v

Dos mitades.

La primera prueba los tres arreglos de cosas que estaban rotas y que no
dependen del hook: el historial ya no se borra cuando se borra la cuenta, la
pantalla filtra por empresa y no por usuario, y el boton de limpiar deja
constancia en vez de barrer el rastro. Mas la cuarta: que el
`registrar_cambio` roto de eva_utils.py ya no existe.

La segunda prueba el hook. Lo que se afirma ahi es que las pantallas de
S3-COSTO y S3-COMISION generan historial **sin que se haya tocado una sola
linea de esas rutas** -- que es todo el punto de haber elegido un hook por
encima de seguir agregando llamadas a mano.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eva_utils  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    Categoria,
    CredencialCanal,
    Empresa,
    Gasto,
    Historial,
    Pedido,
    Producto,
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


class BaseAuditoria(unittest.TestCase):
    """Una empresa con DOS usuarios -- Roman y el que hara de Nachi.

    Dos y no uno a proposito: casi todo lo que esta slice arregla solo se
    puede ver con mas de una persona en la misma empresa. Con un solo usuario,
    filtrar por usuario y filtrar por empresa dan lo mismo, y el test pasaria
    sin probar nada.

    Y una SEGUNDA empresa, para confirmar que el filtro nuevo no se pasa de
    largo y muestra lo ajeno.
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
                             empresa_id=self.empresa.id, rol='usuario',
                             verificado=True)
        self.roman.set_password('irrelevante')
        self.nachi = Usuario(nombre='Nachi', email='nachi@test.local',
                             empresa_id=self.empresa.id, rol='usuario',
                             verificado=True)
        self.nachi.set_password('irrelevante')
        self.ajeno = Usuario(nombre='Ajeno', email='ajeno@test.local',
                             empresa_id=self.otra_empresa.id, rol='usuario',
                             verificado=True)
        self.ajeno.set_password('irrelevante')
        db.session.add_all([self.roman, self.nachi, self.ajeno])

        self.canal = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                nombre='Korvo TN', activo=True,
                                id_tienda_externo='9999')
        db.session.add(self.canal)
        db.session.flush()

        self.producto = Producto(empresa_id=self.empresa.id, sku='TARJ-NEGRO',
                                 nombre='Tarjetero (Negro)', stock=100,
                                 costo_unitario=Decimal('3230.81'),
                                 precio_lista=Decimal('7490.00'))
        db.session.add(self.producto)

        self.pedido = Pedido(empresa_id=self.empresa.id, canal_id=self.canal.id,
                             id_externo='2060210312',
                             fecha_pedido=datetime(2026, 8, 20, 10, 0),
                             estado='open', total=Decimal('7490.00'),
                             total_bruto=Decimal('7490.00'))
        db.session.add(self.pedido)
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.otra_empresa_id = self.otra_empresa.id
        self.roman_id = self.roman.id
        self.nachi_id = self.nachi.id
        self.ajeno_id = self.ajeno.id
        self.producto_id = self.producto.id
        self.pedido_id = self.pedido.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def pedir(self, usuario_id, metodo, ruta, **kwargs):
        """Un request logueado, en su PROPIO app_context.

        El pop/push del contexto es el mismo guard que documenta
        tests/ayuda_auth.py, por el mismo motivo y al reves: flask_login cachea
        el usuario resuelto en `g`, que vive en el app_context que pusheo el
        setUp. Sin el pop, dos requests seguidos del mismo test comparten ese
        cache.

        Aca muerde de verdad: `/cuenta/eliminar` termina en `logout_user()`,
        que deja `g._login_user` vacio. El request siguiente -- aunque salga de
        un cliente nuevo y con su cookie de sesion puesta -- se encontraba ese
        cache y rebotaba al login. El test fallaba por el contexto compartido,
        no por la ruta.

        La base sobrevive al pop: el engine de SQLite en memoria usa StaticPool,
        o sea una sola conexion para toda la app, no una por contexto.
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

    def historial_de(self, empresa_id=None, tipo=None):
        consulta = Historial.query.filter_by(
            empresa_id=empresa_id or self.empresa_id)
        if tipo:
            consulta = consulta.filter_by(tipo=tipo)
        return consulta.order_by(Historial.id).all()


class TestEvaUtilsSinRegistrarCambioRoto(BaseAuditoria):
    """El `registrar_cambio` de eva_utils usaba kwargs que no son columnas.

    No fallaba nunca porque app.py define el suyo mas abajo y pisa al import.
    O sea: andaba por el orden de las lineas. Se borro; esto lo fija.
    """

    def test_eva_utils_no_tiene_registrar_cambio_roto(self):
        self.assertFalse(
            hasattr(eva_utils, 'registrar_cambio'),
            'eva_utils.registrar_cambio volvio a existir. Si hace falta una '
            'version ahi, tiene que usar accion/tipo/id_registro, no '
            'tipo_accion/tipo_registro/registro_id.')

    def test_el_registrar_cambio_que_queda_escribe_de_verdad(self):
        """El de app.py, el unico que sobrevive, tiene que seguir andando."""
        import app as modulo_app
        modulo_app.registrar_cambio(self.roman_id, 'crear', 'gasto', 7,
                                    'Gasto de $100')
        filas = self.historial_de(tipo='gasto')
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].accion, 'crear')
        self.assertEqual(filas[0].id_registro, 7)
        # empresa_id lo deriva del usuario: ninguno de los once call sites
        # lo pasa, y no se tocaron.
        self.assertEqual(filas[0].empresa_id, self.empresa_id)


class TestEliminarCuentaConservaHistorial(BaseAuditoria):
    """Borrar la cuenta ya no borra el rastro de lo que esa cuenta hizo."""

    def test_eliminar_cuenta_no_borra_su_propio_historial(self):
        import app as modulo_app
        modulo_app.registrar_cambio(self.nachi_id, 'crear', 'gasto', 1,
                                    'Gasto que cargo Nachi')
        self.assertEqual(len(self.historial_de(tipo='gasto')), 1)

        # Nachi se borra la cuenta. Roman sigue en la empresa, asi que la
        # empresa NO se borra y el historial tiene a quien seguir sirviendole.
        self.post(self.nachi_id, '/cuenta/eliminar')

        self.assertIsNone(db.session.get(Usuario, self.nachi_id),
                          'la cuenta tendria que haberse borrado')

        filas = self.historial_de(tipo='gasto')
        self.assertEqual(len(filas), 1,
                         'el historial de la cuenta borrada se perdio')
        self.assertIsNone(filas[0].usuario_id,
                          'la FK tiene que quedar en NULL, no apuntando a un '
                          'usuario que ya no existe')
        self.assertEqual(filas[0].empresa_id, self.empresa_id,
                         'la fila tiene que seguir siendo de la empresa')
        self.assertIn('Nachi', filas[0].descripcion or '')

    def test_roman_sigue_viendo_lo_que_hizo_la_cuenta_borrada(self):
        """Es el punto de conservarlas: que se puedan LEER despues."""
        import app as modulo_app
        modulo_app.registrar_cambio(self.nachi_id, 'crear', 'gasto', 1,
                                    'Gasto que cargo Nachi')
        self.post(self.nachi_id, '/cuenta/eliminar')

        self.assertIn('Gasto que cargo Nachi', self.texto_de(self.roman_id, '/historial'))


class TestLimpiarHistorialDejaConstancia(BaseAuditoria):
    """El boton ya no borra: deja una fila diciendo que alguien lo apreto."""

    def test_limpiar_historial_deja_constancia(self):
        import app as modulo_app
        modulo_app.registrar_cambio(self.roman_id, 'crear', 'gasto', 1,
                                    'Un gasto cualquiera')
        antes = len(self.historial_de())

        self.post(self.roman_id, '/historial/limpiar')

        despues = self.historial_de()
        self.assertEqual(len(despues), antes + 1,
                         'tendria que haber UNA fila mas, no la tabla vacia')

        constancia = [f for f in despues if f.tipo == 'historial']
        self.assertEqual(len(constancia), 1)
        self.assertEqual(constancia[0].accion, 'eliminar')
        self.assertEqual(constancia[0].usuario_id, self.roman_id,
                         'la constancia tiene que decir QUIEN lo pidio')

    def test_limpiar_historial_no_borra_las_filas_viejas(self):
        import app as modulo_app
        modulo_app.registrar_cambio(self.roman_id, 'crear', 'gasto', 1,
                                    'Un gasto cualquiera')
        self.post(self.roman_id, '/historial/limpiar')

        self.assertEqual(len(self.historial_de(tipo='gasto')), 1,
                         'la fila vieja se borro: el rastro sigue siendo '
                         'borrable de un boton')

    def test_limpiar_historial_de_nachi_no_toca_el_de_roman(self):
        import app as modulo_app
        modulo_app.registrar_cambio(self.roman_id, 'crear', 'gasto', 1,
                                    'Gasto de Roman')
        self.post(self.nachi_id, '/historial/limpiar')
        self.assertEqual(len(self.historial_de(tipo='gasto')), 1)


class TestVerHistorialFiltraPorEmpresa(BaseAuditoria):
    """Con Nachi logueado, Roman tiene que ver lo que hizo Nachi."""

    def test_ver_historial_filtra_por_empresa_no_por_usuario(self):
        import app as modulo_app
        modulo_app.registrar_cambio(self.roman_id, 'crear', 'gasto', 1,
                                    'Gasto cargado por Roman')
        modulo_app.registrar_cambio(self.nachi_id, 'crear', 'ingreso', 2,
                                    'Ingreso cargado por Nachi')

        texto_roman = self.texto_de(self.roman_id, '/historial')
        self.assertIn('Gasto cargado por Roman', texto_roman)
        self.assertIn('Ingreso cargado por Nachi', texto_roman,
                      'Roman no ve lo que hizo Nachi: el filtro sigue siendo '
                      'por usuario')

        texto_nachi = self.texto_de(self.nachi_id, '/historial')
        self.assertIn('Gasto cargado por Roman', texto_nachi)
        self.assertIn('Ingreso cargado por Nachi', texto_nachi)

    def test_no_se_ve_el_historial_de_otra_empresa(self):
        """El filtro nuevo no puede pasarse de largo."""
        import app as modulo_app
        modulo_app.registrar_cambio(self.ajeno_id, 'crear', 'gasto', 9,
                                    'Secreto de la empresa ajena')
        self.assertNotIn('Secreto de la empresa ajena',
                         self.texto_de(self.roman_id, '/historial'))

    def test_la_pantalla_muestra_el_tipo(self):
        """La plantilla leia `h.tipo_registro`, que no existe en el modelo.

        Jinja resuelve un atributo inexistente como vacio y no avisa: la
        columna 'Tipo' venia en blanco desde siempre. Es el mismo lio de
        nombres que tenia el registrar_cambio de eva_utils.
        """
        import app as modulo_app
        modulo_app.registrar_cambio(self.roman_id, 'crear', 'gasto', 1, 'Algo')
        self.assertIn('gasto', self.texto_de(self.roman_id, '/historial'))


class TestHookAuditaProducto(BaseAuditoria):
    """S3-COSTO genera historial sin que se haya tocado esa ruta."""

    def guardar_costo(self, texto):
        return self.post(self.roman_id, '/productos/costos',
                         data={'sku': ['TARJ-NEGRO'],
                               'costo_unitario': [texto]})

    def test_editar_producto_genera_historial_automatico(self):
        self.guardar_costo('3994.18')

        self.assertEqual(db.session.get(Producto, self.producto_id).costo_unitario,
                         Decimal('3994.18'))

        filas = self.historial_de(tipo='producto')
        self.assertEqual(len(filas), 1,
                         'una fila por campo cambiado; cambio uno solo')
        fila = filas[0]
        self.assertEqual(fila.accion, 'editar')
        self.assertEqual(fila.id_registro, self.producto_id)
        self.assertEqual(fila.usuario_id, self.roman_id)
        self.assertEqual(fila.empresa_id, self.empresa_id)
        self.assertEqual(fila.valor_anterior, '3230.81')
        self.assertEqual(fila.valor_nuevo, '3994.18')
        self.assertIn('costo_unitario', fila.descripcion)
        self.assertIn('TARJ-NEGRO', fila.descripcion)

    def test_guardar_el_mismo_costo_no_genera_fila(self):
        """La ruta ya filtra los sin-cambio; el hook no puede inventarlos."""
        self.guardar_costo('3230.81')
        self.assertEqual(self.historial_de(tipo='producto'), [])

    def test_borrar_el_costo_queda_registrado(self):
        self.guardar_costo('')
        filas = self.historial_de(tipo='producto')
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0].valor_anterior, '3230.81')
        self.assertIsNone(filas[0].valor_nuevo)

    def test_una_tanda_rechazada_no_deja_historial(self):
        """Si la ruta hace rollback, el historial se va con el rollback.

        Es la contracara de escribir en el mismo commit: no hay forma de que
        quede registrado un cambio que no ocurrio.
        """
        self.guardar_costo('-5')
        self.assertEqual(db.session.get(Producto, self.producto_id).costo_unitario,
                         Decimal('3230.81'))
        self.assertEqual(self.historial_de(tipo='producto'), [])


class TestHookAuditaPedido(BaseAuditoria):
    """S3-COMISION, igual que costos: sin tocar la ruta."""

    def test_editar_pedido_genera_historial_automatico(self):
        self.post(self.roman_id, '/pedidos/comisiones',
                  data={'pedido_id': [str(self.pedido_id)],
                        'comision_plataforma': ['749.00']})

        self.assertEqual(db.session.get(Pedido, self.pedido_id).comision_plataforma,
                         Decimal('749.00'))

        filas = self.historial_de(tipo='pedido')
        self.assertEqual(len(filas), 1)
        fila = filas[0]
        self.assertEqual(fila.accion, 'editar')
        self.assertEqual(fila.id_registro, self.pedido_id)
        self.assertEqual(fila.usuario_id, self.roman_id)
        self.assertIsNone(fila.valor_anterior)
        self.assertEqual(fila.valor_nuevo, '749.00')
        self.assertIn('comision_plataforma', fila.descripcion)


class TestHookNoDuplicaNiHaceRuido(BaseAuditoria):
    """La lista blanca no se solapa con las once llamadas manuales."""

    def test_tabla_fuera_de_lista_blanca_no_genera_ruido(self):
        """Gasto ya tiene su registrar_cambio manual: el hook no lo toca."""
        categoria = Categoria(nombre='Insumos', tipo='gasto',
                              empresa_id=self.empresa_id,
                              usuario_id=self.roman_id)
        db.session.add(categoria)
        db.session.commit()

        self.post(self.roman_id, '/gasto/nuevo',
                  data={'descripcion': 'Flete', 'monto': '1500',
                        'categoria_id': str(categoria.id),
                        'fecha': '2026-09-03'})

        gastos = self.historial_de(tipo='gasto')
        self.assertEqual(len(gastos), 1,
                         'el hook duplico la fila que ya escribe app.py a mano')
        self.assertIsNone(gastos[0].valor_nuevo,
                          'esa fila la escribe la llamada manual, que no llena '
                          'valor_nuevo')
        # Y la categoria recien creada tampoco genero nada por el hook.
        self.assertEqual(self.historial_de(tipo='categoria'), [])

    def test_las_cuatro_tablas_manuales_no_estan_en_la_lista_blanca(self):
        """El chequeo directo de que no hay solapamiento posible."""
        import auditoria
        nombres = {clase.__tablename__ for clase in auditoria.TABLAS_AUDITADAS}
        manuales = {'gasto', 'ingreso', 'categoria', 'empresa'}
        self.assertEqual(nombres & manuales, set(),
                         'una tabla quedo en los dos mecanismos a la vez: cada '
                         'cambio generaria dos filas')

    def test_sin_usuario_logueado_el_hook_no_escribe(self):
        """Asi es como el sync queda afuera sin tocar una linea del sync.

        Corre fuera de request context, igual que
        `sync_tiendanube._correr_en_contexto` y que el cron.
        """
        producto = db.session.get(Producto, self.producto_id)
        producto.costo_unitario = Decimal('9999.00')
        db.session.commit()
        self.assertEqual(self.historial_de(tipo='producto'), [])


class TestHookNoFiltraSecretos(BaseAuditoria):
    """Un log de auditoria es una tabla mas: no puede guardar secretos."""

    def test_cambiar_password_no_deja_el_hash_en_el_historial(self):
        self.post(self.roman_id, '/cambiar-password',
                  data={'password_actual': 'irrelevante',
                        'password_nueva': 'otra-cosa-1234',
                        'password_confirmar': 'otra-cosa-1234'})

        filas = self.historial_de(tipo='usuario')
        hash_nuevo = db.session.get(Usuario, self.roman_id).password
        for fila in filas:
            self.assertNotEqual(fila.valor_nuevo, hash_nuevo)
            self.assertNotIn('pbkdf2', (fila.valor_nuevo or ''))
            self.assertNotIn('scrypt', (fila.valor_nuevo or ''))
        cambios_de_password = [f for f in filas if 'password' in (f.descripcion or '')]
        if cambios_de_password:
            # Queda constancia de QUE se cambio, nunca de a que.
            self.assertEqual(cambios_de_password[0].valor_nuevo, '(oculto)')

    def test_los_tokens_de_canal_no_se_copian(self):
        import auditoria
        credencial = CredencialCanal(canal_id=self.canal.id, tipo_credencial='oauth2',
                                     access_token_cifrado='gAAAA-secreto',
                                     activo=True)
        db.session.add(credencial)
        db.session.commit()
        credencial.access_token_cifrado = 'gAAAA-secreto-nuevo'

        cambios = dict((campo, (viejo, nuevo)) for campo, viejo, nuevo
                       in auditoria._campos_cambiados(credencial))
        self.assertIn('access_token_cifrado', cambios)
        self.assertEqual(cambios['access_token_cifrado'], ('(oculto)', '(oculto)'))
        db.session.rollback()


class TestAtomicidad(BaseAuditoria):
    """El cambio y su registro viven o mueren juntos."""

    def test_historial_y_cambio_en_el_mismo_commit(self):
        """Un rollback despues del cambio no puede dejar historial huerfano.

        Es exactamente lo que SI puede pasar con `registrar_cambio()`, que
        hace su propio commit: ahi el cambio ya esta guardado antes de que se
        escriba el registro, y entre los dos commits no hay nada que los ate.
        """
        with app.test_request_context():
            from flask_login import login_user
            login_user(db.session.get(Usuario, self.roman_id))

            producto = db.session.get(Producto, self.producto_id)
            producto.costo_unitario = Decimal('1111.11')

            # El flush dispara el hook: el historial ya esta en la sesion.
            db.session.flush()
            pendientes = [o for o in db.session.new if isinstance(o, Historial)]
            en_la_base = Historial.query.filter_by(tipo='producto').count()
            self.assertEqual(len(pendientes) + en_la_base, 1,
                             'el hook no dejo la fila en la MISMA sesion')

            # Y ahora se cae la transaccion.
            db.session.rollback()

        self.assertEqual(db.session.get(Producto, self.producto_id).costo_unitario,
                         Decimal('3230.81'),
                         'el cambio sobrevivio al rollback')
        self.assertEqual(self.historial_de(tipo='producto'), [],
                         'quedo una fila de historial de un cambio que no ocurrio')


class TestAuthDeLasRutasDeHistorial(BaseAuditoria):
    """Las dos rutas siguen pidiendo sesion."""

    def test_ver_historial_pide_login(self):
        from tests.ayuda_auth import request_anonimo
        self.assertEqual(request_anonimo(self.ctx, 'get', '/historial').status_code, 302)

    def test_limpiar_historial_pide_login(self):
        from tests.ayuda_auth import request_anonimo
        resp = request_anonimo(self.ctx, 'post', '/historial/limpiar')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Historial.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
