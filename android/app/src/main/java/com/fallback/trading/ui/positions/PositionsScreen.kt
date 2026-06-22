package com.fallback.trading.ui.positions

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Close
import androidx.compose.material.icons.outlined.SwapVert
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.fallback.trading.AppContainer
import com.fallback.trading.data.ApiResult
import com.fallback.trading.data.NotificationHelper
import com.fallback.trading.data.PositionDto
import com.fallback.trading.data.TradingRepository
import com.fallback.trading.ui.Format
import com.fallback.trading.ui.UiState
import com.fallback.trading.ui.components.EmptyState
import com.fallback.trading.ui.components.ErrorState
import com.fallback.trading.ui.components.LoadingState
import com.fallback.trading.ui.reduce
import com.fallback.trading.ui.theme.LossRed
import com.fallback.trading.ui.theme.ProfitGreen
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

sealed interface TradeEvent {
    data class Opened(val symbol: String, val side: String, val qty: Double, val price: Double) : TradeEvent
    data class Closed(val symbol: String) : TradeEvent
}

class PositionsViewModel(private val repo: TradingRepository) : ViewModel() {
    private val _state = MutableStateFlow(UiState<List<PositionDto>>(loading = true))
    val state = _state.asStateFlow()

    private val _closing = MutableStateFlow<Set<String>>(emptySet())
    val closing = _closing.asStateFlow()

    private val _message = MutableStateFlow<String?>(null)
    val message = _message.asStateFlow()

    private val _events = MutableSharedFlow<TradeEvent>(extraBufferCapacity = 16)
    val events: SharedFlow<TradeEvent> = _events.asSharedFlow()

    val isAdmin = repo.adminState.isAdmin

    private var previousKeys: Set<String> = emptySet()
    private var isFirstLoad = true

    init {
        refresh()
        viewModelScope.launch {
            repo.adminState.scope.drop(1).collect {
                isFirstLoad = true
                refresh()
            }
        }
        viewModelScope.launch {
            while (true) {
                delay(30_000)
                if (_state.value.data != null) refresh()
            }
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _state.update { it.copy(loading = true, error = null) }
            val result = repo.getOpenPositions()
            _state.update { it.reduce(result) }

            if (result is ApiResult.Success) {
                val newPositions = result.data
                val newKeys = newPositions.map { "${it.userId ?: 0}:${it.symbol}" }.toSet()

                if (!isFirstLoad) {
                    newPositions
                        .filter { "${it.userId ?: 0}:${it.symbol}" !in previousKeys }
                        .forEach { pos ->
                            _events.emit(TradeEvent.Opened(pos.symbol, pos.side, pos.qty, pos.openPrice))
                        }
                    (previousKeys - newKeys).forEach { key ->
                        _events.emit(TradeEvent.Closed(key.substringAfter(":")))
                    }
                } else {
                    isFirstLoad = false
                }
                previousKeys = newKeys
            }
        }
    }

    fun close(symbol: String, ownerUserId: Long?) {
        viewModelScope.launch {
            _closing.update { it + symbol }
            when (val r = repo.closePosition(symbol, ownerUserId)) {
                is ApiResult.Success -> {
                    _message.value = "Close order sent for $symbol."
                    refresh()
                }
                is ApiResult.Error -> _message.value = "Could not close $symbol: ${r.message}"
                ApiResult.Unauthorized -> _state.update { it.copy(sessionExpired = true) }
            }
            _closing.update { it - symbol }
        }
    }

    fun consumeMessage() { _message.value = null }

    companion object {
        fun factory(container: AppContainer) = viewModelFactory {
            initializer { PositionsViewModel(container.repository) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PositionsScreen(
    container: AppContainer,
    onTrade: (String) -> Unit,
    onSessionExpired: () -> Unit,
    viewModel: PositionsViewModel = viewModel(factory = PositionsViewModel.factory(container)),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val closing by viewModel.closing.collectAsStateWithLifecycle()
    val message by viewModel.message.collectAsStateWithLifecycle()
    val isAdmin by viewModel.isAdmin.collectAsStateWithLifecycle()
    val notifyOpened by container.settings.notifyTradeOpened.collectAsStateWithLifecycle(initialValue = true)
    val notifyClosed by container.settings.notifyTradeClosed.collectAsStateWithLifecycle(initialValue = true)
    val context = LocalContext.current

    var confirmFor by remember { mutableStateOf<PositionDto?>(null) }

    LaunchedEffect(state.sessionExpired) {
        if (state.sessionExpired) onSessionExpired()
    }

    LaunchedEffect(Unit) {
        viewModel.events.collect { event ->
            when (event) {
                is TradeEvent.Opened -> if (notifyOpened) {
                    NotificationHelper.notifyOpened(context, event.symbol, event.side, event.qty, event.price)
                }
                is TradeEvent.Closed -> if (notifyClosed) {
                    NotificationHelper.notifyClosed(context, event.symbol)
                }
            }
        }
    }

    message?.let { com.fallback.trading.ui.components.Toast(it) { viewModel.consumeMessage() } }

    confirmFor?.let { position ->
        AlertDialog(
            onDismissRequest = { confirmFor = null },
            title = { Text("Close position") },
            text = {
                val who = if (isAdmin) " (${position.username})" else ""
                Text("Submit a market order to close the entire ${position.symbol}$who position?")
            },
            confirmButton = {
                TextButton(onClick = {
                    viewModel.close(position.symbol, position.userId)
                    confirmFor = null
                }) { Text("Close ${position.symbol}") }
            },
            dismissButton = {
                TextButton(onClick = { confirmFor = null }) { Text("Cancel") }
            },
        )
    }

    PullToRefreshBox(
        isRefreshing = state.loading,
        onRefresh = viewModel::refresh,
        modifier = Modifier.fillMaxSize(),
    ) {
        val positions = state.data
        when {
            positions != null && positions.isNotEmpty() -> {
                LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(
                        start = 16.dp, end = 16.dp, top = 16.dp, bottom = 96.dp,
                    ),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    items(positions, key = { it.username + ":" + it.symbol }) { position ->
                        PositionCard(
                            position = position,
                            isClosing = closing.contains(position.symbol),
                            showUser = isAdmin,
                            onTrade = { onTrade(position.symbol) },
                            onClose = { confirmFor = position },
                        )
                    }
                }
            }
            positions != null -> EmptyState("No open positions.")
            state.loading -> LoadingState()
            state.error != null -> ErrorState(state.error!!, onRetry = viewModel::refresh)
        }
    }
}

@Composable
private fun PositionCard(
    position: PositionDto,
    isClosing: Boolean,
    showUser: Boolean,
    onTrade: () -> Unit,
    onClose: () -> Unit,
) {
    val plColor = if (position.unrealizedPl >= 0) ProfitGreen else LossRed
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column {
                    Text(position.symbol, style = MaterialTheme.typography.titleLarge)
                    if (showUser && !position.username.isNullOrBlank()) {
                        Text(
                            position.username,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                SideChip(position.side)
            }
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                LabelValue("Qty", Format.qty(position.qty))
                LabelValue("Avg cost", Format.money(position.openPrice))
                LabelValue("Last", Format.money(position.currentPrice))
            }
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.Bottom,
            ) {
                Column {
                    Text(
                        "MARKET VALUE",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Text(Format.money(position.marketValue), style = MaterialTheme.typography.titleMedium)
                }
                Column(horizontalAlignment = Alignment.End) {
                    Text(
                        "UNREALIZED P/L",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Text(
                        Format.moneySigned(position.unrealizedPl),
                        style = MaterialTheme.typography.titleMedium,
                        color = plColor,
                        fontWeight = FontWeight.SemiBold,
                    )
                }
            }
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                OutlinedButton(
                    onClick = onTrade,
                    enabled = !isClosing,
                    modifier = Modifier.weight(1f),
                ) {
                    Icon(Icons.Outlined.SwapVert, contentDescription = null, modifier = Modifier.padding(end = 8.dp))
                    Text("Trade")
                }
                OutlinedButton(
                    onClick = onClose,
                    enabled = !isClosing,
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = LossRed),
                    modifier = Modifier.weight(1f),
                ) {
                    if (isClosing) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(18.dp).padding(end = 8.dp),
                            strokeWidth = 2.dp,
                            color = LossRed,
                        )
                        Text("Closing…")
                    } else {
                        Icon(Icons.Outlined.Close, contentDescription = null, modifier = Modifier.padding(end = 8.dp))
                        Text("Close")
                    }
                }
            }
        }
    }
}

@Composable
private fun SideChip(side: String) {
    val isBuy = side.equals("buy", ignoreCase = true)
    val color = if (isBuy) ProfitGreen else LossRed
    AssistChip(
        onClick = {},
        enabled = false,
        label = { Text(if (isBuy) "LONG" else "SHORT") },
        colors = AssistChipDefaults.assistChipColors(
            disabledLabelColor = color,
            disabledContainerColor = Color.Transparent,
        ),
    )
}

@Composable
private fun LabelValue(label: String, value: String) {
    Column {
        Text(
            label.uppercase(),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(value, style = MaterialTheme.typography.bodyLarge)
    }
}
