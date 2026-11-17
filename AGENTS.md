# 카드 이용내역 기반 최적 결제카드 추천 서비스

## 목적과 범위
- 프로젝트명은 "카드 이용내역 기반 최적 결제카드 추천 서비스"다.
- Windows 로컬 환경에서 개발한다.
- Python / Django / SQLite를 사용하고, 화면은 Django Template과 최소한의 HTML/CSS/JavaScript로 구현한다.
- Python 기반 외부 API 연동의 목적과 방식을 학습하는 것이 주요 목적이다.
- MVP 완성을 우선하며 불필요한 기술이나 패키지를 추가하지 않는다.
- Docker와 Cloud 배포는 사용하지 않는다. 서버는 로컬에서만 실행한다.
- 사용자가 최종 결정권자다. 작업 전 계획과 이유를 설명한다.

## 구현과 학습 원칙
- 기능을 구현하기 전에 관련 기존 코드와 설정을 먼저 읽고 현재 동작을 이해한다.
- 사용자가 요청한 주차와 작업 범위만 진행한다. 한 번에 여러 주차 기능을 구현하지 않는다.
- 사용자 요청 범위 밖의 기능을 임의로 추가하지 않는다.
- 가능한 단순하고 이해하기 쉬운 Python 코드를 작성한다.
- 사용자가 학습할 수 있도록 핵심 코드의 동작 원리와 구현 이유를 설명한다.
- 외부 API를 연동할 때 요청, 인증, 응답 처리와 오류 처리의 흐름을 설명한다. 설명이나 로그에 비밀값을 포함하지 않는다.

## 변경과 보안
- 대규모 리팩터링, 큰 구조 변경 또는 파일 삭제 전에 이유와 영향을 설명하고 사용자 승인을 받는다.
- 민감정보는 .env에서 관리한다.
- .env, API Key, SECRET_KEY, API 토큰을 Git에 커밋하지 않는다. 비밀값을 터미널이나 로그에 출력하지 않는다.
- .env.example에는 변수 이름과 비밀값이 아닌 예시만 기록한다.
- 실제 카드 이용내역 대신 가상 데이터를 사용한다. 실제 내역과 API 응답을 로그나 Git에 남기지 않는다.
- 패키지를 추가하면 requirements.txt에 반영한다. 가상환경은 커밋하지 않는다.

## Windows 환경과 확인
- PowerShell에서 프로젝트 루트를 기준으로 실행한다.
- 가상환경 생성: `python -m venv .venv`
- 선택적 활성화: `.\.venv\Scripts\Activate.ps1`
- 활성화가 차단되어도 실행 정책을 바꾸지 않고 아래 가상환경 Python을 직접 사용할 수 있다.
- 설치: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
- 의존성 검사: `.\.venv\Scripts\python.exe -m pip check`
- Django 검사: `.\.venv\Scripts\python.exe manage.py check`
- 로컬 실행: `.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --noreload`
- 서버 중지: `Ctrl+C`
- .env는 .env.example을 참고하여 로컬에서 만들고 Python secrets로 고유한 SECRET_KEY를 생성해 파일에 직접 기록한다. 키를 터미널에 출력하지 않는다.
- 환경 구축만 검증할 때 migrate나 createsuperuser는 실행하지 않는다. 초기 미적용 마이그레이션 안내는 별도로 보고한다.
- 코드 작성 후 변경 범위에 맞는 실행 또는 테스트를 수행한다. Django 실행에 영향을 주는 변경은 Django 검사와 실제 로컬 HTTP 응답도 확인한다.
- 문서만 변경한 경우 내용과 변경 형식을 점검한다.
- 작업 완료 시 변경 파일, 변경 이유, 실행 명령과 테스트 결과를 요약한다. 미완료 사항이 있으면 함께 기록한다.
