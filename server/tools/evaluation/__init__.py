"""평가 기반: 읽기 전용 DB 연결과 결과 형식 (specs/db-evaluation-foundation/spec.md).

지표별 평가 스크립트가 공통으로 쓰는 두 가지만 담는다.
- `db`: 운영 DB 파일을 안전하게 읽기 전용으로 여는 연결 함수
- `report`: CSV·Markdown 보고서를 같은 형식으로 만드는 함수
"""
