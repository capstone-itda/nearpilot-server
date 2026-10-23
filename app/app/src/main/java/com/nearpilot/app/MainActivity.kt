package com.nearpilot.app

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import com.nearpilot.app.beacon.AdvertiserAvailability
import com.nearpilot.app.beacon.BeaconIdentifier
import com.nearpilot.app.beacon.BroadcastState
import com.nearpilot.app.beacon.IdentifierStore
import com.nearpilot.app.beacon.StatusAction
import com.nearpilot.app.ble.BeaconBroadcastService
import com.nearpilot.app.ble.BeaconPermissions
import com.nearpilot.app.ble.BroadcastStatus
import com.nearpilot.app.ui.BeaconScreen
import com.nearpilot.app.ui.theme.NearPilotTheme

class MainActivity : ComponentActivity() {

    private lateinit var store: IdentifierStore
    private val identifier = mutableStateOf<BeaconIdentifier?>(null)
    private val showIntro = mutableStateOf(false)
    private val showPermissionRationale = mutableStateOf(false)

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            if (BeaconPermissions.canAdvertise(this)) {
                startBroadcast()
            } else {
                BroadcastStatus.update(BroadcastState.Blocked(AdvertiserAvailability.PERMISSION_MISSING))
                // 다시 묻지 않음 상태면 시스템이 창을 띄우지 않는다. 앱 설정에서 직접 켜게 한다.
                if (permanentlyDenied()) openAppSettings()
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = IdentifierStore(this)
        identifier.value = store.loadOrCreate()
        showIntro.value = !store.introSeen

        enableEdgeToEdge()
        setContent {
            NearPilotTheme {
                val state by BroadcastStatus.state.collectAsState()
                val current by identifier
                Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
                    current?.let {
                        BeaconScreen(
                            state = state,
                            identifier = it,
                            showIntro = showIntro.value,
                            onDismissIntro = ::dismissIntro,
                            onStart = ::requestStart,
                            onStop = { BeaconBroadcastService.stop(this) },
                            onAction = ::handleAction,
                            onIdentifierChange = ::changeIdentifier,
                            onCopy = ::copy,
                            modifier = Modifier.padding(innerPadding),
                        )
                    }
                }
                if (showPermissionRationale.value) PermissionRationaleDialog()
            }
        }
    }

    @androidx.compose.runtime.Composable
    private fun PermissionRationaleDialog() {
        AlertDialog(
            onDismissRequest = { showPermissionRationale.value = false },
            title = { Text("권한을 허용해 주세요") },
            text = {
                Text(
                    "• 근처 기기: 이 폰이 비콘 신호를 보내는 데 필요해요. 위치를 추적하지 않아요.\n" +
                        "• 알림: 화면을 꺼도 송출 중이라는 걸 알려 드려요. 거부해도 송출은 돼요.",
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    showPermissionRationale.value = false
                    launchPermissions()
                }) { Text("계속") }
            },
            dismissButton = {
                TextButton(onClick = { showPermissionRationale.value = false }) { Text("나중에") }
            },
        )
    }

    private fun requestStart() {
        dismissIntro()
        if (BeaconPermissions.canAdvertise(this)) {
            // 알림 권한은 없어도 송출한다. 아직 묻지 않았으면 함께 묻는다.
            launchPermissions()
        } else if (permanentlyDenied()) {
            BroadcastStatus.update(BroadcastState.Blocked(AdvertiserAvailability.PERMISSION_MISSING))
        } else {
            showPermissionRationale.value = true
        }
    }

    private fun launchPermissions() {
        store.permissionAsked = true
        permissionLauncher.launch(BeaconPermissions.required())
    }

    private fun startBroadcast() {
        val current = identifier.value ?: return
        BeaconBroadcastService.start(this, current)
    }

    private fun handleAction(action: StatusAction) {
        when (action) {
            StatusAction.OPEN_BLUETOOTH_SETTINGS -> startActivity(Intent(Settings.ACTION_BLUETOOTH_SETTINGS))
            StatusAction.REQUEST_PERMISSION ->
                if (permanentlyDenied()) openAppSettings() else showPermissionRationale.value = true
            StatusAction.RETRY -> requestStart()
            StatusAction.NONE -> Unit
        }
    }

    private fun changeIdentifier(next: BeaconIdentifier) {
        store.save(next)
        identifier.value = next
        Toast.makeText(this, "비콘 값을 저장했어요", Toast.LENGTH_SHORT).show()
    }

    private fun copy(label: String, text: String) {
        getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText(label, text))
        // Android 13부터는 시스템이 복사 알림을 띄운다.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            Toast.makeText(this, "$label 복사했어요", Toast.LENGTH_SHORT).show()
        }
    }

    private fun dismissIntro() {
        store.introSeen = true
        showIntro.value = false
    }

    private fun permanentlyDenied(): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            !BeaconPermissions.canAdvertise(this) &&
            store.permissionAsked &&
            !shouldShowRequestPermissionRationale(Manifest.permission.BLUETOOTH_ADVERTISE)

    private fun openAppSettings() {
        Toast.makeText(this, "설정 > 권한에서 '근처 기기'를 허용해 주세요", Toast.LENGTH_LONG).show()
        startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", packageName, null)))
    }
}
