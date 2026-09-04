# -*- coding: utf-8 -*-
"""Tests de FASE-MELI-S1 (OAuth de Mercado Libre + refresh + verificacion).

    python -m unittest discover -s tests -v

NINGUN test pega contra la API real de Mercado Libre: todas las llamadas HTTP
estan mockeadas, tanto el POST /oauth/token como el GET /users/me. Tampoco se
toca la base productiva: este modulo repunta la app a un SQLite en memoria
mientras corre y la devuelve a DATABASE_URL al terminar, igual que FASE3-S1.

Se usa unittest (stdlib) para no sumar pytest como dependencia.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# La clave de cifrado tiene que existir antes de importar app.py: el chequeo es
# de arranque a proposito. En CI la pone el entorno; en local viene del .env.
os.environ.setdefault('CREDENTIALS_ENCRYPTION_KEY',
                      'sO1mHTMYm4Rfy9ii1YV8dqmM1J4KrHnQPy_2xGx0nMk=')
os.environ.setdefault('SECRET_KEY', 'clave-de-test')

import credencial_mercadolibre as credencial_meli  # noqa: E402
import cripto  # noqa: E402
import integracion_mercadolibre as meli  # noqa: E402
from app import app  # noqa: E402
from models import CanalVenta, CredencialCanal, Empresa, Usuario, db  # noqa: E402
from tests.ayuda_auth import request_anonimo  # noqa: E402

ENGINE_PRODUCTIVO = None

SECRETO_FALSO = 'secreto-de-test-no-es-el-real'
ACCESS_1 = 'APP_USR-8020619530821827-aaaa-token-de-prueba-1'
ACCESS_2 = 'APP_USR-8020619530821827-bbbb-token-de-prueba-2'
REFRESH_1 = 'TG-refresh-de-prueba-1'
REFRESH_2 = 'TG-refresh-de-prueba-2'
USER_ID_MELI = '123456789'
APODO = 'FERRETERIA.NACHI'


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
    """Lo minimo de requests.Response que usa integracion_mercadolibre.

    El body se serializa de verdad con json.dumps y se expone en `.text`: el
    modulo lee con json.loads(resp.text, parse_float=Decimal) y no con
    resp.json(), asi que un doble que devolviera el dict ya armado no probaria
    nada del parseo -- que es justamente lo que hay que proteger.
    """

    def __init__(self, status_code=200, datos=None, texto=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        if texto is not None:
            self.text = texto
        else:
            self.text = json.dumps(datos if datos is not None else {})

    def json(self):
        return json.loads(self.text)


def respuesta_token(access=ACCESS_1, refresh=REFRESH_1, expires_in=10800, **extra):
    """La respuesta del POST /oauth/token, con la forma de la doc oficial."""
    cuerpo = {
        'access_token': access,
        'token_type': 'bearer',
        'expires_in': expires_in,
        'scope': 'offline_access read write',
        'user_id': int(USER_ID_MELI),
        'refresh_token': refresh,
    }
    cuerpo.update(extra)
    return RespuestaFalsa(200, cuerpo)


def respuesta_usuario(**extra):
    cuerpo = {'id': int(USER_ID_MELI), 'nickname': APODO, 'site_id': 'MLA'}
    cuerpo.update(extra)
    return RespuestaFalsa(200, cuerpo)


def con_secreto():
    return mock.patch.dict(os.environ, {'MERCADOLIBRE_CLIENT_SECRET': SECRETO_FALSO})


class BaseWeb(unittest.TestCase):
    """Una empresa + un usuario logueado + los dos canales, sobre la base en
    memoria. Cada test arranca con las tablas vacias."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-MELI-S1')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(
            nombre='Roman Test',
            email='faseMeliS1@test.local',
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
        self.canal_id = self.canal().id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def canal(self):
        return CanalVenta.query.filter_by(
            empresa_id=self.empresa_id, tipo='mercadolibre').first()

    def sembrar_state(self, state, empresa_id=None):
        """Deja el state en la sesion, como lo dejaria GET /conectar."""
        with self.client.session_transaction() as sesion:
            sesion['meli_oauth_state'] = state
            sesion['meli_oauth_empresa_id'] = (
                self.empresa_id if empresa_id is None else empresa_id)

    def conectar_a_mano(self, access=ACCESS_1, refresh=REFRESH_1,
                        expira_en=None, activo=True):
        """Deja el canal conectado sin pasar por el OAuth, para los tests de
        refresco y verificacion."""
        canal = self.canal()
        canal.activo = activo
        canal.id_tienda_externo = USER_ID_MELI
        credencial = CredencialCanal(
            canal_id=canal.id,
            tipo_credencial='oauth2',
            access_token_cifrado=cripto.cifrar(access),
            refresh_token_cifrado=cripto.cifrar(refresh) if refresh else None,
            expira_en=(expira_en if expira_en is not None
                       else datetime.utcnow() + timedelta(hours=6)),
            activo=True,
        )
        db.session.add(credencial)
        db.session.commit()
        return credencial


# ===========================================================================
# PARTE 2 - Autorizacion
# ===========================================================================

class TestConectar(BaseWeb):
    """GET /integraciones/mercadolibre/conectar."""

    def test_conectar_arma_url_con_state_y_scopes(self):
        """La URL de autorizacion lleva los tres scopes y un state distinto
        cada vez.

        offline_access es el que importa: sin el, MeLi no devuelve
        refresh_token y la conexion se muere a las seis horas. El state, al
        ser distinto en cada pedido, es lo que hace que un callback fabricado
        no pueda adivinarlo.
        """
        primera = self.client.get('/integraciones/mercadolibre/conectar')
        query_1 = parse_qs(urlparse(primera.headers['Location']).query)

        self.assertEqual(primera.status_code, 302)
        self.assertTrue(primera.headers['Location'].startswith(meli.URL_AUTORIZAR))
        self.assertEqual(query_1['response_type'], ['code'])
        self.assertEqual(query_1['client_id'], ['8020619530821827'])
        self.assertEqual(query_1['redirect_uri'],
                         ['https://pyme-construccion-1jfr.onrender.com'
                          '/integraciones/mercadolibre/callback'])

        scopes = query_1['scope'][0].split()
        self.assertEqual(sorted(scopes), ['offline_access', 'read', 'write'])

        # Y el state cambia entre dos pedidos: si fuera fijo, alcanzaria con
        # verlo una vez para poder fabricar un callback valido.
        segunda = self.client.get('/integraciones/mercadolibre/conectar')
        query_2 = parse_qs(urlparse(segunda.headers['Location']).query)
        self.assertTrue(query_1['state'][0])
        self.assertNotEqual(query_1['state'][0], query_2['state'][0])

    def test_guarda_el_state_y_la_empresa_en_la_sesion(self):
        resp = self.client.get('/integraciones/mercadolibre/conectar')
        enviado = parse_qs(urlparse(resp.headers['Location']).query)['state'][0]

        with self.client.session_transaction() as sesion:
            self.assertEqual(sesion['meli_oauth_state'], enviado)
            self.assertEqual(sesion['meli_oauth_empresa_id'], self.empresa_id)

    def test_conectar_no_escribe_nada(self):
        self.client.get('/integraciones/mercadolibre/conectar')

        self.assertFalse(self.canal().activo)
        self.assertEqual(CredencialCanal.query.count(), 0)

    def test_requiere_login(self):
        resp = request_anonimo(self.ctx, 'get', '/integraciones/mercadolibre/conectar')
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('mercadolibre.com', resp.headers['Location'])


# ===========================================================================
# PARTE 2 - Callback
# ===========================================================================

class TestCallbackExitoso(BaseWeb):
    """El camino feliz: state valido + code valido -> tokens cifrados."""

    def _callback(self, state='state-valido', code='code-valido'):
        self.sembrar_state(state)
        with mock.patch.object(meli.requests, 'post',
                               return_value=respuesta_token()) as post, \
             mock.patch.object(meli.requests, 'get',
                               return_value=respuesta_usuario()) as get, \
             con_secreto():
            resp = self.client.get(
                '/integraciones/mercadolibre/callback?code=%s&state=%s' % (code, state))
        return resp, post, get

    def test_callback_guarda_credencial_cifrada(self):
        """Lo importante de la slice: leer la columna cruda no da el token.

        Se leen las dos columnas por SQL directo, sin pasar por el modelo, para
        que no haya forma de que un descifrado automatico disimule el problema.
        El refresh_token va cifrado igual que el access: sirve para fabricar
        access_tokens nuevos, asi que es igual de sensible.
        """
        self._callback()

        from sqlalchemy import text
        access, refresh = db.session.execute(text(
            'SELECT access_token_cifrado, refresh_token_cifrado FROM credencial_canal'
        )).one()

        for guardado, plano in ((access, ACCESS_1), (refresh, REFRESH_1)):
            self.assertIsNotNone(guardado)
            self.assertNotEqual(guardado, plano)
            self.assertNotIn(plano, guardado)
            # Es un token Fernet: prefijo de version 0x80 en base64url.
            self.assertTrue(guardado.startswith('gAAAAA'), guardado[:20])

        self.assertEqual(cripto.descifrar(access), ACCESS_1)
        self.assertEqual(cripto.descifrar(refresh), REFRESH_1)

    def test_activa_el_canal_y_guarda_el_vencimiento(self):
        antes = datetime.utcnow()
        resp, _, _ = self._callback()

        self.assertEqual(resp.status_code, 302)
        canal = self.canal()
        self.assertTrue(canal.activo)
        self.assertEqual(canal.id_tienda_externo, USER_ID_MELI)

        credencial = CredencialCanal.query.filter_by(canal_id=canal.id).one()
        self.assertEqual(credencial.scope, 'offline_access read write')
        # expires_in = 10800 segundos = 3 horas desde que MeLi lo emitio.
        self.assertIsNotNone(credencial.expira_en)
        self.assertGreaterEqual(credencial.expira_en, antes + timedelta(seconds=10790))
        self.assertLessEqual(credencial.expira_en,
                             datetime.utcnow() + timedelta(seconds=10800))

    def test_el_canje_va_form_urlencoded_con_los_cinco_parametros(self):
        """MeLi pide el body del token como form-urlencoded, no como JSON: es
        la diferencia con Mercado Pago que mas facil se pasa por alto."""
        _, post, _ = self._callback()

        self.assertEqual(post.call_args.args[0], meli.URL_TOKEN)
        self.assertNotIn('json', post.call_args.kwargs,
                         'MeLi rechaza el body JSON: tiene que ir en data=')
        cuerpo = post.call_args.kwargs['data']
        self.assertEqual(cuerpo['grant_type'], 'authorization_code')
        self.assertEqual(cuerpo['code'], 'code-valido')
        self.assertEqual(cuerpo['client_id'], '8020619530821827')
        self.assertEqual(cuerpo['client_secret'], SECRETO_FALSO)
        self.assertEqual(cuerpo['redirect_uri'], meli.redirect_uri_configurado())

        cabeceras = post.call_args.kwargs['headers']
        self.assertEqual(cabeceras['content-type'], 'application/x-www-form-urlencoded')
        self.assertEqual(cabeceras['accept'], 'application/json')

    def test_prueba_el_token_contra_users_me_antes_de_darlo_por_bueno(self):
        _, _, get = self._callback()

        self.assertTrue(get.call_args.args[0].endswith('/users/me'))
        self.assertEqual(get.call_args.kwargs['headers']['Authorization'],
                         'Bearer %s' % ACCESS_1)

    def test_reconectar_reutiliza_la_fila_de_credencial(self):
        self._callback(state='primera')
        self._callback(state='segunda')

        self.assertEqual(CredencialCanal.query.count(), 1)

    def test_no_toca_el_vinculo_con_la_cuenta_de_cobro(self):
        """El canal ya apunta a la cuenta de Nachi desde FASE-MP-S1. Conectar
        la credencial es otra pregunta y no puede reescribir esa."""
        canal = self.canal()
        canal.cuenta_cobro_id = None
        nombre_original = canal.nombre
        db.session.commit()

        self._callback()

        canal = self.canal()
        self.assertIsNone(canal.cuenta_cobro_id)
        self.assertEqual(canal.nombre, nombre_original)


class TestCallbackState(BaseWeb):
    """El state es lo unico que prueba que la vuelta es de un flujo nuestro.

    Mercado Libre no lo valida -- la doc lo dice explicito -- asi que si no se
    chequea aca no lo chequea nadie.
    """

    def _assert_no_conecto(self, resp):
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/integraciones', resp.headers['Location'])
        self.assertEqual(CredencialCanal.query.count(), 0)
        self.assertFalse(self.canal().activo)

    def test_callback_rechaza_state_que_no_matchea(self):
        """Previene CSRF: sin esto, un callback fabricado con un code del
        atacante engancharia la cuenta de MeLi de otro al canal de Roman."""
        self.sembrar_state('el-state-bueno')

        with mock.patch.object(meli.requests, 'post') as post, \
             mock.patch.object(meli.requests, 'get') as get, \
             con_secreto():
            resp = self.client.get(
                '/integraciones/mercadolibre/callback?code=code-valido&state=el-state-malo')

        self._assert_no_conecto(resp)
        # Y ni siquiera se canjeo el code: se corta antes de hablar con MeLi.
        post.assert_not_called()
        get.assert_not_called()

    def test_callback_sin_state_no_escribe(self):
        self.sembrar_state('el-state-bueno')

        with mock.patch.object(meli.requests, 'post') as post, con_secreto():
            resp = self.client.get('/integraciones/mercadolibre/callback?code=code-valido')

        self._assert_no_conecto(resp)
        post.assert_not_called()

    def test_callback_sin_state_en_sesion_no_escribe(self):
        with mock.patch.object(meli.requests, 'post') as post, con_secreto():
            resp = self.client.get(
                '/integraciones/mercadolibre/callback?code=code-valido&state=inventado')

        self._assert_no_conecto(resp)
        post.assert_not_called()

    def test_el_state_es_de_un_solo_uso(self):
        """Reusar el mismo state con otro code no vuelve a escribir."""
        self.sembrar_state('state-unico')
        with mock.patch.object(meli.requests, 'post', return_value=respuesta_token()), \
             mock.patch.object(meli.requests, 'get', return_value=respuesta_usuario()), \
             con_secreto():
            self.client.get(
                '/integraciones/mercadolibre/callback?code=uno&state=state-unico')

            with mock.patch.object(meli.requests, 'post') as post:
                resp = self.client.get(
                    '/integraciones/mercadolibre/callback?code=otro&state=state-unico')

        self.assertEqual(resp.status_code, 302)
        post.assert_not_called()

    def test_state_de_otra_empresa_no_escribe(self):
        """El state prueba que el flujo lo empezo la app; que lo haya empezado
        ESTA empresa es otro chequeo."""
        self.sembrar_state('state-valido', empresa_id=self.empresa_id + 999)

        with mock.patch.object(meli.requests, 'post') as post, con_secreto():
            resp = self.client.get(
                '/integraciones/mercadolibre/callback?code=code-valido&state=state-valido')

        self._assert_no_conecto(resp)
        post.assert_not_called()

    def test_requiere_login(self):
        resp = request_anonimo(self.ctx, 'get',
                               '/integraciones/mercadolibre/callback?code=x&state=y')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(CredencialCanal.query.count(), 0)


class TestCallbackFallido(BaseWeb):
    """Los caminos de error: no se guarda nada a medias."""

    def _con_state(self, respuesta=None, side_effect=None, query='code=code-valido'):
        self.sembrar_state('state-valido')
        with mock.patch.object(meli.requests, 'post', return_value=respuesta,
                               side_effect=side_effect), \
             mock.patch.object(meli.requests, 'get', return_value=respuesta_usuario()), \
             con_secreto():
            return self.client.get(
                '/integraciones/mercadolibre/callback?%s&state=state-valido' % query)

    def _assert_no_conecto(self, resp):
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(CredencialCanal.query.count(), 0)
        self.assertFalse(self.canal().activo)

    def test_usuario_que_cancela_la_autorizacion(self):
        self.sembrar_state('state-valido')
        with mock.patch.object(meli.requests, 'post') as post, con_secreto():
            resp = self.client.get('/integraciones/mercadolibre/callback'
                                   '?error=access_denied&state=state-valido')
        self._assert_no_conecto(resp)
        post.assert_not_called()

    def test_callback_sin_code(self):
        self.sembrar_state('state-valido')
        with mock.patch.object(meli.requests, 'post') as post, con_secreto():
            resp = self.client.get('/integraciones/mercadolibre/callback'
                                   '?state=state-valido')
        self._assert_no_conecto(resp)
        post.assert_not_called()

    def test_code_ya_usado_o_vencido(self):
        rechazo = RespuestaFalsa(400, {
            'error': 'invalid_grant',
            'error_description': 'Error validating grant. Your authorization code '
                                 'or refresh token may be expired or it was already used',
            'status': 400,
        })
        self._assert_no_conecto(self._con_state(respuesta=rechazo))

    def test_secreto_mal_configurado(self):
        rechazo = RespuestaFalsa(400, {'error': 'invalid_client'})
        self._assert_no_conecto(self._con_state(respuesta=rechazo))

    def test_respuesta_sin_access_token(self):
        incompleta = RespuestaFalsa(200, {'token_type': 'bearer'})
        self._assert_no_conecto(self._con_state(respuesta=incompleta))

    def test_sin_red(self):
        import requests as requests_real
        self._assert_no_conecto(
            self._con_state(side_effect=requests_real.ConnectionError('sin red')))

    def test_sin_secreto_configurado_no_explota(self):
        self.sembrar_state('state-valido')
        with mock.patch.object(meli.requests, 'post') as post, \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MERCADOLIBRE_CLIENT_SECRET', None)
            resp = self.client.get('/integraciones/mercadolibre/callback'
                                   '?code=code-valido&state=state-valido')
        self._assert_no_conecto(resp)
        post.assert_not_called()

    def test_sin_refresh_token_avisa_pero_guarda(self):
        """Sin offline_access no viene refresh_token. El access sirve seis
        horas, asi que no se tira la conexion -- pero tiene que avisar, porque
        se va a cortar sola."""
        self.sembrar_state('state-valido')
        sin_refresh = respuesta_token(refresh=None)
        with mock.patch.object(meli.requests, 'post', return_value=sin_refresh), \
             mock.patch.object(meli.requests, 'get', return_value=respuesta_usuario()), \
             con_secreto():
            resp = self.client.get('/integraciones/mercadolibre/callback'
                                   '?code=code-valido&state=state-valido')

        self.assertEqual(resp.status_code, 302)
        credencial = CredencialCanal.query.one()
        self.assertIsNotNone(credencial.access_token_cifrado)
        self.assertIsNone(credencial.refresh_token_cifrado)


# ===========================================================================
# PARTE 3 - Refresh del token
# ===========================================================================

class TestRefresh(BaseWeb):
    """El refresco corre adentro del lock de canal_venta y renueva el par."""

    def test_refresh_actualiza_ambos_tokens(self):
        """access Y refresh se renuevan juntos.

        El refresh_token de MeLi es de un solo uso: guardar solo el access
        dejaria en la base un refresh ya quemado, y el proximo refresco -- el
        de dentro de seis horas, cuando no haya nadie mirando -- fallaria con
        invalid_grant sin forma de recuperarse.
        """
        self.conectar_a_mano(access=ACCESS_1, refresh=REFRESH_1)

        with mock.patch.object(meli.requests, 'post',
                               return_value=respuesta_token(ACCESS_2, REFRESH_2)) as post, \
             con_secreto():
            devuelto = credencial_meli.refrescar_token(self.canal_id)

        self.assertEqual(devuelto, ACCESS_2)

        credencial = CredencialCanal.query.filter_by(canal_id=self.canal_id).one()
        self.assertEqual(cripto.descifrar(credencial.access_token_cifrado), ACCESS_2)
        self.assertEqual(cripto.descifrar(credencial.refresh_token_cifrado), REFRESH_2)
        # Y siguen cifrados: el refresco no puede degradar lo que guardo el callback.
        self.assertTrue(credencial.refresh_token_cifrado.startswith('gAAAAA'))

        # El pedido va con los cuatro parametros del grant de refresco, sin
        # redirect_uri (la doc lo pide solo en el canje del code).
        cuerpo = post.call_args.kwargs['data']
        self.assertEqual(cuerpo['grant_type'], 'refresh_token')
        self.assertEqual(cuerpo['refresh_token'], REFRESH_1)
        self.assertEqual(cuerpo['client_id'], '8020619530821827')
        self.assertEqual(cuerpo['client_secret'], SECRETO_FALSO)
        self.assertNotIn('redirect_uri', cuerpo)

    def test_refresh_usa_el_lock_de_canal_venta(self):
        """Dos refrescos no se pisan: el mismo SELECT ... FOR UPDATE sobre
        canal_venta que ya protege al sync.

        El lock de verdad lo hace Postgres; SQLite ignora el FOR UPDATE y aca
        no hay dos procesos, asi que lo que se verifica es lo que si se puede
        verificar en un test y es lo que decide si el lock sirve:

          a) que el lock se pida sobre la fila de canal_venta -- la misma que
             toma `_reservar_corrida`, porque dos locks distintos sobre el
             mismo recurso no protegen nada;
          b) que se pida ANTES del POST, no despues: un lock tomado despues de
             quemar el refresh_token no protege nada;
          c) que un segundo refresco mande el refresh NUEVO y nunca el viejo,
             que es el resultado observable de no pisarse.
        """
        from sqlalchemy.orm import Query as QueryBase

        self.conectar_a_mano(access=ACCESS_1, refresh=REFRESH_1)

        eventos = []
        original = QueryBase.with_for_update

        def espia(self, *args, **kwargs):
            entidades = [d['entity'] for d in self.column_descriptions]
            eventos.append(('lock', tuple(e.__name__ for e in entidades if e)))
            return original(self, *args, **kwargs)

        enviados = []

        def post_falso(url, **kwargs):
            enviados.append(kwargs['data']['refresh_token'])
            eventos.append(('post', kwargs['data']['grant_type']))
            # Cada refresco devuelve un par nuevo, como hace MeLi de verdad.
            n = len(enviados)
            return respuesta_token('access-%d' % n, 'refresh-%d' % n)

        with mock.patch.object(QueryBase, 'with_for_update', espia), \
             mock.patch.object(meli.requests, 'post', side_effect=post_falso), \
             con_secreto():
            credencial_meli.refrescar_token(self.canal_id)
            credencial_meli.refrescar_token(self.canal_id)

        # (a) y (b): lock sobre canal_venta, y antes del POST, las dos veces.
        self.assertEqual(eventos, [
            ('lock', ('CanalVenta',)), ('post', 'refresh_token'),
            ('lock', ('CanalVenta',)), ('post', 'refresh_token'),
        ])

        # (c): el segundo refresco uso el token que dejo el primero, no el
        # original. Mandar dos veces el mismo seria matar el canal.
        self.assertEqual(enviados, [REFRESH_1, 'refresh-1'])

    def test_refresh_que_falla_no_deja_el_lock_tomado_ni_pisa_lo_guardado(self):
        """Si MeLi rechaza el refresco, la credencial queda como estaba y la
        transaccion se cierra: un canal que falla no puede dejar trabada la
        fila que el sync necesita."""
        self.conectar_a_mano(access=ACCESS_1, refresh=REFRESH_1)
        rechazo = RespuestaFalsa(400, {'error': 'invalid_grant'})

        with mock.patch.object(meli.requests, 'post', return_value=rechazo), \
             con_secreto():
            with self.assertRaises(meli.ErrorMercadoLibre) as caso:
                credencial_meli.refrescar_token(self.canal_id)

        self.assertTrue(caso.exception.reconectar,
                        'invalid_grant no se arregla reintentando')
        self.assertFalse(db.session().in_transaction(),
                         'la transaccion tenia que cerrarse y soltar el lock')

        credencial = CredencialCanal.query.filter_by(canal_id=self.canal_id).one()
        self.assertEqual(cripto.descifrar(credencial.access_token_cifrado), ACCESS_1)
        self.assertEqual(cripto.descifrar(credencial.refresh_token_cifrado), REFRESH_1)

    def test_sin_refresh_token_guardado_pide_reconectar(self):
        self.conectar_a_mano(refresh=None)

        with mock.patch.object(meli.requests, 'post') as post, con_secreto():
            with self.assertRaises(meli.ErrorMercadoLibre) as caso:
                credencial_meli.refrescar_token(self.canal_id)

        self.assertTrue(caso.exception.reconectar)
        post.assert_not_called()

    def test_access_token_vigente_no_gasta_el_refresh_si_no_hace_falta(self):
        """El refresh es de un solo uso: renovarlo cuando el access todavia
        sirve es tirar un recurso finito y abrir una ventana de carrera al
        pedo."""
        self.conectar_a_mano(expira_en=datetime.utcnow() + timedelta(hours=5))

        with mock.patch.object(meli.requests, 'post') as post, con_secreto():
            devuelto = credencial_meli.access_token_vigente(self.canal_id)

        self.assertEqual(devuelto, ACCESS_1)
        post.assert_not_called()

    def test_access_token_vigente_refresca_el_vencido(self):
        self.conectar_a_mano(expira_en=datetime.utcnow() - timedelta(minutes=1))

        with mock.patch.object(meli.requests, 'post',
                               return_value=respuesta_token(ACCESS_2, REFRESH_2)), \
             con_secreto():
            devuelto = credencial_meli.access_token_vigente(self.canal_id)

        self.assertEqual(devuelto, ACCESS_2)


# ===========================================================================
# PARTE 4 - Verificacion de credenciales
# ===========================================================================

class TestVerificarCredenciales(BaseWeb):
    """GET /users/me como prueba de vida de la credencial."""

    def test_verificar_credenciales_detecta_token_invalido(self):
        """Un 401 se reporta como desconectado, no como error 500.

        Con un token que vence cada seis horas, ver el canal caido va a ser
        rutina: tiene que ser un estado de la pagina, no una excepcion.
        """
        no_autorizado = RespuestaFalsa(401, {
            'message': 'invalid_token', 'error': 'not_found', 'status': 401,
        })

        with mock.patch.object(meli.requests, 'get', return_value=no_autorizado):
            estado = meli.verificar_credenciales(ACCESS_1)

        self.assertFalse(estado['conectado'])
        self.assertIsNone(estado['usuario'])
        self.assertIn('conectar el canal', estado['motivo'].lower())

    def test_verificar_credenciales_con_token_bueno(self):
        with mock.patch.object(meli.requests, 'get', return_value=respuesta_usuario()):
            estado = meli.verificar_credenciales(ACCESS_1)

        self.assertTrue(estado['conectado'])
        self.assertEqual(estado['usuario']['nickname'], APODO)
        self.assertIsNone(estado['motivo'])

    def test_verificar_credenciales_sin_red_no_levanta(self):
        import requests as requests_real
        with mock.patch.object(meli.requests, 'get',
                               side_effect=requests_real.ConnectionError('sin red')):
            estado = meli.verificar_credenciales(ACCESS_1)

        self.assertFalse(estado['conectado'])
        self.assertTrue(estado['motivo'])

    def test_verificar_conexion_no_le_pega_a_la_api_si_ya_vencio(self):
        """Pintar una pagina no puede quemar el refresh_token de un solo uso,
        ni gastar una llamada para confirmar algo que ya sabemos."""
        self.conectar_a_mano(expira_en=datetime.utcnow() - timedelta(minutes=1))

        with mock.patch.object(meli.requests, 'get') as get, \
             mock.patch.object(meli.requests, 'post') as post:
            estado = credencial_meli.verificar_conexion(self.canal_id)

        self.assertFalse(estado['conectado'])
        self.assertTrue(estado['vencido'])
        get.assert_not_called()
        post.assert_not_called()

    def test_verificar_conexion_sin_credencial(self):
        estado = credencial_meli.verificar_conexion(self.canal_id)

        self.assertFalse(estado['conectado'])
        self.assertFalse(estado['vencido'])

    def test_la_pagina_de_integraciones_muestra_el_canal_caido_sin_romperse(self):
        """El 401 llega hasta la vista: la tarjeta dice "Sin acceso" y la
        pagina responde 200."""
        self.conectar_a_mano()
        no_autorizado = RespuestaFalsa(401, {'message': 'invalid_token'})

        with mock.patch.object(meli.requests, 'get', return_value=no_autorizado):
            resp = self.client.get('/integraciones')

        self.assertEqual(resp.status_code, 200)
        self.assertIn('Sin acceso', resp.get_data(as_text=True))

    def test_la_pagina_muestra_el_apodo_cuando_el_token_sirve(self):
        self.conectar_a_mano()

        with mock.patch.object(meli.requests, 'get', return_value=respuesta_usuario()):
            resp = self.client.get('/integraciones')

        cuerpo = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(APODO, cuerpo)
        self.assertIn('/integraciones/mercadolibre/conectar', cuerpo)

    def test_la_pagina_no_verifica_un_canal_sin_conectar(self):
        """Sin credencial no hay nada que preguntarle a la API."""
        with mock.patch.object(meli.requests, 'get') as get:
            resp = self.client.get('/integraciones')

        self.assertEqual(resp.status_code, 200)
        get.assert_not_called()


# ===========================================================================
# Parseo de montos
# ===========================================================================

class TestMontosDecimal(unittest.TestCase):
    """Los numeros de la API entran como Decimal, nunca como float.

    No necesita base: es una propiedad del transporte.
    """

    def test_montos_se_parsean_como_decimal_no_float(self):
        """Los numeros JSON entran como Decimal y llegan intactos.

        El monto elegido tiene mas digitos significativos de los que un float
        de doble precision puede guardar: por float vuelve como
        12345678901234568, con los centavos ya perdidos, y de ahi no se
        recupera. Por eso el paso por float tiene que evitarse en la LECTURA y
        no corregirse despues.
        """
        crudo = ('{"id": 1, "nickname": "X", '
                 '"monto": 12345678901234567.89, "comision": 0.1}')

        with mock.patch.object(meli.requests, 'get',
                               return_value=RespuestaFalsa(200, texto=crudo)):
            datos = meli.traer_usuario(ACCESS_1)

        self.assertIsInstance(datos['monto'], Decimal)
        self.assertIsInstance(datos['comision'], Decimal)
        self.assertEqual(datos['monto'], Decimal('12345678901234567.89'))
        self.assertEqual(datos['comision'], Decimal('0.1'))

        # Y la prueba de que la distincion importa: por el parseo de siempre
        # -- el que usa resp.json() -- el mismo body vuelve ya corrido.
        por_float = json.loads(crudo)['monto']
        self.assertIsInstance(por_float, float)
        self.assertNotEqual(Decimal(str(por_float)), Decimal('12345678901234567.89'))

        # Tres decimos suman exactamente uno solo si nunca pasaron por float.
        self.assertEqual(datos['comision'] * 3, Decimal('0.3'))
        self.assertNotEqual(json.loads(crudo)['comision'] * 3, 0.3)

    def test_el_canje_del_code_tambien_pasa_por_decimal(self):
        """La regla es del transporte, no de cada endpoint: hoy el token no
        trae montos, pero el dia que traiga algo con centavos ya esta cubierto."""
        crudo = ('{"access_token": "%s", "refresh_token": "%s", "user_id": %s, '
                 '"expires_in": 10800, "monto": 0.1}'
                 % (ACCESS_1, REFRESH_1, USER_ID_MELI))

        with mock.patch.object(meli.requests, 'post',
                               return_value=RespuestaFalsa(200, texto=crudo)), \
             con_secreto():
            token = meli.intercambiar_code('code', 'https://x/callback')

        self.assertEqual(token['access_token'], ACCESS_1)
        # El dict normalizado no expone el monto, pero el parseo intermedio si
        # lo vio: lo que se verifica es que no exploto y que el user_id llego
        # como string, no como float.
        self.assertEqual(token['user_id'], USER_ID_MELI)


if __name__ == '__main__':
    unittest.main(verbosity=2)
