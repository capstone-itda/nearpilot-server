package com.nearpilot.app.beacon

/** 상태 카드의 색 단계. */
enum class StatusTone { IDLE, BUSY, OK, PROBLEM }

/** 상태 카드에서 사용자가 바로 누를 수 있는 해결 동작. */
enum class StatusAction { NONE, OPEN_BLUETOOTH_SETTINGS, REQUEST_PERMISSION, RETRY }

/** 송출 상태를 사람이 읽는 문장과 해결 동작으로 바꾼 것. */
data class StatusPresentation(
    val tone: StatusTone,
    val title: String,
    val message: String,
    val action: StatusAction = StatusAction.NONE,
)

fun BroadcastState.presentation(): StatusPresentation = when (this) {
    BroadcastState.Stopped -> StatusPresentation(
        StatusTone.IDLE,
        "송출 꺼짐",
        "'송출 시작'을 누르면 주변 기기가 이 폰을 알아볼 수 있어요.",
    )
    BroadcastState.Starting -> StatusPresentation(
        StatusTone.BUSY,
        "송출 준비 중",
        "블루투스 신호를 켜고 있어요. 잠시만 기다려 주세요.",
    )
    BroadcastState.Broadcasting -> StatusPresentation(
        StatusTone.OK,
        "송출 중",
        "주변 기기가 이 폰을 알아볼 수 있어요. 화면을 끄거나 다른 앱을 써도 계속 보내요.",
    )
    is BroadcastState.Blocked -> when (reason) {
        AdvertiserAvailability.BLUETOOTH_OFF -> StatusPresentation(
            StatusTone.PROBLEM,
            "블루투스가 꺼져 있어요",
            "블루투스를 켠 뒤 다시 시도해 주세요.",
            StatusAction.OPEN_BLUETOOTH_SETTINGS,
        )
        AdvertiserAvailability.PERMISSION_MISSING -> StatusPresentation(
            StatusTone.PROBLEM,
            "권한이 필요해요",
            "'근처 기기' 권한이 있어야 비콘 신호를 보낼 수 있어요. 위치를 추적하는 데는 쓰지 않아요.",
            StatusAction.REQUEST_PERMISSION,
        )
        AdvertiserAvailability.UNSUPPORTED -> StatusPresentation(
            StatusTone.PROBLEM,
            "이 폰은 지원하지 않아요",
            "이 폰에는 블루투스 광고(BLE) 기능이 없어서 비콘으로 쓸 수 없어요. 다른 Android 폰을 사용해 주세요.",
        )
        AdvertiserAvailability.READY -> StatusPresentation(
            StatusTone.PROBLEM,
            "시작하지 못했어요",
            "다시 시도해 주세요.",
            StatusAction.RETRY,
        )
    }
    is BroadcastState.Interrupted -> when (val r = reason) {
        Interruption.BluetoothOff -> StatusPresentation(
            StatusTone.PROBLEM,
            "송출이 멈췄어요",
            "블루투스가 꺼져서 송출이 멈췄어요. 블루투스를 켜고 다시 시작해 주세요.",
            StatusAction.OPEN_BLUETOOTH_SETTINGS,
        )
        is Interruption.StartFailed -> StatusPresentation(
            StatusTone.PROBLEM,
            "송출을 시작하지 못했어요",
            startFailureHint(r.errorCode) + " (오류 코드 ${r.errorCode})",
            StatusAction.RETRY,
        )
        Interruption.ServiceStopped -> StatusPresentation(
            StatusTone.PROBLEM,
            "송출이 멈췄어요",
            "시스템이 송출을 종료했어요. 다시 시작해 주세요. 배터리 최적화에서 이 앱을 제외하면 덜 멈춰요.",
            StatusAction.RETRY,
        )
    }
}

// AdvertiseCallback.ADVERTISE_FAILED_* 값. JVM 테스트에서 Android 클래스를 쓰지 않으려고 숫자로 둔다.
private fun startFailureHint(errorCode: Int): String = when (errorCode) {
    2 -> "다른 앱이 블루투스 광고를 많이 쓰고 있어요. 다른 비콘 앱을 끄고 다시 시도해 주세요."
    5 -> "이 폰은 블루투스 광고를 지원하지 않아요."
    else -> "블루투스를 껐다 켠 뒤 다시 시도해 주세요."
}
