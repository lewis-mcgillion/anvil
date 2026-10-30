from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

import pytest

from anvil_server.replay import AgentEnvironment, EnvironmentRegistry, ReplayError


def digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def metadata(*, config_changes=None, manifest_changes=None):
    config = {"os": "linux", "architecture": "amd64",
              "config": {"Entrypoint": ["python", "-m"], "Cmd": ["agent"]}}
    config.update(config_changes or {})
    config_bytes = json.dumps(config).encode()
    manifest = {
        "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                   "size": len(config_bytes), "digest": digest(config_bytes)},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "size": 20, "digest": "sha256:" + "b" * 64}],
    }
    manifest.update(manifest_changes or {})
    manifest_bytes = json.dumps(manifest).encode()
    return {
        "image": "registry.example/agents/demo@" + digest(manifest_bytes),
        "manifest": manifest_bytes, "config": config_bytes, "lock": b"approved lock data",
        "entrypoint": ("python", "-m", "agent"), "adapter": "google-adk", "adapter_version": "1.0",
    }


def assert_code(code, call):
    with pytest.raises(ReplayError) as error:
        call()
    assert error.value.code == code


def test_approved_registry_resolves_pinned_immutable_environment():
    environment = AgentEnvironment.create(**metadata())
    registry = EnvironmentRegistry("project")
    identity = registry.approve("project", environment)
    assert identity == environment.environment_id
    assert registry.approve("project", environment) == identity
    assert registry.resolve("project", identity, image=environment.image) == environment
    view = environment.metadata
    view["entrypoint"][0] = "changed"
    assert registry.resolve("project", identity, image=environment.image).entrypoint[0] == "python"
    assert view["lock_sha256"] == digest(environment.lock)
    assert view["manifest_sha256"] == environment.image.split("@")[1]


@pytest.mark.parametrize("image", [
    "agent:latest", "registry.example/agent:latest", "registry.example/agent",
    "registry.example/agent:tag@sha256:" + "a" * 64,
    "https://registry.example/agent@sha256:" + "a" * 64,
    "registry.example/../agent@sha256:" + "a" * 64,
])
def test_mutable_or_noncanonical_image_references_rejected(image):
    values = metadata()
    values["image"] = image
    assert_code("MUTABLE_IMAGE", lambda: AgentEnvironment.create(**values))


def test_missing_and_mismatched_metadata_rejected():
    values = metadata()
    assert_code("MANIFEST_MISMATCH", lambda: AgentEnvironment.create(
        **{**values, "image": "registry.example/agent@sha256:" + "a" * 64}))
    assert_code("CONFIG_MISMATCH", lambda: AgentEnvironment.create(
        **{**values, "config": values["config"] + b" "}))
    for key in ["manifest", "config"]:
        assert_code("INVALID_MANIFEST", lambda: AgentEnvironment.create(**{**values, key: b""}))
    assert_code("INVALID_LOCK", lambda: AgentEnvironment.create(**{**values, "lock": b""}))
    assert_code("ENTRYPOINT_MISMATCH", lambda: AgentEnvironment.create(
        **{**values, "entrypoint": ("python", "wrong")}))


@pytest.mark.parametrize("changes,code", [
    ({"schemaVersion": True}, "INVALID_MANIFEST"),
    ({"schemaVersion": 1}, "INVALID_MANIFEST"),
    ({"mediaType": "application/vnd.oci.image.index.v1+json"}, "INVALID_MANIFEST"),
    ({"config": {}}, "INVALID_MANIFEST"),
    ({"layers": None}, "INVALID_MANIFEST"),
    ({"layers": [{"mediaType": "foreign", "size": 1, "digest": "sha256:" + "a" * 64}]}, "INVALID_MANIFEST"),
    ({"layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", "size": 1,
                 "digest": "sha256:" + "a" * 64, "urls": ["http://metadata/"]}]}, "INVALID_MANIFEST"),
])
def test_unsupported_oci_descriptors_rejected(changes, code):
    assert_code(code, lambda: AgentEnvironment.create(**metadata(manifest_changes=changes)))


@pytest.mark.parametrize("changes,code", [
    ({"os": "windows"}, "UNSUPPORTED_PLATFORM"),
    ({"architecture": ""}, "UNSUPPORTED_PLATFORM"),
    ({"config": {}}, "ENTRYPOINT_MISMATCH"),
    ({"config": {"Entrypoint": "shell string", "Cmd": []}}, "ENTRYPOINT_MISMATCH"),
])
def test_platform_and_entrypoint_checks(changes, code):
    assert_code(code, lambda: AgentEnvironment.create(**metadata(config_changes=changes)))


def test_duplicate_manifest_fields_and_invalid_json_fail_closed():
    values = metadata()
    for document in [b'{"schemaVersion":2,"schemaVersion":2}', b"[]", b"{", b'{"value":NaN}']:
        assert_code("INVALID_MANIFEST" if b"NaN" not in document else "INVALID_JSON",
                    lambda: AgentEnvironment.create(**{**values, "manifest": document}))


def test_registry_scope_and_unapproved_metadata_are_explicit():
    environment = AgentEnvironment.create(**metadata())
    registry = EnvironmentRegistry("one")
    identity = registry.approve("one", environment)
    assert_code("PROJECT_MISMATCH", lambda: registry.approve("two", environment))
    assert_code("PROJECT_MISMATCH", lambda: registry.resolve("two", identity, image=environment.image))
    assert_code("ENVIRONMENT_UNAPPROVED", lambda: registry.resolve("one", "unknown", image=environment.image))
    assert_code("ENVIRONMENT_MISMATCH", lambda: registry.resolve("one", identity, image="other"))
    assert_code("INVALID_LOCK", lambda: registry.approve("one", replace(environment, lock=b"")))
    registry.close()
    assert_code("ENVIRONMENT_UNAPPROVED", lambda: registry.resolve("one", identity, image=environment.image))


def test_adapter_lock_and_entrypoint_are_bound_to_identity():
    values = metadata()
    environment = AgentEnvironment.create(**values)
    other = AgentEnvironment.create(**{**values, "lock": b"new lock", "adapter_version": "2"})
    assert environment.environment_id != other.environment_id
    registry = EnvironmentRegistry("project")
    registry.approve("project", environment)
    assert_code("ENVIRONMENT_UNAPPROVED", lambda: registry.resolve(
        "project", other.environment_id, image=environment.image))


def test_concurrent_idempotent_approval():
    environment = AgentEnvironment.create(**metadata())
    registry = EnvironmentRegistry("project")
    with ThreadPoolExecutor(max_workers=5) as pool:
        identities = list(pool.map(lambda _: registry.approve("project", environment), range(30)))
    assert set(identities) == {environment.environment_id}
    assert registry.resolve("project", identities[0], image=environment.image) == environment


@pytest.mark.parametrize("entrypoint", [(), ["python"], ("bad\x00arg",), ("",)])
def test_invalid_entrypoint(entrypoint):
    with pytest.raises(ReplayError):
        AgentEnvironment.create(**{**metadata(), "entrypoint": entrypoint})
