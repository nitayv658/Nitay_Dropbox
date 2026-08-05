"""
JWT authentication for the Metadata Service's gRPC servers.

Validates the "authorization: Bearer <token>" gRPC metadata entry on every
call using the same decode_token() the REST API uses, and makes the resolved
identity available to servicer methods via get_authenticated_user().

Only unary_unary RPCs are supported (that's everything in internal.proto
today). A streaming RPC added later without updating this interceptor should
fail loudly rather than silently bypass authentication.
"""

import contextvars
from typing import Optional, Tuple

import grpc

from .auth import AuthError, decode_token

_current_user: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "grpc_authenticated_user", default=None
)

_UNAUTHENTICATED_MESSAGE = "missing or invalid bearer token"


def get_authenticated_user() -> dict:
    """
    Return the identity JWTAuthInterceptor resolved for the in-flight call.
    Raises RuntimeError if called outside an intercepted RPC — a sign the
    interceptor isn't wired up, not something that should happen in production.
    """
    user = _current_user.get()
    if user is None:
        raise RuntimeError(
            "no authenticated user in context — is JWTAuthInterceptor wired?"
        )
    return user


def _extract_bearer_token(metadata: Optional[Tuple[Tuple[str, str], ...]]) -> Optional[str]:
    for key, value in metadata or ():
        if key.lower() == "authorization" and value.lower().startswith("bearer "):
            return value[len("bearer "):]
    return None


class JWTAuthInterceptor(grpc.aio.ServerInterceptor):
    async def intercept_service(self, continuation, handler_call_details):
        handler = await continuation(handler_call_details)
        if handler is None:
            return handler
        if not handler.unary_unary:
            raise NotImplementedError(
                f"JWTAuthInterceptor only supports unary_unary RPCs; "
                f"{handler_call_details.method} needs explicit auth handling added"
            )

        token = _extract_bearer_token(handler_call_details.invocation_metadata)
        try:
            user = decode_token(token)
        except AuthError:
            return self._unauthenticated_handler(handler)
        return self._authenticated_handler(handler, user)

    @staticmethod
    def _unauthenticated_handler(handler):
        async def abort_unary(request, context):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, _UNAUTHENTICATED_MESSAGE)

        return grpc.unary_unary_rpc_method_handler(
            abort_unary,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )

    @staticmethod
    def _authenticated_handler(handler, user: dict):
        inner = handler.unary_unary

        async def wrapped(request, context):
            token = _current_user.set(user)
            try:
                return await inner(request, context)
            finally:
                _current_user.reset(token)

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
