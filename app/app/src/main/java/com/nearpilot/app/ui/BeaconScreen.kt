package com.nearpilot.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
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
import com.nearpilot.app.beacon.BeaconIdentifier
import com.nearpilot.app.beacon.BroadcastState
import com.nearpilot.app.beacon.IdentifierField
import com.nearpilot.app.beacon.IdentifierParseResult
import com.nearpilot.app.beacon.StatusAction
import com.nearpilot.app.beacon.StatusTone
import com.nearpilot.app.beacon.presentation

@Composable
fun BeaconScreen(
    state: BroadcastState,
    identifier: BeaconIdentifier,
    showIntro: Boolean,
    onDismissIntro: () -> Unit,
    onStart: () -> Unit,
    onStop: () -> Unit,
    onAction: (StatusAction) -> Unit,
    onIdentifierChange: (BeaconIdentifier) -> Unit,
    onCopy: (label: String, text: String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("NearPilot 비콘", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text(
                "이 폰이 블루투스 신호를 보내서, 주변 기기가 내가 가까이 있다는 걸 알 수 있게 해요.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        if (showIntro) IntroCard(onDismissIntro)

        StatusCard(state, onAction)

        if (state.isActive) {
            Button(onClick = onStop, modifier = Modifier.fillMaxWidth().height(56.dp),
                colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.secondary)) {
                Text("송출 중지", style = MaterialTheme.typography.titleMedium)
            }
        } else {
            Button(onClick = onStart, modifier = Modifier.fillMaxWidth().height(56.dp)) {
                Text("송출 시작", style = MaterialTheme.typography.titleMedium)
            }
        }

        IdentifierCard(identifier, editable = !state.isActive, onIdentifierChange, onCopy)
    }
}

@Composable
private fun IntroCard(onDismiss: () -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("처음 오셨나요?", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            Text("1. '송출 시작'을 누르고 권한을 허용해 주세요.")
            Text("2. 상태가 초록색 '송출 중'이 되면 준비 끝이에요.")
            Text("3. 화면을 꺼도 알림이 떠 있는 동안 계속 신호를 보내요.")
            Text(
                "비콘 값은 자동으로 만들어 두었어요. 서버에 등록한 값이 따로 있으면 아래 '직접 입력'에서 바꿔 주세요.",
                style = MaterialTheme.typography.bodySmall,
            )
            TextButton(onClick = onDismiss, modifier = Modifier.align(Alignment.End)) { Text("알겠어요") }
        }
    }
}

@Composable
private fun StatusCard(state: BroadcastState, onAction: (StatusAction) -> Unit) {
    val p = state.presentation()
    val (dot, container) = when (p.tone) {
        StatusTone.OK -> Color(0xFF2E7D32) to Color(0xFFE8F5E9)
        StatusTone.BUSY -> Color(0xFFF9A825) to Color(0xFFFFF8E1)
        StatusTone.PROBLEM -> MaterialTheme.colorScheme.error to MaterialTheme.colorScheme.errorContainer
        StatusTone.IDLE -> MaterialTheme.colorScheme.outline to MaterialTheme.colorScheme.surfaceVariant
    }
    Card(colors = CardDefaults.cardColors(containerColor = container)) {
        Column(Modifier.padding(16.dp).fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Box(Modifier.size(14.dp).background(dot, CircleShape))
                Text(p.title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold,
                    color = Color(0xFF1C1B1F))
            }
            Text(p.message, style = MaterialTheme.typography.bodyMedium, color = Color(0xFF1C1B1F))
            actionLabel(p.action)?.let { label ->
                OutlinedButton(onClick = { onAction(p.action) }) { Text(label) }
            }
        }
    }
}

private fun actionLabel(action: StatusAction): String? = when (action) {
    StatusAction.NONE -> null
    StatusAction.OPEN_BLUETOOTH_SETTINGS -> "블루투스 설정 열기"
    StatusAction.REQUEST_PERMISSION -> "권한 허용하기"
    StatusAction.RETRY -> "다시 시도"
}

@Composable
private fun IdentifierCard(
    identifier: BeaconIdentifier,
    editable: Boolean,
    onIdentifierChange: (BeaconIdentifier) -> Unit,
    onCopy: (String, String) -> Unit,
) {
    var editing by rememberSaveable { mutableStateOf(false) }

    Card {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("내 비콘 값", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            Text(
                "서버에 계정을 등록할 때 이 세 값을 그대로 쓰세요.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            ValueRow("UUID", identifier.uuid, onCopy)
            ValueRow("Major", identifier.major.toString(), onCopy)
            ValueRow("Minor", identifier.minor.toString(), onCopy)
            OutlinedButton(
                onClick = {
                    onCopy("비콘 값", "uuid=${identifier.uuid} major=${identifier.major} minor=${identifier.minor}")
                },
                modifier = Modifier.fillMaxWidth(),
            ) { Text("세 값 한 번에 복사") }

            HorizontalDivider()
            if (!editable) {
                Text(
                    "송출 중에는 값을 바꿀 수 없어요. 바꾸려면 먼저 송출을 중지해 주세요.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            } else if (editing) {
                IdentifierEditor(identifier, onSave = { onIdentifierChange(it); editing = false },
                    onCancel = { editing = false })
            } else {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(onClick = { onIdentifierChange(BeaconIdentifier.generate()) }) { Text("새 값 만들기") }
                    TextButton(onClick = { editing = true }) { Text("직접 입력 (고급)") }
                }
            }
        }
    }
}

@Composable
private fun ValueRow(label: String, value: String, onCopy: (String, String) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(value, style = MaterialTheme.typography.bodyMedium, fontFamily = FontFamily.Monospace)
        }
        TextButton(onClick = { onCopy(label, value) }) { Text("복사") }
    }
}

@Composable
private fun IdentifierEditor(
    current: BeaconIdentifier,
    onSave: (BeaconIdentifier) -> Unit,
    onCancel: () -> Unit,
) {
    var uuid by rememberSaveable { mutableStateOf(current.uuid) }
    var major by rememberSaveable { mutableStateOf(current.major.toString()) }
    var minor by rememberSaveable { mutableStateOf(current.minor.toString()) }
    var errors by rememberSaveable { mutableStateOf(emptySet<IdentifierField>()) }

    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedTextField(
            value = uuid,
            onValueChange = { uuid = it },
            label = { Text("UUID") },
            isError = IdentifierField.UUID in errors,
            supportingText = {
                Text(if (IdentifierField.UUID in errors) "8-4-4-4-12자리 형식으로 입력해 주세요" else "예: e2c56db5-dffb-48d2-b060-d0f5a71096e0")
            },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            NumberField("Major", major, { major = it }, IdentifierField.MAJOR in errors, Modifier.weight(1f))
            NumberField("Minor", minor, { minor = it }, IdentifierField.MINOR in errors, Modifier.weight(1f))
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                when (val parsed = BeaconIdentifier.parse(uuid, major, minor)) {
                    is IdentifierParseResult.Invalid -> errors = parsed.errors
                    is IdentifierParseResult.Valid -> onSave(parsed.identifier)
                }
            }) { Text("저장") }
            TextButton(onClick = onCancel) { Text("취소") }
        }
    }
}

@Composable
private fun NumberField(
    label: String,
    value: String,
    onValueChange: (String) -> Unit,
    isError: Boolean,
    modifier: Modifier,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        isError = isError,
        supportingText = { Text(if (isError) "0~65535 숫자" else "0~65535") },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        modifier = modifier,
    )
}

/** 알림에 쓰는 한 줄 상태. */
fun BroadcastState.describe(): String = presentation().title
