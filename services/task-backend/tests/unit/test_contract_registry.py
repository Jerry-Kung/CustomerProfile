from __future__ import annotations

from pathlib import Path

from src.contracts.registry import default_contract_paths, validate_contract_baseline

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_default_contract_paths_exist():
    paths = default_contract_paths(_PROJECT_ROOT)
    assert paths.openapi_path.exists()
    assert paths.output_schema_path.exists()
    assert paths.migration_sql_path.exists()


def test_validate_contract_baseline_success():
    paths = default_contract_paths(_PROJECT_ROOT)
    validate_contract_baseline(paths)
    output_schema = paths.output_schema_path.read_text(encoding="utf-8")
    assert '"required": []' in output_schema


def test_published_submit_contract_matches_runtime_fields():
    from src.api.app import create_api_app
    from src.contracts.registry import load_openapi
    app = create_api_app(db_client=object(), task_service=object())
    runtime = app.openapi()
    published = load_openapi(default_contract_paths(_PROJECT_ROOT))
    path = "/v1/customer-profile/tasks"
    content = runtime["paths"][path]["post"]["requestBody"]["content"]
    assert set(content) == {"application/x-www-form-urlencoded"}
    ref = content["application/x-www-form-urlencoded"]["schema"]["$ref"].split("/")[-1]
    fields = runtime["components"]["schemas"][ref]["properties"]
    assert set(fields) == set(published["components"]["schemas"]["SubmitTaskRequest"]["properties"])


def test_published_openapi_has_no_duplicate_yaml_keys():
    import yaml

    class UniqueKeysLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
            assert len(keys) == len(set(keys)), f"duplicate contract key near line {node.start_mark.line + 1}"
            return super().construct_mapping(node, deep=deep)

    yaml.load(default_contract_paths(_PROJECT_ROOT).openapi_path.read_text(), Loader=UniqueKeysLoader)
