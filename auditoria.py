# -*- coding: utf-8 -*-
"""FASE-AUDITORIA-S2 - Quien toco que, sin tocar las rutas.

EL PROBLEMA QUE RESUELVE

Hasta esta slice el historial se escribia a mano: once llamadas a
`registrar_cambio()` desperdigadas por app.py. El patron existia desde el
principio y aun asi las cinco pantallas nuevas del ecommerce (costos,
comisiones, venta manual, OAuth) nacieron sin una sola llamada. No es
distraccion: acordarse en cada ruta nueva es una disciplina que nadie sostiene.

Este modulo invierte la carga. Un listener de `before_flush` mira lo que la
sesion esta por escribir y, si toca una de las tablas de la lista blanca, deja
la fila de historial el mismo. La ruta no se entera y no hay nada que recordar.

POR QUE `before_flush` Y NO `after_flush`

Las filas se agregan a la MISMA sesion, antes de que salga el INSERT. O sea:
el cambio y su registro entran en la misma transaccion y en el mismo commit.
Si el commit se cae, se caen los dos. `registrar_cambio()` hace su propio
commit aparte, asi que hoy un cambio puede quedar guardado sin registro; el
hook no tiene esa grieta.

POR QUE EL SYNC NO GENERA RUIDO

`Pedido` y `Producto` estan en la lista blanca y el sync de Tiendanube escribe
las dos tablas a mansalva -- una corrida de 200 pedidos generaria 200 filas que
nadie va a leer y que ahogarian las tres ediciones humanas que si importan.

No hace falta ningun flag ni tocar el sync para evitarlo: el sync corre en su
propio app_context, adentro de un thread (`sync_tiendanube._correr_en_contexto`)
o desde el cron (`scripts/sync_periodico.py`). En ninguno de los dos casos hay
request context ni usuario logueado. Y este hook solo escribe cuando hay un
actor humano autenticado. El sync queda afuera por construccion, no por una
lista de excepciones que alguien tenga que mantener.

La contracara, explicita: hoy una escritura sin actor NO se audita. Cuando haya
que registrar lo que hace el sistema -- el caso que importa es
`sync_tiendanube:617`, que borra y reescribe los PedidoItem y puede pisar una
edicion hecha a mano -- va a ser una slice aparte, con `usuario_id = NULL`, que
la migracion de esta slice ya deja permitido.

LO QUE NUNCA SE COPIA A LA TABLA

Un log de auditoria es una tabla mas: se lee desde una pantalla, se exporta, se
mira en Supabase. Meter ahi un hash de contrasena o un token de OAuth seria
sacar el secreto del unico lugar donde esta cuidado. Los campos de
CAMPOS_SECRETOS se registran como cambiados, con los valores reemplazados por
un marcador: queda constancia de que la contrasena se cambio y de quien la
cambio, sin quedar constancia de cual es.
"""

from flask import has_request_context
from sqlalchemy import event, inspect

from models import CredencialCanal, Historial, Pedido, Producto, Usuario, db

# Las cuatro tablas donde estan los datos reales y donde hoy no queda rastro:
# el costo del producto (S3-COSTO), la comision del pedido (S3-COMISION) y de
# la venta manual, los tokens de los canales, y el usuario mismo.
#
# Gasto, Ingreso, Categoria y Empresa quedan AFUERA a proposito: ya tienen sus
# once llamadas manuales a `registrar_cambio()` en app.py. Meterlas aca
# duplicaria cada fila.
TABLAS_AUDITADAS = (Producto, Pedido, CredencialCanal, Usuario)

# Se registra QUE cambiaron, nunca a que. Ver el docstring del modulo.
CAMPOS_SECRETOS = frozenset({
    'password',
    'codigo_verificacion',
    'codigo_reset',
    'access_token_cifrado',
    'refresh_token_cifrado',
})

# Ruido que no dice nada de lo que hizo una persona.
#   fecha_actualizacion: la pone `onupdate` sola en cada UPDATE.
#   raw_payload:         el JSON crudo de Tiendanube, miles de caracteres.
CAMPOS_IGNORADOS = frozenset({
    'fecha_actualizacion',
    'fecha_creacion',
    'raw_payload',
})

SECRETO = '(oculto)'

# `descripcion` es varchar(200) y `valor_*` es Text. El corte de los valores no
# es por la columna sino por lo que sirve leer: un Text sin limite invita a que
# entre un payload entero el dia que alguien sume una tabla a la lista.
LARGO_DESCRIPCION = 200
LARGO_VALOR = 500


def _actor():
    """El usuario logueado, o None si esta escritura no la hizo una persona.

    None es el caso del sync y de los scripts. Devolver None es lo que los
    mantiene afuera del historial; ver el docstring del modulo.
    """
    if not has_request_context():
        return None
    try:
        from flask_login import current_user
        if current_user is None or not current_user.is_authenticated:
            return None
        # Forzar la resolucion del proxy aca adentro: si la sesion quedo rota,
        # que falle contra este `except` y no mas adelante.
        return current_user._get_current_object()
    except Exception:  # noqa: BLE001 - auditar nunca puede voltear la escritura
        return None


def _empresa_de(sesion, obj, actor):
    """A que empresa pertenece lo que se esta tocando.

    `historial.empresa_id` es NOT NULL, asi que sin esto no hay fila. Se busca
    primero en el objeto y recien despues en el actor: si un dia un usuario
    toca algo de otra empresa, el historial tiene que quedar del lado del dato,
    no del lado de quien lo toco.
    """
    propia = getattr(obj, 'empresa_id', None)
    if propia:
        return propia

    # CredencialCanal no tiene empresa_id: cuelga del canal.
    canal_id = getattr(obj, 'canal_id', None)
    if canal_id:
        # no_autoflush es obligatorio: estamos ADENTRO de un before_flush y una
        # consulta con autoflush prendido reentraria al flush que la disparo.
        from models import CanalVenta
        with sesion.no_autoflush:
            canal = sesion.get(CanalVenta, canal_id)
        if canal is not None and canal.empresa_id:
            return canal.empresa_id

    return getattr(actor, 'empresa_id', None)


def _texto(valor):
    """Un valor de columna, listo para guardar como texto."""
    if valor is None:
        return None
    texto = str(valor)
    if len(texto) > LARGO_VALOR:
        texto = texto[:LARGO_VALOR - 3] + '...'
    return texto


def _etiqueta(obj):
    """Como se llama esto en castellano, para que la pantalla se lea sola."""
    for campo in ('sku', 'nombre', 'numero_externo', 'id_externo', 'email'):
        valor = getattr(obj, campo, None)
        if valor:
            return str(valor)
    return '#%s' % getattr(obj, 'id', '?')


def _describir(obj, sufijo=''):
    texto = '%s %s%s' % (obj.__tablename__, _etiqueta(obj), sufijo)
    return texto[:LARGO_DESCRIPCION]


def _campos_cambiados(obj):
    """[(campo, valor_viejo, valor_nuevo)] de lo que de verdad cambio.

    `history` y NUNCA `load_history()`. La diferencia no es de comodidad:

    `load_history()` va a la base a buscar el valor viejo de un atributo que no
    esta cargado. Sobre un objeto EXPIRADO -- el estado normal despues de un
    commit -- esa lectura refresca el objeto entero, y al refrescarlo pisa la
    modificacion que todavia no se habia flusheado. O sea que auditar el cambio
    lo borraba. Con `attrs['id'].load_history()` como primera iteracion del
    bucle alcanzaba: el refresh se disparaba ahi y para cuando el bucle llegaba
    al campo que de verdad cambio, ya no habia cambio que ver.

    Por eso ademas se saltean los atributos no cargados: preguntar por ellos es
    justamente lo que dispara el refresh.

    El costo de no ir a la base: si alguien modifica un objeto expirado, el
    valor viejo no esta en memoria y la fila queda con `valor_anterior` en
    NULL. Es una perdida de detalle, no de correccion, y no pasa por el camino
    real -- las rutas leen el objeto antes de tocarlo, asi que llega cargado.
    """
    estado = inspect(obj)
    sin_cargar = estado.unloaded
    cambios = []
    for attr in estado.mapper.column_attrs:
        campo = attr.key
        if campo in CAMPOS_IGNORADOS or campo in sin_cargar:
            continue
        historia = estado.attrs[campo].history
        if not historia.has_changes():
            continue
        viejo = historia.deleted[0] if historia.deleted else None
        nuevo = historia.added[0] if historia.added else None
        if viejo == nuevo:
            continue
        if campo in CAMPOS_SECRETOS:
            cambios.append((campo, SECRETO, SECRETO))
        else:
            cambios.append((campo, _texto(viejo), _texto(nuevo)))
    return cambios


def _fila(empresa_id, accion, obj, descripcion, usuario_id,
          valor_anterior=None, valor_nuevo=None):
    return Historial(
        usuario_id=usuario_id,
        empresa_id=empresa_id,
        accion=accion,
        tipo=obj.__tablename__,
        id_registro=getattr(obj, 'id', None),
        descripcion=descripcion,
        valor_anterior=valor_anterior,
        valor_nuevo=valor_nuevo,
    )


def _filas_de(sesion, obj, accion, actor, borrados):
    """Las filas de historial que le corresponden a un objeto."""
    empresa_id = _empresa_de(sesion, obj, actor)
    if not empresa_id:
        # Sin empresa no hay fila posible (la columna es NOT NULL). Se saltea
        # en vez de romper: auditar no puede voltear una escritura legitima.
        return []

    # El unico caso donde el actor no puede ir en la fila: se esta borrando a
    # si mismo. La FK apuntaria a un usuario que en este mismo flush deja de
    # existir. Queda NULL -- por eso la migracion la hace nullable -- y el
    # nombre sobrevive en la descripcion.
    se_autoborra = any(obj_borrado is actor for obj_borrado in borrados)
    usuario_id = None if se_autoborra else actor.id
    firma = '' if usuario_id else ' (por %s, cuenta eliminada)' % _etiqueta(actor)

    if accion == 'crear':
        return [_fila(empresa_id, 'crear', obj,
                      _describir(obj, ' creado' + firma), usuario_id)]

    if accion == 'eliminar':
        return [_fila(empresa_id, 'eliminar', obj,
                      _describir(obj, ' eliminado' + firma), usuario_id)]

    # Editar: una fila POR CAMPO. La tabla no tiene columna `campo`, asi que el
    # nombre viaja en la descripcion; el par viejo/nuevo va en sus columnas.
    filas = []
    for campo, viejo, nuevo in _campos_cambiados(obj):
        filas.append(_fila(empresa_id, 'editar', obj,
                           _describir(obj, ' - %s%s' % (campo, firma)),
                           usuario_id, valor_anterior=viejo, valor_nuevo=nuevo))
    return filas


def _registrar(sesion, contexto_flush, instancias):
    actor = _actor()
    if actor is None:
        # Sync, cron o script: no hay persona a quien atribuirle esto.
        return

    # Las tres colecciones se copian ANTES de agregar nada: `sesion.new` crece
    # con cada fila de historial y iterarla mientras se modifica revienta.
    nuevos = [o for o in sesion.new if isinstance(o, TABLAS_AUDITADAS)]
    borrados = [o for o in sesion.deleted if isinstance(o, TABLAS_AUDITADAS)]
    editados = [o for o in sesion.dirty
                if isinstance(o, TABLAS_AUDITADAS)
                and sesion.is_modified(o, include_collections=False)]

    filas = []
    for obj in nuevos:
        filas.extend(_filas_de(sesion, obj, 'crear', actor, borrados))
    for obj in editados:
        filas.extend(_filas_de(sesion, obj, 'editar', actor, borrados))
    for obj in borrados:
        filas.extend(_filas_de(sesion, obj, 'eliminar', actor, borrados))

    for fila in filas:
        sesion.add(fila)


def instalar():
    """Engancha el listener. Idempotente: llamarla dos veces no duplica filas."""
    if not event.contains(db.session, 'before_flush', _registrar):
        event.listen(db.session, 'before_flush', _registrar)


def desinstalar():
    """Solo para los tests que necesitan la sesion sin auditoria."""
    if event.contains(db.session, 'before_flush', _registrar):
        event.remove(db.session, 'before_flush', _registrar)
