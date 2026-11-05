package com.nearpilot.app.provision

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import java.util.UUID

/** 근처에서 설정 서비스를 알리는 기기. 광고 이름은 `NP-<node_id>`, 설정이 없으면 `NP-setup`이다. */
data class SetupNode(val name: String, val address: String, val rssi: Int)

sealed interface ProvisionOutcome {
    data class Saved(val nodeId: String) : ProvisionOutcome
    data class Failed(val message: String) : ProvisionOutcome
}

/**
 * 기기 펌웨어의 BLE 설정 서비스 (#234, architecture.md §5 "BLE 기기 설정").
 * 쓰기 하나가 줄 하나다. 응답은 20바이트씩 알림으로 와서 [LineJoiner]로 잇는다.
 * 응답 줄을 받은 뒤에 다음 줄을 쓴다. 명령 특성은 암호화한 링크에서만 쓰기를 받으므로
 * 첫 쓰기에서 시스템이 페어링(Just Works)을 시작하고, 앱은 그 쓰기를 다시 보낸다.
 * 모든 콜백은 메인 스레드로 옮겨 [Listener]에 전한다. 권한 확인은 부르는 쪽이 한다.
 */
@SuppressLint("MissingPermission")
class BleProvisioner(private val context: Context, private val listener: Listener) {

    interface Listener {
        fun onFound(node: SetupNode)
        fun onScanFailed(message: String)
        fun onProgress(done: Int, total: Int, label: String)
        fun onFinished(outcome: ProvisionOutcome)
    }

    companion object {
        // firmware/include/config.example.h 의 고정 값과 같다.
        val SERVICE: UUID = UUID.fromString("6e9d0001-5a3c-4b8e-9f1d-4e5050494c54")
        val COMMAND: UUID = UUID.fromString("6e9d0002-5a3c-4b8e-9f1d-4e5050494c54")
        val REPLY: UUID = UUID.fromString("6e9d0003-5a3c-4b8e-9f1d-4e5050494c54")
        private val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        private const val MTU = 247
        private const val STEP_TIMEOUT_MS = 10_000L
        private const val AUTH_RETRY_MS = 2_000L
        private const val AUTH_RETRIES = 5

        fun permissions(): Array<String> =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
                arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
            // Android 11 이하에서 BLE 검색은 위치 권한이 필요하다.
            else arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)

        fun hasPermissions(context: Context): Boolean =
            permissions().all { context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }
    }

    private val main = Handler(Looper.getMainLooper())
    private val adapter = context.getSystemService(BluetoothManager::class.java)?.adapter
    private var scanning = false

    private var gatt: BluetoothGatt? = null
    private var command: BluetoothGattCharacteristic? = null
    private var steps: List<ConfigStep> = emptyList()
    private var index = 0
    private var writeInFlight = false
    private var advanceAfterWrite = false
    private var authRetries = 0
    private var finished = true
    private var nodeId = ""
    private var joiner = LineJoiner()
    private val timeout = Runnable { fail("기기가 응답하지 않아요 (${steps.getOrNull(index)?.label ?: "연결"}).") }

    // ── 검색 ─────────────────────────────────────────────────────────

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val name = result.scanRecord?.deviceName ?: result.device.name ?: "NP-?"
            val node = SetupNode(name, result.device.address, result.rssi)
            main.post { listener.onFound(node) }
        }

        override fun onScanFailed(errorCode: Int) {
            scanning = false
            main.post { listener.onScanFailed("기기 검색에 실패했어요 (코드 $errorCode).") }
        }
    }

    fun startScan(): Boolean {
        val scanner = adapter?.takeIf { it.isEnabled }?.bluetoothLeScanner ?: return false
        if (scanning) return true
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(SERVICE)).build()
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        scanner.startScan(listOf(filter), settings, scanCallback)
        scanning = true
        return true
    }

    fun stopScan() {
        if (!scanning) return
        scanning = false
        adapter?.bluetoothLeScanner?.stopScan(scanCallback)
    }

    // ── 설정 쓰기 ────────────────────────────────────────────────────

    fun provision(address: String, nodeId: String, plan: List<ConfigStep>) {
        stopScan()
        close()
        finished = false
        this.nodeId = nodeId
        val device: BluetoothDevice = adapter?.getRemoteDevice(address) ?: return fail("블루투스를 켜 주세요.")
        steps = plan
        index = 0
        writeInFlight = false
        advanceAfterWrite = false
        authRetries = 0
        joiner = LineJoiner()
        main.postDelayed(timeout, STEP_TIMEOUT_MS)
        gatt = device.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
    }

    /** 연결을 끊고 진행 중인 설정을 버린다. 결과는 알리지 않는다. */
    fun close() {
        finished = true
        main.removeCallbacks(timeout)
        gatt?.let {
            it.disconnect()
            it.close()
        }
        gatt = null
        command = null
    }

    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            main.post { connectionChanged(g, newState) }
        }

        private fun connectionChanged(g: BluetoothGatt, newState: Int) {
            if (g != gatt) return
            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> if (!g.requestMtu(MTU)) g.discoverServices()
                BluetoothProfile.STATE_DISCONNECTED -> fail("기기와 연결이 끊겼어요. 설정 모드인지 확인해 주세요.")
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            main.post { if (g == gatt) g.discoverServices() }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            main.post { servicesFound(g, status) }
        }

        @Suppress("DEPRECATION")
        private fun servicesFound(g: BluetoothGatt, status: Int) {
            if (g != gatt) return
            val service = g.getService(SERVICE)
            val reply = service?.getCharacteristic(REPLY)
            command = service?.getCharacteristic(COMMAND)
            if (status != BluetoothGatt.GATT_SUCCESS || reply == null || command == null) {
                return fail("NearPilot 설정 서비스를 찾지 못했어요.")
            }
            g.setCharacteristicNotification(reply, true)
            val cccd = reply.getDescriptor(CCCD) ?: return fail("기기 알림을 켜지 못했어요.")
            val enable = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) g.writeDescriptor(cccd, enable)
            else {
                cccd.value = enable
                g.writeDescriptor(cccd)
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
            main.post {
                if (g != gatt) return@post
                if (status != BluetoothGatt.GATT_SUCCESS) fail("기기 알림을 켜지 못했어요 ($status).") else sendCurrent()
            }
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            main.post { written(g, status) }
        }

        private fun written(g: BluetoothGatt, status: Int) {
            if (g != gatt) return
            writeInFlight = false
            when (status) {
                BluetoothGatt.GATT_SUCCESS -> if (advanceAfterWrite) advance()
                // 암호화 전 쓰기다. 시스템이 페어링을 마치면 같은 줄을 다시 쓴다.
                BluetoothGatt.GATT_INSUFFICIENT_AUTHENTICATION, BluetoothGatt.GATT_INSUFFICIENT_ENCRYPTION ->
                    if (authRetries++ < AUTH_RETRIES) main.postDelayed({ if (!finished) sendCurrent() }, AUTH_RETRY_MS)
                    else fail("기기와 페어링하지 못했어요. 페어링 요청을 허용해 주세요.")
                else -> fail("기기에 쓰지 못했어요 ($status).")
            }
        }

        @Deprecated("Android 13 미만")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            @Suppress("DEPRECATION")
            val value = c.value ?: return
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) received(g, value.copyOf())
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            received(g, value)
        }
    }

    private fun received(g: BluetoothGatt, chunk: ByteArray) {
        main.post {
            if (g != gatt || finished) return@post
            for (line in joiner.feed(chunk)) handle(ConfigLines.parse(line))
        }
    }

    private fun handle(reply: ConfigReply) {
        val step = steps.getOrNull(index) ?: return
        when {
            reply is ConfigReply.Rejected -> fail(ConfigLines.rejectionText(reply))
            // 저장 뒤 기기는 곧 다시 시작하므로 마지막 줄은 쓰기 응답을 기다리지 않는다.
            step.accepts(reply) ->
                if (writeInFlight && index < steps.size - 1) advanceAfterWrite = true else advance()
            // 다른 응답(시리얼 진단 줄 등)은 무시한다.
        }
    }

    private fun advance() {
        advanceAfterWrite = false
        index += 1
        listener.onProgress(index, steps.size, steps.getOrNull(index - 1)?.label ?: "")
        if (index < steps.size) return sendCurrent()
        // CONFIG_SAVED 뒤 기기는 다시 시작한다. 연결이 끊기는 것은 정상이다.
        finish(ProvisionOutcome.Saved(nodeId))
    }

    @Suppress("DEPRECATION")
    private fun sendCurrent() {
        val g = gatt ?: return
        val c = command ?: return
        val step = steps.getOrNull(index) ?: return
        main.removeCallbacks(timeout)
        main.postDelayed(timeout, STEP_TIMEOUT_MS)
        val bytes = step.line.toByteArray(Charsets.UTF_8)
        writeInFlight = true
        val started = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            g.writeCharacteristic(c, bytes, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothGatt.GATT_SUCCESS
        } else {
            c.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            c.value = bytes
            g.writeCharacteristic(c)
        }
        if (!started) {
            writeInFlight = false
            fail("기기에 쓰지 못했어요.")
        }
    }

    private fun fail(message: String) = finish(ProvisionOutcome.Failed(message))

    private fun finish(outcome: ProvisionOutcome) {
        if (finished) return
        close()
        listener.onFinished(outcome)
    }
}
