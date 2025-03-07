import asyncio
import json
import logging
import os
import shutil
import string
import tempfile
import uuid
import zipfile
from asyncio import Future, Queue
from contextlib import asynccontextmanager
from datetime import timedelta, datetime, timezone
from functools import partial
from io import BytesIO
from pathlib import Path
from random import SystemRandom
from typing import (
    AsyncGenerator,
    Any,
    Optional,
    Sequence,
    Union,
    List,
    Dict,
    AsyncIterator,
    Tuple,
    Callable,
    Awaitable,
    Iterable,
)
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import aiohttp_jinja2
import jinja2
import prometheus_client
import yaml
from aiohttp import (
    web,
    MultipartWriter,
    AsyncIterablePayload,
    BufferedReaderPayload,
    MultipartReader,
    ClientSession,
    TCPConnector,
)
from aiohttp.abc import AbstractStreamWriter
from aiohttp.hdrs import METH_ANY
from aiohttp.web import Request, StreamResponse, WebSocketResponse
from aiohttp.web_exceptions import HTTPNotFound, HTTPNoContent, HTTPOk, HTTPNotAcceptable, HTTPSeeOther
from aiohttp.web_fileresponse import FileResponse
from aiohttp.web_response import json_response
from aiohttp_swagger3 import SwaggerFile, SwaggerUiSettings
from aiostream import stream
from attrs import evolve
from dateutil import parser as date_parser
from networkx.readwrite import cytoscape_data
from resotoui import ui_path

from resotocore.analytics import AnalyticsEvent
from resotocore.cli.command import alias_names
from resotocore.dependencies import Dependencies
from resotocore.cli.model import (
    ParsedCommandLine,
    CLIContext,
    CLICommand,
    InternalPart,
    WorkerCustomCommand,
    AliasTemplate,
    InfraAppAlias,
    FilePath,
)
from resotocore.config import ConfigValidation, ConfigEntity
from resotocore.console_renderer import ConsoleColorSystem, ConsoleRenderer
from resotocore.db.graphdb import GraphDB, HistoryChange
from resotocore.db.model import QueryModel
from resotocore.error import NotFoundError
from resotocore.ids import TaskId, ConfigId, NodeId, SubscriberId, WorkerId, GraphName, Email, Password
from resotocore.message_bus import Message, ActionDone, Action, ActionError, ActionInfo, ActionProgress
from resotocore.metrics import timed
from resotocore.model.graph_access import Section
from resotocore.model.json_schema import json_schema
from resotocore.model.model import Kind, Model
from resotocore.model.typed_model import to_json, from_js, to_js_str, to_js
from resotocore.service import Service
from resotocore.task.model import Subscription
from resotocore.types import Json, JsonElement
from resotocore.util import uuid_str, force_gen, rnd_str, if_set, duration, utc_str, parse_utc, async_noop, utc
from resotocore.web.auth import raw_jwt_from_auth_message, AuthorizedUser, AuthHandler
from resotocore.web.content_renderer import result_binary_gen, single_result
from resotocore.web.directives import (
    metrics_handler,
    error_handler,
    on_response_prepare,
    cors_handler,
    enable_compression,
    default_middleware,
)
from resotocore.web.tsdb import tsdb
from resotocore.worker_task_queue import (
    WorkerTaskDescription,
    WorkerTask,
    WorkerTaskResult,
    WorkerTaskInProgress,
)
from resotolib.asynchronous.web.ws_handler import accept_websocket, clean_ws_handler
from resotolib.jwt import encode_jwt
from resotolib.x509 import cert_to_bytes

log = logging.getLogger(__name__)


def section_of(request: Request) -> Optional[str]:
    section = request.match_info.get("section", request.query.get("section"))
    if section and section != "/" and section not in Section.content:
        raise AttributeError(f"Given section does not exist: {section}")
    return section


# No Authorization required for following paths
AlwaysAllowed = {
    "/",
    "/.well-known/.*",
    "/api-doc.*",
    "/authenticate",
    "/ca/cert",
    "/create-first-user",
    "/login",
    "/metrics",
    "/static/.*",
    "/system/.*",
    "/ui.*",
    "/notebook/.*",
    "/debug/.*",
}
# Authorization is not required, but implemented as part of the request handler
DeferredCheck = {"/events", "/work/queue"}


class Api(Service):
    def __init__(self, deps: Dependencies):
        self.deps = deps
        self.auth_handler = AuthHandler(
            deps.db_access.system_data_db, deps.config, deps.cert_handler, AlwaysAllowed | DeferredCheck
        )

        self.app = web.Application(
            client_max_size=self.deps.config.api.max_request_size or 1024**2,
            # note on order: the middleware is passed in the order provided.
            middlewares=[
                metrics_handler,
                self.auth_handler.middleware(),
                cors_handler,
                error_handler(deps.config, deps.event_sender),
                default_middleware(self),
            ],
        )
        self.app.on_response_prepare.append(on_response_prepare)
        self._session: Optional[ClientSession] = None
        self.in_shutdown = False
        self.websocket_handler: Dict[str, Tuple[Future[Any], WebSocketResponse]] = {}
        path_part = deps.config.api.web_path.strip().strip("/").strip()
        web_path = "" if path_part == "" else f"/{path_part}"
        self.__add_routes(web_path)
        aiohttp_jinja2.setup(
            self.app, loader=jinja2.FileSystemLoader(os.path.abspath(os.path.dirname(__file__) + "/../templates"))
        )

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            # only keep connections alive for 15 seconds, cleanup closed transports
            connector = TCPConnector(keepalive_timeout=15.0, enable_cleanup_closed=True)
            self._session = ClientSession(connector=connector)
        return self._session

    def __add_routes(self, prefix: str) -> None:
        static_path = os.path.abspath(os.path.dirname(__file__) + "/../static")
        jupyterlite_path = Path(os.path.abspath(os.path.dirname(__file__) + "/../jupyterlite"))
        if not jupyterlite_path.exists():
            jupyterlite_path.mkdir(parents=True, exist_ok=True)
        self.app.add_routes(
            [
                # Model operations (backwards compatible)
                web.get(prefix + "/model", self.get_model),
                web.patch(prefix + "/model", self.update_model),
                web.get(prefix + "/model/uml", self.model_uml),
                # Graph based model operations
                web.get(prefix + "/graph/{graph_id}/model", self.get_model),
                web.patch(prefix + "/graph/{graph_id}/model", self.update_model),
                web.get(prefix + "/graph/{graph_id}/model/uml", self.model_uml),
                # CRUD Graph operations
                web.get(prefix + "/graph", self.list_graphs),
                web.get(prefix + "/graph/{graph_id}", self.get_node),
                web.post(prefix + "/graph/{graph_id}", self.create_graph),
                web.delete(prefix + "/graph/{graph_id}", self.wipe),
                # search the graph
                web.post(prefix + "/graph/{graph_id}/search/raw", self.raw),
                web.post(prefix + "/graph/{graph_id}/search/structure", self.query_structure),
                web.post(prefix + "/graph/{graph_id}/search/explain", self.explain),
                web.post(prefix + "/graph/{graph_id}/search/list", self.query_list),
                web.post(prefix + "/graph/{graph_id}/search/graph", self.query_graph_stream),
                web.post(prefix + "/graph/{graph_id}/search/aggregate", self.query_aggregation),
                web.post(prefix + "/graph/{graph_id}/search/history/list", self.query_history),
                web.post(prefix + "/graph/{graph_id}/search/history/aggregate", self.query_history),
                # maintain the graph
                web.patch(prefix + "/graph/{graph_id}/nodes", self.update_nodes),
                web.post(prefix + "/graph/{graph_id}/merge", self.merge_graph),
                web.post(prefix + "/graph/{graph_id}/batch/merge", self.update_merge_graph_batch),
                web.get(prefix + "/graph/{graph_id}/batch", self.list_batches),
                web.post(prefix + "/graph/{graph_id}/batch/{batch_id}", self.commit_batch),
                web.delete(prefix + "/graph/{graph_id}/batch/{batch_id}", self.abort_batch),
                # node specific actions
                web.post(prefix + "/graph/{graph_id}/node/{node_id}/under/{parent_node_id}", self.create_node),
                web.get(prefix + "/graph/{graph_id}/node/{node_id}", self.get_node),
                web.patch(prefix + "/graph/{graph_id}/node/{node_id}", self.update_node),
                web.delete(prefix + "/graph/{graph_id}/node/{node_id}", self.delete_node),
                web.patch(prefix + "/graph/{graph_id}/node/{node_id}/section/{section}", self.update_node),
                # Subscriptions
                web.get(prefix + "/subscribers", self.list_all_subscriptions),
                web.get(prefix + "/subscribers/for/{event_type}", self.list_subscription_for_event),
                # Subscription
                web.get(prefix + "/subscriber/{subscriber_id}", self.get_subscriber),
                web.put(prefix + "/subscriber/{subscriber_id}", self.update_subscriber),
                web.delete(prefix + "/subscriber/{subscriber_id}", self.delete_subscriber),
                web.post(prefix + "/subscriber/{subscriber_id}/{event_type}", self.add_subscription),
                web.delete(prefix + "/subscriber/{subscriber_id}/{event_type}", self.delete_subscription),
                web.get(prefix + "/subscriber/{subscriber_id}/handle", self.handle_subscribed),
                # report checks
                web.get(prefix + "/report/checks", self.inspection_checks),
                web.get(prefix + "/report/checks/graph/{graph_id}", self.perform_benchmark_on_checks),
                web.get(prefix + "/report/check/{check_id}/graph/{graph_id}", self.inspection_results),
                web.get(prefix + "/report/benchmark/{benchmark}/graph/{graph_id}", self.perform_benchmark),
                # CLI
                web.post(prefix + "/cli/evaluate", self.evaluate),
                web.post(prefix + "/cli/execute", self.execute),
                web.get(prefix + "/cli/info", self.cli_info),
                # Event operations
                web.get(prefix + "/events", self.handle_events),
                web.post(prefix + "/analytics", self.send_analytics_events),
                # Worker operations
                web.get(prefix + "/work/queue", self.handle_work_tasks),
                web.get(prefix + "/work/list", self.list_work),
                # Serve static filed
                web.get(prefix, self.forward("/ui/index.html")),
                web.static(prefix + "/static", static_path),
                web.get(prefix + "/notebook", self.forward("/notebook/index.html")),
                web.static(prefix + "/notebook", jupyterlite_path),
                # metrics
                web.get(prefix + "/metrics", self.metrics),
                # config operations
                web.get(prefix + "/configs", self.list_configs),
                web.patch(prefix + "/config/{config_id:[^{}]+}", self.patch_config),
                web.delete(prefix + "/config/{config_id:[^{}]+}", self.delete_config),
                # config model operations
                web.get(prefix + "/configs/validation", self.list_config_models),
                web.get(prefix + "/configs/model", self.get_configs_model),
                web.patch(prefix + "/configs/model", self.update_configs_model),
                web.put(prefix + "/config_validation/{config_id:[^{}]+}", self.put_config_validation),
                web.get(prefix + "/config_validation/{config_id:[^{}]+}", self.get_config_validation),
                web.put(prefix + "/config/{config_id:[^{}]+}/validation", self.put_config_validation),
                web.get(prefix + "/config/{config_id:[^{}]+}/validation", self.get_config_validation),
                # config operations, moved here to avoid early matching
                web.put(prefix + "/config/{config_id:[^{}]+}", self.put_config),
                web.get(prefix + "/config/{config_id:[^{}]+}", self.get_config),
                # ca operations
                web.get(prefix + "/ca/cert", self.certificate),
                web.post(prefix + "/ca/sign", self.sign_certificate),
                # system operations
                web.get(prefix + "/system/ping", self.ping),
                web.get(prefix + "/system/ready", self.ready),
                # forwards
                web.get(prefix + "/tsdb", self.forward("/tsdb/")),
                web.get(prefix + "/ui", self.forward("/ui/index.html")),
                web.get(prefix + "/ui/", self.forward("/ui/index.html")),
                # auth operations
                web.get(prefix + "/.well-known/jwks.json", self.jwks),
                web.get(prefix + "/login", self.login_page),
                web.post(prefix + "/create-first-user", self.create_first_user),
                web.post(prefix + "/authenticate", self.authenticate),
                web.get(prefix + "/authorization/user", self.get_authorized_user),
                web.get(prefix + "/authorization/renew", self.renew_authorization),
                # tsdb operations
                web.route(METH_ANY, prefix + "/tsdb/{tail:.+}", tsdb(self)),
                web.static(f"{prefix}/ui/", ui_path),
            ]
        )
        if self.deps.config.runtime.debug:
            self.app.add_routes([web.get(prefix + "/debug/ui/{commit}/{path:.+}", self.serve_debug_ui)])
        SwaggerFile(
            self.app,
            spec_file=f"{static_path}/api-doc.yaml",
            swagger_ui_settings=SwaggerUiSettings(path=prefix + "/api-doc", layout="BaseLayout", docExpansion="none"),
        )

    async def start(self) -> None:
        await self.auth_handler.start()

    async def stop(self) -> None:
        await self.auth_handler.stop()
        if not self.in_shutdown:
            self.in_shutdown = True
            for ws_id in list(self.websocket_handler):
                await clean_ws_handler(ws_id, self.websocket_handler)
            if self.session:
                await self.session.close()

    @staticmethod
    async def login_with_redirect(request: Request) -> StreamResponse:
        params = request.query.copy()
        params["redirect"] = request.raw_path
        return web.HTTPSeeOther("/login?" + urlencode(params))

    @staticmethod
    def forward(to: str) -> Callable[[Request], Awaitable[StreamResponse]]:
        async def forward_to(request: Request) -> StreamResponse:
            goto = to + "?" + urlencode(request.query) if request.query else to
            return web.HTTPFound(goto)

        return forward_to

    @staticmethod
    async def ping(_: Request) -> StreamResponse:
        return web.HTTPOk(text="pong", content_type="text/plain")

    @staticmethod
    async def ready(_: Request) -> StreamResponse:
        return web.HTTPOk(text="ok")

    async def jwks(self, _: Request) -> StreamResponse:
        return web.json_response(self.auth_handler.signing_key_jwk)

    async def login_page(self, request: Request) -> StreamResponse:
        template = "login.html" if await self.deps.user_management.has_users() else "create_first_user.html"
        return aiohttp_jinja2.render_template(template, request, context=request.query)

    async def create_first_user(self, request: Request) -> StreamResponse:
        post_data = await request.post()
        errors = []
        try:
            email = Email(str(post_data.get("email", "")).strip())
            password = Password(str(post_data.get("password", "")))
            password_repeat = str(post_data.get("password_repeat", ""))
            company = str(post_data.get("company", "")).strip()
            fullname = str(post_data.get("fullname", "")).strip()
            if not email or email.startswith("@") or email.endswith("@") or email.count("@") != 1:
                errors.append("Invalid email address")
            if not password:
                errors.append("Password is required")
            if not company:
                errors.append("Company name is required")
            if not fullname:
                errors.append("Full name is required")
            if password != password_repeat:
                errors.append("Passwords do not match")
            if not errors:
                await self.deps.user_management.create_first_user(company, fullname, email, password)
                return await self.authenticate(request)
        except Exception as e:
            errors.append(str(e))
        error_string = ". ".join(errors)
        return aiohttp_jinja2.render_template(
            "create_first_user.html", request, context={**post_data, "error": error_string}
        )

    async def authenticate(self, request: Request) -> StreamResponse:
        post_data = await request.post()
        email = Email(str(post_data.get("email", "")))
        password = Password(str(post_data.get("password", "")))
        redirect = str(post_data.get("redirect", ""))
        if email and password and (user := await self.deps.user_management.login(email, password)):
            params: Dict[str, List[str]] = {}
            if self.deps.config.args.psk:
                code = await self.auth_handler.add_authorized_user(AuthorizedUser(email, user.roles, utc()))
                params["code"] = [code]
            if redirect:
                if params:
                    parsed = urlparse(redirect)
                    query_params = parse_qs(parsed.query)
                    query_params.update(params)
                    parsed = parsed._replace(query=urlencode(query_params, doseq=True))
                    redirect = urlunparse(parsed)
                response: StreamResponse = HTTPSeeOther(redirect)
            else:
                response = HTTPOk(text=urlencode(params, doseq=True))
            return response
        return aiohttp_jinja2.render_template(
            "login.html", request, context=dict(**post_data, error="Invalid username or password")
        )

    @staticmethod
    async def get_authorized_user(request: Request) -> StreamResponse:
        if jwt := request.get("jwt"):
            return web.json_response(jwt)
        else:
            return web.HTTPNoContent()

    async def renew_authorization(self, request: Request) -> StreamResponse:
        if jwt_raw := request.get("jwt"):
            exp = datetime.fromtimestamp(int(jwt_raw["exp"]), tz=timezone.utc)
            user = AuthorizedUser(jwt_raw["email"], set(jwt_raw["roles"].split(",")), exp)
            renewed, data = self.auth_handler.user_jwt(user)
            return web.json_response(data, headers={"Authorization": f"Bearer {renewed}"})
        else:
            return HTTPNoContent()  # no psk, no renewal

    async def list_configs(self, request: Request) -> StreamResponse:
        return await self.stream_response_from_gen(request, self.deps.config_handler.list_config_ids())

    async def get_config(self, request: Request) -> StreamResponse:
        config_id = ConfigId(request.match_info["config_id"])
        accept = request.headers.get("accept", "application/json")
        not_found = HTTPNotFound(text="No config with this id")
        if accept == "application/yaml":
            yml = await self.deps.config_handler.config_yaml(config_id)
            return web.Response(body=yml.encode("utf-8"), content_type="application/yaml") if yml else not_found
        else:

            def get_query_param_bool(name: str, default: bool) -> bool:
                return request.query.get(name, "true" if default else "false").lower() == "true"

            # do we want the config with overrides/env_vars applied in-place or in a separate object?
            separate_overrides = get_query_param_bool("separate_overrides", default=False)

            if separate_overrides:
                # if we want separate overrides, we don't apply overrides to the existing config
                # and don't substitute the env vars by default. E.g. UI asks us for the config
                apply_overrides = get_query_param_bool("apply_overrides", default=False)
                resolve_env_vars = get_query_param_bool("resolve_env_vars", default=False)
                # attach the "raw" config version that was stored in the database
                include_raw_config = get_query_param_bool("include_raw_config", default=False)
            else:
                # if we request a single object with overrides applied,
                # we apply overrides and resolve env vars by default
                apply_overrides = get_query_param_bool("apply_overrides", default=True)
                resolve_env_vars = get_query_param_bool("resolve_env_vars", default=True)
                # ignored in case of a single config object requested
                include_raw_config = False

            config = await self.deps.config_handler.get_config(config_id, apply_overrides, resolve_env_vars)
            if config:
                headers = {"Resoto-Config-Revision": config.revision}
                if separate_overrides:
                    payload = {"config": config.config, "overrides": self.deps.config_override.get_override(config_id)}
                    if include_raw_config:
                        raw_config = await self.deps.config_handler.get_config(
                            config_id, apply_overrides=False, resolve_env_vars=False
                        )
                        payload["raw_config"] = raw_config.config if raw_config else None
                else:
                    payload = config.config

                return await single_result(request, payload, headers)
            else:
                return not_found

    async def put_config(self, request: Request) -> StreamResponse:
        config_id = ConfigId(request.match_info["config_id"])
        validate = request.query.get("validate", "true").lower() != "false"
        dry_run = request.query.get("dry_run", "false").lower() == "true"
        config = await self.json_from_request(request)
        result = await self.deps.config_handler.put_config(
            ConfigEntity(config_id, config), validate=validate, dry_run=dry_run
        )
        headers = {"Resoto-Config-Revision": result.revision}
        return await single_result(request, result.config, headers)

    async def patch_config(self, request: Request) -> StreamResponse:
        config_id = ConfigId(request.match_info["config_id"])
        validate = request.query.get("validate", "true").lower() != "false"
        dry_run = request.query.get("dry_run", "false").lower() == "true"
        patch = await self.json_from_request(request)
        updated = await self.deps.config_handler.patch_config(
            ConfigEntity(config_id, patch), validate=validate, dry_run=dry_run
        )
        headers = {"Resoto-Config-Revision": updated.revision}
        return await single_result(request, updated.config, headers)

    async def delete_config(self, request: Request) -> StreamResponse:
        config_id = ConfigId(request.match_info["config_id"])
        await self.deps.config_handler.delete_config(config_id)
        return HTTPNoContent()

    async def list_config_models(self, request: Request) -> StreamResponse:
        return await self.stream_response_from_gen(request, self.deps.config_handler.list_config_validation_ids())

    async def get_config_validation(self, request: Request) -> StreamResponse:
        config_id = request.match_info["config_id"]
        model = await self.deps.config_handler.get_config_validation(config_id)
        return await single_result(request, to_js(model)) if model else HTTPNotFound(text="No model for this config.")

    async def get_configs_model(self, request: Request) -> StreamResponse:
        model = await self.deps.config_handler.get_configs_model()
        if request.query.get("flat", "false") == "true":
            model = Model.from_kinds(model.flat_kinds())
        return await single_result(request, to_js(model, strip_nulls=True))

    async def update_configs_model(self, request: Request) -> StreamResponse:
        js = await self.json_from_request(request)
        kinds: List[Kind] = from_js(js, List[Kind])
        model = await self.deps.config_handler.update_configs_model(kinds)
        return await single_result(request, to_js(model))

    async def put_config_validation(self, request: Request) -> StreamResponse:
        config_id = request.match_info["config_id"]
        js = await self.json_from_request(request)
        js["id"] = config_id
        config_model = from_js(js, ConfigValidation)
        model = await self.deps.config_handler.put_config_validation(config_model)
        return await single_result(request, to_js(model))

    async def certificate(self, _: Request) -> StreamResponse:
        cert, fingerprint = self.deps.cert_handler.authority_certificate
        headers = {
            "SHA256-Fingerprint": fingerprint,
            "Content-Disposition": 'attachment; filename="resoto_root_ca.pem"',
        }
        if self.deps.config.args.psk:
            headers["Authorization"] = "Bearer " + encode_jwt(
                {"sha256_fingerprint": fingerprint}, self.deps.config.args.psk
            )
        return HTTPOk(headers=headers, body=cert, content_type="application/x-pem-file")

    async def sign_certificate(self, request: Request) -> StreamResponse:
        csr_bytes = await request.content.read()
        cert, fingerprint = self.deps.cert_handler.sign(csr_bytes)
        headers = {"SHA256-Fingerprint": fingerprint}
        return HTTPOk(headers=headers, body=cert_to_bytes(cert), content_type="application/x-pem-file")

    @staticmethod
    async def metrics(_: Request) -> StreamResponse:
        resp = web.Response(body=prometheus_client.generate_latest())
        resp.content_type = prometheus_client.CONTENT_TYPE_LATEST
        return resp

    async def list_all_subscriptions(self, request: Request) -> StreamResponse:
        subscribers = await self.deps.subscription_handler.all_subscribers()
        return await single_result(request, to_json(subscribers))

    async def get_subscriber(self, request: Request) -> StreamResponse:
        subscriber_id = SubscriberId(request.match_info["subscriber_id"])
        subscriber = await self.deps.subscription_handler.get_subscriber(subscriber_id)
        return self.optional_json(subscriber, f"No subscriber with id {subscriber_id}")

    async def list_subscription_for_event(self, request: Request) -> StreamResponse:
        event_type = request.match_info["event_type"]
        subscribers = await self.deps.subscription_handler.list_subscriber_for(event_type)
        return await single_result(request, to_json(subscribers))

    async def update_subscriber(self, request: Request) -> StreamResponse:
        subscriber_id = SubscriberId(request.match_info["subscriber_id"])
        body = await self.json_from_request(request)
        subscriptions = from_js(body, List[Subscription])
        sub = await self.deps.subscription_handler.update_subscriptions(subscriber_id, subscriptions)
        return await single_result(request, to_json(sub))

    async def delete_subscriber(self, request: Request) -> StreamResponse:
        subscriber_id = SubscriberId(request.match_info["subscriber_id"])
        await self.deps.subscription_handler.remove_subscriber(subscriber_id)
        return web.HTTPNoContent()

    async def add_subscription(self, request: Request) -> StreamResponse:
        subscriber_id = SubscriberId(request.match_info["subscriber_id"])
        event_type = request.match_info["event_type"]
        timeout = timedelta(seconds=int(request.query.get("timeout", "60")))
        wait_for_completion = request.query.get("wait_for_completion", "true").lower() != "false"
        sub = await self.deps.subscription_handler.add_subscription(
            subscriber_id, event_type, wait_for_completion, timeout
        )
        return await single_result(request, to_js(sub))

    async def delete_subscription(self, request: Request) -> StreamResponse:
        subscriber_id = SubscriberId(request.match_info["subscriber_id"])
        event_type = request.match_info["event_type"]
        sub = await self.deps.subscription_handler.remove_subscription(subscriber_id, event_type)
        return await single_result(request, to_js(sub))

    async def handle_subscribed(self, request: Request) -> StreamResponse:
        subscriber_id = SubscriberId(request.match_info["subscriber_id"])
        subscriber = await self.deps.subscription_handler.get_subscriber(subscriber_id)
        if subscriber_id in self.deps.message_bus.active_listener:
            log.info(f"There is already a listener for subscriber: {subscriber_id}. Reject.")
            return web.HTTPTooManyRequests(text="Only one connection per subscriber is allowed!")
        elif subscriber and subscriber.subscriptions:
            pending = await self.deps.task_handler.list_all_pending_actions_for(subscriber)
            return await self.listen_to_events(request, subscriber_id, list(subscriber.subscriptions.keys()), pending)
        else:
            return web.HTTPNotFound(text=f"No subscriber with this id: {subscriber_id} or no subscriptions")

    async def perform_benchmark_on_checks(self, request: Request) -> StreamResponse:
        graph = GraphName(request.match_info["graph_id"])
        provider = request.query.get("provider")
        service = request.query.get("service")
        category = request.query.get("category")
        kind = request.query.get("kind")
        acc = request.query.get("accounts")
        accounts = [a.strip() for a in acc.split(",")] if acc else None
        result = await self.deps.inspector.perform_checks(
            graph, provider=provider, service=service, category=category, kind=kind, accounts=accounts
        )
        return await single_result(request, to_js(result))

    async def perform_benchmark(self, request: Request) -> StreamResponse:
        benchmark = request.match_info["benchmark"]
        graph = GraphName(request.match_info["graph_id"])
        acc = request.query.get("accounts")
        accounts = [a.strip() for a in acc.split(",")] if acc else None
        result = await self.deps.inspector.perform_benchmark(graph, benchmark, accounts=accounts)
        result_graph = result.to_graph()
        async with stream.iterate(result_graph).stream() as streamer:
            await self.stream_response_from_gen(request, streamer, len(result_graph))
        return await single_result(request, to_js(result))

    async def inspection_checks(self, request: Request) -> StreamResponse:
        provider = request.query.get("provider")
        service = request.query.get("service")
        category = request.query.get("category")
        kind = request.query.get("kind")
        inspections = await self.deps.inspector.list_checks(
            provider=provider, service=service, category=category, kind=kind
        )
        return await single_result(request, to_js(inspections))

    async def inspection_results(self, request: Request) -> StreamResponse:
        graph = GraphName(request.match_info["graph_id"])
        check_id = request.match_info["check_id"]
        acc = request.query.get("accounts")
        accounts = [a.strip() for a in acc.split(",")] if acc else None
        inspections = await self.deps.inspector.list_failing_resources(graph, check_id, accounts)
        return await self.stream_response_from_gen(request, inspections)

    async def handle_events(self, request: Request) -> StreamResponse:
        show = request.query["show"].split(",") if "show" in request.query else ["*"]
        return await self.listen_to_events(request, SubscriberId(str(uuid.uuid1())), show)

    async def send_analytics_events(self, request: Request) -> StreamResponse:
        events_json = await self.json_from_request(request)
        events = from_js(events_json, List[AnalyticsEvent])
        await self.deps.event_sender.capture(events)
        return web.HTTPNoContent()

    async def listen_to_events(
        self,
        request: Request,
        listener_id: SubscriberId,
        event_types: List[str],
        initial_messages: Optional[Sequence[Message]] = None,
    ) -> WebSocketResponse:
        handler: Callable[[str], Awaitable[None]] = async_noop

        async def authorize_request(msg: str) -> None:
            nonlocal handler
            if (r := raw_jwt_from_auth_message(msg)) and await self.auth_handler.validate_jwt(r, request) is not None:
                handler = handle_message
            else:
                raise ValueError("No Authorization header provided and no valid auth message sent")

        async def handle_message(msg: str) -> None:
            js = json.loads(msg)
            if "data" in js:
                js["data"]["subscriber_id"] = listener_id
                js["data"]["received_at"] = utc_str()
            message: Message = from_js(js, Message)
            if isinstance(message, Action):
                raise AttributeError("Actors should not emit action messages. ")
            elif isinstance(message, ActionInfo):
                await self.deps.task_handler.handle_action_info(message)
            elif isinstance(message, ActionProgress):
                await self.deps.task_handler.handle_action_progress(message)
            elif isinstance(message, ActionDone):
                await self.deps.task_handler.handle_action_done(message)
            elif isinstance(message, ActionError):
                await self.deps.task_handler.handle_action_error(message)
            else:
                await self.deps.message_bus.emit(message)

        handler = authorize_request if request.get("authorized", False) is False else handle_message
        return await accept_websocket(
            request,
            handle_incoming=lambda x: handler(x),  # pylint: disable=unnecessary-lambda # it is required!
            outgoing_context=partial(self.deps.message_bus.subscribe, listener_id, event_types),
            websocket_handler=self.websocket_handler,
            initial_messages=initial_messages,
        )

    async def handle_work_tasks(self, request: Request) -> WebSocketResponse:
        worker_id = WorkerId(uuid_str())
        worker_descriptions: Future[List[WorkerTaskDescription]] = asyncio.get_event_loop().create_future()
        handler: Callable[[str], Awaitable[None]] = async_noop

        async def authorize_request(msg: str) -> None:
            nonlocal handler
            if (r := raw_jwt_from_auth_message(msg)) and await self.auth_handler.validate_jwt(r, request) is not None:
                handler = handle_connect
            else:
                raise ValueError("No Authorization header provided and no valid auth message sent")

        async def handle_connect(msg: str) -> None:
            nonlocal handler
            cmds = from_js(json.loads(msg), List[WorkerCustomCommand])
            description = [WorkerTaskDescription(cmd.name, cmd.filter) for cmd in cmds]
            # set the future and allow attaching the worker to the task queue
            worker_descriptions.set_result(description)
            # register the descriptions as custom command on the CLI
            for cmd in cmds:
                self.deps.cli.register_alias_template(cmd.to_template())
            # the connect process is done, define the final handler
            handler = handle_message

        async def handle_message(msg: str) -> None:
            tr = from_js(json.loads(msg), WorkerTaskResult)
            if tr.result == "error":
                error = tr.error if tr.error else "worker signalled error without detailed error message"
                await self.deps.worker_task_queue.error_task(worker_id, tr.task_id, error)
            elif tr.result == "done":
                await self.deps.worker_task_queue.acknowledge_task(worker_id, tr.task_id, tr.data)
            else:
                log.info(f"Do not understand this message: {msg}")

        def task_json(task: WorkerTask) -> str:
            return to_js_str(task.to_json())

        @asynccontextmanager
        async def connect_to_task_queue() -> AsyncIterator[Queue[WorkerTask]]:
            # we need to wait for the worker to send the list of commands it can handle
            # before we can attach to the worker task queue
            descriptions = await worker_descriptions
            async with self.deps.worker_task_queue.attach(worker_id, descriptions) as queue:
                yield queue

        handler = authorize_request if request.get("authorized", False) is False else handle_connect
        # noinspection PyTypeChecker
        return await accept_websocket(
            request,
            handle_incoming=lambda x: handler(x),  # pylint: disable=unnecessary-lambda # it is required!
            outgoing_context=connect_to_task_queue,
            websocket_handler=self.websocket_handler,
            outgoing_fn=task_json,
        )

    async def list_work(self, _: Request) -> StreamResponse:
        def wt_to_js(ip: WorkerTaskInProgress) -> Json:
            return {
                "task": ip.task.to_json(),
                "worker": ip.worker.worker_id,
                "retry_counter": ip.retry_counter,
                "deadline": to_json(ip.deadline),
            }

        return web.json_response([wt_to_js(ot) for ot in self.deps.worker_task_queue.outstanding_tasks.values()])

    async def model_uml(self, request: Request) -> StreamResponse:
        output = request.query.get("output", "svg")
        graph_id = GraphName(request.match_info.get("graph_id", "resoto"))
        show = request.query["show"].split(",") if "show" in request.query else None
        hide = request.query["hide"].split(",") if "hide" in request.query else None
        with_inheritance = request.query.get("with_inheritance", "true") != "false"
        with_base_classes = request.query.get("with_base_classes", "true") != "false"
        with_subclasses = request.query.get("with_subclasses", "false") != "false"
        dependency = set(request.query["dependency"].split(",")) if "dependency" in request.query else None
        with_predecessors = request.query.get("with_predecessors", "false") != "false"
        with_successors = request.query.get("with_successors", "false") != "false"
        with_properties = request.query.get("with_properties", "true") != "false"
        aggregate_roots = request.query.get("aggregate_roots", "true") != "false"
        link_classes = request.query.get("link_classes", "false") != "false"
        sort_props = request.query.get("sort_props", "true") != "false"
        result = await self.deps.model_handler.uml_image(
            graph_name=graph_id,
            output=output,
            show_packages=show,
            hide_packages=hide,
            with_inheritance=with_inheritance,
            with_base_classes=with_base_classes,
            with_subclasses=with_subclasses,
            dependency_edges=dependency,  # type: ignore
            with_predecessors=with_predecessors,
            with_successors=with_successors,
            with_properties=with_properties,
            link_classes=link_classes,
            only_aggregate_roots=aggregate_roots,
            sort_props=sort_props,
        )
        response = web.StreamResponse()
        mt = {"svg": "image/svg+xml", "png": "image/png", "puml": "text/plain"}
        response.headers["Content-Type"] = mt[output]
        await response.prepare(request)
        await response.write_eof(result)
        return response

    async def get_model(self, request: Request) -> StreamResponse:
        graph_id = GraphName(request.match_info.get("graph_id", "resoto"))
        md = await self.deps.model_handler.load_model(graph_id)
        # default to internal model format, but allow to request json schema format
        if request.headers.get("accept") == "application/schema+json":
            return json_response(json_schema(md), content_type="application/schema+json")
        kinds: Iterable[Kind]
        if request.query.get("flat", "false") == "true":
            kinds = md.flat_kinds()
        else:
            kinds = md.kinds.values()
        return await single_result(request, to_js(kinds, strip_nulls=True))

    async def update_model(self, request: Request) -> StreamResponse:
        graph_id = GraphName(request.match_info.get("graph_id", "resoto"))
        js = await self.json_from_request(request)
        kinds: List[Kind] = from_js(js, List[Kind])
        model = await self.deps.model_handler.update_model(graph_id, kinds)
        return await single_result(request, to_js(model, strip_nulls=True))

    async def get_node(self, request: Request) -> StreamResponse:
        graph_id = GraphName(request.match_info.get("graph_id", "resoto"))
        node_id = NodeId(request.match_info.get("node_id", "root"))
        graph = self.deps.db_access.get_graph_db(graph_id)
        model = await self.deps.model_handler.load_model(graph_id)
        node = await graph.get_node(model, node_id)
        if node is None:
            return web.HTTPNotFound(text=f"No such node with id {node_id} in graph {graph_id}")
        else:
            return await single_result(request, node)

    async def create_node(self, request: Request) -> StreamResponse:
        graph_id = GraphName(request.match_info.get("graph_id", "resoto"))
        node_id = NodeId(request.match_info.get("node_id", "some_existing"))
        parent_node_id = NodeId(request.match_info.get("parent_node_id", "root"))
        graph = self.deps.db_access.get_graph_db(graph_id)
        item = await self.json_from_request(request)
        md = await self.deps.model_handler.load_model(graph_id)
        node = await graph.create_node(md, node_id, item, parent_node_id)
        return await single_result(request, node)

    async def update_node(self, request: Request) -> StreamResponse:
        graph_id = GraphName(request.match_info.get("graph_id", "resoto"))
        node_id = NodeId(request.match_info.get("node_id", "some_existing"))
        section = section_of(request)
        graph = self.deps.db_access.get_graph_db(graph_id)
        patch = await self.json_from_request(request)
        md = await self.deps.model_handler.load_model(graph_id)
        node = await graph.update_node(md, node_id, patch, False, section)
        return await single_result(request, node)

    async def delete_node(self, request: Request) -> StreamResponse:
        graph_id = GraphName(request.match_info.get("graph_id", "resoto"))
        node_id = NodeId(request.match_info.get("node_id", "some_existing"))
        if node_id == "root":
            raise AttributeError("Root node can not be deleted!")
        graph = self.deps.db_access.get_graph_db(graph_id)
        await graph.delete_node(node_id)
        return web.HTTPNoContent()

    async def update_nodes(self, request: Request) -> StreamResponse:
        graph_name = GraphName(request.match_info.get("graph_id", "resoto"))
        allowed = {*Section.content, "id", "revision"}
        updates: Dict[NodeId, Json] = {}
        async for elem in self.to_json_generator(request):
            keys = set(elem.keys())
            assert keys.issubset(allowed), f"Invalid json. Allowed keys are: {allowed}"
            assert "id" in elem, f"No id given for element {elem}"
            assert keys.intersection(Section.content), f"No update provided for element {elem}"
            uid = elem["id"]
            assert uid not in updates, f"Only one update allowed per id! {elem}"
            del elem["id"]
            updates[uid] = elem
        db = self.deps.db_access.get_graph_db(graph_name)
        model = await self.deps.model_handler.load_model(graph_name)
        result_gen = db.update_nodes(model, updates)
        return await self.stream_response_from_gen(request, result_gen)

    async def list_graphs(self, request: Request) -> StreamResponse:
        graphs = await self.deps.db_access.list_graphs()
        return await single_result(request, graphs)

    async def create_graph(self, request: Request) -> StreamResponse:
        graph_id = request.match_info.get("graph_id", "resoto")
        if "_" in graph_id:
            raise AttributeError("Graph name should not have underscores!")
        graph_name = GraphName(graph_id)
        graph = await self.deps.db_access.create_graph(graph_name)
        model = await self.deps.model_handler.load_model(graph_name)
        root = await graph.get_node(model, NodeId("root"))
        return web.json_response(root)

    async def merge_graph(self, request: Request) -> StreamResponse:
        graph_id = GraphName(request.match_info.get("graph_id", "resoto"))
        wait_for_result = request.query.get("wait_for_result", "true").lower() == "true"
        task_id: Optional[TaskId] = None
        if tid := request.headers.get("Resoto-Worker-Task-Id"):
            task_id = TaskId(tid)
        log.info(
            f"Received merge_graph request for graph {graph_id}, wait_for_result={wait_for_result}, task_id={task_id}"
        )
        db = self.deps.db_access.get_graph_db(graph_id)
        it = self.to_line_generator(request)
        max_wait = self.deps.config.graph_update.merge_max_wait_time()
        info = await self.deps.graph_merger.merge_graph(db, it, max_wait, None, task_id, wait_for_result)
        return web.json_response(to_js(info)) if info else web.HTTPNoContent()

    async def update_merge_graph_batch(self, request: Request) -> StreamResponse:
        graph_id = GraphName(request.match_info.get("graph_id", "resoto"))
        wait_for_result = request.query.get("wait_for_result", "true").lower() == "true"
        task_id: Optional[TaskId] = None
        if tid := request.headers.get("Resoto-Worker-Task-Id"):
            task_id = TaskId(tid)
        log.info(f"Received put_sub_graph_batch request for graph {graph_id}, wait_for_result={wait_for_result}")
        db = self.deps.db_access.get_graph_db(graph_id)
        rnd = "".join(SystemRandom().choice(string.ascii_letters) for _ in range(12))
        batch_id = request.query.get("batch_id", rnd)
        it = self.to_line_generator(request)
        max_wait = self.deps.config.graph_update.merge_max_wait_time()
        info = await self.deps.graph_merger.merge_graph(db, it, max_wait, batch_id, task_id, wait_for_result)
        headers = {"BatchId": batch_id}
        return web.json_response(to_json(info), headers=headers) if info else web.HTTPNoContent(headers=headers)

    async def list_batches(self, request: Request) -> StreamResponse:
        graph_db = self.deps.db_access.get_graph_db(GraphName(request.match_info.get("graph_id", "resoto")))
        batch_updates = await graph_db.list_in_progress_updates()
        return web.json_response([b for b in batch_updates if b.get("is_batch")])

    async def commit_batch(self, request: Request) -> StreamResponse:
        graph_db = self.deps.db_access.get_graph_db(GraphName(request.match_info.get("graph_id", "resoto")))
        batch_id = request.match_info.get("batch_id", "some_existing")
        await graph_db.commit_batch_update(batch_id)
        return web.HTTPOk(body="Batch committed.")

    async def abort_batch(self, request: Request) -> StreamResponse:
        graph_db = self.deps.db_access.get_graph_db(GraphName(request.match_info.get("graph_id", "resoto")))
        batch_id = request.match_info.get("batch_id", "some_existing")
        await graph_db.abort_update(batch_id)
        return web.HTTPOk(body="Batch aborted.")

    async def graph_query_model_from_request(self, request: Request) -> Tuple[GraphDB, QueryModel]:
        section = section_of(request)
        query_string = await request.text()
        graph_name = GraphName(request.match_info.get("graph_id", "resoto"))
        raw_at = request.query.get("at")
        at = date_parser.parse(raw_at) if raw_at else None

        snapshot_name = None

        if at:
            snapshot_name = await self.deps.graph_manager.snapshot_at(time=at, graph_name=graph_name)
            if not snapshot_name:
                raise ValueError(f"No snapshot found for {graph_name} at {at}")

        graph_name = snapshot_name or graph_name

        graph_db = self.deps.db_access.get_graph_db(graph_name)
        q = await self.deps.template_expander.parse_query(query_string, section, **request.query)
        m = await self.deps.model_handler.load_model(graph_name)
        return graph_db, QueryModel(q, m)

    async def raw(self, request: Request) -> StreamResponse:
        graph_db, query_model = await self.graph_query_model_from_request(request)
        with_edges = request.query.get("edges") is not None
        query, bind_vars = await graph_db.to_query(query_model, with_edges)
        return web.json_response({"query": query, "bind_vars": bind_vars})

    async def explain(self, request: Request) -> StreamResponse:
        graph_db, query_model = await self.graph_query_model_from_request(request)
        result = await graph_db.explain(query_model)
        return web.json_response(to_js(result))

    async def query_structure(self, request: Request) -> StreamResponse:
        _, query_model = await self.graph_query_model_from_request(request)
        return web.json_response(query_model.query.structure())

    async def query_list(self, request: Request) -> StreamResponse:
        graph_db, query_model = await self.graph_query_model_from_request(request)
        count = request.query.get("count", "true").lower() != "false"
        timeout = if_set(request.query.get("search_timeout"), duration)
        async with await graph_db.search_list(query_model, count, timeout) as cursor:
            return await self.stream_response_from_gen(request, cursor, cursor.count())

    async def cytoscape(self, request: Request) -> StreamResponse:
        graph_db, query_model = await self.graph_query_model_from_request(request)
        result = await graph_db.search_graph(query_model)
        node_link_data = cytoscape_data(result)
        return web.json_response(node_link_data)

    async def query_graph_stream(self, request: Request) -> StreamResponse:
        graph_db, query_model = await self.graph_query_model_from_request(request)
        count = request.query.get("count", "true").lower() != "false"
        timeout = if_set(request.query.get("search_timeout"), duration)
        async with await graph_db.search_graph_gen(query_model, count, timeout) as cursor:
            return await self.stream_response_from_gen(request, cursor, cursor.count())

    async def query_aggregation(self, request: Request) -> StreamResponse:
        graph_db, query_model = await self.graph_query_model_from_request(request)
        async with await graph_db.search_aggregation(query_model) as gen:
            return await self.stream_response_from_gen(request, gen)

    async def query_history(self, request: Request) -> StreamResponse:
        graph_db, query_model = await self.graph_query_model_from_request(request)
        before = request.query.get("before")
        after = request.query.get("after")
        change = request.query.get("change")
        async with await graph_db.search_history(
            query=query_model,
            change=HistoryChange[change] if change else None,
            before=parse_utc(before) if before else None,
            after=parse_utc(after) if after else None,
        ) as gen:
            return await self.stream_response_from_gen(request, gen)

    async def serve_debug_ui(self, request: Request) -> FileResponse:
        """
        This is only for testing different versions of the UI during development.
        """
        commit = request.match_info.get("commit", "default")
        commit = commit[0:6] if len(commit) == 40 else commit  # shorten commit hash
        path = request.match_info.get("path", "index.html")
        dir_path = self.deps.config.run.temp_dir / "ui" / commit
        if not dir_path.exists():
            dir_path.mkdir(parents=True)
            async with self.session.get(f"https://cdn.some.engineering/resoto-ui/commits/{commit}.zip") as resp:
                if resp.status != 200:
                    raise NotFoundError(f"Commit not found: {commit}")
                body = await resp.read()
                with zipfile.ZipFile(BytesIO(body)) as zip_ref:
                    zip_ref.extractall(dir_path)
        file = dir_path / path
        if not file.exists():
            raise NotFoundError(f"File not found: {path}")
        return FileResponse(file)

    async def wipe(self, request: Request) -> StreamResponse:
        graph_id = GraphName(request.match_info.get("graph_id", "resoto"))
        if "truncate" in request.query:
            await self.deps.db_access.get_graph_db(graph_id).wipe()
            return web.HTTPOk(body="Graph truncated.")
        else:
            await self.deps.db_access.delete_graph(graph_id)
            return web.HTTPOk(body="Graph deleted.")

    async def cli_info(self, _: Request) -> StreamResponse:
        def cmd_json(cmd: CLICommand) -> Json:
            return {
                "name": cmd.name,
                "info": cmd.info(),
                "help": cmd.help(),
                "args": to_js(cmd.args_info(), force_dict=True),
                "source": cmd.allowed_in_source_position,
            }

        def alias_json(cmd: AliasTemplate) -> Json:
            return {
                "name": cmd.name,
                "info": cmd.info,
                "help": cmd.help(),
                "args": to_js(cmd.args_info(), force_dict=True),
                "source": cmd.allowed_in_source_position,
            }

        def infra_app_alias_json(cmd: InfraAppAlias) -> Json:
            return {
                "name": cmd.name,
                "info": cmd.description,
                "help": cmd.readme,
                "args": to_js(cmd.parameters, force_dict=True),
                "source": True,
            }

        commands = [
            cmd_json(cmd) for cmd in self.deps.cli.direct_commands.values() if not isinstance(cmd, InternalPart)
        ]
        replacements = self.deps.cli.replacements()
        return web.json_response(
            {
                "commands": commands,
                "replacements": replacements,
                "alias_names": alias_names(),
                "alias_templates": [alias_json(alias) for alias in self.deps.cli.alias_templates.values()],
                "infra_app_aliases": [
                    infra_app_alias_json(alias) for alias in self.deps.cli.infra_app_aliases.values()
                ],
            }
        )

    @staticmethod
    def cli_context_from_request(request: Request) -> CLIContext:
        try:
            columns = int(request.headers.get("Resoto-Shell-Columns", "120"))
            rows = int(request.headers.get("Resoto-Shell-Rows", "50"))
            terminal = request.headers.get("Resoto-Shell-Terminal", "false") == "true"
            colors = ConsoleColorSystem.from_name(request.headers.get("Resoto-Shell-Color-System", "monochrome"))
            renderer = ConsoleRenderer(width=columns, height=rows, color_system=colors, terminal=terminal)
            return CLIContext(env=dict(request.query), console_renderer=renderer, source="api")
        except Exception as ex:
            log.debug("Could not create CLI context.", exc_info=ex)
            return CLIContext(
                env=dict(request.query), console_renderer=ConsoleRenderer.default_renderer(), source="api"
            )

    async def evaluate(self, request: Request) -> StreamResponse:
        ctx = self.cli_context_from_request(request)
        command = await request.text()
        parsed = await self.deps.cli.evaluate_cli_command(command, ctx)

        def line_to_js(line: ParsedCommandLine) -> Json:
            parsed_commands = to_json(line.parsed_commands.commands)
            execute_commands = [{"cmd": part.command.name, "arg": part.arg} for part in line.executable_commands]
            return {"parsed": parsed_commands, "execute": execute_commands, "env": line.parsed_commands.env}

        return web.json_response([line_to_js(line) for line in parsed])

    @timed("api", "execute")
    async def execute(self, request: Request) -> StreamResponse:
        temp_dir: Optional[str] = None
        try:
            ctx = self.cli_context_from_request(request)
            if request.content_type.startswith("text"):
                command = (await request.text()).strip()
            elif request.content_type.startswith("multipart"):
                command = request.headers["Resoto-Shell-Command"].strip()
                temp = tempfile.mkdtemp()
                temp_dir = temp
                files = {}
                # for now, we assume that all multi-parts are file uploads
                async for part in MultipartReader(request.headers, request.content):
                    name = part.name
                    if not name:
                        raise AttributeError("Multipart request: content disposition name is required!")
                    path = os.path.join(temp, rnd_str())  # use random local path to avoid clashes
                    files[name] = path
                    with open(path, "wb") as writer:
                        while not part.at_eof():
                            writer.write(await part.read_chunk())
                ctx = evolve(ctx, uploaded_files=files)
            else:
                raise AttributeError(f"Not able to handle: {request.content_type}")

            # we want to eagerly evaluate the command, so that parse exceptions will throw directly here
            parsed = await self.deps.cli.evaluate_cli_command(command, ctx)
            return await self.execute_parsed(request, command, parsed, ctx)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir)

    async def execute_parsed(
        self, request: Request, command: str, parsed: List[ParsedCommandLine], ctx: CLIContext
    ) -> StreamResponse:
        # make sure, all requirements are fulfilled
        not_met_requirements = [not_met for line in parsed for not_met in line.unmet_requirements]
        # what is the accepted content type
        # only required for multipart requests
        boundary = "cli-part"
        mp_response = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": f"multipart/mixed;boundary={boundary}"}
        )

        if not_met_requirements:
            requirements = [req for line in parsed for cmd in line.executable_commands for req in cmd.action.required]
            data = {"command": command, "env": dict(request.query), "required": to_json(requirements)}
            return web.json_response(data, status=424)
        elif len(parsed) == 1:
            first_result = parsed[0]
            count, generator = await first_result.execute()
            # flat the results from 0 or 1
            async with generator.stream() as streamer:
                gen = await force_gen(streamer)
                if first_result.produces.text:
                    text_gen = ctx.text_generator(first_result, gen)
                    return await self.stream_response_from_gen(request, text_gen, count, first_result.envelope)
                elif first_result.produces.file_path:
                    await mp_response.prepare(request)
                    await Api.multi_file_response(first_result, gen, boundary, mp_response)
                    await Api.close_multi_part_response(mp_response, boundary)
                    return mp_response
                else:
                    raise AttributeError(f"Can not handle type: {first_result.produces}")
        elif len(parsed) > 1:
            await mp_response.prepare(request)
            for single in parsed:
                count, generator = await single.execute()
                async with generator.stream() as streamer:
                    gen = await force_gen(streamer)
                    if single.produces.text:
                        with MultipartWriter(repr(single.produces), boundary) as mp:
                            text_gen = ctx.text_generator(single, gen)
                            content_type, result_stream = await result_binary_gen(request, text_gen)
                            mp.append_payload(
                                AsyncIterablePayload(result_stream, content_type=content_type, headers=single.envelope)
                            )
                            await mp.write(mp_response, close_boundary=False)
                    elif single.produces.file_path:
                        await Api.multi_file_response(single, gen, boundary, mp_response)
                    else:
                        raise AttributeError(f"Can not handle type: {single.produces}")
            await Api.close_multi_part_response(mp_response, boundary)
            return mp_response
        else:
            raise AttributeError("No command could be parsed!")

    @classmethod
    async def json_from_request(cls, request: Request) -> Json:
        if request.content_type in ["application/json"]:
            return await request.json()  # type: ignore
        elif request.content_type in ["application/yaml", "text/yaml"]:
            text = await request.text()
            return yaml.safe_load(text)  # type: ignore
        else:
            raise HTTPNotAcceptable(text="Only support json")

    @classmethod
    async def to_json_generator(cls, request: Request) -> AsyncGenerator[Json, None]:
        async for line in cls.to_line_generator(request):
            yield json.loads(line) if isinstance(line, bytes) else line

    @staticmethod
    def to_line_generator(request: Request) -> AsyncGenerator[Union[bytes, Json], None]:
        async def stream_lines() -> AsyncGenerator[Union[bytes, Json], None]:
            async for line in request.content:
                if len(line.strip()) == 0:
                    continue
                yield line

        async def stream_json_array() -> AsyncGenerator[Union[bytes, Json], None]:
            js_elem = await request.json()
            if isinstance(js_elem, list):
                for doc in js_elem:
                    yield doc
            elif isinstance(js_elem, dict):
                yield js_elem
            else:
                log.warning(f"Received json is neither array nor document: {js_elem}! Ignore.")

        if request.content_type == "application/json":
            return stream_json_array()
        elif request.content_type in ["application/x-ndjson", "application/ndjson"]:
            return stream_lines()
        else:
            raise AttributeError("Can not read graph. Currently supported formats: json and ndjson!")

    @staticmethod
    def optional_json(o: Any, hint: str) -> StreamResponse:
        if o:
            return web.json_response(to_json(o))
        else:
            return web.HTTPNotFound(text=hint)

    @staticmethod
    async def stream_response_from_gen(
        request: Request,
        gen_in: AsyncIterator[JsonElement],
        count: Optional[int] = None,
        additional_header: Optional[Dict[str, str]] = None,
    ) -> StreamResponse:
        # force the async generator, to get an early exception in case of failure
        gen = await force_gen(gen_in)
        content_type, result_gen = await result_binary_gen(request, gen)
        count_header = {"Resoto-Shell-Element-Count": str(count)} if count else {}
        hdr = additional_header or {}
        response = web.StreamResponse(status=200, headers={**hdr, "Content-Type": content_type, **count_header})
        enable_compression(request, response)
        writer: AbstractStreamWriter = await response.prepare(request)  # type: ignore
        cr = "\n".encode("utf-8")
        async for data in result_gen:
            await writer.write(data + cr)
        await response.write_eof()
        return response

    @staticmethod
    async def multi_file_response(
        cmd_line: ParsedCommandLine, results: AsyncIterator[JsonElement], boundary: str, response: StreamResponse
    ) -> None:
        async for file_path in results:
            path = FilePath.from_path(file_path)
            if not (path.local.is_file()):
                raise HTTPNotFound(text=f"No file with this path: {file_path}")
            with open(path.local, "rb") as content:
                headers = cmd_line.envelope
                # only add path header if the user path is more than a file name
                if path.user.name != str(path.user):
                    headers = {**headers, "file-path": str(path.user)}
                with MultipartWriter(boundary=boundary) as mp:
                    pl = BufferedReaderPayload(
                        content,
                        content_type="application/octet-stream",
                        filename=path.user.name,
                        headers=headers,
                    )
                    mp.append_payload(pl)
                    await mp.write(response, close_boundary=False)

    @staticmethod
    async def close_multi_part_response(response: StreamResponse, boundary: str) -> None:
        with MultipartWriter(boundary=boundary) as mp:
            await mp.write(response, close_boundary=True)
        await response.write_eof()
