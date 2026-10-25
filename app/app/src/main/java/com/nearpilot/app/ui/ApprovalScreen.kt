package com.nearpilot.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.nearpilot.app.approval.ApprovalChoice
import com.nearpilot.app.approval.ApprovalItem
import com.nearpilot.app.approval.ApprovalText
import kotlinx.coroutines.delay
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

data class ApprovalUiState(
    val connected: Boolean,
    val watching: Boolean,
    val items: List<ApprovalItem> = emptyList(),
    val loading: Boolean = false,
    val busyId: String? = null,
    val highlightId: String? = null,
    val message: String? = null,
    val messageIsError: Boolean = false,
)

private val TIME = DateTimeFormatter.ofPattern("HH:mm").withZone(ZoneId.systemDefault())

@Composable
fun ApprovalScreen(
    state: ApprovalUiState,
    onToggleWatch: (Boolean) -> Unit,
    onRefresh: () -> Unit,
    onDecide: (ApprovalItem, ApprovalChoice) -> Unit,
    onGoToAccount: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var now by remember { mutableStateOf(Instant.now()) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(1_000)
            now = Instant.now()
        }
    }

    Column(
        modifier = modifier.verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("승인", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text(
                "내 기기를 쓰고 싶다는 방문자의 요청을 승인하거나 거부해요. 기기 소유자만 쓰는 화면이에요.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        if (!state.connected) {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("먼저 계정을 연결해 주세요", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                    Text("계정 탭에서 서버 주소를 저장하고, 시드 스크립트가 알려 준 소유자 토큰을 넣어 주세요.")
                    Button(onClick = onGoToAccount) { Text("계정 탭으로") }
                }
            }
            return@Column
        }

        state.message?.let {
            val container = if (state.messageIsError) MaterialTheme.colorScheme.errorContainer else Color(0xFFE8F5E9)
            Card(modifier = Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = container)) {
                Text(it, Modifier.padding(16.dp), color = Color(0xFF1C1B1F))
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("승인 알림 받기", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                    Text(
                        if (state.watching) "앱을 닫아도 새 요청이 오면 알림으로 알려 드려요."
                        else "켜 두면 앱을 닫아도 새 요청을 알림으로 받아요.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Switch(checked = state.watching, onCheckedChange = onToggleWatch)
            }
        }

        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "대기 중인 요청 ${state.items.size}건",
                Modifier.weight(1f),
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            OutlinedButton(onClick = onRefresh, enabled = !state.loading) {
                Text(if (state.loading) "불러오는 중…" else "새로 고침")
            }
        }

        if (state.items.isEmpty() && !state.loading) {
            Text(
                "지금은 대기 중인 요청이 없어요.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        state.items.forEach { item ->
            ApprovalCard(item, now, highlighted = item.approvalId == state.highlightId,
                busy = state.busyId == item.approvalId, onDecide = onDecide)
        }
    }
}

@Composable
private fun ApprovalCard(
    item: ApprovalItem,
    now: Instant,
    highlighted: Boolean,
    busy: Boolean,
    onDecide: (ApprovalItem, ApprovalChoice) -> Unit,
) {
    val expired = ApprovalText.isExpired(item, now)
    val container = if (highlighted) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceVariant
    Card(modifier = Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = container)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(ApprovalText.summary(item), style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            Text("기기: ${item.nodeId} · 동작: ${ApprovalText.actionLabel(item.action)}")
            item.createdAt?.let { Text("요청 시각: ${TIME.format(it)}", style = MaterialTheme.typography.bodySmall) }
            val remaining = ApprovalText.remaining(item.pendingExpiresAt, now)
            if (remaining.isNotEmpty()) {
                Text(
                    remaining,
                    style = MaterialTheme.typography.bodySmall,
                    color = if (expired) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (expired) {
                Text("만료된 요청이라 결정할 수 없어요. 방문자가 다시 요청해야 해요.", style = MaterialTheme.typography.bodySmall)
            } else {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { onDecide(item, ApprovalChoice.APPROVE) }, enabled = !busy, modifier = Modifier.weight(1f)) {
                        Text(if (busy) "보내는 중…" else "승인")
                    }
                    Button(
                        onClick = { onDecide(item, ApprovalChoice.DENY) },
                        enabled = !busy,
                        modifier = Modifier.weight(1f),
                        colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error),
                    ) { Text("거부") }
                }
            }
        }
    }
}
