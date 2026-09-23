"""运行配置。

端点和凭据来自环境变量（仓库根目录 `.env`，已被忽略）；节点的模型参数、提示词、
重试次数属工作流定义，随代码维护，不进这里（见 `.env.example` 的分工说明）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""仓库根目录（`src/customer_profile/settings.py` 上溯三层）。"""


class Settings(BaseSettings):
    """全部运行期配置。缺省值取向：保守、可解释、不静默失败。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------ LLM
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_rpm_limit: int | None = None
    llm_tpm_limit: int | None = None

    llm_temperature: float = 0.5
    """全项目统一，不可按节点覆盖（规划 §6.4.4）。"""

    # ------------------------------------------------------------ 外部服务
    auc_base_url: str = "http://192.168.0.5:5005"
    auc_ssl_verify: bool = False
    profile_api_base_url: str = "http://118.145.238.50:8084"
    profile_api_key: str = ""
    mhero_base_url: str = "https://mhero.dfmc.com.cn/customer_profile/m2m_api"
    profile_mhero_api_key: str = ""

    # ------------------------------------------------------------ HTTP 策略
    http_connect_timeout: float = 10.0
    http_read_timeout: float = 600.0
    http_write_timeout: float = 600.0
    http_retry_enabled: bool = True
    http_retry_max: int = 3
    http_retry_interval_ms: int = 500

    # ------------------------------------------------------------ 执行器
    database_url: str = "sqlite:///./data/customer_profile.db"
    max_active_runs: int = 4
    max_concurrent_requests: int = 8
    log_level: str = "INFO"

    data_dir: Path = PROJECT_ROOT / "data"
    fixture_dir: Path = PROJECT_ROOT / "tests" / "fixtures" / "replay"

    replay_mode: Literal["off", "fixture"] = "off"
    """``fixture`` 时 LLM 与 HTTP 全部走本地固定响应，不出网。

    测试与影子模式禁止生产回写（`Dify迁移任务说明.md` §9.3），固定响应模式下
    任何外部写请求都不会真的发出。
    """

    replay_strict: bool = True
    """固定响应未命中时是否直接失败。默认严格，避免「静默走过」被误当成等价验证。"""

    interrupt_on_start: bool = True
    """启动时把上次进程遗留的 running 运行标记为 interrupted（§7.3）。"""

    @field_validator("llm_temperature")
    @classmethod
    def _only_unified_temperature(cls, value: float) -> float:
        if value != 0.5:
            raise ValueError(
                "temperature 全项目统一为 0.5（规划 §6.4.4）；"
                "如需变更请先修改规划文档并记入差异清单"
            )
        return value

    @property
    def sqlite_path(self) -> Path | None:
        """``sqlite:///`` 形式的数据库文件路径；非 SQLite 时返回 ``None``。"""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        raw = self.database_url[len(prefix) :]
        path = Path(raw)
        return path if path.is_absolute() else (PROJECT_ROOT / path)

    @property
    def is_replaying(self) -> bool:
        return self.replay_mode == "fixture"

    def base_url_for(self, service: str) -> str:
        """按服务名取外部端点。服务名对应节点配置里的 ``@service``。"""
        mapping = {
            "auc": self.auc_base_url,
            "profile": self.profile_api_base_url,
            "mhero": self.mhero_base_url,
        }
        try:
            return mapping[service].rstrip("/")
        except KeyError as exc:
            raise KeyError(
                f"未知的外部服务 {service!r}；已知：{sorted(mapping)}"
            ) from exc

    def api_key_for(self, service: str) -> str:
        mapping = {
            "auc": "",
            "profile": self.profile_api_key,
            "mhero": self.profile_mhero_api_key,
        }
        if service not in mapping:
            raise KeyError(f"未知的外部服务 {service!r}；已知：{sorted(mapping)}")
        return mapping[service]

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试可用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()
