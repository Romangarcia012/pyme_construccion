# -*- coding: utf-8 -*-
"""Tests de FASE-MELI-S2 (catalogo de Mercado Libre: productos y variaciones).

    python -m unittest discover -s tests -v

NINGUN test pega contra la API real de Mercado Libre: todas las llamadas HTTP
estan mockeadas. Tampoco se toca la base productiva: este modulo repunta la app
a un SQLite en memoria mientras corre y la devuelve a DATABASE_URL al terminar,
igual que FASE-MELI-S1.

Se usa unittest (stdlib) para no sumar pytest como dependencia.
"""

import json
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('CREDENTIALS_ENCRYPTION_KEY',
                      'sO1mHTMYm4Rfy9ii1YV8dqmM1J4KrHnQPy_2xGx0nMk=')
os.environ.setdefault('SECRET_KEY', 'clave-de-test')

import ingestor_mercadolibre as ing  # noqa: E402
import integracion_mercadolibre as meli  # noqa: E402
import sync_mercadolibre  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    Empresa,
    MapeoProductoCanal,
    Producto,
    db,
)

ENGINE_PRODUCTIVO = None

TOKEN = 'APP_USR-8020619530821827-token-de-prueba'
USER_ID_MELI = '435665654'


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


# ---------------------------------------------------------------------------
# Payloads de ejemplo, con la forma que documenta la API de Mercado Libre
# ---------------------------------------------------------------------------

def item(id_item='MLA111', titulo='Guantes de trabajo', precio=1234.56,
         cantidad=7, status='active', logistic_type='drop_off',
         attributes=None, variations=None):
    """Un item tal como lo devuelve el `body` de /items/bulk."""
    crudo = {
        'id': id_item,
        'title': titulo,
        'price': precio,
        'available_quantity': cantidad,
        'status': status,
        'logistic_type': logistic_type,
        'attributes': attributes if attributes is not None else [
            atributo_sku('SKU-GUANTES'),
        ],
    }
    if variations is not None:
        crudo['variations'] = variations
    return crudo


def atributo_sku(valor, id_atributo='SELLER_SKU'):
    return {'id': id_atributo, 'name': 'SKU', 'value_name': valor}


def variacion(id_var='888111', precio=1500.10, cantidad=3, sku=None,
              combinaciones=None):
    crudo = {
        'id': id_var,
        'price': precio,
        'available_quantity': cantidad,
        'attribute_combinations': combinaciones if combinaciones is not None else [
            {'id': 'COLOR', 'name': 'Color', 'value_name': 'Rojo'},
        ],
        'attributes': [atributo_sku(sku)] if sku else [],
    }
    return crudo


def sobre_bulk(cuerpo, status_code=200):
    """El envelope de /items/bulk: status_code + body."""
    return {'status_code': status_code, 'body': cuerpo}


def ingestor():
    return ing.IngestorMercadoLibre(
        canal_id=1, credenciales={'access_token': TOKEN},
        id_tienda_externo=USER_ID_MELI)


def normalizar_uno(crudo_item):
    """Un item -> la lista de dicts normalizados, uno por variacion vendible."""
    ing_ = ingestor()
    return [ing_.normalizar(ing.ENTIDAD_PRODUCTO, par)
            for par in ing_.variantes_de_producto(crudo_item)]


# ---------------------------------------------------------------------------
# SKU
# ---------------------------------------------------------------------------

class TestSku(unittest.TestCase):

    def test_lee_sku_de_seller_sku(self):
        """El SKU sale del atributo SELLER_SKU, no de seller_custom_field.

        Es la trampa de la API: `seller_custom_field` sigue existiendo como
        campo top-level, esta deprecado y en las publicaciones nuevas viene en
        null. Un ingestor que lo leyera sincronizaria el catalogo con SKUs
        fabricados sin que nada fallara.
        """
        crudo = item(attributes=[atributo_sku('FERR-0042')])
        crudo['seller_custom_field'] = 'CODIGO-VIEJO-QUE-NO-VA'

        datos = normalizar_uno(crudo)[0]

        self.assertEqual(datos['sku'], 'FERR-0042')
        self.assertEqual(datos['sku_externo'], 'FERR-0042')

    def test_lee_sku_de_values_cuando_no_hay_value_name(self):
        """El mismo atributo, con el dato adentro de `values[0].name`."""
        crudo = item(attributes=[{
            'id': 'SELLER_SKU', 'name': 'SKU', 'value_name': '',
            'values': [{'id': None, 'name': 'FERR-0099'}],
        }])

        self.assertEqual(normalizar_uno(crudo)[0]['sku'], 'FERR-0099')

    def test_fallback_sku_sin_seller_sku(self):
        """Sin SELLER_SKU se fabrica ML-{item}-{var}, con prefijo visible.

        `sku_externo` queda en None y no en el SKU fabricado: la columna
        significa "el codigo que puso el vendedor", y ahi no hay ninguno.
        """
        sin_atributos = item(id_item='MLA777', attributes=[])

        datos = normalizar_uno(sin_atributos)[0]

        self.assertEqual(datos['sku'], 'ML-MLA777-0')
        self.assertIsNone(datos['sku_externo'])

    def test_fallback_sku_usa_el_id_de_la_variacion(self):
        con_variaciones = item(
            id_item='MLA777', attributes=[],
            variations=[variacion(id_var='555000', sku=None)])

        datos = normalizar_uno(con_variaciones)[0]

        self.assertEqual(datos['sku'], 'ML-MLA777-555000')

    def test_el_sku_de_la_variacion_gana_sobre_el_del_item(self):
        """Dos variaciones con SKU propio no pueden colapsar en un solo SKU.

        Si ganara el del item, las dos filas de producto tendrian el mismo SKU,
        la UNIQUE (empresa_id, sku) las juntaria en una sola y el margen de las
        dos quedaria calculado sobre el precio de una.
        """
        crudo = item(
            attributes=[atributo_sku('SKU-DEL-ITEM')],
            variations=[variacion(id_var='1', sku='SKU-ROJO'),
                        variacion(id_var='2', sku='SKU-AZUL')])

        skus = [d['sku'] for d in normalizar_uno(crudo)]

        self.assertEqual(skus, ['SKU-ROJO', 'SKU-AZUL'])

    def test_variacion_sin_sku_propio_hereda_el_del_item(self):
        crudo = item(attributes=[atributo_sku('SKU-DEL-ITEM')],
                     variations=[variacion(id_var='1', sku=None)])

        self.assertEqual(normalizar_uno(crudo)[0]['sku'], 'SKU-DEL-ITEM')


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------

class TestStock(unittest.TestCase):

    def test_stock_none_si_es_full(self):
        """Full -> NULL, no 0.

        El stock de una publicacion Full vive en el deposito de Mercado Libre y
        no en available_quantity. Un 0 afirmaria "no queda ninguno" y frenaria
        la venta de un producto que tiene stock de sobra.
        """
        crudo = item(logistic_type='fulfillment', cantidad=0)

        self.assertIsNone(normalizar_uno(crudo)[0]['stock'])

    def test_stock_none_si_es_full_aunque_venga_un_numero(self):
        """Ni siquiera un available_quantity con numero cambia el criterio: en
        Full ese numero no es el stock vendible."""
        crudo = item(logistic_type='fulfillment', cantidad=42)

        self.assertIsNone(normalizar_uno(crudo)[0]['stock'])

    def test_stock_real_si_no_es_full(self):
        crudo = item(logistic_type='drop_off', cantidad=7)

        self.assertEqual(normalizar_uno(crudo)[0]['stock'], 7)

    def test_stock_cero_real_entra_como_cero(self):
        """Cero de verdad SI es un dato: es el que deberia frenar una venta."""
        crudo = item(logistic_type='cross_docking', cantidad=0)

        self.assertEqual(normalizar_uno(crudo)[0]['stock'], 0)

    def test_stock_rangueado_cae_en_none(self):
        """'RANGO_1_50' no es un stock: no se descuenta ni se compara."""
        crudo = item(cantidad='RANGO_1_50')

        self.assertIsNone(normalizar_uno(crudo)[0]['stock'])

    def test_stock_de_la_variacion_y_no_el_del_item(self):
        """En un item con variaciones el stock es por variacion.

        El del item es la suma; repartirla entre las variaciones seria
        inventarla.
        """
        crudo = item(cantidad=10, variations=[
            variacion(id_var='1', cantidad=3, sku='A'),
            variacion(id_var='2', cantidad=7, sku='B'),
        ])

        self.assertEqual([d['stock'] for d in normalizar_uno(crudo)], [3, 7])

    def test_stock_bool_no_se_guarda_como_uno(self):
        """isinstance(True, int) es True en Python: un bool colado en el campo
        no puede terminar guardado como 1 unidad."""
        crudo = item(cantidad=True)

        self.assertIsNone(normalizar_uno(crudo)[0]['stock'])


# ---------------------------------------------------------------------------
# Montos
# ---------------------------------------------------------------------------

class TestMontos(unittest.TestCase):

    def test_monto_parseado_como_decimal(self):
        """El precio llega a normalizar() como Decimal exacto, sin pasar por
        float en ningun punto del camino.

        Se arranca del texto JSON crudo y se baja por el transporte real
        (_json_de_bulk), que es donde vive `parse_float=Decimal`. Un test que
        armara el dict a mano no probaria nada: probaria el dict.
        """
        texto = json.dumps([sobre_bulk(item(precio=1234.56))])
        crudos = meli._cuerpos_de_bulk(
            meli._json_de_bulk(RespuestaFalsa(200, texto=texto), 'test'))

        precio = normalizar_uno(crudos[0])[0]['precio_lista']

        self.assertIsInstance(precio, Decimal)
        self.assertNotIsInstance(precio, float)
        self.assertEqual(precio, Decimal('1234.56'))

    def test_el_precio_de_la_variacion_gana_sobre_el_del_item(self):
        crudo = item(precio=1000, variations=[
            variacion(id_var='1', precio=1500.10, sku='A')])

        self.assertEqual(normalizar_uno(crudo)[0]['precio_lista'],
                         Decimal('1500.10'))

    def test_variacion_sin_precio_cae_al_del_item(self):
        var = variacion(id_var='1', sku='A')
        del var['price']
        crudo = item(precio=999.99, variations=[var])

        self.assertEqual(normalizar_uno(crudo)[0]['precio_lista'],
                         Decimal('999.99'))

    def test_a_decimal_no_arrastra_el_binario_del_float(self):
        """Rescate de un payload que ya paso por json.load() a secas: se
        convierte con repr() y no con Decimal(float), que arrastraria los 17
        digitos del binario."""
        self.assertEqual(ing.a_decimal(0.1), Decimal('0.1'))


# ---------------------------------------------------------------------------
# Resto del mapeo
# ---------------------------------------------------------------------------

class TestMapeo(unittest.TestCase):

    def test_mapeo_calza_sin_tocar_modelo(self):
        """id_producto_externo / id_variante_externo salen con la forma que ya
        espera mapeo_producto_canal: el item_id, el variation_id, y '' -no
        NULL- cuando el item no tiene variaciones."""
        con_var = normalizar_uno(
            item(id_item='MLA111', variations=[variacion(id_var='888111', sku='A')]))[0]
        sin_var = normalizar_uno(item(id_item='MLA222'))[0]

        self.assertEqual(con_var['id_producto_externo'], 'MLA111')
        self.assertEqual(con_var['id_variante_externo'], '888111')

        self.assertEqual(sin_var['id_producto_externo'], 'MLA222')
        self.assertEqual(sin_var['id_variante_externo'], '')
        self.assertIsNotNone(sin_var['id_variante_externo'])

    def test_el_nombre_lleva_los_valores_de_la_variacion(self):
        crudo = item(titulo='Guantes de trabajo', variations=[
            variacion(id_var='1', sku='A', combinaciones=[
                {'id': 'COLOR', 'value_name': 'Rojo'},
                {'id': 'SIZE', 'value_name': 'M'},
            ])])

        self.assertEqual(normalizar_uno(crudo)[0]['nombre'],
                         'Guantes de trabajo (Rojo / M)')

    def test_item_sin_variaciones_conserva_el_titulo(self):
        self.assertEqual(
            normalizar_uno(item(titulo='Martillo'))[0]['nombre'], 'Martillo')

    def test_publicacion_pausada_queda_inactiva(self):
        """Pausada no se puede comprar hoy, pero la fila del producto sigue
        existiendo: se le vendio en el pasado y su margen se sigue calculando."""
        self.assertTrue(normalizar_uno(item(status='active'))[0]['activo'])
        self.assertFalse(normalizar_uno(item(status='paused'))[0]['activo'])
        self.assertFalse(normalizar_uno(item(status='closed'))[0]['activo'])

    def test_costo_unitario_normalizado_siempre_none(self):
        """Mercado Libre no sabe cuanto costo la mercaderia."""
        self.assertIsNone(normalizar_uno(item())[0]['costo_unitario'])

    def test_normalizar_rechaza_entidades_de_S3(self):
        with self.assertRaises(ing.ErrorIngesta):
            ingestor().normalizar('pedido', {})

    def test_no_trae_pedidos_ni_pagos(self):
        """S2 no toca pedidos ni comision: eso es S3."""
        ing_ = ingestor()
        self.assertEqual(list(ing_.traer_pedidos()), [])
        self.assertEqual(list(ing_.traer_pagos()), [])


# ---------------------------------------------------------------------------
# Transporte: /items/search + /items/bulk
# ---------------------------------------------------------------------------

class TestTransporte(unittest.TestCase):

    def test_bulk_lee_status_code_y_body(self):
        """El envelope nuevo (status_code + body), que es el de /items/bulk."""
        datos = [sobre_bulk(item(id_item='MLA1')), sobre_bulk(item(id_item='MLA2'))]

        cuerpos = meli._cuerpos_de_bulk(datos)

        self.assertEqual([c['id'] for c in cuerpos], ['MLA1', 'MLA2'])

    def test_bulk_tambien_lee_el_envelope_viejo(self):
        """`code` en vez de `status_code`: la forma de /items?ids=, deprecado
        el 25/10/2026. Se acepta para que un payload guardado de antes del
        cambio siga siendo legible."""
        cuerpos = meli._cuerpos_de_bulk([{'code': 200, 'body': item(id_item='MLA9')}])

        self.assertEqual([c['id'] for c in cuerpos], ['MLA9'])

    def test_bulk_saltea_los_sobres_que_no_vinieron_200(self):
        """Un item borrado entre el /search y el /bulk no voltea la tanda."""
        datos = [sobre_bulk(item(id_item='MLA1')),
                 {'status_code': 404, 'body': {'message': 'not found'}},
                 sobre_bulk(item(id_item='MLA3'))]

        cuerpos = meli._cuerpos_de_bulk(datos)

        self.assertEqual([c['id'] for c in cuerpos], ['MLA1', 'MLA3'])

    def test_listar_items_pagina_hasta_el_final(self):
        """Se corta por lote corto, no por `paging.total`."""
        pagina_1 = RespuestaFalsa(200, {
            'results': ['MLA%d' % n for n in range(meli.ITEMS_POR_PAGINA)],
            'paging': {'total': 9999},
        })
        pagina_2 = RespuestaFalsa(200, {'results': ['MLA-ultimo'],
                                        'paging': {'total': 9999}})

        with mock.patch.object(meli, '_get_api',
                               side_effect=[pagina_1, pagina_2]) as get:
            ids = meli.listar_items(USER_ID_MELI, TOKEN)

        self.assertEqual(len(ids), meli.ITEMS_POR_PAGINA + 1)
        self.assertEqual(ids[-1], 'MLA-ultimo')
        self.assertEqual(get.call_args_list[0].kwargs['params']['offset'], 0)
        self.assertEqual(get.call_args_list[1].kwargs['params']['offset'],
                         meli.ITEMS_POR_PAGINA)

    def test_listar_items_catalogo_vacio(self):
        """Una cuenta sin publicaciones devuelve [], no un error."""
        with mock.patch.object(meli, '_get_api',
                               return_value=RespuestaFalsa(200, {'results': []})):
            self.assertEqual(meli.listar_items(USER_ID_MELI, TOKEN), [])

    def test_listar_items_avisa_si_pasa_el_tope_de_offset(self):
        """Mas de 1000 items necesita search_type=scan. Se dice en vez de
        devolver un catalogo cortado en silencio."""
        llena = RespuestaFalsa(200, {
            'results': ['MLA%d' % n for n in range(meli.ITEMS_POR_PAGINA)]})

        with mock.patch.object(meli, '_get_api', return_value=llena):
            with self.assertRaises(meli.ErrorMercadoLibre) as caja:
                meli.listar_items(USER_ID_MELI, TOKEN)

        self.assertIn('scan', caja.exception.detalle)

    def test_detalle_items_parte_en_tandas(self):
        ids = ['MLA%d' % n for n in range(meli.IDS_POR_TANDA + 5)]
        respuesta = RespuestaFalsa(200, [sobre_bulk(item())])

        with mock.patch.object(meli, '_get_api', return_value=respuesta) as get:
            meli.detalle_items(ids, TOKEN)

        self.assertEqual(get.call_count, 2)
        primera = get.call_args_list[0].kwargs['params']['ids'].split(',')
        self.assertEqual(len(primera), meli.IDS_POR_TANDA)

    def test_token_vencido_pide_reconectar(self):
        with mock.patch.object(meli, '_get_api',
                               return_value=RespuestaFalsa(401, {'message': 'invalid token'})):
            with self.assertRaises(meli.ErrorMercadoLibre) as caja:
                meli.listar_items(USER_ID_MELI, TOKEN)

        self.assertTrue(caja.exception.reconectar)


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

class TestPersistencia(unittest.TestCase):
    """El upsert contra la base, con el catalogo mockeado."""

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-MELI-S2')
        db.session.add(self.empresa)
        db.session.flush()

        self.canal = CanalVenta(empresa_id=self.empresa.id, tipo='mercadolibre',
                                nombre='Mercado Libre', activo=True,
                                id_tienda_externo=USER_ID_MELI)
        db.session.add(self.canal)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _correr(self, items):
        """Una corrida completa con el catalogo dado, sin red ni sync_log."""
        ing_ = ingestor()
        with mock.patch.object(ing_, 'traer_productos', return_value=items):
            return sync_mercadolibre._sincronizar_productos(self.canal, ing_, None)

    def test_guarda_producto_y_mapeo(self):
        resumen = self._correr([item(id_item='MLA111', attributes=[atributo_sku('FERR-1')])])

        self.assertEqual(resumen['leidos'], 1)
        self.assertEqual(resumen['nuevos'], 1)
        self.assertEqual(resumen['error'], 0)

        producto = Producto.query.filter_by(empresa_id=self.empresa.id,
                                            sku='FERR-1').one()
        mapeo = MapeoProductoCanal.query.filter_by(canal_id=self.canal.id).one()

        self.assertEqual(mapeo.producto_id, producto.id)
        self.assertEqual(mapeo.id_producto_externo, 'MLA111')
        self.assertEqual(mapeo.id_variante_externo, '')
        self.assertEqual(mapeo.sku_externo, 'FERR-1')

    def test_una_fila_por_variacion(self):
        crudo = item(id_item='MLA111', variations=[
            variacion(id_var='1', sku='FERR-ROJO'),
            variacion(id_var='2', sku='FERR-AZUL'),
        ])

        self._correr([crudo])

        self.assertEqual(Producto.query.count(), 2)
        self.assertEqual(
            sorted(m.id_variante_externo
                   for m in MapeoProductoCanal.query.all()), ['1', '2'])

    def test_no_pisa_costo_unitario_existente(self):
        """El costo lo carga Roman a mano; un re-sync no puede borrarlo.

        Si lo pisara con NULL, el unico dato de costo que existe desapareceria
        y todos los margenes quedarian en cero.
        """
        self._correr([item(id_item='MLA111', attributes=[atributo_sku('FERR-1')])])

        producto = Producto.query.filter_by(sku='FERR-1').one()
        producto.costo_unitario = Decimal('800.25')
        db.session.commit()

        resumen = self._correr([
            item(id_item='MLA111', titulo='Guantes renombrados',
                 precio=2000, attributes=[atributo_sku('FERR-1')])])

        self.assertEqual(resumen['nuevos'], 0)
        self.assertEqual(resumen['actualizados'], 1)

        producto = Producto.query.filter_by(sku='FERR-1').one()
        self.assertEqual(producto.costo_unitario, Decimal('800.25'))
        # Lo que si se refresca, para probar que la corrida efectivamente paso
        # por encima del producto y no lo salteo.
        self.assertEqual(producto.nombre, 'Guantes renombrados')
        self.assertEqual(producto.precio_lista, Decimal('2000'))

    def test_resync_no_duplica_ni_producto_ni_mapeo(self):
        crudo = item(id_item='MLA111', attributes=[atributo_sku('FERR-1')])

        self._correr([crudo])
        self._correr([crudo])

        self.assertEqual(Producto.query.count(), 1)
        self.assertEqual(MapeoProductoCanal.query.count(), 1)

    def test_el_stock_se_refresca_a_null_cuando_pasa_a_full(self):
        """La fuente de verdad del stock es el canal, NULL incluido.

        Un producto que se muda a Full tiene que volver a "no controlado" y no
        quedarse con el ultimo numero que se vio.
        """
        self._correr([item(id_item='MLA111', attributes=[atributo_sku('FERR-1')],
                           cantidad=7, logistic_type='drop_off')])
        self.assertEqual(Producto.query.filter_by(sku='FERR-1').one().stock, 7)

        self._correr([item(id_item='MLA111', attributes=[atributo_sku('FERR-1')],
                           cantidad=7, logistic_type='fulfillment')])
        self.assertIsNone(Producto.query.filter_by(sku='FERR-1').one().stock)

    def test_un_item_roto_no_voltea_la_corrida(self):
        """Cada SKU va en su propio SAVEPOINT: uno malo no se lleva el resto.

        Se hace fallar el mapeo del item del medio. Sin el savepoint por
        registro, ese fallo dejaria la sesion inutilizable y la corrida
        perderia tambien el item que ya se habia escrito y el que faltaba.
        """
        bueno = item(id_item='MLA111', attributes=[atributo_sku('FERR-1')])
        malo = item(id_item='MLA222', attributes=[atributo_sku('FERR-2')])
        otro = item(id_item='MLA333', attributes=[atributo_sku('FERR-3')])

        ing_ = ingestor()
        original = ing_._normalizar_producto

        def revienta_en_el_del_medio(par):
            if par['producto']['id'] == 'MLA222':
                raise ValueError('payload roto')
            return original(par)

        with mock.patch.object(ing_, 'traer_productos',
                               return_value=[bueno, malo, otro]),                 mock.patch.object(ing_, '_normalizar_producto',
                                  side_effect=revienta_en_el_del_medio):
            resumen = sync_mercadolibre._sincronizar_productos(self.canal, ing_, None)

        self.assertEqual(resumen['leidos'], 3)
        self.assertEqual(resumen['error'], 1)
        self.assertEqual(resumen['nuevos'], 2)
        self.assertEqual(
            sorted(p.sku for p in Producto.query.all()), ['FERR-1', 'FERR-3'])


if __name__ == '__main__':
    unittest.main()
