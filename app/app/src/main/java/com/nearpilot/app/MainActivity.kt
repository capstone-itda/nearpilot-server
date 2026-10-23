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
import com.nearpilot.app.approval.ApprovalApi
import com.nearpilot.app.approval.ApprovalChoice
import com.nearpilot.app.approval.ApprovalItem
import com.nearpilot.app.approval.ApprovalStore
import com.nearpilot.app.approval.ApprovalText
import com.nearpilot.app.approval.ApprovalWatchService
import com.nearpilot.app.ui.ApprovalScreen
import com.nearpilot.app.ui.ApprovalUiState
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
        const val LOCAL_NETWORK_DENIED =
            "같은 Wi-Fi의 서버에 연결하려면 '근처 기기(로컬 네트워크)' 권한이 필요해요. 설정 > 앱 > NearPilot > 권한에서 허용해 주세요."
    }

    private enum class Tab(val label: String, val icon: String) {
        BEACON("비콘", "📡"),
        ACCOUNT("계정", "👤"),
        APPROVAL("승인", "✅"),
    }

    private val tab = mutableStateOf(Tab.BEACON)
    private lateinit var approvalStore: ApprovalStore
    private val approvalState = mutableStateOf(ApprovalUiState(connected = false, watching = false))

    private lateinit var store: IdentifierStore
    private lateinit var accounts: AccountStore
    private val accountState = mutableStateOf(AccountUiState(serverUrl = null, token = null, beaconRegistered = false))
    private val identifier = mutableStateOf<BeaconIdentifier?>(null)
    private val showIntro = mutableStateOf(false)
    private val showPermissionRationale = mutableStateOf(false)

    private var pendingNetworkAction: (() -> Unit)? = null
    private var pendingNetworkDenied: ((String) -> Unit)? = null

    private val localNetworkLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            val action = pendingNetworkAction
            val denied = pendingNetworkDenied
            pendingNetworkAction = null
            pendingNetworkDenied = null
            if (granted) action?.invoke() else denied?.invoke(LOCAL_NETWORK_DENIED)
        }

    private val watchPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { results ->
            if (results.values.all { it }) {
                ApprovalWatchService.start(this)
                refreshApprovalState("승인 알림을 켰어요. 새 요청이 오면 알려 드릴게요.")
            } else {
                refreshApprovalState(
                    "알림과 '근처 기기(로컬 네트워크)' 권한이 있어야 승인 알림을 받을 수 있어요. 설정 > 앱 > NearPilot > 권한에서 허용해 주세요.",
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
        approvalStore = ApprovalStore(this)
        identifier.value = store.loadOrCreate()
        showIntro.value = !store.introSeen
        refreshAccount()
        refreshApprovalState()
        handleApprovalIntent(intent)

        enableEdgeToEdge()
        setContent {
            NearPilotTheme {
                val state by BroadcastStatus.state.collectAsState()
                val current by identifier
                var tab by tab
                Scaffold(
                    modifier = Modifier.fillMaxSize(),
                    bottomBar = {
                        NavigationBar {
                            Tab.entries.forEach { item ->
                                NavigationBarItem(
                                    selected = tab == item,
                                    onClick = {
                                        tab = item
                                        if (item == Tab.APPROVAL) loadApprovals()
                                    },
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
                        Tab.APPROVAL -> ApprovalScreen(
                            state = approvalState.value,
                            onToggleWatch = ::toggleWatch,
                            onRefresh = ::loadApprovals,
                            onDecide = ::decide,
                            onGoToAccount = { tab = Tab.ACCOUNT },
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

    private fun withLocalNetwork(
        onDenied: (String) -> Unit = { refreshAccount(it, isError = true) },
        action: () -> Unit,
    ) {
        if (Build.VERSION.SDK_INT < LOCAL_NETWORK_SDK ||
            checkSelfPermission(Manifest.permission.ACCESS_LOCAL_NETWORK) == PackageManager.PERMISSION_GRANTED
        ) {
            action()
        } else {
            pendingNetworkAction = action
            pendingNetworkDenied = onDenied
            localNetworkLauncher.launch(Manifest.permission.ACCESS_LOCAL_NETWORK)
        }
    }

    // ── 승인 ─────────────────────────────────────────────────────────

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleApprovalIntent(intent)
    }

    private fun handleApprovalIntent(intent: Intent?) {
        if (intent?.getBooleanExtra(ApprovalWatchService.EXTRA_OPEN_APPROVALS, false) != true) return
        tab.value = Tab.APPROVAL
        approvalState.value = approvalState.value.copy(
            highlightId = intent.getStringExtra(ApprovalWatchService.EXTRA_APPROVAL_ID))
        loadApprovals()
    }

    private fun refreshApprovalState(message: String? = null, isError: Boolean = false) {
        approvalState.value = approvalState.value.copy(
            connected = accounts.serverUrl != null && accounts.token != null,
            watching = approvalStore.watching,
            message = message,
            messageIsError = isError,
        )
    }

    private fun approvalApi(): Pair<ApprovalApi, String>? {
        val server = accounts.serverUrl ?: return null
        val token = accounts.token ?: return null
        return ApprovalApi(server, UrlConnectionTransport()) to token
    }

    private fun loadApprovals() = loadApprovals(keepMessage = false)

    private fun loadApprovals(keepMessage: Boolean) {
        if (keepMessage) refreshApprovalState(approvalState.value.message, approvalState.value.messageIsError)
        else refreshApprovalState()
        val (api, token) = approvalApi() ?: return
        withLocalNetwork(onDenied = { refreshApprovalState(it, isError = true) }) {
            approvalState.value = approvalState.value.copy(loading = true)
            lifecycleScope.launch {
                val result = withContext(Dispatchers.IO) { api.list(token) }
                approvalState.value = when (result) {
                    is ApiResult.Success -> approvalState.value.copy(loading = false, items = result.value)
                    else -> approvalState.value.copy(
                        loading = false, message = ApprovalText.failure(result), messageIsError = true)
                }
            }
        }
    }

    private fun decide(item: ApprovalItem, choice: ApprovalChoice) {
        val (api, token) = approvalApi() ?: return
        withLocalNetwork(onDenied = { refreshApprovalState(it, isError = true) }) {
            approvalState.value = approvalState.value.copy(busyId = item.approvalId)
            lifecycleScope.launch {
                val result = withContext(Dispatchers.IO) { api.decide(token, item.approvalId, choice) }
                approvalState.value = approvalState.value.copy(busyId = null)
                when (result) {
                    is ApiResult.Success -> refreshApprovalState(ApprovalText.decided(choice))
                    else -> refreshApprovalState(ApprovalText.failure(result), isError = true)
                }
                // 성공이든 거부든 서버의 현재 목록으로 맞춘다. 결정 결과 문장은 남긴다.
                loadApprovals(keepMessage = true)
            }
        }
    }

    private fun toggleWatch(enabled: Boolean) {
        if (!enabled) {
            ApprovalWatchService.stop(this)
            refreshApprovalState("승인 알림을 껐어요.")
            return
        }
        val needed = buildList {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) add(Manifest.permission.POST_NOTIFICATIONS)
            if (Build.VERSION.SDK_INT >= LOCAL_NETWORK_SDK) add(Manifest.permission.ACCESS_LOCAL_NETWORK)
        }
        watchPermissionLauncher.launch(needed.toTypedArray())
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
