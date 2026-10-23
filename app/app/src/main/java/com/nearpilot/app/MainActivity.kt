package com.nearpilot.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Scaffold
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import com.nearpilot.app.beacon.AdvertiserAvailability
import com.nearpilot.app.beacon.BeaconIdentifier
import com.nearpilot.app.beacon.BroadcastState
import com.nearpilot.app.beacon.IdentifierStore
import com.nearpilot.app.ble.BeaconBroadcastService
import com.nearpilot.app.ble.BeaconPermissions
import com.nearpilot.app.ble.BroadcastStatus
import com.nearpilot.app.ui.BeaconScreen
import com.nearpilot.app.ui.theme.NearPilotTheme

class MainActivity : ComponentActivity() {

    private lateinit var store: IdentifierStore
    private var pendingStart: BeaconIdentifier? = null

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
            val identifier = pendingStart ?: return@registerForActivityResult
            pendingStart = null
            startBroadcast(identifier)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = IdentifierStore(this)
        val saved = store.load()

        enableEdgeToEdge()
        setContent {
            NearPilotTheme {
                val state by BroadcastStatus.state.collectAsState()
                Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
                    BeaconScreen(
                        state = state,
                        saved = saved,
                        onStart = ::requestStart,
                        onStop = { BeaconBroadcastService.stop(this) },
                        modifier = Modifier.padding(innerPadding),
                    )
                }
            }
        }
    }

    private fun requestStart(identifier: BeaconIdentifier) {
        store.save(identifier)
        pendingStart = identifier
        permissionLauncher.launch(BeaconPermissions.required())
    }

    private fun startBroadcast(identifier: BeaconIdentifier) {
        // 광고 권한 없이 포그라운드 서비스를 시작하면 Android 14 이상에서 실패한다.
        if (!BeaconPermissions.canAdvertise(this)) {
            BroadcastStatus.update(BroadcastState.Blocked(AdvertiserAvailability.PERMISSION_MISSING))
            return
        }
        BeaconBroadcastService.start(this, identifier)
    }
}
