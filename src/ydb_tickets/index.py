import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import ydb
import ydb.iam

YDB_ENDPOINT = os.getenv("YDB_ENDPOINT")
YDB_DATABASE = os.getenv("YDB_DATABASE")

if not YDB_ENDPOINT or not YDB_DATABASE:
    raise RuntimeError("Missing YDB_ENDPOINT or YDB_DATABASE in environment variables")

driver = ydb.Driver(
    endpoint=YDB_ENDPOINT,
    database=YDB_DATABASE,
    credentials=ydb.iam.MetadataUrlCredentials(),
)
try:
    driver.wait(fail_fast=True, timeout=5)
except Exception:
    raise RuntimeError(f"Failed to connect to YDB: {driver.discovery_debug_details()}")

session_pool = ydb.SessionPool(driver)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def create_ticket(user_id: str, category: str, text: str) -> Dict[str, Any]:
    ticket_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    created_at = _now()

    yql_ticket = """
        DECLARE $id AS Utf8;
        DECLARE $user_id AS Utf8;
        DECLARE $category AS Utf8;
        DECLARE $text AS Utf8;
        DECLARE $created_at AS Timestamp;
        DECLARE $updated_at AS Timestamp;

        INSERT INTO tickets (id, user_id, category, status, text, created_at, updated_at)
        VALUES ($id, $user_id, $category, 'open', $text, $created_at, $updated_at);
    """

    yql_message = """
        DECLARE $id AS Utf8;
        DECLARE $ticket_id AS Utf8;
        DECLARE $text AS Utf8;
        DECLARE $created_at AS Timestamp;

        INSERT INTO messages (id, ticket_id, role, text, created_at)
        VALUES ($id, $ticket_id, 'user', $text, $created_at);
    """

    def callee(session):
        prepared_ticket = session.prepare(yql_ticket)
        prepared_message = session.prepare(yql_message)
        tx = session.transaction()
        tx.execute(
            prepared_ticket,
            {
                "$id": ticket_id,
                "$user_id": user_id,
                "$category": category,
                "$text": text,
                "$created_at": created_at,
                "$updated_at": created_at,
            },
        )
        tx.execute(
            prepared_message,
            {
                "$id": message_id,
                "$ticket_id": ticket_id,
                "$text": text,
                "$created_at": created_at,
            },
            commit_tx=True,
        )

    session_pool.retry_operation_sync(callee)
    return {"ticket_id": ticket_id, "created_at": _iso(created_at)}


def list_my_tickets(user_id: str) -> List[Dict[str, Any]]:
    yql = """
        DECLARE $user_id AS Utf8;

        SELECT id, status, category, text, created_at
        FROM tickets VIEW tickets_by_user
        WHERE user_id = $user_id;
    """

    def callee(session):
        prepared = session.prepare(yql)
        result_sets = session.transaction().execute(
            prepared,
            {"$user_id": user_id},
            commit_tx=True,
        )
        rows = []
        for row in result_sets[0].rows:
            rows.append(
                {
                    "id": row.id,
                    "status": row.status,
                    "category": row.category,
                    "text": row.text,
                    "created_at": _iso(row.created_at),
                }
            )
        return rows

    return session_pool.retry_operation_sync(callee)


def append_message(ticket_id: str, user_id: str, text: str, role: str) -> Dict[str, Any]:
    # user_id нужен для контракта MCP/диспетчеризации; в messages колонки user_id нет
    _ = user_id
    message_id = str(uuid.uuid4())
    created_at = _now()

    yql = """
        DECLARE $id AS Utf8;
        DECLARE $ticket_id AS Utf8;
        DECLARE $role AS Utf8;
        DECLARE $text AS Utf8;
        DECLARE $created_at AS Timestamp;

        INSERT INTO messages (id, ticket_id, role, text, created_at)
        VALUES ($id, $ticket_id, $role, $text, $created_at);
    """

    def callee(session):
        prepared = session.prepare(yql)
        session.transaction().execute(
            prepared,
            {
                "$id": message_id,
                "$ticket_id": ticket_id,
                "$role": role,
                "$text": text,
                "$created_at": created_at,
            },
            commit_tx=True,
        )

    session_pool.retry_operation_sync(callee)
    return {"message_id": message_id, "ok": True}


def detect_tool_from_keys(params: Dict[str, Any]) -> Optional[str]:
    """Диспетчеризация MCP Hub: аргументы приходят без обёртки {"tool": ...}."""
    has_ticket = "ticket_id" in params
    has_user = "user_id" in params
    has_text = "text" in params
    has_category = "category" in params

    if has_ticket and has_user and has_text and not has_category:
        return "append-message"
    if has_user and has_category and has_text and not has_ticket:
        return "create-ticket"
    if has_user and not has_ticket and not has_category and not has_text:
        return "list-my-tickets"
    return None


def normalize_event(event: Any) -> Dict[str, Any]:
    """
    Три источника события → {"action": "...", "params": {...}}:
    1) прямой invoke: {"action": "create-ticket", ...}
    2) HTTP API Gateway: {"httpMethod": "POST", "body": "<JSON>"}
    3) MCP Hub: аргументы инструмента напрямую (диспетчеризация по ключам)
    """
    if not isinstance(event, dict):
        raise ValueError(f"Expected dict event, got {type(event).__name__}: {event!r}")

    if "action" in event:
        action = event["action"]
        params = {k: v for k, v in event.items() if k != "action"}
        return {"action": action, "params": params}

    if "httpMethod" in event and "body" in event:
        body = event["body"]
        if body is None or body == "":
            raise ValueError("Empty HTTP body")
        if isinstance(body, str):
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in body: {e}") from e
        elif isinstance(body, dict):
            payload = body
        else:
            raise ValueError("Body must be a JSON string or dict")
        return normalize_event(payload)

    detected = detect_tool_from_keys(event)
    if detected:
        return {"action": detected, "params": event}

    raise ValueError("Unable to normalize event: unknown format and could not detect action from keys")


def _http_response(status_code: int, payload: Any) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def handle_request(event: Any, *, as_http: bool = False) -> Any:
    try:
        normalized = normalize_event(event)
        action = normalized["action"]
        params = normalized["params"]

        if action == "create-ticket":
            user_id = params.get("user_id")
            category = params.get("category")
            text = params.get("text")
            if not all([user_id, category, text]):
                error = {"error": "Missing required params for create-ticket (user_id, category, text)"}
                return _http_response(400, error) if as_http else error
            result = create_ticket(user_id=user_id, category=category, text=text)

        elif action == "list-my-tickets":
            user_id = params.get("user_id")
            if not user_id:
                error = {"error": "Missing user_id for list-my-tickets"}
                return _http_response(400, error) if as_http else error
            result = list_my_tickets(user_id=user_id)

        elif action == "append-message":
            ticket_id = params.get("ticket_id")
            user_id = params.get("user_id")
            text = params.get("text")
            role = params.get("role")
            if not all([ticket_id, user_id, text, role]):
                error = {"error": "Missing required params for append-message (ticket_id, user_id, text, role)"}
                return _http_response(400, error) if as_http else error
            result = append_message(ticket_id=ticket_id, user_id=user_id, text=text, role=role)

        else:
            error = {"error": f"Unknown action: {action}"}
            return _http_response(400, error) if as_http else error

        return _http_response(200, result) if as_http else result

    except Exception as e:
        print(f"ERROR: {e}")
        error = {"error": str(e)}
        return _http_response(500, error) if as_http else error


def handle(event, context=None):
    print(f"RAW EVENT: {event!r}")
    as_http = isinstance(event, dict) and "httpMethod" in event
    return handle_request(event, as_http=as_http)
