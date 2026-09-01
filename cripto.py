"""Cifrado simetrico de credenciales de terceros (FASE3-S1).

Los access_token de los canales de venta no pueden vivir en texto plano en la
base: quien lea una fila de `credencial_canal` no tiene que poder operar la
tienda de Tiendanube. Se usa Fernet (AES-128-CBC + HMAC-SHA256, con IV y
timestamp), que ya viene resuelto en `cryptography`.

La clave se toma de la variable de entorno CREDENTIALS_ENCRYPTION_KEY y NO
tiene fallback: sin clave la app no arranca, igual que sin SECRET_KEY. Un
fallback silencioso a texto plano seria peor que no cifrar, porque nadie se
enteraria.

Para generar una clave nueva:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os

from cryptography.fernet import Fernet, InvalidToken

VARIABLE_CLAVE = 'CREDENTIALS_ENCRYPTION_KEY'

# Se cachea el Fernet ya construido para no rearmarlo en cada token.
_fernet = None


class ErrorCifrado(Exception):
    """El texto cifrado no se pudo descifrar con la clave actual.

    Pasa si se rota CREDENTIALS_ENCRYPTION_KEY sin recifrar lo guardado, o si
    la columna quedo corrupta. El llamador deberia tratarlo como credencial
    invalida y pedir reconectar el canal, no como un error de red.
    """


def _construir_fernet():
    clave = os.environ.get(VARIABLE_CLAVE)
    if not clave:
        raise RuntimeError(
            f"{VARIABLE_CLAVE} no esta definida. Configurala como variable de "
            "entorno antes de arrancar la aplicacion: sin ella no se pueden "
            "guardar credenciales de canales cifradas, y guardarlas en texto "
            "plano no es una opcion."
        )
    try:
        return Fernet(clave.encode() if isinstance(clave, str) else clave)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{VARIABLE_CLAVE} no es una clave Fernet valida (se espera base64 "
            f"urlsafe de 32 bytes): {exc}"
        ) from exc


def verificar_clave_configurada():
    """Falla al arrancar si la clave falta o es invalida. La llama app.py."""
    _obtener_fernet()


def _obtener_fernet():
    global _fernet
    if _fernet is None:
        _fernet = _construir_fernet()
    return _fernet


def reset_cache():
    """Olvida el Fernet cacheado. Solo para tests que cambian la env."""
    global _fernet
    _fernet = None


def cifrar(texto_plano):
    """Devuelve el token Fernet (str ASCII) listo para guardar en la base.

    None y '' pasan de largo como None: no tiene sentido cifrar un vacio, y
    devolver None deja la columna nullable como lo que es.
    """
    if not texto_plano:
        return None
    return _obtener_fernet().encrypt(texto_plano.encode('utf-8')).decode('ascii')


def descifrar(texto_cifrado):
    """Inversa de cifrar(). Levanta ErrorCifrado si el token no es valido."""
    if not texto_cifrado:
        return None
    try:
        dato = texto_cifrado.encode('ascii') if isinstance(texto_cifrado, str) else texto_cifrado
        return _obtener_fernet().decrypt(dato).decode('utf-8')
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ErrorCifrado(
            'No se pudo descifrar la credencial guardada. Puede que se haya '
            'rotado CREDENTIALS_ENCRYPTION_KEY: hay que reconectar el canal.'
        ) from exc
