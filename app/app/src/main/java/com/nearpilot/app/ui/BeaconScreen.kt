package com.nearpilot.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.nearpilot.app.beacon.AdvertiserAvailability
import com.nearpilot.app.beacon.BeaconIdentifier
import com.nearpilot.app.beacon.BroadcastState
import com.nearpilot.app.beacon.IdentifierField
import com.nearpilot.app.beacon.IdentifierParseResult
import com.nearpilot.app.beacon.Interruption
import java.util.UUID

@Composable
fun BeaconScreen(
    state: BroadcastState,
    saved: BeaconIdentifier?,
    onStart: (BeaconIdentifier) -> Unit,
    onStop: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var uuid by rememberSaveable { mutableStateOf(saved?.uuid.orEmpty()) }
    var major by rememberSaveable { mutableStateOf(saved?.major?.toString().orEmpty()) }
    var minor by rememberSaveable { mutableStateOf(saved?.minor?.toString().orEmpty()) }
    var errors by rememberSaveable { mutableStateOf(emptySet<IdentifierField>()) }
    val editable = !state.isActive

    Column(
        modifier = modifier.padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("비콘 송출", style = MaterialTheme.typography.headlineSmall)
        Text("상태: ${state.describe()}", style = MaterialTheme.typography.titleMedium)

        OutlinedTextField(
            value = uuid,
            onValueChange = { uuid = it },
            label = { Text("UUID") },
            isError = IdentifierField.UUID in errors,
            supportingText = { if (IdentifierField.UUID in errors) Text("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx 형식이어야 합니다") },
            enabled = editable,
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedButton(onClick = { uuid = UUID.randomUUID().toString() }, enabled = editable) {
            Text("UUID 새로 만들기")
        }
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            NumberField("Major", major, { major = it }, IdentifierField.MAJOR in errors, editable, Modifier.weight(1f))
            NumberField("Minor", minor, { minor = it }, IdentifierField.MINOR in errors, editable, Modifier.weight(1f))
        }

        if (state.isActive) {
            Button(onClick = onStop, modifier = Modifier.fillMaxWidth()) { Text("송출 중지") }
        } else {
            Button(
                onClick = {
                    when (val parsed = BeaconIdentifier.parse(uuid, major, minor)) {
                        is IdentifierParseResult.Invalid -> errors = parsed.errors
                        is IdentifierParseResult.Valid -> {
                            errors = emptySet()
                            uuid = parsed.identifier.uuid
                            onStart(parsed.identifier)
                        }
                    }
                },
                modifier = Modifier.fillMaxWidth(),
            ) { Text("저장하고 송출 시작") }
        }
    }
}

@Composable
private fun NumberField(
    label: String,
    value: String,
    onValueChange: (String) -> Unit,
    isError: Boolean,
    enabled: Boolean,
    modifier: Modifier,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        isError = isError,
        supportingText = { if (isError) Text("0~65535") },
        enabled = enabled,
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        modifier = modifier,
    )
}

fun BroadcastState.describe(): String = when (this) {
    BroadcastState.Stopped -> "중지"
    BroadcastState.Starting -> "시작 중"
    BroadcastState.Broadcasting -> "송출 중"
    is BroadcastState.Blocked -> "시작 못 함 — " + when (reason) {
        AdvertiserAvailability.BLUETOOTH_OFF -> "블루투스를 켜 주세요"
        AdvertiserAvailability.PERMISSION_MISSING -> "근처 기기 권한이 필요합니다"
        AdvertiserAvailability.UNSUPPORTED -> "이 폰은 BLE 광고를 지원하지 않습니다"
        AdvertiserAvailability.READY -> "알 수 없는 이유"
    }
    is BroadcastState.Interrupted -> "중단됨 — " + when (val r = reason) {
        Interruption.BluetoothOff -> "블루투스가 꺼졌습니다"
        is Interruption.StartFailed -> "광고 시작 실패 (코드 ${r.errorCode})"
        Interruption.ServiceStopped -> "송출 서비스가 종료됐습니다"
    }
}
