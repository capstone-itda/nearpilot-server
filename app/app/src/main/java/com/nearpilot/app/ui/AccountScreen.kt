package com.nearpilot.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.nearpilot.app.account.AccountApi
import com.nearpilot.app.account.ServerAddress

data class AccountUiState(
    val serverUrl: String?,
    val token: String?,
    val beaconRegistered: Boolean,
    val busy: Boolean = false,
    val message: String? = null,
    val messageIsError: Boolean = false,
)

@Composable
fun AccountScreen(
    state: AccountUiState,
    onSaveServer: (String) -> Unit,
    onCreateAccount: (String) -> Unit,
    onSaveToken: (String) -> Unit,
    onRegisterBeacon: () -> Unit,
    onClearToken: () -> Unit,
    onCopy: (label: String, text: String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier.verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("계정", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text(
                "서버에 계정을 만들면 AI 비서(Claude 등)에 넣을 토큰을 받아요. 위에서부터 차례대로 진행해 주세요.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        state.message?.let { MessageBanner(it, state.messageIsError) }

        ServerCard(state.serverUrl, state.busy, onSaveServer)

        val serverReady = state.serverUrl != null
        AccountCard(state, serverReady, onCreateAccount, onSaveToken, onClearToken, onCopy)

        if (state.token != null) {
            BeaconSyncCard(state.beaconRegistered, state.busy, onRegisterBeacon)
            if (state.serverUrl != null) HostCard(state.serverUrl, state.token, onCopy)
        }
    }
}

@Composable
private fun MessageBanner(message: String, isError: Boolean) {
    val container = if (isError) MaterialTheme.colorScheme.errorContainer else Color(0xFFE8F5E9)
    Card(modifier = Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = container)) {
        Text(message, Modifier.padding(16.dp).fillMaxWidth(), color = Color(0xFF1C1B1F))
    }
}

@Composable
private fun StepTitle(text: String) {
    Text(text, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
}

@Composable
private fun Hint(text: String) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

@Composable
private fun ServerCard(serverUrl: String?, busy: Boolean, onSave: (String) -> Unit) {
    var input by rememberSaveable(serverUrl) { mutableStateOf(serverUrl.orEmpty()) }
    val invalid = input.isNotBlank() && ServerAddress.normalize(input) == null
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            StepTitle("1. 서버 주소")
            Hint("서버를 켠 PC의 주소를 넣어 주세요. 폰과 서버가 같은 Wi-Fi(독립 AP)에 있어야 해요.")
            OutlinedTextField(
                value = input,
                onValueChange = { input = it },
                label = { Text("서버 주소") },
                placeholder = { Text("예: 192.168.0.10:8000") },
                isError = invalid,
                supportingText = { if (invalid) Text("주소 형식이 맞지 않아요") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                modifier = Modifier.fillMaxWidth(),
            )
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(onClick = { onSave(input) }, enabled = !busy && input.isNotBlank() && !invalid) { Text("저장") }
                if (serverUrl != null) Hint("저장됨: $serverUrl")
            }
        }
    }
}

@Composable
private fun AccountCard(
    state: AccountUiState,
    serverReady: Boolean,
    onCreateAccount: (String) -> Unit,
    onSaveToken: (String) -> Unit,
    onClearToken: () -> Unit,
    onCopy: (String, String) -> Unit,
) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            StepTitle("2. 계정")
            when {
                !serverReady -> Hint("먼저 서버 주소를 저장해 주세요.")
                state.token == null -> NewAccountForm(state.busy, onCreateAccount, onSaveToken)
                else -> ConnectedAccount(state.token, onClearToken, onCopy)
            }
        }
    }
}

@Composable
private fun NewAccountForm(busy: Boolean, onCreateAccount: (String) -> Unit, onSaveToken: (String) -> Unit) {
    var name by rememberSaveable { mutableStateOf("") }
    var showTokenInput by rememberSaveable { mutableStateOf(false) }
    var tokenInput by rememberSaveable { mutableStateOf("") }
    val nameValid = AccountApi.normalizeName(name) != null

    Hint("이름을 넣고 계정을 만들면 토큰을 받고, 지금 송출하는 비콘도 서버에 함께 등록해요.")
    OutlinedTextField(
        value = name,
        onValueChange = { name = it },
        label = { Text("이름") },
        placeholder = { Text("예: 홍길동") },
        supportingText = { Text("1~64자. 다른 사람에게 보이지 않아요.") },
        singleLine = true,
        modifier = Modifier.fillMaxWidth(),
    )
    Button(onClick = { onCreateAccount(name) }, enabled = !busy && nameValid, modifier = Modifier.fillMaxWidth()) {
        Text(if (busy) "만드는 중…" else "계정 만들기")
    }

    HorizontalDivider(Modifier.padding(vertical = 4.dp))
    if (showTokenInput) {
        Hint("소유자는 시드 스크립트가 알려 준 토큰을, 다른 폰에서 만든 계정은 그 토큰을 넣어 주세요.")
        OutlinedTextField(
            value = tokenInput,
            onValueChange = { tokenInput = it },
            label = { Text("토큰") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { onSaveToken(tokenInput.trim()) }, enabled = tokenInput.isNotBlank()) { Text("토큰 저장") }
            TextButton(onClick = { showTokenInput = false }) { Text("취소") }
        }
    } else {
        TextButton(onClick = { showTokenInput = true }) { Text("이미 토큰이 있어요") }
    }
}

@Composable
private fun ConnectedAccount(token: String, onClearToken: () -> Unit, onCopy: (String, String) -> Unit) {
    var reveal by rememberSaveable { mutableStateOf(false) }
    var confirmClear by rememberSaveable { mutableStateOf(false) }

    Text("계정이 연결됐어요", color = Color(0xFF2E7D32), fontWeight = FontWeight.Bold)
    Text("토큰", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    Text(if (reveal) token else maskToken(token), fontFamily = FontFamily.Monospace)
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = { reveal = !reveal }) { Text(if (reveal) "가리기" else "보기") }
        OutlinedButton(onClick = { onCopy("토큰", token) }) { Text("복사") }
    }
    Hint("토큰은 비밀번호와 같아요. 다른 사람에게 보여 주지 마세요.")
    TextButton(onClick = { confirmClear = true }) { Text("토큰 지우기", color = MaterialTheme.colorScheme.error) }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("토큰을 지울까요?") },
            text = { Text("토큰은 서버에서 다시 받을 수 없어요. 복사해 두지 않았다면 새 계정을 만들어야 해요.") },
            confirmButton = {
                TextButton(onClick = { confirmClear = false; onClearToken() }) {
                    Text("지우기", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("취소") } },
        )
    }
}

private fun maskToken(token: String): String =
    if (token.length <= 4) "••••" else "••••••••" + token.takeLast(4)

@Composable
private fun BeaconSyncCard(registered: Boolean, busy: Boolean, onRegister: () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            StepTitle("3. 비콘 서버 등록")
            if (registered) {
                Text("서버에 등록됨", color = Color(0xFF2E7D32), fontWeight = FontWeight.Bold)
                Hint("지금 송출하는 비콘이 서버에 등록돼 있어요.")
            } else {
                Text("서버 등록 필요", color = MaterialTheme.colorScheme.error, fontWeight = FontWeight.Bold)
                Hint("지금 송출하는 비콘이 서버에 등록돼 있지 않아요. 등록해야 서버가 나를 알아봐요.")
                Button(onClick = onRegister, enabled = !busy) { Text(if (busy) "등록하는 중…" else "지금 등록") }
            }
        }
    }
}

@Composable
private fun HostCard(serverUrl: String, token: String, onCopy: (String, String) -> Unit) {
    val mcpUrl = ServerAddress.mcpUrl(serverUrl)
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            StepTitle("AI 비서 연결 정보")
            Hint("Claude 같은 AI 비서의 MCP 연결 설정에 아래 두 값을 넣으면, 말로 주변 기기를 쓸 수 있어요.")
            Text("MCP 주소", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(mcpUrl, Modifier.weight(1f), fontFamily = FontFamily.Monospace)
                TextButton(onClick = { onCopy("MCP 주소", mcpUrl) }) { Text("복사") }
            }
            Text("인증", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Bearer 토큰", Modifier.weight(1f))
                TextButton(onClick = { onCopy("토큰", token) }) { Text("토큰 복사") }
            }
        }
    }
}
