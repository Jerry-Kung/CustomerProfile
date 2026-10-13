# -*- coding: utf-8 -*-
"""只读探针：列出 v2 批次一案各节点的原始取数结构，供「完整输入数据」栏目设计用。"""
import json
import sys
from pathlib import Path

RUNS = Path("experiments/abexperiments/batches/A-pipeline-L8-20261012/runs")


def nid(it):
    return it.get("node_id") or it.get("id")


def index(root, phone):
    nodes = json.loads((root / phone / "nodes.json").read_text(encoding="utf-8"))
    if isinstance(nodes, dict) and "nodes" in nodes:
        nodes = nodes["nodes"]
    d = {}
    for it in nodes:
        d.setdefault(nid(it), []).append(it.get("outputs") or it.get("output") or {})
    return d


def main():
    phone = sys.argv[1] if len(sys.argv) > 1 else "13530559591"
    d = index(RUNS, phone)

    bundle = (d.get("pipeline_channel_bundle") or [{}])[0]
    cj = bundle.get("channels_json")
    print("=== channel_bundle 顶层键 ===", sorted(bundle))
    if isinstance(cj, str):
        try:
            obj = json.loads(cj)
        except Exception as exc:
            print("channels_json 非 JSON:", exc)
            obj = None
        if isinstance(obj, dict):
            print("channels_json 是 dict，键数", len(obj))
            for k, v in list(obj.items())[:20]:
                print("   %-36s %s" % (k, str(v)[:100]))
        elif isinstance(obj, list):
            print("channels_json 是 list，条数", len(obj))
            print(json.dumps(obj[0], ensure_ascii=False)[:600] if obj else "")
    else:
        print("channels_json 类型", type(cj).__name__)

    print()
    print("=== 各渠道节点的原始输出 ===")
    for node_id, label in (
        ("pipeline_history", "历史数据 body"),
        ("pipeline_wechat_search", "微信手机号搜索"),
        ("pipeline_jiguang", "极光"),
        ("pipeline_mengshi_it", "盟识 IT"),
        ("pipeline_phone_company", "企业分析"),
        ("pipeline_outbound_call", "外呼录音"),
        ("pipeline_test_drive", "试驾录音"),
        ("pipeline_moments", "朋友圈"),
        ("pipeline_wechat_home", "微信主页"),
        ("pipeline_douyin", "抖音"),
        ("pipeline_xiaohongshu", "小红书"),
        ("pipeline_alipay", "支付宝"),
        ("pipeline_locked_notes", "人工确认 Notes"),
        ("pipeline_chat_history", "聊天记录"),
        ("pipeline_feedback", "反馈记录"),
    ):
        item = (d.get(node_id) or [{}])[0]
        print("  -- %-14s %s" % (node_id, json.dumps(item, ensure_ascii=False)[:160]))

    print()
    print("=== records_json 单条样例 ===")
    rec = (d.get("pipeline_l0_records") or [{}])[0]
    try:
        R = json.loads(rec.get("records_json") or "[]")
        for r in R[:3]:
            print(json.dumps(r, ensure_ascii=False)[:400])
    except Exception as exc:
        print("解析失败", exc)

    print()
    print("=== l1_input 输出的键 ===")
    li = (d.get("pipeline_l1_input") or [{}])[0]
    for k, v in li.items():
        print("   %-16s 长度 %s" % (k, len(str(v))))
    text = str(li.get("output") or "")
    marker = "## 记录"
    idx = text.find(marker)
    print("  记录段起始位置:", idx, "总长", len(text))


if __name__ == "__main__":
    main()
