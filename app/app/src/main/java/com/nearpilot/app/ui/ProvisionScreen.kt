package com.nearpilot.app.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.nearpilot.app.provision.SetupNode

data class ProvisionUiState(
    val connected: Boolean,
    val scanning: Boolean = false,
    val nodes: List<SetupNode> = emptyList(),
    val busy: Boolean = false,
    val progress: Float? = null,
    val message: String? = null,
    val messageIsError: Boolean = false,
)

/**
 * 앱 BLE 기기 설정 (#234). 소유자가 기기 ID와 Wi-Fi를 입력하면 서버의 접속 설정 묶음과 함께 기기에 넣는다.
 * Wi-Fi 비밀번호와 묶음은 저장하지 않는다. 입력 칸은 화면을 떠나면 지워진다.
 */
@Composable
fun ProvisionScreen(
    state: ProvisionUiState,
    onScan: () -> Unit,
    onProvision: (address: String, nodeId: String, ssid: String, password: String) -> Unit,
    onCancel: () -> Unit,
    onGoToAccount: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var nodeId by remember { mutableStateOf("") }
    var ssid by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var selected by remember { mutableStateOf<String?>(null) }

    Column(
        modifier = modifier.verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("기기 설정", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text(
                "USB 없이 블루투스로 기기에 Wi-Fi와 서버 접속 정보를 넣어요. 설정이 없는 기기는 바로 보이고, " +
                    "이미 설정한 기기는 BOOT 버튼을 3초 누르면 5분 동안 보여요.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        if (!state.connected) {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("서버 주소와 소유자 토큰이 있어야 해요.")
                    OutlinedButton(onClick = onGoToAccount) { Text("계정 탭으로") }
                }
            }
            return@Column
        }

        OutlinedTextField(
            value = nodeId, onValueChange = { nodeId = it.trim() }, label = { Text("기기 ID (예: light-1)") },
            singleLine = true, enabled = !state.busy, modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = ssid, onValueChange = { ssid = it }, label = { Text("Wi-Fi 이름") },
            singleLine = true, enabled = !state.busy, modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = password, onValueChange = { password = it }, label = { Text("Wi-Fi 비밀번호") },
            singleLine = true, enabled = !state.busy, modifier = Modifier.fillMaxWidth(),
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
        )

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedButton(onClick = onScan, enabled = !state.busy) {
                Text(if (state.scanning) "다시 찾기" else "근처 기기 찾기")
            }
            if (state.scanning) Text("찾는 중…", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        state.nodes.forEach { node ->
            Row(
                Modifier.fillMaxWidth().clickable(enabled = !state.busy) { selected = node.address },
                verticalAlignment = Alignment.CenterVertically,
            ) {
                RadioButton(selected = selected == node.address, onClick = { selected = node.address },
                    enabled = !state.busy)
                Column {
                    Text(node.name, fontWeight = FontWeight.Medium)
                    Text("${node.address} · ${node.rssi} dBm", style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }

        val chosen = selected?.takeIf { address -> state.nodes.any { it.address == address } }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = { chosen?.let { onProvision(it, nodeId, ssid, password) } },
                enabled = !state.busy && chosen != null && nodeId.isNotEmpty() && ssid.isNotEmpty(),
            ) { Text("설정 넣기") }
            if (state.busy) OutlinedButton(onClick = onCancel) { Text("취소") }
        }

        state.progress?.let { LinearProgressIndicator(progress = { it }, modifier = Modifier.fillMaxWidth()) }
        state.message?.let {
            Text(it, color = if (state.messageIsError) MaterialTheme.colorScheme.error
            else MaterialTheme.colorScheme.onSurface)
        }
    }
}
