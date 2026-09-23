"""把一次真实运行录成回放 fixture。

用途：``REPLAY_MODE=off`` 下按真实链路跑一遍工作流，把每次 LLM / HTTP 请求与响应
落成 fixture JSON；之后即可用 ``REPLAY_MODE=fixture`` 在不接模型、不出网的前提下
复跑同一条链路。``replay.py`` 的未命中报错里点名的就是这个脚本。

两条硬约束：

1. **不许写生产。** 录制时强制 ``WRITEBACK_ENABLED=false``——回写节点只记录
   「本应发出的请求」而不发送。录制 fixture 是**读**操作，不产生任何生产副作用。
2. **真实客户数据不许进仓库。** 写盘前逐字段脱敏（手机号、密钥、X-API-Key），
   且默认输出到 ``tests/fixtures/replay/``（已被 ``.gitignore`` 忽略）。若要把某个
   fixture 入库，须先人工复核其内容不含个人信息。

用法：

    # 录主入口（默认工作流），号码取自 phone_numbers.txt
    python scripts/record_fixture.py --workflow customer_profile_entry --phone 13800000000

    # 指定输出
    python scripts/record_fixture.py --workflow moments_info \
        --input phone_number=13800000000 --out tests/fixtures/replay/moments.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.replay import build_fixture, http_key, llm_fingerprint  # noqa: E402
from customer_profile.runner import build_service  # noqa: E402
from customer_profile.settings import Settings  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "replay"
PHONE_FILE = REPO_ROOT / "phone_numbers.txt"

PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
"""中国大陆手机号。录制结果里出现它就意味着真实客户信息，必须掩码。"""

SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
"""API 密钥。台账提取器用的是同一形状（见 ledger/secrets_report.md）。"""

MASK = "***REDACTED***"


def sanitize_text(text: str) -> str:
    """对一段文本脱敏。手机号与密钥都换成掩码，形状保留以便核对。"""
    if not text:
        return text
    text = PHONE_PATTERN.sub(MASK, text)
    return SECRET_PATTERN.sub(MASK, text)


def sanitize_value(value: Any) -> Any:
    """递归脱敏任意 JSON 值。"""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {k: sanitize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_value(v) for v in value]
    return value


class FixtureRecorder:
    """收集一次运行里的全部外部响应，按回放键归档。

    LLM 的键是 ``llm_fingerprint(payload)``（模型 + messages 的 SHA256），
    HTTP 的键是 ``METHOD 服务相对路径``——与 ``HttpClient`` 留痕时用的寻址口径一致，
    因此 fixture 与具体端点无关，换环境不必重录。
    """

    def __init__(self, *, record_binary: bool = False) -> None:
        self.llm: dict[str, list[dict[str, Any]]] = {}
        self.http: dict[str, list[dict[str, Any]]] = {}
        self.record_binary = record_binary
        self.skipped: list[str] = []
        """未归档的尝试（失败、回放未命中、二进制未录制等）。如实报告，不伪造响应。"""

    async def __call__(
        self, node_ref: Any, attempt: Any, payload: Mapping[str, Any] | None = None
    ) -> None:
        if getattr(attempt, "status_code", None) is not None:
            self._record_http(attempt)
        else:
            self._record_llm(attempt, payload)

    def _record_http(self, attempt: Any) -> None:
        if not attempt.succeeded:
            self.skipped.append(
                f"http {attempt.method} {attempt.url}: {attempt.error or attempt.status_code}"
            )
            return
        key = http_key(attempt.method, attempt.url)
        frame: dict[str, Any] = {
            "status_code": attempt.status_code,
            "text": sanitize_text(attempt.response_text or ""),
            "headers": sanitize_value(dict(attempt.response_headers or {})),
        }
        # 二进制响应（图片/录音下载）必须按字节录。只录 ``text`` 的话，回放时会走
        # 「文本编码成 base64」那条路，产出的 data URI 与真实文件不一致
        # （PNG 头被改写），而 vision 节点拿它去调用只会报语焉不详的下载失败。
        # 图片属于客户资料，同样要脱敏——这里按「不含个人信息的二进制」处理：
        # 图片本身可能含人脸/手机号截图，因此默认**不录二进制**，除非显式开启。
        raw = getattr(attempt, "response_bytes", None)
        if raw and self.record_binary:
            import base64

            frame["bytes_base64"] = base64.b64encode(raw).decode("ascii")
        elif raw:
            self.skipped.append(
                f"http {attempt.method} {attempt.url}: 二进制响应 {len(raw)} 字节未录制"
                "（需 --record-binary；图片可能含客户信息，默认不录）"
            )
        self.http.setdefault(key, []).append(frame)

    def _record_llm(self, attempt: Any, payload: Mapping[str, Any] | None) -> None:
        if not attempt.succeeded or payload is None:
            reason = attempt.error or attempt.anomaly or "缺少请求载荷"
            self.skipped.append(f"llm attempt {attempt.attempt_no}: {reason}")
            return
        usage = attempt.usage.asdict() if attempt.usage is not None else {}
        self.llm.setdefault(llm_fingerprint(payload), []).append(
            {
                "text": sanitize_text(attempt.response_text or ""),
                "reasoning_content": sanitize_text(attempt.reasoning_content or ""),
                "usage": usage,
                "model": attempt.model,
            }
        )

    def as_fixture(self) -> dict[str, Any]:
        return build_fixture(llm=self.llm, http=self.http)


def read_first_phone() -> str:
    """取 ``phone_numbers.txt`` 的第一个号码（该文件已被忽略，不入库）。"""
    if not PHONE_FILE.is_file():
        raise SystemExit(f"缺少 {PHONE_FILE}——请先放入一个测试号码")
    for line in PHONE_FILE.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("#"):
            return candidate
    raise SystemExit(f"{PHONE_FILE} 中没有可用号码")


def parse_inputs(pairs: list[str]) -> dict[str, Any]:
    """把 ``k=v`` 列表转成入参字典。值一律按字符串处理（与 Dify 入参一致）。"""
    result: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"入参格式应为 key=value，实际为 {pair!r}")
        result[key.strip()] = value
    return result


async def record(
    workflow_id: str, inputs: dict[str, Any], out: Path, *, record_binary: bool = False
) -> int:
    # 录制一律不出生产回写：回写节点只产出「已跳过」的可解释结果
    settings = Settings(writeback_enabled=False)
    if settings.replay_mode != "off":
        raise SystemExit(
            f"REPLAY_MODE={settings.replay_mode!r}，录制要求 off（否则录到的是回放）"
        )
    recorder = FixtureRecorder(record_binary=record_binary)
    service = await build_service(settings, replay=None)
    service.llm_client._recorder = recorder
    service.http_client._recorder = recorder
    try:
        outcome = await service.run(workflow_id, inputs)
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(recorder.as_fixture(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"运行 {workflow_id}：{outcome.status}（{outcome.duration_ms} ms）")
    if outcome.error:
        print(f"  错误：{outcome.error}")
    print(f"  LLM 响应 {len(recorder.llm)} 个指纹，HTTP 响应 {len(recorder.http)} 个端点")
    print(f"  写出 {out}")
    if recorder.skipped:
        # 如实列出未归档的尝试：fixture 不完整时，回放会在同一处失败，
        # 提前说清楚比让人对着 ReplayMiss 猜要好。
        print(f"  未归档 {len(recorder.skipped)} 条尝试：")
        for item in recorder.skipped[:20]:
            print(f"    - {item}")
    return 0 if outcome.succeeded else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把一次真实运行录成回放 fixture")
    parser.add_argument("--workflow", default="customer_profile_entry", help="工作流 ID")
    parser.add_argument("--phone", default=None, help="手机号（缺省取 phone_numbers.txt 首个）")
    parser.add_argument(
        "--input", action="append", default=[], metavar="KEY=VALUE", help="附加入参，可重复"
    )
    parser.add_argument("--out", default=None, help="输出文件（缺省按工作流 ID 命名）")
    parser.add_argument(
        "--record-binary", action="store_true",
        help="录制二进制响应（图片/录音）。注意图片可能含客户信息，默认不录",
    )
    args = parser.parse_args(argv)

    inputs = parse_inputs(args.input)
    if args.phone:
        inputs.setdefault("phone_number", args.phone)
    elif "phone_number" not in inputs:
        inputs["phone_number"] = read_first_phone()
    inputs.setdefault("batch_id", "")

    out = Path(args.out) if args.out else DEFAULT_OUT / f"{args.workflow}.json"
    return asyncio.run(
        record(args.workflow, inputs, out, record_binary=args.record_binary)
    )


if __name__ == "__main__":
    raise SystemExit(main())
