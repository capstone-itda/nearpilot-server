package com.nearpilot.app

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
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
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.lifecycleScope
import com.nearpilot.app.account.AccountApi
import com.nearpilot.app.account.AccountStore
import com.nearpilot.app.account.ApiResult
import com.nearpilot.app.account.BeaconSync
import com.nearpilot.app.account.ReasonMessages
import com.nearpilot.app.account.ServerAddress
import com.nearpilot.app.account.UrlConnectionTransport
import com.nearpilot.app.ui.AccountScreen
import com.nearpilot.app.ui.AccountUiState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
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

    private companion object {
        /** Android 17. 이 버전부터 사설 IP 접속에 ACCESS_LOCAL_NETWORK 권한이 필요하다. */
        const val LOCAL_NETWORK_SDK = 37
    }

    private enum class Tab(val label: String, val icon: String) {
        BEACON("비콘", "📡"),
        ACCOUNT("계정", "👤"),
    }

    private lateinit var store: IdentifierStore
    private lateinit var accounts: AccountStore
    private val accountState = mutableStateOf(AccountUiState(serverUrl = null, token = null, beaconRegistered = false))
    private val identifier = mutableStateOf<BeaconIdentifier?>(null)
    private val showIntro = mutableStateOf(false)
    private val showPermissionRationale = mutableStateOf(false)

    private var pendingNetworkAction: (() -> Unit)? = null

    private val localNetworkLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            val action = pendingNetworkAction
            pendingNetworkAction = null
            if (granted) {
                action?.invoke()
            } else {
                refreshAccount(
                    "같은 Wi-Fi의 서버에 연결하려면 '근처 기기(로컬 네트워크)' 권한이 필요해요. 설정 > 앱 > NearPilot > 권한에서 허용해 주세요.",
                    isError = true,
                )
            }
        }

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
        accounts = AccountStore(this)
        identifier.value = store.loadOrCreate()
        showIntro.value = !store.introSeen
        refreshAccount()

        enableEdgeToEdge()
        setContent {
            NearPilotTheme {
                val state by BroadcastStatus.state.collectAsState()
                val current by identifier
                var tab by rememberSaveable { mutableStateOf(Tab.BEACON) }
                Scaffold(
                    modifier = Modifier.fillMaxSize(),
                    bottomBar = {
                        NavigationBar {
                            Tab.entries.forEach { item ->
                                NavigationBarItem(
                                    selected = tab == item,
                                    onClick = { tab = item },
                                    icon = { Text(item.icon) },
                                    label = { Text(item.label) },
                                )
                            }
                        }
                    },
                ) { innerPadding ->
                    val contentModifier = Modifier.padding(innerPadding)
                    when (tab) {
                        Tab.BEACON -> current?.let {
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
                                modifier = contentModifier,
                            )
                        }
                        Tab.ACCOUNT -> AccountScreen(
                            state = accountState.value,
                            onSaveServer = ::saveServer,
                            onCreateAccount = ::createAccount,
                            onSaveToken = ::saveToken,
                            onRegisterBeacon = ::registerBeacon,
                            onClearToken = ::clearToken,
                            onCopy = ::copy,
                            modifier = contentModifier,
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
        refreshAccount()
        val hint = if (accounts.token != null) " 계정 탭에서 서버에 다시 등록해 주세요." else ""
        Toast.makeText(this, "비콘 값을 저장했어요.$hint", Toast.LENGTH_SHORT).show()
    }

    // ── 계정 ─────────────────────────────────────────────────────────

    private fun refreshAccount(message: String? = null, isError: Boolean = false, busy: Boolean = false) {
        val current = identifier.value
        accountState.value = AccountUiState(
            serverUrl = accounts.serverUrl,
            token = accounts.token,
            beaconRegistered = current != null && BeaconSync.isRegistered(accounts.registeredBeacon, current),
            busy = busy,
            message = message,
            messageIsError = isError,
        )
    }

    private fun api(): AccountApi? = accounts.serverUrl?.let { AccountApi(it, UrlConnectionTransport()) }

    private fun saveServer(input: String) {
        val normalized = ServerAddress.normalize(input)
        if (normalized == null) {
            refreshAccount("서버 주소 형식이 맞지 않아요. 예: 192.168.0.10:8000", isError = true)
            return
        }
        accounts.serverUrl = normalized
        refreshAccount("서버 주소를 저장했어요.")
    }

    private fun withLocalNetwork(action: () -> Unit) {
        if (Build.VERSION.SDK_INT < LOCAL_NETWORK_SDK ||
            checkSelfPermission(Manifest.permission.ACCESS_LOCAL_NETWORK) == PackageManager.PERMISSION_GRANTED
        ) {
            action()
        } else {
            pendingNetworkAction = action
            localNetworkLauncher.launch(Manifest.permission.ACCESS_LOCAL_NETWORK)
        }
    }

    private fun createAccount(input: String) = withLocalNetwork {
        val api = api() ?: return@withLocalNetwork
        val name = AccountApi.normalizeName(input)
        if (name == null) {
            refreshAccount(ReasonMessages.ofReason("INVALID_INPUT"), isError = true)
            return@withLocalNetwork
        }
        refreshAccount(busy = true)
        lifecycleScope.launch {
            when (val created = withContext(Dispatchers.IO) { api.createAccount(name) }) {
                is ApiResult.Success -> {
                    // 토큰은 다시 받을 수 없어서 비콘 등록 결과와 관계없이 먼저 저장한다.
                    accounts.token = created.value
                    registerBeaconWith(api, created.value, successMessage = "계정을 만들고 비콘도 서버에 등록했어요.")
                }
                else -> refreshAccount(ReasonMessages.of(created), isError = true)
            }
        }
    }

    private fun saveToken(token: String) {
        if (token.isBlank()) return
        accounts.token = token
        refreshAccount("토큰을 저장했어요. 아래에서 비콘을 서버에 등록해 주세요.")
    }

    private fun registerBeacon() = withLocalNetwork {
        val api = api() ?: return@withLocalNetwork
        val token = accounts.token ?: return@withLocalNetwork
        refreshAccount(busy = true)
        lifecycleScope.launch { registerBeaconWith(api, token, successMessage = "비콘을 서버에 등록했어요.") }
    }

    private suspend fun registerBeaconWith(api: AccountApi, token: String, successMessage: String) {
        val current = identifier.value ?: return
        when (val result = withContext(Dispatchers.IO) { api.putBeacon(token, current) }) {
            is ApiResult.Success -> {
                accounts.markRegistered(current)
                refreshAccount(successMessage)
            }
            else -> refreshAccount(ReasonMessages.of(result), isError = true)
        }
    }

    private fun clearToken() {
        accounts.token = null
        refreshAccount("토큰을 지웠어요.")
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
