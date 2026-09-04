# -*- coding: utf-8 -*-
"""La credencial del canal de Mercado Libre: leerla, refrescarla, verificarla
(FASE-MELI-S1).

Es la unica parte de la slice que toca la base. El transporte HTTP vive en
`integracion_mercadolibre.py`; aca se decide cuando se refresca, con que lock y
que queda guardado.

POR QUE EL REFRESCO VA ADENTRO DEL LOCK DE canal_venta

El refresh_token de Mercado Libre es DE UN SOLO USO: apenas se canjea, el
viejo muere y la respuesta trae uno nuevo. Eso cambia la naturaleza de una
condicion de carrera. En Tiendanube, dos sincronizaciones simultaneas
desperdician rate limit; aca, dos refrescos simultaneos MATAN EL CANAL:

    hilo A: lee refresh R1  ─┐
    hilo B: lee refresh R1  ─┘  (los dos leyeron lo mismo)
    hilo A: canjea R1 -> R2, guarda R2
    hilo B: canjea R1 -> 400 invalid_grant (R1 ya se uso)
    hilo B: ...y si el codigo fuera ingenuo, guardaria basura arriba de R2

y a partir de ahi no hay refresh_token valido en la base. La unica salida es
que Nachi vuelva a autorizar a mano, que es exactamente lo que la conexion
tenia que evitar.

Por eso la lectura del refresh_token, el POST a Mercado Libre y la escritura
del par nuevo van los tres DENTRO de la misma transaccion, detras del mismo
SELECT ... FOR UPDATE sobre la fila de canal_venta que ya usa
`sync_tiendanube._reservar_corrida`. Es el mismo lock a proposito: el cron de
sync y el refresco del token compiten por el mismo canal, y dos locks distintos
sobre el mismo recurso no protegen nada.

El segundo en llegar espera al primero y RECIEN AHI lee la credencial, con el
par nuevo ya commiteado. Por eso `refrescar_token` chequea el vencimiento
DESPUES de tomar el lock: el que espero se encuentra con un token recien
emitido y se vuelve sin gastar nada. Chequear antes del lock seria mirar un
dato viejo.

SQLite ignora el FOR UPDATE (no lo soporta y el dialecto de SQLAlchemy no lo
emite), lo cual esta bien: local hay un solo proceso. El lock de verdad es el
de Postgres en Supabase.

NADA de esto lo dispara todavia un cron: engancharlo al polling es S4. Por
ahora son funciones invocables y probadas.
"""

import sys
from datetime import datetime

import cripto
import integracion_mercadolibre as meli
from models import CanalVenta, CredencialCanal, db

TIPO_MERCADOLIBRE = 'mercadolibre'


def _log(mensaje):
    """Detalle tecnico al log del servidor. Nunca al navegador: los detalles
    de OAuth pueden traer eco de credenciales."""
    sys.stderr.write('[meli-credencial] %s\n' % mensaje)
    sys.stderr.flush()


def credencial_de(canal_id):
    """La credencial vigente del canal, o None.

    La mas nueva y no "la primera que aparezca": `credencial_canal` no tiene
    UNIQUE por canal_id, asi que el orden explicito es lo unico que garantiza
    que el callback escriba la misma fila que despues lee el refresco. Todo el
    modulo -- y la ruta del callback -- pasan por aca por ese motivo.
    """
    return (CredencialCanal.query
            .filter_by(canal_id=canal_id)
            .order_by(CredencialCanal.id.desc())
            .first())


def _descifrar(texto_cifrado, que):
    if not texto_cifrado:
        return None
    try:
        return cripto.descifrar(texto_cifrado)
    except cripto.ErrorCifrado as exc:
        raise meli.ErrorMercadoLibre(
            'La credencial de Mercado Libre no se pudo descifrar. Hay que volver '
            'a conectar el canal.',
            detalle='%s ilegible: %s' % (que, exc),
            reconectar=True,
        ) from exc


def refrescar_token(canal_id, solo_si_hace_falta=False):
    """Canjea el refresh_token por un par nuevo y lo guarda. Devuelve el
    access_token en texto plano.

    Con `solo_si_hace_falta=True` no gasta el refresh si el access todavia
    sirve: es la forma que va a usar el cron de S4. Con False (el default)
    refresca si o si, que es lo que hace falta para probarlo y para forzar una
    renovacion a mano.

    Todo -- lectura, POST y escritura -- pasa adentro del lock de canal_venta.
    Ver el docstring del modulo para el por que.

    Levanta ErrorMercadoLibre. Si `reconectar` viene en True, no hay reintento
    que lo arregle: la persona tiene que volver a autorizar.
    """
    try:
        return _refrescar_bajo_lock(canal_id, solo_si_hace_falta)
    except Exception:
        # Suelta el lock pase lo que pase. Sin esto, un canal que falla al
        # refrescar deja la fila de canal_venta trabada hasta que muera la
        # conexion, y el sync de ese canal se cuelga esperandola.
        db.session.rollback()
        raise


def _refrescar_bajo_lock(canal_id, solo_si_hace_falta):
    canal = (CanalVenta.query
             .filter_by(id=canal_id)
             .with_for_update()
             .first())
    if canal is None or canal.tipo != TIPO_MERCADOLIBRE:
        raise meli.ErrorMercadoLibre(
            'Ese canal de Mercado Libre no existe.',
            detalle='canal %r inexistente o de otro tipo' % canal_id,
        )

    # Se lee DESPUES de tomar el lock, no antes: si este es el segundo hilo, lo
    # que habia antes de esperar ya quedo viejo.
    credencial = credencial_de(canal_id)
    if credencial is None or not credencial.refresh_token_cifrado:
        raise meli.ErrorMercadoLibre(
            'El canal de Mercado Libre no tiene refresh_token guardado. Hay que '
            'volver a conectarlo.',
            detalle='canal %s sin refresh_token' % canal_id,
            reconectar=True,
        )

    if solo_si_hace_falta and credencial.access_token_cifrado \
            and not meli.token_vencido(credencial.expira_en):
        vigente = _descifrar(credencial.access_token_cifrado, 'el access_token')
        # Commit y no rollback: `_corrida_viva` del sync tambien commitea para
        # soltar el lock, y aca ademas cierra la transaccion que lo abrio.
        db.session.commit()
        return vigente

    anterior = _descifrar(credencial.refresh_token_cifrado, 'el refresh_token')

    # El POST va adentro de la transaccion abierta. Es deliberado que una
    # llamada HTTP sostenga un lock de fila -- son unos cientos de milisegundos
    # y el timeout esta acotado -- porque es la unica forma de que nadie mas
    # lea el refresh_token viejo mientras se lo esta quemando.
    datos = meli.refrescar_token(anterior)

    credencial.access_token_cifrado = cripto.cifrar(datos['access_token'])
    if datos['refresh_token']:
        credencial.refresh_token_cifrado = cripto.cifrar(datos['refresh_token'])
    else:
        # No deberia pasar: MeLi devuelve uno nuevo en cada refresco. Si pasa,
        # se conserva el que habia (peor es quedarse sin ninguno) y queda
        # escrito en el log, porque el proximo refresco va a fallar.
        _log('canal %s: el refresco no devolvio refresh_token nuevo; se conserva '
             'el anterior, que probablemente ya no sirva' % canal_id)
    credencial.expira_en = datos['expira_en']
    if datos['scope']:
        credencial.scope = datos['scope'][:255]
    credencial.activo = True
    credencial.fecha_actualizacion = datetime.utcnow()

    db.session.commit()
    _log('canal %s: token renovado, vence %s' % (canal_id, datos['expira_en']))
    return datos['access_token']


def access_token_vigente(canal_id):
    """El access_token del canal, renovado si estaba por vencer.

    Es la puerta que van a usar las slices que leen de la API (S2/S3): pedis el
    token y te olvidas de si habia que refrescarlo.
    """
    return refrescar_token(canal_id, solo_si_hace_falta=True)


def verificar_conexion(canal_id):
    """"Este canal, esta conectado de verdad?". Devuelve un dict, NUNCA levanta.

    {'conectado': bool, 'usuario': dict|None, 'motivo': str|None,
     'vencido': bool}

    Lo consume la pagina de integraciones, asi que tiene dos reglas:

      - No levanta. Un token vencido o revocado es un estado normal del mundo,
        no un error 500.
      - No refresca. Es un GET del navegador: no puede tener el efecto
        colateral de quemar el refresh_token de un solo uso. Si el token ya
        vencio segun `expira_en`, se dice y listo, sin pegarle a la API -- ya
        sabemos la respuesta y no hace falta gastar una llamada para
        confirmarla.
    """
    credencial = credencial_de(canal_id)
    if credencial is None or not credencial.access_token_cifrado:
        return {'conectado': False, 'usuario': None, 'vencido': False,
                'motivo': 'El canal no tiene credencial guardada.'}

    if meli.token_vencido(credencial.expira_en):
        return {
            'conectado': False, 'usuario': None, 'vencido': True,
            'motivo': 'El acceso vencio. Se renueva solo en la proxima '
                      'sincronizacion; si sigue asi, reconecta el canal.',
        }

    try:
        access_token = _descifrar(credencial.access_token_cifrado, 'el access_token')
    except meli.ErrorMercadoLibre as exc:
        return {'conectado': False, 'usuario': None, 'vencido': False,
                'motivo': str(exc)}

    estado = meli.verificar_credenciales(access_token)
    estado['vencido'] = False
    return estado
