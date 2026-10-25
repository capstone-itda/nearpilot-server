package com.nearpilot.app.account

/** 서버 응답을 사용자가 할 일이 담긴 문장으로 바꾼다. */
object ReasonMessages {

    fun of(result: ApiResult<*>): String = when (result) {
        is ApiResult.Success -> ""
        ApiResult.NetworkError ->
            "서버에 연결하지 못했어요. 서버 주소가 맞는지, 폰이 서버와 같은 Wi-Fi에 있는지, 서버가 켜져 있는지 확인해 주세요."
        is ApiResult.UnexpectedResponse ->
            "서버가 알 수 없는 응답을 보냈어요 (HTTP ${result.status}). 서버 주소와 서버 버전을 확인해 주세요."
        is ApiResult.Rejected -> ofReason(result.reason)
    }

    fun ofReason(reason: String): String = when (reason) {
        "INVALID_TOKEN" -> "토큰이 맞지 않아요. 토큰을 다시 넣거나, 토큰을 지우고 새 계정을 만들어 주세요."
        "BEACON_ALREADY_REGISTERED" ->
            "이 비콘 값은 다른 계정이 이미 쓰고 있어요. 비콘 탭에서 '새 값 만들기'를 누른 뒤 다시 등록해 주세요."
        "ACCOUNT_ALREADY_EXISTS" -> "계정을 만들지 못했어요. 잠시 뒤 다시 시도해 주세요."
        "INVALID_INPUT" -> "이름을 1~64자로 입력해 주세요."
        "INVALID_BEACON" -> "비콘 값이 올바르지 않아요. 비콘 탭에서 값을 확인해 주세요."
        "STATE_CONFLICT" -> "서버의 비콘 정보와 충돌했어요. 비콘 탭에서 '새 값 만들기'를 누른 뒤 다시 등록해 주세요."
        "INTERNAL_ERROR" -> "서버에서 오류가 났어요. 잠시 뒤 다시 시도하고, 계속되면 서버 담당자에게 알려 주세요."
        else -> "요청이 거부됐어요 ($reason)."
    }
}
