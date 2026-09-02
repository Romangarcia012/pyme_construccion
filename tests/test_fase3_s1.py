# -*- coding: utf-8 -*-
"""Tests de la FASE3-S1 (OAuth Tiendanube + credenciales cifradas).

    python -m unittest discover -s tests -v

NINGUN test pega contra la API real de Tiendanube: todas las llamadas HTTP
estan mockeadas con unittest.mock, tanto el intercambio del code como el GET
/store. Tampoco se toca la base productiva: este modulo repunta la app a un
SQLite en memoria mientras corre y la devuelve a DATABASE_URL al terminar.

Se usa unittest (stdlib) para no sumar pytest como dependencia, igual que en
FASE2-S1.
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# La clave de cifrado tiene que existir antes de importar app.py: el chequeo es
# de arranque a proposito. En CI la pone el entorno; en local viene del .env.
os.environ.setdefault('CREDENTIALS_ENCRYPTION_KEY',
                      'sO1mHTMYm4Rfy9ii1YV8dqmM1J4KrHnQPy_2xGx0nMk=')
os.environ.setdefault('SECRET_KEY', 'clave-de-test')

import cripto  # noqa: E402
import integracion_tiendanube as tn  # noqa: E402
from app import app  # noqa: E402
from tests.ayuda_auth import request_anonimo  # noqa: E402
from models import CanalVenta, CredencialCanal, Empresa, Usuario, db  # noqa: E402

ENGINE_PRODUCTIVO = None

TOKEN_FALSO = 'tn_token_de_prueba_no_es_real_1234567890'
STORE_ID_FALSO = '9876543'


def setUpModule():
    """Repunta la app a SQLite en memoria. La base real no se toca.

    Se cambia el engine en caliente en vez de llamar a db.init_app() de nuevo
    (Flask-SQLAlchemy no deja registrar dos veces la misma extension). El
    engine original se restaura al terminar, para no romper a los tests de
    FASE2-S1, que si corren contra la base real.
    """
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
    """Lo minimo de requests.Response que usa integracion_tiendanube."""

    def __init__(self, status_code=200, datos=None, texto=None):
        self.status_code = status_code
        self._datos = datos
        self.text = texto if texto is not None else str(datos)

    def json(self):
        if self._datos is None:
            raise ValueError('no es JSON')
        return self._datos


class BaseWeb(unittest.TestCase):
    """Una empresa + un usuario logueado, sobre la base en memoria.

    Cada test arranca con las tablas vacias: asi el test del camino feliz y el
    del camino de error no se pisan.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE3-S1')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(
            nombre='Roman Test',
            email='fase3s1@test.local',
            empresa_id=self.empresa.id,
            rol='admin',
            verificado=True,
        )
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        # Los dos canales que sembro la migracion de FASE2-S1, ambos inactivos.
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

    def canal_tiendanube(self):
        return CanalVenta.query.filter_by(
            empresa_id=self.empresa_id, tipo='tiendanube').first()


class TestConectar(BaseWeb):
    """GET /integraciones/tiendanube/conectar."""

    def test_redirige_al_consentimiento_de_tiendanube_y_no_escribe_nada(self):
        resp = self.client.get('/integraciones/tiendanube/conectar')

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers['Location'],
                         'https://www.tiendanube.com/apps/%s/authorize' % tn.APP_ID)
        # Iniciar el flujo no conecta nada: el canal sigue apagado y sin credencial.
        self.assertFalse(self.canal_tiendanube().activo)
        self.assertEqual(CredencialCanal.query.count(), 0)

    def test_requiere_login(self):
        resp = request_anonimo(self.ctx, 'get', '/integraciones/tiendanube/conectar')
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('tiendanube.com', resp.headers['Location'])


class TestCallbackExitoso(BaseWeb):
    """El camino feliz: code valido -> token cifrado en la base."""

    def _mock_ok(self):
        """Un POST al token y un GET al store, ambos falsos."""
        respuesta_token = RespuestaFalsa(200, {
            'access_token': TOKEN_FALSO,
            'user_id': int(STORE_ID_FALSO),
            'scope': 'read_orders,read_products',
            'token_type': 'bearer',
        })
        respuesta_store = RespuestaFalsa(200, {
            'id': int(STORE_ID_FALSO),
            'name': {'es': 'Ferreteria Roman'},
        })
        return respuesta_token, respuesta_store

    def test_guarda_token_cifrado_y_activa_el_canal(self):
        respuesta_token, respuesta_store = self._mock_ok()

        with mock.patch.object(tn.requests, 'post', return_value=respuesta_token) as post, \
             mock.patch.object(tn.requests, 'get', return_value=respuesta_store) as get, \
             mock.patch.dict(os.environ, {'TIENDANUBE_CLIENT_SECRET': 'secreto-de-test'}):
            resp = self.client.get('/integraciones/tiendanube/callback?code=code-valido')

        self.assertEqual(resp.status_code, 302)

        canal = self.canal_tiendanube()
        self.assertTrue(canal.activo)
        self.assertEqual(canal.id_tienda_externo, STORE_ID_FALSO)
        self.assertEqual(canal.nombre, 'Ferreteria Roman')

        credencial = CredencialCanal.query.filter_by(canal_id=canal.id).one()
        self.assertTrue(credencial.activo)
        self.assertEqual(credencial.tipo_credencial, 'oauth2')
        self.assertEqual(credencial.scope, 'read_orders,read_products')

        # Se pidio el token con los cuatro campos del grant.
        cuerpo = post.call_args.kwargs['json']
        self.assertEqual(cuerpo['grant_type'], 'authorization_code')
        self.assertEqual(cuerpo['code'], 'code-valido')
        self.assertEqual(cuerpo['client_id'], str(tn.APP_ID))
        self.assertEqual(cuerpo['client_secret'], 'secreto-de-test')

        # Y se probo el token contra /store antes de dar por buena la conexion.
        self.assertIn('/%s/store' % STORE_ID_FALSO, get.call_args.args[0])

    def test_la_columna_no_guarda_el_token_en_texto_plano(self):
        """Lo importante de la slice: leer la columna cruda no da el token."""
        respuesta_token, respuesta_store = self._mock_ok()

        with mock.patch.object(tn.requests, 'post', return_value=respuesta_token), \
             mock.patch.object(tn.requests, 'get', return_value=respuesta_store), \
             mock.patch.dict(os.environ, {'TIENDANUBE_CLIENT_SECRET': 'secreto-de-test'}):
            self.client.get('/integraciones/tiendanube/callback?code=code-valido')

        # Se lee la columna directo por SQL, sin pasar por el modelo, para que
        # no haya forma de que un descifrado automatico disimule el problema.
        from sqlalchemy import text
        guardado = db.session.execute(
            text('SELECT access_token_cifrado FROM credencial_canal')
        ).scalar()

        self.assertIsNotNone(guardado)
        self.assertNotEqual(guardado, TOKEN_FALSO)
        self.assertNotIn(TOKEN_FALSO, guardado)
        # Es un token Fernet: prefijo de version 0x80 en base64url ('gAAAAA').
        self.assertTrue(guardado.startswith('gAAAAA'), guardado[:20])
        # Y descifra de vuelta al original.
        self.assertEqual(cripto.descifrar(guardado), TOKEN_FALSO)

    def test_todas_las_llamadas_llevan_user_agent(self):
        respuesta_token, respuesta_store = self._mock_ok()

        with mock.patch.object(tn.requests, 'post', return_value=respuesta_token) as post, \
             mock.patch.object(tn.requests, 'get', return_value=respuesta_store) as get, \
             mock.patch.dict(os.environ, {'TIENDANUBE_CLIENT_SECRET': 'secreto-de-test'}):
            self.client.get('/integraciones/tiendanube/callback?code=code-valido')

        for llamada in (post, get):
            cabeceras = llamada.call_args.kwargs['headers']
            self.assertIn('User-Agent', cabeceras)
            self.assertTrue(cabeceras['User-Agent'].strip())

        # El GET a la API viaja con el header propietario de Tiendanube.
        self.assertEqual(get.call_args.kwargs['headers']['Authentication'],
                         'bearer %s' % TOKEN_FALSO)

    def test_reconectar_reutiliza_la_fila_de_credencial(self):
        respuesta_token, respuesta_store = self._mock_ok()

        with mock.patch.object(tn.requests, 'post', return_value=respuesta_token), \
             mock.patch.object(tn.requests, 'get', return_value=respuesta_store), \
             mock.patch.dict(os.environ, {'TIENDANUBE_CLIENT_SECRET': 'secreto-de-test'}):
            self.client.get('/integraciones/tiendanube/callback?code=code-valido')
            self.client.get('/integraciones/tiendanube/callback?code=otro-code')

        self.assertEqual(CredencialCanal.query.count(), 1)


class TestCallbackFallido(BaseWeb):
    """Los caminos de error: no se guarda nada a medias."""

    def _assert_no_conecto(self, resp):
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/integraciones', resp.headers['Location'])
        self.assertEqual(CredencialCanal.query.count(), 0)
        canal = self.canal_tiendanube()
        self.assertFalse(canal.activo)
        self.assertIsNone(canal.id_tienda_externo)

    def test_code_invalido_rechazado_por_tiendanube(self):
        rechazo = RespuestaFalsa(400, {'error': 'invalid_grant'},
                                 texto='{"error":"invalid_grant"}')

        with mock.patch.object(tn.requests, 'post', return_value=rechazo), \
             mock.patch.object(tn.requests, 'get') as get, \
             mock.patch.dict(os.environ, {'TIENDANUBE_CLIENT_SECRET': 'secreto-de-test'}):
            resp = self.client.get('/integraciones/tiendanube/callback?code=code-invalido')

        # Si el code no sirve, ni se intenta usar la API.
        get.assert_not_called()
        self._assert_no_conecto(resp)

    def test_error_200_con_cuerpo_de_error(self):
        """Tiendanube puede contestar 200 con un cuerpo de error."""
        rechazo = RespuestaFalsa(200, {'error': 'invalid_grant',
                                       'error_description': 'code ya usado'})

        with mock.patch.object(tn.requests, 'post', return_value=rechazo), \
             mock.patch.object(tn.requests, 'get') as get, \
             mock.patch.dict(os.environ, {'TIENDANUBE_CLIENT_SECRET': 'secreto-de-test'}):
            resp = self.client.get('/integraciones/tiendanube/callback?code=usado')

        get.assert_not_called()
        self._assert_no_conecto(resp)

    def test_token_obtenido_pero_rechazado_por_la_api(self):
        """El caso peligroso: el code se canjeo bien pero el token no sirve.

        Si se guardara antes de probarlo, quedaria un canal activo con una
        credencial muerta.
        """
        respuesta_token = RespuestaFalsa(200, {'access_token': TOKEN_FALSO,
                                               'user_id': int(STORE_ID_FALSO)})
        rechazo_api = RespuestaFalsa(401, {'code': 401, 'message': 'Unauthorized'},
                                     texto='{"message":"Unauthorized"}')

        with mock.patch.object(tn.requests, 'post', return_value=respuesta_token), \
             mock.patch.object(tn.requests, 'get', return_value=rechazo_api), \
             mock.patch.dict(os.environ, {'TIENDANUBE_CLIENT_SECRET': 'secreto-de-test'}):
            resp = self.client.get('/integraciones/tiendanube/callback?code=code-valido')

        self._assert_no_conecto(resp)

    def test_sin_code_en_el_querystring(self):
        with mock.patch.object(tn.requests, 'post') as post:
            resp = self.client.get('/integraciones/tiendanube/callback')
        post.assert_not_called()
        self._assert_no_conecto(resp)

    def test_usuario_cancela_la_autorizacion(self):
        with mock.patch.object(tn.requests, 'post') as post:
            resp = self.client.get('/integraciones/tiendanube/callback?error=access_denied')
        post.assert_not_called()
        self._assert_no_conecto(resp)

    def test_red_caida(self):
        import requests as requests_real

        with mock.patch.object(tn.requests, 'post',
                               side_effect=requests_real.ConnectionError('sin red')), \
             mock.patch.dict(os.environ, {'TIENDANUBE_CLIENT_SECRET': 'secreto-de-test'}):
            resp = self.client.get('/integraciones/tiendanube/callback?code=code-valido')

        self._assert_no_conecto(resp)

    def test_sin_client_secret_configurado_en_el_servidor(self):
        entorno = dict(os.environ)
        entorno.pop('TIENDANUBE_CLIENT_SECRET', None)

        with mock.patch.dict(os.environ, entorno, clear=True), \
             mock.patch.object(tn.requests, 'post') as post:
            resp = self.client.get('/integraciones/tiendanube/callback?code=code-valido')

        post.assert_not_called()
        self._assert_no_conecto(resp)

    def test_el_error_que_ve_el_usuario_no_es_un_stack_trace(self):
        rechazo = RespuestaFalsa(400, {'error': 'invalid_grant'})

        with mock.patch.object(tn.requests, 'post', return_value=rechazo), \
             mock.patch.dict(os.environ, {'TIENDANUBE_CLIENT_SECRET': 'secreto-de-test'}):
            self.client.get('/integraciones/tiendanube/callback?code=malo')
            pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('Tiendanube rechazo la autorizacion', pagina)
        self.assertNotIn('Traceback', pagina)
        # El detalle tecnico va al log del servidor, no al navegador.
        self.assertNotIn('invalid_grant', pagina)


class TestPaginaIntegraciones(BaseWeb):
    """GET /integraciones."""

    def test_lista_los_dos_canales_con_su_estado(self):
        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('Tiendanube', pagina)
        self.assertIn('Mercado Libre', pagina)
        self.assertIn('Sin conectar', pagina)
        # Tiendanube tiene boton; Mercado Libre todavia no.
        self.assertIn('/integraciones/tiendanube/conectar', pagina)
        self.assertIn('Próximamente', pagina)

    def test_muestra_la_cuenta_cuando_el_canal_esta_conectado(self):
        canal = self.canal_tiendanube()
        canal.activo = True
        canal.nombre = 'Ferreteria Roman'
        canal.id_tienda_externo = STORE_ID_FALSO
        db.session.commit()

        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertIn('Conectado', pagina)
        self.assertIn('Ferreteria Roman', pagina)
        self.assertIn(STORE_ID_FALSO, pagina)

    def test_requiere_login(self):
        resp = request_anonimo(self.ctx, 'get', '/integraciones')
        self.assertEqual(resp.status_code, 302)

    def test_no_muestra_canales_de_otra_empresa(self):
        otra = Empresa(nombre='Empresa Ajena')
        db.session.add(otra)
        db.session.flush()
        db.session.add(CanalVenta(empresa_id=otra.id, tipo='tiendanube',
                                  nombre='Tienda Ajena SRL', activo=True,
                                  id_tienda_externo='111'))
        db.session.commit()

        pagina = self.client.get('/integraciones').get_data(as_text=True)

        self.assertNotIn('Tienda Ajena SRL', pagina)
        self.assertNotIn('Conectado', pagina)


class TestRegresion(BaseWeb):
    """Lo que ya andaba tiene que seguir andando."""

    def test_dashboard_sigue_dando_200(self):
        resp = self.client.get('/dashboard')
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_linkea_a_integraciones(self):
        pagina = self.client.get('/dashboard').get_data(as_text=True)
        self.assertIn('/integraciones', pagina)

    def test_integraciones_da_200(self):
        self.assertEqual(self.client.get('/integraciones').status_code, 200)


class TestCifrado(unittest.TestCase):
    """cripto.py aislado, sin base ni HTTP."""

    def test_ida_y_vuelta(self):
        self.assertEqual(cripto.descifrar(cripto.cifrar('hola')), 'hola')

    def test_dos_cifrados_del_mismo_texto_dan_distinto(self):
        """Fernet mete IV y timestamp: el cifrado no es un hash determinista,
        asi que no se puede deducir que dos canales comparten token."""
        self.assertNotEqual(cripto.cifrar('mismo'), cripto.cifrar('mismo'))

    def test_vacio_no_se_cifra(self):
        self.assertIsNone(cripto.cifrar(None))
        self.assertIsNone(cripto.cifrar(''))
        self.assertIsNone(cripto.descifrar(None))

    def test_token_de_otra_clave_no_descifra(self):
        from cryptography.fernet import Fernet
        ajeno = Fernet(Fernet.generate_key()).encrypt(b'secreto').decode()
        with self.assertRaises(cripto.ErrorCifrado):
            cripto.descifrar(ajeno)

    def test_basura_no_descifra(self):
        with self.assertRaises(cripto.ErrorCifrado):
            cripto.descifrar('esto no es un token fernet')


class TestArranqueSinClave(unittest.TestCase):
    """La app no puede arrancar sin CREDENTIALS_ENCRYPTION_KEY.

    Se corre en un subproceso porque `import app` ya paso en este proceso. Se
    neutraliza load_dotenv para simular Render sin la variable cargada, en vez
    de depender de que exista o no un .env local.
    """

    GUION = (
        'import os, sys, dotenv\n'
        'dotenv.load_dotenv = lambda *a, **k: False\n'
        'os.environ["SECRET_KEY"] = "clave-de-test"\n'
        'os.environ.pop("DATABASE_URL", None)\n'
        '{linea_clave}\n'
        'sys.path.insert(0, {raiz!r})\n'
        'import app\n'
        'print("ARRANCO")\n'
    )

    def _correr(self, linea_clave):
        guion = self.GUION.format(linea_clave=linea_clave, raiz=RAIZ)
        return subprocess.run(
            [sys.executable, '-c', guion],
            cwd=RAIZ, capture_output=True, text=True, timeout=180,
            # app.py reescribe sys.stdout en UTF-8 y saca emojis por ahi. Sin
            # forzar el encoding, en Windows el padre decodifica con cp1252 y
            # se rompe la lectura del pipe.
            encoding='utf-8', errors='replace',
        )

    def test_sin_la_variable_falla_explicitamente(self):
        proc = self._correr('os.environ.pop("CREDENTIALS_ENCRYPTION_KEY", None)')

        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn('ARRANCO', proc.stdout)
        self.assertIn('CREDENTIALS_ENCRYPTION_KEY', proc.stderr)
        self.assertIn('RuntimeError', proc.stderr)

    def test_con_una_clave_invalida_tambien_falla(self):
        proc = self._correr('os.environ["CREDENTIALS_ENCRYPTION_KEY"] = "no-es-fernet"')

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('CREDENTIALS_ENCRYPTION_KEY', proc.stderr)

    def test_control_positivo_con_la_clave_arranca(self):
        """Sin esto, los dos tests de arriba pasarian aunque el import fallara
        por cualquier otro motivo."""
        proc = self._correr(
            'os.environ["CREDENTIALS_ENCRYPTION_KEY"] = '
            '"sO1mHTMYm4Rfy9ii1YV8dqmM1J4KrHnQPy_2xGx0nMk="'
        )

        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn('ARRANCO', proc.stdout)


class TestNoSePegaALaApiReal(unittest.TestCase):
    """Guarda explicita: ningun test de este modulo sale a internet."""

    def test_el_modulo_solo_sale_por_requests_get_y_post(self):
        # Los tests parchean requests.get y requests.post. Si el cliente usara
        # otra puerta (Session, urllib), esos parches no la taparian y el test
        # suite pegaria contra Tiendanube de verdad.
        import inspect
        fuente = inspect.getsource(tn)
        self.assertNotIn('requests.request(', fuente)
        self.assertNotIn('requests.Session(', fuente)
        self.assertNotIn('urllib', fuente)


if __name__ == '__main__':
    unittest.main()
