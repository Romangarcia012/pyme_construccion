# -*- coding: utf-8 -*-
"""Tests de FASE-PRODUCTOS-S2 (alta manual de producto).

    python -m unittest discover -s tests -v

Hasta esta slice `Producto` nacia en un solo lugar -- `sync_tiendanube
._upsert_producto`, durante el sync -- asi que algo que todavia no esta
publicado en Tiendanube, o que nunca va a estarlo porque se vende solo en el
mostrador, no se podia vender. La pantalla de venta manual ya prometia la
salida ("carga productos antes de registrar una venta") y era texto plano.

Lo que se prueba:

    SKU + nombre                -> alcanza; moneda/activo salen del default
    SKU repetido en la empresa  -> mensaje claro, sin 500 ni IntegrityError
    SKU repetido entre empresas -> entra: la UNIQUE es (empresa_id, sku)
    "no llevo la cuenta" vs "0" -> None y 0, dos resultados distintos
    producto manual + venta     -> aparece en el datalist sin tocar esa ruta
    producto manual + margen    -> su propio grupo, no se pega a ninguno
    el alta                     -> deja historial sin llamada manual
    el link de venta_manual     -> ya apunta a algun lado

Los dos anteultimos son los que importan mas alla de esta pantalla: prueban
que un producto sin `MapeoProductoCanal` -- el estado en el que nace todo lo
que se cargue aca -- no se cae de lo que ya existia. El del margen es el
complemento de `TestProductoSinMapeo` (FASE-REPORTES-S1), que cubre el otro
reporte.

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_tiendanube  # noqa: E402
from app import app  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    Empresa,
    Historial,
    MapeoProductoCanal,
    Pedido,
    Producto,
    Usuario,
    db,
)
from tests.ayuda_auth import request_anonimo  # noqa: E402

ENGINE_PRODUCTIVO = None

RUTA_ALTA = '/productos/nuevo'
# El blueprint de ventas cuelga de /pedidos, no de /ventas.
RUTA_VENTA = '/pedidos/manual/nuevo'


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


class BaseAlta(unittest.TestCase):
    """Una empresa con canal manual y de Tiendanube, y una segunda empresa.

    El unico producto que existe de entrada es TARJ-NEGRO, que vino del sync y
    tiene su mapeo: sirve para probar el SKU repetido contra algo que de verdad
    esta en el catalogo. La segunda empresa tiene el MISMO SKU a proposito, que
    es lo que hace verificable que la UNIQUE es por empresa.
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-PRODUCTOS-S2')
        self.otra_empresa = Empresa(nombre='Empresa Ajena')
        db.session.add_all([self.empresa, self.otra_empresa])
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='faseproductos@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial', activo=True)
        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True, id_tienda_externo='9999')
        db.session.add_all([self.canal_manual, self.canal_tn])
        db.session.flush()

        # El producto que si vino del sync, con su mapeo.
        self.del_sync = Producto(empresa_id=self.empresa.id, sku='TARJ-NEGRO',
                                 nombre='Tarjetero Minimalista de Aluminio (Negro)',
                                 costo_unitario=Decimal('3994.18'), stock=93)
        # El mismo SKU en otra empresa: no tiene que estorbar.
        self.ajeno = Producto(empresa_id=self.otra_empresa.id, sku='CINTA-19',
                              nombre='Cinta de otra empresa')
        db.session.add_all([self.del_sync, self.ajeno])
        db.session.flush()

        db.session.add(MapeoProductoCanal(
            producto_id=self.del_sync.id, canal_id=self.canal_tn.id,
            id_producto_externo='360354459', id_variante_externo='1574653133',
            sku_externo='TARJ-NEGRO'))

        db.session.commit()

        self.empresa_id = self.empresa.id
        self.otra_empresa_id = self.otra_empresa.id
        self.usuario_id = self.usuario.id
        self.canal_manual_id = self.canal_manual.id

        # La venta manual empuja stock a Tiendanube. Se anula para que ningun
        # test salga a internet ni dependa del cifrado de credenciales.
        self.original_push = stock_tiendanube.empujar_stock
        stock_tiendanube.empujar_stock = lambda empresa_id, producto_ids: []

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        stock_tiendanube.empujar_stock = self.original_push
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def alta(self, **campos):
        """POST al formulario de alta. Solo se mandan los campos que se pasan,
        igual que un navegador: un radio no elegido no viaja."""
        return self.client.post(RUTA_ALTA, data=campos, follow_redirects=True)

    def producto(self, sku, empresa_id=None):
        return Producto.query.filter_by(
            empresa_id=empresa_id or self.empresa_id, sku=sku).first()

    def texto(self, respuesta):
        return respuesta.get_data(as_text=True)

    def cuantos(self, empresa_id=None):
        return Producto.query.filter_by(
            empresa_id=empresa_id or self.empresa_id).count()


class TestAltaMinima(BaseAlta):
    """SKU + nombre alcanza. El resto queda en su default o en NULL."""

    def setUp(self):
        super().setUp()
        self.respuesta = self.alta(sku='CINTA-19', nombre='Cinta aisladora 19mm')

    def test_alta_manual_producto_minimo(self):
        producto = self.producto('CINTA-19')
        self.assertIsNotNone(producto, 'el producto tiene que existir')
        self.assertEqual(producto.nombre, 'Cinta aisladora 19mm')
        self.assertEqual(producto.empresa_id, self.empresa_id)

    def test_los_defaults_del_modelo_se_aplican_solos(self):
        producto = self.producto('CINTA-19')
        self.assertEqual(producto.moneda, 'ARS')
        self.assertTrue(producto.activo, 'un producto se carga para venderlo')

    def test_lo_que_no_se_pidio_queda_en_null(self):
        producto = self.producto('CINTA-19')
        self.assertIsNone(producto.costo_unitario)
        self.assertIsNone(producto.precio_lista, 'precio_lista no se pide')
        # Sin elegir la opcion de stock, NULL: "nadie lleva la cuenta". No 0.
        self.assertIsNone(producto.stock)

    def test_el_post_termina_bien_y_avisa(self):
        self.assertEqual(self.respuesta.status_code, 200)
        self.assertIn('Cinta aisladora 19mm', self.texto(self.respuesta))

    def test_sin_costo_el_aviso_lo_dice(self):
        """El costo es opcional, pero su ausencia no puede pasar callada: la
        venta congela NULL y esa venta se queda sin ganancia para siempre."""
        self.assertIn('sin costo', self.texto(self.respuesta))

    def test_no_se_creo_ningun_mapeo_de_canal(self):
        """Un producto cargado a mano no existe en ningun canal externo."""
        producto = self.producto('CINTA-19')
        self.assertEqual(
            MapeoProductoCanal.query.filter_by(producto_id=producto.id).count(), 0)

    def test_termina_en_el_listado_de_stock(self):
        """Ahi se ve entre los demas y ahi se carga el costo si quedo pendiente."""
        respuesta = self.client.post(
            RUTA_ALTA, data={'sku': 'OTRO-1', 'nombre': 'Otro'})
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/productos/listar', respuesta.headers['Location'])


class TestAltaCompleta(BaseAlta):
    """Los cuatro campos cargados, que es el camino que se espera."""

    def setUp(self):
        super().setUp()
        self.alta(sku='MART-500', nombre='Martillo 500g',
                  costo_unitario='1234.56', control_stock='si', stock='12')

    def test_guarda_los_cuatro_campos(self):
        producto = self.producto('MART-500')
        self.assertEqual(producto.nombre, 'Martillo 500g')
        self.assertEqual(producto.costo_unitario, Decimal('1234.56'))
        self.assertEqual(producto.stock, 12)

    def test_el_costo_es_decimal_y_no_float(self):
        self.assertIsInstance(self.producto('MART-500').costo_unitario, Decimal)

    def test_la_coma_decimal_se_acepta(self):
        """Mismo criterio que el formulario de costos y que la venta manual:
        es lo que tipea cualquiera aca."""
        self.alta(sku='DEST-PH2', nombre='Destornillador PH2',
                  costo_unitario='890,50')
        self.assertEqual(self.producto('DEST-PH2').costo_unitario,
                         Decimal('890.50'))


class TestSkuDuplicado(BaseAlta):
    """El SKU es la identidad del producto y es unico por empresa."""

    def setUp(self):
        super().setUp()
        self.antes = self.cuantos()
        self.respuesta = self.alta(sku='TARJ-NEGRO', nombre='Otra cosa distinta')

    def test_alta_rechaza_sku_duplicado_de_la_empresa(self):
        self.assertEqual(self.cuantos(), self.antes,
                         'no se creo ningun producto')

    def test_no_es_un_500_ni_revienta_con_integrityerror(self):
        """Se verifica ANTES de insertar, justamente para poder contestar."""
        self.assertEqual(self.respuesta.status_code, 200)

    def test_el_mensaje_es_claro_y_nombra_el_codigo(self):
        texto = self.texto(self.respuesta)
        self.assertIn('Ya existe un producto con el codigo', texto)
        self.assertIn('TARJ-NEGRO', texto)

    def test_no_le_toco_nada_al_producto_que_ya_estaba(self):
        producto = self.producto('TARJ-NEGRO')
        self.assertEqual(producto.nombre,
                         'Tarjetero Minimalista de Aluminio (Negro)')
        self.assertEqual(producto.costo_unitario, Decimal('3994.18'))
        self.assertEqual(producto.stock, 93)

    def test_lo_tipeado_vuelve_al_formulario(self):
        """Un rechazo no puede obligar a tipear todo de nuevo."""
        self.assertIn('Otra cosa distinta', self.texto(self.respuesta))


class TestSkuEntreEmpresas(BaseAlta):
    """La UNIQUE es (empresa_id, sku): dos empresas pueden compartir un SKU."""

    def test_sku_duplicado_entre_empresas_distintas_no_choca(self):
        # CINTA-19 ya existe, pero en la OTRA empresa.
        self.assertIsNotNone(self.producto('CINTA-19', self.otra_empresa_id))

        self.alta(sku='CINTA-19', nombre='Cinta aisladora 19mm')

        propio = self.producto('CINTA-19')
        self.assertIsNotNone(propio, 'el SKU ajeno no puede bloquear el alta')
        self.assertEqual(propio.empresa_id, self.empresa_id)

    def test_el_producto_de_la_otra_empresa_queda_intacto(self):
        self.alta(sku='CINTA-19', nombre='Cinta aisladora 19mm')

        ajeno = self.producto('CINTA-19', self.otra_empresa_id)
        self.assertEqual(ajeno.nombre, 'Cinta de otra empresa')
        self.assertEqual(self.cuantos(self.otra_empresa_id), 1,
                         'no se le agrego nada a la otra empresa')


class TestStockNoneVsCero(BaseAlta):
    """None y 0 son estados distintos en todo el sistema, y el form los separa.

    None -> nadie lleva la cuenta: la venta no descuenta, el listado dice "sin
            control de stock" y el total del grupo tampoco lo cuenta.
    0    -> se lleva la cuenta y no queda ninguno: el listado lo marca en rojo.
    """

    def test_stock_none_vs_cero_segun_eleccion(self):
        self.alta(sku='SIN-CUENTA', nombre='Arena a granel', control_stock='no')
        self.alta(sku='EN-CERO', nombre='Lija grano 120',
                  control_stock='si', stock='0')

        sin_cuenta = self.producto('SIN-CUENTA').stock
        en_cero = self.producto('EN-CERO').stock

        self.assertIsNone(sin_cuenta, '"no llevo la cuenta" es NULL, no 0')
        self.assertEqual(en_cero, 0, '"llevo la cuenta: 0" es 0, no NULL')
        self.assertIsNot(sin_cuenta, en_cero,
                         'los dos resultados tienen que ser distinguibles')

    def test_el_cero_no_se_confunde_con_el_vacio_en_el_listado(self):
        """La distincion no se queda en la base: la pantalla ya la muestra."""
        self.alta(sku='SIN-CUENTA', nombre='Arena a granel', control_stock='no')
        self.alta(sku='EN-CERO', nombre='Lija grano 120',
                  control_stock='si', stock='0')

        texto = self.client.get('/productos/listar').get_data(as_text=True)
        self.assertIn('sin control de stock', texto)
        self.assertIn('agotado', texto, 'el 0 va marcado en rojo')

    def test_llevo_la_cuenta_sin_numero_no_inventa_un_cero(self):
        antes = self.cuantos()
        respuesta = self.alta(sku='INCOMPLETO', nombre='Algo',
                              control_stock='si', stock='')
        self.assertEqual(self.cuantos(), antes)
        self.assertIn('no pusiste cuantos', self.texto(respuesta))

    def test_stock_negativo_se_rechaza(self):
        antes = self.cuantos()
        respuesta = self.alta(sku='NEG', nombre='Algo',
                              control_stock='si', stock='-3')
        self.assertEqual(self.cuantos(), antes)
        self.assertIn('no puede ser negativo', self.texto(respuesta))

    def test_stock_con_decimales_se_rechaza(self):
        antes = self.cuantos()
        respuesta = self.alta(sku='FRAC', nombre='Algo',
                              control_stock='si', stock='2.5')
        self.assertEqual(self.cuantos(), antes)
        self.assertIn('numero entero', self.texto(respuesta))


class TestCamposObligatorios(BaseAlta):
    """Los dos NOT NULL que tipea el usuario, y los largos de las columnas."""

    def test_sin_sku_no_se_crea_nada(self):
        antes = self.cuantos()
        respuesta = self.alta(nombre='Sin codigo')
        self.assertEqual(self.cuantos(), antes)
        self.assertIn('Falta el codigo', self.texto(respuesta))

    def test_sin_nombre_no_se_crea_nada(self):
        antes = self.cuantos()
        respuesta = self.alta(sku='SOLO-SKU')
        self.assertEqual(self.cuantos(), antes)
        self.assertIn('Falta el nombre', self.texto(respuesta))

    def test_el_sku_en_blanco_no_cuenta_como_sku(self):
        antes = self.cuantos()
        self.alta(sku='   ', nombre='Algo')
        self.assertEqual(self.cuantos(), antes)

    def test_el_sku_se_guarda_sin_espacios_de_los_costados(self):
        self.alta(sku='  CINTA-19  ', nombre='  Cinta aisladora  ')
        producto = self.producto('CINTA-19')
        self.assertIsNotNone(producto)
        self.assertEqual(producto.nombre, 'Cinta aisladora')

    def test_un_sku_mas_largo_que_la_columna_se_rechaza_aca(self):
        """SQLite no hace cumplir el largo del VARCHAR y Postgres si: sin esta
        validacion el test pasaria y Supabase reventaria en el commit."""
        antes = self.cuantos()
        respuesta = self.alta(sku='X' * 61, nombre='Algo')
        self.assertEqual(self.cuantos(), antes)
        self.assertIn('mas de 60 caracteres', self.texto(respuesta))

    def test_un_nombre_mas_largo_que_la_columna_se_rechaza_aca(self):
        antes = self.cuantos()
        respuesta = self.alta(sku='LARGO', nombre='X' * 201)
        self.assertEqual(self.cuantos(), antes)
        self.assertIn('mas de 200 caracteres', self.texto(respuesta))

    def test_costo_invalido_no_crea_el_producto(self):
        antes = self.cuantos()
        respuesta = self.alta(sku='MALO', nombre='Algo', costo_unitario='abc')
        self.assertEqual(self.cuantos(), antes)
        self.assertIn('no es un numero', self.texto(respuesta))

    def test_costo_negativo_no_crea_el_producto(self):
        antes = self.cuantos()
        respuesta = self.alta(sku='MALO', nombre='Algo', costo_unitario='-1')
        self.assertEqual(self.cuantos(), antes)
        self.assertIn('no puede ser negativo', self.texto(respuesta))


class TestProductoManualEnVentaManual(BaseAlta):
    """El circulo que cierra la slice: lo cargado a mano se puede vender.

    `nueva_venta_manual` no se toco. Elige de `_productos_de_la_empresa()`, que
    filtra por empresa y activo y nunca miro el mapeo de canal.
    """

    def setUp(self):
        super().setUp()
        self.alta(sku='CINTA-19', nombre='Cinta aisladora 19mm',
                  costo_unitario='300.00', control_stock='si', stock='5')

    def test_producto_manual_aparece_en_venta_manual(self):
        texto = self.client.get(RUTA_VENTA).get_data(as_text=True)
        self.assertIn('CINTA-19', texto)
        self.assertIn('Cinta aisladora 19mm', texto)

    def test_se_puede_vender_sin_tocar_la_ruta_de_ventas(self):
        respuesta = self.client.post(RUTA_VENTA, data={
            'fecha': '2026-09-01',
            'medio': 'efectivo',
            'sku': ['CINTA-19'],
            'cantidad': ['2'],
            'precio_unitario': ['500.00'],
        }, follow_redirects=True)
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self.producto('CINTA-19').stock, 3,
                         'la venta descuenta el stock cargado a mano')

    def test_la_venta_congela_el_costo_que_se_cargo_en_el_alta(self):
        """Es la razon por la que el costo se pide en el alta y no despues."""
        from models import PedidoItem

        self.client.post(RUTA_VENTA, data={
            'fecha': '2026-09-01', 'medio': 'efectivo',
            'sku': ['CINTA-19'], 'cantidad': ['1'],
            'precio_unitario': ['500.00'],
        }, follow_redirects=True)

        item = PedidoItem.query.filter_by(
            producto_id=self.producto('CINTA-19').id).first()
        self.assertIsNotNone(item)
        self.assertEqual(item.costo_unitario_snapshot, Decimal('300.00'))


class TestProductoManualEnReporteDeMargen(BaseAlta):
    """Un producto sin mapeo cae en su propio grupo del reporte de margen.

    Es el complemento de `TestProductoSinMapeo` (FASE-REPORTES-S1), que cubre
    el otro reporte. Los dos agrupan por `_clave_de_grupo`, que sin mapeo
    devuelve ('producto', id) -- por eso dos productos sin mapeo tampoco se
    pegan entre si por compartir un id externo vacio.
    """

    def setUp(self):
        super().setUp()
        self.alta(sku='CINTA-19', nombre='Cinta aisladora 19mm',
                  costo_unitario='300.00', control_stock='si', stock='5')
        self.alta(sku='LIJA-120', nombre='Lija grano 120',
                  costo_unitario='100.00', control_stock='si', stock='9')

        for sku, precio in (('CINTA-19', '500.00'), ('LIJA-120', '250.00')):
            self.client.post(RUTA_VENTA, data={
                'fecha': '2026-09-01', 'medio': 'efectivo',
                'sku': [sku], 'cantidad': ['1'], 'precio_unitario': [precio],
            }, follow_redirects=True)

        # La comision en cero, cargada a mano. NO es parte de esta slice y por
        # eso se escribe directo en vez de por su pantalla: el reporte de
        # margen exige `comision_plataforma` no NULL para calcular, y la venta
        # de mostrador la deja NULL (la carga Roman desde Ver Ventas mirando la
        # liquidacion del canal). Sin esto los dos pedidos caen en "falta
        # cargar datos" y el test no llegaria a mirar lo que vino a mirar, que
        # es el AGRUPAMIENTO de un producto sin mapeo.
        for pedido in Pedido.query.filter_by(empresa_id=self.empresa_id).all():
            pedido.comision_plataforma = Decimal('0.00')
        db.session.commit()

        self.texto_reporte = self.client.get(
            '/reportes/margen').get_data(as_text=True)

    def test_producto_manual_sin_mapeo_en_reporte_margen(self):
        self.assertIn('Cinta aisladora 19mm', self.texto_reporte)

    def test_no_rompe_el_reporte(self):
        self.assertEqual(self.client.get('/reportes/margen').status_code, 200)

    def test_no_se_pego_a_ningun_otro_grupo(self):
        """Sin mapeo la clave es el propio id: los dos productos manuales
        tienen que ser dos filas, no una sola con el id externo vacio."""
        self.assertIn('Lija grano 120', self.texto_reporte)
        self.assertNotIn('Sin identificar', self.texto_reporte)

    def test_la_ganancia_se_calcula_igual_que_para_uno_del_sync(self):
        """El mapeo nunca participo de la cuenta: el costo sale del snapshot."""
        # 500 cobrados - 300 de costo = 200 de ganancia, sin comision ni flete.
        # Se busca dentro de una celda y no suelto en el HTML: '200' a secas
        # matchearia con cualquier '200' de los estilos de la pagina.
        self.assertIn('>200.00<', self.texto_reporte.replace('\n', '')
                      .replace(' ', ''))


class TestAuditoriaDelAlta(BaseAlta):
    """El hook de `before_flush` registra el alta sin llamada manual.

    `Producto` ya estaba en TABLAS_AUDITADAS desde FASE-AUDITORIA-S2, asi que
    la ruta nueva no toca `registrar_cambio()` -- una llamada ahi duplicaria la
    fila. Esto prueba que el hook lo cubre solo.
    """

    def historial(self):
        return (Historial.query
                .filter_by(empresa_id=self.empresa_id, tipo='producto')
                .all())

    def test_auditoria_registra_el_alta_manual(self):
        self.alta(sku='CINTA-19', nombre='Cinta aisladora 19mm')

        filas = self.historial()
        self.assertEqual(len(filas), 1, 'una sola fila, no una por campo')
        fila = filas[0]
        self.assertEqual(fila.accion, 'crear')
        self.assertEqual(fila.usuario_id, self.usuario_id)
        self.assertEqual(fila.empresa_id, self.empresa_id)
        self.assertIn('CINTA-19', fila.descripcion,
                      'el SKU tiene que ser legible en la pantalla de historial')
        self.assertIn('creado', fila.descripcion)

    def test_un_alta_rechazada_no_deja_historial(self):
        """La fila viaja en el mismo commit: si hay rollback, se va con el."""
        self.alta(sku='TARJ-NEGRO', nombre='Repetido')
        self.assertEqual(self.historial(), [])

    def test_el_alta_aparece_en_la_pantalla_de_historial(self):
        self.alta(sku='CINTA-19', nombre='Cinta aisladora 19mm')
        texto = self.client.get('/historial').get_data(as_text=True)
        self.assertIn('CINTA-19', texto)


class TestLinksConectados(BaseAlta):
    """La pantalla nueva tiene que ser alcanzable desde donde se la promete."""

    def test_link_de_venta_manual_ya_no_esta_roto(self):
        """venta_manual.html decia "carga productos" en texto plano. El estado
        vacio es el unico momento en que ese texto se muestra, asi que hay que
        vaciar el catalogo para verlo."""
        Producto.query.filter_by(empresa_id=self.empresa_id).delete()
        db.session.commit()

        texto = self.client.get(RUTA_VENTA).get_data(as_text=True)
        self.assertIn('Todavía no hay productos', texto)
        self.assertIn('href="%s"' % RUTA_ALTA, texto,
                      'la mencion tiene que ser un link de verdad')

    def test_el_listado_de_stock_ofrece_el_alta(self):
        texto = self.client.get('/productos/listar').get_data(as_text=True)
        self.assertIn('href="%s"' % RUTA_ALTA, texto)

    def test_el_sidebar_tiene_la_entrada(self):
        texto = self.client.get('/dashboard').get_data(as_text=True)
        self.assertIn('href="%s"' % RUTA_ALTA, texto)

    def test_los_links_apuntan_a_una_ruta_que_responde(self):
        """Un link que existe pero da 404 estaria igual de roto."""
        self.assertEqual(self.client.get(RUTA_ALTA).status_code, 200)


class TestAuth(BaseAlta):
    """Las dos puntas de la ruta piden sesion."""

    def test_el_formulario_pide_login(self):
        respuesta = request_anonimo(self.ctx, 'get', RUTA_ALTA)
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn('/login', respuesta.headers['Location'])

    def test_el_alta_pide_login(self):
        antes = self.cuantos()
        respuesta = request_anonimo(self.ctx, 'post', RUTA_ALTA,
                                    data={'sku': 'COLADO', 'nombre': 'Colado'})
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(self.cuantos(), antes,
                         'un anonimo no puede escribir en el catalogo')


if __name__ == '__main__':
    unittest.main()
