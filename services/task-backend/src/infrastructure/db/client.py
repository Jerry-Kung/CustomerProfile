from __future__ import annotations

import logging
import time

import mysql.connector
from mysql.connector import Error

from src.config.observability import elapsed_ms
from src.config.settings import MySQLConfig


_log = logging.getLogger(__name__)


class MySQLClient:
    def __init__(self, config: MySQLConfig):
        self._config = config

    def connect(self):
        start = time.perf_counter()
        try:
            conn = mysql.connector.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password,
                database=self._config.database,
                connection_timeout=5,
            )
        except Error:
            _log.exception(
                "mysql_connect_failed host=%s port=%s database=%s user=%s elapsed_ms=%s",
                self._config.host,
                self._config.port,
                self._config.database,
                self._config.user,
                elapsed_ms(start),
            )
            raise
        _log.debug(
            "mysql_connect_succeeded host=%s port=%s database=%s elapsed_ms=%s",
            self._config.host,
            self._config.port,
            self._config.database,
            elapsed_ms(start),
        )
        return conn

    def ping(self) -> bool:
        try:
            conn = self.connect()
            conn.close()
            return True
        except Error as exc:
            _log.warning(
                "mysql_healthcheck_failed host=%s port=%s database=%s error_type=%s",
                self._config.host,
                self._config.port,
                self._config.database,
                type(exc).__name__,
            )
            return False

