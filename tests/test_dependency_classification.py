"""Provenance contracts, including deliberately unknown and unavailable imports."""

import json

import pytest

from code_context.intelligence_dependencies import (
    MAX_DEPENDENCY_DECLARATIONS,
    MAX_DEPENDENCY_MANIFEST_BYTES,
    MAX_DEPENDENCY_MANIFESTS,
    MAX_DEPENDENCY_METADATA_BYTES,
    DependencyClassifier,
    merge_dependency_metadata,
    parse_dependency_manifest,
)
from code_context.models import FileChange, SyncBatch, content_hash
from code_context.storage import MirrorStore


def metadata(files):
    return merge_dependency_metadata(
        parse_dependency_manifest(path, text) for path, text in files.items()
    )


def classification(module, language="python", *, declarations=None, files=(), name=None):
    classifier = DependencyClassifier(files, declarations or metadata({}), ("", "src"))
    return classifier.classify(
        {"target_module": module, "name": name or module, "source_path": "src/main.py"},
        language,
    )


def apply(store, files, revision=0):
    return store.apply(
        "sample",
        SyncBatch(
            request_id=f"write-{revision}",
            base_revision=revision,
            mode="full" if revision == 0 else "delta",
            changes=[
                FileChange(op="upsert", path=path, content=text, sha256=content_hash(text))
                for path, text in files.items()
            ],
        ),
    )


def test_python_stdlib_names_do_not_claim_runtime_availability():
    result = classification("email.mime")
    assert result["dependency_kind"] == "standard_library"
    assert result["dependency_evidence"][0]["code"] == "PYTHON_STDLIB_CATALOG"
    assert "python-" in result["dependency_evidence"][0]["catalog"]
    assert classification("email_typo")["dependency_kind"] == "unknown"


def test_local_shadowing_precedes_stdlib_and_distribution_catalogs():
    files = [
        {"path": "src/json.py", "language": "python", "module": ""},
        {"path": "src/httpx/__init__.py", "language": "python", "module": ""},
    ]
    declarations = metadata({"pyproject.toml": '[project]\ndependencies=["httpx"]\n'})
    assert classification("json", files=files)["dependency_kind"] == "local_module"
    result = classification("httpx.Client", files=files, declarations=declarations)
    assert result["dependency_kind"] == "local_module"
    assert result["dependency_evidence"][0]["path"] == "src/httpx/__init__.py"


def test_relative_local_and_unknown_absolute_imports_remain_distinct():
    assert classification("pkg.missing", name=".missing")["dependency_kind"] == "local_module"
    assert classification("missing")["dependency_kind"] == "unknown"
    # The resolver's broad Java top-level org/com hint is not package evidence.
    files = [{"path": "src/X.java", "language": "java", "module": "org.example.app"}]
    assert (
        classification("org.somewhere.lib.X", "java", files=files)["dependency_kind"] == "unknown"
    )
    assert (
        classification("org.example.app.Missing", "java", files=files)["dependency_kind"]
        == "local_module"
    )


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("java.util.List", "standard_library"),
        ("javax.sql.DataSource", "standard_library"),
        ("javax.annotation.processing.Processor", "standard_library"),
        ("javax.servlet.Servlet", "unknown"),
        ("javax.annotation.PostConstruct", "unknown"),
        ("javax.xml.bind.JAXBContext", "unknown"),
        ("javax.xml.ws.Service", "unknown"),
    ],
)
def test_java_standard_catalog_does_not_generalize_all_javax(module, expected):
    assert classification(module, "java")["dependency_kind"] == expected


def test_python_third_party_requires_declaration_and_explicit_alias():
    declarations = metadata(
        {"pyproject.toml": '[project]\ndependencies=["Pillow>=10", "example-thing"]\n'}
    )
    assert classification("PIL.Image")["dependency_kind"] == "unknown"
    result = classification("PIL.Image", declarations=declarations)
    assert result["dependency_kind"] == "third_party"
    assert result["dependency_evidence"][0]["declaration"] == "pillow"
    result = classification("example_thing", declarations=declarations)
    assert result["dependency_kind"] == "unknown"
    assert result["dependency_evidence"][1]["code"] == "DECLARED_DISTRIBUTION_NAME_CANDIDATE"


@pytest.mark.parametrize(
    "text",
    [
        '[project]\ndependencies=["httpx[http2] @ file:///outside/vendor"]\n',
        '[project]\ndependencies=["httpx"]\n[tool.uv.sources]\nhttpx={path="../../outside"}\n',
        '[tool.poetry.dependencies]\nhttpx={path="../../outside"}\n',
        '[project]\ndependencies=["httpx"]\n[tool.poetry.dependencies]\n'
        'httpx={path="../../outside"}\n',
    ],
)
def test_local_distribution_overrides_are_not_called_third_party_or_followed(text):
    declarations = metadata({"pyproject.toml": text})
    result = classification("httpx", declarations=declarations)
    assert result["dependency_kind"] == "local_module"
    assert result["dependency_evidence"][0]["code"] == "DECLARED_LOCAL_DISTRIBUTION_ALIAS_CATALOG"
    encoded = json.dumps(declarations)
    assert "outside" not in encoded and "vendor" not in encoded


def test_java_requires_exact_coordinate_alias_not_groupid_prefix():
    declarations = metadata(
        {
            "pom.xml": "<project><dependencies><dependency><groupId>org.slf4j</groupId>"
            "<artifactId>slf4j-api</artifactId></dependency><dependency>"
            "<groupId>org.custom</groupId><artifactId>arbitrary</artifactId>"
            "</dependency></dependencies></project>"
        }
    )
    assert (
        classification("org.slf4j.Logger", "java", declarations=declarations)["dependency_kind"]
        == "third_party"
    )
    assert (
        classification("org.custom.Whatever", "java", declarations=declarations)["dependency_kind"]
        == "unknown"
    )


def test_declared_dependencies_are_scoped_to_nearest_project_manifest():
    declarations = metadata(
        {
            "pyproject.toml": '[project]\ndependencies=["httpx"]\n',
            "sub/pyproject.toml": "[project]\ndependencies=[]\n",
            "sibling/pyproject.toml": '[project]\ndependencies=["Pillow"]\n',
        }
    )
    classifier = DependencyClassifier([], declarations, ("",))
    result = classifier.classify(
        {"target_module": "httpx", "name": "httpx", "source_path": "sub/main.py"}, "python"
    )
    assert result["dependency_kind"] == "unknown"
    assert classification("PIL", declarations=declarations)["dependency_kind"] == "unknown"
    assert classification("httpx", declarations=declarations)["dependency_kind"] == "third_party"


def test_manifest_facts_keep_names_without_urls_versions_or_executing_includes():
    declaration = parse_dependency_manifest(
        "requirements-dev.txt",
        "httpx @ https://private.invalid/wheel?access=example-value\n"
        "-r ../../outside-requirements.txt\n"
        "--index-url https://private.invalid/\n",
    )
    assert declaration["declarations"] == [{"kind": "python_distribution", "name": "httpx"}]
    encoded = json.dumps(declaration)
    assert "https" not in encoded and "outside" not in encoded and "example-value" not in encoded
    assert {item["code"] for item in declaration["diagnostics"]} == {
        "REQUIREMENT_INCLUDES_NOT_FOLLOWED",
        "REQUIREMENT_OPTIONS_NOT_EVALUATED",
    }


def test_invalid_xml_entities_and_dynamic_configuration_fail_closed():
    for text in (
        '<!DOCTYPE project SYSTEM "file:///etc/passwd"><project/>',
        '<!DOCTYPE project [<!ENTITY x "value">]><project>&x;</project>',
        "<project><dependencies>",
    ):
        result = parse_dependency_manifest("pom.xml", text)
        assert result["declarations"] == []
        assert result["diagnostics"][-1]["code"] == "DEPENDENCY_MANIFEST_INVALID"
        assert "passwd" not in json.dumps(result)
    result = parse_dependency_manifest("pyproject.toml", '[project]\ndynamic=["dependencies"]\n')
    assert result["status"] == "partial"
    assert result["diagnostics"] == [{"code": "DYNAMIC_DEPENDENCIES_NOT_EVALUATED"}]


def test_manifest_and_publication_budgets_are_explicit_and_bounded():
    result = parse_dependency_manifest(
        "requirements.txt", "a" * (MAX_DEPENDENCY_MANIFEST_BYTES + 1)
    )
    assert result["diagnostics"] == [{"code": "DEPENDENCY_MANIFEST_BUDGET_EXCEEDED"}]
    result = parse_dependency_manifest(
        "requirements.txt", "\n".join(f"lib{i}" for i in range(MAX_DEPENDENCY_DECLARATIONS + 1))
    )
    assert len(result["declarations"]) == MAX_DEPENDENCY_DECLARATIONS
    result = merge_dependency_metadata(
        parse_dependency_manifest(f"project{i}/requirements.txt", "httpx")
        for i in range(MAX_DEPENDENCY_MANIFESTS + 1)
    )
    assert len(result["manifests"]) == MAX_DEPENDENCY_MANIFESTS
    assert result["partial"]
    assert result["diagnostics"] == [{"code": "DEPENDENCY_METADATA_LIMIT_EXCEEDED"}]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_DEPENDENCY_METADATA_BYTES


def test_mirror_query_preserves_missing_import_semantics_and_explains_kinds(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    apply(
        store,
        {
            "pyproject.toml": '[project]\ndependencies=["httpx"]\n',
            "pkg/__init__.py": "",
            "pkg/models.py": "class Item: pass\n",
            "pkg/use.py": "from .models import Item\nfrom .missing import Missing\n"
            "import json\nimport httpx\nimport unseen_library\n",
        },
    )
    result = store.code_query("sample", None, "external_dependencies")
    kinds = {item["module"]: item["dependency_kind"] for item in result["dependencies"]}
    assert kinds == {
        "pkg.missing.Missing": "local_module",
        "json": "standard_library",
        "httpx": "third_party",
        "unseen_library": "unknown",
    }
    assert result["local_imports_omitted"] == 1
    assert result["local_dependency_query"] == "file_dependencies"
    assert result["dependency_metadata_partial"] is False
    assert all(
        item["classification"] in {"unavailable", "external_or_unavailable"}
        for item in result["dependencies"]
    )
    assert "availability" in result["coverage"]


def test_mirror_declaration_change_is_pinned_to_source_revision(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    apply(store, {"main.py": "import httpx\n", "pyproject.toml": "[project]\ndependencies=[]\n"})
    old = store.code_query("sample", None, "external_dependencies")
    assert old["dependencies"][0]["dependency_kind"] == "unknown"
    apply(store, {"pyproject.toml": '[project]\ndependencies=["httpx"]\n'}, revision=1)
    assert (
        store.code_query("sample", None, "external_dependencies")["dependencies"][0][
            "dependency_kind"
        ]
        == "third_party"
    )
    assert (
        store.code_query("sample", old["snapshot"], "external_dependencies")["dependencies"][0][
            "dependency_kind"
        ]
        == "unknown"
    )


def test_mirror_java_query_classifies_static_imports_without_claiming_resolution(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    apply(
        store,
        {
            "pom.xml": "<project><dependencies><dependency><groupId>org.slf4j</groupId>"
            "<artifactId>slf4j-api</artifactId></dependency></dependencies></project>",
            "src/X.java": "package demo; import java.util.List; import org.slf4j.Logger; "
            "import static org.slf4j.LoggerFactory.getLogger; import javax.servlet.Servlet; "
            "public class X {}",
        },
    )
    result = store.code_query("sample", None, "external_dependencies")
    kinds = {item["module"]: item["dependency_kind"] for item in result["dependencies"]}
    assert kinds == {
        "java.util.List": "standard_library",
        "org.slf4j.Logger": "third_party",
        "org.slf4j.LoggerFactory.getLogger": "third_party",
        "javax.servlet.Servlet": "unknown",
    }
    assert all(
        item["classification"] == "external_or_unavailable" for item in result["dependencies"]
    )
