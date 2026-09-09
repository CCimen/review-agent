"""The supported SCIM 2.0 Users protocol over existing console accounts."""

import hmac
import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi_users import exceptions
from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from sqlalchemy import func, select
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .admin_auth import (
    AdminAuth,
    ProvisioningConflict,
    ProvisionedAccountUpdate,
    User,
    provision_account,
    update_provisioned_account,
)
from .admin_identity_config import IdentitySettings


USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
ExternalId = Annotated[str, Field(min_length=1, max_length=255)]


class SCIMErrorBody(BaseModel):
    schemas: list[str] = [ERROR_SCHEMA]
    status: str
    detail: str
    scimType: str | None = None


class SCIMResponse(JSONResponse):
    media_type = "application/scim+json"


class SCIMBodyLimit:
    """Bound SCIM JSON before the framework parses it, including chunked input."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/scim/v2/"):
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > 65536:
                await SCIMResponse(
                    {
                        "schemas": [ERROR_SCHEMA],
                        "status": "413",
                        "detail": "SCIM requests may contain at most 64 KiB",
                    },
                    status_code=413,
                )(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def receive_body() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, receive_body, send)


class SCIMError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        self.status = status
        self.detail = detail
        self.scim_type = scim_type


def error_response(_request: Request, error: Exception) -> SCIMResponse:
    if not isinstance(error, SCIMError):
        raise error
    return SCIMResponse(
        SCIMErrorBody(
            status=str(error.status), detail=error.detail, scimType=error.scim_type
        ).model_dump(exclude_none=True),
        status_code=error.status,
        headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else None,
    )


class SCIMInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def attribute_names(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        fields = {name.lower(): name for name in cls.model_fields}
        result: dict[str, object] = {}
        for name, item in cast(dict[str, object], value).items():
            canonical = fields.get(name.lower(), name)
            if canonical in result:
                raise ValueError("Duplicate SCIM attribute")
            result[canonical] = item
        return result


class SCIMUserInput(SCIMInput):
    schemas: list[Literal["urn:ietf:params:scim:schemas:core:2.0:User"]] = Field(
        min_length=1, max_length=1
    )
    userName: EmailStr
    externalId: str | None = Field(default=None, min_length=1, max_length=255)
    active: bool = Field(default=True, strict=True)


class SCIMPatchOperation(SCIMInput):
    op: str = Field(max_length=7)
    path: str | None = Field(default=None, max_length=255)
    value: JsonValue = None


class SCIMPatch(SCIMInput):
    schemas: list[Literal["urn:ietf:params:scim:api:messages:2.0:PatchOp"]] = Field(
        min_length=1, max_length=1
    )
    Operations: list[SCIMPatchOperation] = Field(min_length=1, max_length=100)


def patch_changes(body: SCIMPatch) -> ProvisionedAccountUpdate:
    change = ProvisionedAccountUpdate()
    for operation in body.Operations:
        values: dict[str, JsonValue] = {}
        op = operation.op.lower()
        path = operation.path.lower() if operation.path else None
        if op == "remove":
            if path != "externalid":
                raise SCIMError(400, "Only externalId can be removed", "mutability")
            values["externalid"] = None
        elif op in ("add", "replace"):
            if path is None:
                if not isinstance(operation.value, dict):
                    raise SCIMError(
                        400, "An attribute object is required", "invalidValue"
                    )
                values.update(
                    {key.lower(): value for key, value in operation.value.items()}
                )
            else:
                values[path] = operation.value
        else:
            raise SCIMError(400, "Unsupported PATCH operation", "invalidSyntax")
        if not values.keys() <= {"username", "externalid", "active"}:
            raise SCIMError(
                400,
                "Supported paths are userName, externalId and active",
                "invalidPath",
            )
        # Validate each operation in order before changing any account state.
        try:
            if "username" in values:
                change.email = TypeAdapter(EmailStr).validate_python(values["username"])
            if "externalid" in values:
                change.external_id = TypeAdapter(ExternalId | None).validate_python(
                    values["externalid"]
                )
            if "active" in values:
                change.active = TypeAdapter(bool).validate_python(
                    values["active"], strict=True
                )
        except ValidationError as exc:
            raise SCIMError(400, "Invalid attribute value", "invalidValue") from exc
    return change


class SCIMMeta(BaseModel):
    resourceType: Literal["User"] = "User"
    location: str
    created: datetime | None
    lastModified: datetime | None


class SCIMUser(BaseModel):
    schemas: list[str] = [USER_SCHEMA]
    id: uuid.UUID
    userName: str
    externalId: str | None
    active: bool
    meta: SCIMMeta


class SCIMUserList(BaseModel):
    schemas: list[str] = [LIST_SCHEMA]
    totalResults: int
    startIndex: int
    itemsPerPage: int
    Resources: list[SCIMUser]


def create_router(auth: AdminAuth, settings: IdentitySettings) -> APIRouter:
    bearer = HTTPBearer(scheme_name="SCIMCredential", auto_error=False)

    def authenticate(
        credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        token = settings.scim_token
        if token is None or settings.oidc is None:
            raise SCIMError(404, "SCIM is not configured")
        if (
            credential is None
            or len(credential.credentials) > 512
            or not hmac.compare_digest(
                credential.credentials.encode(), token.get_secret_value().encode()
            )
        ):
            raise SCIMError(401, "A SCIM credential is required")

    router = APIRouter(
        prefix="/scim/v2",
        tags=["SCIM"],
        dependencies=[Depends(authenticate)],
        default_response_class=SCIMResponse,
        responses={"4XX": {"model": SCIMErrorBody}, 503: {"model": SCIMErrorBody}},
    )
    issuer = settings.oidc.issuer if settings.oidc else ""

    def resource(user: User) -> SCIMUser:
        return SCIMUser(
            id=user.id,
            userName=user.email,
            externalId=user.scim_external_id,
            active=user.is_active,
            meta=SCIMMeta(
                location=f"{auth.origin}/scim/v2/Users/{user.id}",
                created=user.scim_created_at,
                lastModified=user.scim_modified_at,
            ),
        )

    async def create_user(body: SCIMUserInput, response: Response) -> SCIMUser:
        async with auth.sessions() as session:
            try:
                user = await provision_account(
                    session,
                    issuer=issuer,
                    email=body.userName,
                    external_id=body.externalId,
                    active=body.active,
                )
            except ProvisioningConflict as exc:
                raise SCIMError(409, str(exc), "uniqueness") from exc
            result = resource(user)
            response.headers["Location"] = result.meta.location
            return result

    async def list_users(
        startIndex: Annotated[int, Query(le=2147483647)] = 1,
        count: Annotated[int, Query(le=2147483647)] = 100,
        filter: Annotated[str | None, Query(max_length=1000)] = None,
    ) -> SCIMUserList:
        startIndex, count = max(1, startIndex), max(0, min(100, count))
        query = select(User).where(User.scim_issuer == issuer, ~User.scim_deleted)
        if filter is not None:
            match = re.fullmatch(
                r'\s*(userName|externalId)\s+eq\s+("(?:[^"\\]|\\.)*")\s*',
                filter,
                re.IGNORECASE,
            )
            if match is None:
                raise SCIMError(
                    400,
                    "Supported filters are userName eq and externalId eq",
                    "invalidFilter",
                )
            try:
                value: str = json.loads(match[2])
            except ValueError as exc:
                raise SCIMError(400, "Invalid filter value", "invalidFilter") from exc
            query = query.where(
                func.lower(User.email) == value.lower()
                if match[1].lower() == "username"
                else User.scim_external_id == value
            )
        async with auth.sessions() as session:
            total = await session.scalar(
                select(func.count()).select_from(query.subquery())
            )
            users = await session.scalars(
                query.order_by(User.__table__.c.id).offset(startIndex - 1).limit(count)
            )
            resources = [resource(user) for user in users]
        return SCIMUserList(
            totalResults=total or 0,
            startIndex=startIndex,
            itemsPerPage=len(resources),
            Resources=resources,
        )

    async def get_user(user_id: uuid.UUID) -> SCIMUser:
        async with auth.sessions() as session:
            user = await session.get(User, user_id)
            if user is None or user.scim_issuer != issuer or user.scim_deleted:
                raise SCIMError(404, "User not found")
            return resource(user)

    async def update(
        user_id: uuid.UUID, change: ProvisionedAccountUpdate, *, deleted: bool = False
    ) -> SCIMUser:
        async with auth.sessions() as session:
            try:
                return resource(
                    await update_provisioned_account(
                        session,
                        user_id=user_id,
                        issuer=issuer,
                        change=change,
                        deleted=deleted,
                    )
                )
            except exceptions.UserNotExists as exc:
                raise SCIMError(404, "User not found") from exc
            except PermissionError as exc:
                raise SCIMError(403, str(exc)) from exc
            except ProvisioningConflict as exc:
                raise SCIMError(409, str(exc), "uniqueness") from exc

    async def replace_user(user_id: uuid.UUID, body: SCIMUserInput) -> SCIMUser:
        return await update(
            user_id,
            ProvisionedAccountUpdate(
                email=body.userName, external_id=body.externalId, active=body.active
            ),
        )

    async def patch_user(user_id: uuid.UUID, body: SCIMPatch) -> SCIMUser:
        return await update(user_id, patch_changes(body))

    async def delete_user(user_id: uuid.UUID) -> None:
        await update(user_id, ProvisionedAccountUpdate(), deleted=True)

    def service_provider_config() -> dict[str, JsonValue]:
        return {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 100},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "SCIM bearer credential",
                    "description": "Dedicated organization provisioning credential",
                    "primary": True,
                }
            ],
        }

    def user_type() -> dict[str, JsonValue]:
        return {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "schema": USER_SCHEMA,
            "description": "Directory-managed console accounts; team membership and roles are managed in the console",
        }

    def user_schema() -> dict[str, JsonValue]:
        return {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Schema"],
            "id": USER_SCHEMA,
            "name": "User",
            "description": "userName is the account email. Supported mutable attributes: userName, externalId and active.",
            "attributes": [
                {
                    "name": "userName",
                    "type": "string",
                    "multiValued": False,
                    "required": True,
                    "caseExact": False,
                    "mutability": "readWrite",
                    "returned": "default",
                    "uniqueness": "server",
                    "description": "Account email address",
                },
                {
                    "name": "externalId",
                    "type": "string",
                    "multiValued": False,
                    "required": False,
                    "caseExact": True,
                    "mutability": "readWrite",
                    "returned": "default",
                    "uniqueness": "server",
                },
                {
                    "name": "active",
                    "type": "boolean",
                    "multiValued": False,
                    "required": False,
                    "mutability": "readWrite",
                    "returned": "default",
                },
            ],
        }

    def resource_types() -> dict[str, JsonValue]:
        return {
            "schemas": [LIST_SCHEMA],
            "totalResults": 1,
            "startIndex": 1,
            "itemsPerPage": 1,
            "Resources": [user_type()],
        }

    def schemas() -> dict[str, JsonValue]:
        return {
            "schemas": [LIST_SCHEMA],
            "totalResults": 1,
            "startIndex": 1,
            "itemsPerPage": 1,
            "Resources": [user_schema()],
        }

    router.add_api_route(
        "/Users",
        create_user,
        methods=["POST"],
        status_code=201,
        response_model_exclude_none=True,
    )
    router.add_api_route(
        "/Users", list_users, methods=["GET"], response_model_exclude_none=True
    )
    router.add_api_route(
        "/Users/{user_id}", get_user, methods=["GET"], response_model_exclude_none=True
    )
    router.add_api_route(
        "/Users/{user_id}",
        replace_user,
        methods=["PUT"],
        response_model_exclude_none=True,
    )
    router.add_api_route(
        "/Users/{user_id}",
        patch_user,
        methods=["PATCH"],
        response_model_exclude_none=True,
    )
    router.add_api_route(
        "/Users/{user_id}", delete_user, methods=["DELETE"], status_code=204
    )
    router.add_api_route(
        "/ServiceProviderConfig", service_provider_config, methods=["GET"]
    )
    router.add_api_route("/ResourceTypes", resource_types, methods=["GET"])
    router.add_api_route("/ResourceTypes/User", user_type, methods=["GET"])
    router.add_api_route("/Schemas", schemas, methods=["GET"])
    router.add_api_route(f"/Schemas/{USER_SCHEMA}", user_schema, methods=["GET"])

    return router
