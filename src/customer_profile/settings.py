"""运行配置。

端点和凭据来自环境变量（仓库根目录 `.env`，已被忽略）；节点的模型参数、提示词、
重试次数属工作流定义，随代码维护，不进这里（见 `.env.example` 的分工说明）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
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
    """每分钟请求数上限（RPM）。置空表示不限。**每个进程**的限额，见 worker_replicas。"""

    llm_tpm_limit: int | None = None
    """每分钟 token 数上限（TPM）。置空表示不限。按响应的 ``usage`` 事后校正。"""

    llm_rpm_burst: int | None = None
    llm_tpm_burst: int | None = None
    """令牌桶容量（突发额度）。置空时取对应的 limit。

    容量即「瞬时最多放行多少」。等于 limit 时相当于不允许突发；设得更大则允许短时
    超发、由后续限速补回。多数服务方的限额本身带一个小的突发窗口，因此不宜把它
    压成 1（那会让每个请求都排队等待，即使总额度远未用满）。
    """

    worker_replicas: int = 1
    """worker 进程数。**配额语义由它决定**（V0.5.3，规划 Q8 无明确数值时的显式口径）。

    限额是**进程内**的，因此 ``LLM_RPM_LIMIT`` 的含义是「每进程限额」，全局限额 =
    每进程 × 本值。这就是验收要求的「显式分配」：不共享，只按进程数划分。跨进程共享
    配额需要中间件，与「不引入未实际需要的重型框架」冲突（规划 §5 约束 7）。

    本值同时参与校验：限额必须能被进程数整除，否则会出现静默超发（如 RPM=60 配 4 个
    worker，实际打到 240）。
    """

    llm_temperature: float = 0.5
    """全项目统一，不可按节点覆盖（规划 §6.4.4）。"""

    # ------------------------------------------------------------ 外部服务
    auc_base_url: str = "https://openspeech.bytedance.com/api/v3/auc/bigmodel"
    """AUC 语音识别服务的基地址。

    原 DSL 指向内网自建服务 ``http://192.168.0.5:5005``（无鉴权）。该服务长期不可达，
    按用户 2026-09-23 决定改接火山引擎云服务。DSL 的 ``/submit_analyze`` 与
    ``/query_analyze`` 两个节点契约由 :mod:`customer_profile.execution.auc` 保持不变——
    下游的两个 code 节点是逐字符取自 DSL 的，不能改。
    """

    auc_ssl_verify: bool = True
    auc_api_key: str = ""
    """火山引擎控制台的 API Key。原以明文硬编码在一份未跟踪的 handler 文件里，
    按 ``secrets_report.md`` 的口径视为已暴露，建议轮换后填入。

    与 ``profile_api_key`` 等一致：只从 ``.env`` 读，定义里不出现。
    """

    auc_resource_id: str = "volc.seedasr.auc"
    """火山引擎的 ``X-Api-Resource-Id``。不是密钥，随代码维护。"""

    auc_poll_interval_s: float = 1.0
    """轮询间隔。云服务的处理在服务端进行，与本机读超时无关。"""

    auc_poll_max_attempts: int = 1800
    """轮询次数上限。每次间隔 1s，即默认 30 分钟封顶。

    缺省值对着「录音文件大、单次识别可达 20–30 分钟」这一实测口径（规划 §V0.3.2）。
    """

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
    max_active_runs: int | None = 4
    max_concurrent_requests: int | None = 8

    max_queued_runs: int | None = 50
    """待跑队列的深度上限（V0.5.2）。置空表示不限。

    语义变化（差异 W23）：v0.5.1 之前 ``POST /runs`` 的 429 来自「运行名额满」，
    进入队列模型后执行已不在 API 进程内，名额判断挪到 worker 侧；API 侧改为按
    **队列深度**拒绝。缺省 50 是保守值（规划 Q8 无数值），不是压测结论。
    """

    log_level: str = "INFO"

    data_dir: Path = PROJECT_ROOT / "data"
    fixture_dir: Path = PROJECT_ROOT / "tests" / "fixtures" / "replay"

    replay_mode: Literal["off", "fixture"] = "off"
    """``fixture`` 时 LLM 与 HTTP 全部走本地固定响应，不出网。

    测试与影子模式禁止生产回写（`Dify迁移任务说明.md` §9.3），固定响应模式下
    任何外部写请求都不会真的发出。
    """

    writeback_enabled: bool = False
    """是否真的发送生产回写请求（``POST /callback/update-profile``）。

    缺省 **false**：测试与影子模式禁止生产回写（`Dify迁移任务说明.md` §9.3）。
    置为 false 时回写节点只记录「本应发出的请求」并把结果标记为未发送，不产生任何
    生产副作用。真实冒烟也保持 false——V0.3 的验收目标是端到端**读**通，不含写。
    """

    replay_strict: bool = True
    """固定响应未命中时是否直接失败。默认严格，避免「静默走过」被误当成等价验证。"""

    serve_ui: bool = False
    """是否由后端托管前端构建产物（``frontend/dist``）。

    缺省 **false**：托管要求先跑过 ``npm run build``，而测试与无前端环境不应因此失败。
    置 true 时在 ``/`` 挂载静态资源，见 ``docs/specs/V0.4只读运行台.md`` §4。
    """

    interrupt_on_start: bool = True
    """启动时把上次进程遗留的 running 运行标记为 interrupted（§7.3）。

    **只标 ``running``，不标 ``queued``**（V0.5.2 修正）：``queued`` 是持久化队列里
    等待被领取的任务，把它们一并标成中断会让「worker 重启不丢任务」不成立。
    """

    # ------------------------------------------------------------ worker（V0.5.2）
    worker_poll_interval_ms: int = 1000
    """worker 轮询待跑队列的间隔。

    一次画像流程实测约 1282 s，秒级轮询的空转开销可忽略；不引入跨进程通知机制
    （那需要中间件，与「不引入未实际需要的重型框架」冲突）。
    """

    worker_lease_ms: int = 60_000
    """领取后的租约时长。worker 崩溃后租约到期，运行由 Store.requeue_expired_leases 退回队列。"""

    worker_heartbeat_ms: int = 20_000
    """续租心跳间隔。必须显著小于租约时长，否则长运行会被误判为「租约过期」而被跑第二遍。"""

    worker_id: str = ""
    """worker 标识。置空时按 ``主机名:pid`` 生成，用于区分「谁在跑这次运行」。"""

    inline_worker: bool = True
    """本进程是否**也**消费队列。

    缺省 true：API 进程同时充当一个消费者，这样「提交后立即可查、可跑」的既有契约
    不变——测试与单机部署都不需要额外起一个 worker 进程。生产按
    Dify迁移任务说明.md §4 单独跑 ``python -m customer_profile.worker`` 时，
    可把 API 侧置为 false，让执行集中在一个进程里。
    """

    sqlite_busy_timeout_ms: int = 5000
    """SQLite 写冲突的等待时长。API 与 worker 双进程共库后必需，否则写相遇会直接报 database is locked。"""

    @field_validator(
        "llm_rpm_limit",
        "llm_tpm_limit",
        "max_active_runs",
        "max_concurrent_requests",
        "max_queued_runs",
        "worker_poll_interval_ms",
        "worker_lease_ms",
        "worker_heartbeat_ms",
        "sqlite_busy_timeout_ms",
        "llm_rpm_burst",
        "llm_tpm_burst",
        mode="before",
    )
    @classmethod
    def _blank_means_unset(cls, value: Any) -> Any:
        """空串按「未设置」处理，回落到缺省值。

        ``.env`` 里 ``KEY=`` 是空串而不是缺失。对 ``int`` 字段，pydantic 会尝试把
        空串解析成整数并抛 ``ValidationError``，于是整个 ``Settings()`` 构造失败、
        服务根本装配不起来——一个空配置项能阻断全部运行。这里统一归一到缺省值：
        ``.env`` 的占位行不该成为开机失败的原因。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("worker_replicas")
    @classmethod
    def _replicas_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"worker 数至少为 1，实际为 {value}")
        return value

    @model_validator(mode="after")
    def _limits_split_evenly_across_workers(self) -> "Settings":
        """限额必须能被 worker 数整除。

        不校验的话，``LLM_RPM_LIMIT=60`` 配 4 个 worker 会变成实际 240——限额悄悄失效
        而没有任何提示，正是本版本要消灭的那类问题（RPM/TPM 此前是死配置）。宁可在
        启动时报错，也不要运行时静默超发。
        """
        replicas = self.worker_replicas
        if replicas <= 1:
            return self
        for name in ("llm_rpm_limit", "llm_tpm_limit"):
            limit = getattr(self, name)
            if limit is not None and limit % replicas != 0:
                raise ValueError(
                    f"{name.upper()}={limit} 不能被 WORKER_REPLICAS={replicas} 整除；"
                    "请调整为可整除的值，或改用单进程部署"
                )
        return self

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

    @property
    def resolved_worker_id(self) -> str:
        """本进程的 worker 标识。未显式配置时按 ``主机名:pid`` 生成。

        带 pid 是刻意的：同一台机器上手工起了第二个 worker 时，两者必须能被区分，
        否则 ``claimed_by`` 看不出「谁领走的」，租约归属也无从判断。
        """
        if self.worker_id.strip():
            return self.worker_id.strip()
        import os
        import socket

        try:
            host = socket.gethostname()
        except OSError:
            host = "unknown-host"
        return f"{host}:{os.getpid()}"

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
            "auc": self.auc_api_key,
            "profile": self.profile_api_key,
            "mhero": self.profile_mhero_api_key,
        }
        if service not in mapping:
            raise KeyError(f"未知的外部服务 {service!r}；已知：{sorted(mapping)}")
        return mapping[service]

    @property
    def per_process_rpm_limit(self) -> int | None:
        """单个 worker 进程应遵守的 RPM。多 worker 时为总额度除以进程数。"""
        if self.llm_rpm_limit is None:
            return None
        return self.llm_rpm_limit // max(1, self.worker_replicas)

    @property
    def per_process_tpm_limit(self) -> int | None:
        """单个 worker 进程应遵守的 TPM。"""
        if self.llm_tpm_limit is None:
            return None
        return self.llm_tpm_limit // max(1, self.worker_replicas)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试可用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()
