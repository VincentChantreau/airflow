#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import functools
import gzip
import itertools
import json
import logging
from io import BytesIO
from typing import Callable, TypeVar, cast

import pendulum
from flask import after_this_request, request
from pendulum.parsing.exceptions import ParserError

from airflow.models import Log
from airflow.utils.log import secrets_masker
from airflow.utils.session import create_session
from airflow.www.extensions.init_auth_manager import get_auth_manager

T = TypeVar("T", bound=Callable)

logger = logging.getLogger(__name__)


def _mask_variable_fields(extra_fields):
    """
    Mask the 'val_content' field if 'key_content' is in the mask list.

    The variable requests values and args comes in this form:
    [('key', 'key_content'),('val', 'val_content'), ('description', 'description_content')]
    """
    result = []
    keyname = None
    for k, v in extra_fields:
        if k == "key":
            keyname = v
            result.append((k, v))
        elif keyname and k == "val":
            x = secrets_masker.redact(v, keyname)
            result.append((k, x))
            keyname = None
        else:
            result.append((k, v))
    return result


def _mask_connection_fields(extra_fields):
    """Mask connection fields."""
    result = []
    for k, v in extra_fields:
        if k == "extra":
            try:
                extra = json.loads(v)
                extra = [(k, secrets_masker.redact(v, k)) for k, v in extra.items()]
                result.append((k, json.dumps(dict(extra))))
            except json.JSONDecodeError:
                result.append((k, "Encountered non-JSON in `extra` field"))
        else:
            result.append((k, secrets_masker.redact(v, k)))
    return result


def action_logging(func: T | None = None, event: str | None = None) -> T | Callable:
    """Log user actions."""

    def log_action(f: T) -> T:
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            __tracebackhide__ = True  # Hide from pytest traceback.

            with create_session() as session:
                event_name = event or f.__name__
                if not get_auth_manager().is_logged_in():
                    user = "anonymous"
                    user_display = ""
                else:
                    user = get_auth_manager().get_user_name()
                    user_display = get_auth_manager().get_user_display_name()

                fields_skip_logging = {"csrf_token", "_csrf_token", "is_paused"}
                extra_fields = [
                    (k, secrets_masker.redact(v, k))
                    for k, v in itertools.chain(request.values.items(multi=True), request.view_args.items())
                    if k not in fields_skip_logging
                ]
                if event and event.startswith("variable."):
                    extra_fields = _mask_variable_fields(extra_fields)
                if event and event.startswith("connection."):
                    extra_fields = _mask_connection_fields(extra_fields)

                params = {**request.values, **request.view_args}

                if request.blueprint == "/api/v1":
                    if f"{request.origin}/" == request.root_url:
                        event_name = f"ui.{event_name}"
                    else:
                        event_name = f"api.{event_name}"

                if params and "is_paused" in params:
                    extra_fields.append(("is_paused", params["is_paused"] == "false"))
                log = Log(
                    event=event_name,
                    task_instance=None,
                    owner=user,
                    owner_display_name=user_display,
                    extra=str(extra_fields),
                    task_id=params.get("task_id"),
                    dag_id=params.get("dag_id"),
                )

                if "execution_date" in request.values:
                    execution_date_value = request.values.get("execution_date")
                    try:
                        log.execution_date = pendulum.parse(execution_date_value, strict=False)
                    except ParserError:
                        logger.exception(
                            "Failed to parse execution_date from the request: %s", execution_date_value
                        )

                session.add(log)

            return f(*args, **kwargs)

        return cast(T, wrapper)

    if func:
        return log_action(func)
    return log_action


def gzipped(f: T) -> T:
    """Make a view compressed."""

    @functools.wraps(f)
    def view_func(*args, **kwargs):
        @after_this_request
        def zipper(response):
            accept_encoding = request.headers.get("Accept-Encoding", "")

            if "gzip" not in accept_encoding.lower():
                return response

            response.direct_passthrough = False

            if (
                response.status_code < 200
                or response.status_code >= 300
                or "Content-Encoding" in response.headers
            ):
                return response
            with BytesIO() as gzip_buffer:
                with gzip.GzipFile(mode="wb", fileobj=gzip_buffer) as gzip_file:
                    gzip_file.write(response.data)
                response.data = gzip_buffer.getvalue()
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Vary"] = "Accept-Encoding"
            response.headers["Content-Length"] = len(response.data)

            return response

        return f(*args, **kwargs)

    return cast(T, view_func)
