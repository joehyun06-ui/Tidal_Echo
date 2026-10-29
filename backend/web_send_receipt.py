"""Optional Web send correlation in the canonical human message, not a retry key."""
import re

from fastapi import HTTPException


def request_meta(body):
    if 'client_request_id' not in body:
        return {}  # Existing clients retain their exact admission behavior.
    value = body['client_request_id']
    if not isinstance(value, str) or not re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', value
    ):
        raise HTTPException(400, detail='send_request_id_invalid')
    return {'client_request_id': value}
