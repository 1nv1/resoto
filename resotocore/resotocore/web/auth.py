import json
import logging
import re
import time
from contextvars import ContextVar
from datetime import datetime, timedelta
from re import RegexFlag
from typing import Any, Dict, Optional, Set, Callable, Awaitable, Tuple, Union
from urllib.parse import urlparse

import jwt
from aiohttp import web
from aiohttp.web import Request, StreamResponse
from aiohttp.web import middleware
from attr import define
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey, RSAPrivateKey
from cryptography.x509 import Certificate
from jwt import PyJWTError

from resotocore.core_config import CoreConfig
from resotocore.db.system_data_db import SystemDataDb
from resotocore.util import uuid_str
from resotocore.web.certificate_handler import CertificateHandler
from resotolib import jwt as ck_jwt
from resotolib.asynchronous.web import RequestHandler, Middleware
from resotolib.jwt import encode_jwt, create_jwk_dict
from resotolib.types import Json
from resotolib.utils import utc
from resotolib.x509 import gen_rsa_key, gen_csr, key_to_bytes, cert_to_bytes, load_key_from_bytes, load_cert_from_bytes

log = logging.getLogger(__name__)
JWT = Dict[str, Any]
_JWT_Context: ContextVar[JWT] = ContextVar("JWT", default={})
CodeLifeTime = timedelta(minutes=5)


@define
class AuthorizedUser:
    email: str
    roles: Set[str]
    authorized_at: datetime

    def is_valid(self) -> bool:
        return utc() - self.authorized_at < CodeLifeTime


async def jwt_from_context() -> JWT:
    """
    Inside a request handler, this value retrieves the current jwt.
    """
    return _JWT_Context.get()


def raw_jwt_from_auth_message(msg: str) -> Optional[str]:
    """
    Expected message: json object with type kind="authorization" and a jwt field
    { "kind": "authorization", "jwt": "Bearer <jwt>" }
    """
    try:
        js = json.loads(msg)
        assert js.get("kind") == "authorization"
        return js.get("jwt")  # type: ignore
    except Exception:
        return None


@middleware
async def no_check(request: Request, handler: RequestHandler) -> StreamResponse:
    # all requests are authorized automatically
    request["authorized"] = True
    return await handler(request)


class AuthHandler:
    def __init__(
        self,
        system_db: SystemDataDb,
        config: CoreConfig,
        cert_handler: CertificateHandler,
        always_allowed_paths: Set[str],
        not_allowed: Optional[Callable[[Request], Awaitable[StreamResponse]]] = None,
    ) -> None:
        self.system_db = system_db
        self.config = config
        self.psk = config.args.psk
        self.cert_handler = cert_handler
        self.always_allowed_paths = always_allowed_paths
        self.not_allowed = not_allowed
        self.authorization_codes: Dict[str, AuthorizedUser] = {}
        self.signing_key_private: Optional[RSAPrivateKey] = None  # set on start
        self.signing_key_certificate: Optional[Certificate] = None  # set on start
        self.signing_key_jwk: Optional[Json] = None  # set on start

    async def start(self) -> None:
        keys = await self.system_db.jwt_signing_keys()
        # check if the signing key is already in the database
        if keys is None:
            signing_key = gen_rsa_key()
            # TODO: implement certificate renewal
            cert, _ = self.cert_handler.sign(
                gen_csr(signing_key), days_valid=3650, key_usage={"key_cert_sign": True, "key_agreement": True}
            )
            key_string = key_to_bytes(signing_key).decode("utf-8")
            await self.system_db.update_jwt_signing_keys(key_string, cert_to_bytes(cert).decode("utf-8"))
            self.signing_key_private = signing_key
            self.signing_key_certificate = cert
            self.signing_key_jwk = {"keys": [create_jwk_dict(cert)]}
        else:
            key_string, cert_str = keys
            cert = load_cert_from_bytes(cert_str.encode("utf-8"))
            self.signing_key_private = load_key_from_bytes(key_string.encode("utf-8"))
            self.signing_key_certificate = cert
            self.signing_key_jwk = {"keys": [create_jwk_dict(cert)]}

    async def stop(self) -> None:
        pass

    def middleware(self) -> Middleware:
        if self.psk:
            log.info("Use JWT authentication with a pre shared key")
            return self.check_auth()
        else:
            log.info("No authentication requested.")
            return no_check

    async def validate_jwt(self, auth_header: str, request: Request) -> bool:
        def set_valid_jwt(psk_or_cert: Union[str, Certificate, RSAPublicKey]) -> Optional[JWT]:
            try:
                # note: the expiration is already checked by this function
                jwt_token = ck_jwt.decode_jwt_from_header_value(auth_header, psk_or_cert)
            except PyJWTError:
                return None
            if jwt_token:
                request["authorized"] = True  # deferred check in websocket handler
                request["jwt"] = jwt_token
                _JWT_Context.set(jwt_token)
            return jwt_token

        assert self.signing_key_certificate is not None, "AuthHandler not started"
        # based on the jwt, we either use the PSK or the public key
        _, token = auth_header.split(" ", maxsplit=1)
        jwt_header = jwt.get_unverified_header(token)
        # in case of RS256, the public key is used to verify the signature
        secret = self.signing_key_certificate if jwt_header.get("alg") == "RS256" else self.psk
        authorized = set_valid_jwt(secret) is not None
        return authorized

    async def validate_code(self, code: str, request: Request) -> bool:
        if (user := await self.authorized_user(code)) and user.is_valid():
            jwt_token, data = self.user_jwt(user)
            # this will be picked up in on_response_prepare and sent as a header
            request["send_auth_response_header"] = f"Bearer {jwt_token}"
            # set encoded data the same way as if it was a jwt
            request["jwt"] = data
            request["authorized"] = True
            return True
        return False

    def check_auth(self) -> Middleware:
        def always_allowed(request: Request) -> bool:
            for path in self.always_allowed_paths:
                if re.fullmatch(path, request.path, RegexFlag.IGNORECASE):
                    return True
            return False

        @middleware
        async def valid_auth_handler(request: Request, handler: RequestHandler) -> StreamResponse:
            # make sure origin and host match, so the request is valid
            origin: Optional[str] = urlparse(request.headers.get("Origin")).hostname  # type: ignore
            host: Optional[str] = request.headers.get("Host")
            if host is not None and origin is not None:
                if ":" in host:
                    host = host.split(":")[0]
                if origin.lower() != host.lower():
                    log.warning(f"Origin {origin} is not allowed in request from {request.remote} to {request.path}")
                    raise web.HTTPForbidden()

            allowed = False
            # Note: order is important: Authorization header and code is checked even for always allowed paths
            if auth_head := (request.headers.get("Authorization") or request.cookies.get("resoto_authorization")):
                allowed = await self.validate_jwt(auth_head, request)
            elif code := request.query.get("code"):
                allowed = await self.validate_code(code, request)
            elif always_allowed(request):
                allowed = True
            if allowed:
                return await handler(request)
            else:
                if self.not_allowed:
                    return await self.not_allowed(request)
                else:
                    raise web.HTTPUnauthorized()

        return valid_auth_handler

    async def authorized_user(self, code: str) -> Optional[AuthorizedUser]:
        for invalid_code in [k for k, v in self.authorization_codes.items() if not v.is_valid()]:
            self.authorization_codes.pop(invalid_code, None)
        return self.authorization_codes.get(code)

    async def add_authorized_user(self, user: AuthorizedUser) -> str:
        code = str(uuid_str())
        self.authorization_codes[code] = user
        return code

    def user_jwt(self, user: AuthorizedUser) -> Tuple[str, Json]:
        assert self.signing_key_private is not None, "AuthHandler not started"
        assert self.signing_key_certificate is not None, "AuthHandler not started"
        exp = int(time.time() + self.config.api.access_token_expiration_seconds)
        data = {"email": user.email, "roles": ",".join(user.roles), "exp": exp}
        return encode_jwt(data, self.signing_key_private, cert=self.signing_key_certificate), data
