# -*- coding: utf-8 -*-
"""Tests de FASE-REPORTES-S1 (vendido por canal y color).

    python -m unittest discover -s tests -v

La pantalla reemplaza una hoja de Excel, asi que lo que hay que probar no es
que "se vea": es que los numeros cierren. Un reporte que agrupa mal o que
pierde una linea en el camino es peor que no tener reporte, porque se le cree.

    dos colores del mismo producto   -> un solo grupo, filas y columnas cierran
    un pedido cancelado              -> no cuenta como vendido
    un item sin producto             -> aparece en "Sin identificar", no se pierde
    un canal que no vendio nada      -> aparece igual, en cero
    productos de otra empresa        -> no se filtran a este reporte

Como en las slices anteriores, la app se repunta a SQLite en memoria: la base
productiva no se toca.
"""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rutas_productos  # noqa: E402
from models import (  # noqa: E402
    CanalVenta,
    Empresa,
    MapeoProductoCanal,
    Pedido,
    PedidoItem,
    Producto,
    Usuario,
    db,
)
from app import app  # noqa: E402
from tests.ayuda_auth import request_anonimo  # noqa: E402

ENGINE_PRODUCTIVO = None

# El valor que manda Tiendanube cuando el comprador o la tienda dan de baja el
# pedido: su API devuelve el status crudo ('open' | 'closed' | 'cancelled') y
# el ingestor lo guarda tal cual, sin traducirlo.
ESTADO_CANCELADO = 'cancelled'


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


class BaseReporte(unittest.TestCase):
    """El caso del Excel de Roman, reproducido en la base.

    Un producto con dos variantes de color (mismo id de producto padre en
    Tiendanube, dos filas de `producto`), un producto sin variantes, y tres
    canales de los cuales uno -- Mercado Libre -- nunca vendio nada.

        Tarjetero (Negro)  stock 170   vendido: 3 TN + 12 presencial = 15
        Tarjetero (Gris)   stock  93   vendido: 0 TN +  5 presencial =  5
        Micrófono          stock 100   vendido: 2 TN                 =  2
    """

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

        self.empresa = Empresa(nombre='Empresa Test FASE-REPORTES-S1')
        db.session.add(self.empresa)
        db.session.flush()

        self.usuario = Usuario(nombre='Roman Test', email='fasereportes@test.local',
                               empresa_id=self.empresa.id, rol='admin', verificado=True)
        self.usuario.set_password('irrelevante')
        db.session.add(self.usuario)

        self.canal_tn = CanalVenta(empresa_id=self.empresa.id, tipo='tiendanube',
                                   nombre='Korvo', activo=True, id_tienda_externo='9999')
        # Existe pero no esta conectado: es justamente el canal que tiene que
        # aparecer en la tabla con todo en cero.
        self.canal_meli = CanalVenta(empresa_id=self.empresa.id, tipo='mercadolibre',
                                     nombre='Mercado Libre', activo=False)
        self.canal_manual = CanalVenta(empresa_id=self.empresa.id, tipo='manual',
                                       nombre='Venta manual / presencial', activo=True)
        db.session.add_all([self.canal_tn, self.canal_meli, self.canal_manual])
        db.session.flush()

        self.negro = Producto(empresa_id=self.empresa.id, sku='TN-360354459-1574653133',
                              nombre='Tarjetero Minimalista de Aluminio (Negro)',
                              stock=170, precio_lista=Decimal('12000.00'))
        self.gris = Producto(empresa_id=self.empresa.id, sku='TN-360354459-1574653135',
                             nombre='Tarjetero Minimalista de Aluminio (Gris)',
                             stock=93, precio_lista=Decimal('12000.00'))
        self.micro = Producto(empresa_id=self.empresa.id, sku='TN-363274370-1584388030',
                              nombre='Micrófono Inalámbrico Korvo',
                              stock=100, precio_lista=Decimal('45000.00'))
        db.session.add_all([self.negro, self.gris, self.micro])
        db.session.flush()

        # Las dos variantes comparten el id del producto PADRE (360354459) y
        # difieren en el de la variante. Eso es lo unico que las junta: los
        # nombres son distintos a proposito.
        for producto, id_padre, id_variante in (
            (self.negro, '360354459', '1574653133'),
            (self.gris, '360354459', '1574653135'),
            (self.micro, '363274370', '1584388030'),
        ):
            db.session.add(MapeoProductoCanal(
                producto_id=producto.id, canal_id=self.canal_tn.id,
                id_producto_externo=id_padre, id_variante_externo=id_variante,
                sku_externo=producto.sku))

        db.session.commit()

        self.empresa_id = self.empresa.id
        self.usuario_id = self.usuario.id
        self.canal_tn_id = self.canal_tn.id
        self.canal_meli_id = self.canal_meli.id
        self.canal_manual_id = self.canal_manual.id

        self.client = app.test_client()
        with self.client.session_transaction() as sesion:
            sesion['_user_id'] = str(self.usuario_id)
            sesion['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # -- helpers --------------------------------------------------------

    def registrar_pedido(self, canal_id, items, estado='closed', empresa_id=None,
                         id_externo=None):
        """Un pedido con sus lineas. `items`: (producto|None, descripcion, cantidad)."""
        pedido = Pedido(empresa_id=empresa_id or self.empresa_id, canal_id=canal_id,
                        id_externo=id_externo, fecha_pedido=datetime(2026, 8, 1),
                        estado=estado, total=Decimal('0.00'))
        db.session.add(pedido)
        db.session.flush()

        for producto, descripcion, cantidad in items:
            db.session.add(PedidoItem(
                pedido_id=pedido.id,
                producto_id=producto.id if producto is not None else None,
                descripcion=descripcion, cantidad=cantidad,
                precio_unitario=Decimal('12000.00'),
                subtotal=Decimal('12000.00') * cantidad))

        db.session.commit()
        return pedido

    def ventas_del_excel(self):
        """Las ventas que reproducen la hoja INVENTARIO."""
        self.registrar_pedido(self.canal_tn_id, [(self.negro, 'Tarjetero Negro', 3)],
                              id_externo='1001')
        self.registrar_pedido(self.canal_manual_id, [
            (self.negro, 'Tarjetero Negro', 12),
            (self.gris, 'Tarjetero Gris', 5),
        ])
        self.registrar_pedido(self.canal_tn_id, [(self.micro, 'Micrófono', 2)],
                              id_externo='1002')

    def reporte(self):
        """Los datos que la ruta le pasa a la plantilla, sin pasar por el HTML.

        Afirmar sobre el contexto y no sobre el markup es lo que hace que
        estos tests hablen de los numeros -- que es lo que importa -- y no se
        rompan el dia que alguien cambie una clase de CSS.
        """
        from flask import template_rendered

        capturado = {}

        def anotar(remitente, template, context, **extra):
            capturado['context'] = context

        template_rendered.connect(anotar, app)
        try:
            respuesta = self.client.get('/productos/resumen')
        finally:
            template_rendered.disconnect(anotar, app)

        return respuesta, capturado.get('context', {})

    def grupo_llamado(self, contexto, nombre):
        for grupo in contexto['grupos']:
            if grupo['nombre'] == nombre:
                return grupo
        self.fail('no hay ningún grupo llamado %r; hay %r'
                  % (nombre, [g['nombre'] for g in contexto['grupos']]))

    def columna(self, contexto, nombre):
        """Indice de una columna de canal, para leer las listas por_canal."""
        return contexto['columnas'].index(nombre)


class TestAgrupadoYTotales(BaseReporte):
    """El caso central: dos colores, dos canales, y que la aritmética cierre."""

    def setUp(self):
        super().setUp()
        self.ventas_del_excel()
        self.respuesta, self.contexto = self.reporte()

    def test_responde_ok(self):
        self.assertEqual(self.respuesta.status_code, 200)

    def test_las_dos_variantes_caen_bajo_un_solo_encabezado(self):
        # Y el encabezado es el nombre base, sin el sufijo de color de ninguna
        # de las dos.
        grupo = self.grupo_llamado(self.contexto, 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(len(grupo['filas']), 2)
        self.assertEqual(sorted(fila['variante'] for fila in grupo['filas']),
                         ['Gris', 'Negro'])

    def test_el_producto_sin_variantes_es_su_propio_grupo(self):
        grupo = self.grupo_llamado(self.contexto, 'Micrófono Inalámbrico Korvo')
        self.assertEqual(len(grupo['filas']), 1)
        # Sin sufijo de color no hay etiqueta que poner: se repite el nombre
        # antes que dejar la celda en blanco.
        self.assertEqual(grupo['filas'][0]['variante'], 'Micrófono Inalámbrico Korvo')

    def test_cada_variante_reparte_sus_ventas_por_canal(self):
        grupo = self.grupo_llamado(self.contexto, 'Tarjetero Minimalista de Aluminio')
        filas = {fila['variante']: fila for fila in grupo['filas']}
        tn = self.columna(self.contexto, 'Korvo')
        manual = self.columna(self.contexto, 'Venta manual / presencial')

        self.assertEqual(filas['Negro']['por_canal'][tn], 3)
        self.assertEqual(filas['Negro']['por_canal'][manual], 12)
        self.assertEqual(filas['Gris']['por_canal'][tn], 0)
        self.assertEqual(filas['Gris']['por_canal'][manual], 5)

    def test_el_vendido_de_cada_fila_es_la_suma_de_sus_canales(self):
        for grupo in self.contexto['grupos']:
            for fila in grupo['filas']:
                self.assertEqual(fila['vendido'], sum(fila['por_canal']),
                                 'la fila %r no cierra' % fila['variante'])

    def test_la_fila_total_es_la_suma_de_las_variantes(self):
        for grupo in self.contexto['grupos']:
            for indice in range(len(self.contexto['columnas'])):
                esperado = sum(fila['por_canal'][indice] for fila in grupo['filas'])
                self.assertEqual(grupo['total']['por_canal'][indice], esperado,
                                 'la columna %d de %r no cierra'
                                 % (indice, grupo['nombre']))
            self.assertEqual(grupo['total']['vendido'],
                             sum(fila['vendido'] for fila in grupo['filas']))
            self.assertEqual(grupo['total']['vendido'],
                             sum(grupo['total']['por_canal']))

    def test_los_numeros_del_excel(self):
        # Los mismos 15 + 5 = 20 que Roman suma a mano.
        grupo = self.grupo_llamado(self.contexto, 'Tarjetero Minimalista de Aluminio')
        filas = {fila['variante']: fila for fila in grupo['filas']}
        self.assertEqual(filas['Negro']['vendido'], 15)
        self.assertEqual(filas['Gris']['vendido'], 5)
        self.assertEqual(grupo['total']['vendido'], 20)

    def test_el_stock_es_el_que_tiene_el_producto_y_el_total_los_suma(self):
        grupo = self.grupo_llamado(self.contexto, 'Tarjetero Minimalista de Aluminio')
        filas = {fila['variante']: fila for fila in grupo['filas']}
        self.assertEqual(filas['Negro']['stock'], 170)
        self.assertEqual(filas['Gris']['stock'], 93)
        self.assertEqual(grupo['total']['stock'], 263)

    def test_el_reporte_no_toca_el_stock(self):
        # Es de solo lectura: mirarlo no puede mover un numero.
        self.assertEqual(db.session.get(Producto, self.negro.id).stock, 170)
        self.assertEqual(db.session.get(Producto, self.gris.id).stock, 93)

    def test_los_nombres_llegan_al_html(self):
        html = self.respuesta.get_data(as_text=True)
        self.assertIn('Tarjetero Minimalista de Aluminio', html)
        self.assertIn('Negro', html)
        self.assertIn('Gris', html)


class TestPedidoCancelado(BaseReporte):
    """Un pedido cancelado salió del sistema: no se vendió nada."""

    def setUp(self):
        super().setUp()
        self.registrar_pedido(self.canal_tn_id, [(self.negro, 'Tarjetero Negro', 3)],
                              id_externo='2001')
        self.registrar_pedido(self.canal_tn_id, [(self.negro, 'Tarjetero Negro', 40)],
                              estado=ESTADO_CANCELADO, id_externo='2002')
        _, self.contexto = self.reporte()

    def test_las_40_unidades_canceladas_no_cuentan(self):
        grupo = self.grupo_llamado(self.contexto, 'Tarjetero Minimalista de Aluminio')
        negro = [f for f in grupo['filas'] if f['variante'] == 'Negro'][0]
        self.assertEqual(negro['vendido'], 3)

    def test_tampoco_entran_al_total_del_grupo(self):
        grupo = self.grupo_llamado(self.contexto, 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(grupo['total']['vendido'], 3)

    def test_el_estado_se_compara_sin_importar_mayusculas(self):
        # Un canal futuro que mande 'CANCELLED' no tiene que colar la venta.
        pedido = Pedido.query.filter_by(id_externo='2002').one()
        pedido.estado = ESTADO_CANCELADO.upper()
        db.session.commit()

        _, contexto = self.reporte()
        grupo = self.grupo_llamado(contexto, 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(grupo['total']['vendido'], 3)


class TestItemSinProducto(BaseReporte):
    """producto_id NULL: la línea existe, la venta pasó, el producto no se sabe."""

    def setUp(self):
        super().setUp()
        self.registrar_pedido(self.canal_tn_id, [(self.negro, 'Tarjetero Negro', 3)],
                              id_externo='3001')
        self.registrar_pedido(self.canal_manual_id, [
            (None, 'Algo que el catálogo no reconoce', 7),
        ])
        self.respuesta, self.contexto = self.reporte()

    def test_la_pagina_no_se_rompe(self):
        self.assertEqual(self.respuesta.status_code, 200)

    def test_aparece_el_renglon_con_su_cantidad(self):
        sin_identificar = self.contexto['sin_identificar']
        self.assertIsNotNone(sin_identificar)
        self.assertEqual(sin_identificar['vendido'], 7)

    def test_se_le_atribuye_al_canal_correcto(self):
        sin_identificar = self.contexto['sin_identificar']
        manual = self.columna(self.contexto, 'Venta manual / presencial')
        tn = self.columna(self.contexto, 'Korvo')
        self.assertEqual(sin_identificar['por_canal'][manual], 7)
        self.assertEqual(sin_identificar['por_canal'][tn], 0)

    def test_no_se_mezcla_con_ningun_producto(self):
        vendido_en_grupos = sum(grupo['total']['vendido']
                                for grupo in self.contexto['grupos'])
        self.assertEqual(vendido_en_grupos, 3)

    def test_la_cuenta_general_sigue_cerrando(self):
        # Las 10 unidades que salieron son 3 identificadas + 7 sin identificar.
        # Si el renglon de abajo se cayera, el reporte diria 3 y seria mentira.
        total = (sum(grupo['total']['vendido'] for grupo in self.contexto['grupos'])
                 + self.contexto['sin_identificar']['vendido'])
        self.assertEqual(total, 10)

    def test_el_renglon_se_ve_en_la_pagina(self):
        self.assertIn('Sin identificar', self.respuesta.get_data(as_text=True))


class TestSinItemsHuerfanos(BaseReporte):
    """Sin líneas sueltas no hay tabla de sobra: la sección no se dibuja."""

    def setUp(self):
        super().setUp()
        self.ventas_del_excel()
        self.respuesta, self.contexto = self.reporte()

    def test_no_hay_seccion_sin_identificar(self):
        self.assertIsNone(self.contexto['sin_identificar'])
        self.assertNotIn('Sin identificar', self.respuesta.get_data(as_text=True))


class TestCanalSinVentas(BaseReporte):
    """Mercado Libre existe y no vendió nada. Cero es un dato, no un vacío."""

    def setUp(self):
        super().setUp()
        self.ventas_del_excel()
        self.respuesta, self.contexto = self.reporte()

    def test_la_columna_existe_igual(self):
        self.assertIn('Mercado Libre', self.contexto['columnas'])

    def test_todas_sus_celdas_estan_en_cero(self):
        meli = self.columna(self.contexto, 'Mercado Libre')
        for grupo in self.contexto['grupos']:
            for fila in grupo['filas']:
                self.assertEqual(fila['por_canal'][meli], 0)
            self.assertEqual(grupo['total']['por_canal'][meli], 0)

    def test_estan_los_tres_canales_y_ninguno_de_mas(self):
        self.assertEqual(sorted(self.contexto['columnas']),
                         ['Korvo', 'Mercado Libre', 'Venta manual / presencial'])

    def test_el_canal_apagado_se_ve_en_la_pagina(self):
        self.assertIn('Mercado Libre', self.respuesta.get_data(as_text=True))


class TestAislamientoYAcceso(BaseReporte):
    """Lo que no es de esta empresa no entra, y sin sesión no se entra."""

    def test_pide_login(self):
        # request_anonimo da de baja el app_context del test mientras dura el
        # request: sin eso el usuario cacheado en `g` haria pasar este test
        # aunque a la ruta le faltara el @login_required.
        respuesta = request_anonimo(self.ctx, 'get', '/productos/resumen')

        self.assertIn(respuesta.status_code, (301, 302))
        self.assertIn('/login', respuesta.headers.get('Location', ''))

    def test_no_muestra_productos_ni_canales_de_otra_empresa(self):
        otra = Empresa(nombre='Ferretería Ajena')
        db.session.add(otra)
        db.session.flush()

        canal_ajeno = CanalVenta(empresa_id=otra.id, tipo='tiendanube',
                                 nombre='Canal Ajeno', activo=True)
        db.session.add(canal_ajeno)
        db.session.flush()

        producto_ajeno = Producto(empresa_id=otra.id, sku='AJENO-1',
                                  nombre='Producto de otra empresa', stock=99)
        db.session.add(producto_ajeno)
        db.session.flush()

        self.registrar_pedido(canal_ajeno.id,
                              [(producto_ajeno, 'Producto ajeno', 50)],
                              empresa_id=otra.id, id_externo='9001')

        respuesta, contexto = self.reporte()
        html = respuesta.get_data(as_text=True)

        self.assertNotIn('AJENO-1', html)
        self.assertNotIn('Canal Ajeno', contexto['columnas'])
        self.assertEqual(sum(grupo['total']['vendido'] for grupo in contexto['grupos']), 0)


class TestCatalogoVacio(BaseReporte):
    """Una empresa recién creada entra a la pantalla y no ve un error."""

    def setUp(self):
        super().setUp()
        PedidoItem.query.delete()
        Pedido.query.delete()
        MapeoProductoCanal.query.delete()
        Producto.query.delete()
        db.session.commit()

    def test_responde_ok_y_avisa_que_no_hay_nada(self):
        respuesta, contexto = self.reporte()
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(contexto['grupos'], [])
        self.assertIn('Todavía no hay productos', respuesta.get_data(as_text=True))


class TestProductoSinMapeo(BaseReporte):
    """Un producto que nunca vino de un canal es su propio grupo."""

    def setUp(self):
        super().setUp()
        self.suelto = Producto(empresa_id=self.empresa_id, sku='CINTA-19',
                               nombre='Cinta aisladora 19mm', stock=5)
        db.session.add(self.suelto)
        db.session.commit()
        self.registrar_pedido(self.canal_manual_id, [(self.suelto, 'Cinta', 2)])
        _, self.contexto = self.reporte()

    def test_aparece_solo_y_con_su_venta(self):
        grupo = self.grupo_llamado(self.contexto, 'Cinta aisladora 19mm')
        self.assertEqual(len(grupo['filas']), 1)
        self.assertEqual(grupo['total']['vendido'], 2)
        self.assertEqual(grupo['total']['stock'], 5)

    def test_no_se_pego_a_ningun_otro_grupo(self):
        # Sin mapeo la clave es el propio id: dos productos sin mapeo no pueden
        # terminar bajo el mismo encabezado por tener el id_externo vacio.
        for grupo in self.contexto['grupos']:
            if grupo['nombre'] != 'Cinta aisladora 19mm':
                self.assertNotIn('CINTA-19', [f['sku'] for f in grupo['filas']])


class TestVariantesSinControlDeStock(BaseReporte):
    """stock NULL no es cero: el total del grupo tampoco lo inventa."""

    def setUp(self):
        super().setUp()
        db.session.get(Producto, self.negro.id).stock = None
        db.session.get(Producto, self.gris.id).stock = None
        db.session.commit()
        _, self.contexto = self.reporte()

    def test_el_total_del_grupo_queda_sin_control_y_no_en_cero(self):
        grupo = self.grupo_llamado(self.contexto, 'Tarjetero Minimalista de Aluminio')
        self.assertIsNone(grupo['total']['stock'])

    def test_si_una_sola_variante_lleva_la_cuenta_el_total_es_la_de_ella(self):
        db.session.get(Producto, self.gris.id).stock = 93
        db.session.commit()

        _, contexto = self.reporte()
        grupo = self.grupo_llamado(contexto, 'Tarjetero Minimalista de Aluminio')
        self.assertEqual(grupo['total']['stock'], 93)


class TestPartirNombre(unittest.TestCase):
    """El corte del sufijo de variante, sin base ni request de por medio."""

    def test_separa_el_color_del_nombre_base(self):
        self.assertEqual(rutas_productos._partir_nombre('Tarjetero (Negro)'),
                         ('Tarjetero', 'Negro'))

    def test_un_nombre_sin_parentesis_vuelve_entero(self):
        self.assertEqual(rutas_productos._partir_nombre('Micrófono Korvo'),
                         ('Micrófono Korvo', None))

    def test_corta_por_el_ultimo_parentesis(self):
        self.assertEqual(rutas_productos._partir_nombre('Caño (PVC) 3/4 (Blanco)'),
                         ('Caño (PVC) 3/4', 'Blanco'))

    def test_varios_valores_de_variante_quedan_juntos(self):
        # El sync los junta con ' / ' y aca no hay razon para volver a partirlos.
        self.assertEqual(rutas_productos._partir_nombre('Martillo (500g / Azul)'),
                         ('Martillo', '500g / Azul'))

    def test_un_nombre_que_es_solo_un_parentesis_no_deja_la_base_vacia(self):
        self.assertEqual(rutas_productos._partir_nombre('(Negro)'),
                         ('(Negro)', None))


class TestSumarStock(unittest.TestCase):
    """La suma donde None significa 'no se lleva la cuenta'."""

    def test_suma_normal(self):
        self.assertEqual(rutas_productos._sumar_stock(10, 5), 15)

    def test_arranca_desde_none(self):
        self.assertEqual(rutas_productos._sumar_stock(None, 5), 5)

    def test_un_none_no_baja_el_acumulado(self):
        self.assertEqual(rutas_productos._sumar_stock(10, None), 10)

    def test_todo_none_sigue_siendo_none(self):
        self.assertIsNone(rutas_productos._sumar_stock(None, None))


if __name__ == '__main__':
    unittest.main()
