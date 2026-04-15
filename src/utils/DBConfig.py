"""
Database utility centralizes MySQL connection setup and common query helpers.

Example `config.json`:
{
    "db": {
        "host": "localhost",
        "port": 3306,
        "user": "your_username",
        "password": "your_password",
        "database": "model_changes",
        "folder": "raw_data"
    }
}
"""

import json
import os
from pathlib import Path

import pymysql

# targets a single database name and data root.
DEFAULT_DB_NAME = "model_changes"
DEFAULT_DATA_FOLDER = "raw_data"
# Optional environment variable for a local, user-provided DB config file.
CONFIG_ENV_VAR = "MODEL_UPDATE_DB_CONFIG"


class DatabaseConfig:
    def __init__(self, host=None, port=None, user=None, pwd=None, db=None, folder=None):
        self._host = host
        self._port = port
        self._user = user
        self._pwd = pwd
        self._db = db or DEFAULT_DB_NAME
        self._folder = folder or DEFAULT_DATA_FOLDER

        self.connection = None
        self.cursor = None

    @property
    def host(self):
        return self._host

    @property
    def port(self):
        return self._port

    @property
    def user(self):
        return self._user

    @property
    def pwd(self):
        return self._pwd

    @property
    def db(self):
        return self._db

    @property
    def folder(self):
        return self._folder

    def __str__(self):
        return "DB = host: {}, port: {}, user: {}, db: {}, folder: {}".format(
            self._host,
            self._port,
            self._user,
            self._db,
            self._folder,
        )

    # Prefer an explicit config path, then fall back to files shipped beside the replication code.
    def _candidate_config_paths(self):
        env_path = os.getenv(CONFIG_ENV_VAR)
        if env_path:
            return [Path(env_path).expanduser()]

        utils_dir = Path(__file__).resolve().parent
        src_dir = utils_dir.parent
        return [
            src_dir / "config.json",
            src_dir / "config.example.json",
        ]

    def _load_config(self):
        for path in self._candidate_config_paths():
            if path.is_file():
                with path.open("r", encoding="utf-8") as json_file:
                    return json.load(json_file)
        return None

    # Keep constructor values if they were provided, otherwise fill from config.
    def _apply_connection_settings(self, db_config):
        self._host = self._host or db_config.get("host")
        self._port = self._port or db_config.get("port", 3306)
        self._user = self._user or db_config.get("user")
        self._pwd = self._pwd or db_config.get("password")
        self._db = self._db or db_config.get("database", DEFAULT_DB_NAME)
        self._folder = self._folder or db_config.get("folder", DEFAULT_DATA_FOLDER)

    def _validate_connection_settings(self):
        missing = []
        if not self._host:
            missing.append("host")
        if self._port is None:
            missing.append("port")
        if not self._user:
            missing.append("user")
        if self._pwd is None:
            missing.append("password")
        if not self._db:
            missing.append("database")

        if missing:
            raise ValueError(
                "Missing database settings: {}. "
                "For the replication package, provide them via the constructor or "
                "create a config file and point MODEL_UPDATE_DB_CONFIG to it. "
                "A minimal placeholder file should define db.host, db.port, db.user, and db.password."
                .format(", ".join(missing))
            )

    # Connection setup is deterministic in the replication package: no interactive prompts.
    def create_db_connection(self):
        config = self._load_config()
        if config:
            self._apply_connection_settings(config.get("db", {}))

        # Replication package uses a single database target only.
        # The defaults act as placeholders when no explicit override is provided.
        self._db = DEFAULT_DB_NAME if self._db is None else self._db
        self._folder = DEFAULT_DATA_FOLDER if self._folder is None else self._folder

        self._validate_connection_settings()

        self.connection = pymysql.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._pwd,
            db=self._db,
            cursorclass=pymysql.cursors.DictCursor,
        )
        self.cursor = self.connection.cursor()
        return self.connection, self.cursor

    def close_db_connection(self):
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
        print("[DBConfig] Database connection closed")

    def log_error_to_db(self, error_message):
        try:
            log_query = "INSERT INTO error_logs (log) VALUES (%s)"
            self.cursor.execute(log_query, (error_message,))
            self.connection.commit()
            print("[DBConfig] Logged error to `error_logs`.")
        except Exception as e:
            print(f"[DBConfig] Failed to log error to `error_logs`: {e}")
            self.connection.rollback()

    def select_from_db(self, table_name, columns="*", where=None, params=None, order_by=None, fetch_one=False, limit=None, offset=None):
        if isinstance(columns, list):
            columns = ", ".join(columns)

        query = f"SELECT {columns} FROM {table_name}"
        if where:
            query += f" WHERE {where}"
        if order_by:
            query += f" ORDER BY {order_by}"
        if limit:
            query += f" LIMIT {limit}"
        if offset:
            query += f" OFFSET {offset}"

        try:
            self.cursor.execute(query, params if params else ())
            result = self.cursor.fetchone() if fetch_one else self.cursor.fetchall()
            return result
        except Exception as e:
            print(f"[DBConfig] Error selecting from `{table_name}`: {e}")
            self.log_error_to_db(error_message=str(e))
            return None

    def select_custom_query(self, query, params=None, fetch_one=False):
        try:
            self.cursor.execute(query, params if params else ())
            result = self.cursor.fetchone() if fetch_one else self.cursor.fetchall()
            return result
        except Exception as e:
            print(f"[DBConfig] Error executing custom query: {e}")
            self.log_error_to_db(error_message=str(e))
            return None

    def insert_to_db(self, table_name, data_dict):
        keys = ", ".join(data_dict.keys())
        values = ", ".join(["%s"] * len(data_dict))
        insert_query = f"INSERT INTO {table_name} ({keys}) VALUES ({values})"

        try:
            self.cursor.execute(insert_query, tuple(data_dict.values()))
            self.connection.commit()
            print(f"[DBConfig] Inserted data into {table_name}.")
        except Exception as e:
            print(f"[DBConfig] Error inserting data into {table_name}: {e}")
            self.connection.rollback()
            self.log_error_to_db(error_message=str(e))

    def insert_to_db_return(self, table_name, data_dict) -> bool:
        keys = ", ".join(data_dict.keys())
        values = ", ".join(["%s"] * len(data_dict))
        insert_query = f"INSERT INTO {table_name} ({keys}) VALUES ({values})"

        try:
            self.cursor.execute(insert_query, tuple(data_dict.values()))
            self.connection.commit()
            print(f"[DBConfig] Inserted data into {table_name}.")
            return True
        except Exception as e:
            print(f"[DBConfig] Error inserting data into {table_name}: {e}")
            self.connection.rollback()
            self.log_error_to_db(error_message=str(e))
            return False

    def update_db(self, table_name, data_dict, where, params=None):
        set_clause = ", ".join([f"{key} = %s" for key in data_dict.keys()])
        update_query = f"UPDATE {table_name} SET {set_clause} WHERE {where}"

        try:
            self.cursor.execute(update_query, tuple(data_dict.values()) + (params if params else ()))
            self.connection.commit()
            print(f"[DBConfig] Updated data in {table_name}.")
        except Exception as e:
            print(f"[DBConfig] Error updating data in {table_name}: {e}")
            self.connection.rollback()
            self.log_error_to_db(error_message=str(e))

    def execute_manual_sql(self, sql: str, params=None, fetch: bool = False, fetch_one: bool = False):
        try:
            self.cursor.execute(sql, params if params else ())
            if fetch:
                return self.cursor.fetchone() if fetch_one else self.cursor.fetchall()
            self.connection.commit()
            print("[DBConfig] Executed manual SQL.")
            return self.cursor.rowcount
        except Exception as e:
            print(f"[DBConfig] Error executing manual SQL: {e}")
            self.connection.rollback()
            self.log_error_to_db(error_message=str(e))
            return None

    def execute_sql(self, sql: str, params=None, fetch: bool = False, fetch_one: bool = False):
        return self.execute_manual_sql(sql, params=params, fetch=fetch, fetch_one=fetch_one)

    def execute_many_manual_sql(self, sql: str, seq_params):
        try:
            self.cursor.executemany(sql, seq_params)
            self.connection.commit()
            print("[DBConfig] Executed bulk SQL.")
            return self.cursor.rowcount
        except Exception as e:
            print(f"[DBConfig] Error executing bulk SQL: {e}")
            self.connection.rollback()
            self.log_error_to_db(error_message=str(e))
            return None
