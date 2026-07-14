from __future__ import annotations

import mysql.connector
from mysql.connector import Error

from src.config.settings import MySQLConfig


class MySQLClient:
    def __init__(self, config: MySQLConfig):
        self._config = config

    def connect(self):
        return mysql.connector.connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            database=self._config.database,
            connection_timeout=5,
        )

    def ping(self) -> bool:
        try:
            conn = self.connect()
            conn.close()
            return True
        except Error:
            return False

