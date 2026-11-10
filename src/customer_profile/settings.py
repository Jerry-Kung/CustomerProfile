"""运行配置。

端点、凭据、模型选择与 ASR 识别选项来自环境变量（仓库根目录 `.env`，已被忽略）；
提示词、统一 temperature 与重试策略随工作流代码维护（见 `.env.example`）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .workflow_scope import WorkflowScope

PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""仓库根目录（`src/customer_profile/settings.py` 上溯三层）。"""

DEFAULT_API_PORT = 18173
"""运行台 / API 的缺省端口。

由 8000 改来（V0.5.6）：8000 是各框架的开发端口，部署机上常被别的服务占用，
而原先它写死在 `main.py` 里，撞端口只能改代码。常量在此处定义是为了让字段缺省值与
「空值回落」两处引同一个数，不会各改一半。
"""


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
    llm_chat_completions_path: str = "/chat/completions"
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
    """worker 进程数。**配额语义由它决定**（V0.5.3）。

    限额是**进程内**的，因此 ``LLM_RPM_LIMIT`` 的含义是「每进程限额」，全局限额 =
    每进程 × 本值。这就是验收要求的「显式分配」：不共享，只按进程数划分。跨进程共享
    配额需要中间件，与「不引入未实际需要的重型框架」冲突。

    本值同时参与校验：限额必须能被进程数整除，否则会出现静默超发（如 RPM=60 配 4 个
    worker，实际打到 240）。
    """

    llm_temperature: float = 0.5
    """全项目统一，不可按节点覆盖。"""

    llm_enable_thinking: bool | None = None
    """是否启用模型的思维链（``LLM_ENABLE_THINKING``）。

    ``None``（未设置）时不发送思考参数，沿用服务端默认值；
    显式设置时按 ``resolved_llm_thinking_format`` 选择请求字段。
    """

    llm_thinking_format: Literal["auto", "enable_thinking", "thinking"] = "auto"
    """思考参数格式。auto 按接口域名识别火山，其他地址保持原 enable_thinking 格式。

    自建网关可显式设为 thinking 或 enable_thinking，不根据模型名猜测供应商。
    """

    @field_validator("llm_thinking_format", mode="before")
    @classmethod
    def _normalize_llm_thinking_format(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower() or "auto"
        return value

    @property
    def resolved_llm_thinking_format(self) -> Literal["enable_thinking", "thinking"]:
        """解析实际使用的参数格式；显式配置优先于域名识别。"""
        if self.llm_thinking_format != "auto":
            return self.llm_thinking_format
        hostname = (urlsplit(self.llm_base_url).hostname or "").lower()
        if hostname.startswith("ark.") and hostname.endswith(".volces.com"):
            return "thinking"
        return "enable_thinking"

    llm_max_tokens: int | None = None
    """单次响应的输出上限（``LLM_MAX_TOKENS``）。``None`` 时不发这个键，沿用服务端默认。

    部分服务在关闭思考、未指定上限时仅允许 8192 token，可能截断长 JSON。
    具体上限由供应商和模型决定，不能由上下文窗口大小推断。
    """

    # ------------------------------------------------------------ 运行台 / API（V0.5.6）
    api_host: str = "0.0.0.0"
    api_port: int = DEFAULT_API_PORT
    """运行台与 API 的监听地址与端口。

    **这两个值必须可配。** 原先 ``main.py`` 把端口写成 ``8000`` 字面量，与
    ``docker-compose.yml`` 的端口映射各写一份，部署机上 8000 被占用时只能改代码。
    缺省由 8000 改为 **18173**：8000 是各框架的开发端口，测试机上极易被别的服务占用。

    与 ``inbound_port``（8099）分开：那是**对外写面**，这里是**内部只读面**。
    容器部署时宿主机映射与健康检查都跟着本值走（compose 用 ``${API_PORT:-18173}``）。
    """

    @field_validator("api_port", mode="before")
    @classmethod
    def _api_port_blank_means_default(cls, value: Any) -> Any:
        """``API_PORT=`` 留空时回落到缺省端口。

        不能复用 ``_blank_means_unset``：那个把空串归一成 ``None``，而本字段是纯 ``int``，
        于是 ``Settings()`` 直接构造失败——一个空配置行就能阻断开机。部署侧从
        ``.env.example`` 复制模板后最容易留下的就是这种空行，宁可按缺省值起，也不要起不来。
        写成 ``str`` 分支是为了兼容 ``.env``（空串）与 compose（``${API_PORT:-…}``）。
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return DEFAULT_API_PORT
        return value

    # ------------------------------------------------------------ 外部服务
    auc_base_url: str = "https://openspeech.bytedance.com/api/v3/auc/bigmodel"
    """AUC 语音识别服务的基地址。

    迁移前的定义指向内网自建服务 ``http://192.168.0.5:5005``（无鉴权）。该服务长期不可达，
    按用户 2026-09-23 决定改接火山引擎云服务。迁移前的定义的 ``/submit_analyze`` 与
    ``/query_analyze`` 两个节点契约由 :mod:`customer_profile.execution.auc` 保持不变——
    下游的两个 code 节点是逐字符取自迁移前的定义的，不能改。
    """

    auc_ssl_verify: bool = True
    auc_api_key: str = ""
    """火山引擎控制台的 API Key。原以明文硬编码在一份未跟踪的 handler 文件里，
    按 `归档的密钥盘点` 的口径视为已暴露，建议轮换后填入。

    与 ``profile_api_key`` 等一致：只从 ``.env`` 读，定义里不出现。
    """

    auc_resource_id: str = "volc.seedasr.auc"
    """火山引擎的 ``X-Api-Resource-Id``，可随部署环境配置。"""

    auc_submit_path: str = "/submit"
    auc_query_path: str = "/query"
    """相对于 ``auc_base_url`` 的外部接口路径；对内节点契约保持不变。"""

    auc_model_name: str = "bigmodel"
    auc_model_version: str | None = "400"
    auc_ssd_version: str | None = "200"
    """版本留空时不发送对应字段，采用服务端默认版本。"""

    auc_enable_channel_split: bool = True
    auc_enable_ddc: bool = True
    auc_enable_speaker_info: bool = True
    auc_enable_punc: bool = True
    auc_enable_itn: bool = True
    auc_show_speech_rate: bool = True
    auc_show_volume: bool = True
    auc_enable_emotion_detection: bool = True
    auc_enable_gender_detection: bool = True
    auc_corpus_correct_table_name: str = ""
    auc_corpus_context: str = ""

    @field_validator("llm_chat_completions_path", "auc_submit_path", "auc_query_path")
    @classmethod
    def _relative_service_path(cls, value: str) -> str:
        """只允许同一服务基地址下的路径，防止把鉴权信息发到配置外的主机。"""
        value = value.strip()
        parts = urlsplit(value)
        if (
            not value.startswith("/")
            or value.startswith("//")
            or parts.scheme
            or parts.netloc
            or parts.query
            or parts.fragment
        ):
            raise ValueError("接口路径必须以 / 开头，且不能包含主机、查询参数或片段")
        return value

    @field_validator("auc_model_name")
    @classmethod
    def _auc_model_name_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("AUC_MODEL_NAME 不能为空")
        return value

    @field_validator("auc_model_version", "auc_ssd_version", mode="before")
    @classmethod
    def _optional_auc_version(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    auc_poll_interval_s: float = 1.0
    """轮询间隔。云服务的处理在服务端进行，与本机读超时无关。"""

    auc_poll_max_attempts: int = 1800
    """轮询次数上限。每次间隔 1s，即默认 30 分钟封顶。

    缺省值对着「录音文件大、单次识别可达 20–30 分钟」这一实测口径。
    """

    profile_api_base_url: str = "https://mhero.dfmc.com.cn/customer_profile/m2m_api"
    """取数与回写的服务根地址。**以 ``.env`` 为准**，这里只是兜底。

    2026-11-02 起缺省值指向 mhero：旧地址 ``118.145.238.50:8084`` 随接口改版弃用，
    对它取数会返回 ``200 []``——看起来像「该客户无资料」，实际是打到了死端点。
    """
    profile_api_key: str = ""
    mhero_base_url: str = "https://mhero.dfmc.com.cn/customer_profile/m2m_api"
    profile_mhero_api_key: str = ""

    # ------------------------------------------------------------ 对外任务接口（V0.5.5）
    inbound_api_key: str = ""
    """对外任务接口（``python -m customer_profile.inbound``）的**入站**密钥，请求头 ``X-API-Key``。

    **独立于 ``profile_api_key``，不复用。** 那个是我们主动调用生产回写服务的**出站**凭据；
    同一个值身兼两用，会把「谁能调我们的接口」与「我们能写生产库」并成同一个泄露面。

    置空表示**关闭接口**——全部请求返回 503，而不是关闭校验。空密钥若被当作「不校验」，
    这个端口就是任何人可写的入口：一条外部请求会变成一次真实算力消耗与一次生产回写。
    """

    inbound_host: str = "0.0.0.0"
    inbound_port: int = 8099
    """对外任务接口的监听地址与端口。与运行台的 ``api_port`` 分开：那是内部只读面，这是对外写面。"""

    # ------------------------------------------------------------ HTTP 策略
    http_connect_timeout: float = 10.0
    http_read_timeout: float = 600.0
    http_write_timeout: float = 600.0
    http_retry_enabled: bool = True
    http_retry_max: int = 3
    http_retry_interval_ms: int = 500

    # ------------------------------------------------------------ 执行器
    database_url: str = "sqlite:///./data/customer_profile.db"
    workflow_scope: WorkflowScope = "all"
    """本进程接收、领取和恢复的主任务范围；all 仅用于兼容本地单进程运行。"""

    worker_concurrency: int | None = Field(default=1, ge=1)
    """本地兼容模式的任务上限；None 时按已派发任务自动执行，业务并发由 backend 控制。"""

    max_active_runs: int | None = 4
    max_concurrent_requests: int | None = 8

    max_queued_runs: int | None = 50
    """待跑队列的深度上限（V0.5.2）。置空表示不限。

    语义变化：v0.5.1 之前 ``POST /runs`` 的 429 来自「运行名额满」，
    进入队列模型后执行已不在 API 进程内，名额判断挪到 worker 侧；API 侧改为按
    **队列深度**拒绝。缺省 50 是保守值，不是压测结论。
    """

    log_level: str = "INFO"

    data_dir: Path = PROJECT_ROOT / "data"
    fixture_dir: Path = PROJECT_ROOT / "tests" / "fixtures" / "replay"

    replay_mode: Literal["off", "fixture"] = "off"
    """``fixture`` 时 LLM 与 HTTP 全部走本地固定响应，不出网。

    测试与影子模式禁止生产回写，固定响应模式下
    任何外部写请求都不会真的发出。
    """

    writeback_enabled: bool = False
    """是否真的发送生产回写请求（``POST /callback/update-profile``）。

    缺省 **false**：测试与影子模式禁止生产回写。
    置为 false 时回写节点只记录「本应发出的请求」并把结果标记为未发送，不产生任何
    生产副作用。真实冒烟也保持 false——V0.3 的验收目标是端到端**读**通，不含写。
    """

    incremental_enabled: bool = False
    """是否启用增量画像入口（``INCREMENTAL_ENABLED``）。

    缺省 **false**：关闭时入口仍是 ``customer_profile_pipeline``，产物与接入增量前
    逐字节一致，也不写基线。开启后入口解析为 ``customer_profile_incremental``，
    由它按档位分流（P1 只有 T0 / T3 两档）。

    **前后端必须同改**：前端 ``SubmitRun.tsx`` 里的入口常量决定提交页写哪个 ID，
    这里决定入参校验与入站接口认哪个 ID。只改一侧会让手机号校验或批次号补全静默失效。
    """

    incremental_tiers: str = "T0,T1,T2"
    """增量入口放行哪些档位（``INCREMENTAL_TIERS``，逗号分隔，缺省 ``T0,T1,T2``）。

    **灰度与回滚的唯一手段**（``03`` T2-7）：把 ``T1,T2`` 摘掉即回到「判据命中后仍落
    T3」，不需要改代码、也不需要重启改图。空串按缺省值处理——配置写空不该让增量停摆。
    """

    baseline_enabled: bool = True
    """是否在运行成功后写回传体基线（``BASELINE_ENABLED``）。

    缺省 **true**，但只在 ``incremental_enabled`` 打开时生效——没有增量入口就没有
    读基线的地方，白写一表业务数据没有意义。

    **回写关闭时也写**（``WRITEBACK_ENABLED=false``）：基线描述的是「我们算出来的
    上一版」，不是「后台收到的那份」（``03`` T1-2）。
    """

    replay_strict: bool = True
    """固定响应未命中时是否直接失败。默认严格，避免「静默走过」被误当成等价验证。"""

    # ------------------------------------------------------------ 规则层
    rules_disabled: str = ""
    """禁用的规则编号，逗号分隔（``RULES_DISABLED=INCOME-01,EXPR-01``）。

    **单条规则的回滚开关**：加一个编号即摘除该规则，不整批回滚提示词。未登记的编号
    被忽略——灰度配置比代码先改是常态，配置里残留一个已退役的编号不该阻断运行。
    用逗号串而不是列表：环境变量里的列表要写成 JSON，运维侧容易写错。
    """

    sort_tier_first: bool = True
    """冲突消解排序是否**来源档优先**（``SORT_TIER_FIRST``，缺省 true）。

    用户口径（2026-10-30）：多来源冲突时按「客户原话 > 销售描述 > 第三方标签」取舍，
    **来源档位排在置信档之前**。关闭即回到旧序 ``(basis, 来源档)``——旧序下
    「第三方预测 + 自称 fact」会被抬成 ``inferred_high``，于是**任何一手断言**（哪怕只标
    ``inferred_low``）都输给它，业务方报障的「无车」正是这样胜出。

    留这个开关是因为一手断言可能来自**说话方误判**（录音里销售方说的被当成客户说的），
    全部放开需要真实批次验证；关掉即单批回滚，不必改代码。
    """

    rule_checker_enabled: bool = False
    """``llm_checker`` 通道总开关。

    缺省 **false**：checker 是新增的 LLM 调用，先离线跑通再开。关闭时规则层的
    code / prompt 通道照常工作，checker 动作集为空。
    """

    enum_mapping_enabled: bool = False
    """枚举语义映射总开关（``ENUM_MAPPING_ENABLED``）。

    缺省 **false**：关闭时投影层之后**不发起**映射调用，产物与启用前逐字节一致。
    开启后，L1 给出的、归不到契约合法键的中文自由文本（「已婚有子女」「分期」）会先经
    一次批量 LLM 映射落到合法键，再进 gate 与回写。

    与 ``rule_checker_enabled`` 分开是刻意的：两者方向相反（checker 只判不补，映射只补
    不判），成本模型与风险面都不同，用一个开关会把「想省钱」与「想救字段」绑死。
    """

    # ------------------------------------------------------------ 知识库
    knowledge_pack: str = "dfms-mengshi"
    """当前生效的知识包 ID（``KNOWLEDGE_PACK``）。

    **换合作伙伴只改这一处**（外加把新包放进 ``knowledge/packs/``）。包里的品牌、
    在售车型、价格与卖点全部随包切换，代码与提示词不动。

    置空串表示**不启用知识**：视图输出空串、意向车型闭集校验跳过，管线行为与接入
    知识前逐字节一致。这是刻意留的回滚口——比代码开关可靠，开关会腐烂，数据不会。
    """

    knowledge_packs_dir: str = ""
    """知识包根目录（``KNOWLEDGE_PACKS_DIR``）。空表示用包内缺省目录
    ``src/customer_profile/knowledge/packs/``。

    存在的意义是测试注入临时包：无需改动仓库内的正式知识文件，也避免两个测试
    共用同一个 ``pack_id`` 时互相污染。
    """

    knowledge_competitors: str = ""
    """当前生效的竞品库 ID（``KNOWLEDGE_COMPETITORS``）。**缺省空串 = 不启用。**

    缺省为空不是疏漏：竞品情报需要业务方提供资料来源（见
    ``docs/feature-knowledge-base/07`` Q8），目前尚未到位。空串的产物与「没有这个
    模块」逐字节一致，因此骨架可以先落地，等资料到了只改这一处配置。

    竞品库与知识包**分开配置**，理由见 :mod:`customer_profile.knowledge.competitors`：
    它描述「别人家有什么」，变化快于本方资料，且换合作伙伴时不该跟着换。
    """

    knowledge_competitors_dir: str = ""
    """竞品库根目录（``KNOWLEDGE_COMPETITORS_DIR``）。空表示用包内缺省目录
    ``src/customer_profile/knowledge/competitors/``。与 ``knowledge_packs_dir`` 同理，
    存在的意义是测试注入临时库。"""

    @property
    def disabled_rule_ids(self) -> tuple[str, ...]:
        """``rules_disabled`` 的解析结果（去空白、去空项）。"""
        return tuple(
            item.strip() for item in self.rules_disabled.split(",") if item.strip()
        )

    serve_ui: bool = False
    """是否由后端托管前端构建产物（``frontend/dist``）。

    缺省 **false**：托管要求先跑过 ``npm run build``，而测试与无前端环境不应因此失败。
    置 true 时在 ``/`` 挂载静态资源，见 `归档的运行台设计` §4。
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
    单独跑 ``python -m customer_profile.worker`` 时，
    可把 API 侧置为 false，让执行集中在一个进程里。
    """

    sqlite_busy_timeout_ms: int = 5000
    """SQLite 写冲突的等待时长。API 与 worker 双进程共库后必需，否则写相遇会直接报 database is locked。"""

    # ------------------------------------------------------------ 留痕存储瘦身
    trace_persist_mode: Literal["high", "low"] = "high"
    """留痕运行时数据的留存档位（``TRACE_PERSIST_MODE``）。

    - ``high``：包内保留节点输入输出与完整请求体；
    - ``low``：只留外部响应原文、根输出、``pipeline_l0_records`` 的原样输入与诊断切片。

    **只接受这两个值。** ``none`` 与任何拼写错误都直接拒绝启动，不回落缺省——
    「以为开了 low 其实还在存全量」和「以为存了其实什么都没有」都是最难查的一类问题。
    方案稿里的 ``none`` 档本轮未实现，配置层明确拒绝而不是静默当成别的东西。
    """

    trace_archive_dir: Path | None = None
    """运行时数据包（``*.json.gz``）的存放目录。

    缺省 ``<DATA_DIR>/trace-archives``。**必须落在持久卷内**：容器重建后包就没了，
    而库里那部分载荷在打包成功后已被清空。"""

    trace_retention_days: int = 30
    """包文件的保留天数。骨架（运行、节点、尝试的非载荷列）永久保留。"""

    trace_archive_compress_level: int = 6
    """包内 gzip 压缩级别。缺省 6 是体积与耗时的折中。"""

    @field_validator("trace_persist_mode", mode="before")
    @classmethod
    def _validate_trace_mode(cls, value: Any) -> str:
        """非法档位拒绝启动；旧的布尔开关映射到新档位。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            return "high"
        text = str(value).strip().lower()
        if text in ("high", "low"):
            return text
        if text == "none":
            raise ValueError(
                "TRACE_PERSIST_MODE=none 本轮未实现；请使用 high 或 low"
            )
        raise ValueError(
            f"未知的 TRACE_PERSIST_MODE={value!r}；只接受 high / low"
            "（不静默回落，避免「以为换了档位其实没换」）"
        )

    @model_validator(mode="before")
    @classmethod
    def _legacy_persist_flag(cls, data: Any) -> Any:
        """兼容旧的 ``TRACE_PERSIST_ENABLED`` 布尔开关：true → high，false → low。

        两者同时给出时以 ``TRACE_PERSIST_MODE`` 为准。旧变量只是过渡，不写进文档的
        配置表，免得后来的人以为它还能长期使用。
        """
        if not isinstance(data, Mapping):
            return data
        if data.get("trace_persist_mode"):
            return data
        legacy = data.get("trace_persist_enabled")
        if legacy is None:
            return data
        text = str(legacy).strip().lower()
        if text in ("1", "true", "yes", "on"):
            merged = dict(data)
            merged["trace_persist_mode"] = "high"
            return merged
        if text in ("0", "false", "no", "off"):
            merged = dict(data)
            merged["trace_persist_mode"] = "low"
            return merged
        return data

    @property
    def trace_archive_path(self) -> Path:
        """包目录的解析结果：显式配置优先，否则落在数据目录下。"""
        base = self.trace_archive_dir
        return Path(base) if base else self.data_dir / "trace-archives"

    @field_validator(
        "llm_rpm_limit",
        "llm_tpm_limit",
        "max_active_runs",
        "worker_concurrency",
        "max_concurrent_requests",
        "max_queued_runs",
        "worker_poll_interval_ms",
        "worker_lease_ms",
        "worker_heartbeat_ms",
        "sqlite_busy_timeout_ms",
        "llm_rpm_burst",
        "llm_tpm_burst",
        "llm_enable_thinking",
        "llm_max_tokens",
        "api_port",
        "inbound_port",
        "trace_archive_dir",
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
                "temperature 全项目统一为 0.5；"
                "如需变更请先修改配置并记入 ``docs/design/执行语义口径.md``"
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
        self.trace_archive_path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试可用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()
