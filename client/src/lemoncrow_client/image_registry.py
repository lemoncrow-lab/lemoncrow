"""The digest a container registry serves for an image, for ``lc verify init --toolchain IMAGE``.

It asks the registry itself, through the OCI distribution API (``/v2/<repository>/manifests/<tag or digest>``), and
anonymously: a registry that answers 401 with a ``Bearer`` challenge (Docker Hub, ghcr.io and most others) gets the
anonymous token flow, for the scope the challenge names. No credential is read or sent, so a private image is refused
with what the registry said. A tag resolves to the digest of the manifest the registry serves for it (the index of
a multi-platform image); a digest is accepted only once the registry confirms it has that manifest. Nothing is ever
guessed: any failure is a :class:`RegistryError`.

Only https, except a loopback registry (``localhost:5000``, ``127.0.0.1:PORT``), which Docker also reaches over plain
HTTP. Redirects are not followed. Standard library only, and nothing here writes a file.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from http.client import HTTPException
from typing import Final

__all__ = ["ImageReference", "RegistryError", "parse_reference", "resolve_digest"]

DOCKER_HUB: Final[str] = "docker.io"
_DOCKER_HUB_API: Final[str] = "registry-1.docker.io"
_DOCKER_HUB_ALIASES: Final[frozenset[str]] = frozenset({"index.docker.io", "registry-1.docker.io"})
#: Every manifest type a registry may hold for an image; the index and list types come first, so a multi-platform
#: image resolves to its index and a server pulls the platform it runs on.
_MANIFEST_TYPES: Final[str] = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
_USER_AGENT: Final[str] = "lemoncrow-client"
_TIMEOUT_S: Final[float] = 20.0
#: The most a manifest or token answer may be; a larger one is refused rather than cut.
_MAX_BODY: Final[int] = 4 << 20
_DIGEST: Final[re.Pattern[str]] = re.compile(r"sha256:[0-9a-f]{64}")
_COMPONENT: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")
_TAG: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_DOMAIN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?::[0-9]+)?")
_CHALLENGE_PARAM: Final[re.Pattern[str]] = re.compile(r'([A-Za-z_]+)="([^"]*)"')


class RegistryError(Exception):
    """The digest could not be resolved; the message says why."""


@dataclass(frozen=True)
class ImageReference:
    """An image reference as Docker reads it: ``python:3.12`` is ``docker.io/library/python`` at tag ``3.12``."""

    #: As written, without a digest: what the pinned reference keeps.
    name: str
    domain: str
    repository: str
    tag: str = ""
    digest: str = ""

    @property
    def api_host(self) -> str:
        return _DOCKER_HUB_API if self.domain == DOCKER_HUB else self.domain

    @property
    def scheme(self) -> str:
        return "http" if _is_loopback(self.domain.rsplit(":", 1)[0]) else "https"

    def pinned(self, digest: str) -> str:
        return f"{self.name}@{digest}"


def _is_loopback(host: str) -> bool:
    return host == "localhost" or re.fullmatch(r"127(?:\.[0-9]{1,3}){3}", host) is not None


def parse_reference(image: str) -> ImageReference:
    """*image* as a reference (``[registry/]repository[:tag][@sha256:...]``); a malformed one is refused."""

    text = image.strip()
    if not text or len(text) > 512 or any(ch.isspace() for ch in text):
        raise RegistryError(f"{image!r} is not an image reference")
    name, at, digest = text.partition("@")
    if at and _DIGEST.fullmatch(digest) is None:
        raise RegistryError(f"{image}: a digest must be sha256:<64 lowercase hex digits>")
    path, tag = name, ""
    colon = name.rfind(":")
    if colon > name.rfind("/"):
        path, tag = name[:colon], name[colon + 1 :]
        if _TAG.fullmatch(tag) is None:
            raise RegistryError(f"{image}: {tag!r} is not a valid tag")
    first, slash, rest = path.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        domain, repository = first, rest
    else:
        domain, repository = DOCKER_HUB, path
    if domain.lower() in _DOCKER_HUB_ALIASES:
        domain = DOCKER_HUB
    if _DOMAIN.fullmatch(domain) is None:
        raise RegistryError(f"{image}: {domain!r} is not a registry host")
    if domain == DOCKER_HUB and "/" not in repository:
        repository = f"library/{repository}"
    if not all(_COMPONENT.fullmatch(part) for part in repository.split("/")):
        raise RegistryError(f"{image}: {repository!r} is not a repository name (lowercase letters, digits, . _ -)")
    return ImageReference(name=name, domain=domain, repository=repository, tag=tag, digest=digest)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER: Final[urllib.request.OpenerDirector] = urllib.request.build_opener(_NoRedirect)


def _send(request: urllib.request.Request, *, body: bool, timeout: float) -> tuple[int, Message, bytes]:
    host = urllib.parse.urlsplit(request.full_url).netloc
    headers: Message
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            data = response.read(_MAX_BODY + 1) if body else b""
            status, headers = response.status, response.headers
    except urllib.error.HTTPError as exc:
        status, headers, data = exc.code, exc.headers, b""
        exc.close()
    except (urllib.error.URLError, HTTPException, OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        raise RegistryError(f"could not reach {host}: {reason}") from None
    if len(data) > _MAX_BODY:
        raise RegistryError(f"{host} answered more than {_MAX_BODY} bytes")
    return status, headers, data


def _anonymous_token(reference: ImageReference, challenge: str, timeout: float) -> str:
    scheme, _, rest = challenge.strip().partition(" ")
    if scheme.lower() != "bearer":
        raise RegistryError(
            f"{reference.domain} asks for credentials ({scheme or 'no challenge'}); only anonymous pulls are resolved"
        )
    params = dict(_CHALLENGE_PARAM.findall(rest))
    realm = urllib.parse.urlsplit(params.get("realm", ""))
    if not realm.hostname or not (realm.scheme == "https" or (realm.scheme == "http" and _is_loopback(realm.hostname))):
        raise RegistryError(f"{reference.domain} named a token service that is not https: {params.get('realm', '')!r}")
    query = urllib.parse.parse_qsl(realm.query)
    if params.get("service"):
        query.append(("service", params["service"]))
    query.append(("scope", params.get("scope") or f"repository:{reference.repository}:pull"))
    url = urllib.parse.urlunsplit((realm.scheme, realm.netloc, realm.path, urllib.parse.urlencode(query), ""))
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    status, _headers, data = _send(request, body=True, timeout=timeout)
    if status != 200:
        raise RegistryError(f"{realm.netloc} refused an anonymous token for {reference.repository} (HTTP {status})")
    try:
        answer = json.loads(data)
    except ValueError:
        raise RegistryError(f"{realm.netloc} answered a token request with something that is not JSON") from None
    token = (answer.get("token") or answer.get("access_token")) if isinstance(answer, dict) else None
    if not isinstance(token, str) or not token or len(token) > 16384 or any(ch.isspace() for ch in token):
        raise RegistryError(f"{realm.netloc} answered a token request without a token")
    return token


def _header(headers: Message, name: str) -> str:
    value = headers.get(name)
    return value.strip() if isinstance(value, str) else ""


def _manifest(url: str, token: str, *, head: bool, timeout: float) -> tuple[int, Message, bytes]:
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": _MANIFEST_TYPES}, method="HEAD" if head else "GET"
    )
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    return _send(request, body=not head, timeout=timeout)


def resolve_digest(reference: ImageReference, *, timeout: float = _TIMEOUT_S) -> str:
    """``sha256:<hex>`` of the manifest the registry serves for *reference*'s tag (``latest`` without one), or its
    digest once the registry confirmed it has that manifest."""

    target = reference.digest or reference.tag or "latest"
    url = f"{reference.scheme}://{reference.api_host}/v2/{reference.repository}/manifests/{target}"
    where = f"{reference.domain}/{reference.repository}"
    token = ""
    for head in (True, False):
        status, headers, data = _manifest(url, token, head=head, timeout=timeout)
        if status == 401 and not token:
            token = _anonymous_token(reference, _header(headers, "WWW-Authenticate"), timeout)
            status, headers, data = _manifest(url, token, head=head, timeout=timeout)
        if status == 404:
            raise RegistryError(f"{where} has no manifest for {target} (no such image, tag or digest)")
        if status in {401, 403}:
            raise RegistryError(
                f"{where} refused anonymous access (HTTP {status}); only public images are resolved. For a private "
                "image, write its pinned reference (IMAGE@sha256:<digest>) under [toolchain] yourself"
            )
        if status == 429:
            raise RegistryError(f"{reference.domain} is rate limiting anonymous requests (HTTP 429); try again later")
        if 300 <= status < 400:
            raise RegistryError(f"{reference.domain} redirected the manifest request (HTTP {status}); not followed")
        if status != 200:
            raise RegistryError(f"{reference.domain} answered HTTP {status} for {where}:{target}")
        served = _header(headers, "Docker-Content-Digest")
        if head and _DIGEST.fullmatch(served) is None:
            continue  # some registries leave the header out of HEAD answers: fetch the manifest and hash it
        if not head:
            computed = "sha256:" + hashlib.sha256(data).hexdigest()
            if served and served != computed:
                raise RegistryError(f"{where}: the manifest's bytes do not match the digest the registry named")
            served = computed
        if reference.digest and served != reference.digest:
            raise RegistryError(f"{where}: the registry served {served} for {reference.digest}")
        return served
    raise RegistryError(f"{where}: the registry named no digest for {target}")  # unreachable: GET always returns
