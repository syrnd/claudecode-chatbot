---
project_name: "daily-fortune"
type: web

stack:
  frontend: "nextjs"
  backend: "nextjs"
  database: "sqlite"
  infra: "docker-compose"

deploy:
  target: "ssh-docker"
  server_host: ""
  server_dir: "/srv/daily-fortune"
  ci_cd: "none"

extensions:
  security_baseline: true
  property_based_testing: false

review:
  codex: true
  codex_severity: "high"

nfr:
  estimated_users: ""
  response_time: "500ms 이내"
  auth_method: "none"
---

## 개요

생년월일과 별자리를 기반으로 매일의 운세를 제공하는 웹사이트. 사용자가 생년월일과 별자리를 입력하면 오늘의 종합운세, 사랑/취업/금전 운세를 확인할 수 있다.

## 주요 기능

1. **생년월일 입력** - 사용자가 자신의 생년월일(양력)을 입력
2. **별자리 선택** - 12별자리 중 하나를 선택
3. **운세 보기** - 오늘의 종합운세, 사랑/취업/금전 카테고리별 운세 표시
4. **매일 업데이트** - 매일 자정에 운세 데이터 갱신

## 데이터 모델

**Fortune (운세)**
- id: 고유 식별자
- zodiac_sign: 별자리 (12개)
- date: 날짜 (YYYY-MM-DD)
- overall: 종합운세 (0-100 점수 + 텍스트)
- love: 사랑운세 (0-100 점수 + 텍스트)
- career: 취업운세 (0-100 점수 + 텍스트)
- money: 금전운세 (0-100 점수 + 텍스트)
- lucky_color: 행운의 색깔
- lucky_number: 행운의 숫자
- lucky_direction: 행운의 방향

## API 엔드포인트

- `GET /api/fortune?birthdate=YYYY-MM-DD&zodiac=사수자리` - 오늘의 운세 조회
- `GET /api/zodiacs` - 12별자리 목록 반환

## 디자인

- **스타일**: 현대적이고 세련된 디자인 (Minimal + Premium feel)
- **색상**: 심플한 배경에 포인트 컬러로 별자리 테마 컬러 활용
- **레이아웃**: 반응형, 모바일 우선

## 제약사항

- 별자리 데이터는 서버 내부에서 직접 생성 (외부 API 미사용)
- 사용자 인증 없음 (익명 사용)
- 모바일/데스크톱 반응형 디자인 지원
