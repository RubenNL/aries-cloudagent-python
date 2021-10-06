"""Credential definition admin routes."""

import json
from time import time

from asyncio import ensure_future, shield

from aiohttp import web
from aiohttp_apispec import (
    docs,
    match_info_schema,
    querystring_schema,
    request_schema,
    response_schema,
)

from marshmallow import fields

from ...admin.request_context import AdminRequestContext
from ...core.event_bus import Event, EventBus
from ...core.profile import Profile
from ...indy.issuer import IndyIssuer, IndyIssuerError
from ...indy.models.cred_def import CredentialDefinitionSchema
from ...ledger.base import BaseLedger
from ...ledger.error import LedgerError
from ...protocols.endorse_transaction.v1_0.manager import (
    TransactionManager,
    TransactionManagerError,
)
from ...protocols.endorse_transaction.v1_0.models.transaction_record import (
    TransactionRecordSchema,
)
from ...revocation.error import RevocationError, RevocationNotSupportedError
from ...revocation.indy import IndyRevocation
from ...revocation.util import (
    REVOCATION_EVENT_PREFIX,
    REVOCATION_REG_EVENT,
)
from ...storage.base import BaseStorage, StorageRecord
from ...storage.error import StorageError
from ...tails.base import BaseTailsServer

from ..models.openapi import OpenAPISchema
from ..valid import INDY_CRED_DEF_ID, INDY_REV_REG_SIZE, INDY_SCHEMA_ID


from .util import (
    CredDefQueryStringSchema,
    CRED_DEF_TAGS,
    CRED_DEF_SENT_RECORD_TYPE,
    CRED_DEF_EVENT_PREFIX,
    EVENT_LISTENER_PATTERN,
)


from ..valid import UUIDFour
from ...connections.models.conn_record import ConnRecord
from ...storage.error import StorageNotFoundError
from ..models.base import BaseModelError


class CredentialDefinitionSendRequestSchema(OpenAPISchema):
    """Request schema for schema send request."""

    schema_id = fields.Str(description="Schema identifier", **INDY_SCHEMA_ID)
    support_revocation = fields.Boolean(
        required=False, description="Revocation supported flag"
    )
    revocation_registry_size = fields.Int(
        description="Revocation registry size",
        required=False,
        strict=True,
        **INDY_REV_REG_SIZE,
    )
    tag = fields.Str(
        required=False,
        description="Credential definition identifier tag",
        default="default",
        example="default",
    )


class CredentialDefinitionSendResultSchema(OpenAPISchema):
    """Result schema content for schema send request with auto-endorse."""

    credential_definition_id = fields.Str(
        description="Credential definition identifier", **INDY_CRED_DEF_ID
    )


class TxnOrCredentialDefinitionSendResultSchema(OpenAPISchema):
    """Result schema for credential definition send request."""

    sent = fields.Nested(
        CredentialDefinitionSendResultSchema(),
        required=False,
        definition="Content sent",
    )
    txn = fields.Nested(
        TransactionRecordSchema(),
        required=False,
        description="Credential definition transaction to endorse",
    )


class CredentialDefinitionGetResultSchema(OpenAPISchema):
    """Result schema for schema get request."""

    credential_definition = fields.Nested(CredentialDefinitionSchema)


class CredentialDefinitionsCreatedResultSchema(OpenAPISchema):
    """Result schema for cred-defs-created request."""

    credential_definition_ids = fields.List(
        fields.Str(description="Credential definition identifiers", **INDY_CRED_DEF_ID)
    )


class CredDefIdMatchInfoSchema(OpenAPISchema):
    """Path parameters and validators for request taking cred def id."""

    cred_def_id = fields.Str(
        description="Credential definition identifier",
        required=True,
        **INDY_CRED_DEF_ID,
    )


class CreateCredDefTxnForEndorserOptionSchema(OpenAPISchema):
    """Class for user to input whether to create a transaction for endorser or not."""

    create_transaction_for_endorser = fields.Boolean(
        description="Create Transaction For Endorser's signature",
        required=False,
    )


class CredDefConnIdMatchInfoSchema(OpenAPISchema):
    """Path parameters and validators for request taking connection id."""

    conn_id = fields.Str(
        description="Connection identifier", required=False, example=UUIDFour.EXAMPLE
    )


@docs(
    tags=["credential-definition"],
    summary="Sends a credential definition to the ledger",
)
@request_schema(CredentialDefinitionSendRequestSchema())
@querystring_schema(CreateCredDefTxnForEndorserOptionSchema())
@querystring_schema(CredDefConnIdMatchInfoSchema())
@response_schema(TxnOrCredentialDefinitionSendResultSchema(), 200, description="")
async def credential_definitions_send_credential_definition(request: web.BaseRequest):
    """
    Request handler for sending a credential definition to the ledger.

    Args:
        request: aiohttp request object

    Returns:
        The credential definition identifier

    """
    context: AdminRequestContext = request["context"]
    outbound_handler = request["outbound_message_router"]

    create_transaction_for_endorser = json.loads(
        request.query.get("create_transaction_for_endorser", "false")
    )
    write_ledger = not create_transaction_for_endorser
    endorser_did = None
    connection_id = request.query.get("conn_id")

    body = await request.json()

    schema_id = body.get("schema_id")
    support_revocation = bool(body.get("support_revocation"))
    tag = body.get("tag")
    rev_reg_size = body.get("revocation_registry_size")

    # check if we need to endorse
    if context.settings.get_value("endorser.author"):
        # authors cannot write to the ledger
        write_ledger = False
        create_transaction_for_endorser = True
        if not connection_id:
            # author has not provided a connection id, so determine which to use
            endorser_alias = context.settings.get_value("endorser.endorser_alias")
            if not endorser_alias:
                raise web.HTTPBadRequest(reason="No endorser conenction specified")
            try:
                async with context.session() as session:
                    connection_records = await ConnRecord.retrieve_by_alias(
                        session, endorser_alias
                    )
                    connection_id = connection_records[0].connection_id
            except StorageNotFoundError as err:
                raise web.HTTPNotFound(reason=err.roll_up) from err
            except BaseModelError as err:
                raise web.HTTPBadRequest(reason=err.roll_up) from err
            except Exception as err:
                raise web.HTTPBadRequest(reason=err.roll_up) from err

    if not write_ledger:
        try:
            async with context.session() as session:
                connection_record = await ConnRecord.retrieve_by_id(
                    session, connection_id
                )
        except StorageNotFoundError as err:
            raise web.HTTPNotFound(reason=err.roll_up) from err
        except BaseModelError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

        session = await context.session()
        endorser_info = await connection_record.metadata_get(session, "endorser_info")
        if not endorser_info:
            raise web.HTTPForbidden(
                reason="Endorser Info is not set up in "
                "connection metadata for this connection record"
            )
        if "endorser_did" not in endorser_info.keys():
            raise web.HTTPForbidden(
                reason=' "endorser_did" is not set in "endorser_info"'
                " in connection metadata for this connection record"
            )
        endorser_did = endorser_info["endorser_did"]

    ledger = context.inject_or(BaseLedger)
    if not ledger:
        reason = "No ledger available"
        if not context.settings.get_value("wallet.type"):
            reason += ": missing wallet-type?"
        raise web.HTTPForbidden(reason=reason)

    issuer = context.inject(IndyIssuer)
    try:  # even if in wallet, send it and raise if erroneously so
        async with ledger:
            (cred_def_id, cred_def, novel) = await shield(
                ledger.create_and_send_credential_definition(
                    issuer,
                    schema_id,
                    signature_type=None,
                    tag=tag,
                    support_revocation=support_revocation,
                    write_ledger=write_ledger,
                    endorser_did=endorser_did,
                )
            )

    except (IndyIssuerError, LedgerError) as e:
        raise web.HTTPBadRequest(reason=e.message) from e

    meta_data = {
        "context": {
            "schema_id": schema_id,
            "support_revocation": support_revocation,
            "novel": novel,
            "tag": tag,
            "rev_reg_size": rev_reg_size,
        }
    }

    if not create_transaction_for_endorser:
        # Notify event
        issuer_did = cred_def_id.split(":")[0]
        meta_data["context"]["schema_id"] = schema_id
        meta_data["context"]["cred_def_id"] = cred_def_id
        meta_data["context"]["issuer_did"] = issuer_did
        meta_data["context"]["auto_create_rev_reg"] = True
        print(
            "Notify event:",
            CRED_DEF_EVENT_PREFIX + cred_def_id,
            meta_data,
        )
        await context.profile.notify(
            CRED_DEF_EVENT_PREFIX + cred_def_id,
            meta_data,
        )

    # If revocation is requested and cred def is novel, create revocation registry
    if support_revocation and novel and write_ledger:
        profile = context.profile
        tails_base_url = profile.settings.get("tails_server_base_url")
        if not tails_base_url:
            raise web.HTTPBadRequest(reason="tails_server_base_url not configured")
        try:
            # Create registry
            revoc = IndyRevocation(profile)
            registry_record = await revoc.init_issuer_registry(
                cred_def_id,
                max_cred_num=rev_reg_size,
            )
        except RevocationNotSupportedError as e:
            raise web.HTTPBadRequest(reason=e.message) from e

        await shield(registry_record.generate_registry(profile))
        try:
            await registry_record.set_tails_file_public_uri(
                profile, f"{tails_base_url}/{registry_record.revoc_reg_id}"
            )
            await registry_record.send_def(profile)
            await registry_record.send_entry(profile)

            # stage pending registry independent of whether tails server is OK
            pending_registry_record = await revoc.init_issuer_registry(
                registry_record.cred_def_id,
                max_cred_num=registry_record.max_cred_num,
            )
            ensure_future(
                pending_registry_record.stage_pending_registry(profile, max_attempts=16)
            )

            tails_server = profile.inject(BaseTailsServer)
            (upload_success, reason) = await tails_server.upload_tails_file(
                profile,
                registry_record.revoc_reg_id,
                registry_record.tails_local_path,
                interval=0.8,
                backoff=-0.5,
                max_attempts=5,  # heuristic: respect HTTP timeout
            )
            if not upload_success:
                raise web.HTTPInternalServerError(
                    reason=(
                        f"Tails file for rev reg {registry_record.revoc_reg_id} "
                        f"failed to upload: {reason}"
                    )
                )

        except RevocationError as e:
            raise web.HTTPBadRequest(reason=e.message) from e

    if not create_transaction_for_endorser:
        return web.json_response({"credential_definition_id": cred_def_id})

    else:
        session = await context.session()
        meta_data["context"][
            "auto_create_rev_reg"
        ] = session.context.settings.get_value("endorser.auto_create_rev_reg")

        transaction_mgr = TransactionManager(session)
        try:
            transaction = await transaction_mgr.create_record(
                messages_attach=cred_def["signed_txn"],
                connection_id=connection_id,
                meta_data=meta_data,
            )
        except StorageError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

        # if auto-request, send the request to the endorser
        if context.settings.get_value("endorser.auto_request"):
            try:
                transaction, transaction_request = await transaction_mgr.create_request(
                    transaction=transaction,
                    # TODO see if we need to parameterize these params
                    # expires_time=expires_time,
                    # endorser_write_txn=endorser_write_txn,
                )
            except (StorageError, TransactionManagerError) as err:
                raise web.HTTPBadRequest(reason=err.roll_up) from err

            await outbound_handler(transaction_request, connection_id=connection_id)

        return web.json_response({"txn": transaction.serialize()})


@docs(
    tags=["credential-definition"],
    summary="Search for matching credential definitions that agent originated",
)
@querystring_schema(CredDefQueryStringSchema())
@response_schema(CredentialDefinitionsCreatedResultSchema(), 200, description="")
async def credential_definitions_created(request: web.BaseRequest):
    """
    Request handler for retrieving credential definitions that current agent created.

    Args:
        request: aiohttp request object

    Returns:
        The identifiers of matching credential definitions.

    """
    context: AdminRequestContext = request["context"]

    session = await context.session()
    storage = session.inject(BaseStorage)
    found = await storage.find_all_records(
        type_filter=CRED_DEF_SENT_RECORD_TYPE,
        tag_query={
            tag: request.query[tag] for tag in CRED_DEF_TAGS if tag in request.query
        },
    )

    return web.json_response(
        {"credential_definition_ids": [record.value for record in found]}
    )


@docs(
    tags=["credential-definition"],
    summary="Gets a credential definition from the ledger",
)
@match_info_schema(CredDefIdMatchInfoSchema())
@response_schema(CredentialDefinitionGetResultSchema(), 200, description="")
async def credential_definitions_get_credential_definition(request: web.BaseRequest):
    """
    Request handler for getting a credential definition from the ledger.

    Args:
        request: aiohttp request object

    Returns:
        The credential definition details.

    """
    context: AdminRequestContext = request["context"]

    cred_def_id = request.match_info["cred_def_id"]

    ledger = context.inject_or(BaseLedger)
    if not ledger:
        reason = "No ledger available"
        if not context.settings.get_value("wallet.type"):
            reason += ": missing wallet-type?"
        raise web.HTTPForbidden(reason=reason)

    async with ledger:
        cred_def = await ledger.get_credential_definition(cred_def_id)

    return web.json_response({"credential_definition": cred_def})


@docs(
    tags=["credential-definition"],
    summary="Writes a credential definition non-secret record to the wallet",
)
@match_info_schema(CredDefIdMatchInfoSchema())
@response_schema(CredentialDefinitionGetResultSchema(), 200, description="")
async def credential_definitions_fix_cred_def_wallet_record(request: web.BaseRequest):
    """
    Request handler for fixing a credential definition wallet non-secret record.

    Args:
        request: aiohttp request object

    Returns:
        The credential definition details.

    """
    context: AdminRequestContext = request["context"]

    session = await context.session()
    storage = session.inject(BaseStorage)

    cred_def_id = request.match_info["cred_def_id"]

    ledger = context.inject(BaseLedger, required=False)
    if not ledger:
        reason = "No ledger available"
        if not context.settings.get_value("wallet.type"):
            reason += ": missing wallet-type?"
        raise web.HTTPForbidden(reason=reason)

    async with ledger:
        cred_def = await ledger.get_credential_definition(cred_def_id)
        cred_def_id_parts = cred_def_id.split(":")
        schema_seq_no = cred_def_id_parts[3]
        schema_response = await ledger.get_schema(schema_seq_no)
        schema_id = schema_response["id"]
        iss_did = cred_def_id_parts[0]

        # check if the record exists, if not add it
        found = await storage.find_all_records(
            type_filter=CRED_DEF_SENT_RECORD_TYPE,
            tag_query={
                "cred_def_id": cred_def_id,
            },
        )
        if 0 == len(found):
            await ledger.add_cred_def_non_secrets_record(
                session.profile, schema_id, iss_did, cred_def_id
            )

    return web.json_response({"credential_definition": cred_def})


def register_events(event_bus: EventBus):
    """Subscribe to any events we need to support."""
    event_bus.subscribe(EVENT_LISTENER_PATTERN, on_cred_def_event)


async def on_cred_def_event(profile: Profile, event: Event):
    """Handle any events we need to support."""
    print(f">>>> Handle event: {event}")
    schema_id = event.payload["context"]["schema_id"]
    cred_def_id = event.payload["context"]["cred_def_id"]
    issuer_did = event.payload["context"]["issuer_did"]
    if "cred_def" in event.payload:
        pass
    else:
        pass
    await add_cred_def_non_secrets_record(profile, schema_id, issuer_did, cred_def_id)

    # check if we need to kick off the revocation registry setup
    support_revocation = event.payload["context"]["support_revocation"]
    novel = event.payload["context"]["novel"]
    auto_create_rev_reg = event.payload["context"]["auto_create_rev_reg"]
    if support_revocation and novel and auto_create_rev_reg:
        print("TODO kick off revocation ...")
        event_id = REVOCATION_EVENT_PREFIX + REVOCATION_REG_EVENT + "::" + cred_def_id
        meta_data = event.payload
        print(
            "Notify event:",
            event_id,
            meta_data,
        )
        await profile.notify(
            event_id,
            meta_data,
        )


async def add_cred_def_non_secrets_record(
    profile: Profile, schema_id: str, issuer_did: str, credential_definition_id: str
):
    """
    Write the wallet non-secrets record for cred def (already written to the ledger).

    Note that the cred def private key signing informtion must already exist in the
    wallet.

    Args:
        schema_id: The schema id (or stringified sequence number)
        issuer_did: The DID of the issuer
        credential_definition_id: The credential definition id

    """
    schema_id_parts = schema_id.split(":")
    cred_def_tags = {
        "schema_id": schema_id,
        "schema_issuer_did": schema_id_parts[0],
        "schema_name": schema_id_parts[-2],
        "schema_version": schema_id_parts[-1],
        "issuer_did": issuer_did,
        "cred_def_id": credential_definition_id,
        "epoch": str(int(time())),
    }
    record = StorageRecord(
        CRED_DEF_SENT_RECORD_TYPE, credential_definition_id, cred_def_tags
    )
    async with profile.session() as session:
        storage = session.inject(BaseStorage)
        await storage.add_record(record)


async def register(app: web.Application):
    """Register routes."""
    app.add_routes(
        [
            web.post(
                "/credential-definitions",
                credential_definitions_send_credential_definition,
            ),
            web.get(
                "/credential-definitions/created",
                credential_definitions_created,
                allow_head=False,
            ),
            web.get(
                "/credential-definitions/{cred_def_id}",
                credential_definitions_get_credential_definition,
                allow_head=False,
            ),
            web.post(
                "/credential-definitions/{cred_def_id}/write_record",
                credential_definitions_fix_cred_def_wallet_record,
            ),
        ]
    )


def post_process_routes(app: web.Application):
    """Amend swagger API."""

    # Add top-level tags description
    if "tags" not in app._state["swagger_dict"]:
        app._state["swagger_dict"]["tags"] = []
    app._state["swagger_dict"]["tags"].append(
        {
            "name": "credential-definition",
            "description": "Credential definition operations",
            "externalDocs": {
                "description": "Specification",
                "url": (
                    "https://github.com/hyperledger/indy-node/blob/master/"
                    "design/anoncreds.md#cred_def"
                ),
            },
        }
    )
