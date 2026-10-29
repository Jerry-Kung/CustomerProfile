"""邀约专用录音链：保留此次导出中的校对与有效性判断。"""

from dataclasses import replace

from ..definitions import WorkflowDef, make_node
from . import audio_content_extract as extract
from .invitation import MODULE

START = "invitation_audio_start"
SELECT = "invitation_audio_select"
ITERATION = "invitation_audio_files"
ITER_START = "invitation_audio_file_start"
EXTRACT = "invitation_audio_extract"
COMBINE = "invitation_audio_combine"
END = "invitation_audio_end"
EXTRACT_ID = "invitation_audio_content_extract"


def _extract_workflow() -> WorkflowDef:
    nodes = []
    for node in extract.WORKFLOW.nodes:
        config = dict(node.config)
        if node.node_id in (extract.PROOFREAD_LLM, extract.VALIDITY_LLM):
            config["template"] = f"invitation_audio__{node.node_id}"
        if node.node_id == extract.VALIDITY_LLM:
            config["response_validator"] = (
                "customer_profile.invitation_contract:validate_audio_validity"
            )
        nodes.append(replace(node, config=config))
    return replace(
        extract.WORKFLOW,
        workflow_id=EXTRACT_ID,
        display_name="邀约录音内容抽取（含校对与有效性判断）",
        source_dsl="录音文件内容抽取（纯识别，无加工）.yml（2026-10-29）",
        nodes=tuple(nodes),
    )


WORKFLOW = WorkflowDef(
    workflow_id="invitation_outbound_audio",
    display_name="外呼录音原始数据解析（邀约）",
    source_dsl="外呼录音原始数据解析.yml（2026-10-29）",
    entries=(START,),
    exits=(END,),
    outputs={"result": "result", "update_time": "update_time"},
    nodes=(
        make_node(
            START,
            "历史资料与邀约批次",
            "start",
            variables=("customer_data", "batch_id"),
            outputs=("customer_data", "batch_id"),
        ),
        make_node(
            SELECT,
            "选择本次外呼录音",
            "code",
            after=(START,),
            inputs={
                "records": (START, "customer_data"),
                "batch_id": (START, "batch_id"),
            },
            outputs=("files", "updated_at", "record_id"),
            function=f"{MODULE}:select_recordings",
        ),
        make_node(
            ITERATION,
            "按文件顺序识别",
            "iteration",
            after=(SELECT,),
            iterator=(SELECT, "files"),
            inputs={"@output": (EXTRACT, "file_content")},
            body=(ITER_START, EXTRACT),
            outputs=("output", "item"),
            is_parallel=False,
            flatten_output=True,
            error_handle_mode="terminated",
        ),
        make_node(
            ITER_START,
            "录音文件",
            "iteration-start",
            outputs=("item", "index"),
            iteration_id=ITERATION,
        ),
        make_node(
            EXTRACT,
            "ASR、校对与有效性判断",
            "tool",
            after=(ITER_START,),
            inputs={"file_url": (ITERATION, "item")},
            outputs=("file_content",),
            iteration_id=ITERATION,
            **{"@workflow": EXTRACT_ID, "@inputs": {"file_url": "file_url"}},
        ),
        make_node(
            COMBINE,
            "合并本次录音内容",
            "code",
            after=(ITERATION,),
            inputs={
                "parts": (ITERATION, "output"),
                "files": (SELECT, "files"),
                "updated_at": (SELECT, "updated_at"),
            },
            outputs=("result", "update_time"),
            function=f"{MODULE}:combine_recordings",
        ),
        make_node(
            END,
            "录音解析输出",
            "end",
            after=(COMBINE,),
            inputs={
                "result": (COMBINE, "result"),
                "update_time": (COMBINE, "update_time"),
            },
            outputs=("result", "update_time"),
        ),
    ),
)
SUBWORKFLOWS = (_extract_workflow(),)
