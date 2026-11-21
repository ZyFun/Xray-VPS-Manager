"""Точечный откат клиентской записи после отказа применения config.json."""

from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from xray_vps_manager.core.errors import ConflictError
from xray_vps_manager.core.paths import paths


# Порядок восстановления следует за внешними ключами.
_CHANGED_TABLES = {
    "reality_connections": ("tag",),
    "cascade_routes": ("tag",),
    "clients": ("name",),
    "client_traffic_limits": ("client_name",),
    "client_traffic_limit_state": ("client_name",),
    "client_credentials": ("client_name", "connection_tag"),
}
_CLIENT_CASCADES = (
    "traffic_totals", "traffic_history", "activity_events",
    "activity_blocklist_hits", "activity_capture_clients", "activity_alert_events",
    "activity_client_counters", "activity_client_counter_uniques",
)
_CREDENTIAL_CASCADES = ("credential_traffic_totals", "credential_traffic_history")
_CLIENT_EVENTS = "SELECT id FROM activity_events WHERE client_name = :name"
_CLEARED_LINKS = (
    ("telegram_subscriptions", "client_name", "client_name = :name"),
    ("activity_blocklist", "source_client_name", "source_client_name = :name"),
    ("activity_blocklist", "source_event_id", f"source_event_id IN ({_CLIENT_EVENTS})"),
    ("activity_alert_events", "raw_ref_event_id", f"raw_ref_event_id IN ({_CLIENT_EVENTS}) AND client_name <> :name"),
)
_BATCH_SIZE = 256


def _select(conn, table, predicate, parameters):
    return tuple(map(dict, conn.execute(f"SELECT * FROM {table} WHERE {predicate}", parameters)))


def _changed_rows(conn, table, names):
    key = _CHANGED_TABLES[table][0]
    return {
        tuple(row[column] for column in _CHANGED_TABLES[table]): row
        for name in names
        for row in _select(conn, table, f"{key} = ?", (name,))
    }


def _restore_rows(conn, table, rows, *, key):
    if not rows:
        return
    columns = tuple(rows[0])
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(f"{column} = excluded.{column}" for column in columns if column not in key)
    sql = (f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
           f" ON CONFLICT ({', '.join(key)}) DO UPDATE SET {assignments}")
    conn.executemany(sql, (tuple(row[column] for column in columns) for row in rows))


def _batches(cursor):
    while rows := cursor.fetchmany(_BATCH_SIZE):
        yield rows


def _copy_rows(conn, table, cursor):
    columns = ", ".join(column[0] for column in cursor.description)
    parameters = ", ".join("?" for _ in cursor.description)
    for rows in _batches(cursor):
        conn.executemany(f"INSERT INTO {table} ({columns}) VALUES ({parameters})", rows)


class _CascadeBackup:
    """Приватная копия строк, которые удаляются или меняются по внешним ключам."""

    def __init__(self, resources: ExitStack):
        directory = resources.enter_context(TemporaryDirectory(
            prefix="xray-client-undo-", dir=paths().manager_db.parent,
        ))
        path = Path(directory) / "cascade.sqlite3"
        path.touch(mode=0o600)
        self.db = resources.enter_context(closing(sqlite3.connect(path)))
        self.db.execute("PRAGMA journal_mode = OFF")
        self.db.execute("PRAGMA cache_size = -1024")
        self.tables: set[str] = set()

    def _capture_query(self, conn, target, query, parameters):
        with closing(conn.execute(query, parameters)) as cursor:
            if target not in self.tables:
                columns = ", ".join(column[0] for column in cursor.description)
                self.db.execute(f"CREATE TABLE {target} ({columns})")
                self.tables.add(target)
            _copy_rows(self.db, target, cursor)

    def capture(self, conn, removed_clients, removed_credentials):
        removed_clients = frozenset(removed_clients)
        with self.db:
            for table in (*_CLIENT_CASCADES, *_CREDENTIAL_CASCADES):
                for name in removed_clients:
                    self._capture_query(conn, table, f"SELECT * FROM {table} WHERE client_name = ?", (name,))
            for table in _CREDENTIAL_CASCADES:
                for name, tag in removed_credentials:
                    if name not in removed_clients:
                        self._capture_query(
                            conn, table, f"SELECT * FROM {table} WHERE client_name = ? AND connection_tag = ?",
                            (name, tag),
                        )
            for name in removed_clients:
                self._capture_query(
                    conn, "activity_event_risks",
                    f"SELECT * FROM activity_event_risks WHERE event_id IN ({_CLIENT_EVENTS})", {"name": name},
                )
                for table, column, predicate in _CLEARED_LINKS:
                    self._capture_query(
                        conn, f"links_{table}_{column}", f"SELECT {column}, id FROM {table} WHERE {predicate}",
                        {"name": name},
                    )

    def restore(self, conn):
        for table in (*_CLIENT_CASCADES, *_CREDENTIAL_CASCADES, "activity_event_risks"):
            if table in self.tables:
                with closing(self.db.execute(f"SELECT * FROM {table}")) as cursor:
                    _copy_rows(conn, table, cursor)
        # Параллельная запись могла изменить другие поля, удалить ссылку
        # или привязать строку к другой записи. Возвращаем только сброшенное поле.
        for table, column, _ in _CLEARED_LINKS:
            target = f"links_{table}_{column}"
            if target in self.tables:
                with closing(self.db.execute(f"SELECT {column}, id FROM {target}")) as cursor:
                    for links in _batches(cursor):
                        conn.executemany(f"UPDATE {table} SET {column} = ? WHERE id = ? AND {column} IS NULL", links)


@dataclass
class ClientUndo:
    scopes: dict
    before: dict
    after: dict = field(default_factory=dict)
    cascaded: _CascadeBackup | None = None

    @classmethod
    def capture(
        cls, conn, *, resources: ExitStack, clients, connections, routes,
        removed_clients=(), removed_credentials=(), removed_connections=(),
    ):
        clients = set(clients)
        for tag in removed_connections:
            # Legacy-поле основного подключения может отсутствовать в нормализованном снимке.
            clients.update(row["name"] for row in conn.execute(
                "SELECT name FROM clients WHERE connection_tag = ?", (tag,),
            ))
        scopes = {
            "reality_connections": connections,
            "cascade_routes": routes,
            "clients": clients,
            "client_traffic_limits": clients,
            "client_traffic_limit_state": clients,
            "client_credentials": clients,
        }
        undo = cls(scopes, {table: _changed_rows(conn, table, keys) for table, keys in scopes.items()})
        if removed_clients or removed_credentials:
            undo.cascaded = _CascadeBackup(resources)
            undo.cascaded.capture(conn, removed_clients, removed_credentials)
        return undo

    def record_written(self, conn):
        self.after = {table: _changed_rows(conn, table, keys) for table, keys in self.scopes.items()}

    def restore(self, conn):
        current = {table: _changed_rows(conn, table, keys) for table, keys in self.scopes.items()}
        if current == self.before:
            return
        if current != self.after:
            raise ConflictError("Клиентские данные изменились после записи. Компенсация прервана.")
        # Сначала убираем созданные credentials и клиентов, затем возвращаем
        # исходные ссылки на подключения. Новые подключения удаляем последними.
        for table, key in reversed(tuple(_CHANGED_TABLES.items())):
            if table != "reality_connections":
                self._delete_created(conn, table, key)
        for table, key in _CHANGED_TABLES.items():
            changed = [row for pk, row in self.before[table].items() if row != self.after[table].get(pk)]
            _restore_rows(conn, table, changed, key=key)
        self._delete_created(conn, "reality_connections", _CHANGED_TABLES["reality_connections"])
        if self.cascaded is not None:
            self.cascaded.restore(conn)

    def _delete_created(self, conn, table, key):
        predicate = " AND ".join(f"{column} = ?" for column in key)
        conn.executemany(f"DELETE FROM {table} WHERE {predicate}", self.after[table].keys() - self.before[table].keys())
