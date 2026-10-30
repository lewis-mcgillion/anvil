"""Project-scoped approval inventory for already-resolved immutable OCI metadata.

No image pull, archive extraction, filesystem access or container launch occurs.
The caller supplies trusted approval decisions and raw OCI manifest/config bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from threading import RLock

from .fixtures import ReplayError, _canonical, _text

_IMAGE = re.compile(
    r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"@sha256:[0-9a-f]{64}"
)
_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
_LAYERS = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.v1.tar+zstd",
}


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _object(data: bytes) -> dict:
    if type(data) is not bytes or not data or len(data) > 1024 * 1024:
        raise ReplayError("INVALID_MANIFEST", "Expected bounded raw OCI metadata bytes")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReplayError("INVALID_MANIFEST", "Duplicate OCI metadata field")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ReplayError("INVALID_MANIFEST", "Malformed OCI metadata") from exc
    if type(value) is not dict:
        raise ReplayError("INVALID_MANIFEST", "Expected an OCI metadata object")
    _canonical(value)
    return value


def _descriptor(descriptor: object, media_types: set[str]) -> None:
    if (type(descriptor) is not dict
            or type(descriptor.get("mediaType")) is not str
            or descriptor["mediaType"] not in media_types
            or type(descriptor.get("digest")) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", descriptor["digest"]) is None
            or type(descriptor.get("size")) is not int or descriptor["size"] < 0
            or "urls" in descriptor or "data" in descriptor):
        raise ReplayError("INVALID_MANIFEST", "Unsupported or unpinned OCI descriptor")


@dataclass(frozen=True)
class AgentEnvironment:
    """Content-addressed approved metadata, not evidence that an image is runnable."""

    image: str
    manifest: bytes
    config: bytes
    lock: bytes
    entrypoint: tuple[str, ...]
    adapter: str
    adapter_version: str

    @classmethod
    def create(cls, *, image: str, manifest: bytes, config: bytes, lock: bytes,
               entrypoint: tuple[str, ...], adapter: str, adapter_version: str) -> AgentEnvironment:
        if type(image) is not str or len(image) > 2048 or _IMAGE.fullmatch(image) is None:
            raise ReplayError("MUTABLE_IMAGE", "Expected a fully qualified OCI repository pinned by SHA-256")
        document, configuration = _object(manifest), _object(config)
        if image.rsplit("@", 1)[1] != _sha(manifest):
            raise ReplayError("MANIFEST_MISMATCH", "Image reference does not match raw manifest digest")
        if (type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 2
                or document.get("mediaType") != _OCI_MANIFEST):
            raise ReplayError("INVALID_MANIFEST", "Only OCI image manifests v1 are supported")
        descriptor = document.get("config")
        _descriptor(descriptor, {_OCI_CONFIG})
        if descriptor["digest"] != _sha(config) or descriptor["size"] != len(config):
            raise ReplayError("CONFIG_MISMATCH", "OCI config digest or size mismatch")
        layers = document.get("layers")
        if type(layers) is not list:
            raise ReplayError("INVALID_MANIFEST", "OCI layer manifest is missing")
        for layer in layers:
            _descriptor(layer, _LAYERS)
        if (configuration.get("os") != "linux" or type(configuration.get("architecture")) is not str
                or not configuration["architecture"]):
            raise ReplayError("UNSUPPORTED_PLATFORM", "Only explicitly identified Linux images are supported")
        if type(lock) is not bytes or not lock or len(lock) > 1024 * 1024:
            raise ReplayError("INVALID_LOCK", "Expected nonempty bounded dependency lock metadata")
        if type(entrypoint) is not tuple or not entrypoint or len(entrypoint) > 64:
            raise ReplayError("INVALID_ENTRYPOINT", "Expected explicit immutable entrypoint arguments")
        for argument in entrypoint:
            _text(argument, "entrypoint argument")
            if "\x00" in argument:
                raise ReplayError("INVALID_ENTRYPOINT", "Entrypoint arguments contain NUL")
        image_config = configuration.get("config")
        if type(image_config) is not dict:
            raise ReplayError("INVALID_MANIFEST", "OCI execution config is missing")
        declared_entrypoint, declared_cmd = image_config.get("Entrypoint", []), image_config.get("Cmd", [])
        if (type(declared_entrypoint) is not list or type(declared_cmd) is not list
                or declared_entrypoint + declared_cmd != list(entrypoint)):
            raise ReplayError("ENTRYPOINT_MISMATCH", "Approved entrypoint differs from image config")
        return cls(image, manifest, config, lock, entrypoint,
                   _text(adapter, "adapter"), _text(adapter_version, "adapter version"))

    @property
    def metadata(self) -> dict:
        return {
            "schema_version": "1", "image": self.image,
            "manifest_sha256": _sha(self.manifest), "config_sha256": _sha(self.config),
            "lock_sha256": _sha(self.lock), "entrypoint": list(self.entrypoint),
            "adapter": self.adapter, "adapter_version": self.adapter_version,
        }

    @property
    def environment_id(self) -> str:
        return _sha(_canonical(self.metadata))


class EnvironmentRegistry:
    """In-memory inventory; approval is a trusted host action, never runner input.

    Persistence, project authorization and approval audit are caller-owned.
    Lookup requires the expected metadata ID, not only an image digest.
    """

    def __init__(self, project_id: str):
        self._project_id = _text(project_id, "project identity")
        self._approved: dict[str, AgentEnvironment] = {}
        self._lock = RLock()

    def _scope(self, project_id: str) -> None:
        if project_id != self._project_id:
            raise ReplayError("PROJECT_MISMATCH", "Registry is bound to another project")

    def approve(self, project_id: str, environment: AgentEnvironment) -> str:
        self._scope(project_id)
        if type(environment) is not AgentEnvironment:
            raise ReplayError("INVALID_ENVIRONMENT", "Expected an immutable environment")
        validated = AgentEnvironment.create(
            image=environment.image, manifest=environment.manifest, config=environment.config,
            lock=environment.lock, entrypoint=environment.entrypoint, adapter=environment.adapter,
            adapter_version=environment.adapter_version,
        )
        identity = validated.environment_id
        with self._lock:
            existing = self._approved.get(identity)
            if existing is not None and existing != validated:
                raise ReplayError("ENVIRONMENT_CONFLICT", "Immutable environment metadata conflict")
            self._approved[identity] = validated
        return identity

    def resolve(self, project_id: str, environment_id: str, *, image: str) -> AgentEnvironment:
        self._scope(project_id)
        if type(environment_id) is not str or type(image) is not str:
            raise ReplayError("ENVIRONMENT_UNAPPROVED", "Environment is not approved")
        with self._lock:
            environment = self._approved.get(environment_id)
            if environment is None:
                raise ReplayError("ENVIRONMENT_UNAPPROVED", "Environment is not approved")
            if environment.image != image:
                raise ReplayError("ENVIRONMENT_MISMATCH", "Image and environment metadata differ")
            return environment

    def close(self) -> None:
        with self._lock:
            self._approved.clear()
