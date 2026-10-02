"""LLM / embedding checks. Only synthetic prompts are ever sent; server messages shown here are redacted."""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from urllib.parse import urlparse

from ..config import Settings
from ..engine.embed import DEFAULT_EMBED_MODEL, embedder_from_env
from ..engine.llm import ContextExceeded, llm_from_env
from ..engine.params import DEFAULT_CONTEXT_TOKENS, context_char_budget
from ..engine.providers import chat_endpoint_url, provider_name
from ..engine.providers_config import ConfigError
from ..engine.providers_http import NetPolicy, load_secret
from .diagnose import classify, http_status
from .model import Result, fail, ok, warn

PING_SYSTEM = "Reply with the single word OK."
PING_PROMPT = "연결 확인용 합성 질문입니다. OK 라고만 답하세요."
PROBE_SIZES = (2_000, 4_000, 8_000, 16_000, 32_000, 64_000, 128_000, 256_000)
_FILLER = "문맥 길이 확인용 합성 문장입니다. This is synthetic filler text. "
_STRUCT = {"type": "json_object"}


def _host(url: str) -> str:
    u = urlparse(url)
    return (u.hostname or "?") + (f":{u.port}" if u.port else "")


def _net_summary(p: NetPolicy) -> str:
    parts = []
    if p.allowed_hosts:
        parts.append("허용 호스트=" + ",".join(sorted(p.allowed_hosts)))
    if p.proxy:
        parts.append("프록시=" + str(p.proxy_display()))
    if p.ca_bundle:
        parts.append("CA번들=" + p.ca_bundle)
    return ", ".join(parts) or "기본(엄격: 사내 주소만, 프록시/CA 추가 없음)"


def _scrub(text: str, *keys) -> str:
    for k in keys:
        if k is not None and k.reveal():
            text = text.replace(k.reveal(), "***")
    return " ".join(text.split())[:300]


def build_llm(settings: Settings, environ: Mapping[str, str]):
    """(client | None, [Results]) - provider/config summary; never prints secrets."""
    try:
        name = provider_name(environ, "WIKI_LLM_PROVIDER")
        client = llm_from_env(settings, environ)
        key = load_secret(environ, "LLM")
    except (ConfigError, ValueError) as e:
        code, why, hint = classify(e)
        general = ("WIKI_LLM_PROVIDER(openai|custom), WIKI_LLM_CUSTOM_CONFIG, WIKI_LLM_BASE_URL, WIKI_LLM_API_KEY[_FILE], "
                   "WIKI_LLM_ALLOWED_HOSTS / WIKI_LLM_PROXY / WIKI_LLM_CA_BUNDLE 값을 확인하세요. "
                   "custom 설정 예시: config/llm-custom.example.json")
        return None, [fail("llm.config", "LLM 설정 오류", (why + " | " if code != "unknown" else "") + _scrub(str(e)),
                           hint if code != "unknown" else general)]
    target = getattr(client, "url", None) or getattr(client, "base_url", "")
    detail = (f"provider={name}, host={_host(target)}, model={client.model}, Bearer 키={'설정됨(값 비공개)' if key else '없음'}; "
              f"{_net_summary(client.policy)}")
    if name == "custom":
        c = client.cfg
        detail += f"; stream={c.stream}, json_schema={c.supports_json_schema}, timeout={client.timeout:g}s"
    else:
        detail += (f"; stream={client.stream}, structured={'auto' if client._structured_ok else 'off'}, "
                   f"max_tokens={client.max_tokens or '미지정'}, timeout={client.timeout:g}s")
    res = [ok("llm.config", "LLM 설정 로드", detail)]
    if name == "openai" and not key:
        local = (urlparse(target).hostname or "") in ("127.0.0.1", "localhost", "::1")
        res.append(ok("llm.auth", "API 키 없음 (로컬 LM Studio는 키 불필요)") if local else
                   warn("llm.auth", "API 키가 설정되지 않음", f"host={_host(target)}",
                        "사내 API(Gauss 등)는 보통 Bearer 키가 필요합니다: WIKI_LLM_API_KEY_FILE=<키가 든 파일 경로> (권장) "
                        "또는 WIKI_LLM_API_KEY. 키는 로그/화면에 출력되지 않습니다."))
    elif key:
        res.append(ok("llm.auth", "Bearer 키 설정됨", "Authorization 헤더로 전송, 값은 표시하지 않음"))
    res.append(check_context_setting(environ))
    return client, res


def check_context_setting(environ: Mapping[str, str]) -> Result:
    raw = (environ.get("WIKI_LLM_CONTEXT_TOKENS") or "").strip()
    try:
        tokens = int(raw) if raw else 0
    except ValueError:
        tokens = 0
    if tokens <= 0:
        return warn("llm.ctxsetting", f"WIKI_LLM_CONTEXT_TOKENS 미설정/무효 -> 기본 {DEFAULT_CONTEXT_TOKENS} 토큰 가정",
                    f"검색 컨텍스트 예산 약 {context_char_budget(DEFAULT_CONTEXT_TOKENS)}자",
                    "모델 컨텍스트가 더 크면 올리세요. 예: Gauss 64k 모델이면 WIKI_LLM_CONTEXT_TOKENS=65536 "
                    f"(예산 약 {context_char_budget(65536)}자). --probe-context 로 실측할 수 있습니다.")
    return ok("llm.ctxsetting", f"WIKI_LLM_CONTEXT_TOKENS={tokens}", f"검색 컨텍스트 예산 약 {context_char_budget(tokens)}자")


def check_chat(client, probe_context: bool = False, sizes=PROBE_SIZES) -> list[Result]:
    key = getattr(client, "_key", None)
    t0 = time.monotonic()
    try:
        reply = client.complete(PING_SYSTEM, PING_PROMPT)
    except Exception as e:  # noqa: BLE001 - any failure is a diagnosis, never a crash
        return [_failure("llm.call", "LLM 호출 실패", e, client, key)]
    ms = int((time.monotonic() - t0) * 1000)
    out = [ok("llm.call", "LLM 호출 성공", f"{ms} ms, 응답 {len(reply)}자") if reply.strip() else
           warn("llm.call", "LLM이 빈 응답을 반환함", f"{ms} ms", "response_path/모델 이름/프롬프트 형식을 확인하세요.")]
    out.append(_check_structured(client, key))
    out += _think_hint(client)
    if probe_context:
        out += probe_context_window(client, sizes, key)
    return out


def _think_hint(client) -> list[Result]:
    if not getattr(client, "saw_reasoning", False):
        return []
    mt, to = getattr(client, "max_tokens", None), client.timeout
    if (mt or 0) >= 4096 and to >= 300:
        return [ok("llm.think", "추론(think) 모델 감지", f"max_tokens={mt}, timeout={to:g}s (충분)")]
    return [warn("llm.think", "추론(think) 모델 감지: 응답에 <think>/reasoning 이 포함됨",
                 f"max_tokens={mt or '미지정'}, timeout={to:g}s (<think> 블록은 자동 제거됨)",
                 "추론이 출력 토큰을 소모하므로 WIKI_LLM_MAX_TOKENS=4096 이상, WIKI_LLM_TIMEOUT=300 이상을 권장합니다. "
                 "긴 추론에서 타임아웃이 나면 WIKI_LLM_STREAM=1 (읽기 단위 타임아웃)도 켜세요.")]


def check_stream(settings: Settings, environ: Mapping[str, str], client) -> list[Result]:
    """Is SSE streaming usable? (default openai provider only; custom is configured via its JSON file)"""
    if provider_name(environ, "WIKI_LLM_PROVIDER") != "openai":
        return []
    if getattr(client, "stream", False):
        return [ok("llm.stream", "스트리밍(SSE) 사용 중", "위 호출이 스트리밍으로 수행되어 성공함")]
    try:
        probe = llm_from_env(settings, {**environ, "WIKI_LLM_STREAM": "1"})
        out = probe.complete(PING_SYSTEM, PING_PROMPT)
    except Exception as e:  # noqa: BLE001
        code, why, _ = classify(e)
        return [warn("llm.stream", "스트리밍 호출 실패 (비활성 상태로 계속 사용 가능)", why,
                     "서버/프록시가 SSE를 막을 수 있습니다. WIKI_LLM_STREAM 은 켜지 마세요.")]
    return [ok("llm.stream", "스트리밍(SSE) 가능", f"응답 {len(out)}자. 긴 추론 모델은 WIKI_LLM_STREAM=1 로 켜면 읽기 단위 타임아웃이 적용됩니다.")]


def _failure(id_: str, title: str, e: BaseException, client, key) -> Result:
    code, why, hint = classify(e, proxy=bool(getattr(getattr(client, "policy", None), "proxy", None)))
    detail = why
    srv = getattr(e, "detail", "")
    if srv:
        detail += f" | 서버 메시지: {_scrub(srv, key)}"
    return fail(id_, title, detail, hint)


def _check_structured(client, key) -> Result:
    from ..engine.providers import CustomChatClient

    if isinstance(client, CustomChatClient) and not client.cfg.supports_json_schema:
        return ok("llm.structured", "구조화 출력(response_format) 미사용", "설정 supports_json_schema=false: 일반 모드로 분류 JSON을 받습니다.")
    try:
        client.complete(PING_SYSTEM, 'Return {"ok": true} as JSON.', response_format=_STRUCT)
    except Exception as e:  # noqa: BLE001
        return _failure("llm.structured", "구조화 출력 호출 실패", e, client, key)
    if getattr(client, "_structured_ok", True):
        return ok("llm.structured", "구조화 출력(response_format) 허용됨")
    return warn("llm.structured", "서버가 response_format을 거부함 -> 일반 모드로 자동 전환",
                "동작에는 문제 없으나 분류 JSON이 덜 안정적일 수 있습니다.",
                "custom 제공자면 서버가 지원할 때만 supports_json_schema=true 로 두세요.")


def probe_context_window(client, sizes, key=None) -> list[Result]:
    """Send growing synthetic prompts until ContextExceeded / failure, then bisect a little."""

    def attempt(n: int) -> str:
        text = (_FILLER * (n // len(_FILLER) + 1))[:n]
        try:
            client.complete(PING_SYSTEM, text + "\n\nOK 라고만 답하세요.")
            return "ok"
        except ContextExceeded as e:
            attempt.n_ctx = e.n_ctx or attempt.n_ctx
            return "ctx"
        except Exception as e:  # noqa: BLE001
            attempt.err = e
            if last_ok_ref[0] and http_status(e) in (400, 413, 422):  # failed only once the prompt got big: size limit
                attempt.unrecognized = _scrub(getattr(e, "detail", "") or str(e), key)  # type: ignore[attr-defined]
                return "ctx"
            return "err"

    attempt.n_ctx, attempt.err, attempt.unrecognized = None, None, ""  # type: ignore[attr-defined]
    last_ok_ref = [0]
    last_ok, first_bad = 0, None
    for n in sizes:
        r = attempt(n)
        if r == "ok":
            last_ok = last_ok_ref[0] = n
            continue
        if r == "err":
            res = [_failure("llm.context", "컨텍스트 탐지 중 오류", attempt.err, client, key)]  # type: ignore[attr-defined]
            if last_ok:
                res.append(_context_summary(last_ok, None, attempt.n_ctx, partial=True))  # type: ignore[attr-defined]
            return res
        first_bad = n
        break
    if first_bad is not None:
        lo, hi = last_ok, first_bad
        for _ in range(3):
            mid = (lo + hi) // 2
            if mid - lo < 500:
                break
            r = attempt(mid)
            if r == "ok":
                lo = last_ok_ref[0] = mid
            elif r == "ctx":
                hi = mid
            else:
                break
        last_ok, first_bad = lo, hi
    res = _context_summary(last_ok, first_bad, attempt.n_ctx)  # type: ignore[attr-defined]
    if attempt.unrecognized:  # type: ignore[attr-defined]
        res.detail += (f" | 서버 메시지: {attempt.unrecognized}")  # type: ignore[attr-defined]
        res.hint = ("서버가 컨텍스트 초과를 알아볼 수 없는 메시지로 알렸습니다. custom 제공자라면 위 메시지의 일부를 "
                    "context_exceeded_patterns에 추가하세요 (그래야 자동 재시도가 동작).")
    return [res]


def _context_summary(ok_chars: int, bad_chars: int | None, n_ctx: int | None, partial: bool = False) -> Result:
    note = " (서버가 조용히 잘라내는 경우는 감지할 수 없음)"
    if n_ctx:
        sugg = n_ctx
        detail = f"서버가 알려준 컨텍스트 = {n_ctx} 토큰; 통과 {ok_chars}자 / 초과 {bad_chars}자"
    elif bad_chars is None:
        sugg = ok_chars + 1500
        detail = f"테스트한 최대 {ok_chars}자까지 모두 통과 (한계 미발견){note}"
    else:
        sugg = ok_chars + 1500  # context_char_budget(t) = (t - 1500) * 0.9 chars: budget ~= 0.9 * measured limit
        detail = f"통과 {ok_chars}자 / 초과 {bad_chars}자 (한글 기준 약 1자=1토큰으로 보수적 추정){note}"
    sugg = max(2048, sugg // 256 * 256)
    return ok("llm.context", "컨텍스트 한계 탐지" + (" (일부)" if partial else ""),
              detail + f" -> 권장: WIKI_LLM_CONTEXT_TOKENS={sugg}")


def check_embed(settings: Settings, environ: Mapping[str, str]) -> list[Result]:
    raw = environ.get("WIKI_EMBED_MODEL")
    if raw is not None and (not raw.strip() or raw.strip().lower() in {"off", "none", "false", "0", "disabled"}):
        return [warn("embed.call", "임베딩 비활성 (WIKI_EMBED_MODEL=off)", "BM25 키워드 검색만 사용합니다.",
                     f"의미 검색을 쓰려면 WIKI_EMBED_MODEL={DEFAULT_EMBED_MODEL} (LM Studio의 bge-m3 등)를 설정하세요.")]
    try:
        emb = embedder_from_env(settings, environ)
    except (ConfigError, ValueError) as e:
        return [fail("embed.config", "임베딩 설정 오류", _scrub(str(e)),
                     "WIKI_EMBED_PROVIDER(openai|custom), WIKI_EMBED_BASE_URL, WIKI_EMBED_CUSTOM_CONFIG, WIKI_EMBED_API_KEY[_FILE] 확인. "
                     "채팅은 사내 API, 임베딩은 LM Studio를 쓰면 WIKI_EMBED_BASE_URL=http://127.0.0.1:1234/v1 을 지정하세요.")]
    assert emb is not None
    key = getattr(emb, "_key", None)
    target = getattr(emb, "url", None) or getattr(emb, "base_url", "")
    chat = chat_endpoint_url(settings.llm_base_url, environ)
    same = urlparse(target).netloc.lower() == urlparse(chat).netloc.lower()
    cfg = ok("embed.config", "임베딩 설정 로드", f"provider={provider_name(environ, 'WIKI_EMBED_PROVIDER')}, host={_host(target)}, "
             f"model={emb.model}, Bearer 키={'설정됨' if key else '없음'}; {_net_summary(emb.policy)}; "
             + ("채팅과 같은 서버(WIKI_LLM_* 설정 상속)" if same else "채팅과 다른 서버(채팅 키/프록시는 전송·상속되지 않음)"))
    pre = []
    if provider_name(environ, "WIKI_EMBED_PROVIDER") == "openai"             and not (environ.get("WIKI_EMBED_BASE_URL") or "").strip()             and (urlparse(target).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        pre.append(warn("embed.endpoint", "임베딩이 채팅 서버로 요청됨 (WIKI_EMBED_BASE_URL 미설정)", f"host={_host(target)}",
                        "채팅 API가 임베딩을 제공하지 않으면(Gauss 등) 같은 PC의 LM Studio를 가리키세요: "
                        "WIKI_EMBED_BASE_URL=http://127.0.0.1:1234/v1 (필요하면 WIKI_EMBED_API_KEY[_FILE])."))
    t0 = time.monotonic()
    try:
        vecs = emb.embed(["서버 장애 대응 절차", "vacation request process"])
    except Exception as e:  # noqa: BLE001
        return [cfg, *pre, _failure("embed.call", "임베딩 호출 실패", e, emb, key)]
    ms = int((time.monotonic() - t0) * 1000)
    dim = len(vecs[0])
    norms = [math.sqrt(sum(x * x for x in v)) for v in vecs]
    cos = sum(a * b for a, b in zip(vecs[0], vecs[1])) / ((norms[0] * norms[1]) or 1.0)
    if not all(math.isfinite(n) and n > 0 for n in norms):
        return [cfg, *pre, fail("embed.call", "임베딩 벡터의 크기가 0 또는 비정상", f"dim={dim}", "모델 이름(WIKI_EMBED_MODEL)과 서버의 임베딩 모델 로딩 상태를 확인하세요.")]
    if cos > 0.9999:
        return [cfg, *pre, warn("embed.call", "서로 다른 문장이 같은 벡터로 변환됨", f"dim={dim}, {ms} ms",
                          "임베딩 모델이 아닌 모델이 연결되었거나 입력 필드(input) 매핑이 틀렸을 수 있습니다.")]
    return [cfg, *pre, ok("embed.call", "임베딩 호출 성공", f"차원={dim}, {ms} ms, 벡터 크기={norms[0]:.3f}/{norms[1]:.3f} "
                    "(저장 시 L2 정규화됨), 서로 다른 문장 코사인=" + f"{cos:.3f}")]
