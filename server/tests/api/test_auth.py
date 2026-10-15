"""FR-08 토큰으로 계정 식별. 계정 조회는 fake 로 대신한다."""

from __future__ import annotations

import pytest

from nearpilot.api.auth.authenticator import AuthenticationError, Authenticator
from nearpilot.shared.enums import ReasonCode, Role
from nearpilot.shared.models import Account
from nearpilot.shared.tokens import hash_token

TOKEN = "tok_AbC-123_xyz"
ACCOUNT = Account("u1", Role.VISITOR)


class FakeAccounts:
    def __init__(self, known: dict[str, Account] | None = None):
        self.known = {hash_token(TOKEN): ACCOUNT} if known is None else known
        self.calls: list[str] = []
        self.error: Exception | None = None

    def account_by_token_hash(self, token_hash):
        self.calls.append(token_hash)
        if self.error:
            raise self.error
        return self.known.get(token_hash)


def reason_of(auth: Authenticator, header) -> ReasonCode:
    with pytest.raises(AuthenticationError) as e:
        auth.authenticate(header)
    return e.value.reason


def test_FR08_유효한_토큰은_해시로_조회해서_계정을_반환한다():
    accounts = FakeAccounts()
    assert Authenticator(accounts).authenticate(f"Bearer {TOKEN}") == ACCOUNT
    assert accounts.calls == [hash_token(TOKEN)]  # 원문이 아니라 해시를 넘긴다


@pytest.mark.parametrize("header", [None, ""])
def test_FR08_헤더가_없으면_INVALID_TOKEN이고_조회하지_않는다(header):
    accounts = FakeAccounts()
    assert reason_of(Authenticator(accounts), header) is ReasonCode.INVALID_TOKEN
    assert accounts.calls == []


@pytest.mark.parametrize("header", [f"Basic {TOKEN}", TOKEN, f"Token {TOKEN}", f"Bearer{TOKEN}", f"X Bearer {TOKEN}"])
def test_FR08_Bearer가_아니면_INVALID_TOKEN이고_조회하지_않는다(header):
    accounts = FakeAccounts()
    assert reason_of(Authenticator(accounts), header) is ReasonCode.INVALID_TOKEN
    assert accounts.calls == []


@pytest.mark.parametrize("header", ["Bearer ", "Bearer", f"Bearer  {TOKEN}", f"Bearer {TOKEN} ", f"Bearer {TOKEN} x", "Bearer a\tb"])
def test_FR08_토큰이_비었거나_공백을_포함하면_INVALID_TOKEN(header):
    accounts = FakeAccounts()
    assert reason_of(Authenticator(accounts), header) is ReasonCode.INVALID_TOKEN
    assert accounts.calls == []


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr"])
def test_FR08_Bearer_체계_이름은_대소문자를_구분하지_않는다(scheme):
    assert Authenticator(FakeAccounts()).authenticate(f"{scheme} {TOKEN}") == ACCOUNT


def test_FR08_등록되지_않은_토큰은_INVALID_TOKEN():
    accounts = FakeAccounts()
    assert reason_of(Authenticator(accounts), "Bearer unknown-token") is ReasonCode.INVALID_TOKEN
    assert accounts.calls == [hash_token("unknown-token")]


def test_FR08_토큰을_정규화하지_않는다():
    accounts = FakeAccounts()
    assert reason_of(Authenticator(accounts), f"Bearer {TOKEN.upper()}") is ReasonCode.INVALID_TOKEN
    assert accounts.calls == [hash_token(TOKEN.upper())]  # 대소문자를 바꾸지 않는다


def test_FR08_계정_조회가_실패하면_INTERNAL_ERROR():
    accounts = FakeAccounts()
    accounts.error = RuntimeError("secret internal detail")
    with pytest.raises(AuthenticationError) as e:
        Authenticator(accounts).authenticate(f"Bearer {TOKEN}")
    assert e.value.reason is ReasonCode.INTERNAL_ERROR
    assert "secret" not in str(e.value) and e.value.__cause__ is None


def test_FR08_같은_헤더에는_같은_결과를_반환한다():
    auth = Authenticator(FakeAccounts())
    assert auth.authenticate(f"Bearer {TOKEN}") == auth.authenticate(f"Bearer {TOKEN}")
    assert reason_of(auth, "Bearer nope") is reason_of(auth, "Bearer nope")


@pytest.mark.parametrize("header", [f"Bearer {TOKEN}x", f"Basic {TOKEN}", None])
def test_NFR08_거부_오류에_토큰_원문이_없다(header):
    with pytest.raises(AuthenticationError) as e:
        Authenticator(FakeAccounts()).authenticate(header)
    assert TOKEN not in str(e.value) and TOKEN not in repr(e.value)
    assert not hasattr(e.value, "token")
