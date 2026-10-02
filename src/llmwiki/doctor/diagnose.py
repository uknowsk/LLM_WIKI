"""Turn an exception from an LLM/embedding call into (code, Korean explanation, remediation hint)."""
from __future__ import annotations

import re
import socket
import ssl

_HTTP = re.compile(r"HTTP (\d{3})")


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    seen: set[int] = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        out.append(e)
        stack += [e.__cause__, e.__context__, getattr(e, "reason", None) if isinstance(getattr(e, "reason", None), BaseException) else None]
    return out


def http_status(exc: BaseException) -> int | None:
    for e in _chain(exc):
        for attr in ("status", "code"):
            v = getattr(e, attr, None)
            if isinstance(v, int) and not isinstance(v, bool) and 100 <= v < 600:
                return v
        m = _HTTP.search(str(e))
        if m:
            return int(m.group(1))
    return None


def classify(exc: BaseException, *, proxy: bool = False) -> tuple[str, str, str]:
    chain = _chain(exc)
    text = " ".join(str(e) for e in chain)
    low = text.lower()
    if any(isinstance(e, ssl.SSLCertVerificationError) for e in chain) or "certificate verify failed" in low:
        return ("tls_cert", "TLS 인증서 검증 실패 (사내 CA 미신뢰 또는 호스트명 불일치)",
                "사내 CA 인증서(PEM)를 WIKI_LLM_CA_BUNDLE=<경로> 로 지정하세요 (임베딩은 WIKI_EMBED_CA_BUNDLE, 미설정 시 LLM 값 사용). "
                "접속 주소의 호스트명이 인증서의 이름과 같은지 확인하세요. 인증서 검증을 끄는 옵션은 제공하지 않습니다.")
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return ("tls", "TLS 협상 실패", "서버가 https인지 http인지 URL 스킴(http/https)과 포트를 확인하세요.")
    if "does not resolve" in low or any(isinstance(e, socket.gaierror) for e in chain):
        return ("dns", "호스트 이름을 찾을 수 없음 (DNS)",
                "URL의 호스트명 철자와 사내 DNS를 확인하세요. 프록시를 통해서만 해석되는 이름이면 WIKI_LLM_ALLOWED_HOSTS=<호스트명> 이 필요할 수 있습니다.")
    if "must be on the intranet" in low:
        return ("not_internal", "호스트가 사내(사설) 주소로 해석되지 않아 차단됨",
                "의도한 사내 서버라면 WIKI_LLM_ALLOWED_HOSTS=<정확한 호스트명> 으로 명시 허용하세요 "
                "(위험: 그 이름을 외부에서 바꿀 수 있으면 문서가 유출될 수 있음). 그렇지 않다면 URL을 확인하세요.")
    if "scheme must be" in low or "has no host" in low:
        return ("bad_url", "엔드포인트 URL 형식 오류", "http:// 또는 https:// 로 시작하는 전체 URL을 설정하세요.")
    if any(isinstance(e, ConnectionRefusedError) for e in chain) or "refused" in low:
        return ("refused", "연결 거부 (서버가 해당 포트에서 대기 중이 아님)",
                ("프록시 주소/포트(WIKI_LLM_PROXY)를 확인하세요. " if proxy else "") +
                "LM Studio라면 Local Server를 시작했는지(기본 포트 1234), 사내 API라면 호스트/포트/방화벽을 확인하세요.")
    if any(isinstance(e, (TimeoutError, socket.timeout)) for e in chain) or "timed out" in low or "total_timeout" in low:
        return ("timeout", "응답 시간 초과",
                "서버가 느리거나 방화벽/프록시가 막고 있을 수 있습니다. 프록시가 필요하면 WIKI_LLM_PROXY를 설정하고, "
                "느린 모델이면 설정 파일의 timeout 값을 늘리세요.")
    st = http_status(exc)
    if st in (401, 403):
        return ("auth", f"인증/권한 오류 (HTTP {st})",
                "WIKI_LLM_API_KEY 또는 WIKI_LLM_API_KEY_FILE의 키와 헤더 형식(예: Authorization: Bearer {api_key})을 확인하세요. "
                "키 앞뒤 공백/줄바꿈, 만료, 해당 API에 대한 권한도 확인하세요.")
    if st == 404:
        return ("not_found", "경로를 찾을 수 없음 (HTTP 404)", "URL의 경로(/chat, /v1/chat/completions 등)와 모델 이름(WIKI_LLM_MODEL)을 확인하세요.")
    if st == 429:
        return ("rate_limit", "요청 한도 초과 (HTTP 429)", "잠시 후 재시도하세요. 동시 요청을 줄이거나 관리자에게 한도를 문의하세요.")
    if st is not None and st >= 500:
        return ("server", f"서버 내부 오류 (HTTP {st})", "서버 상태를 확인하세요. 요청 본문이 서버가 기대하는 형식과 다를 때도 5xx가 올 수 있습니다.")
    if st in (400, 415, 422):
        return ("bad_request", f"요청 형식 거부 (HTTP {st})",
                "설정 파일의 body 템플릿(필드 이름, messages 구조, 파라미터 범위)을 API 명세와 맞추세요. "
                "아래 '서버 메시지'를 단서로 사용하세요.")
    if st is not None and 300 <= st < 400:
        return ("redirect", f"리다이렉트 응답 (HTTP {st}) - 보안상 따라가지 않음", "URL을 최종 주소로 직접 지정하세요 (http→https, 끝의 / 등).")
    if "response_path" in low or "stream_path" in low or "not valid json" in low or "not text" in low or "sse" in low:
        return ("shape", "응답은 왔지만 설정의 경로로 텍스트를 꺼내지 못함",
                "설정 파일의 response_path / stream_path를 실제 응답 구조에 맞추세요. stream 설정이 서버와 맞는지(true/false)도 확인하세요.")
    return ("unknown", f"알 수 없는 오류 ({type(exc).__name__})", "세부 메시지를 확인하세요.")
