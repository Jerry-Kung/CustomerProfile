"""AUC 语音识别服务的可达性探测（临时运维脚本）。

**本脚本探测的端点已从内网自建服务改为火山引擎云服务**（2026-09-23）。

原先 ``192.168.0.5:5005`` 的内网服务长期不可达（TCP 超时），导致两个录音类工作流的
真实调用链路无法验证。现运行期实现是火山引擎 openspeech 云服务，由
``src/customer_profile/execution/auc.py`` 适配（见该模块说明）。

云服务与内网服务的协议不同，探测方式因此也不同：

- 云服务**必须在请求头带凭据**（``x-api-key`` / ``X-Api-Resource-Id``）；
- 处理状态在**响应头** ``X-Api-Status-Code``，不在响应体；
- 提交与查询都走 ``POST``，且都以 ``X-Api-Status-Code`` 判定成败。

三步探测（**不提交任何真实分析任务**）：

1. **TCP 连接**：能否建立到 host:port 的连接。不通时报告具体 errno 与可能原因。
2. **鉴权与协议探测**：对 ``/query`` 发一个**空查询体**。服务端会回
   ``45000000 cannot find task``——这正是**成功的信号**：它说明 DNS、TLS、
   凭据、资源 ID 四项全对，只是这个 task 不存在。若凭据错，会回 401/403。
3. **裁决**：按上面的证据给出「可用 / 部分可用 / 不可达」三档结论。

安全与副作用：

- 只发空查询体，不提交任何 file_url，因此不产生真实识别任务、不消耗额度。
- **不读取、不打印凭据值**——只报告「已配置 / 未配置」。
- 不写文件、不写数据库。

用法：

    python scripts/probe_auc.py                  # 探缺省 AUC 端点
    python scripts/probe_auc.py --all            # 连同 Notes/mhero 端点一起探
    python scripts/probe_auc.py --json           # 机器可读输出

退出码：0 = 可用；1 = 部分可用；2 = 不可达。
"""

from __future__ import annotations

import argparse
import errno
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

OTHER_ENDPOINTS = {
    "profile": "http://118.145.238.50:8084",
    "mhero": "https://mhero.dfmc.com.cn/customer_profile/m2m_api",
}

# 云服务的协议常量。与 execution/auc.py 保持一致。
OK = "20000000"
NOT_FOUND_TASK = "45000000"
"""凭据正确但 task 不存在。这是探测的**预期**结果。"""

AUTH_FAILED = frozenset({"401", "403"})


def load_auc_config() -> tuple[str, str, str]:
    """从 ``Settings`` 读 AUC 配置。返回 ``(base_url, api_key, resource_id)``。

    经 ``Settings`` 而非直接读 ``.env``：保证探测用的正是运行期那一份配置，
    配置项改名时这里会立刻失败，而不是悄悄探一个错的端点。
    """
    from customer_profile.settings import Settings

    settings = Settings()
    return settings.auc_base_url, settings.auc_api_key, settings.auc_resource_id


def split_url(url: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    base = f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    return host, port, base


def describe_socket_error(exc: BaseException) -> str:
    """把连接异常翻译成一句可执行的原因判断，而不是把栈丢给运维。"""
    if isinstance(exc, socket.timeout):
        return "连接超时（未收到 SYN 应答；可能被防火墙丢弃，或本地无出口）"
    if isinstance(exc, ConnectionRefusedError):
        return "连接被拒绝（主机在线但该端口无监听）"
    if isinstance(exc, socket.gaierror):
        return f"域名解析失败（{exc}）"
    if isinstance(exc, urllib.error.URLError) and isinstance(
        exc.reason, BaseException
    ):
        return describe_socket_error(exc.reason)
    if isinstance(exc, OSError):
        code = getattr(exc, "errno", None)
        if code == errno.ENETUNREACH:
            return "网络不可达（本机没有到该网段的路由）"
        if code == errno.EHOSTUNREACH:
            return "主机不可达（路由存在但主机无响应）"
        if code == errno.ETIMEDOUT:
            return "操作系统层超时"
        return f"系统错误 errno={code}：{exc}"
    return f"{type(exc).__name__}: {exc}"


def probe_tcp(host: str, port: int, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except BaseException as exc:  # 探测脚本要把任何失败都变成一行结论
        return {
            "step": "tcp",
            "ok": False,
            "target": f"{host}:{port}",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "detail": describe_socket_error(exc),
        }
    return {
        "step": "tcp",
        "ok": True,
        "target": f"{host}:{port}",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "detail": "TCP 连接建立成功",
    }


def classify_auc_code(code: str) -> tuple[bool, str]:
    """把 ``X-Api-Status-Code`` 翻译成一句结论。返回 ``(是否算可用, 说明)``。"""
    if code == NOT_FOUND_TASK:
        return True, (
            f"X-Api-Status-Code={code}（cannot find task）——"
            "**预期**结果：DNS/TLS/凭据/资源 ID 四项全对，只是该 task 不存在"
        )
    if code == OK:
        return True, f"X-Api-Status-Code={code}（OK）——服务正常应答"
    if code == "":
        return False, "响应里没有 X-Api-Status-Code 头——不像该云服务的应答"
    return False, f"X-Api-Status-Code={code}（X-Api-Message 见下）"


def probe_auc_query(
    base: str, api_key: str, resource_id: str, timeout: float
) -> dict[str, Any]:
    """对 ``/query`` 发一个空查询体。这是**不产生真实任务**的最小鉴权探测。"""
    url = f"{base}/query"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": "probe",
    }
    request = urllib.request.Request(
        url, data=json.dumps({}).encode("utf-8"), headers=headers, method="POST"
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            code = response.headers.get("X-Api-Status-Code", "")
            message = response.headers.get("X-Api-Message", "")
    except urllib.error.HTTPError as exc:
        # 401/403 也是「有应答」，且是明确的鉴权结论，不能当成连接失败
        status = int(exc.code)
        code = exc.headers.get("X-Api-Status-Code", "")
        message = exc.headers.get("X-Api-Message", "")
    except BaseException as exc:
        return {
            "step": "auc",
            "ok": False,
            "url": url,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "detail": describe_socket_error(exc),
        }

    ok, detail = classify_auc_code(code)
    if status in (401, 403) or code in AUTH_FAILED:
        ok = False
        detail = f"HTTP {status}：鉴权被拒——检查 AUC_API_KEY 与 AUC_RESOURCE_ID"
    return {
        "step": "auc",
        "ok": ok,
        "url": url,
        "status": status,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "detail": detail,
        "api_message": message,
    }


def probe_get(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
    except BaseException as exc:
        return {
            "step": "http",
            "ok": False,
            "url": url,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "detail": describe_socket_error(exc),
        }
    return {
        "step": "http",
        "ok": True,
        "url": url,
        "status": status,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "detail": f"HTTP {status}：收到应答",
    }


def verdict(records: list[dict[str, Any]]) -> tuple[int, str]:
    tcp = [r for r in records if r["step"] == "tcp"]
    auc = [r for r in records if r["step"] == "auc"]
    if not tcp or not tcp[0]["ok"]:
        return 2, "不可达：TCP 层就没通，网络路径或防火墙有问题"
    if not auc:
        return 1, "部分可用：端口通了，但未做 AUC 协议探测"
    if auc[0]["ok"]:
        return 0, "可用：云服务在线且凭据有效（空查询被正确拒绝为 cannot find task）"
    return 1, "部分可用：端口通了，但 AUC 协议探测未通过（多为凭据或资源 ID 不符）"


def render(records: list[dict[str, Any]], as_json: bool) -> str:
    if as_json:
        return json.dumps(records, ensure_ascii=False, indent=2)
    lines: list[str] = []
    for record in records:
        mark = "OK  " if record["ok"] else "FAIL"
        target = record.get("target") or record.get("url")
        lines.append(f"[{mark}] {target}  ({record['elapsed_ms']} ms)")
        lines.append(f"        {record['detail']}")
        if record.get("api_message"):
            lines.append(f"        X-Api-Message：{record['api_message']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AUC 云服务可达性探测")
    parser.add_argument("--timeout", type=float, default=15.0, help="每步超时秒数")
    parser.add_argument("--all", action="store_true", help="连同 Notes/mhero 端点一起探")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    base_url, api_key, resource_id = load_auc_config()
    host, port, base = split_url(base_url)

    print(f"AUC 端点：{base_url}")
    print(f"凭据    ：{'已配置' if api_key else '**未配置**（AUC_API_KEY 为空）'}")
    print(f"资源 ID ：{resource_id}")
    print()

    records: list[dict[str, Any]] = [probe_tcp(host, port, args.timeout)]
    if api_key:
        records.append(probe_auc_query(base, api_key, resource_id, args.timeout))
    else:
        records.append(
            {
                "step": "auc",
                "ok": False,
                "url": f"{base}/query",
                "elapsed_ms": 0,
                "detail": "跳过协议探测：未配置 AUC_API_KEY",
            }
        )

    per_endpoint: dict[str, list[dict[str, Any]]] = {"auc": records}
    if args.all:
        for name, url in OTHER_ENDPOINTS.items():
            per_endpoint[name] = [probe_tcp(*split_url(url)[:2], args.timeout), probe_get(url, args.timeout)]

    flat = [r for rs in per_endpoint.values() for r in rs]
    print(render(flat, args.json))

    code, conclusion = verdict(records)
    if args.json:
        return code

    print()
    for name, rs in per_endpoint.items():
        _, text = verdict(rs)
        print(f"[{name}] {text}")

    print()
    print("下一步：")
    if code == 0:
        print("  AUC 可用。跑一次真实录音链路（用云端可达的音频 URL）：")
        print("  python scripts/smoke_run.py --workflow audio_content_extract \\")
        print("      --input file_url=<云端可达的音频 URL> --read-timeout 1800")
    else:
        print("  未通过：核对 .env 的 AUC_API_KEY / AUC_RESOURCE_ID / AUC_BASE_URL。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
