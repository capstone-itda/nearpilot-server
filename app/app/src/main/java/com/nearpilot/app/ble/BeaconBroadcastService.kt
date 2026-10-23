package com.nearpilot.app.ble

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.nearpilot.app.MainActivity
import com.nearpilot.app.beacon.BeaconIdentifier
import com.nearpilot.app.beacon.BroadcastController
import com.nearpilot.app.beacon.BroadcastState
import com.nearpilot.app.beacon.IdentifierParseResult
import com.nearpilot.app.beacon.presentation

/** 화면 밖에서도 송출을 유지하는 포그라운드 서비스. */
class BeaconBroadcastService : Service() {

    private lateinit var controller: BroadcastController
    private var foreground = false

    private val bluetoothReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR)) {
                BluetoothAdapter.STATE_TURNING_OFF, BluetoothAdapter.STATE_OFF -> controller.onBluetoothOff()
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        createChannel()
        controller = BroadcastController(BleBeaconAdvertiser(this, MEASURED_POWER_DBM), ::onStateChanged)
        ContextCompat.registerReceiver(
            this,
            bluetoothReceiver,
            IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED),
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> handleStart(intent)
            ACTION_STOP -> controller.stop()
            else -> stopSelf()
        }
        return START_NOT_STICKY
    }

    private fun handleStart(intent: Intent) {
        // startForegroundService()로 시작했으므로 먼저 포그라운드로 올린다.
        if (!enterForeground()) {
            stopSelf()
            return
        }
        val parsed = BeaconIdentifier.parse(
            intent.getStringExtra(EXTRA_UUID).orEmpty(),
            intent.getStringExtra(EXTRA_MAJOR).orEmpty(),
            intent.getStringExtra(EXTRA_MINOR).orEmpty(),
        )
        if (parsed !is IdentifierParseResult.Valid) {
            controller.stop()
            return
        }
        controller.start(parsed.identifier)
    }

    private fun onStateChanged(state: BroadcastState) {
        BroadcastStatus.update(state)
        when (state) {
            BroadcastState.Starting, BroadcastState.Broadcasting -> notify(state, ongoing = true)
            is BroadcastState.Interrupted -> {
                leaveForeground(keepNotification = true)
                notify(state, ongoing = false)
                stopSelf()
            }
            BroadcastState.Stopped, is BroadcastState.Blocked -> {
                leaveForeground(keepNotification = false)
                stopSelf()
            }
        }
    }

    override fun onDestroy() {
        controller.onServiceStopping()
        unregisterReceiver(bluetoothReceiver)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun enterForeground(): Boolean {
        if (foreground) return true
        val notification = buildNotification(BroadcastState.Starting, ongoing = true)
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
            foreground = true
            true
        } catch (e: RuntimeException) {
            // 권한이 없으면 Android 14 이상에서 SecurityException이 난다.
            false
        }
    }

    private fun leaveForeground(keepNotification: Boolean) {
        if (!foreground) return
        stopForeground(if (keepNotification) STOP_FOREGROUND_DETACH else STOP_FOREGROUND_REMOVE)
        foreground = false
    }

    private fun notify(state: BroadcastState, ongoing: Boolean) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, buildNotification(state, ongoing))
    }

    private fun buildNotification(state: BroadcastState, ongoing: Boolean): Notification {
        val openApp = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val presentation = state.presentation()
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setContentTitle("NearPilot 비콘 · ${presentation.title}")
            .setContentText(presentation.message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(presentation.message))
            .setContentIntent(openApp)
            .setOngoing(ongoing)
            .setOnlyAlertOnce(true)
            .build()
    }

    private fun createChannel() {
        val channel = NotificationChannel(CHANNEL_ID, "비콘 송출", NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    companion object {
        private const val ACTION_START = "com.nearpilot.app.action.START_BROADCAST"
        private const val ACTION_STOP = "com.nearpilot.app.action.STOP_BROADCAST"
        private const val EXTRA_UUID = "uuid"
        private const val EXTRA_MAJOR = "major"
        private const val EXTRA_MINOR = "minor"
        private const val CHANNEL_ID = "beacon_broadcast"
        private const val NOTIFICATION_ID = 1

        /** iBeacon의 1m 기준 측정 전력(dBm). 서버 판정에는 쓰지 않는다. */
        private const val MEASURED_POWER_DBM = -59

        fun start(context: Context, identifier: BeaconIdentifier) {
            val intent = Intent(context, BeaconBroadcastService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_UUID, identifier.uuid)
                .putExtra(EXTRA_MAJOR, identifier.major.toString())
                .putExtra(EXTRA_MINOR, identifier.minor.toString())
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            context.startService(Intent(context, BeaconBroadcastService::class.java).setAction(ACTION_STOP))
        }
    }
}
