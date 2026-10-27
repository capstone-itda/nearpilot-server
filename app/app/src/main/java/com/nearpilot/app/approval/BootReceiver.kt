package com.nearpilot.app.approval

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** 재부팅·앱 갱신 뒤 승인 알림 조회를 다시 시작한다 (FR-03, spec 6절). */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (BootRestart.shouldStart(intent.action, ApprovalStore(context).watching)) {
            ApprovalWatchService.start(context)
        }
    }
}

/** 받은 동작과 저장한 설정으로 조회를 다시 시작할지 정한다. Android API 없이 테스트한다. */
object BootRestart {
    private const val ACTION_BOOT_COMPLETED = "android.intent.action.BOOT_COMPLETED"
    private const val ACTION_MY_PACKAGE_REPLACED = "android.intent.action.MY_PACKAGE_REPLACED"

    fun shouldStart(action: String?, watching: Boolean): Boolean =
        watching && (action == ACTION_BOOT_COMPLETED || action == ACTION_MY_PACKAGE_REPLACED)
}
