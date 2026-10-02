# 사내 LLM Wiki

회사 문서(회의록, 이슈, 메일 .eml, docx/xlsx/pdf)를 LLM 이 연관된 Markdown 위키(Obsidian 호환)로 컴파일하고,
권한이 있는 사람만 자연어로 질문해 출처 인용 답변을 받는 사내 전용 지식창고. Python, 코어는 표준 라이브러리만 사용.

## 문서
- `docs/ONSITE-HANDOFF.md` : 사내 이관 런북 (완료/미완료, 설치 순서, Gauss/LM Studio 설정, SAML/OCR, 트러블슈팅)
- `docs/DEV-GUIDE.md` : 아키텍처, 데이터 구조, 확장 방법, 테스트/코드 규칙, 결정 로그
- `docs/PLAN-company-llm-wiki.md` : 기획과 확정 요구사항
- `config/env.example` : 모든 환경변수 (복사해서 `.env` 로 사용, 커밋 금지)
- `CLAUDE.md` : Claude Code 세션용 규칙 (상단 섹션)

## 빠른 시작 (Windows, PowerShell)
```
scripts\setup-venv.ps1 -IndexUrl <사내 pip 미러>   # .venv + pip install -e .[dev,prod]
copy config\env.example .env                        # 값 채우기 (비밀은 *_FILE 로)
scripts\run-doctor.ps1 -SkipLlm -SkipEmbed          # 자가진단 (이후 전체 진단)
scripts\run-tests.ps1                               # 테스트
scripts\run-web.ps1 ; scripts\run-pipeline.ps1      # 웹 서버 / inbox 파이프라인
```
운영 서비스 등록: `scripts\install-service-taskscheduler.ps1 -WhatIf` 부터. WSL/Docker 는 사용하지 않는다.
