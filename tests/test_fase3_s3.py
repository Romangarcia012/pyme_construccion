# -*- coding: utf-8 -*-
"""Tests de la FASE3-S3 (stock de productos desde Tiendanube).

    python -m unittest discover -s tests -v

La slice agrega un solo dato -- `producto.stock` -- pero con dos reglas que
conviene tener escritas al lado, porque son opuestas entre si:

    costo_unitario  el sync NO lo pisa nunca (lo carga Roman a mano)
    stock           el sync SI lo pisa en cada corrida, NULL incluido

El contraste esta explicito en TestStockContraCosto: el test de FASE3-S2
`test_no_pisa_el_costo_que_cargo_roman` prueba una mitad y estos prueban la
inversa.

La otra regla que se prueba aca es que NULL y 0 no son lo mismo: NULL es "la
tienda no lleva la cuenta de este producto" y 0 es "no queda ninguno".

Igual que en FASE3-S2, ningun test sale a internet ni toca la base productiva:
se reusan la app en SQLite en memoria y la API mockeada de ese modulo.
"""

import copy
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ingestor_tiendanube  # noqa: E402
from ingestor_canal import ENTIDAD_PRODUCTO  # noqa: E402
from models import Producto, db  # noqa: E402
from test_fase3_s2 import (  # noqa: E402
    PRODUCTOS,
    STORE_ID_FALSO,
    TOKEN_FALSO,
    ApiFalsa,
    BaseSync,
)
from app import app  # noqa: E402

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


def catalogo_con(stock, **campos_variante):
    """El catalogo de siempre, con el stock de MART-500 cambiado.

    Se parte de PRODUCTOS y no de un payload nuevo para que estos tests midan
    solo el stock: todo lo demas (nombres, precios, mapeos) queda igual que en
    los tests de FASE3-S2.
    """
    productos = copy.deepcopy(PRODUCTOS)
    variante = productos[0]['variants'][0]
    variante['stock'] = stock
    variante.update(campos_variante)
    return productos


# ---------------------------------------------------------------------------
# El stock que llega a la base
# ---------------------------------------------------------------------------

class TestStock(BaseSync):

    def test_el_stock_de_cada_variante_se_guarda(self):
        self.correr()

        self.assertEqual(self.sku('MART-500').stock, 7)
        self.assertEqual(self.sku('MART-800').stock, 3)

    def test_producto_sin_control_de_stock_queda_en_null_y_no_en_cero(self):
        """La cinta viene con stock null: la tienda no lleva la cuenta.

        Guardar 0 seria afirmar "no queda ninguno", que es lo contrario de lo
        que dice el payload.
        """
        self.correr()

        cinta = self.sku('TN-222-2001')
        self.assertIsNone(cinta.stock)
        self.assertNotEqual(cinta.stock, 0)

    def test_stock_management_apagado_queda_en_null(self):
        """El otro camino al mismo NULL: el flag apagado manda sobre el numero.

        Con el control de stock desactivado la API no promete un stock util; si
        igual llega un numero, no representa una existencia real.
        """
        self.correr(api=ApiFalsa(productos=catalogo_con(0, stock_management=False)))

        self.assertIsNone(self.sku('MART-500').stock)

    def test_stock_cero_se_guarda_como_cero(self):
        """La mitad que falta: un 0 de verdad es un dato, no un faltante."""
        self.correr(api=ApiFalsa(productos=catalogo_con(0, stock_management=True)))

        martillo = self.sku('MART-500')
        self.assertEqual(martillo.stock, 0)
        self.assertIsNotNone(martillo.stock)


# ---------------------------------------------------------------------------
# El resync: el stock si se pisa, el costo no
# ---------------------------------------------------------------------------

class TestStockContraCosto(BaseSync):
    """El contraste inverso de `test_no_pisa_el_costo_que_cargo_roman`."""

    def test_el_resync_actualiza_el_stock(self):
        self.correr()
        self.assertEqual(self.sku('MART-500').stock, 7)

        self.correr(api=ApiFalsa(productos=catalogo_con(2)))

        self.assertEqual(self.sku('MART-500').stock, 2)

    def test_el_resync_pisa_un_stock_editado_a_mano(self):
        """La fuente de verdad del stock es la tienda, no la base.

        Es exactamente lo contrario de lo que pasa con el costo: ahi el valor
        cargado a mano es el unico que existe y el sync lo respeta.
        """
        self.correr()

        martillo = self.sku('MART-500')
        martillo.stock = 999
        db.session.commit()

        self.correr()

        self.assertEqual(self.sku('MART-500').stock, 7)

    def test_el_stock_vuelve_a_null_si_le_apagan_el_control(self):
        """Un stock que ya existia tiene que poder volver a NULL.

        Si el upsert se saltara los None, el producto se quedaria para siempre
        con el ultimo numero que se vio, que ya no lo respalda nadie.
        """
        self.correr()
        self.assertEqual(self.sku('MART-500').stock, 7)

        self.correr(api=ApiFalsa(productos=catalogo_con(None)))

        self.assertIsNone(self.sku('MART-500').stock)

    def test_en_la_misma_corrida_el_stock_se_pisa_y_el_costo_no(self):
        """Las dos reglas opuestas, una al lado de la otra."""
        self.correr()

        martillo = self.sku('MART-500')
        martillo.costo_unitario = Decimal('8000.00')
        db.session.commit()

        self.correr(api=ApiFalsa(productos=catalogo_con(2)))

        martillo = self.sku('MART-500')
        self.assertEqual(martillo.stock, 2)                            # se piso
        self.assertEqual(martillo.costo_unitario, Decimal('8000.00'))  # no se piso

    def test_el_resync_no_duplica_productos_al_cambiar_el_stock(self):
        """El stock nuevo viaja en la fila que ya existe, no en una nueva."""
        self.correr()
        ids = sorted(p.id for p in Producto.query.all())

        self.correr(api=ApiFalsa(productos=catalogo_con(2)))

        self.assertEqual(Producto.query.count(), 3)
        self.assertEqual(sorted(p.id for p in Producto.query.all()), ids)


# ---------------------------------------------------------------------------
# a_stock() aislado (sin base ni red)
# ---------------------------------------------------------------------------

class TestNormalizarStock(unittest.TestCase):

    def setUp(self):
        self.ingestor = ingestor_tiendanube.IngestorTiendanube(
            canal_id=1, credenciales={'access_token': TOKEN_FALSO},
            id_tienda_externo=STORE_ID_FALSO)

    def test_un_entero_pasa_derecho(self):
        a_stock = ingestor_tiendanube.a_stock
        self.assertEqual(a_stock({'stock': 12}), 12)
        self.assertEqual(a_stock({'stock': 0}), 0)
        self.assertEqual(a_stock({'stock': -3}), -3)

    def test_un_numero_como_string_tambien(self):
        """Por si la API cambia de opinion, como ya hace con los importes."""
        self.assertEqual(ingestor_tiendanube.a_stock({'stock': '12'}), 12)

    def test_los_casos_que_dan_none(self):
        a_stock = ingestor_tiendanube.a_stock
        self.assertIsNone(a_stock({'stock': None}))     # control de stock apagado
        self.assertIsNone(a_stock({}))                  # campo ausente
        self.assertIsNone(a_stock({'stock': ''}))
        self.assertIsNone(a_stock({'stock': 'infinito'}))
        # isinstance(True, int) es True: un bool no puede volverse 1 unidad.
        self.assertIsNone(a_stock({'stock': True}))

    def test_el_flag_apagado_gana_sobre_el_numero(self):
        a_stock = ingestor_tiendanube.a_stock
        self.assertIsNone(a_stock({'stock': 5, 'stock_management': False}))
        self.assertEqual(a_stock({'stock': 5, 'stock_management': True}), 5)

    def test_normalizar_expone_el_stock_en_el_dict(self):
        crudo = {'id': 111, 'name': 'Martillo',
                 'variants': [{'id': 1001, 'sku': 'MART-500', 'stock': 7,
                               'stock_management': True}]}
        par = self.ingestor.variantes_de_producto(crudo)[0]
        datos = self.ingestor.normalizar(ENTIDAD_PRODUCTO, par)
        self.assertEqual(datos['stock'], 7)

    def test_un_producto_sin_variantes_no_revienta(self):
        par = self.ingestor.variantes_de_producto({'id': 5, 'name': 'Suelto'})[0]
        datos = self.ingestor.normalizar(ENTIDAD_PRODUCTO, par)
        self.assertIsNone(datos['stock'])


if __name__ == '__main__':
    unittest.main()
