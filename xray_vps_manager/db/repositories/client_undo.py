"""Точечный откат клиентской записи после отказа применения config.json."""

from dataclasses import dataclass, field

from xray_vps_manager.core.errors import ConflictError


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


def _select(conn, table, predicate, parameters):
    return tuple(map(dict, conn.execute(f"SELECT * FROM {table} WHERE {predicate}", parameters)))


def _changed_rows(conn, table, names):
    key = _CHANGED_TABLES[table][0]
    return {
        tuple(row[column] for column in _CHANGED_TABLES[table]): row
        for name in names
        for row in _select(conn, table, f"{key} = ?", (name,))
    }


def _restore_rows(conn, table, rows, *, key=()):
    if not rows:
        return
    columns = tuple(rows[0])
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    if key:
        assignments = ", ".join(f"{column} = excluded.{column}" for column in columns if column not in key)
        sql += f" ON CONFLICT ({', '.join(key)}) DO UPDATE SET {assignments}"
    conn.executemany(sql, (tuple(row[column] for column in columns) for row in rows))


@dataclass
class ClientUndo:
    scopes: dict
    before: dict
    after: dict = field(default_factory=dict)
    cascaded: dict = field(default_factory=dict)
    cleared_links: list = field(default_factory=list)

    @classmethod
    def capture(cls, conn, *, clients, connections, routes, removed_client=None, removed_credentials=()):
        scopes = {
            "reality_connections": connections,
            "cascade_routes": routes,
            "clients": clients,
            "client_traffic_limits": clients,
            "client_traffic_limit_state": clients,
            "client_credentials": clients,
        }
        undo = cls(scopes, {table: _changed_rows(conn, table, keys) for table, keys in scopes.items()})
        if removed_client is not None:
            undo._capture_client_deletion(conn, removed_client)
        elif removed_credentials:
            for table in _CREDENTIAL_CASCADES:
                undo.cascaded[table] = tuple(
                    row
                    for key in removed_credentials
                    for row in _select(conn, table, "client_name = ? AND connection_tag = ?", key)
                )
        return undo

    def _capture_client_deletion(self, conn, name):
        for table in (*_CLIENT_CASCADES, *_CREDENTIAL_CASCADES):
            self.cascaded[table] = _select(conn, table, "client_name = ?", (name,))
        events = "SELECT id FROM activity_events WHERE client_name = ?"
        self.cascaded["activity_event_risks"] = _select(conn, "activity_event_risks", f"event_id IN ({events})", (name,))
        for table, column, predicate, parameters in (
            ("telegram_subscriptions", "client_name", "client_name = ?", (name,)),
            ("activity_blocklist", "source_client_name", "source_client_name = ?", (name,)),
            ("activity_blocklist", "source_event_id", f"source_event_id IN ({events})", (name,)),
            ("activity_alert_events", "raw_ref_event_id", f"raw_ref_event_id IN ({events}) AND client_name <> ?", (name, name)),
        ):
            links = conn.execute(f"SELECT {column}, id FROM {table} WHERE {predicate}", parameters)
            self.cleared_links.append((table, column, tuple(map(tuple, links))))

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
        for table, rows in self.cascaded.items():
            _restore_rows(conn, table, rows)
        # Параллельная запись могла изменить другие поля, удалить ссылку
        # или привязать строку к другой записи. Возвращаем только сброшенное поле.
        for table, column, links in self.cleared_links:
            conn.executemany(f"UPDATE {table} SET {column} = ? WHERE id = ? AND {column} IS NULL", links)

    def _delete_created(self, conn, table, key):
        predicate = " AND ".join(f"{column} = ?" for column in key)
        conn.executemany(f"DELETE FROM {table} WHERE {predicate}", self.after[table].keys() - self.before[table].keys())
