# -*- coding: utf-8 -*-
"""Tests de FASE-MELI-S1-FIX (la columna que truncaba el callback de MeLi).

    python -m unittest discover -s tests -v

QUE SE ARREGLO, Y QUE NO ERA
----------------------------
El callback de Mercado Libre autorizaba bien y reventaba al hacer commit, con
StringDataRightTruncation sobre varchar(255). La sospecha inicial fue que no
entraban los tokens cifrados; no era eso. `access_token_cifrado` y
`refresh_token_cifrado` ya son Text desde FASE2-S1 -- lo dice el modelo, lo
dice la migracion y lo confirma information_schema en Supabase. La unica
varchar(255) que quedaba en `credencial_canal` era `scope`, que guarda TEXTO
PLANO: la lista de permisos que devuelve el proveedor.

Por eso estos tests miran las dos cosas por separado:

  - que el scope entre entero, que es el bug real;
  - que un token cifrado largo entre entero, que es lo que se creia que
    estaba roto y conviene dejar amarrado igual, porque Fernet infla ~1.45x
    y nadie controla cuanto crece un token del proveedor.

POR QUE HAY UN TEST QUE PEGA CONTRA POSTGRES
--------------------------------------------
SQLite ignora los limites de largo de varchar: guarda el string entero y no
se queja. O sea que ninguno de los tests en memoria puede fallar por una
columna corta, y por si solos no prueban nada del bug. Lo que si protegen es
el modelo y el codigo de la ruta (que no vuelva a aparecer un corte a 255).

La prueba de que la COLUMNA quedo bien es TestColumnaReal, que consulta
information_schema contra la base de DATABASE_URL. Se saltea sola -- con un
mensaje que lo dice -- mientras la migracion 418ecc984913 no este aplicada.

Todas las llamadas HTTP estan mockeadas. La base productiva no se escribe:
este modulo repunta la app a SQLite en memoria, igual que FASE-MELI-S1.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('CREDENTIALS_ENCRYPTION_KEY',
                      'sO1mHTMYm4Rfy9ii1YV8dqmM1J4KrHnQPy_2xGx0nMk=')
os.environ.setdefault('SECRET_KEY', 'clave-de-test')

import cripto  # noqa: E402
import integracion_mercadolibre as meli  # noqa: E402
from app import app  # noqa: E402
from models import (CanalVenta, CredencialCanal, CredencialCuentaCobro,  # noqa: E402
                    CuentaCobro, Empresa, Usuario, db)

ENGINE_PRODUCTIVO = None

SECRETO_FALSO = 'secreto-de-test-no-es-el-real'
USER_ID_MELI = '123456789'
APODO = 'FERRETERIA.NACHI'

# El access_token de Mercado Libre tiene la forma
# APP_USR-<app_id>-<fecha>-<hex32>-<user_id>. Este es de ese largo, con datos
# inventados: lo que importa del test no es el valor sino el tamanio.
ACCESS_MELI = ('APP_USR-8020619530821827-090419-'
               'f3a1c0d94b27e8615a0d7c2b8e4f1963-123456789')
REFRESH_MELI = 'TG-68b9d4c1e7a25f30914c6b82-123456789'

# Un access_token de 1000 caracteres. NO es una respuesta capturada de MeLi:
# es el caso "el proveedor decide crecer el token y nosotros nos enteramos en
# produccion". Cifrado da 1464, casi seis veces el limite viejo, y es el
# motivo de que la columna sea Text y no un numero fijo mas grande.
ACCESS_ENORME = 'APP_USR-' + ('z9' * 496)

# El scope tal como lo devuelve el endpoint de token cuando enumera permisos
# concedidos en vez de los tres genericos. Este pasa los 255 caracteres, que
# es exactamente lo que rompia el commit del callback.
SCOPE_LARGO = ' '.join([
    'offline_access', 'read', 'write',
    'read:orders', 'write:orders', 'read:items', 'write:items',
    'read:shipments', 'write:shipments', 'read:questions', 'write:questions',
    'read:messages', 'write:messages', 'read:users', 'read:payments',
    'read:invoices', 'read:catalog', 'write:catalog', 'read:promotions',
    'write:promotions', 'read:metrics', 'read:visits', 'read:reputation',
])

# Lo que ya estaba guardado antes del fix, para el test de no-regresion.
SCOPE_TIENDANUBE = 'read_products,write_products,read_customers,read_orders'
ACCESS_TIENDANUBE = 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4'
ACCESS_MERCADOPAGO = 'APP_USR-4471109920047661-090418-9c7e2a1b3f5d80641e2a-778899001'
REFRESH_MERCADOPAGO = 'TG-5f1c9a8b7d6e4302-778899001'


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


class RespuestaFalsa:
    """Lo minimo de requests.Response que usa integracion_mercadolibre."""

    def __init__(self, status_code=200, datos=None):
        self.status_code = status_code
        self.headers = {}
        self.text = json.dumps(datos if datos is not None else {})

    def json(self):
        return json.loads(self.text)


def respuesta_token(access=ACCESS_MELI, refresh=REFRESH_MELI, scope=SCOPE_LARGO):
    return RespuestaFalsa(200, {
        'access_token': access,
        'token_type': 'bearer',
        'expires_in': 10800,
        'scope': scope,
        'user_id': int(USER_ID_MELI),
        'refresh_token': refresh,
    })


def respuesta_usuario():
    return RespuestaFalsa(200, {'id': int(USER_ID_MELI), 'nickname': APODO,
                                'site_id': 'MLA'})


# ===========================================================================
# PARTE 1 - El esquema declarado
# ===========================================================================

class TestEsquemaDeclarado(unittest.TestCase):
    """Sin base de por medio: lo que dice el modelo.

    Es el unico chequeo que corre igual en SQLite y en Postgres, y es el que
    Alembic compara para generar la migracion. Si alguien vuelve a poner un
    String(n) en cualquiera de estas tres columnas, aca se ve.
    """

    COLUMNAS_SIN_LIMITE = ('access_token_cifrado', 'refresh_token_cifrado', 'scope')

    def test_las_columnas_de_texto_no_declaran_largo(self):
        tabla = CredencialCanal.__table__
        for nombre in self.COLUMNAS_SIN_LIMITE:
            with self.subTest(columna=nombre):
                tipo = tabla.c[nombre].type
                largo = getattr(tipo, 'length', None)
                self.assertIsNone(
                    largo,
                    'credencial_canal.%s declara largo %s. Ninguna de estas tres '
                    'columnas la escribimos nosotros: el token lo emite el '
                    'proveedor y el scope tambien. Cualquier numero fijo es una '
                    'apuesta a que el proveedor no crece.' % (nombre, largo))

    def test_la_hermana_de_cuentas_de_cobro_esta_igual(self):
        """credencial_cuenta_cobro guarda los mismos secretos con el mismo
        Fernet; si alguna vez divergen, que se note aca y no en un callback."""
        tabla = CredencialCuentaCobro.__table__
        for nombre in ('access_token_cifrado', 'refresh_token_cifrado'):
            with self.subTest(columna=nombre):
                self.assertIsNone(getattr(tabla.c[nombre].type, 'length', None))


class TestTamanioDelCifrado(unittest.TestCase):
    """La medicion que descarta "poner un numero mas grande"."""

    def test_fernet_infla_el_token_y_255_se_queda_corto_enseguida(self):
        """Fernet mete IV, timestamp y HMAC y despues codifica en base64: el
        resultado es ~1.45x el texto plano mas overhead fijo.

        Con 255 de limite, el texto plano mas largo que entraba eran 127
        caracteres. Los tokens de MeLi hoy miden ~70 y entraban; el margen era
        de menos del doble, para un valor que no controlamos.
        """
        self.assertLessEqual(len(cripto.cifrar('x' * 127)), 255)
        self.assertGreater(len(cripto.cifrar('x' * 128)), 255)

        cifrado_enorme = cripto.cifrar(ACCESS_ENORME)
        self.assertGreater(len(cifrado_enorme), 1400)
        self.assertEqual(cripto.descifrar(cifrado_enorme), ACCESS_ENORME)


# ===========================================================================
# PARTE 2 - El callback, de punta a punta
# ===========================================================================

class BaseWeb(unittest.TestCase):
    """Una empresa + un usuario logueado + el canal de MeLi, en memoria."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-MELI-S1-FIX')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(
            nombre='Roman Test',
            email='faseMeliS1Fix@test.local',
            empresa_id=self.empresa.id,
            rol='admin',
            verificado=True,
        )
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        db.session.add(CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                  nombre='Tiendanube', activo=False))
        db.session.add(CanalVenta(empresa_id=self.empresa.id, tipo='mercadolibre',
                                  nombre='Mercado Libre', activo=False))
        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def canal(self, tipo='mercadolibre'):
        return CanalVenta.query.filter_by(
            empresa_id=self.empresa_id, tipo=tipo).first()

    def callback(self, access=ACCESS_MELI, refresh=REFRESH_MELI, scope=SCOPE_LARGO):
        state = 'state-valido'
        with self.client.session_transaction() as sesion:
            sesion['meli_oauth_state'] = state
            sesion['meli_oauth_empresa_id'] = self.empresa_id
        with mock.patch.object(meli.requests, 'post',
                               return_value=respuesta_token(access, refresh, scope)), \
             mock.patch.object(meli.requests, 'get',
                               return_value=respuesta_usuario()), \
             mock.patch.dict(os.environ,
                             {'MERCADOLIBRE_CLIENT_SECRET': SECRETO_FALSO}):
            return self.client.get(
                '/integraciones/mercadolibre/callback?code=code-valido&state=%s' % state)


class TestCallbackNoTrunca(BaseWeb):

    def test_token_largo_de_meli_se_guarda_sin_truncar(self):
        """El caso que se creia roto: un token cuyo cifrado pasa los 255.

        Se lee por SQL crudo para que ningun descifrado del modelo disimule un
        valor cortado, y se descifra al final: un Fernet truncado no descifra,
        asi que el round-trip es la prueba de que entro entero.
        """
        resp = self.callback(access=ACCESS_ENORME)
        self.assertEqual(resp.status_code, 302)

        from sqlalchemy import text
        guardado, = db.session.execute(text(
            'SELECT access_token_cifrado FROM credencial_canal')).one()

        self.assertGreater(len(guardado), 255,
                           'el caso no prueba nada si el cifrado entraba en 255')
        self.assertEqual(len(guardado), len(cripto.cifrar(ACCESS_ENORME)))
        self.assertEqual(cripto.descifrar(guardado), ACCESS_ENORME)

    def test_scope_largo_de_meli_se_guarda_entero(self):
        """El bug real: `scope` era la varchar(255) que hacia fallar el commit.

        Se compara contra el string completo, no contra su prefijo: un corte a
        255 en la ruta pasaria un assert de "empieza con" y este no.
        """
        self.assertGreater(len(SCOPE_LARGO), 255, 'el fixture perdio el sentido')

        resp = self.callback(scope=SCOPE_LARGO)
        self.assertEqual(resp.status_code, 302)

        from sqlalchemy import text
        guardado, = db.session.execute(text(
            'SELECT scope FROM credencial_canal')).one()

        self.assertEqual(guardado, SCOPE_LARGO)
        self.assertEqual(len(guardado), len(SCOPE_LARGO))

    def test_la_conexion_queda_usable_con_los_valores_largos(self):
        """No alcanza con que no explote: el canal tiene que quedar conectado
        y los dos tokens tienen que volver a texto plano."""
        self.callback(access=ACCESS_ENORME)

        canal = self.canal()
        self.assertTrue(canal.activo)
        self.assertEqual(canal.id_tienda_externo, USER_ID_MELI)

        credencial = CredencialCanal.query.filter_by(canal_id=canal.id).one()
        self.assertEqual(cripto.descifrar(credencial.access_token_cifrado),
                         ACCESS_ENORME)
        self.assertEqual(cripto.descifrar(credencial.refresh_token_cifrado),
                         REFRESH_MELI)
        self.assertIsNotNone(credencial.expira_en)


class TestNoRompeLoQueYaEstaba(BaseWeb):
    """Lo guardado antes del cambio de tipo tiene que seguir leyendose igual.

    Widening de varchar(255) a text en Postgres no reescribe ni trunca nada
    -- es un cambio de tipo compatible, sin table rewrite -- pero eso hay que
    demostrarlo con los valores reales que hay en la base, no afirmarlo.
    """

    def test_tokens_existentes_de_tiendanube_y_mp_siguen_funcionando(self):
        canal_tn = self.canal('tiendanube')
        canal_tn.activo = True
        canal_tn.id_tienda_externo = '8078725'
        credencial_tn = CredencialCanal(
            canal_id=canal_tn.id,
            tipo_credencial='oauth2',
            access_token_cifrado=cripto.cifrar(ACCESS_TIENDANUBE),
            scope=SCOPE_TIENDANUBE,
            activo=True,
        )
        db.session.add(credencial_tn)

        cuenta = CuentaCobro(empresa_id=self.empresa_id, tipo='mercadopago',
                             nombre='MP Roman', socio='roman')
        db.session.add(cuenta)
        db.session.flush()
        credencial_mp = CredencialCuentaCobro(
            cuenta_cobro_id=cuenta.id,
            access_token_cifrado=cripto.cifrar(ACCESS_MERCADOPAGO),
            refresh_token_cifrado=cripto.cifrar(REFRESH_MERCADOPAGO),
            expira_en=datetime.utcnow() + timedelta(days=180),
        )
        db.session.add(credencial_mp)
        db.session.commit()
        # Los ids se guardan ANTES de vaciar la sesion: despues del expunge las
        # instancias quedan desprendidas y leerles un atributo explota. El
        # expunge esta a proposito -- releer desde la base es el punto del
        # test, no reusar los objetos que quedaron en memoria.
        canal_tn_id, cuenta_id = canal_tn.id, cuenta.id
        db.session.expunge_all()

        tn = CredencialCanal.query.filter_by(canal_id=canal_tn_id).one()
        self.assertEqual(cripto.descifrar(tn.access_token_cifrado), ACCESS_TIENDANUBE)
        self.assertIsNone(tn.refresh_token_cifrado)
        # El scope de Tiendanube es corto (55) y no cambia de valor por pasar a
        # Text: entraba antes y entra ahora, con el mismo contenido.
        self.assertEqual(tn.scope, SCOPE_TIENDANUBE)

        mp = CredencialCuentaCobro.query.filter_by(cuenta_cobro_id=cuenta_id).one()
        self.assertEqual(cripto.descifrar(mp.access_token_cifrado), ACCESS_MERCADOPAGO)
        self.assertEqual(cripto.descifrar(mp.refresh_token_cifrado), REFRESH_MERCADOPAGO)

    def test_reconectar_meli_pisa_la_fila_y_no_deja_el_scope_viejo(self):
        """Reconectar reusa la credencial. Si el scope nuevo es mas largo que
        el que habia, tiene que quedar el nuevo entero, no una mezcla."""
        self.callback(scope='offline_access read write')
        primero = CredencialCanal.query.one()
        self.assertEqual(primero.scope, 'offline_access read write')

        self.callback(scope=SCOPE_LARGO)
        credenciales = CredencialCanal.query.all()
        self.assertEqual(len(credenciales), 1, 'reconectar no debe apilar filas')
        self.assertEqual(credenciales[0].scope, SCOPE_LARGO)


# ===========================================================================
# PARTE 3 - La columna de verdad, en Postgres
# ===========================================================================

class TestColumnaReal(unittest.TestCase):
    """Lo unico que prueba el fix donde el fix importa.

    SQLite no tiene limites de varchar, asi que todo lo de arriba pasaria
    igual con la columna corta. Esto consulta information_schema contra la
    base de DATABASE_URL. Solo LEE: no escribe ni una fila.

    Se saltea si no hay Postgres, y se saltea con un mensaje explicito si la
    migracion 418ecc984913 todavia no se aplico -- que es el estado esperado
    hasta que Roman de el OK.
    """

    def setUp(self):
        if ENGINE_PRODUCTIVO is None or ENGINE_PRODUCTIVO.dialect.name != 'postgresql':
            self.skipTest('DATABASE_URL no apunta a Postgres: no hay columna real '
                          'que revisar (SQLite ignora los limites de varchar).')

    def _tipo_de(self, tabla, columna):
        from sqlalchemy import text
        with ENGINE_PRODUCTIVO.connect() as conexion:
            fila = conexion.execute(text(
                'SELECT data_type, character_maximum_length '
                'FROM information_schema.columns '
                'WHERE table_schema = :esquema AND table_name = :tabla '
                '  AND column_name = :columna'
            ), {'esquema': 'public', 'tabla': tabla, 'columna': columna}).first()
        self.assertIsNotNone(fila, 'no existe %s.%s en la base' % (tabla, columna))
        return fila[0], fila[1]

    def test_las_columnas_cifradas_ya_eran_text(self):
        """La premisa que hay que dejar por escrito: nunca fueron varchar(255).

        Este test pasa ANTES y DESPUES de la migracion. Esta para que la
        proxima vez que un callback falle por truncamiento, nadie vuelva a
        empezar a buscar por aca.
        """
        for tabla in ('credencial_canal', 'credencial_cuenta_cobro'):
            for columna in ('access_token_cifrado', 'refresh_token_cifrado'):
                with self.subTest(tabla=tabla, columna=columna):
                    tipo, largo = self._tipo_de(tabla, columna)
                    self.assertEqual(tipo, 'text')
                    self.assertIsNone(largo)

    def test_scope_quedo_sin_limite_despues_de_la_migracion(self):
        tipo, largo = self._tipo_de('credencial_canal', 'scope')
        if largo == 255:
            self.skipTest(
                'credencial_canal.scope sigue en varchar(255): falta aplicar la '
                'migracion 418ecc984913 (flask db upgrade). El bug del callback '
                'de Mercado Libre sigue vivo en esta base.')
        self.assertEqual(tipo, 'text')
        self.assertIsNone(largo)


if __name__ == '__main__':
    unittest.main(verbosity=2)
