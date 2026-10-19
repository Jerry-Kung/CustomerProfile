"""``INBOUND_API_KEY`` 生成脚本：形状、落盘、拒绝覆盖、行尾保持。

被测对象是一组纯函数加一个命令行入口（``tmp_path`` 上的 ``.env``），不碰数据库、
不出网。用例一律显式传 ``--env-file``：默认目标是仓库里那份真实的 ``.env``，
漏传的用例会静默改写真实凭据。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from .conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_inbound_key as gik  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "gen_inbound_key.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# ---------------------------------------------------------------- 密钥形状


def test_generated_key_matches_its_own_pattern():
    key = gik.generate_key()
    assert gik.key_pattern().fullmatch(key)
    assert key.startswith("inb_")
    # 32 字节 → 64 个十六进制字符
    assert len(key) == len("inb_") + 64


def test_generated_keys_are_distinct():
    """同一进程内连续生成不重复。

    随机源被换成一个退化实现时（如固定种子），这条会立刻失败——而重复的密钥意味着
    「换了密钥」这件事在两端都没发生。
    """
    keys = {gik.generate_key() for _ in range(50)}
    assert len(keys) == 50


def test_key_is_hex_so_it_survives_being_pasted():
    """十六进制：不含 ``+`` ``/`` ``-`` ``_`` 与大小写混用，手工传递不会被改写。"""
    key = gik.generate_key()
    body = key[len("inb_") :]
    assert set(body) <= set("0123456789abcdef")


@pytest.mark.parametrize("nbytes", [0, 8, 15, 65])
def test_out_of_range_byte_counts_are_refused(nbytes):
    """低于 128 位或高得离谱的长度都拒绝，不静默取个「差不多的」值。"""
    with pytest.raises(ValueError):
        gik.generate_key(nbytes)


def test_custom_prefix_is_honoured():
    key = gik.generate_key(16, "test_")
    assert gik.key_pattern(16, "test_").fullmatch(key)


# ---------------------------------------------------------------- 读取


def test_read_returns_none_when_file_missing(tmp_path):
    assert gik.read_env_key(tmp_path / "nope.env") is None


def test_read_returns_none_for_missing_empty_or_commented_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OTHER=1\nINBOUND_API_KEY=\n# INBOUND_API_KEY=abc\n", encoding="utf-8")
    assert gik.read_env_key(path) is None


def test_read_finds_key_despite_spacing_and_case(tmp_path):
    path = tmp_path / ".env"
    path.write_text("inbound_api_key = abc123\n", encoding="utf-8")
    assert gik.read_env_key(path) == "abc123"


# ---------------------------------------------------------------- 写入


def test_write_creates_the_file_when_absent(tmp_path):
    path = tmp_path / ".env"
    key = "inb_" + "a1" * 32
    assert gik.write_env_key(path, key) == "created"
    assert path.read_bytes() == f"INBOUND_API_KEY={key}\n".encode()


def test_write_creates_missing_parent_directory(tmp_path):
    path = tmp_path / "nested" / "deeper" / ".env"
    gik.write_env_key(path, "inb_" + "b2" * 32)
    assert path.is_file()


def test_write_replaces_only_its_own_line(tmp_path):
    """其余行逐字节回写：换密钥不该产出与本次改动无关的 diff。"""
    path = tmp_path / ".env"
    path.write_bytes(
        "# comment\nPROFILE_API_KEY=keep-me\nINBOUND_API_KEY=old\nLLM_MODEL=m\n".encode()
    )
    assert gik.write_env_key(path, "inb_new", force=True) == "updated"
    assert path.read_bytes() == (
        b"# comment\nPROFILE_API_KEY=keep-me\nINBOUND_API_KEY=inb_new\nLLM_MODEL=m\n"
    )


def test_write_appends_when_key_is_absent(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"PROFILE_API_KEY=keep-me\nLLM_MODEL=m\n")
    assert gik.write_env_key(path, "inb_new") == "appended"
    assert path.read_bytes() == (
        b"PROFILE_API_KEY=keep-me\nLLM_MODEL=m\nINBOUND_API_KEY=inb_new\n"
    )


def test_write_appends_after_a_missing_final_newline(tmp_path):
    """末尾没有换行时不能把新键粘到最后一个值后面（那会造出一个谁也读不到的键）。"""
    path = tmp_path / ".env"
    path.write_bytes(b"LLM_MODEL=m")
    assert gik.write_env_key(path, "inb_new") == "appended"
    assert path.read_bytes() == b"LLM_MODEL=m\nINBOUND_API_KEY=inb_new\n"


def test_write_preserves_crlf_line_endings(tmp_path):
    """Windows 上 ``.env`` 常见 CRLF。整份重写若统一成 LF，每次换密钥都留下一整份 diff。"""
    path = tmp_path / ".env"
    path.write_bytes(b"A=1\r\nINBOUND_API_KEY=old\r\nB=2\r\n")
    gik.write_env_key(path, "inb_new", force=True)
    assert path.read_bytes() == b"A=1\r\nINBOUND_API_KEY=inb_new\r\nB=2\r\n"


def test_write_refuses_to_overwrite_an_existing_value(tmp_path):
    """换密钥要两端同时动作——对端还拿着旧值时，它的请求会全部 403。

    拒绝时抛出的异常**不得带上旧值**：它会被打到日志里。
    """
    path = tmp_path / ".env"
    path.write_bytes(b"INBOUND_API_KEY=old-secret\n")
    with pytest.raises(gik.KeyAlreadySet) as excinfo:
        gik.write_env_key(path, "inb_new")
    assert "old-secret" not in str(excinfo.value)
    assert path.read_bytes() == b"INBOUND_API_KEY=old-secret\n"


def test_force_overwrites(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"INBOUND_API_KEY=old-secret\n")
    assert gik.write_env_key(path, "inb_new", force=True) == "updated"
    assert path.read_bytes() == b"INBOUND_API_KEY=inb_new\n"


# ---------------------------------------------------------------- 命令行


def test_cli_prints_a_key_and_touches_nothing(tmp_path):
    """默认只打印。不传 ``--write`` 时不得创建或改写任何文件。"""
    env_path = tmp_path / ".env"
    result = _run("--env-file", str(env_path))
    assert result.returncode == 0
    assert not env_path.exists()
    printed = result.stdout.splitlines()[0]
    assert gik.key_pattern().fullmatch(printed)


def test_cli_write_then_refuse_then_force(tmp_path):
    env_path = tmp_path / ".env"

    first = _run("--env-file", str(env_path), "--write")
    assert first.returncode == 0
    first_key = first.stdout.splitlines()[0]
    assert gik.read_env_key(env_path) == first_key

    second = _run("--env-file", str(env_path), "--write")
    assert second.returncode == 1
    assert "refused" in second.stderr
    assert gik.read_env_key(env_path) == first_key  # 值没被动过

    third = _run("--env-file", str(env_path), "--write", "--force")
    assert third.returncode == 0
    third_key = third.stdout.splitlines()[0]
    assert third_key != first_key
    assert gik.read_env_key(env_path) == third_key


def test_cli_refusal_does_not_echo_the_old_key(tmp_path):
    """拒绝信息里不得出现旧密钥——它会被贴进工单或聊天窗口。"""
    env_path = tmp_path / ".env"
    env_path.write_text("INBOUND_API_KEY=super-secret-old\n", encoding="utf-8")
    result = _run("--env-file", str(env_path), "--write")
    assert result.returncode == 1
    assert "super-secret-old" not in result.stdout
    assert "super-secret-old" not in result.stderr


def test_cli_force_without_write_is_a_usage_error(tmp_path):
    """``--force`` 单独用没有意义：它只描述「允许覆盖」，而覆盖只在写入时发生。"""
    result = _run("--env-file", str(tmp_path / ".env"), "--force")
    assert result.returncode == 2
    assert not (tmp_path / ".env").exists()


def test_cli_rejects_an_out_of_range_byte_count(tmp_path):
    result = _run("--env-file", str(tmp_path / ".env"), "--bytes", "4")
    assert result.returncode == 2


def test_cli_json_output_is_ascii_only(tmp_path):
    """``--json`` 输出全 ASCII：控制台代码页不是 UTF-8 时也不会乱码。"""
    result = _run("--env-file", str(tmp_path / ".env"), "--json")
    assert result.returncode == 0
    result.stdout.encode("ascii")
    payload = json.loads(result.stdout)
    assert gik.key_pattern().fullmatch(payload["key"])
    assert payload["env_name"] == "INBOUND_API_KEY"
    assert payload["written"] is None


def test_cli_json_reports_what_it_did(tmp_path):
    env_path = tmp_path / ".env"
    result = _run("--env-file", str(env_path), "--write", "--json")
    payload = json.loads(result.stdout)
    assert payload["written"] == "created"
    assert gik.read_env_key(env_path) == payload["key"]


def test_cli_json_refusal_exits_nonzero(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("INBOUND_API_KEY=super-secret-old\n", encoding="utf-8")
    result = _run("--env-file", str(env_path), "--write", "--json")
    assert result.returncode == 1
    assert "super-secret-old" not in result.stdout
    assert result.stdout.strip() == ""


def test_script_never_touches_the_repository_env_file():
    """跑一次生成，仓库里的 ``.env`` 必须原封不动。

    这条挡的是「测试自己有副作用」：默认目标是真实 ``.env``，忘了传 ``--env-file``
    的用例会把开发者的凭据换掉。只比对字节，不读取内容。
    """
    real_env = REPO_ROOT / ".env"
    if not real_env.is_file():
        pytest.skip("仓库没有 .env")
    before = real_env.read_bytes()

    result = _run("--env-file", str(REPO_ROOT / ".env.nonexistent-probe"))
    assert result.returncode == 0

    assert real_env.read_bytes() == before
    probe = REPO_ROOT / ".env.nonexistent-probe"
    assert not probe.exists()
