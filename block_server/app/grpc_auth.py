"""
Service-to-service API key auth for the Block Server's gRPC server.

Mirrors the REST layer's require_service_key (main.py's _check_service_key):
enforced only on UploadBlock and GetBlock, skipped entirely when
SERVICE_API_KEY isn't configured (dev/test mode), and never applied to
HasBlock — matching REST's public GET /blocks/has/{hash}.
"""

from typing import Optional, Tuple

import grpc

from .config import settings

_PROTECTED_METHODS = {
    "/dropboxsim.internal.BlockServer/UploadBlock",
    "/dropboxsim.internal.BlockServer/GetBlock",
}

_FORBIDDEN_MESSAGE = "invalid or missing service API key"


def _extract_service_key(metadata: Optional[Tuple[Tuple[str, str], ...]]) -> Optional[str]:
    for key, value in metadata or ():
        if key.lower() == "x-service-key":
            return value
    return None


class ServiceKeyInterceptor(grpc.aio.ServerInterceptor):
    async def intercept_service(self, continuation, handler_call_details):
        handler = await continuation(handler_call_details)
        if handler is None:
            return handler
        if not settings.service_api_key:
            return handler  # enforcement disabled (dev/test)
        if handler_call_details.method not in _PROTECTED_METHODS:
            return handler

        key = _extract_service_key(handler_call_details.invocation_metadata)
        if key == settings.service_api_key:
            return handler

        if handler.unary_unary:
            async def abort_unary(request, context):
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, _FORBIDDEN_MESSAGE)

            return grpc.unary_unary_rpc_method_handler(
                abort_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        async def abort_stream(request, context):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, _FORBIDDEN_MESSAGE)
            yield  # pragma: no cover — unreachable, satisfies generator shape

        return grpc.unary_stream_rpc_method_handler(
            abort_stream,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
