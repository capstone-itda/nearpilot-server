package com.nearpilot.app.approval

import com.nearpilot.app.account.ApiResult
import com.nearpilot.app.account.ReasonMessages
import java.time.Duration
import java.time.Instant

/** 승인 화면과 알림에 쓰는 문장. */
object ApprovalText {

    fun actionLabel(action: String): String = when (action) {
        "open" -> "열기"
        "on" -> "켜기"
        "off" -> "끄기"
        else -> action
    }

    fun summary(item: ApprovalItem): String = "${item.nodeId} ${actionLabel(item.action)} 요청"

    /** 대기 만료까지 남은 시간. 지났으면 "만료됨"이다. */
    fun remaining(expiresAt: Instant?, now: Instant): String {
        if (expiresAt == null) return ""
        val left = Duration.between(now, expiresAt)
        if (left.isNegative || left.isZero) return "만료됨"
        val minutes = left.toMinutes()
        val seconds = left.seconds % 60
        return if (minutes > 0) "${minutes}분 ${seconds}초 남음" else "${seconds}초 남음"
    }

    fun isExpired(item: ApprovalItem, now: Instant): Boolean =
        item.pendingExpiresAt?.let { !it.isAfter(now) } ?: false

    fun decided(choice: ApprovalChoice): String = when (choice) {
        ApprovalChoice.APPROVE -> "승인했어요. 방문자가 AI 비서로 다시 요청하면 기기를 쓸 수 있어요."
        ApprovalChoice.DENY -> "거부했어요."
    }

    fun failure(result: ApiResult<*>): String = when {
        result is ApiResult.UnexpectedResponse && result.status == 404 ->
            "서버에 승인 기능이 아직 없어요. 서버를 최신 버전으로 업데이트한 뒤 다시 시도해 주세요."
        result is ApiResult.Rejected -> when (result.reason) {
            "ALREADY_DECIDED" -> "이미 다른 결정이 반영된 요청이에요. 목록을 새로 고쳤어요."
            "POLICY_VERSION_MISMATCH" -> "그사이 기기의 접근 정책이 바뀌어서 이 요청은 더 이상 결정할 수 없어요. 방문자가 다시 요청해야 해요."
            "REQUEST_EXPIRED" -> "대기 시간(10분)이 지나 만료된 요청이에요. 방문자가 다시 요청해야 해요."
            "NOT_AUTHORIZED" -> "이 기기의 승인 권한이 없어요. 소유자 계정의 토큰인지 확인해 주세요."
            "NOT_FOUND" -> "없는 승인 요청이에요. 목록을 새로 고쳤어요."
            else -> ReasonMessages.ofReason(result.reason)
        }
        else -> ReasonMessages.of(result)
    }
}

/** 새 승인 요청만 골라 알린다. 대기 목록에서 사라진 ID는 잊는다. */
object NewApprovalDetector {
    data class Outcome(val toNotify: List<ApprovalItem>, val seen: Set<String>)

    fun detect(seen: Set<String>, pending: List<ApprovalItem>): Outcome {
        val current = pending.map { it.approvalId }.toSet()
        return Outcome(toNotify = pending.filter { it.approvalId !in seen }, seen = current)
    }
}

/** 한 번의 조회 결과로 다음 동작을 정한다. */
object PollPolicy {
    enum class Next { CONTINUE, STOP }

    fun after(result: ApiResult<*>): Next = when (result) {
        is ApiResult.Rejected -> if (result.status == 401) Next.STOP else Next.CONTINUE
        else -> Next.CONTINUE
    }
}
