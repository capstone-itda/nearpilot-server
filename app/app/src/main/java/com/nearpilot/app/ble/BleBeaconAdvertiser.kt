package com.nearpilot.app.ble

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import com.nearpilot.app.beacon.AdvertiserAvailability
import com.nearpilot.app.beacon.BeaconAdvertiser
import com.nearpilot.app.beacon.BeaconIdentifier
import com.nearpilot.app.beacon.IBeaconPacket

/** Android `BluetoothLeAdvertiser`로 iBeacon을 광고한다. */
class BleBeaconAdvertiser(
    private val context: Context,
    private val measuredPower: Int,
) : BeaconAdvertiser {

    private var activeCallback: AdvertiseCallback? = null

    private val adapter: BluetoothAdapter?
        get() = context.getSystemService(BluetoothManager::class.java)?.adapter

    override fun availability(): AdvertiserAvailability {
        val adapter = adapter ?: return AdvertiserAvailability.UNSUPPORTED
        if (!BeaconPermissions.canAdvertise(context)) return AdvertiserAvailability.PERMISSION_MISSING
        if (!adapter.isEnabled) return AdvertiserAvailability.BLUETOOTH_OFF
        if (!adapter.isMultipleAdvertisementSupported || adapter.bluetoothLeAdvertiser == null) {
            return AdvertiserAvailability.UNSUPPORTED
        }
        return AdvertiserAvailability.READY
    }

    @SuppressLint("MissingPermission") // availability()가 권한을 확인한다.
    override fun start(identifier: BeaconIdentifier, callback: BeaconAdvertiser.Callback) {
        val advertiser = adapter?.bluetoothLeAdvertiser
        if (advertiser == null) {
            callback.onStartFailed(AdvertiseCallback.ADVERTISE_FAILED_FEATURE_UNSUPPORTED)
            return
        }

        val settings = AdvertiseSettings.Builder()
            .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
            .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
            .setConnectable(false)
            .setTimeout(0)
            .build()
        val data = AdvertiseData.Builder()
            .setIncludeDeviceName(false)
            .setIncludeTxPowerLevel(false)
            .addManufacturerData(
                IBeaconPacket.APPLE_COMPANY_ID,
                IBeaconPacket.manufacturerData(identifier, measuredPower),
            )
            .build()
        val advertiseCallback = object : AdvertiseCallback() {
            override fun onStartSuccess(settingsInEffect: AdvertiseSettings?) = callback.onStarted()
            override fun onStartFailure(errorCode: Int) = callback.onStartFailed(errorCode)
        }

        activeCallback = advertiseCallback
        try {
            advertiser.startAdvertising(settings, data, advertiseCallback)
        } catch (e: SecurityException) {
            activeCallback = null
            callback.onStartFailed(AdvertiseCallback.ADVERTISE_FAILED_INTERNAL_ERROR)
        }
    }

    @SuppressLint("MissingPermission")
    override fun stop() {
        val callback = activeCallback ?: return
        activeCallback = null
        adapter?.bluetoothLeAdvertiser?.stopAdvertising(callback)
    }
}

object BeaconPermissions {
    /** 송출 전에 요청할 런타임 권한. 알림 권한은 거부해도 송출은 한다. */
    fun required(): Array<String> = buildList {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) add(Manifest.permission.BLUETOOTH_ADVERTISE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) add(Manifest.permission.POST_NOTIFICATIONS)
    }.toTypedArray()

    fun canAdvertise(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
            context.checkSelfPermission(Manifest.permission.BLUETOOTH_ADVERTISE) == PackageManager.PERMISSION_GRANTED
}
