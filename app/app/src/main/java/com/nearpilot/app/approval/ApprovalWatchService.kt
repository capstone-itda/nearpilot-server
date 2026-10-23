package com.nearpilot.app.approval

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.nearpilot.app.MainActivity
import com.nearpilot.app.account.AccountStore
import com.nearpilot.app.account.ApiResult
import com.nearpilot.app.account.UrlConnectionTransport
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** 서버에 새 승인 요청을 주기적으로 묻고 알린다 (FR-03, spec 6절). */
class ApprovalWatchService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var loop: Job? = null
    private lateinit var approvals: ApprovalStore
    private lateinit var accounts: AccountStore

    override fun onCreate() {
        super.onCreate()
        approvals = ApprovalStore(this)
        accounts = AccountStore(this)
        createChannels(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!enterForeground()) {
            approvals.watching = false
            stopSelf()
            return START_NOT_STICKY
        }
        if (loop?.isActive != true) loop = scope.launch { poll() }
        return START_STICKY
    }

    private suspend fun poll() {
        while (scope.isActive) {
            val server = accounts.serverUrl
            val token = accounts.token
            if (server == null || token == null) {
                stopWithNotice("계정 탭에서 서버와 토큰을 먼저 연결해 주세요.")
                return
            }
            val result = ApprovalApi(server, UrlConnectionTransport()).list(token)
            if (result is ApiResult.Success) {
                val outcome = NewApprovalDetector.detect(approvals.seenIds, result.value)
                outcome.toNotify.forEach(::notifyApproval)
                approvals.seenIds = outcome.seen
            }
            if (PollPolicy.after(result) == PollPolicy.Next.STOP) {
                stopWithNotice("토큰이 맞지 않아 승인 알림을 멈췄어요. 계정 탭에서 토큰을 확인해 주세요.")
                return
            }
            delay(POLL_INTERVAL_MILLIS)
        }
    }

    private fun stopWithNotice(message: String) {
        approvals.watching = false
        notificationManager().notify(
            NOTICE_ID,
            NotificationCompat.Builder(this, CHANNEL_REQUESTS)
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle("승인 알림이 멈췄어요")
                .setContentText(message)
                .setStyle(NotificationCompat.BigTextStyle().bigText(message))
                .setContentIntent(openApp(null))
                .setAutoCancel(true)
                .build(),
        )
        stopSelf()
    }

    private fun notifyApproval(item: ApprovalItem) {
        val notification = NotificationCompat.Builder(this, CHANNEL_REQUESTS)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("새 승인 요청")
            .setContentText("${ApprovalText.summary(item)} · 눌러서 승인하거나 거부해 주세요")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setContentIntent(openApp(item.approvalId))
            .setAutoCancel(true)
            .build()
        notificationManager().notify(item.approvalId.hashCode(), notification)
    }

    private fun openApp(approvalId: String?): PendingIntent {
        val intent = Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(EXTRA_OPEN_APPROVALS, true)
            .putExtra(EXTRA_APPROVAL_ID, approvalId)
        return PendingIntent.getActivity(
            this, approvalId?.hashCode() ?: 0, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    private fun enterForeground(): Boolean {
        val notification: Notification = NotificationCompat.Builder(this, CHANNEL_WATCH)
            .setSmallIcon(android.R.drawable.ic_popup_sync)
            .setContentTitle("승인 요청 확인 중")
            .setContentText("새 승인 요청이 오면 알려 드릴게요.")
            .setContentIntent(openApp(null))
            .setOngoing(true)
            .build()
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                startForeground(WATCH_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                startForeground(WATCH_ID, notification)
            }
            true
        } catch (e: RuntimeException) {
            false
        }
    }

    private fun notificationManager() = getSystemService(NotificationManager::class.java)

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        const val EXTRA_OPEN_APPROVALS = "open_approvals"
        const val EXTRA_APPROVAL_ID = "approval_id"
        private const val CHANNEL_WATCH = "approval_watch"
        private const val CHANNEL_REQUESTS = "approval_requests"
        private const val WATCH_ID = 2
        private const val NOTICE_ID = 3

        /** 대기 만료(10분) 안에 충분히 알린다 (spec 6절). */
        private const val POLL_INTERVAL_MILLIS = 10_000L

        fun start(context: Context) {
            ApprovalStore(context).watching = true
            ContextCompat.startForegroundService(context, Intent(context, ApprovalWatchService::class.java))
        }

        fun stop(context: Context) {
            ApprovalStore(context).watching = false
            context.stopService(Intent(context, ApprovalWatchService::class.java))
        }

        private fun createChannels(context: Context) {
            val manager = context.getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_WATCH, "승인 요청 확인", NotificationManager.IMPORTANCE_LOW))
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_REQUESTS, "새 승인 요청", NotificationManager.IMPORTANCE_HIGH))
        }
    }
}

/** 승인 알림 설정과 이미 알린 승인 ID. */
class ApprovalStore(context: Context) {
    private val prefs = context.getSharedPreferences("approval", Context.MODE_PRIVATE)

    var watching: Boolean
        get() = prefs.getBoolean("watching", false)
        set(value) = prefs.edit().putBoolean("watching", value).apply()

    var seenIds: Set<String>
        get() = prefs.getStringSet("seen_ids", emptySet()).orEmpty().toSet()
        set(value) = prefs.edit().putStringSet("seen_ids", value).apply()
}
