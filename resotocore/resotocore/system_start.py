import argparse
import logging
import multiprocessing as mp
import os.path
import sys
from argparse import Namespace
from collections import namedtuple
from pathlib import Path
from typing import Optional, List, Callable, Tuple

import psutil
from arango.database import StandardDatabase
from parsy import Parser

from resotocore import async_extensions, version
from resotocore.analytics import AnalyticsEventSender
from resotocore.core_config import CoreConfig, parse_config, git_hash_from_file, inside_docker
from resotocore.db.db_access import DbAccess
from resotocore.model.adjust_node import DirectAdjuster
from resotocore.types import JsonElement
from resotocore.util import utc
from resotolib.args import ArgumentParser
from resotolib.jwt import add_args as jwt_add_args
from resotolib.logger import setup_logger
from resotolib.parse_util import make_parser, variable_p, equals_p, comma_p, json_value_dp
from resotolib.utils import iec_size_format, get_local_tzinfo

log = logging.getLogger(__name__)

SystemInfo = namedtuple(
    "SystemInfo",
    ["version", "git_hash", "cpus", "mem_available", "mem_total", "inside_docker", "time_zone", "started_at"],
)
started_at = utc()


@make_parser
def path_json_value_parser() -> Parser:
    key = yield variable_p
    yield equals_p
    value = yield json_value_dp.sep_by(comma_p, min=1)
    return key, value


def system_info() -> SystemInfo:
    mem = psutil.virtual_memory()
    return SystemInfo(
        version=version(),
        git_hash=git_hash_from_file() or "n/a",
        cpus=mp.cpu_count(),
        mem_available=iec_size_format(mem.available),
        mem_total=iec_size_format(mem.total),
        inside_docker=inside_docker(),
        time_zone=get_local_tzinfo().key,
        started_at=started_at,
    )


def parse_args(args: Optional[List[str]] = None) -> Namespace:
    def is_file(message: str) -> Callable[[str], str]:
        def check_file(path: str) -> str:
            if os.path.isfile(path):
                return path
            else:
                raise AttributeError(f"{message}: path {path} is not a directory!")

        return check_file

    def key_value(kv: str) -> Tuple[str, JsonElement]:
        try:
            key, value = path_json_value_parser.parse(kv)
            return (key, value[0]) if len(value) == 1 else (key, value)
        except Exception as ex:
            raise AttributeError(f"Can not parse config option: {kv}. Reason: {ex}") from ex

    parser = ArgumentParser(
        env_args_prefix="RESOTOCORE_",
        description="Maintains graphs of resources of any shape.",
        epilog="Keeps all the things.",
    )
    jwt_add_args(parser)
    parser.add_argument(
        "--graphdb-server",
        default="http://localhost:8529",
        dest="graphdb_server",
        help="Graph database server (default: http://localhost:8529)",
    )
    parser.add_argument(
        "--graphdb-database", default="resoto", dest="graphdb_database", help="Graph database name (default: resoto)"
    )
    parser.add_argument(
        "--graphdb-username", default="resoto", dest="graphdb_username", help="Graph database login (default: resoto)"
    )
    parser.add_argument(
        "--graphdb-password", default="", dest="graphdb_password", help='Graph database password (default: "")'
    )
    parser.add_argument(
        "--graphdb-root-password",
        default="",
        dest="graphdb_root_password",
        help="Graph root database password used for creating user and database if not existent.",
    )
    parser.add_argument(
        "--graphdb-bootstrap-do-not-secure",
        default=False,
        action="store_true",
        dest="graphdb_bootstrap_do_not_secure",
        help="Leave an empty root password during system setup process.",
    )
    parser.add_argument(
        "--graphdb-type", default="arangodb", dest="graphdb_type", help="Graph database type (default: arangodb)"
    )
    parser.add_argument(
        "--graphdb-no-ssl-verify",
        action="store_true",
        dest="graphdb_no_ssl_verify",
        help="If the connection should not be verified (default: False)",
    )
    parser.add_argument(
        "--graphdb-request-timeout",
        type=int,
        default=900,
        dest="graphdb_request_timeout",
        help="Request timeout in seconds (default: 900)",
    )
    parser.add_argument("--no-tls", default=False, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--cert",
        type=is_file("can not parse --cert"),
        dest="cert",
        help="Path to a single file in PEM format containing the host certificate. "
        "If no certificate is provided, it is created using the CA.",
    )
    parser.add_argument(
        "--cert-key",
        type=is_file("can not parse --cert-key"),
        dest="cert_key",
        help="In case a --cert is provided. Path to a file containing the private key.",
    )
    parser.add_argument(
        "--cert-key-pass",
        dest="cert_key_pass",
        type=str,
        help="In case a --cert is provided. Optional password to decrypt the private key file.",
    )
    parser.add_argument(
        "--ca-cert",
        type=is_file("can not parse --ca-cert"),
        dest="ca_cert",
        help="Path to a single file in PEM format containing the CA certificate.",
    )
    parser.add_argument(
        "--ca-cert-key",
        type=is_file("can not parse --ca-cert-key"),
        dest="ca_cert_key",
        help="Path to a file containing the private key for the CA certificate. "
        "New certificates can be created when a CA certificate and private key is provided. "
        "Without the private key, the CA certificate is only used for outgoing http requests.",
    )
    parser.add_argument(
        "--ca-cert-key-pass",
        dest="ca_cert_key_pass",
        type=str,
        help="Optional password to decrypt the private ca-cert-key file.",
    )
    parser.add_argument("--version", action="store_true", help="Print the version of resotocore and exit.")
    parser.add_argument(
        "--override",
        "-o",
        nargs="+",
        type=key_value,
        dest="config_override",
        default=[],
        help="Override configuration parameters. Format: path.to.property=value. "
        "The existing configuration will be patched with the provided values. "
        "A value can be a simple value or a comma separated list of values if a list is required. "
        "Note: this argument allows multiple overrides separated by space. "
        "Example: --override resotocore.api.web_hosts=localhost,some.domain resotocore.api.https_port=12345",
    )
    parser.add_argument(
        "--override-path",
        nargs="+",
        type=Path,
        dest="config_override_path",
        default=[],
        help="Override configuration parameters via a YAML file or directory with YAML files. "
        "The existing configuration will be patched with the provided values. "
        "Note: this argument allows multiple overrides separated by space, in this case the "
        "resulting configuration will be the merge of all the provided files, in the order they are provided. "
        "The same section can be overridden multiple times, in this case the last override will be used. "
        "Example: --override-path /path/to/config/dir/ /path/to/your/config.yaml "
        "Be sure to specify the correct config id in the name of the yaml file, e.g. override for resotoworker would "
        "be called resoto.worker.yaml and look like:\n"
        """
resotoworker:
    ...
aws:
    ...""",
    )
    parser.add_argument(
        "--verbose", "-v", dest="verbose", default=False, action="store_true", help="Enable verbose logging."
    )
    parser.add_argument(  # No default here on purpose: it can be reconfigured!
        "--debug", default=None, action="store_true", help="Enable debug mode. If not defined use configuration."
    )
    parser.add_argument("--ui-path", help=argparse.SUPPRESS)  # no effect any longer, only here to not break anything
    parser.add_argument("--analytics-opt-out", default=None, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-scheduling", default=False, action="store_true", help="Disable scheduling of jobs and workflows."
    )
    parser.add_argument(
        "--ignore-interrupted-tasks",
        default=False,
        action="store_true",
        help="Do not load and continue interrupted tasks.",
    )

    parsed: Namespace = parser.parse_args(args if args else [])

    if parsed.version:
        # print here on purpose, since logging is not set up yet.
        print(f"resotocore {version()}")
        sys.exit(0)

    return parsed


def empty_config(args: Optional[List[str]] = None) -> CoreConfig:
    return parse_config(parse_args(args or []), {}, lambda: None)


# Note: this method should be called from every started process as early as possible
def setup_process(args: Namespace, config: Optional[CoreConfig] = None) -> None:
    if config:
        configure_logging(config.runtime.log_level, (config.runtime.debug or False) | (args.verbose or False))
    else:
        configure_logging("info", (args.debug or False) | (args.verbose or False))
    # set/reset process creation method
    reset_process_start_method()
    # reset global async thread pool (forked processes need to create a fresh pool)
    async_extensions.GlobalAsyncPool = None


def reconfigure_logging(config: CoreConfig) -> None:
    configure_logging(config.runtime.log_level, (config.runtime.debug or False) | (config.args.verbose or False))


def configure_logging(log_level: str, verbose: bool) -> None:
    # Note: if another appender than the log appender is used, proper multiprocess logging needs to be enabled.
    # See https://docs.python.org/3/howto/logging-cookbook.html#logging-to-a-single-file-from-multiple-processes
    level = log_level.upper()
    setup_logger("resotocore", level=level, verbose=verbose, force=True)

    # adjust log levels for specific loggers
    if verbose:
        logging.getLogger("resoto").setLevel(logging.DEBUG)
        logging.getLogger("resotolib").setLevel(logging.DEBUG)
        logging.getLogger("resotocore").setLevel(logging.DEBUG)
        # in case of restart: reset the original level
        logging.getLogger("posthog").setLevel(level)
        logging.getLogger("backoff").setLevel(level)
        logging.getLogger("transitions.core").setLevel(level)
        logging.getLogger("apscheduler.executors").setLevel(level)
        logging.getLogger("apscheduler.scheduler").setLevel(level)
    else:
        # in case of restart: reset the original level
        logging.getLogger("resoto").setLevel(level)
        logging.getLogger("resotolib").setLevel(level)
        logging.getLogger("resotocore").setLevel(level)
        # mute analytics transmission errors unless debug is enabled
        logging.getLogger("posthog").setLevel(logging.FATAL)
        logging.getLogger("backoff").setLevel(logging.FATAL)
        # transitions (fsm) creates a lot of log noise. Only show warnings.
        logging.getLogger("transitions.core").setLevel(logging.WARNING)
        # apscheduler uses the term Job when it triggers, which confuses people.
        logging.getLogger("apscheduler.executors").setLevel(logging.WARNING)
        logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)


def reset_process_start_method() -> None:
    preferred = "spawn"
    current = mp.get_start_method(True)
    if current != preferred:
        if preferred in mp.get_all_start_methods():
            log.debug(f"Set process start method to {preferred}")
            mp.set_start_method(preferred, True)
            return
        log.warning(f"{preferred} method not available. Have {mp.get_all_start_methods()}. Use {current}")


def db_access(config: CoreConfig, db: StandardDatabase, event_sender: AnalyticsEventSender) -> DbAccess:
    adjuster = DirectAdjuster()
    return DbAccess(db, event_sender, adjuster, config)
