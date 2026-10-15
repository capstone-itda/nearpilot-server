"""FR-01·FR-08·NFR-08의 발급·조회 공통 토큰 해시."""

from hashlib import sha256


def hash_token(token: str) -> str:
    """토큰 원문을 UTF-8로 해시한다. 공백을 지우거나 값을 정규화하지 않는다."""
    return sha256(token.encode('utf-8')).hexdigest()
